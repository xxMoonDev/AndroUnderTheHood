"""PhoneDesktop (no root) engine: Ubuntu 24.04 + KDE Plasma (Xvfb) + RustDesk under proot.

The no-root twin of the phone_desktop plugin. Everything runs as Android's shell user (uid 2000):
over ADB, or over SSH when the SSH server runs commands through Shizuku. Nothing needs su and
nothing is mounted: proot fakes root and the Ubuntu file layout in user space.

No Qt in here. The UI passes a `ui` object with three callbacks (called from the worker thread):
    ui.log(text)                         raw terminal text
    ui.step(key, state, detail="")       state: "run" | "ok" | "fail" | "skip" | "wait"
    ui.progress(overall, label, phase)   overall/phase in 0..1, -1 for "busy", -2 = phase only
Long work (the install, building a bundle) runs on the device as a detached job (pdn-job.sh),
and the engine follows its log, so a dropped connection does not kill it.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request

UBUNTU = {
    "file": "ubuntu-base-24.04.5-base-arm64.tar.gz",
    "url": "https://cdimage.ubuntu.com/ubuntu-base/releases/24.04.5/release/ubuntu-base-24.04.5-base-arm64.tar.gz",
    "sha256": "a91d5a93010193712d346d761372b7c9db6dfcf093893161c64ca107f05914f2",
}
RUSTDESK = {
    "file": "rustdesk-1.5.3-aarch64.deb",
    "url": "https://github.com/rustdesk/rustdesk/releases/download/1.5.3/rustdesk-1.5.3-aarch64.deb",
    "sha256": "fa85e8d3ccd45e2e303c2d1359af5dc51aa93d880e39ba47c9b04bfabd82ae19",
}
# Optional office suite: arm64 repack of the official amd64 .deb (no upstream arm64 build to
# download), used only when this exact file is in the cache folder.
ELIXIROFFICE = {
    "file": "ElixirOffice-0.9.4-linux-arm64.deb",
    "sha256": "12dd059fe68932122c071843e9f91c2b18b2d99aa2068edac8be09452beda55f",
}
BUNDLE_TAR = "offline-bundle.tar"

STAGE = "/data/local/tmp/pdnr-stage"
PD = "/data/local/tmp/pdnr"
BIN = PD + "/bin"
ROOTFS = PD + "/rootfs"
JOBS = PD + "/jobs"
SHELL_UID = 2000
DEVICE_FILES = ["pd.conf", "pdn-proot.sh", "pdn-install.sh", "pdn-setup.sh", "pdn-desktop.sh", "pdn-start.sh",
                "pdn-stop.sh", "pdn-status.sh", "pdn-rustdesk.sh", "pdn-job.sh", "pdn-uninstall.sh",
                "pd-packages.sh", "pd-bundle.sh", "pd-loginctl.sh", "pd-shmshim.c", "pdn-unixsock.c", "mozilla-apt.asc"]
BINARY_FILES = ["proot"]                 # sent base64-encoded (ADB stdin is not binary-safe)
MIN_FREE_GB = {"minimal": 5, "standard": 8, "full": 15}

INSTALL_STEPS = [
    ("check", "Check device"),
    ("files", "Prepare files on this PC"),
    ("upload", "Upload to the device"),
    ("install", "Install Ubuntu + KDE Plasma"),
    ("rustdesk", "Configure RustDesk"),
    ("start", "Start the desktop"),
    ("status", "Status"),
]
BUNDLE_STEPS = [
    ("check", "Check device"),
    ("upload", "Upload scripts"),
    ("bundle", "Download all packages on the device"),
    ("pull", "Copy the bundle to this PC"),
]


class Cancelled(Exception):
    pass


def shq(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class Options:
    def __init__(self, profile="standard", res="1920x1080", user="kde", mode="direct", server="", key="",
                 port=21118, offline=False, start=True):
        self.profile, self.res, self.user, self.mode = profile, res, user, mode
        self.server, self.key, self.port, self.offline, self.start = server, key, port, offline, start

    def validate(self):
        if self.profile not in MIN_FREE_GB:
            raise ValueError(f"Unknown profile {self.profile}")
        if not re.fullmatch(r"\d{3,5}x\d{3,5}", self.res):
            raise ValueError("Resolution must look like 1920x1080")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", self.user):
            raise ValueError("User name: lowercase letters, digits, _ and -")
        if self.mode not in ("direct", "public", "both", "custom"):
            raise ValueError(f"Unknown RustDesk mode {self.mode}")
        try:
            self.port = int(self.port)
        except (TypeError, ValueError):
            raise ValueError("The direct IP port must be a number") from None
        if not 1024 <= self.port <= 65535:
            raise ValueError("The direct IP port must be between 1024 and 65535 (no root: no low ports)")
        if self.mode == "custom":
            if not re.fullmatch(r"[A-Za-z0-9.:\-\[\]]+", self.server or ""):
                raise ValueError("Custom mode needs a server host (letters, digits, . : -)")
            if self.key and not re.fullmatch(r"[A-Za-z0-9+/=_\-]+", self.key):
                raise ValueError("The server key has unexpected characters")


class InstallProgress:
    """Turns the installer's output into progress (phase markers + apt's counters)."""
    PHASES = [  # (marker, label, weight)
        ("== [1/4]", "Unpacking Ubuntu", 3),
        ("-- apt update", "Updating package lists", 4),
        ("-- base packages", "Base packages, Xvfb", 12),
        ("-- socket shim", "Socket shim", 1),
        ("-- KDE Plasma", "KDE Plasma (the big one)", 70),
        ("-- RustDesk", "RustDesk", 6),
        ("-- ElixirOffice", "ElixirOffice", 4),
        ("-- desktop user", "Desktop user and settings", 5),
    ]
    TOTAL = re.compile(r"(\d+) upgraded, (\d+) newly installed")

    def __init__(self):
        self.idx = -1
        self.reset_counts()
        self.done = False

    def reset_counts(self):
        self.total = self.get = self.unpack = self.setup = 0

    def feed(self, line: str):
        for i, (mark, _label, _w) in enumerate(self.PHASES):
            if line.startswith(mark):
                self.idx = i
                self.reset_counts()
                return
        if line.startswith("== install finished"):
            self.done = True
            return
        m = self.TOTAL.search(line)
        if m:
            self.total = int(m.group(1)) + int(m.group(2))
        elif line.startswith("Get:"):
            self.get += 1
        elif line.startswith("Unpacking "):
            self.unpack += 1
        elif line.startswith("Setting up "):
            self.setup += 1

    def state(self) -> tuple[float, str, float]:
        if self.done:
            return 1.0, "Install finished", 1.0
        if self.idx < 0:
            return 0.0, "Starting…", -1
        weights = [w for _m, _l, w in self.PHASES]
        before = sum(weights[:self.idx]) / sum(weights)
        phase = min(1.0, (min(self.get, self.total) + self.unpack + self.setup) / (3 * self.total)) \
            if self.total else -1
        overall = before + (max(phase, 0) * weights[self.idx] / sum(weights))
        label = self.PHASES[self.idx][1]
        if self.total:
            label += f" – {self.setup}/{self.total} packages set up"
        return overall, label, phase


class BundleProgress(InstallProgress):
    PHASES = [("-- apt update", "Updating package lists", 3),
              ("-- downloading", "Downloading every package", 92),
              ("-- building repository index", "Building the repository index", 5)]

    def feed(self, line):
        if line.startswith("-- bundle:"):
            self.done = True
            return
        super().feed(line)

    def state(self):
        if self.done:
            return 1.0, "Bundle built", 1.0
        o, label, p = super().state()
        if self.idx == 1 and self.total:     # download-only: only Get: lines count
            p = min(1.0, self.get / self.total)
            o = (3 + 92 * p) / 100
            label = f"Downloading every package – {self.get}/{self.total}"
        return o, label, p


class Engine:
    def __init__(self, api, ui, files_dir: str):
        self.api = api
        self.ui = ui
        self.files_dir = files_dir
        self.cancel = False
        self.uid = None             # the connection's uid (2000 = shell; 0 = root -> su 2000)

    # ---- helpers -------------------------------------------------------------------------------
    @property
    def conn(self):
        c = self.api.conn
        if not c:
            raise RuntimeError("Not connected")
        return c

    @property
    def is_adb(self) -> bool:
        return bool(getattr(self.conn, "is_adb", False))

    @property
    def cache_dir(self):
        return os.path.join(self.files_dir, "cache")

    @property
    def repo_dir(self):
        return os.path.join(self.files_dir, "offline", "repo")

    def check_cancel(self):
        if self.cancel:
            raise Cancelled()

    def detect_uid(self) -> int:
        if self.uid is None:
            rc, out, _ = self.conn.exec("id -u", timeout=30, as_root=False)
            try:
                self.uid = int(out.decode().strip())
            except ValueError:
                self.uid = -1
        return self.uid

    def exec(self, cmd: str, timeout: float = 120, stdin: bytes | None = None):
        """Run as the shell user, never through su for root. A connection that is already root
        (a root SSH server) drops to uid 2000, so every file stays owned by shell. Returns (rc, text)."""
        uid = self.detect_uid()
        if uid == 0:
            cmd = f"su {SHELL_UID} -c {shq(cmd)}"
        elif uid != SHELL_UID:
            raise RuntimeError(self.uid_problem(uid))
        rc, out, err = self.conn.exec(cmd, timeout=timeout, stdin=stdin, as_root=False)
        return rc, (out + err).decode("utf-8", "replace")

    @staticmethod
    def uid_problem(uid: int) -> str:
        return (f"This connection runs commands as uid {uid}. The no-root desktop needs Android's shell user "
                f"(uid 2000): connect over ADB (USB or wireless debugging), or over SSH with the server in "
                f"Shizuku mode.")

    def run_logged(self, cmd: str, timeout: float = 300) -> int:
        self.ui.log(f"$ {cmd}\n")
        rc, text = self.exec(cmd + " 2>&1", timeout=timeout)
        if text:
            self.ui.log(text if text.endswith("\n") else text + "\n")
        return rc

    # ---- device state ----------------------------------------------------------------------------
    def probe(self) -> dict:
        st = {"abi": "", "uid": -1, "ok_user": False, "installed": False, "setup_done": False, "rd_listen": False,
              "jobs": {}, "procs": 0, "port": 21118, "root_desktop": False}
        uid = self.detect_uid()
        st["uid"] = uid
        st["ok_user"] = uid in (0, SHELL_UID)
        if not st["ok_user"]:
            rc, out, _ = self.conn.exec("getprop ro.product.cpu.abi", timeout=30, as_root=False)
            st["abi"] = out.decode(errors="replace").strip()
            return st
        rc, text = self.exec(
            f"echo ABI $(getprop ro.product.cpu.abi); "
            f"test -x {ROOTFS}/usr/local/sbin/pd-desktop && echo INSTALLED; "
            f"test -f {ROOTFS}/var/lib/phonedesktop/setup.done && echo SETUPDONE; "
            f"for j in install bundle; do echo JOB $j $(sh {BIN}/pdn-job.sh --status $j 2>/dev/null || echo none); done; "
            f"echo PROCS $(ls -l /proc/[0-9]*/exe 2>/dev/null | grep -c ' -> {PD}/tmp/prooted-'); "
            f"p=$(sed -n 's/^PD_RDPORT=//p' {PD}/pd.local.conf 2>/dev/null); echo PORT ${{p:-21118}}; "
            f"h=$(printf '%04X' ${{p:-21118}}); grep -qi \":$h \" /proc/net/tcp /proc/net/tcp6 2>/dev/null && echo RDLISTEN; "
            f"test -d /data/local/phonedesktop && echo ROOTDESKTOP; true", timeout=60)
        for ln in text.splitlines():
            parts = ln.split()
            if not parts:
                continue
            if parts[0] == "ABI":
                st["abi"] = parts[1] if len(parts) > 1 else ""
            elif parts[0] == "JOB" and len(parts) >= 3:
                st["jobs"][parts[1]] = parts[2:]
            elif parts[0] == "PROCS" and len(parts) > 1 and parts[1].isdigit():
                st["procs"] = int(parts[1])
            elif parts[0] == "PORT" and len(parts) > 1 and parts[1].isdigit():
                st["port"] = int(parts[1])
            elif parts[0] == "INSTALLED":
                st["installed"] = True
            elif parts[0] == "SETUPDONE":
                st["setup_done"] = True
            elif parts[0] == "RDLISTEN":
                st["rd_listen"] = True
            elif parts[0] == "ROOTDESKTOP":
                st["root_desktop"] = True
        # an install/bundle job also runs under proot: only count the desktop when no job runs
        busy = any(info and info[0] == "running" for info in st["jobs"].values())
        st["running"] = st["procs"] > 0 and not busy
        return st

    def check_device(self, opts: Options | None):
        self.ui.step("check", "run")
        st = self.probe()
        if not st["ok_user"]:
            raise RuntimeError(self.uid_problem(st["uid"]))
        self.ui.log(f"ABI {st['abi']}, user {'shell' if st['uid'] == SHELL_UID else 'root -> su 2000'}, "
                    f"no-root desktop {'installed' if st['installed'] else 'not installed'}\n")
        if st["abi"] != "arm64-v8a":
            raise RuntimeError(f"Needs an arm64 device (this one is {st['abi'] or 'unknown'})")
        if opts and not st["installed"]:
            rc, text = self.exec("df -k /data/local/tmp | tail -n1", timeout=30)
            try:
                free_gb = int(text.split()[3]) / 1024 / 1024
            except (IndexError, ValueError):
                free_gb = 999
            need = MIN_FREE_GB[opts.profile]
            self.ui.log(f"Free space in /data: {free_gb:.1f} GB (needs about {need} GB)\n")
            if free_gb < need:
                raise RuntimeError(f"Not enough space in /data: {free_gb:.1f} GB free, {need} GB needed")
        self.ui.step("check", "ok", st["abi"])
        return st

    # ---- PC side files ---------------------------------------------------------------------------
    def offline_info(self) -> dict | None:
        p = os.path.join(self.repo_dir, "Packages")
        if not os.path.isfile(p):
            return None
        n = sum(1 for f in os.listdir(self.repo_dir) if f.endswith(".deb"))
        size = sum(os.path.getsize(os.path.join(self.repo_dir, f)) for f in os.listdir(self.repo_dir))

        def rd(name):
            try:
                return open(os.path.join(self.repo_dir, name), encoding="utf-8").read().strip()
            except OSError:
                return ""
        return {"packages": n, "size": size, "profile": rd("PROFILE") or "?", "built": rd("BUILT")}

    def fetch(self, item: dict, offline: bool) -> str:
        os.makedirs(self.cache_dir, exist_ok=True)
        path = os.path.join(self.cache_dir, item["file"])
        if os.path.isfile(path):
            self.ui.log(f"checking {item['file']} … ")
            if sha256_file(path) == item["sha256"]:
                self.ui.log("ok\n")
                return path
            self.ui.log("checksum mismatch, downloading again\n")
            os.remove(path)
        if offline:
            raise RuntimeError(f"{item['file']} is missing in {self.cache_dir} and offline mode can't download it")
        self.ui.log(f"downloading {item['url']}\n")
        req = urllib.request.Request(item["url"], headers={"User-Agent": "AndroUnderTheHood-PhoneDesktop"})
        tmp = path + ".part"
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                self.check_cancel()
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
                done += len(b)
                if total:
                    self.ui.progress(-2, f"Downloading {item['file']} – {done >> 20}/{total >> 20} MB", done / total)
        if sha256_file(tmp) != item["sha256"]:
            os.remove(tmp)
            raise RuntimeError(f"SHA-256 mismatch for {item['file']}")
        os.replace(tmp, path)
        self.ui.log(f"ok  {item['file']}\n")
        return path

    def optional_file(self, item: dict) -> str | None:
        path = os.path.join(self.cache_dir, item["file"])
        if not os.path.isfile(path):
            self.ui.log(f"no {item['file']} in {self.cache_dir} – skipped\n")
            return None
        self.ui.log(f"checking {item['file']} … ")
        if sha256_file(path) != item["sha256"]:
            self.ui.log("checksum mismatch – skipped\n")
            return None
        self.ui.log("ok\n")
        return path

    def bundle_tar(self) -> str:
        """offline/repo as one tar (one upload instead of ~1700), rebuilt when the repo changed."""
        tar = os.path.join(self.cache_dir, BUNDLE_TAR)
        idx = os.path.join(self.repo_dir, "Packages")
        if os.path.isfile(tar) and os.path.getmtime(tar) >= os.path.getmtime(idx):
            self.ui.log(f"offline bundle tar is up to date ({os.path.getsize(tar) >> 20} MB)\n")
            return tar
        names = sorted(os.listdir(self.repo_dir))
        self.ui.log(f"packing {len(names)} files from {self.repo_dir} into {BUNDLE_TAR} …\n")
        os.makedirs(self.cache_dir, exist_ok=True)
        with tarfile.open(tar + ".part", "w", format=tarfile.GNU_FORMAT) as t:
            for i, n in enumerate(names):
                self.check_cancel()
                t.add(os.path.join(self.repo_dir, n), arcname=n, recursive=False)
                if i % 50 == 0:
                    self.ui.progress(-2, f"Packing the offline bundle – {i}/{len(names)}", i / len(names))
        os.replace(tar + ".part", tar)
        return tar

    def prepare_files(self, opts: Options) -> list[str]:
        self.ui.step("files", "run")
        files = [self.fetch(UBUNTU, opts.offline), self.fetch(RUSTDESK, opts.offline)]
        eo = self.optional_file(ELIXIROFFICE)
        if eo:
            files.append(eo)
        if opts.offline:
            info = self.offline_info()
            if not info:
                raise RuntimeError(f"No offline bundle in {self.repo_dir}")
            if info["profile"] != opts.profile:
                self.ui.log(f"WARNING: the bundle was built for profile '{info['profile']}', "
                            f"installing '{opts.profile}' – packages that aren't in it will fail\n")
            files.append(self.bundle_tar())
        self.ui.step("files", "ok", "offline" if opts.offline else "online")
        return files

    # ---- transfers (always as the shell user) ----------------------------------------------------------
    def push_scripts(self, opts: Options | None = None):
        """Device scripts come from inside the plugin (memory); nothing is written to the PC's disk."""
        self.exec(f"mkdir -p {STAGE} && chmod 755 {STAGE}")
        for name in DEVICE_FILES:
            data = self.api.read_file("device/" + name)
            rc, text = self.exec(f"cat > {STAGE}/{name}", stdin=data)
            if rc != 0:
                raise RuntimeError(f"writing {name} failed: {text.strip()}")
        for name in BINARY_FILES:
            data = base64.b64encode(self.api.read_file("device/" + name))
            rc, text = self.exec(f"base64 -d > {STAGE}/{name} && chmod 755 {STAGE}/{name}", stdin=data, timeout=120)
            if rc != 0:
                raise RuntimeError(f"writing {name} failed: {text.strip()}")
        if opts:
            conf = (f"PD_PROFILE={opts.profile}\nPD_RES={opts.res}\nPD_USER={opts.user}\n"
                    f"PD_RDPORT={opts.port}\n").encode()
            self.exec(f"cat > {STAGE}/pd.local.conf", stdin=conf)
        self.ui.log(f"{len(DEVICE_FILES) + len(BINARY_FILES)} device files written to {STAGE}\n")

    def _remote_size(self, path: str) -> int:
        rc, text = self.exec(f"stat -c %s {shq(path)} 2>/dev/null || echo -1", timeout=30)
        try:
            return int(text.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return -1

    def push_file(self, local: str, remote: str, prog):
        """Copy a PC file to the device as the shell user (adb push / cat over SSH)."""
        size = os.path.getsize(local)
        if self.is_adb and self.uid == SHELL_UID:
            p = subprocess.Popen([self.conn.exe, "-s", self.conn.serial, "push", local, remote],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = []
            t = threading.Thread(target=lambda: out.append(p.stdout.read()), daemon=True)
            t.start()
            last = 0
            while p.poll() is None:
                time.sleep(1)
                if self.cancel:
                    p.kill()
                    raise Cancelled()
                now = max(self._remote_size(remote), 0)
                if now > last:
                    prog(now - last)
                    last = now
            t.join(5)
            if p.returncode != 0:
                raise RuntimeError("adb push failed: " + b"".join(out).decode(errors="replace").strip())
            if size > last:
                prog(size - last)
            return
        # SSH: binary-safe stdin, 4 MB per command
        self.exec(f": > {shq(remote)}")
        with open(local, "rb") as f:
            for chunk in iter(lambda: f.read(4 << 20), b""):
                self.check_cancel()
                rc, text = self.exec(f"cat >> {shq(remote)}", stdin=chunk, timeout=300)
                if rc != 0:
                    raise RuntimeError(f"upload failed: {text.strip()}")
                prog(len(chunk))

    def pull_file(self, remote: str, local: str, total: int, prog):
        if self.is_adb and self.uid == SHELL_UID:
            p = subprocess.Popen([self.conn.exe, "-s", self.conn.serial, "pull", remote, local],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            last = 0
            while p.poll() is None:
                time.sleep(1)
                if self.cancel:
                    p.kill()
                    raise Cancelled()
                now = os.path.getsize(local) if os.path.exists(local) else 0
                if now > last:
                    prog(now - last)
                    last = now
            if p.returncode != 0:
                raise RuntimeError("adb pull failed: " + p.stdout.read().decode(errors="replace").strip())
            return
        step = 4 << 20                     # SSH: binary-safe stdout, 4 MB per command
        with open(local, "wb") as f:
            for i in range((total + step - 1) // step):
                self.check_cancel()
                out = self.exec_raw(f"dd if={shq(remote)} bs={step} skip={i} count=1 2>/dev/null")
                f.write(out)
                prog(len(out))

    def exec_raw(self, cmd: str, timeout: float = 300) -> bytes:
        """Like exec(), but returns stdout as bytes (for binary data over SSH)."""
        if self.detect_uid() == 0:
            cmd = f"su {SHELL_UID} -c {shq(cmd)}"
        rc, out, err = self.conn.exec(cmd, timeout=timeout, as_root=False)
        if rc != 0:
            raise RuntimeError(err.decode(errors="replace").strip() or f"exit code {rc}")
        return out

    def upload(self, files: list[str]):
        self.ui.step("upload", "run")
        total = sum(os.path.getsize(f) for f in files)
        sent = [0]
        t0 = time.monotonic()
        for path in files:
            name = os.path.basename(path)
            size = os.path.getsize(path)
            if self._remote_size(f"{STAGE}/{name}") == size:
                self.ui.log(f"{name}: already on the device\n")
                sent[0] += size
                continue
            self.ui.log(f"uploading {name} ({size >> 20} MB)\n")

            def prog(n):
                self.check_cancel()
                sent[0] += n
                rate = sent[0] / max(time.monotonic() - t0, 0.1)
                self.ui.progress(-2, f"Uploading {name} – {sent[0] >> 20}/{total >> 20} MB "
                                     f"({rate / 1e6:.1f} MB/s)", sent[0] / max(total, 1))
            self.push_file(path, f"{STAGE}/{name}.part", prog)
            self.exec(f"mv {STAGE}/{name}.part {STAGE}/{name}")
        self.ui.step("upload", "ok", f"{total >> 20} MB")

    # ---- device jobs -------------------------------------------------------------------------------
    def start_job(self, name: str, command: str):
        rc, text = self.exec(f"mkdir -p {JOBS}; sh {STAGE}/pdn-job.sh {name} {command}", timeout=60)
        self.ui.log(f"[job {name}] {text.strip()}\n")
        if rc not in (0, 3):           # 3 = already running: just follow it
            raise RuntimeError(f"Could not start the {name} job: {text.strip()}")

    def kill_job(self, name: str):
        self.exec(f"sh {BIN}/pdn-job.sh --kill {name} 2>/dev/null || sh {STAGE}/pdn-job.sh --kill {name}",
                  timeout=60)

    def follow(self, name: str, parser=None, step_key: str | None = None) -> int:
        """Stream a device job's log into the terminal until it ends; returns its exit code."""
        log = f"{JOBS}/{name}.log"
        off = 0
        carry = ""
        idle_polls = 0
        while True:
            self.check_cancel()
            try:
                rc, text = self.exec(
                    f"s=$(stat -c %s {log} 2>/dev/null || echo 0); echo \"$s\"; "
                    f"if [ -f {JOBS}/{name}.rc ]; then echo \"RC $(cat {JOBS}/{name}.rc)\"; else echo RUN; fi; "
                    f"[ \"$s\" -gt {off} ] && tail -c +{off + 1} {log} | head -c $((s - {off}))", timeout=60)
            except Exception as e:  # noqa: BLE001  – connection hiccup: the job keeps running
                self.ui.log(f"\n[lost contact with the device: {e} – retrying]\n")
                time.sleep(3)
                continue
            head, _, data = text.partition("\n")
            state, _, data = data.partition("\n")
            try:
                size = int(head.strip())
            except ValueError:
                size = off
            if data:
                self.ui.log(data)
                lines = (carry + data).split("\n")
                carry = lines.pop()
                if parser:
                    for ln in lines:
                        parser.feed(ln.rstrip("\r"))
                    overall, label, phase = parser.state()
                    self.ui.progress(overall, label, phase)
                    if step_key:
                        self.ui.step(step_key, "run", label)
                idle_polls = 0
            else:
                idle_polls += 1
            off = max(off, size)
            if state.startswith("RC") and size <= off:
                return int(state.split()[1])
            time.sleep(1.0 if idle_polls < 30 else 2.0)

    # ---- operations ---------------------------------------------------------------------------------
    def install(self, opts: Options):
        opts.validate()
        self.check_device(opts)
        files = self.prepare_files(opts)
        self.push_scripts(opts)
        self.upload(files)

        self.ui.step("install", "run")
        self.ui.progress(0, "Starting the installer on the device", -1)
        self.start_job("install", f"sh {STAGE}/pdn-install.sh {STAGE}")
        self.run_install_follow()

        self.configure_rustdesk(opts)
        self.exec(f"rm -rf {STAGE}/*.tar.gz {STAGE}/*.deb {STAGE}/{BUNDLE_TAR}")
        if opts.start:
            self.start()
        else:
            self.ui.step("start", "skip", "not requested")
        return self.status()

    def run_install_follow(self):
        rc = self.follow("install", InstallProgress(), "install")
        if rc != 0:
            self.ui.step("install", "fail", f"exit code {rc}")
            raise RuntimeError(f"The installer failed (exit code {rc}) – see the terminal. Running it again "
                               f"resumes where it stopped.")
        self.ui.step("install", "ok")
        self.ui.progress(1, "Install finished", 1)

    def configure_rustdesk(self, opts: Options):
        self.ui.step("rustdesk", "run", opts.mode)
        args = f"config {opts.mode}"
        if opts.mode == "custom":
            args += f" {shq(opts.server)} {shq(opts.key)}"
        if self.run_logged(f"sh {BIN}/pdn-rustdesk.sh {args}", timeout=180) != 0:
            raise RuntimeError("Configuring RustDesk failed – see the terminal")
        self.ui.step("rustdesk", "ok", opts.mode)

    def start(self):
        self.ui.step("start", "run")
        if self.run_logged(f"sh {BIN}/pdn-start.sh", timeout=300) != 0:
            self.ui.step("start", "fail")
            raise RuntimeError("Starting the desktop failed – see the terminal")
        self.ui.log("waiting for Plasma to come up (slower under proot) …\n")
        for _ in range(45):
            self.check_cancel()
            time.sleep(1)
        self.ui.step("start", "ok")

    def stop(self, power_off: bool = False):
        return self.run_logged(f"sh {BIN}/pdn-stop.sh" + (" --now" if power_off else ""), timeout=180)

    def status(self) -> dict:
        self.ui.step("status", "run")
        self.ui.log(f"$ sh {BIN}/pdn-status.sh\n")
        rc, text = self.exec(f"sh {BIN}/pdn-status.sh 2>&1", timeout=180)
        self.ui.log(text)
        info = {"id": "", "addrs": [], "listening": "listening: yes" in text, "port": 21118,
                "up": [ln.split("]", 1)[1].strip() for ln in text.splitlines() if "[up]" in ln],
                "down": [ln.split("]", 1)[1].strip() for ln in text.splitlines() if "[down]" in ln]}
        for ln in text.splitlines():
            if "RustDesk ID:" in ln:
                info["id"] = ln.split(":", 1)[1].strip()
            m = re.search(r"direct IP port (\d+)", ln)
            if m:
                info["port"] = int(m.group(1))
            m = re.match(r"\s+(\d+\.\d+\.\d+\.\d+)\s+\((\S+)\)", ln)
            if m:
                info["addrs"].append((m.group(1), m.group(2)))
        self.ui.step("status", "ok" if info["listening"] else "fail",
                     "RustDesk listening" if info["listening"] else "RustDesk not listening")
        return info

    def uninstall(self) -> str:
        st = self.check_device(None)
        busy = [n for n, info in st["jobs"].items() if info and info[0] == "running"]
        if busy:
            raise RuntimeError(f"A '{busy[0]}' job is still running on the device – cancel it first")
        self.ui.step("uninstall", "run")
        self.push_scripts()            # runs from the staging folder, not from what it deletes
        self.ui.progress(-1, "Stopping and deleting (a few minutes)…", -1)
        rc = self.run_logged(f"sh {STAGE}/pdn-uninstall.sh", timeout=1800)
        if rc != 0:
            self.ui.step("uninstall", "fail")
            raise RuntimeError("Uninstall stopped – see the terminal.")
        self.ui.step("uninstall", "ok")
        return "removed"

    def set_password(self, password: str):
        """The user's password goes over stdin – never on a command line, never in a file."""
        rc, text = self.exec(f"sh {BIN}/pdn-rustdesk.sh password -", stdin=password.encode(), timeout=120)
        if rc != 0 or "password set" not in text:
            raise RuntimeError(text.strip() or f"exit code {rc}")
        self.ui.log("RustDesk permanent password set\n")

    def build_bundle(self, profile: str):
        self.check_device(None)
        st = self.probe()
        if not st["installed"]:
            raise RuntimeError("The no-root desktop must be installed on this device first (it builds the bundle).")
        self.ui.step("upload", "run")
        self.push_scripts()
        self.exec(f"cp {STAGE}/pd-bundle.sh {ROOTFS}/usr/local/sbin/pd-bundle && "
                  f"cp {STAGE}/pd-packages.sh {ROOTFS}/usr/local/sbin/pd-packages && "
                  f"chmod 755 {ROOTFS}/usr/local/sbin/pd-bundle")
        self.ui.step("upload", "ok")

        self.ui.step("bundle", "run")
        self.start_job("bundle", f"sh {BIN}/pdn-proot.sh /usr/local/sbin/pd-bundle {shq(profile)}")
        rc = self.follow("bundle", BundleProgress(), "bundle")
        if rc != 0:
            self.ui.step("bundle", "fail", f"exit code {rc}")
            raise RuntimeError(f"Building the bundle failed (exit code {rc})")
        self.ui.step("bundle", "ok")

        self.ui.step("pull", "run")
        dev_tar = f"{STAGE}/pdn-{BUNDLE_TAR}"
        rc, text = self.exec(f"cd {ROOTFS}/var/cache/phonedesktop/bundle && tar -cf {dev_tar} . "
                             f"&& rm -rf {ROOTFS}/var/cache/phonedesktop/bundle && stat -c %s {dev_tar}", timeout=900)
        if rc != 0:
            raise RuntimeError("Packing the bundle on the device failed: " + text.strip())
        total = int(text.split()[-1])
        os.makedirs(self.cache_dir, exist_ok=True)
        local = os.path.join(self.cache_dir, BUNDLE_TAR + ".part")
        got = [0]

        def prog(n):
            self.check_cancel()
            got[0] += n
            self.ui.progress(-2, f"Copying the bundle – {got[0] >> 20}/{total >> 20} MB", got[0] / max(total, 1))
        try:
            self.pull_file(dev_tar, local, total, prog)
        finally:
            self.exec(f"rm -f {dev_tar}")
        new = self.repo_dir + ".new"
        shutil.rmtree(new, ignore_errors=True)
        os.makedirs(new)
        with tarfile.open(local) as t:
            t.extractall(new, filter="data")
        if not os.path.isfile(os.path.join(new, "Packages")):
            raise RuntimeError("The pulled bundle has no Packages index")
        shutil.rmtree(self.repo_dir, ignore_errors=True)
        os.replace(new, self.repo_dir)
        tar = os.path.join(self.cache_dir, BUNDLE_TAR)
        os.replace(local, tar)
        os.utime(tar)
        info = self.offline_info()
        self.ui.step("pull", "ok", f"{info['packages']} packages")
        self.ui.log(f"offline bundle ready: {info['packages']} packages in {self.repo_dir}\n")
        return info
