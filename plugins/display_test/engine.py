"""Display & touch tester engine: display info, the test app (installed from inside this plugin), test modes,
and the app's results streamed from logcat (tag AuthDisplayTest, one JSON object per line)."""
from __future__ import annotations

import base64
import json
import re
import subprocess
import threading

PKG = "dev.auth.displaytest"
ACT = f"{PKG}/.TestActivity"
APK = "app/DisplayTest.apk"
_NOWIN = 0x08000000


def run(conn, cmd, timeout=60, stdin=None, root=True):
    rc, out, err = conn.exec(cmd, timeout=timeout, stdin=stdin, as_root=root)
    return rc, (out + err).decode("utf-8", "replace")


def info(conn) -> dict:
    rc, text = run(conn, "wm size; wm density; settings get system screen_brightness; "
                         "settings get system screen_brightness_mode; dumpsys display 2>/dev/null | "
                         "grep -m1 'DisplayDeviceInfo{' ; dumpsys display 2>/dev/null | grep -m1 -o 'HdrCapabilities{[^}]*}'; "
                         f"pm path {PKG} >/dev/null 2>&1 && echo APP_INSTALLED")
    d = {"app": "APP_INSTALLED" in text}
    m = re.search(r"Physical size: (\d+)x(\d+)", text)
    if m:
        d["size"] = f"{m.group(1)} × {m.group(2)}"
    m = re.search(r"Override size: (\d+x\d+)", text)
    if m:
        d["override"] = m.group(1)
    m = re.search(r"Physical density: (\d+)", text)
    if m:
        d["density"] = int(m.group(1))
    lines = [ln.strip() for ln in text.splitlines()]
    for ln in lines:
        if ln.isdigit() and "brightness" not in d:
            d["brightness"] = int(ln)
        elif ln in ("0", "1") and "brightness" in d:
            d["auto"] = ln == "1"
    m = re.search(r'DisplayDeviceInfo\{"([^"]+)"', text)
    if m:
        d["name"] = m.group(1)
    fps = sorted({float(x) for x in re.findall(r"fps=([\d.]+)", text)})
    if fps:
        d["refresh"] = fps
    m = re.search(r"renderFrameRate ([\d.]+)", text)
    if m:
        d["current_fps"] = float(m.group(1))
    m = re.search(r"([\d.]+) x ([\d.]+) dpi", text)
    if m:
        d["dpi"] = f"{float(m.group(1)):.0f} × {float(m.group(2)):.0f}"
    m = re.search(r"mSupportedHdrTypes=\[([^\]]*)\], mMaxLuminance=([\d.]+)", text)
    if m:
        types = {"1": "Dolby Vision", "2": "HDR10", "3": "HLG", "4": "HDR10+"}
        d["hdr"] = ", ".join(types.get(t.strip(), t.strip()) for t in m.group(1).split(",") if t.strip()) or "none"
        d["max_nits"] = float(m.group(2))
    return d


def install_app(conn, read_file) -> str:
    apk = read_file(APK)
    tmp = "/data/local/tmp/.auth-displaytest.apk"
    rc, text = run(conn, f"base64 -d > {tmp} && chmod 644 {tmp} && pm install -r {tmp}; r=$?; rm -f {tmp}; exit $r",
                   stdin=base64.b64encode(apk), timeout=180)
    if "Success" not in text:
        raise RuntimeError("Installing the test app failed: " + text.strip()[-200:])
    return "installed"


def show(conn, mode: str, max_brightness: bool = False):
    if not re.fullmatch(r"color:#[0-9A-Fa-f]{6}|sweep|gradient|grid|checker|touch|fps", mode):
        raise ValueError(mode)
    rc, text = run(conn, f"am start --activity-single-top --activity-clear-top -n {ACT} --es mode {mode} --ez max_brightness {str(max_brightness).lower()} "
                         f"2>&1", timeout=30)
    if "Error" in text:
        raise RuntimeError(text.strip())


def stop(conn):
    run(conn, f"am force-stop {PKG}", timeout=30)


def uninstall(conn):
    run(conn, f"pm uninstall {PKG}", timeout=60)


class LogStream:
    """Follows the test app's logcat output; calls on_event(dict) for each JSON line."""

    def __init__(self, conn, on_event):
        self.on_event, self.stopped = on_event, False
        cmd = "logcat -T 1 -v raw -s AuthDisplayTest:I"
        if getattr(conn, "is_adb", False):
            self.proc = subprocess.Popen([conn.exe, "-s", conn.serial, "exec-out", cmd], stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=_NOWIN)
            self.readline = self.proc.stdout.readline
            self.chan = None
        else:
            self.chan = conn.open_channel(as_root=False)
            self.chan.exec_command(cmd)
            f = self.chan.makefile("rb")
            self.readline = f.readline
            self.proc = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stopped:
            ln = self.readline()
            if not ln:
                break
            ln = ln.decode("utf-8", "replace").strip()
            if ln.startswith("{"):
                try:
                    self.on_event(json.loads(ln))
                except ValueError:
                    pass

    def stop(self):
        self.stopped = True
        try:
            if self.proc:
                self.proc.kill()
            if self.chan:
                self.chan.close()
        except Exception:  # noqa: BLE001
            pass
