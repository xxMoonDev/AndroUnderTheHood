"""SSH / SFTP backend for AndroUnderTheHood.

Three ways of reaching the Android filesystem are supported:

* ``sftp``       – the normal SFTP subsystem, running as the login user.
* ``su-sftp``    – an ``sftp-server`` binary started through ``su`` so the
                   whole SFTP session runs as root (needs sftp-server on the
                   device, e.g. Termux's openssh package).
* ``shell``      – no SFTP at all: every operation is a shell command run
                   through ``su`` (toybox ``stat``/``cat``/``du``...).  Works on
                   any rooted device that has an SSH server with exec support.
"""
from __future__ import annotations

import base64
import hashlib
import os
import posixpath
import socket
import stat as statmod
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

import logging

import paramiko

log = logging.getLogger("androunderthehood")

MODE_SFTP = "sftp"
MODE_SU_SFTP = "su-sftp"
MODE_SHELL = "shell"

MODE_LABELS = {
    MODE_SFTP: "SFTP (as login user / root login)",
    MODE_SU_SFTP: "SFTP as root via su (needs sftp-server on device)",
    MODE_SHELL: "Root shell via su (no SFTP needed)",
}

SFTP_SERVER_CANDIDATES = [
    "/data/data/com.termux/files/usr/libexec/sftp-server",
    "/system/xbin/sftp-server",
    "/system/bin/sftp-server",
    "/system/libexec/sftp-server",
    "/data/adb/modules/ssh/usr/libexec/sftp-server",
    "/sbin/sftp-server",
    "/usr/libexec/sftp-server",
    "/usr/lib/openssh/sftp-server",
    "/usr/lib/ssh/sftp-server",
]


ROOT_REQUEST = "ROOTSSH_ROOT"


def shq(s: str) -> str:
    """Quote a string for a POSIX shell."""
    return "'" + s.replace("'", "'\\''") + "'"


class BackendError(Exception):
    pass


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class Entry:
    name: str
    path: str
    size: int = 0
    mode: int = 0
    mtime: int | None = None
    atime: int | None = None
    uid: int | None = None
    gid: int | None = None
    owner: str | None = None
    group: str | None = None
    link_target: str | None = None
    target_is_dir: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def is_link(self) -> bool:
        return statmod.S_ISLNK(self.mode)

    @property
    def is_dir(self) -> bool:
        return statmod.S_ISDIR(self.mode) or (self.is_link and self.target_is_dir)

    @property
    def perm_string(self) -> str:
        return statmod.filemode(self.mode) if self.mode else "?"

    @property
    def perm_octal(self) -> str:
        return format(statmod.S_IMODE(self.mode), "04o")

    @property
    def kind(self) -> str:
        m = self.mode
        if statmod.S_ISLNK(m):
            return "Link to folder" if self.target_is_dir else "Symbolic link"
        if statmod.S_ISDIR(m):
            return "Folder"
        if statmod.S_ISCHR(m):
            return "Character device"
        if statmod.S_ISBLK(m):
            return "Block device"
        if statmod.S_ISFIFO(m):
            return "FIFO / pipe"
        if statmod.S_ISSOCK(m):
            return "Socket"
        return "File"

    @property
    def is_regular(self) -> bool:
        return statmod.S_ISREG(self.mode) or (self.is_link and not self.target_is_dir)


@dataclass
class Profile:
    name: str = ""
    host: str = ""
    port: int = 22
    username: str = "root"
    key_path: str = ""
    mode: str = MODE_SFTP
    su_command: str = "su -c"
    sftp_server: str = ""          # blank = auto detect
    start_dir: str = "/sdcard"

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        p = cls()
        for k, v in d.items():
            if hasattr(p, k):
                setattr(p, k, v)
        p.port = int(p.port or 22)
        return p


def default_key_paths() -> list[str]:
    d = os.path.join(os.path.expanduser("~"), ".ssh")
    return [os.path.join(d, n) for n in ("id_ed25519", "id_ecdsa", "id_rsa")
            if os.path.isfile(os.path.join(d, n))]


def fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


# --------------------------------------------------------------------------
# Connection (one SSH transport, many channels)
# --------------------------------------------------------------------------
class Connection:
    def __init__(self, profile: Profile):
        self.profile = profile
        self.transport: paramiko.Transport | None = None
        self.sftp_server_path: str | None = None
        self.is_root: bool | None = None
        self._fs_lock = threading.Lock()
        self._extra_fs: list[FileSystem] = []
        self.use_pty = False
        self.used_key: str | None = None  # default ~/.ssh key that logged in
        self.native_root = False   # login shell / exec is already uid 0 (root login, AndroOpenHeart)
        self.ns_prefix = ""        # "nsenter -t 1 -m -- " when su is stuck in an app namespace
        # AndroOpenHeart 1.3+: root channels ask the server to run them through its own su
        # (env ROOTSSH_ROOT=1). Needed where su from a Shizuku session is root without
        # capabilities (Magisk 30).
        self.server_root = False
        self._tls = threading.local()
        self._shells: list[PtyShell] = []
        # what the session can do without su: "shell" (uid 2000: ADB / AndroOpenHeart via Shizuku),
        # plus AndroOpenHeart's granted permissions (logs, dump, packages, storage, ...)
        self.caps: set[str] = set()
        self.login_uid: int | None = None
        self.openheart_mode = ""

    # ---- phase 1: TCP + key exchange -------------------------------------
    def open(self, timeout: float = 15) -> paramiko.PKey:
        p = self.profile
        sock = socket.create_connection((p.host, int(p.port)), timeout=timeout)
        t = paramiko.Transport(sock)
        t.set_keepalive(30)
        t.start_client(timeout=timeout)
        self.transport = t
        return t.get_remote_server_key()

    # ---- phase 2: authentication ----------------------------------------
    def authenticate(self, password: str = "", passphrase: str = "") -> None:
        t = self.transport
        assert t is not None
        p = self.profile
        errors: list[str] = []
        if p.key_path:
            try:
                key = paramiko.PKey.from_path(p.key_path, passphrase or None)
                t.auth_publickey(p.username, key)
            except Exception as e:  # noqa: BLE001
                errors.append(f"key: {e}")
        if not t.is_authenticated():
            # try keys from the local ssh agent
            try:
                for key in paramiko.Agent().get_keys():
                    try:
                        t.auth_publickey(p.username, key)
                        break
                    except paramiko.AuthenticationException:
                        continue
            except Exception:  # noqa: BLE001
                pass
        if not t.is_authenticated():
            # like OpenSSH: try the standard keys in ~/.ssh
            for path in default_key_paths():
                if p.key_path and os.path.normcase(os.path.abspath(path)) == \
                        os.path.normcase(os.path.abspath(p.key_path)):
                    continue  # already tried
                try:
                    key = paramiko.PKey.from_path(path, passphrase or None)
                    t.auth_publickey(p.username, key)
                    log.info("logged in with %s", path)
                    self.used_key = path
                    break
                except paramiko.PasswordRequiredException:
                    errors.append(f"{os.path.basename(path)}: needs passphrase")
                except paramiko.AuthenticationException:
                    continue
                except Exception as e:  # noqa: BLE001
                    log.info("default key %s unusable: %r", path, e)
                if not t.is_active():
                    break
        if not t.is_authenticated() and password:
            try:
                t.auth_password(p.username, password)
            except paramiko.BadAuthenticationType as e:
                if "keyboard-interactive" in e.allowed_types:
                    try:
                        t.auth_interactive(p.username, lambda title, instr, prompts: [password] * len(prompts))
                    except Exception as e2:  # noqa: BLE001
                        errors.append(f"keyboard-interactive: {e2}")
                else:
                    errors.append(f"password not allowed (server accepts: {', '.join(e.allowed_types)})")
            except Exception as e:  # noqa: BLE001
                errors.append(f"password: {e}")
        if not t.is_authenticated() and not password and not p.key_path:
            try:
                t.auth_none(p.username)
            except Exception as e:  # noqa: BLE001
                errors.append(f"none: {e}")
        if not t.is_authenticated():
            raise BackendError("Authentication failed. " + "; ".join(errors))

    def close(self) -> None:
        for fs in self._extra_fs:
            try:
                fs.close()
            except Exception:  # noqa: BLE001
                pass
        self._extra_fs.clear()
        for sh in self._shells:
            sh.close()
        self._shells.clear()
        if self.transport:
            self.transport.close()
            self.transport = None

    @property
    def alive(self) -> bool:
        return bool(self.transport and self.transport.is_active())

    # ---- command execution ---------------------------------------------
    def open_channel(self, as_root: bool = True) -> paramiko.Channel:
        """New session channel; root channels carry AndroOpenHeart's root request when in use."""
        chan = self.transport.open_session(timeout=15)
        if as_root and self.server_root:
            chan.update_environment({ROOT_REQUEST: "1"})
        return chan

    def wrap_root(self, cmd: str) -> str:
        """Wrap a shell command so it runs as root (if the profile uses su)."""
        if self.ns_prefix:  # escape the SSH app's isolated mount namespace
            cmd = f"{self.ns_prefix}sh -c {shq(cmd)}"
        if self.native_root:  # server already runs our commands as root
            return cmd
        if self.profile.mode in (MODE_SU_SFTP, MODE_SHELL) and self.profile.su_command.strip():
            return f"{self.profile.su_command.strip()} {shq(cmd)}"
        return cmd

    def detect_isolated_ns(self) -> None:
        """Android 11+ gives each app a private mount namespace in which /data/data only
        lists a few packages; `su` launched from an SSH app inherits it. If init's
        (global) namespace shows more, run every root command through nsenter."""
        try:
            rc, out, _ = self.exec(
                "a=$(ls -a /data/data 2>/dev/null | wc -l); "
                "b=$(nsenter -t 1 -m -- ls -a /data/data 2>/dev/null | wc -l); echo \"$a $b\"", timeout=30)
            a, b = (int(x) for x in out.split()[:2])
            log.info("/data/data entries: current namespace=%s, init namespace=%s", a, b)
            if b > a:
                self.ns_prefix = "nsenter -t 1 -m -- "
                for sh in self._shells:  # existing shells were started without it
                    sh.close()
        except Exception as e:  # noqa: BLE001
            log.info("namespace check failed: %r", e)

    def probe_exec(self) -> bool:
        """Some Android SSH servers refuse 'exec' requests but allow an interactive
        shell. Detect that and switch to running commands through a PTY shell."""
        try:
            chan = self.transport.open_session(timeout=15)
            try:
                chan.exec_command("true")
                chan.recv_exit_status()
            finally:
                chan.close()
            self.use_pty = False
        except Exception as e:  # noqa: BLE001
            log.info("exec channel refused (%r) - using interactive shell for commands", e)
            self.use_pty = True
        return not self.use_pty

    def _pty_shell(self, as_root: bool) -> "PtyShell":
        root = as_root and not self.native_root and self.profile.mode in (MODE_SU_SFTP, MODE_SHELL) \
            and bool(self.profile.su_command.strip())
        key = "root" if root else "user"
        sh = getattr(self._tls, key, None)
        if sh is None or not sh.alive:
            sh = PtyShell(self, root)
            setattr(self._tls, key, sh)
            with self._fs_lock:
                self._shells.append(sh)
        return sh

    def exec(self, cmd: str, timeout: float = 60, stdin: bytes | None = None,
             as_root: bool = True) -> tuple[int, bytes, bytes]:
        if not self.alive:
            raise BackendError("Not connected")
        if self.use_pty:
            if stdin is not None:
                raise BackendError("stdin not supported over interactive shell")
            return self._pty_shell(as_root).run(cmd, timeout)
        chan = self.open_channel(as_root)
        chan.settimeout(timeout)
        try:
            chan.exec_command(self.wrap_root(cmd) if as_root else cmd)
            if stdin is not None:
                chan.sendall(stdin)
            chan.shutdown_write()
            out = bytearray()
            err = bytearray()
            deadline = time.monotonic() + timeout
            while True:
                if chan.recv_ready():
                    out += chan.recv(262144)
                    continue
                if chan.recv_stderr_ready():
                    err += chan.recv_stderr(65536)
                    continue
                if chan.exit_status_ready() or chan.closed:
                    while chan.recv_ready():
                        out += chan.recv(262144)
                    while chan.recv_stderr_ready():
                        err += chan.recv_stderr(65536)
                    break
                if time.monotonic() > deadline:
                    raise BackendError(f"Command timed out after {timeout:.0f}s")
                time.sleep(0.004)
            rc = chan.recv_exit_status()
            log.debug("exec rc=%s cmd=%r out=%r err=%r", rc, cmd[:300], bytes(out[:300]), bytes(err[:300]))
            return rc, bytes(out), bytes(err)
        finally:
            chan.close()

    def exec_stream_out(self, cmd: str, sink: Callable[[bytes], None], timeout: float = 120) -> int:
        """Run command, streaming stdout chunks to ``sink``. Returns exit code."""
        if self.use_pty:
            rc, out, err = self.exec(cmd, timeout=timeout)
            if rc != 0:
                raise BackendError((out + err).decode(errors="replace").strip() or f"exit code {rc}")
            sink(out)
            return rc
        chan = self.open_channel()
        chan.settimeout(timeout)
        try:
            chan.exec_command(self.wrap_root(cmd))
            chan.shutdown_write()
            err = bytearray()
            while True:
                data = chan.recv(262144)
                if not data:
                    break
                sink(data)
                while chan.recv_stderr_ready():
                    err += chan.recv_stderr(65536)
            rc = chan.recv_exit_status()
            if rc != 0:
                raise BackendError(err.decode(errors="replace").strip() or f"exit code {rc}")
            return rc
        finally:
            chan.close()

    # ---- filesystem sessions ------------------------------------------
    def detect_sftp_server(self) -> str:
        if self.profile.sftp_server.strip():
            return self.profile.sftp_server.strip()
        cands = " ".join(shq(c) for c in SFTP_SERVER_CANDIDATES)
        script = (
            f"for p in {cands}; do [ -x \"$p\" ] && {{ echo \"$p\"; exit 0; }}; done; "
            "p=$(command -v sftp-server 2>/dev/null) && [ -n \"$p\" ] && { echo \"$p\"; exit 0; }; "
            "for p in /data/adb/modules/*/system/bin/sftp-server /data/adb/modules/*/usr/libexec/sftp-server "
            "/data/adb/modules/*/system/xbin/sftp-server; do [ -x \"$p\" ] && { echo \"$p\"; exit 0; }; done; exit 1"
        )
        rc, out, err = self.exec(script, timeout=30)
        path = out.decode(errors="replace").strip().splitlines()
        if rc != 0 or not path:
            path = self._sftp_server_from_proc()
        if not path:
            raise BackendError(
                "Could not find an sftp-server binary on the device (as root).\n"
                "Install Termux's 'openssh' package, enter the path manually, or use the "
                "'Root shell via su' mode.\n" + err.decode(errors="replace").strip())
        return path[-1]

    def _sftp_server_from_proc(self) -> list[str]:
        """Find the sftp-server binary the SSH app itself uses.

        Apps like SimpleSSHD ship it inside their APK's lib folder under an odd
        name. We open a normal (non-root) SFTP session so the server spawns its
        sftp process, then look up that process's executable as root.
        """
        tmp = None
        try:
            tmp = self.transport.open_sftp_client()
        except Exception:  # noqa: BLE001
            pass
        try:
            script = (
                "for p in /proc/[0-9]*; do e=$(readlink \"$p/exe\" 2>/dev/null) || continue; "
                "case \"$e\" in *sftp*) echo \"$e\";; esac; done; "
                "find /data/app -name '*sftp-server*' -type f 2>/dev/null | head -n 3"
            )
            rc, out, _ = self.exec(script, timeout=45)
            seen = []
            for line in out.decode(errors="replace").splitlines():
                line = line.strip().replace(" (deleted)", "")
                if line and line not in seen:
                    seen.append(line)
            return seen[:1]
        except Exception:  # noqa: BLE001
            return []
        finally:
            if tmp:
                tmp.close()

    def detect_caps(self) -> set[str]:
        """Probe the unprivileged session: its uid and what AndroOpenHeart reports it may do."""
        try:
            rc, out, _ = self.exec('id -u; echo "M=$ROOTSSH_MODE"; echo "P=$ROOTSSH_PERMS"; '
                                   '[ -x /vendor/bin/diag_socket_log ] && echo "P=diag"',
                                   timeout=30, as_root=False)
            lines = out.decode(errors="replace").split("\n")
            self.login_uid = int(lines[0].strip()) if lines and lines[0].strip().isdigit() else None
            for ln in lines[1:]:
                if ln.startswith("M="):
                    self.openheart_mode = ln[2:].strip()
                elif ln.startswith("P="):
                    self.caps |= {c for c in ln[2:].strip().split(",") if c}
            if self.login_uid == 2000:
                self.caps.add("shell")
        except Exception as e:  # noqa: BLE001
            log.info("capability probe failed: %r", e)
        log.info("session caps: uid=%s mode=%s caps=%s", self.login_uid, self.openheart_mode, sorted(self.caps))
        return self.caps

    def _try_server_root(self) -> bool:
        """AndroOpenHeart 1.3+ runs a channel as root when asked (older versions ignore the request)."""
        try:
            self.server_root = self.native_root = True   # probe without su: the server does it
            rc, out, _ = self.exec('id -u; echo "M=$ROOTSSH_MODE"', timeout=60)
            lines = out.decode(errors="replace").split()
            if rc == 0 and lines[:1] == ["0"] and "M=root" in lines:
                self.native_root = True
                self.is_root = True
                log.info("AndroOpenHeart runs root channels itself (root request)")
                self.detect_isolated_ns()
                return True
        except Exception as e:  # noqa: BLE001
            log.info("root request probe failed: %r", e)
        self.server_root = self.native_root = False
        return False

    def check_root(self) -> bool:
        try:  # are commands already root without su? (root login, or servers like AndroOpenHeart)
            rc, out, _ = self.exec("id -u", timeout=60, as_root=False)
            if rc == 0 and out.strip() == b"0":
                self.native_root = True
                self.is_root = True
                log.info("commands already run as root - not wrapping in su")
                self.detect_isolated_ns()
                return True
        except Exception as e:  # noqa: BLE001
            log.info("plain id -u failed: %r", e)
        if self.profile.su_command.strip() and not self.use_pty and self._try_server_root():
            return True
        try:
            rc, out, _ = self.exec("id -u", timeout=60)  # time to tap "Allow" in Magisk
            self.is_root = rc == 0 and out.strip() == b"0"
        except Exception:  # noqa: BLE001
            self.is_root = False
        if self.is_root:
            self.detect_isolated_ns()
        return self.is_root

    def new_fs(self) -> "FileSystem":
        """Open an independent filesystem session (new channel on same transport)."""
        if not self.alive:
            raise BackendError("Not connected")
        mode = self.profile.mode
        if mode == MODE_SFTP:
            fs: FileSystem = SftpFS(self, self.transport.open_sftp_client())
        elif mode == MODE_SU_SFTP:
            if self.use_pty:
                raise BackendError("this SSH server does not allow running sftp-server")
            with self._fs_lock:
                if not self.sftp_server_path:
                    self.sftp_server_path = self.detect_sftp_server()
            chan = self.open_channel()
            chan.exec_command(self.wrap_root(self.sftp_server_path))
            try:
                client = paramiko.SFTPClient(chan)
            except Exception as e:  # noqa: BLE001
                err = b""
                try:
                    while chan.recv_stderr_ready():
                        err += chan.recv_stderr(4096)
                except Exception:  # noqa: BLE001
                    pass
                raise BackendError(f"Root SFTP session failed to start ({self.sftp_server_path}): {e} "
                                   f"{err.decode(errors='replace')}") from e
            fs = SftpFS(self, client)
        else:
            fs = ShellFS(self)
        with self._fs_lock:
            self._extra_fs.append(fs)
        return fs


class PtyShell:
    """Runs commands through one long-lived interactive shell (optionally after `su`).

    Each command's combined output plus exit code is base64-encoded on the device
    and framed by markers, so prompts, echo and CRLF translation can't corrupt it.
    """

    def __init__(self, conn: Connection, root: bool):
        self.conn = conn
        self.root = root
        self.lock = threading.Lock()
        self.n = 0
        ch = conn.transport.open_session(timeout=15)
        try:
            ch.get_pty(term="dumb", width=4000, height=100)
        except Exception:  # noqa: BLE001
            pass
        ch.invoke_shell()
        self.ch = ch
        self.buf = b""
        self._raw("stty -echo 2>/dev/null; PS1=''; PS2=''; unset HISTFILE")
        self.run("true", 20)
        if root:
            su = conn.profile.su_command.strip().split()[0] if conn.profile.su_command.strip() else "su"
            self._raw(su)
            # su starts a new shell: silence it too (harmless if su failed)
            self._raw("stty -echo 2>/dev/null; PS1=''; PS2=''; unset HISTFILE")
            rc, out, _ = self.run("id -u", 90)  # time to approve the root prompt on the phone
            if out.strip() != b"0":
                log.warning("interactive su did not give root: %r", out[:200])

    @property
    def alive(self):
        return not self.ch.closed

    def close(self):
        try:
            self.ch.close()
        except Exception:  # noqa: BLE001
            pass

    def _raw(self, line: str):
        self.ch.sendall((line + "\n").encode("utf-8", "surrogateescape"))

    def run(self, script: str, timeout: float = 60) -> tuple[int, bytes, bytes]:
        with self.lock:
            self.n += 1
            tag = f"{os.getpid()}x{id(self)}x{self.n}"
            b64 = base64.encodebytes(script.encode("utf-8", "surrogateescape")).decode().strip()
            heredoc = f"__B{tag}"
            # markers are split with "" so the echoed command line never matches them
            ns = self.conn.ns_prefix if (self.root or self.conn.native_root) else ""
            cmd = (f'echo "__S""{tag}"; {{ base64 -d <<\'{heredoc}\' | {ns}sh 2>&1\n{b64}\n{heredoc}\n'
                   f'echo "RC=$?"; }} | base64; echo "__E""{tag}"')
            self._raw(cmd)
            start, end = f"__S{tag}".encode(), f"__E{tag}".encode()
            deadline = time.monotonic() + timeout
            while True:
                j = self.buf.find(end)
                if j >= 0:
                    i = self.buf.rfind(start, 0, j)
                    payload = self.buf[i + len(start):j] if i >= 0 else b""
                    self.buf = self.buf[j + len(end):]
                    break
                if self.ch.recv_ready():
                    self.buf += self.ch.recv(1 << 20)
                    continue
                if self.ch.closed or self.ch.exit_status_ready():
                    raise BackendError("interactive shell closed")
                if time.monotonic() > deadline:
                    self.close()
                    raise BackendError(f"Command timed out after {timeout:.0f}s")
                time.sleep(0.003)
        data = base64.b64decode(b"".join(payload.split()))
        body, sep, rc_txt = data.rpartition(b"RC=")
        try:
            rc = int(rc_txt.strip()) if sep else 255
        except ValueError:
            rc = 255
        log.debug("pty rc=%s root=%s cmd=%r out=%r", rc, self.root, script[:300], body[:300])
        return (rc, body, b"") if rc == 0 else (rc, b"", body)


# --------------------------------------------------------------------------
# File system abstraction
# --------------------------------------------------------------------------
class RemoteFile:
    """Seekable read-only file object built on FileSystem.read_range."""

    def __init__(self, fs: "FileSystem", path: str, size: int, chunk: int = 256 * 1024):
        self.fs, self.path, self.size, self.chunk = fs, path, size, chunk
        self.pos = 0
        self._buf_off = -1
        self._buf = b""

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        if whence == 0:
            self.pos = off
        elif whence == 1:
            self.pos += off
        else:
            self.pos = self.size + off
        self.pos = max(0, self.pos)
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, max(0, self.size - self.pos))
        if n <= 0:
            return b""
        out = bytearray()
        while n > 0:
            if self._buf_off >= 0 and self._buf_off <= self.pos < self._buf_off + len(self._buf):
                i = self.pos - self._buf_off
                piece = self._buf[i:i + n]
            else:
                want = max(self.chunk, min(n, 8 * 1024 * 1024))
                self._buf = self.fs.read_range(self.path, self.pos, want)
                self._buf_off = self.pos
                if not self._buf:
                    break
                piece = self._buf[:n]
            out += piece
            self.pos += len(piece)
            n -= len(piece)
        return bytes(out)

    def close(self):
        self._buf = b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FileSystem:
    conn: Connection
    lock: threading.RLock

    def listdir(self, path: str) -> list[Entry]: ...
    def stat(self, path: str) -> Entry: ...
    def read_range(self, path: str, offset: int, length: int) -> bytes: ...
    def realpath(self, path: str) -> str: ...
    def mkdir(self, path: str) -> None: ...
    def rename(self, old: str, new: str) -> None: ...
    def remove(self, path: str, is_dir: bool) -> None: ...
    def download(self, path: str, local: str, progress: Callable[[int], None]) -> None: ...
    def upload(self, local: str, path: str, progress: Callable[[int], None]) -> None: ...
    def close(self) -> None: ...

    def open_read(self, entry_or_path, size: int | None = None):
        if isinstance(entry_or_path, Entry):
            path, size = entry_or_path.path, entry_or_path.size
        else:
            path = entry_or_path
            if size is None:
                size = self.stat(path).size
        return RemoteFile(self, path, size)

    def read_all(self, path: str, size: int) -> bytes:
        return self.read_range(path, 0, size)

    # shared helpers -----------------------------------------------------
    def folder_usage(self, path: str) -> tuple[int, int, int]:
        """Return (bytes, files, folders) for a directory tree using du/find."""
        script = (f"cd -- {shq(path)} || exit 1; "
                  "du -sk . 2>/dev/null | tail -n1; "
                  "echo F $(find . -type f 2>/dev/null | wc -l); "
                  "echo D $(find . -mindepth 1 -type d 2>/dev/null | wc -l)")
        rc, out, err = self.conn.exec(script, timeout=600)
        kb = files = dirs = 0
        for line in out.decode(errors="replace").splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "F" and len(parts) > 1:
                files = int(parts[1])
            elif parts[0] == "D" and len(parts) > 1:
                dirs = int(parts[1])
            elif parts[0].isdigit():
                kb = int(parts[0])
        if not out.strip():
            raise BackendError(err.decode(errors="replace").strip() or "du failed")
        return kb * 1024, files, dirs

    def walk_files(self, root: str) -> Iterable[tuple[Entry, str]]:
        """Yield (entry, relative_path) for every regular file below root, depth first."""
        stack = [(root, "")]
        while stack:
            d, rel = stack.pop()
            for e in self.listdir(d):
                r = posixpath.join(rel, e.name) if rel else e.name
                if statmod.S_ISDIR(e.mode):
                    yield e, r
                    stack.append((e.path, r))
                elif e.is_regular:
                    yield e, r


class SftpFS(FileSystem):
    def __init__(self, conn: Connection, client: paramiko.SFTPClient):
        self.conn = conn
        self.sftp = client
        self.lock = threading.RLock()
        self.sftp.get_channel().settimeout(120)

    def close(self):
        try:
            self.sftp.close()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _owner_group(longname: str | None) -> tuple[str | None, str | None]:
        if not longname:
            return None, None
        parts = longname.split(None, 4)
        if len(parts) >= 4:
            return parts[2], parts[3]
        return None, None

    def _entry(self, path: str, a: paramiko.SFTPAttributes, name: str | None = None) -> Entry:
        owner, group = self._owner_group(getattr(a, "longname", None))
        return Entry(name=name or posixpath.basename(path) or path, path=path,
                     size=a.st_size or 0, mode=a.st_mode or 0, mtime=a.st_mtime, atime=a.st_atime,
                     uid=a.st_uid, gid=a.st_gid, owner=owner, group=group)

    def _resolve_link(self, e: Entry):
        try:
            e.link_target = self.sftp.readlink(e.path)
        except Exception:  # noqa: BLE001
            e.link_target = None
        try:
            st = self.sftp.stat(e.path)
            e.target_is_dir = statmod.S_ISDIR(st.st_mode or 0)
            if not e.target_is_dir:
                e.extra["target_size"] = st.st_size
        except Exception:  # noqa: BLE001
            e.target_is_dir = False

    def listdir(self, path):
        with self.lock:
            out = []
            for a in self.sftp.listdir_attr(path):
                if a.filename in (".", ".."):
                    continue
                e = self._entry(posixpath.join(path, a.filename), a, a.filename)
                if e.is_link:
                    self._resolve_link(e)
                out.append(e)
            return out

    def stat(self, path):
        with self.lock:
            a = self.sftp.lstat(path)
            e = self._entry(path, a)
            if e.is_link:
                self._resolve_link(e)
            return e

    def realpath(self, path):
        with self.lock:
            return self.sftp.normalize(path)

    def read_range(self, path, offset, length):
        with self.lock:
            with self.sftp.open(path, "rb") as f:
                f.seek(offset)
                if length > 1024 * 1024:
                    f.prefetch(min(length, max(0, self._size_hint(f) - offset)) or None)
                return f.read(length)

    @staticmethod
    def _size_hint(f):
        try:
            return f.stat().st_size or 0
        except Exception:  # noqa: BLE001
            return 0

    def read_all(self, path, size):
        with self.lock:
            with self.sftp.open(path, "rb") as f:
                f.prefetch(size or None)
                return f.read()

    def open_read(self, entry_or_path, size=None):
        path = entry_or_path.path if isinstance(entry_or_path, Entry) else entry_or_path
        return _LockedSftpFile(self, path)

    def mkdir(self, path):
        with self.lock:
            self.sftp.mkdir(path)

    def rename(self, old, new):
        with self.lock:
            try:
                self.sftp.posix_rename(old, new)
            except IOError:
                self.sftp.rename(old, new)

    def remove(self, path, is_dir):
        with self.lock:
            if not is_dir:
                self.sftp.remove(path)
                return
            # recursive delete
            def rm(p):
                for a in self.sftp.listdir_attr(p):
                    if a.filename in (".", ".."):
                        continue
                    cp = posixpath.join(p, a.filename)
                    if statmod.S_ISDIR(a.st_mode or 0):
                        rm(cp)
                    else:
                        self.sftp.remove(cp)
                self.sftp.rmdir(p)
            rm(path)

    def download(self, path, local, progress):
        with self.lock:
            last = [0]

            def cb(done, total):
                progress(done - last[0])
                last[0] = done
            self.sftp.get(path, local, callback=cb, prefetch=True)

    def upload(self, local, path, progress):
        with self.lock:
            last = [0]

            def cb(done, total):
                progress(done - last[0])
                last[0] = done
            self.sftp.put(local, path, callback=cb, confirm=False)


class _LockedSftpFile:
    """SFTP file handle that holds the FS lock while reading."""

    def __init__(self, fs: SftpFS, path: str):
        self.fs = fs
        with fs.lock:
            self.f = fs.sftp.open(path, "rb")
            self.size = self.f.stat().st_size or 0
        self.f.set_pipelined(True)

    def seekable(self):
        return True

    def readable(self):
        return True

    def seek(self, off, whence=0):
        return self.f.seek(off, whence) or self.f.tell()

    def tell(self):
        return self.f.tell()

    def read(self, n=-1):
        with self.fs.lock:
            return self.f.read(n if n is not None and n >= 0 else None)

    def close(self):
        with self.fs.lock:
            self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class ShellFS(FileSystem):
    """Filesystem implemented with shell commands (toybox / busybox / coreutils)."""

    STAT_FMT = "%f/%s/%X/%Y/%u/%g/%U/%G/%n"
    LINK_MARK = "//LINKS//"

    def __init__(self, conn: Connection):
        self.conn = conn
        self.lock = threading.RLock()

    def close(self):
        pass

    def _run(self, script: str, timeout=60, stdin=None) -> bytes:
        rc, out, err = self.conn.exec(script, timeout=timeout, stdin=stdin)
        if rc != 0:
            raise BackendError(err.decode(errors="replace").strip() or f"command failed ({rc})")
        return out

    def _parse_stat(self, line: str, base: str) -> Entry | None:
        parts = line.split("/", 8)
        if len(parts) < 9:
            return None
        try:
            mode = int(parts[0], 16)
            size = int(parts[1])
            atime = int(parts[2])
            mtime = int(parts[3])
            uid = int(parts[4])
            gid = int(parts[5])
        except ValueError:
            return None
        name = parts[8]
        return Entry(name=name, path=posixpath.join(base, name) if base else name, size=size, mode=mode,
                     atime=atime, mtime=mtime, uid=uid, gid=gid,
                     owner=parts[6] if parts[6] not in ("UNKNOWN", "") else None,
                     group=parts[7] if parts[7] not in ("UNKNOWN", "") else None)

    def listdir(self, path):
        globs = "* .[!.]* ..?*"
        script = (
            f"cd -- {shq(path)} || exit 1\n"
            f"[ -r . ] || {{ echo {shq(path + ': Permission denied')} >&2; exit 13; }}\n"
            f"stat -c '{self.STAT_FMT}' -- {globs} 2>/dev/null\n"
            f"echo '{self.LINK_MARK}'\n"
            f"for f in {globs}; do [ -L \"$f\" ] || continue; "
            "if [ -d \"$f\" ]; then k=D; else k=F; fi; "
            "echo \"$k/$f/$(readlink -- \"$f\")\"; done\n"
            "exit 0\n"
        )
        out = self._run(script).decode("utf-8", errors="surrogateescape")
        entries: dict[str, Entry] = {}
        in_links = False
        for line in out.split("\n"):
            if not line:
                continue
            if line == self.LINK_MARK:
                in_links = True
                continue
            if not in_links:
                e = self._parse_stat(line, path)
                if e and e.name not in (".", ".."):
                    entries[e.name] = e
            else:
                kind, _, rest = line.partition("/")
                name, _, target = rest.partition("/")
                e = entries.get(name)
                if e:
                    e.target_is_dir = kind == "D"
                    e.link_target = target
        return list(entries.values())

    def stat(self, path):
        script = (f"stat -c '{self.STAT_FMT}' -- {shq(path)} || exit 1\n"
                  f"echo '{self.LINK_MARK}'\n"
                  f"if [ -L {shq(path)} ]; then if [ -d {shq(path)} ]; then k=D; else k=F; fi; "
                  f"echo \"$k/x/$(readlink -- {shq(path)})\"; fi\n")
        out = self._run(script).decode("utf-8", errors="surrogateescape")
        lines = [l for l in out.split("\n") if l]
        e = self._parse_stat(lines[0], "")
        if not e:
            raise BackendError(f"Cannot stat {path}")
        e.path = path
        e.name = posixpath.basename(path.rstrip("/")) or "/"
        if len(lines) > 2:
            kind, _, rest = lines[2].partition("/")
            e.target_is_dir = kind == "D"
            e.link_target = rest.partition("/")[2]
        return e

    def realpath(self, path):
        out = self._run(f"cd -- {shq(path)} && pwd -P").decode(errors="replace").strip()
        return out or path

    def read_range(self, path, offset, length):
        if offset == 0:
            script = f"head -c {int(length)} -- {shq(path)}"
        else:
            script = f"tail -c +{int(offset) + 1} -- {shq(path)} | head -c {int(length)}"
        return self._run(script, timeout=300)

    def mkdir(self, path):
        self._run(f"mkdir -- {shq(path)}")

    def rename(self, old, new):
        self._run(f"mv -- {shq(old)} {shq(new)}")

    def remove(self, path, is_dir):
        self._run(f"rm {'-rf' if is_dir else '-f'} -- {shq(path)}", timeout=600)

    def download(self, path, local, progress):
        with open(local, "wb") as f:
            def sink(b):
                f.write(b)
                progress(len(b))
            self.conn.exec_stream_out(f"cat -- {shq(path)}", sink, timeout=600)

    def upload(self, local, path, progress):
        if hasattr(self.conn, "upload_stream"):     # ADB transport
            self.conn.upload_stream(local, path, progress)
            return
        if self.conn.use_pty:
            with open(local, "rb") as f:
                first = True
                while True:
                    b = f.read(384 * 1024)
                    if not b and not first:
                        break
                    b64 = base64.encodebytes(b).decode().strip()
                    op = ">" if first else ">>"
                    self._run(f"base64 -d {op} {shq(path)} <<'__UPLOAD__'\n{b64}\n__UPLOAD__\n", timeout=300)
                    progress(len(b))
                    first = False
                    if not b:
                        break
            return
        chan = self.conn.open_channel()
        chan.settimeout(600)
        try:
            chan.exec_command(self.conn.wrap_root(f"cat > {shq(path)}"))
            with open(local, "rb") as f:
                while True:
                    b = f.read(262144)
                    if not b:
                        break
                    chan.sendall(b)
                    progress(len(b))
            chan.shutdown_write()
            rc = chan.recv_exit_status()
            if rc != 0:
                err = b""
                while chan.recv_stderr_ready():
                    err += chan.recv_stderr(4096)
                raise BackendError(err.decode(errors="replace").strip() or f"upload failed ({rc})")
        finally:
            chan.close()


# --------------------------------------------------------------------------
# Known hosts (trust on first use)
# --------------------------------------------------------------------------
def migrate_old_name() -> None:
    """The app used to be called DroidBrowser: move its settings, known hosts, caches and
    partition backups to the new folder names (once, only if the new folder doesn't exist yet)."""
    home = os.path.expanduser("~")
    for base in (os.environ.get("APPDATA") or home, os.environ.get("LOCALAPPDATA") or home,
                 os.path.join(home, "Documents")):
        old, new = os.path.join(base, "DroidBrowser"), os.path.join(base, "AndroUnderTheHood")
        if os.path.isdir(old) and not os.path.exists(new):
            try:
                os.rename(old, new)
                lg = os.path.join(new, "droidbrowser.log")
                if os.path.isfile(lg):
                    os.replace(lg, os.path.join(new, "androunderthehood-old.log"))
            except OSError:
                pass


def app_dir() -> str:
    d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "AndroUnderTheHood")
    os.makedirs(d, exist_ok=True)
    return d


class KnownHosts:
    def __init__(self):
        self.path = os.path.join(app_dir(), "known_hosts")
        self.keys = paramiko.HostKeys()
        if os.path.exists(self.path):
            try:
                self.keys.load(self.path)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _name(host, port):
        return host if int(port) == 22 else f"[{host}]:{port}"

    def check(self, host, port, key: paramiko.PKey) -> str:
        """Return 'ok', 'unknown' or 'changed'."""
        entry = self.keys.lookup(self._name(host, port))
        if not entry:
            return "unknown"
        known = entry.get(key.get_name())
        if known is None:
            return "unknown"
        return "ok" if known.asbytes() == key.asbytes() else "changed"

    def add(self, host, port, key: paramiko.PKey):
        name = self._name(host, port)
        if name in self.keys:
            del self.keys[name]
        self.keys.add(name, key.get_name(), key)
        self.keys.save(self.path)
