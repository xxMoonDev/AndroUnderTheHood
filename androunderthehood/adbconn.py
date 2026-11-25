"""ADB as an alternative transport: AndroUnderTheHood features run over `adb shell` instead of SSH."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time

from . import backend as B

log = logging.getLogger("androunderthehood")
q = B.shq
_NOWIN = 0x08000000 if sys.platform == "win32" else 0


def find_adb() -> str | None:
    home = os.path.expanduser("~")
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    local = os.environ.get("LOCALAPPDATA", "")
    cands = [os.environ.get("ADB", ""),
             os.path.join(here, "scrcpy-ssh", "adb.exe"),                     # release layout
             os.path.join(os.path.dirname(here), "scrcpy-ssh", "adb.exe"),
             os.path.join(home, "Desktop", "scrcpy-ssh", "adb.exe"),
             os.path.join(local, "AndroUnderTheHood", "tools", "platform-tools", "adb.exe")]   # Flash tools
    winget = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
    if os.path.isdir(winget):
        for d in os.listdir(winget):
            if d.lower().startswith("genymobile.scrcpy"):
                for sub in os.listdir(os.path.join(winget, d)):
                    cands.append(os.path.join(winget, d, sub, "adb.exe"))
    cands.append(shutil.which("adb") or "")
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def adb(args: list[str], timeout=30, input: bytes | None = None) -> subprocess.CompletedProcess:
    exe = find_adb()
    if not exe:
        raise B.BackendError("adb.exe not found (it ships with scrcpy; or set the ADB environment variable).")
    return subprocess.run([exe, *args], capture_output=True, timeout=timeout, input=input, creationflags=_NOWIN)


def adb_devices() -> list[dict]:
    """[{serial, state, model, transport}]"""
    r = adb(["devices", "-l"], timeout=20)
    out = []
    for line in r.stdout.decode(errors="replace").splitlines()[1:]:
        f = line.split()
        if len(f) < 2:
            continue
        info = dict(x.split(":", 1) for x in f[2:] if ":" in x)
        out.append({"serial": f[0], "state": f[1], "model": info.get("model", "").replace("_", " "),
                    "transport": "network" if ":" in f[0] else "usb"})
    return out


def adb_connect(address: str) -> tuple[bool, str]:
    r = adb(["connect", address], timeout=20)
    msg = (r.stdout + r.stderr).decode(errors="replace").strip()
    if "connected" not in msg or "cannot" in msg or "failed" in msg:
        return False, msg or "adb connect failed"
    # wait until authorized
    for _ in range(30):
        st = adb(["-s", address, "get-state"], timeout=10)
        state = (st.stdout + st.stderr).decode(errors="replace").strip()
        if state == "device":
            return True, msg
        if "unauthorized" in state:
            msg = "Waiting for you to allow USB debugging on the phone…"
        time.sleep(1)
    return False, "The phone has not authorized this computer (check the “Allow USB debugging?” prompt)."


class AdbConnection(B.Connection):
    is_adb = True

    def __init__(self, profile: B.Profile):
        super().__init__(profile)
        self.serial = profile.host
        self._alive = False
        self.exe = find_adb()

    # ---- lifecycle -----------------------------------------------------------
    def open(self, timeout: float = 15):
        if not self.exe:
            raise B.BackendError("adb.exe not found.")
        r = adb(["-s", self.serial, "get-state"], timeout=timeout)
        state = (r.stdout + r.stderr).decode(errors="replace").strip()
        if state != "device":
            if "unauthorized" in state:
                raise B.BackendError("The phone has not authorized this PC yet – accept “Allow USB debugging?” on "
                                     "the phone, then connect again.")
            raise B.BackendError(f"ADB device {self.serial} is not available: {state or 'offline'}")
        self._alive = True
        # The Windows adb client writes `adb shell` output in text mode (every \n becomes \r\n,
        # even with -T). Detect it once; the exact inverse is \r\n -> \n (an original \r\n
        # arrives as \r\r\n and becomes \r\n again).
        r = subprocess.run([self.exe, "-s", self.serial, "shell", "-T", "printf 'a\\nb'"], capture_output=True,
                           timeout=timeout, creationflags=_NOWIN)
        self.crlf = r.stdout == b"a\r\nb"
        log.info("adb %s: output CRLF translation %s", self.serial, "detected" if self.crlf else "not present")
        return None

    crlf = False

    def _fix(self, data: bytes) -> bytes:
        return data.replace(b"\r\n", b"\n") if self.crlf else data

    def authenticate(self, password: str = "", passphrase: str = ""):
        return None

    def probe_exec(self) -> bool:
        self.use_pty = False
        return True

    def close(self):
        self._alive = False

    @property
    def alive(self) -> bool:
        return self._alive

    # ---- commands ----------------------------------------------------------------
    def _argv(self, cmd: str, as_root: bool) -> list[str]:
        # -T: never allocate a PTY. On Windows the NUL device counts as a "terminal", which otherwise
        # makes adb use a PTY and turn every \n in the output into \r\n (corrupting binary data).
        return [self.exe, "-s", self.serial, "shell", "-T", self.wrap_root(cmd) if as_root else cmd]

    def exec(self, cmd: str, timeout: float = 60, stdin: bytes | None = None,
             as_root: bool = True) -> tuple[int, bytes, bytes]:
        if not self._alive:
            raise B.BackendError("Not connected")
        try:
            r = subprocess.run(self._argv(cmd, as_root), capture_output=True, timeout=timeout,
                               input=stdin if stdin is not None else b"", creationflags=_NOWIN)
        except subprocess.TimeoutExpired:
            raise B.BackendError(f"Command timed out after {timeout:.0f}s")
        if r.returncode == 1 and b"error: device" in r.stderr:
            self._alive = False
            raise B.BackendError(r.stderr.decode(errors="replace").strip())
        return r.returncode, self._fix(r.stdout), self._fix(r.stderr)

    def exec_stream_out(self, cmd: str, sink, timeout: float = 120) -> int:
        p = subprocess.Popen(self._argv(cmd, True), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, creationflags=_NOWIN)
        err = bytearray()

        def drain():
            for b in iter(lambda: p.stderr.read(4096), b""):
                err.extend(b)
        t = threading.Thread(target=drain, daemon=True)
        t.start()
        try:
            carry = b""
            for chunk in iter(lambda: p.stdout.read(262144), b""):
                if self.crlf:
                    chunk = carry + chunk
                    carry = b""
                    if chunk.endswith(b"\r"):        # may be the first half of a translated \r\n
                        chunk, carry = chunk[:-1], b"\r"
                    chunk = chunk.replace(b"\r\n", b"\n")
                if chunk:
                    sink(chunk)
            if carry:
                sink(carry)
        except BaseException:
            p.kill()
            raise
        rc = p.wait(timeout=timeout)
        t.join(2)
        if rc != 0:
            raise B.BackendError(err.decode(errors="replace").strip() or f"exit code {rc}")
        return rc

    PUSH_CHUNK = 8 << 20

    def upload_stream(self, local: str, remote: str, progress):
        """Copy a local file to the device.

        Piping data into `adb shell` is not binary-safe on Windows (adb.exe stops reading stdin at a
        0x1A byte), so the file is sent with `adb push` – in 8 MB pieces for progress – to a staging
        path in /data/local/tmp and then moved into place as root.
        """
        import tempfile
        import uuid
        stage = f"/data/local/tmp/.androunderthehood-up-{uuid.uuid4().hex[:10]}"
        size = os.path.getsize(local)
        tmpdir = tempfile.mkdtemp(prefix="androunderthehood-push-")
        try:
            pieces = []
            if size <= self.PUSH_CHUNK:
                pieces.append((local, size))
            else:
                with open(local, "rb") as f:
                    for i, chunk in enumerate(iter(lambda: f.read(self.PUSH_CHUNK), b"")):
                        part = os.path.join(tmpdir, f"part{i:05d}")
                        with open(part, "wb") as pf:
                            pf.write(chunk)
                        pieces.append((part, len(chunk)))
            for i, (path, n) in enumerate(pieces):
                dest = stage if len(pieces) == 1 else f"{stage}.p{i:05d}"
                r = adb(["-s", self.serial, "push", path, dest], timeout=600)
                if r.returncode != 0:
                    raise B.BackendError("adb push failed: " + (r.stdout + r.stderr).decode(errors="replace").strip())
                progress(n)
                if path != local:
                    os.remove(path)
            if len(pieces) == 1:
                cmd = f"cat {q(stage)} > {q(remote)}; r=$?; rm -f {q(stage)}; exit $r"
            else:
                cmd = (f"cat {q(stage)}.p* > {q(remote)}; r=$?; rm -f {q(stage)}.p*; exit $r")
            rc, out, err = self.exec(cmd, timeout=600)
            if rc != 0:
                raise B.BackendError((out + err).decode(errors="replace").strip() or f"upload failed ({rc})")
        finally:
            import shutil as _sh
            _sh.rmtree(tmpdir, ignore_errors=True)
            try:
                self.exec(f"rm -f {q(stage)} {q(stage)}.p*", timeout=30, as_root=True)
            except Exception:  # noqa: BLE001
                pass

    def popen(self, cmd: str) -> subprocess.Popen:
        """Long-running command (logcat) with stdout as a pipe."""
        return subprocess.Popen(self._argv(cmd, True), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, creationflags=_NOWIN)

    # ---- file system -----------------------------------------------------------
    def new_fs(self) -> B.FileSystem:
        fs = B.ShellFS(self)
        with self._fs_lock:
            self._extra_fs.append(fs)
        return fs
