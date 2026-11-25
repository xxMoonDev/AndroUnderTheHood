"""Backup / restore engine for the Reset & Restore plugin (no Qt in here).

Backup folder layout (one folder per backup):
    manifest.json             device info, app list, what was saved, sizes, sha256 of every file
    apps/<pkg>/*.apk          APKs (base + splits) of user-installed apps
    data/<pkg>.tar            app data: /data/data/<pkg>, /data/user_de/0/<pkg>, Android/data/<pkg>   (root)
    storage.tar               internal storage without Android/data and Android/obb
    system/telephony.tar      SMS / MMS provider                                               (root)
    system/contacts.tar       contacts + call log provider                                     (root)
    system/wifi.tar           saved Wi-Fi networks                                             (root)
    magisk/modules.tar        Magisk modules                                                   (root)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
import zipfile

_NOWIN = 0x08000000 if sys.platform == "win32" else 0

SKIP_APPS = {"dev.rootssh", "moe.shizuku.privileged.api"}     # put back by the AndroOpenHeart setup
NO_DATA = SKIP_APPS | {"com.topjohnwu.magisk"}
PROVIDERS = {
    "telephony": ("Messages (SMS / MMS)", "com.android.providers.telephony"),
    "contacts": ("Contacts & call log", "com.android.providers.contacts"),
}
WIFI_DIRS = ["/data/misc/apexdata/com.android.wifi", "/data/misc/wifi"]
CACHE_EXCLUDES = ["cache", "code_cache"]


def q(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


class Cancelled(Exception):
    pass


def relabel(dirs: list[str]) -> str:
    """Shell snippet: give everything inside each folder the folder's own SELinux label.
    App data labels carry per-app categories (e.g. app_data_file:s0:c251,c256,c512,c768) that `restorecon`
    can drop when run from a root shell, which locks the app out of its own files. Android labels all of an
    app's data with the label of its top folder, so copying that label down is exact."""
    return "; ".join(f'[ -d {d} ] && chcon -R "$(stat -c %C {d})" {d} 2>/dev/null' for d in dirs)


class Device:
    """Shell access through AndroUnderTheHood's connection (SSH or ADB), with streaming in both directions."""

    def __init__(self, conn):
        self.conn = conn
        self.adb = bool(getattr(conn, "is_adb", False))
        if not self.adb and getattr(conn, "transport", None) is None:
            raise RuntimeError("Not connected – connect to the phone again")

    def sh(self, cmd: str, root: bool = False, timeout: float = 300) -> tuple[int, str, str]:
        rc, out, err = self.conn.exec(cmd, timeout=timeout, as_root=root)
        return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def _chan(self, cmd: str, root: bool):
        if self.conn.transport is None or not self.conn.transport.is_active():
            raise RuntimeError("The SSH connection was lost – reconnect and start again")
        chan = self.conn.open_channel(root)
        chan.settimeout(600)
        chan.exec_command(self.conn.wrap_root(cmd) if root else cmd)
        return chan

    # ---- ADB --------------------------------------------------------------------
    # adb.exe on Windows may turn \n into \r\n on output (conn.crlf says whether this adb does) and stops
    # reading stdin at a 0x1A byte, so input files are sent with `adb push` instead of a pipe.
    def _adb_popen(self, cmd: str, root: bool, stdin):
        argv = self.conn._argv(cmd, root)
        return subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=_NOWIN)

    def _adb_stream_out(self, cmd, root, sink, cancel):
        p = self._adb_popen(cmd, root, subprocess.DEVNULL)
        err = bytearray()
        t = threading.Thread(target=lambda: err.extend(p.stderr.read()), daemon=True)
        t.start()
        crlf = bool(getattr(self.conn, "crlf", False))
        carry = b""
        try:
            while True:
                if cancel():
                    p.kill()
                    raise Cancelled()
                b = p.stdout.read1(262144) if hasattr(p.stdout, "read1") else p.stdout.read(262144)
                if not b:
                    break
                if crlf:
                    b = carry + b
                    carry = b""
                    if b.endswith(b"\r"):          # maybe the first half of a translated \r\n
                        b, carry = b[:-1], b"\r"
                    b = b.replace(b"\r\n", b"\n")
                if b:
                    sink(b)
            if carry:
                sink(carry)
            rc = p.wait()
            t.join(5)
            return rc, err.decode("utf-8", "replace")
        finally:
            if p.poll() is None:
                p.kill()

    def _adb_stream_in(self, cmd, root, path, progress, cancel):
        import uuid
        if cancel():
            raise Cancelled()
        stage = f"/data/local/tmp/.aoh-restore-{uuid.uuid4().hex[:10]}"
        from androunderthehood import adbconn
        r = adbconn.adb(["-s", self.conn.serial, "push", path, stage], timeout=3600)
        if r.returncode != 0:
            return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")
        if progress:
            progress(os.path.getsize(path))
        try:
            rc, out, err = self.sh(f"({cmd}) < {q(stage)}", root=root, timeout=3600)
            return rc, out + err
        finally:
            self.sh(f"rm -f {q(stage)}", root=True, timeout=60)

    def stream_out(self, cmd: str, root: bool, sink, cancel=lambda: False) -> tuple[int, str]:
        """Run cmd and hand its stdout to sink(bytes). Returns (exit code, stderr)."""
        if self.adb:
            return self._adb_stream_out(cmd, root, sink, cancel)
        chan = self._chan(cmd, root)
        chan.shutdown_write()
        err = bytearray()
        try:
            while True:
                if cancel():
                    raise Cancelled()
                if chan.recv_ready():
                    sink(chan.recv(262144))
                elif chan.recv_stderr_ready():
                    err += chan.recv_stderr(65536)
                elif chan.exit_status_ready() and not chan.recv_ready():
                    while chan.recv_stderr_ready():
                        err += chan.recv_stderr(65536)
                    break
                else:
                    time.sleep(0.003)
            return chan.recv_exit_status(), err.decode("utf-8", "replace")
        finally:
            chan.close()

    def stream_in(self, cmd: str, root: bool, path: str, progress=None, cancel=lambda: False) -> tuple[int, str]:
        """Run cmd with the local file `path` as its stdin. Returns (exit code, stdout+stderr)."""
        if self.adb:
            return self._adb_stream_in(cmd, root, path, progress, cancel)
        chan = self._chan(cmd, root)
        out = bytearray()
        try:
            with open(path, "rb") as f:
                done = 0
                while True:
                    if cancel():
                        raise Cancelled()
                    b = f.read(262144)
                    if not b:
                        break
                    chan.sendall(b)
                    done += len(b)
                    if progress:
                        progress(len(b))
                    while chan.recv_ready():
                        out += chan.recv(65536)
                    while chan.recv_stderr_ready():
                        out += chan.recv_stderr(65536)
            chan.shutdown_write()
            while not chan.exit_status_ready() or chan.recv_ready() or chan.recv_stderr_ready():
                if chan.recv_ready():
                    out += chan.recv(65536)
                elif chan.recv_stderr_ready():
                    out += chan.recv_stderr(65536)
                else:
                    time.sleep(0.01)
            return chan.recv_exit_status(), out.decode("utf-8", "replace")
        finally:
            chan.close()


class _HashWriter:
    def __init__(self, path):
        self.f = open(path, "wb")
        self.h = hashlib.sha256()
        self.n = 0

    def __call__(self, b):
        self.f.write(b)
        self.h.update(b)
        self.n += len(b)

    def close(self):
        self.f.close()


# ---------------------------------------------------------------------------
# Survey
# ---------------------------------------------------------------------------
def device_info(dev: Device) -> dict:
    keys = {"brand": "ro.product.brand", "manufacturer": "ro.product.manufacturer", "model": "ro.product.model",
            "device": "ro.product.device", "android": "ro.build.version.release", "sdk": "ro.build.version.sdk",
            "fingerprint": "ro.build.fingerprint", "serial": "ro.serialno"}
    rc, out, _ = dev.sh("; ".join(f"echo \"{k}=$(getprop {p})\"" for k, p in keys.items()))
    info = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
    rc, out, _ = dev.sh("magisk -v 2>/dev/null")
    info["magisk"] = out.strip()
    return info


def list_apps(dev: Device) -> list[dict]:
    rc, out, err = dev.sh("pm list packages -3 --show-versioncode -i 2>/dev/null")
    apps = []
    for ln in out.splitlines():
        m = re.match(r"package:(\S+)\s+versionCode:(\d+)(?:\s+installer=(\S+))?", ln.strip())
        if m and m.group(1) not in SKIP_APPS:
            apps.append({"pkg": m.group(1), "versionCode": int(m.group(2)),
                         "installer": "" if m.group(3) in (None, "null") else m.group(3)})
    return sorted(apps, key=lambda a: a["pkg"])


def estimate(dev: Device, root: bool, apps: list[dict]) -> dict:
    """Rough sizes (KB) of each part, for the PC free-space check."""
    est = {}
    if root:
        pk = " ".join(q(f"/data/data/{a['pkg']}") + " " + q(f"/data/user_de/0/{a['pkg']}") + " " +
                      q(f"/data/media/0/Android/data/{a['pkg']}") for a in apps if a["pkg"] not in NO_DATA)
        rc, out, _ = dev.sh(f"du -sk {pk} 2>/dev/null | awk '{{s+=$1}} END {{print s+0}}'", root=True, timeout=600)
        est["app_data"] = int(out.strip() or 0)
        rc, out, _ = dev.sh("du -sk /data/media/0 /data/media/0/Android/data /data/media/0/Android/obb 2>/dev/null",
                            root=True, timeout=900)
        v = [int(x.split()[0]) for x in out.splitlines() if x.split() and x.split()[0].isdigit()]
        est["storage"] = max(0, v[0] - sum(v[1:])) if v else 0
    else:
        rc, out, _ = dev.sh("du -sk /sdcard/ 2>/dev/null | tail -1", timeout=900)
        est["storage"] = int((out.split() or ["0"])[0]) if out.strip()[:1].isdigit() else 0
    rc, out, _ = dev.sh("for p in $(pm list packages -3 | cut -d: -f2); do pm path $p; done | cut -d: -f2 | "
                        "xargs stat -c %s 2>/dev/null | awk '{s+=$1} END {print int(s/1024)}'", timeout=300)
    est["apps"] = int(out.strip() or 0)
    return est


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
class Backup:
    def __init__(self, dev: Device, folder: str, say, progress=None, cancel=lambda: False):
        self.dev, self.folder, self.say = dev, folder, say
        self.progress = progress or (lambda *a: None)
        self.cancel = cancel
        self.files: dict[str, dict] = {}
        self.errors: list[str] = []

    def _save(self, rel: str, cmd: str, root: bool, allow_warn=True) -> int:
        path = os.path.join(self.folder, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        w = _HashWriter(path + ".part")
        try:
            rc, err = self.dev.stream_out(cmd, root, lambda b: (w(b), self.progress(len(b))), self.cancel)
        except BaseException:
            w.close()
            os.remove(path + ".part")
            raise
        w.close()
        if rc != 0 and not (allow_warn and w.n > 0):
            os.remove(path + ".part")
            raise RuntimeError(f"{rel}: {err.strip()[:300] or f'exit code {rc}'}")
        if rc != 0:
            self.errors.append(f"{rel}: warnings: {err.strip()[:200]}")
        os.replace(path + ".part", path)
        self.files[rel] = {"size": w.n, "sha256": w.h.hexdigest()}
        return w.n

    @staticmethod
    def _tar(paths: list[str], excludes=()) -> str:
        """tar of absolute paths (stored relative to /), only those that exist."""
        ex = " ".join(f"--exclude {q(e)}" for e in excludes)
        rel = " ".join(q(p.lstrip("/")) for p in paths)
        return (f"cd / && l=''; for p in {rel}; do [ -e \"$p\" ] && l=\"$l $p\"; done; "
                f"[ -n \"$l\" ] && tar -cf - {ex} $l")

    def run(self, items: dict, apps: list[dict], info: dict) -> dict:
        os.makedirs(self.folder, exist_ok=True)
        root = items.get("root", False)
        man = {"format": 1, "created": dt.datetime.now().isoformat(timespec="seconds"), "device": info,
               "items": {}, "apps": [], "magisk": {}}

        if items.get("apps") or items.get("app_data"):
            for i, a in enumerate(apps):
                if self.cancel():
                    raise Cancelled()
                pkg = a["pkg"]
                entry = dict(a)
                self.say(f"App {i + 1}/{len(apps)}: {pkg}")
                if items.get("apps"):
                    rc, out, _ = self.dev.sh(f"pm path {q(pkg)}")
                    apks = [ln[8:].strip() for ln in out.splitlines() if ln.startswith("package:")]
                    entry["apks"] = []
                    if not apks:
                        self.errors.append(f"{pkg}: the phone reported no APK files")
                    for apk in apks:
                        name = os.path.basename(apk)
                        try:
                            self._save(f"apps/{pkg}/{name}", f"cat {q(apk)}", False, allow_warn=False)
                            entry["apks"].append(name)
                        except RuntimeError as e:      # that file failed; connection problems stop the backup
                            self.errors.append(f"{pkg} {name}: {e}")
                if items.get("app_data") and root and pkg not in NO_DATA:
                    try:
                        self._save(f"data/{pkg}.tar", self._tar(
                            [f"/data/data/{pkg}", f"/data/user_de/0/{pkg}", f"/data/media/0/Android/data/{pkg}"],
                            CACHE_EXCLUDES), True)
                        entry["data"] = True
                    except RuntimeError as e:
                        self.errors.append(f"{pkg} data: {e}")
                man["apps"].append(entry)
            man["items"]["apps"] = bool(items.get("apps"))
            man["items"]["app_data"] = bool(items.get("app_data") and root)

        if items.get("storage"):
            self.say("Internal storage (this can take a while)…")
            ex = ["./Android/data", "./Android/obb", "./.thumbnails", "./.transforms", "./Android/media/*/cache"]
            exs = " ".join(f"--exclude {q(e)}" for e in ex)
            if root:
                cmd = f"cd /data/media/0 && tar -cf - {exs} ."
            else:
                cmd = f"cd /sdcard/ && tar -cf - {exs} ."
            self._save("storage.tar", cmd, root)
            man["items"]["storage"] = True

        if root:
            for key, (label, pkg) in PROVIDERS.items():
                if items.get(key):
                    self.say(label + "…")
                    try:
                        self._save(f"system/{key}.tar", self._tar([f"/data/data/{pkg}", f"/data/user_de/0/{pkg}"],
                                                                  CACHE_EXCLUDES), True)
                        man["items"][key] = True
                    except RuntimeError as e:
                        self.errors.append(f"{label}: {e}")
            if items.get("wifi"):
                self.say("Wi-Fi networks…")
                try:
                    self._save("system/wifi.tar", self._tar([d + "/WifiConfigStore.xml" for d in WIFI_DIRS] +
                                                            [d + "/WifiConfigStoreSoftAp.xml" for d in WIFI_DIRS]), True)
                    man["items"]["wifi"] = True
                except RuntimeError as e:
                    self.errors.append(f"Wi-Fi: {e}")
            if items.get("magisk") and info.get("magisk"):
                self.say("Magisk modules and settings…")
                try:
                    rc, out, _ = self.dev.sh("ls /data/adb/modules 2>/dev/null", root=True)
                    if out.strip():
                        self._save("magisk/modules.tar", self._tar(["/data/adb/modules"]), True)
                    rc, s, _ = self.dev.sh("magisk --sqlite 'SELECT key, value FROM settings'", root=True)
                    rc, d, _ = self.dev.sh("magisk --denylist ls 2>/dev/null", root=True)
                    man["magisk"] = {
                        "version": info["magisk"],
                        "modules": [m for m in out.split() if m],
                        "settings": {ln.split("|")[0][4:]: ln.split("|")[1][6:] for ln in s.splitlines() if "|" in ln},
                        "denylist": [ln.strip() for ln in d.splitlines() if "|" in ln],
                    }
                    man["items"]["magisk"] = True
                except RuntimeError as e:
                    self.errors.append(f"Magisk: {e}")

        man["files"] = self.files
        man["errors"] = self.errors
        man["total_bytes"] = sum(f["size"] for f in self.files.values())
        with open(os.path.join(self.folder, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(man, f, indent=2)
        return man


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
def load_manifest(folder: str) -> dict:
    with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def verify(folder: str, say, progress=None, cancel=lambda: False) -> tuple[bool, list[str]]:
    """Re-hash every file and check that tars and APKs open. Returns (ok, problems)."""
    man = load_manifest(folder)
    problems = []
    for rel, meta in man.get("files", {}).items():
        if cancel():
            raise Cancelled()
        path = os.path.join(folder, rel)
        say(f"Checking {rel}")
        if not os.path.isfile(path):
            problems.append(f"missing: {rel}")
            continue
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
                if progress:
                    progress(len(chunk))
        if h.hexdigest() != meta["sha256"]:
            problems.append(f"changed or damaged: {rel}")
            continue
        try:
            if rel.endswith(".tar"):
                with tarfile.open(path) as t:
                    for _m in t:
                        pass
            elif rel.endswith(".apk"):
                with zipfile.ZipFile(path) as z:
                    if "AndroidManifest.xml" not in z.namelist():
                        problems.append(f"not an APK: {rel}")
        except Exception as e:  # noqa: BLE001
            problems.append(f"unreadable: {rel} ({e})")
    return not problems, problems


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
class Restore:
    def __init__(self, dev: Device, folder: str, say, progress=None, cancel=lambda: False):
        self.dev, self.folder, self.say = dev, folder, say
        self.progress = progress or (lambda *a: None)
        self.cancel = cancel
        self.man = load_manifest(folder)
        self.errors: list[str] = []
        self.done: list[str] = []

    def _p(self, rel):
        return os.path.join(self.folder, rel)

    def installed(self) -> dict[str, int]:
        rc, out, _ = self.dev.sh("pm list packages --show-versioncode 2>/dev/null")
        res = {}
        for ln in out.splitlines():
            m = re.match(r"package:(\S+)\s+versionCode:(\d+)", ln.strip())
            if m:
                res[m.group(1)] = int(m.group(2))
        return res

    def install_app(self, a: dict, installed: dict) -> bool:
        pkg = a["pkg"]
        if installed.get(pkg, -1) >= a.get("versionCode", 0):
            return True
        apks = [n for n in a.get("apks", []) if os.path.isfile(self._p(f"apps/{pkg}/{n}"))]
        if not apks:
            self.errors.append(f"{pkg}: no APK in the backup")
            return False
        tmp = f"/data/local/tmp/aoh-restore/{pkg}"
        self.dev.sh(f"rm -rf {q(tmp)}; mkdir -p {q(tmp)}")
        for n in apks:
            rc, out = self.dev.stream_in(f"cat > {q(tmp + '/' + n)}", False, self._p(f"apps/{pkg}/{n}"),
                                         self.progress, self.cancel)
            if rc != 0:
                self.errors.append(f"{pkg}: copying {n} failed: {out[:200]}")
                return False
        inst = f"-i {q(a['installer'])} " if a.get("installer") else ""
        script = (f"cd {q(tmp)} && sid=$(pm install-create -r -g {inst}| grep -o '[0-9][0-9]*' | head -1) && "
                  f"[ -n \"$sid\" ] && for f in *.apk; do pm install-write -S $(stat -c %s \"$f\") $sid \"$f\" "
                  f"\"$PWD/$f\" >/dev/null || exit 3; done && pm install-commit $sid")
        rc, out, err = self.dev.sh(script, timeout=600)
        if rc != 0 and inst:   # installer attribution not allowed here: try without it
            rc, out, err = self.dev.sh(script.replace(inst, ""), timeout=600)
        self.dev.sh(f"rm -rf {q(tmp)}")
        if rc != 0 or "Success" not in out:
            self.errors.append(f"{pkg}: install failed: {(out + err).strip()[:200]}")
            return False
        return True

    def restore_app_data(self, pkg: str) -> bool:
        tar = self._p(f"data/{pkg}.tar")
        if not os.path.isfile(tar):
            return True
        rc, out, _ = self.dev.sh(f"am force-stop {q(pkg)}; stat -c %u /data/data/{q(pkg)}", root=True)
        uid = out.strip().splitlines()[-1] if out.strip() else ""
        if not uid.isdigit():
            self.errors.append(f"{pkg}: app is not installed – data skipped")
            return False
        rc, out = self.dev.stream_in("cd / && tar -xf -", True, tar, self.progress, self.cancel)
        if rc != 0:
            self.errors.append(f"{pkg}: data restore: {out[:200]}")
        u = int(uid)
        p = q(pkg)
        fix = (f"chown -R {u}:{u} /data/data/{p} /data/user_de/0/{p} 2>/dev/null; "
               f"for d in cache code_cache; do for b in /data/data /data/user_de/0; do "
               f"[ -d $b/{p}/$d ] && chown {u}:{u + 10000} $b/{p}/$d; done; done; "
               f"[ -d /data/media/0/Android/data/{p} ] && chown -R {u}:1078 /data/media/0/Android/data/{p}; "
               + relabel([f"/data/data/{p}", f"/data/user_de/0/{p}", f"/data/media/0/Android/data/{p}"]) + "; true")
        self.dev.sh(fix, root=True, timeout=600)
        return rc == 0

    def restore_storage(self) -> bool:
        rc, out = self.dev.stream_in("cd /sdcard/ && tar -xf -", False, self._p("storage.tar"),
                                     self.progress, self.cancel)
        # Android won't let anyone set the time stamp of the top storage folders: not an error
        real = [ln for ln in out.splitlines() if ln.strip() and "settime" not in ln and "had errors" not in ln]
        if rc != 0 and real:
            self.errors.append("Internal storage: " + " | ".join(real)[:300])
        self.dev.sh("am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file:///sdcard/ >/dev/null 2>&1; "
                    "content call --uri content://media/external/file --method scan_volume --arg external_primary "
                    ">/dev/null 2>&1; true")
        return not (rc != 0 and real)

    def restore_provider(self, key: str) -> bool:
        label, pkg = PROVIDERS[key]
        tar = self._p(f"system/{key}.tar")
        p = q(pkg)
        rc, out, _ = self.dev.sh(f"stat -c '%u:%g' /data/data/{p} /data/user_de/0/{p} 2>/dev/null", root=True)
        owners = out.split()
        if not owners:
            self.errors.append(f"{label}: provider not found on this device")
            return False
        # stop whoever has the databases open, clear them, unpack, fix owners, restart
        stop = "killall com.android.phone 2>/dev/null" if key == "telephony" else \
            f"am force-stop {p}; killall android.process.acore 2>/dev/null"
        self.dev.sh(f"{stop}; rm -rf /data/data/{p}/databases/* /data/user_de/0/{p}/databases/*", root=True)
        rc, out = self.dev.stream_in("cd / && tar -xf -", True, tar, self.progress, self.cancel)
        own = owners[0]
        self.dev.sh(f"chown -R {own} /data/data/{p} 2>/dev/null; chown -R {owners[-1]} /data/user_de/0/{p} 2>/dev/null; "
                    + relabel([f"/data/data/{p}", f"/data/user_de/0/{p}"]) + f"; {stop}; true", root=True)
        if rc != 0:
            self.errors.append(f"{label}: {out[:200]}")
        return rc == 0

    def restore_wifi(self) -> bool:
        rc, out = self.dev.stream_in("cd / && tar -xf -", True, self._p("system/wifi.tar"), self.progress, self.cancel)
        self.dev.sh("for d in " + " ".join(WIFI_DIRS) + "; do [ -d $d ] && chown 1000:1000 $d/WifiConfigStore*.xml "
                    "2>/dev/null && chmod 600 $d/WifiConfigStore*.xml && restorecon -RF $d; done; true", root=True)
        if rc != 0:
            self.errors.append(f"Wi-Fi: {out[:200]}")
        return rc == 0

    def restore_magisk(self) -> bool:
        mg = self.man.get("magisk", {})
        ok = True
        if os.path.isfile(self._p("magisk/modules.tar")):
            rc, out = self.dev.stream_in("cd / && tar -xf -", True, self._p("magisk/modules.tar"),
                                         self.progress, self.cancel)
            self.dev.sh("chown -R 0:0 /data/adb/modules; restorecon -RF /data/adb/modules 2>/dev/null; true", root=True)
            if rc != 0:
                ok = False
                self.errors.append(f"Magisk modules: {out[:200]}")
        for k, v in mg.get("settings", {}).items():
            if re.fullmatch(r"[a-z_]+", k) and str(v).isdigit() and k != "bootloop":
                self.dev.sh(f"magisk --sqlite \"REPLACE INTO settings (key, value) VALUES ('{k}', {v})\"", root=True)
        for ln in mg.get("denylist", []):
            pkg, _, proc = ln.partition("|")
            self.dev.sh(f"magisk --denylist add {q(pkg)} {q(proc)} >/dev/null 2>&1", root=True)
        return ok

    def run(self, items: dict, root: bool):
        man = self.man
        apps = man.get("apps", [])
        # Magisk app first: root comes back once it finishes its setup
        apps = sorted(apps, key=lambda a: a["pkg"] != "com.topjohnwu.magisk")
        if items.get("apps"):
            installed = self.installed()
            for i, a in enumerate(apps):
                if self.cancel():
                    raise Cancelled()
                self.say(f"Installing app {i + 1}/{len(apps)}: {a['pkg']}")
                self.install_app(a, installed)
            self.done.append("apps")
        if items.get("app_data") and root:
            for i, a in enumerate(apps):
                if self.cancel():
                    raise Cancelled()
                if a.get("data"):
                    self.say(f"Restoring app data {i + 1}/{len(apps)}: {a['pkg']}")
                    self.restore_app_data(a["pkg"])
            self.done.append("app_data")
        if items.get("storage") and os.path.isfile(self._p("storage.tar")):
            self.say("Restoring internal storage…")
            self.restore_storage()
            self.done.append("storage")
        if root:
            for key, (label, _pkg) in PROVIDERS.items():
                if items.get(key) and os.path.isfile(self._p(f"system/{key}.tar")):
                    self.say(f"Restoring {label}…")
                    self.restore_provider(key)
                    self.done.append(key)
            if items.get("magisk") and man.get("items", {}).get("magisk"):
                self.say("Restoring Magisk modules and settings…")
                self.restore_magisk()
                self.done.append("magisk")
            if items.get("wifi") and os.path.isfile(self._p("system/wifi.tar")):
                self.say("Restoring Wi-Fi networks…")
                self.restore_wifi()
                self.done.append("wifi")
        return self.done, self.errors


def factory_reset(dev: Device) -> str:
    """Ask Android to erase everything (needs root). Returns the command's output."""
    rc, out, err = dev.sh("am broadcast -a android.intent.action.FACTORY_RESET -p android --receiver-foreground "
                          "--es android.intent.extra.REASON AndroUnderTheHood "
                          "--ez android.intent.extra.WIPE_EXTERNAL_STORAGE true", root=True, timeout=60)
    return (out + err).strip()
