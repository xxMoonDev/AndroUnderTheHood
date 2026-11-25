"""PhoneDesktop engine: installs Ubuntu 24.04 + KDE Plasma (Xvfb) + RustDesk on the phone.

Two modes, chosen per connection:
  root   Root is on in the app: a real chroot in /data/local/phonedesktop (fast).
  proot  No root (ADB shell user, or AndroOpenHeart in Shizuku mode = uid 2000): Termux's proot,
         a userspace chroot, in /data/local/tmp/pdesk. Slower; no widget app / bridge / autostart.

No Qt in here. The UI passes a `ui` object with three callbacks (called from the worker thread):
    ui.log(text)                         raw terminal text
    ui.step(key, state, detail="")       state: "run" | "ok" | "fail" | "skip" | "wait"
    ui.progress(overall, label, phase)   overall/phase in 0..1, or -1 for "busy"
Device work goes through the app's connection (ADB or SSH): `conn.exec` for commands and
`fs.upload/download` for files. Long work (the install, building a bundle) runs on the
device as a detached job (pd-job.sh), and the engine follows its log, so a dropped USB/SSH
connection does not kill the install. Re-attach with `follow()`.
"""
from __future__ import annotations

import hashlib
import os
import re
import tarfile
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
# Optional office suite: arm64 repack of the official amd64 .deb. There is no upstream arm64
# build to download, so it is used only when this exact file is in the plugin's cache folder.
ELIXIROFFICE = {
    "file": "ElixirOffice-0.9.4-linux-arm64.deb",
    "sha256": "12dd059fe68932122c071843e9f91c2b18b2d99aa2068edac8be09452beda55f",
}
BUNDLE_TAR = "offline-bundle.tar"
# No-root mode: Termux's proot build (plain proot can't run dpkg on Android: hard links are
# forbidden in /data; Termux's adds --link2symlink and --sysvipc). From Termux's own repository,
# pinned by the SHA-256 in its package index; the files are taken out of the .debs on this PC.
TERMUX_BASE = "https://packages.termux.dev/apt/termux-main/"
TERMUX_DEBS = [
    {"file": "proot_5.1.107.102_aarch64.deb", "url": TERMUX_BASE + "pool/main/p/proot/proot_5.1.107.102_aarch64.deb",
     "sha256": "10a394b585c2fc2b2c8502544f4e5f1be581dc8bfff79e8ba62f16207423e030",
     "take": {"usr/bin/proot": "proot", "usr/libexec/proot/loader": "loader"}},
    {"file": "libtalloc_2.5.0_aarch64.deb", "url": TERMUX_BASE + "pool/main/libt/libtalloc/libtalloc_2.5.0_aarch64.deb",
     "sha256": "556591f43bb773ad8777e1a29522640866a55f95dab71914418b94a8c58ad5a7",
     "take": {"usr/lib/libtalloc.so.2.5.0": "libtalloc.so.2"}},
    {"file": "libandroid-shmem_0.7_aarch64.deb",
     "url": TERMUX_BASE + "pool/main/liba/libandroid-shmem/libandroid-shmem_0.7_aarch64.deb",
     "sha256": "0da3a24d558b93c92bcf8d611e0826a99ff96e396b148e6cdf33b47c47c57ff6",
     "take": {"usr/lib/libandroid-shmem.so": "libandroid-shmem.so"}},
]
PROOT_FILES = ["proot", "loader", "libtalloc.so.2", "libandroid-shmem.so"]

STAGE = "/data/local/tmp/phonedesktop"
PD_ROOT = "/data/local/phonedesktop"       # root mode
PD_PROOT = "/data/local/tmp/pdesk"          # no-root (proot) mode
SHELL_UID = "2000"
DEVICE_FILES = ["pd.conf", "pd-mount.sh", "pd-shell.sh", "pd-start.sh", "pd-stop.sh", "pd-status.sh",
                "pd-rustdesk.sh", "pd-boot.sh", "pd-job.sh", "pd-install.sh", "pd-chroot-setup.sh",
                "pd-desktop.sh", "pd-shmshim.c", "pd-loginctl.sh", "pd-packages.sh", "pd-bundle.sh",
                "pd-uninstall.sh", "pd-bridge.sh", "pd-unixshim.c", "mozilla-apt.asc",
                "pd-android-setup.sh", "pd-android-app.sh", "pd-android-sync.sh", "pd-android-watch.sh",
                "pd-sysd.sh", "pd-dbus-helper.sh"]
ANDROID_BINARIES = {"android/scrcpy-ssh-linux-arm64.tar.gz": "scrcpy-ssh-linux-arm64.tar.gz",
                    "android/pd-android-apps.jar": "pd-android-apps.jar"}
APP_PACKAGE = "dev.phonedesktop.widget"
APP_APK = "app/PhoneDesktopWidget.apk"
MIN_FREE_GB = {"minimal": 5, "standard": 8, "full": 15}

INSTALL_STEPS = [
    ("check", "Check device"),
    ("files", "Prepare files on this PC"),
    ("upload", "Upload to the device"),
    ("install", "Install Ubuntu + KDE Plasma"),
    ("rustdesk", "Configure RustDesk"),
    ("app", "Widget app"),
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


def deb_extract(deb: str, take: dict, out_dir: str):
    """Copy files out of a .deb's data.tar (an ar archive) - no external tools needed."""
    import io
    with open(deb, "rb") as f:
        if f.read(8) != b"!<arch>\n":
            raise RuntimeError(f"{os.path.basename(deb)} is not a .deb")
        while True:
            h = f.read(60)
            if len(h) < 60:
                break
            name, size = h[:16].decode().strip().rstrip("/"), int(h[48:58])
            data = f.read(size)
            if size % 2:
                f.read(1)
            if not name.startswith("data.tar"):
                continue
            with tarfile.open(fileobj=io.BytesIO(data)) as t:
                for m in t.getmembers():
                    for suffix, dest in take.items():
                        if m.isfile() and m.name.endswith("/" + suffix):
                            with open(os.path.join(out_dir, dest), "wb") as o:
                                o.write(t.extractfile(m).read())
    for dest in take.values():
        if not os.path.isfile(os.path.join(out_dir, dest)):
            raise RuntimeError(f"{dest} not found in {os.path.basename(deb)}")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class Options:
    def __init__(self, profile="standard", res="1920x1080", user="kde", mode="direct", server="", key="",
                 autostart=None, offline=False, start=True, app=True):
        self.profile, self.res, self.user, self.mode = profile, res, user, mode
        self.server, self.key, self.autostart, self.offline, self.start = server, key, autostart, offline, start
        self.app = app

    def validate(self):
        if self.profile not in MIN_FREE_GB:
            raise ValueError(f"Unknown profile {self.profile}")
        if not re.fullmatch(r"\d{3,5}x\d{3,5}", self.res):
            raise ValueError("Resolution must look like 1920x1080")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", self.user):
            raise ValueError("User name: lowercase letters, digits, _ and -")
        if self.mode not in ("direct", "public", "both", "custom"):
            raise ValueError(f"Unknown RustDesk mode {self.mode}")
        if self.mode == "custom":
            if not re.fullmatch(r"[A-Za-z0-9.:\-\[\]]+", self.server or ""):
                raise ValueError("Custom mode needs a server host (letters, digits, . : -)")
            if self.key and not re.fullmatch(r"[A-Za-z0-9+/=_\-]+", self.key):
                raise ValueError("The server key has unexpected characters")


class InstallProgress:
    """Turns the installer's output into progress. Phases are the markers pd-install.sh and
    pd-chroot-setup print; inside an apt phase, Get/Unpacking/Setting up lines count up
    against apt's "N newly installed" total."""
    PHASES = [  # (marker, label, weight)
        ("== [1/4]", "Unpacking Ubuntu", 3),
        ("-- apt update", "Updating package lists", 4),
        ("-- base packages", "Base packages, Xvfb", 12),
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
        if self.total:
            phase = min(1.0, (min(self.get, self.total) + self.unpack + self.setup) / (3 * self.total))
        else:
            phase = -1
        overall = before + (max(phase, 0) * weights[self.idx] / sum(weights))
        label = self.PHASES[self.idx][1]
        if self.total:
            label += f" – {self.setup}/{self.total} packages set up"
        return overall, label, phase


class Engine:
    def __init__(self, api, ui, files_dir: str):
        self.api = api
        self.ui = ui
        self.files_dir = files_dir
        self.cancel = False
        self.nsenter = None
        self.mode = None            # "root" | "proot", decided on first use (detect_mode)
        self.no_root_reason = ""

    # ---- mode --------------------------------------------------------------------------------
    def detect_mode(self) -> str:
        """root when the app has root switched on and working; otherwise proot, which needs the
        shell user (uid 2000: ADB, or AndroOpenHeart in Shizuku mode)."""
        if self.mode:
            return self.mode
        root_on = False
        try:
            root_on = bool(self.api.is_root())
        except Exception:  # noqa: BLE001
            pass
        if root_on:
            self.mode = "root"
            return self.mode
        rc, out, err = self.conn.exec("id -u", timeout=30, as_root=False)
        uid = out.decode(errors="replace").strip()
        self.mode = "proot"
        if uid != SHELL_UID:
            self.no_root_reason = (f"Without root this needs the shell user (uid 2000), but this connection runs "
                                   f"as uid {uid or '?'}. Connect over ADB, or use AndroOpenHeart in Shizuku "
                                   f"mode - or turn Root on.")
        return self.mode

    @property
    def proot(self) -> bool:
        return self.detect_mode() == "proot"

    @property
    def PD(self):
        return PD_PROOT if self.proot else PD_ROOT

    @property
    def BIN(self):
        return self.PD + "/bin"

    @property
    def ROOTFS(self):
        return self.PD + "/rootfs"

    @property
    def JOBS(self):
        return self.PD + "/jobs"

    # ---- helpers -------------------------------------------------------------------------
    @property
    def conn(self):
        c = self.api.conn
        if not c:
            raise RuntimeError("Not connected")
        return c

    @property
    def cache_dir(self):
        return os.path.join(self.files_dir, "cache")

    @property
    def repo_dir(self):
        return os.path.join(self.files_dir, "offline", "repo")

    def check_cancel(self):
        if self.cancel:
            raise Cancelled()

    def exec(self, cmd: str, timeout: float = 120, stdin: bytes | None = None, global_ns: bool = True):
        """Run as root (root mode; global_ns enters init's mount namespace like `su -mm`, where the
        chroot's mounts live) or as the shell user (proot mode). Returns (rc, text)."""
        if self.proot:
            rc, out, err = self.conn.exec(cmd, timeout=timeout, stdin=stdin, as_root=False)
            return rc, (out + err).decode("utf-8", "replace")
        if global_ns:
            if self.nsenter is None:
                rc, out, _ = self.conn.exec("command -v nsenter >/dev/null && echo yes", timeout=30)
                self.nsenter = b"yes" in out
            if self.nsenter:
                cmd = f"nsenter -t 1 -m -- sh -c {shq(cmd)}"
        rc, out, err = self.conn.exec(cmd, timeout=timeout, stdin=stdin, as_root=True)
        return rc, (out + err).decode("utf-8", "replace")

    def run_logged(self, cmd: str, timeout: float = 300) -> int:
        """Short device command whose output goes to the terminal."""
        self.ui.log(f"$ {cmd}\n")
        rc, text = self.exec(cmd + " 2>&1", timeout=timeout)
        if text:
            self.ui.log(text if text.endswith("\n") else text + "\n")
        return rc

    # ---- device state ----------------------------------------------------------------------
    def probe(self) -> dict:
        """Quick state for the window header."""
        ROOTFS, BIN, PD = self.ROOTFS, self.BIN, self.PD
        if self.proot:      # the desktop's processes = what its proot supervisor traces
            procs = (f"p=$(cat {PD}/desktop.pid 2>/dev/null); n=0; [ -n \"$p\" ] && "
                     f"grep -q proot /proc/$p/cmdline 2>/dev/null && n=1; echo PROCS $n; ")
        else:
            procs = f"echo PROCS $(ls -l /proc/[0-9]*/root 2>/dev/null | grep -c ' -> {ROOTFS}$'); "
        rc, text = self.exec(
            f"echo ABI $(getprop ro.product.cpu.abi); echo UID $(id -u); "
            f"test -x {ROOTFS}/usr/local/sbin/pd-chroot-setup && echo INSTALLED; "
            f"test -f {ROOTFS}/var/lib/phonedesktop/setup.done && echo SETUPDONE; "
            f"for j in install bundle android; do echo JOB $j $(sh {BIN}/pd-job.sh --status $j 2>/dev/null || echo none); done; "
            + procs +
            f"grep -qi ':527E ' /proc/net/tcp /proc/net/tcp6 2>/dev/null && echo RDLISTEN; "
            f"test -f /data/adb/service.d/phonedesktop.sh && echo AUTOSTART; "
            f"pm path {APP_PACKAGE} >/dev/null 2>&1 && echo APP; true", timeout=60)
        st = {"abi": "", "root": False, "installed": False, "setup_done": False, "rd_listen": False,
              "autostart": False, "app": False, "jobs": {}, "procs": 0, "mode": self.mode,
              "no_root_reason": self.no_root_reason, "folder": PD}
        flags = {"INSTALLED": "installed", "SETUPDONE": "setup_done", "RDLISTEN": "rd_listen",
                 "AUTOSTART": "autostart", "APP": "app"}
        for ln in text.splitlines():
            parts = ln.split()
            if not parts:
                continue
            if parts[0] == "ABI":
                st["abi"] = parts[1] if len(parts) > 1 else ""
            elif parts[0] == "UID":
                st["root"] = len(parts) > 1 and parts[1] == "0"
                st["uid"] = parts[1] if len(parts) > 1 else ""
            elif parts[0] == "JOB" and len(parts) >= 3:
                st["jobs"][parts[1]] = parts[2:]
            elif parts[0] == "PROCS" and len(parts) > 1 and parts[1].isdigit():
                st["procs"] = int(parts[1])
            elif parts[0] in flags:
                st[flags[parts[0]]] = True
        st["running"] = st["procs"] > 0
        return st

    def check_device(self, opts: Options | None):
        self.ui.step("check", "run")
        st = self.probe()
        mode = "root (chroot)" if not self.proot else "no root (proot)"
        self.ui.log(f"ABI {st['abi']}, mode {mode} in {self.PD}, "
                    f"PhoneDesktop {'installed' if st['installed'] else 'not installed'}\n")
        if st["abi"] != "arm64-v8a":
            raise RuntimeError(f"Needs an arm64 device (this one is {st['abi'] or 'unknown'})")
        if self.no_root_reason:
            raise RuntimeError(self.no_root_reason)
        if not self.proot and not st["root"]:
            raise RuntimeError("Root is switched on but su did not give root. Check Magisk, or turn Root off "
                               "to use the no-root (proot) mode.")
        if opts and not st["installed"]:
            rc, text = self.exec("df -k /data | tail -n1", timeout=30)
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
        """A local-only file (no download): its path when it is in the cache with the right
        checksum, else None (the install goes on without it)."""
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

    def proot_files(self, offline: bool) -> list[str]:
        """No-root mode: proot + its loader and libraries, taken out of Termux's packages."""
        out = os.path.join(self.cache_dir, "proot")
        os.makedirs(out, exist_ok=True)
        for item in TERMUX_DEBS:
            deb = self.fetch(item, offline)
            if not all(os.path.isfile(os.path.join(out, d)) and os.path.getmtime(os.path.join(out, d)) >=
                       os.path.getmtime(deb) for d in item["take"].values()):
                deb_extract(deb, item["take"], out)
        return [os.path.join(out, f) for f in PROOT_FILES]

    def prepare_files(self, opts: Options) -> list[str]:
        self.ui.step("files", "run")
        files = [self.fetch(UBUNTU, opts.offline), self.fetch(RUSTDESK, opts.offline)]
        if self.proot:
            files += self.proot_files(opts.offline)
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

    # ---- uploads ---------------------------------------------------------------------------------
    def push_scripts(self, opts: Options | None = None):
        """Device scripts come from inside the plugin (memory) and go over stdin – nothing is
        written to the PC's disk."""
        self.exec(f"mkdir -p {STAGE} && chmod 755 {STAGE}", global_ns=False)
        for name in DEVICE_FILES:
            data = self.api.read_file("device/" + name)
            rc, text = self.exec(f"cat > {STAGE}/{name}", stdin=data, global_ns=False)
            if rc != 0:
                raise RuntimeError(f"writing {name} failed: {text.strip()}")
        if opts:
            conf = f"PD_PROFILE={opts.profile}\nPD_RES={opts.res}\nPD_USER={opts.user}\n".encode()
            self.exec(f"cat > {STAGE}/pd.local.conf", stdin=conf, global_ns=False)
        self.ui.log(f"{len(DEVICE_FILES)} device scripts written to {STAGE}\n")

    def upload(self, files: list[str]):
        self.ui.step("upload", "run")
        total = sum(os.path.getsize(f) for f in files)
        sent = [0]
        fs = self.conn.new_fs() if hasattr(self.conn, "new_fs") else self.api.fs
        try:
            for path in files:
                name = os.path.basename(path)
                size = os.path.getsize(path)
                rc, text = self.exec(f"stat -c %s {STAGE}/{name} 2>/dev/null", global_ns=False)
                if text.strip() == str(size):
                    self.ui.log(f"{name}: already on the device\n")
                    sent[0] += size
                    continue
                self.ui.log(f"uploading {name} ({size >> 20} MB)\n")
                t0 = time.monotonic()

                def prog(n):
                    self.check_cancel()
                    sent[0] += n
                    rate = sent[0] / max(time.monotonic() - t0, 0.1)
                    self.ui.progress(-2, f"Uploading {name} – {sent[0] >> 20}/{total >> 20} MB "
                                         f"({rate / 1e6:.1f} MB/s)", sent[0] / total)
                fs.upload(path, f"{STAGE}/{name}.part", prog)
                self.exec(f"mv {STAGE}/{name}.part {STAGE}/{name}", global_ns=False)
        finally:
            if fs is not self.api.fs:
                try:
                    fs.close()
                except Exception:  # noqa: BLE001
                    pass
        self.ui.step("upload", "ok", f"{total >> 20} MB")

    # ---- device jobs -------------------------------------------------------------------------------
    def job_status(self, name: str) -> list[str]:
        rc, text = self.exec(f"sh {STAGE}/pd-job.sh --status {name} 2>/dev/null || "
                             f"sh {self.BIN}/pd-job.sh --status {name}", timeout=30)
        return text.split() or ["none"]

    def start_job(self, name: str, command: str):
        rc, text = self.exec(f"sh {STAGE}/pd-job.sh {name} {command}", timeout=60)
        self.ui.log(f"[job {name}] {text.strip()}\n")
        if rc not in (0, 3):           # 3 = already running: just follow it
            raise RuntimeError(f"Could not start the {name} job: {text.strip()}")

    def kill_job(self, name: str):
        self.exec(f"sh {self.BIN}/pd-job.sh --kill {name} 2>/dev/null || sh {STAGE}/pd-job.sh --kill {name}",
                  timeout=60)

    def follow(self, name: str, parser=None, step_key: str | None = None) -> int:
        """Stream a device job's log into the terminal until it ends; returns its exit code."""
        JOBS = self.JOBS
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
            if state.startswith("RC"):
                if size <= off:
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
        self.start_job("install", f"sh {STAGE}/pd-install.sh {STAGE}")
        self.run_install_follow()

        self.configure_rustdesk(opts)
        if opts.autostart is not None and not self.proot:
            self.set_autostart(opts.autostart)
        self.exec(f"rm -rf {STAGE}/*.tar.gz {STAGE}/*.deb {STAGE}/{BUNDLE_TAR} {STAGE}/repo", global_ns=False)
        if opts.app:      # root: through su (Magisk); no root: through Shizuku (app 1.3+)
            self.install_app(launch=True)
        else:
            self.ui.step("app", "skip", "not requested")
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
        if self.run_logged(f"sh {self.BIN}/pd-rustdesk.sh {args}", timeout=180) != 0:
            raise RuntimeError("Configuring RustDesk failed – see the terminal")
        self.ui.step("rustdesk", "ok", opts.mode)

    def set_autostart(self, on: bool):
        if self.proot:
            self.ui.log("start at boot needs root (Magisk service.d) - not available without root\n")
            return
        if on:
            self.run_logged(f"mkdir -p /data/adb/service.d && cp {self.BIN}/pd-boot.sh /data/adb/service.d/phonedesktop.sh"
                            f" && chmod 755 /data/adb/service.d/phonedesktop.sh && echo 'boot autostart enabled'")
        else:
            self.run_logged("rm -f /data/adb/service.d/phonedesktop.sh && echo 'boot autostart disabled'")

    def start(self):
        self.ui.step("start", "run")
        if self.run_logged(f"sh {self.BIN}/pd-start.sh", timeout=180) != 0:
            self.ui.step("start", "fail")
            raise RuntimeError("Starting the desktop failed – see the terminal")
        self.ui.log("waiting for Plasma to come up …\n")
        for _ in range(30):
            self.check_cancel()
            time.sleep(1)
        self.ui.step("start", "ok")

    def stop(self, power_off: bool = False):
        """Shut down (Plasma logs out cleanly, mounts stay) or power off (immediate, and unmount)."""
        return self.run_logged(f"sh {self.BIN}/pd-stop.sh" + (" --now umount" if power_off else ""), timeout=180)

    def status(self) -> dict:
        self.ui.step("status", "run")
        self.ui.log(f"$ sh {self.BIN}/pd-status.sh\n")
        rc, text = self.exec(f"sh {self.BIN}/pd-status.sh 2>&1", timeout=180)
        self.ui.log(text)
        info = {"id": "", "addrs": [], "listening": "listening: yes" in text,
                "up": [ln.split("]", 1)[1].strip() for ln in text.splitlines() if "[up]" in ln],
                "down": [ln.split("]", 1)[1].strip() for ln in text.splitlines() if "[down]" in ln]}
        for ln in text.splitlines():
            if "RustDesk ID:" in ln:
                info["id"] = ln.split(":", 1)[1].strip()
            m = re.match(r"\s+(\d+\.\d+\.\d+\.\d+)\s+\((\S+)\)", ln)
            if m:
                info["addrs"].append((m.group(1), m.group(2)))
        self.ui.step("status", "ok" if info["listening"] else "fail",
                     "RustDesk listening" if info["listening"] else "RustDesk not listening")
        return info

    def stream(self, cmd: str, timeout: float = 1800) -> int:
        """Device command with its output streamed live into the terminal; returns the exit code."""
        self.ui.log(f"$ {cmd}\n")
        if self.proot:            # the app's streaming exec always uses su: run it whole instead
            rc, text = self.exec(cmd + " 2>&1", timeout=timeout)
            self.ui.log(text if text.endswith("\n") else text + "\n")
            return rc
        if self.nsenter is None:
            self.exec("true", timeout=30)              # detects nsenter
        full = f"nsenter -t 1 -m -- sh -c {shq(cmd + ' 2>&1')}" if self.nsenter else cmd + " 2>&1"
        try:
            self.conn.exec_stream_out(full, lambda b: self.ui.log(b.decode("utf-8", "replace")), timeout=timeout)
            return 0
        except Exception as e:  # noqa: BLE001 – exec_stream_out raises on a non-zero exit code
            self.ui.log(f"[{e}]\n")
            return 1

    def install_app(self, launch: bool = True) -> str:
        """Installs the widget app that ships inside this plugin. The APK travels base64-encoded over
        stdin (binary-safe on ADB too) and is never written to the PC's disk."""
        import base64
        self.ui.step("app", "run")
        apk = self.api.read_file(APP_APK)
        self.ui.log(f"installing the widget app ({len(apk) >> 10} KB)\n")
        tmp = "/data/local/tmp/phonedesktop-widget.apk"
        rc, text = self.exec(f"base64 -d > {tmp} && chmod 644 {tmp} && pm install -r {tmp}; r=$?; rm -f {tmp}; "
                             f"exit $r", stdin=base64.b64encode(apk), timeout=180, global_ns=False)
        self.ui.log(text if text.endswith("\n") else text + "\n")
        if rc != 0 or "Success" not in text:
            if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in text:
                raise RuntimeError("The installed widget app is signed with another key – uninstall "
                                   "'Ubuntu desktop' on the phone, then install it again")
            self.ui.step("app", "fail")
            raise RuntimeError("Installing the widget app failed: " + text.strip()[-200:])
        if launch:     # opening it once makes Magisk (root) or the app's Shizuku button ask for access
            self.exec(f"am start -n {APP_PACKAGE}/.MainActivity >/dev/null 2>&1", timeout=30, global_ns=False)
            self.ui.log("opened the app on the phone – "
                        + ("tap \"Allow in Shizuku\" there (Shizuku must be running)" if self.proot
                           else "allow root in the Magisk prompt") + ", then add the widget\n")
        self.ui.step("app", "ok")
        return "installed"

    def uninstall(self, keep_app: bool) -> str:
        st = self.check_device(None)
        busy = [n for n, info in st["jobs"].items() if info and info[0] == "running"]
        if busy:
            raise RuntimeError(f"A '{busy[0]}' job is still running on the device – cancel it first")
        self.ui.step("uninstall", "run")
        self.push_scripts()            # pd-uninstall.sh runs from the staging folder, not from what it deletes
        self.ui.progress(-1, "Stopping, unmounting and deleting (about a minute)…", -1)
        rc = self.stream(f"sh {STAGE}/pd-uninstall.sh" + (" --keep-app" if keep_app else ""))
        if rc != 0:
            self.ui.step("uninstall", "fail")
            raise RuntimeError("Uninstall stopped – see the terminal. Nothing is deleted while anything is "
                               "still mounted.")
        self.ui.step("uninstall", "ok")
        return "removed"

    # ---- phone <-> Ubuntu bridge (clipboard, notifications, desktop sound on the phone) -------------------
    BRIDGE_KEYS = (("clipboard", "BRIDGE_CLIPBOARD", True), ("notifications", "BRIDGE_NOTIFICATIONS", True),
                   ("audio", "BRIDGE_AUDIO", False), ("autostart", "BRIDGE_AUTOSTART", False))
    BRIDGE_MIN_APP = 2          # widget app versionCode that contains the bridge helper

    def bridge_state(self) -> dict:
        PD, BIN = self.PD, self.BIN
        rc, text = self.exec(f"cat {PD}/bridge.conf 2>/dev/null; echo ==; "
                             f"[ -f {BIN}/pd-bridge.sh ] && sh {BIN}/pd-bridge.sh status 2>&1; echo ==; "
                             f"dumpsys package {APP_PACKAGE} 2>/dev/null | grep -m1 versionCode=", timeout=60)
        conf, status, ver = (text.split("==\n") + ["", ""])[:3]
        st = {k: d for k, _, d in self.BRIDGE_KEYS}
        for k, var, _ in self.BRIDGE_KEYS:
            m = re.search(rf"^{var}=([01])", conf, re.M)
            if m:
                st[k] = m.group(1) == "1"
        m = re.search(r"versionCode=(\d+)", ver)
        st["app_version"] = int(m.group(1)) if m else 0
        st["running"] = status.startswith("bridge running")
        st["log"] = "\n".join(ln for ln in status.splitlines()[1:] if ln.strip())
        return st

    def bridge_apply(self, settings: dict, run: bool) -> dict:
        """Saves the bridge settings, then (re)starts the bridge (run=True) or stops it."""
        if self.proot:
            raise RuntimeError("The phone bridge needs root for now (its helper enters Ubuntu with chroot).")
        st = self.check_device(None)
        if not st["installed"]:
            raise RuntimeError("Install the desktop first.")
        self.ui.step("bridge", "run")
        # older installs: bring the bridge script (and the start/stop scripts that know it) up to date
        self.push_scripts()
        self.exec(f"for f in pd-bridge.sh pd-start.sh pd-stop.sh pd.conf; do cp {STAGE}/$f {self.BIN}/$f; done; "
                  f"chmod 755 {self.BIN}/*.sh")
        if run and self.bridge_state()["app_version"] < self.BRIDGE_MIN_APP:
            self.ui.log("the bridge helper lives in the widget app – installing the current version\n")
            self.install_app(launch=False)
        kv = " ".join(f"{k}={1 if settings.get(k) else 0}" for k, _, _ in self.BRIDGE_KEYS)
        BIN = self.BIN
        cmds = [f"sh {BIN}/pd-bridge.sh config {kv}", f"sh {BIN}/pd-bridge.sh stop"]
        if run:
            cmds.append(f"sh {BIN}/pd-bridge.sh start")
        for c in cmds:
            self.ui.log(f"$ {c}\n")
            rc, text = self.exec(c + " 2>&1", timeout=90)
            self.ui.log(text if text.endswith("\n") else text + "\n")
            if rc != 0:
                self.ui.step("bridge", "fail")
                raise RuntimeError(text.strip().splitlines()[-1] if text.strip() else f"exit code {rc}")
        self.ui.step("bridge", "ok", "running" if run else "stopped")
        return self.bridge_state()

    def set_password(self, password: str):
        """The user's password goes over stdin – never on a command line, never in a file."""
        rc, text = self.exec(f"sh {self.BIN}/pd-rustdesk.sh password -", stdin=password.encode(), timeout=120)
        if rc != 0 or "password set" not in text:
            raise RuntimeError(text.strip() or f"exit code {rc}")
        self.ui.log("RustDesk permanent password set\n")

    # ---- Android apps inside Ubuntu (scrcpy-ssh windows + start-menu entries) ---------------------------
    def android_state(self) -> dict:
        rc, text = self.exec(f"[ -f {self.BIN}/pd-android-setup.sh ] && sh {self.BIN}/pd-android-setup.sh status; "
                             f"pm path dev.rootssh >/dev/null 2>&1 && echo AOH_INSTALLED; "
                             f"dumpsys package dev.rootssh 2>/dev/null | grep -m1 versionName=", timeout=90)
        st = {"setup": text.startswith("set up:") or "\nset up:" in text, "target": "", "apps": 0,
              "watcher": "watcher: running" in text, "aoh": "AOH_INSTALLED" in text, "aoh_version": "",
              "text": text.strip()}
        m = re.search(r"set up: (\S+)", text)
        if m:
            st["target"] = m.group(1)
        m = re.search(r"apps in the menu: (\d+)", text)
        if m:
            st["apps"] = int(m.group(1))
        m = re.search(r"versionName=(\S+)", text)
        if m:
            st["aoh_version"] = m.group(1)
        return st

    def _push_android_binaries(self):
        import base64
        for src, name in ANDROID_BINARIES.items():
            data = self.api.read_file(src)
            rc, text = self.exec(f"base64 -d > {STAGE}/{name}.part && mv {STAGE}/{name}.part {STAGE}/{name}",
                                 stdin=base64.b64encode(data), timeout=300, global_ns=False)
            if rc != 0:
                raise RuntimeError(f"uploading {name} failed: {text.strip()}")
            self.ui.log(f"{name} ({len(data) >> 10} KB) uploaded\n")

    def android_setup(self):
        """Installs scrcpy-ssh + the app helper in Ubuntu, authorizes the desktop user's SSH key in
        AndroOpenHeart and fills the start menu. Runs as a device job: AndroOpenHeart may restart, which
        drops an SSH connection to the phone (the job goes on; reconnect and the window follows it)."""
        st = self.check_device(None)
        if not st["installed"]:
            raise RuntimeError("Install the desktop first.")
        self.ui.step("upload", "run")
        self.push_scripts()
        self._push_android_binaries()
        # the current helper scripts: the setup script itself, and start/stop/desktop that know the watcher
        self.exec(f"for f in pd-android-setup.sh pd-stop.sh pd.conf; do cp {STAGE}/$f {self.BIN}/$f; done; "
                  f"cp {STAGE}/pd-desktop.sh {self.ROOTFS}/usr/local/sbin/pd-desktop; "
                  f"chmod 755 {self.BIN}/*.sh {self.ROOTFS}/usr/local/sbin/pd-desktop")
        self.ui.step("upload", "ok")
        self.ui.step("android", "run")
        self.start_job("android", f"sh {self.BIN}/pd-android-setup.sh setup {STAGE}")
        rc = self.follow("android")
        self.exec(f"rm -f {STAGE}/scrcpy-ssh-linux-arm64.tar.gz", global_ns=False)
        if rc != 0:
            self.ui.step("android", "fail", f"exit code {rc}")
            raise RuntimeError("Setting up Android apps failed - see the terminal")
        self.ui.step("android", "ok")
        return self.android_state()

    def android_cmd(self, what: str):
        """sync (refresh the menu now) or remove."""
        self.ui.step("android", "run", what)
        rc = self.run_logged(f"sh {self.BIN}/pd-android-setup.sh {what}", timeout=600)
        self.ui.step("android", "ok" if rc == 0 else "fail")
        if rc != 0:
            raise RuntimeError(f"Android apps {what} failed - see the terminal")
        return self.android_state()

    def build_bundle(self, profile: str):
        self.check_device(None)
        st = self.probe()
        if not st["installed"]:
            raise RuntimeError("PhoneDesktop must be installed on this device first (it builds the bundle).")
        self.ui.step("upload", "run")
        self.push_scripts()
        ROOTFS, BIN = self.ROOTFS, self.BIN
        self.exec(f"cp {STAGE}/pd-bundle.sh {ROOTFS}/usr/local/sbin/pd-bundle && "
                  f"cp {STAGE}/pd-packages.sh {ROOTFS}/usr/local/sbin/pd-packages && "
                  f"chmod 755 {ROOTFS}/usr/local/sbin/pd-bundle")
        self.ui.step("upload", "ok")

        self.ui.step("bundle", "run")
        self.start_job("bundle", f"sh {BIN}/pd-shell.sh /usr/local/sbin/pd-bundle {shq(profile)}")
        rc = self.follow("bundle", BundleProgress(), "bundle")
        if rc != 0:
            self.ui.step("bundle", "fail", f"exit code {rc}")
            raise RuntimeError(f"Building the bundle failed (exit code {rc})")
        self.ui.step("bundle", "ok")

        self.ui.step("pull", "run")
        dev_tar = f"/data/local/tmp/pd-{BUNDLE_TAR}"
        tar = "/system/bin/tar" if self.proot else "/data/adb/magisk/busybox tar"
        rc, text = self.exec(f"cd {ROOTFS}/var/cache/phonedesktop/bundle && {tar} -cf {dev_tar} . "
                             f"&& chmod 644 {dev_tar} && rm -rf {ROOTFS}/var/cache/phonedesktop/bundle "
                             f"&& stat -c %s {dev_tar}", timeout=900)
        if rc != 0:
            raise RuntimeError("Packing the bundle on the device failed: " + text.strip())
        total = int(text.split()[-1])
        os.makedirs(self.cache_dir, exist_ok=True)
        local = os.path.join(self.cache_dir, BUNDLE_TAR + ".part")
        got = [0]

        def prog(n):
            self.check_cancel()
            got[0] += n
            self.ui.progress(-2, f"Copying the bundle – {got[0] >> 20}/{total >> 20} MB", got[0] / total)
        fs = self.conn.new_fs() if hasattr(self.conn, "new_fs") else self.api.fs
        try:
            fs.download(dev_tar, local, prog)
        finally:
            if fs is not self.api.fs:
                fs.close()
            self.exec(f"rm -f {dev_tar}", global_ns=False)
        # unpack to offline/repo (what deploy.bat uses); keep the tar for the next upload
        new = self.repo_dir + ".new"
        import shutil
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
        os.utime(tar)          # newer than the repo -> reused as is
        info = self.offline_info()
        self.ui.step("pull", "ok", f"{info['packages']} packages")
        self.ui.log(f"offline bundle ready: {info['packages']} packages in {self.repo_dir}\n")
        return info


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
