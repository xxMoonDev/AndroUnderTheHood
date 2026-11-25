"""Cell, Wi-Fi & Bluetooth plugin.

Reads (as the shell user; no root needed on most devices):
  * Cellular: modem (supported radio technologies, modems, radio HAL, baseband/RIL, modem DSP state), every SIM
    (carrier, network type, 5G state, registrations incl. Wi-Fi calling), serving + neighbour cells with band,
    frequency, PCI/TAC/CI (LTE eNB/sector), RSRP/RSRQ/SINR/RSSI, timing advance -> distance estimate
  * Wi-Fi: chip/driver/firmware, supported bands/standards/features, the current link, nearby access points with
    channel use, saved networks
  * Bluetooth: adapter, controller features, paired devices (connected, battery), nearby LE devices
  * Networks: every active network (transport, interface, IPs, DNS, gateway, validation, bandwidth) and ping
Live: a watcher refreshes every few seconds; samples and events (cell changes, network type, Wi-Fi AP, radios on/off)
are logged, graphed and exported to CSV.
Controls: Wi-Fi / Bluetooth / mobile data / airplane mode on-off, Wi-Fi connect / select saved / disconnect /
reconnect / forget, Bluetooth connect / disconnect.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

JAR_REMOTE = "/data/local/tmp/androunderthehood-nettest.jar"
_NOWIN = 0x08000000 if sys.platform == "win32" else 0

# LTE downlink: band -> (F_DL_low MHz, N_Offs-DL, first EARFCN, last EARFCN)   (3GPP TS 36.101)
LTE_BANDS = {1: (2110, 0, 0, 599), 2: (1930, 600, 600, 1199), 3: (1805, 1200, 1200, 1949), 4: (2110, 1950, 1950, 2399),
             5: (869, 2400, 2400, 2649), 7: (2620, 2750, 2750, 3449), 8: (925, 3450, 3450, 3799),
             11: (1475.9, 4750, 4750, 4949), 12: (729, 5010, 5010, 5179), 13: (746, 5180, 5180, 5279),
             14: (758, 5280, 5280, 5379), 17: (734, 5730, 5730, 5849), 18: (860, 5850, 5850, 5999),
             19: (875, 6000, 6000, 6149), 20: (791, 6150, 6150, 6449), 21: (1495.9, 6450, 6450, 6599),
             25: (1930, 8040, 8040, 8689), 26: (859, 8690, 8690, 9039), 28: (758, 9210, 9210, 9659),
             29: (717, 9660, 9660, 9769), 30: (2350, 9770, 9770, 9869), 32: (1452, 9920, 9920, 10359),
             34: (2010, 36200, 36200, 36349), 38: (2570, 37750, 37750, 38249), 39: (1880, 38250, 38250, 38649),
             40: (2300, 38650, 38650, 39649), 41: (2496, 39650, 39650, 41589), 42: (3400, 41590, 41590, 43589),
             43: (3600, 43590, 43590, 45589), 46: (5150, 46790, 46790, 54539), 48: (3550, 55240, 55240, 56739),
             66: (2110, 66436, 66436, 67335), 71: (617, 68586, 68586, 68935)}
# Android network types (TelephonyManager.NETWORK_TYPE_*), used to decode the radio access family bitmask
NET_TYPES = {1: "GPRS", 2: "EDGE", 3: "UMTS", 4: "CDMA", 5: "EVDO_0", 6: "EVDO_A", 7: "1xRTT", 8: "HSDPA", 9: "HSUPA",
             10: "HSPA", 11: "iDEN", 12: "EVDO_B", 13: "LTE", 14: "eHRPD", 15: "HSPA+", 16: "GSM", 17: "TD-SCDMA",
             18: "IWLAN", 19: "LTE-CA", 20: "NR (5G)"}
WIFI_STD = {1: "legacy (a/b/g)", 4: "Wi-Fi 4 (n)", 5: "Wi-Fi 5 (ac)", 6: "Wi-Fi 6 (ax)", 7: "WiGig (ad)",
            8: "Wi-Fi 7 (be)"}
WIFI_SEC = {0: "open", 1: "WEP", 2: "WPA2-PSK", 3: "EAP", 4: "WPA3-SAE", 5: "EAP Suite-B", 6: "OWE", 7: "WAPI-PSK",
            8: "WAPI-CERT", 9: "WPA3-EAP", 10: "OSEN", 11: "Passpoint", 12: "Passpoint R3", 13: "DPP"}
WIDTH = {0: 20, 1: 40, 2: 80, 3: 160, 4: 80, 5: 320}
DATA_STATE = {0: "disconnected", 1: "connecting", 2: "connected", 3: "suspended", 4: "disconnecting"}
SERVICE = {0: "in service", 1: "no service", 2: "emergency only", 3: "radio off"}
BT_COMPANY = {0x004C: "Apple", 0x0006: "Microsoft", 0x0075: "Samsung", 0x00E0: "Google", 0x0059: "Nordic Semi",
              0x0087: "Garmin", 0x009E: "Bose", 0x012D: "Sony", 0x0171: "Amazon", 0x038F: "Xiaomi", 0x027D: "Huawei",
              0x02E5: "Espressif", 0x000F: "Broadcom", 0x001D: "Qualcomm", 0x0002: "Intel"}
HW_SCRIPT = r"""
echo "//PROPS//"
getprop | grep -E '^\[(gsm\.version\.baseband|gsm\.version\.ril-impl|gsm\.network\.type|persist\.radio\.multisim\.config|ro\.telephony\.sim\.count|ro\.baseband|ro\.boot\.baseband|persist\.vendor\.qcom\.bluetooth\.soc|vendor\.wlan\.firmware\.version|vendor\.wlan\.driver\.version|wifi\.interface|ro\.boot\.wificountrycode|ro\.bluetooth\.a2dp_offload\.supported|persist\.bluetooth\.a2dp_offload\.cap|ro\.vendor\.bt\.chip|persist\.vendor\.bt\.soc|vendor\.bluetooth\.soc|ro\.hardware\.wlan|ro\.vendor\.wlan\.chip|ro\.telephony\.default_network|ril\.modem\.board|ro\.vendor\.mtk_md[^]]*|ro\.vendor\.md_[^]]*|vendor\.gsm\.version\.baseband|ro\.vendor\.connsys[^]]*|vendor\.connsys[^]]*)\]'
echo "//WLAN//"
for i in wlan0 wlan1 swlan0 p2p0; do [ -e /sys/class/net/$i ] || continue; d=$(readlink /sys/class/net/$i/device/driver)
  echo "$i|${d##*/}|$(grep -h -E '^(OF_COMPATIBLE_0|PCI_ID|SDIO_ID|MODALIAS)=' /sys/class/net/$i/device/uevent 2>/dev/null | tr '\n' ' ')"; done
echo "//PROC//"
for d in /sys/class/remoteproc/* /sys/bus/msm_subsys/devices/*; do [ -e "$d" ] || continue; echo "$(cat $d/name 2>/dev/null)|$(cat $d/state 2>/dev/null)"; done
echo "//HAL//"
service list 2>/dev/null | grep -i -E 'radio|wifi|bluetooth|wlan|supplicant' | sed 's/^[0-9]*[[:space:]]*//' | cut -d: -f1
echo "//DUMPWIFI//"
dumpsys wifi 2>/dev/null | grep -i -E 'firmware version|driver version' | head -3
"""


def register(api):
    api.add_tool("Cell/Wi-Fi/BT", lambda: NetDialog(api).show(), icon="wifi",
                 tooltip="Cellular, Wi-Fi and Bluetooth hardware, signal, logging and controls (plugin)",
                 needs="connection")


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------------------------- radio maths
def lte_band_freq(earfcn: int) -> tuple[int | None, float | None]:
    for b, (f, off, lo, hi) in LTE_BANDS.items():
        if lo <= earfcn <= hi:
            return b, round(f + 0.1 * (earfcn - off), 1)
    return None, None


def nr_freq(n: int) -> float:
    """NR-ARFCN -> MHz (3GPP TS 38.104 global raster)."""
    if n < 600000:
        return round(0.005 * n, 2)
    if n < 2016667:
        return round(3000 + 0.015 * (n - 600000), 2)
    return round(24250.08 + 0.06 * (n - 2016667), 2)


def gsm_freq(a: int) -> float | None:
    if 0 <= a <= 124:
        return round(935 + 0.2 * a, 1)
    if 975 <= a <= 1023:
        return round(935 + 0.2 * (a - 1024), 1)
    if 128 <= a <= 251:
        return round(869.2 + 0.2 * (a - 128), 1)
    if 512 <= a <= 885:
        return round(1805.2 + 0.2 * (a - 512), 1)      # DCS 1800 (PCS 1900 shares numbers; ambiguous)
    return None


def cell_band_freq(c: dict) -> tuple[str, str]:
    t, n = c.get("type"), c.get("arfcn")
    bands = c.get("bands") or []
    band, freq = (str(bands[0]) if bands else ""), ""
    if n is None:
        return band, freq
    if t == "LTE":
        b, f = lte_band_freq(n)
        band = band or (str(b) if b else "")
        freq = f"{f:.1f}" if f else ""
    elif t == "NR":
        freq = f"{nr_freq(n):.2f}"
        band = ("n" + band) if band else ""
    elif t in ("WCDMA", "TD-SCDMA"):
        freq = f"{n / 5:.1f}"
    elif t == "GSM":
        f = gsm_freq(n)
        freq = f"{f:.1f}" if f else ""
    return band, freq


def wifi_channel(freq: int) -> int:
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if freq == 2484:
        return 14
    if 5000 <= freq < 5900:
        return (freq - 5000) // 5
    if 5925 <= freq <= 7125:
        return (freq - 5950) // 5
    if 58000 <= freq <= 71000:
        return (freq - 56160) // 2160
    return 0


def wifi_band(freq: int) -> str:
    return "2.4 GHz" if freq < 3000 else "5 GHz" if freq < 5925 else "6 GHz" if freq < 7200 else "60 GHz"


def lte_ta_m(ta: int) -> float:
    return ta * 78.12     # one LTE timing-advance step = 16 Ts = 0.52 µs round trip ≈ 78 m


def col_rsrp(v):
    return "#46a758" if v >= -85 else "#8bc34a" if v >= -95 else "#f0c040" if v >= -105 else "#e08a3c" if v >= -115 else "#e5484d"


def col_sinr(v):
    return "#46a758" if v >= 20 else "#8bc34a" if v >= 13 else "#f0c040" if v >= 0 else "#e5484d"


def col_rssi(v):
    return "#46a758" if v >= -55 else "#8bc34a" if v >= -67 else "#f0c040" if v >= -75 else "#e5484d"


def bars(level: int) -> str:
    level = max(0, min(4, int(level or 0)))
    return "▂▄▆█"[:level] + "·" * (4 - level)


def serving(sim: dict) -> dict | None:
    cells = sim.get("cells") or []
    for c in cells:
        if c.get("status") == 1:
            return c
    for c in cells:
        if c.get("registered"):
            return c
    return None


# ---------------------------------------------------------------------------------------------- device side
class Device:
    def __init__(self, api):
        self.api = api
        self.conn = api.conn
        self.jar = api.read_file("nettest.jar")
        self.user_uid0 = None
        self.user_uid = None
        self.via_root = False

    @property
    def is_adb(self) -> bool:
        return getattr(self.conn, "transport", None) is None

    def root_ok(self) -> bool:
        try:
            return bool(self.api.is_root() or getattr(self.conn, "native_root", False))
        except Exception:  # noqa: BLE001
            return False

    def ensure_jar(self):
        want = hashlib.md5(self.jar).hexdigest()
        rc, out, _ = self.conn.exec("id -u", timeout=20, as_root=False)
        try:
            self.user_uid = int(out.decode(errors="replace").split()[0])
        except (ValueError, IndexError):
            self.user_uid = -1
        self.user_uid0 = self.user_uid == 0
        self.via_root = self.user_uid not in (0, 2000)
        if self.via_root and not self.root_ok():
            raise RuntimeError(f"The connection runs as an app (uid {self.user_uid}), which Android does not let read "
                               "the radios. Turn Root on, or start Shizuku on the device so the session runs as the "
                               "shell user, then try again.")
        rc, out, _ = self.conn.exec(f"md5sum {JAR_REMOTE} 2>/dev/null", timeout=20, as_root=self.via_root)
        if want in out.decode(errors="replace").split():
            return
        for root in (False, True):
            try:
                self.conn.exec(f"rm -f {JAR_REMOTE}", timeout=20, as_root=root)
            except Exception:  # noqa: BLE001
                pass
        if self.is_adb:
            os.makedirs(self.api.data_dir, exist_ok=True)
            tmp = os.path.join(self.api.data_dir, "nettest.jar")
            with open(tmp, "wb") as f:
                f.write(self.jar)
            r = subprocess.run([self.conn.exe, "-s", self.conn.serial, "push", tmp, JAR_REMOTE],
                               capture_output=True, timeout=60, creationflags=_NOWIN)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).decode(errors="replace").strip())
        else:
            cmd = f"cat > {JAR_REMOTE} && chmod 644 {JAR_REMOTE}"
            if self.via_root:
                cmd += f" && chown 2000:2000 {JAR_REMOTE}"
            rc, out, err = self.conn.exec(cmd, timeout=60, stdin=self.jar, as_root=self.via_root)
            if rc != 0:
                raise RuntimeError(err.decode(errors="replace").strip() or f"upload failed ({rc})")

    def as_shell(self, cmd: str) -> tuple[str, bool]:
        """A shell command run as the shell user (uid 2000) + whether it needs the root channel."""
        if self.user_uid0 or self.via_root:
            return f"su 2000 -c {shq(cmd)}", self.via_root
        return cmd, False

    def helper(self, args: str, system: bool = False) -> tuple[str, bool]:
        base = f"CLASSPATH={JAR_REMOTE} exec app_process / NetTest {args}"
        if system:
            return f"su 1000 -c {shq(base)}", True
        return self.as_shell(base)

    def run_helper(self, args: str, system: bool = False, timeout: float = 60) -> tuple[int, str, str]:
        cmd, as_root = self.helper(args, system)
        rc, out, err = self.conn.exec(cmd, timeout=timeout, as_root=as_root)
        return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def run_action(self, cmd: str, timeout: float = 60) -> str:
        """Run a control command as the shell user; if Android refuses it and root is available, retry as root."""
        c, as_root = self.as_shell(cmd)
        rc, out, err = self.conn.exec(c, timeout=timeout, as_root=as_root)
        text = (out + err).decode("utf-8", "replace").strip()
        refused = rc != 0 or re.search(r"Exception|denied|not allowed|Unknown command|Failure", text, re.I)
        if refused and self.root_ok():
            rc, out, err = self.conn.exec(cmd, timeout=timeout, as_root=True)
            text = (out + err).decode("utf-8", "replace").strip()
            refused = rc != 0 or re.search(r"Exception|denied|not allowed|Unknown command|Failure", text, re.I)
        if refused:
            raise RuntimeError(text[-800:] or f"exit code {rc}")
        return text

    def open_stream(self, args: str):
        cmd, as_root = self.helper(args)
        if self.is_adb:
            p = subprocess.Popen([self.conn.exe, "-s", self.conn.serial, "exec-out", cmd], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, creationflags=_NOWIN)
            err = bytearray()
            threading.Thread(target=lambda: [err.extend(b) for b in iter(lambda: p.stderr.read(4096), b"")],
                             daemon=True).start()
            return (lambda n: p.stdout.read1(n)), p.kill, (lambda: err.decode(errors="replace"))
        if getattr(self.conn, "use_pty", False):
            raise RuntimeError("This SSH server only allows interactive shells, which can't carry a live stream.")
        chan = self.conn.open_channel(as_root)
        chan.exec_command(self.conn.wrap_root(cmd) if as_root else cmd)
        chan.shutdown_write()
        err = bytearray()

        def read(n):
            while chan.recv_stderr_ready():
                err.extend(chan.recv_stderr(65536))
            return chan.recv(n)

        def stderr():
            try:
                while chan.recv_stderr_ready():
                    err.extend(chan.recv_stderr(65536))
            except Exception:  # noqa: BLE001
                pass
            return err.decode(errors="replace")
        return read, chan.close, stderr

    def kill_leftovers(self, token: str):
        pat = shq("NetTest watch .* " + token + "$")
        for root in (False, True):
            try:
                rc, out, _ = self.conn.exec(f"pkill -f {pat} 2>/dev/null; sleep 0.3; pgrep -f {pat} >/dev/null && echo alive",
                                            timeout=15, as_root=root)
                if b"alive" not in out:
                    return
            except Exception:  # noqa: BLE001
                pass


def parse_hw(text: str) -> dict:
    sec: dict[str, list[str]] = {}
    cur = None
    for ln in text.splitlines():
        m = re.match(r"//(\w+)//", ln)
        if m:
            cur = m.group(1)
            sec[cur] = []
        elif cur and ln.strip():
            sec[cur].append(ln.strip())
    props = {}
    for ln in sec.get("PROPS", []):
        m = re.match(r"\[(.*?)\]: \[(.*)\]", ln)
        if m and m.group(2):
            props[m.group(1)] = m.group(2)
    wlan = []
    for ln in sec.get("WLAN", []):
        i, drv, ids = (ln.split("|") + ["", "", ""])[:3]
        wlan.append({"iface": i, "driver": drv, "ids": ids.strip()})
    procs = []
    for ln in sec.get("PROC", []):
        n, st = (ln.split("|") + ["", ""])[:2]
        if n:
            procs.append((n, st))
    hal = sorted(set(x for x in sec.get("HAL", []) if x))
    return {"props": props, "wlan": wlan, "procs": procs, "hal": hal, "dumpwifi": sec.get("DUMPWIFI", [])}


def detect(dev: Device) -> dict:
    dev.ensure_jar()
    rc, out, err = dev.run_helper("list", timeout=90)
    start = out.find("{")
    if rc != 0 or start < 0:
        raise RuntimeError((err.strip() or out.strip() or f"exit code {rc}")[-1500:])
    data = json.loads(out[start:])
    hw = {}
    for root in ([False, True] if dev.root_ok() else [False]):
        try:
            rc, o, _ = dev.conn.exec(HW_SCRIPT, timeout=60, as_root=root or dev.via_root)
            h = parse_hw(o.decode("utf-8", "replace"))
            if not hw or len(h["procs"]) > len(hw.get("procs", [])):
                hw = h
            if hw.get("procs"):
                break
        except Exception:  # noqa: BLE001
            pass
    data["hw"] = hw
    return data


# ---------------------------------------------------------------------------------------------- live watcher
class Watcher(QObject):
    snapshot = Signal(dict)
    failed = Signal(str)
    ended = Signal()

    def __init__(self, dev: Device, ms: int, token: str, prev_token: str = ""):
        super().__init__()
        self.dev, self.ms, self.token, self.prev_token = dev, ms, token, prev_token
        self._stop = threading.Event()
        self._stopfn = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._stopfn:
            try:
                self._stopfn()
            except Exception:  # noqa: BLE001
                pass

    def _run(self):
        try:
            if self.prev_token:
                self.dev.kill_leftovers(self.prev_token)
            read, self._stopfn, stderr = self.dev.open_stream(f"watch {self.ms} {self.token}")
            if self._stop.is_set():
                self._stopfn()
                return
            buf = b""
            while not self._stop.is_set():
                chunk = read(65536)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    if raw.startswith(b"D "):
                        self.snapshot.emit(json.loads(raw[2:]))
                    elif raw.startswith(b"E "):
                        raise RuntimeError(raw[2:].decode(errors="replace"))
            if not self._stop.is_set():
                raise RuntimeError(stderr().strip()[-800:] or "The live watcher stopped.")
        except Exception as e:  # noqa: BLE001
            if not self._stop.is_set():
                self.failed.emit(str(e) or repr(e))
        finally:
            if self._stopfn:
                try:
                    self._stopfn()
                except Exception:  # noqa: BLE001
                    pass
            self.ended.emit()


# ---------------------------------------------------------------------------------------------- widgets
class LineGraph(QWidget):
    """Time series: [(name, colour, unit, [(t, v), ...])]; each unit gets its own scale (left / right axis)."""

    def __init__(self, theme):
        super().__init__()
        self.theme = theme
        self.series: list = []
        self.span = 600.0
        self.setMinimumHeight(200)

    def sizeHint(self):
        return QSize(700, 260)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#101418"))
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        x0, x1, top, bot = 50, W - 50, 22, H - 22
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)
        pts_all = [pt for s in self.series for pt in s[3]]
        if not pts_all:
            p.setPen(QColor(self.theme.TEXT_DIM))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No samples yet")
            return
        t_end = max(t for t, _v in pts_all)
        t_start = t_end - self.span
        units = []
        for s in self.series:
            if s[2] not in units:
                units.append(s[2])
        scales = {}
        for u in units:
            vals = [v for s in self.series if s[2] == u for t, v in s[3] if t >= t_start]
            if not vals:
                continue
            lo, hi = min(vals), max(vals)
            pad = max(2.0, (hi - lo) * 0.1)
            scales[u] = (math.floor((lo - pad) / 5) * 5, math.ceil((hi + pad) / 5) * 5)
        p.setPen(QColor("#2a333c"))
        for i in range(5):
            y = top + (bot - top) * i / 4
            p.drawLine(QPointF(x0, y), QPointF(x1, y))
        for idx, u in enumerate(units[:2]):
            if u not in scales:
                continue
            lo, hi = scales[u]
            p.setPen(QColor("#7d8790"))
            for i in range(5):
                v = hi - (hi - lo) * i / 4
                y = top + (bot - top) * i / 4
                r = QRectF(0, y - 7, x0 - 4, 14) if idx == 0 else QRectF(x1 + 4, y - 7, 46, 14)
                p.drawText(r, (Qt.AlignmentFlag.AlignRight if idx == 0 else Qt.AlignmentFlag.AlignLeft), f"{v:g}")
            p.drawText(QRectF(0 if idx == 0 else x1, 2, 50, 14), Qt.AlignmentFlag.AlignCenter, u)
        p.setPen(QColor("#7d8790"))
        for frac in (0, 0.5, 1):
            t = t_start + self.span * frac
            x = x0 + (x1 - x0) * frac
            p.drawText(QRectF(x - 40, bot + 3, 80, 14), Qt.AlignmentFlag.AlignCenter,
                       time.strftime("%H:%M:%S", time.localtime(t)))
        lx = x0 + 6
        for name, colour, u, pts in self.series:
            if u not in scales:
                continue
            lo, hi = scales[u]
            path = None
            for t, v in pts:
                if t < t_start:
                    continue
                x = x0 + (t - t_start) / self.span * (x1 - x0)
                y = bot - (v - lo) / (hi - lo) * (bot - top) if hi > lo else (top + bot) / 2
                if path is None:
                    path = QPainterPath(QPointF(x, y))
                else:
                    path.lineTo(QPointF(x, y))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(colour))
                p.drawEllipse(QPointF(x, y), 2, 2)
            if path is not None:
                p.setPen(QPen(QColor(colour), 1.6))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
            p.setPen(QColor(colour))
            p.drawText(QPointF(lx, top + 12), f"● {name}")
            lx += 12 + len(name) * 7


class ChannelGraph(QWidget):
    """Access points drawn as channel-width humps by signal strength (like a spectrum view)."""

    def __init__(self, theme):
        super().__init__()
        self.theme = theme
        self.aps: list = []
        self.band = "2.4 GHz"
        self.connected_bssid = ""
        self.setMinimumHeight(220)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#101418"))
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        x0, x1, top, bot = 40, W - 10, 10, H - 24
        aps = [a for a in self.aps if wifi_band(a.get("freq", 0)) == self.band]
        if self.band == "2.4 GHz":
            fmin, fmax, ticks = 2400, 2490, [(2412 + 5 * (c - 1), str(c)) for c in range(1, 14)]
        elif self.band == "5 GHz":
            fmin, fmax = 5160, 5900
            ticks = [(5000 + 5 * c, str(c)) for c in (36, 52, 64, 100, 116, 132, 149, 165)]
        else:
            fmin, fmax = 5940, 7130
            ticks = [(5950 + 5 * c, str(c)) for c in (1, 33, 65, 97, 129, 161, 193, 225)]
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)

        def X(fr):
            return x0 + (fr - fmin) / (fmax - fmin) * (x1 - x0)

        def Y(dbm):
            return bot - (max(-100, min(-20, dbm)) + 100) / 80 * (bot - top)

        p.setPen(QColor("#2a333c"))
        for d in (-30, -50, -70, -90):
            p.drawLine(QPointF(x0, Y(d)), QPointF(x1, Y(d)))
            p.setPen(QColor("#7d8790"))
            p.drawText(QRectF(0, Y(d) - 7, x0 - 4, 14), Qt.AlignmentFlag.AlignRight, str(d))
            p.setPen(QColor("#2a333c"))
        p.setPen(QColor("#7d8790"))
        for fr, lbl in ticks:
            p.drawText(QRectF(X(fr) - 15, bot + 4, 30, 14), Qt.AlignmentFlag.AlignCenter, lbl)
        for i, a in enumerate(sorted(aps, key=lambda a: a.get("level", -100))):
            width = WIDTH.get(a.get("width", 0), 20)
            centre = a.get("center0") or a.get("freq")
            if not centre:
                continue
            lo, hi = centre - width / 2, centre + width / 2
            col = QColor.fromHsvF((hash(a.get("bssid", "")) % 360) / 360.0, 0.65, 0.95)
            path = QPainterPath(QPointF(X(lo), bot))
            path.cubicTo(QPointF(X(lo + width * 0.15), Y(a["level"])), QPointF(X(hi - width * 0.15), Y(a["level"])),
                         QPointF(X(hi), bot))
            fill = QColor(col)
            fill.setAlpha(45)
            p.fillPath(path, fill)
            mine = a.get("bssid") == self.connected_bssid
            p.setPen(QPen(col, 3 if mine else 1.5))
            p.drawPath(path)
            p.setPen(col)
            name = a.get("ssid") or "(hidden)"
            p.drawText(QPointF(X(centre) - len(name) * 3, Y(a["level"]) - 4), name + (" ★" if mine else ""))
        if not aps:
            p.setPen(QColor(self.theme.TEXT_DIM))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, f"No {self.band} networks in the last scan")


def table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.setSortingEnabled(True)
    t.sortByColumn(-1, Qt.SortOrder.AscendingOrder)       # keep our order until the user clicks a header
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def fill_table(t: QTableWidget, rows: list[list], colours: dict | None = None):
    """rows: list of cell values (numbers sort numerically). colours: {(row, col): '#hex'}."""
    t.setSortingEnabled(False)
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            it = QTableWidgetItem()
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                it.setData(Qt.ItemDataRole.DisplayRole, v)
            else:
                it.setText("" if v is None else str(v))
            if colours and (r, c) in colours:
                it.setForeground(QColor(colours[(r, c)]))
            t.setItem(r, c, it)
    t.setSortingEnabled(True)


# ---------------------------------------------------------------------------------------------- window
class NetDialog(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"Cell, Wi-Fi & Bluetooth – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1300, 820)
        self.dev = Device(api)
        self.data: dict = {}
        self.watcher: Watcher | None = None
        self.token = ""
        self.last: dict | None = None
        self.samples: list[dict] = []
        self.events: list[tuple] = []
        self.scan_aps: list = []
        self.log_file = None
        self.log_writer = None

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.b_scan = QPushButton(api.icon("refresh"), " Read hardware")
        self.b_scan.clicked.connect(self.scan)
        top.addWidget(self.b_scan)
        top.addWidget(QLabel("Live refresh every"))
        self.interval = QSpinBox()
        self.interval.setRange(1, 300)
        self.interval.setSuffix(" s")
        self.interval.setValue(int(api.get_setting("interval", 3)))
        self.interval.editingFinished.connect(self.restart_watch)
        top.addWidget(self.interval)
        self.live_lbl = QLabel("")
        self.live_lbl.setStyleSheet(f"color: {api.theme.TEXT_DIM};")
        top.addWidget(self.live_lbl)
        top.addStretch(1)
        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color: {api.theme.TEXT_DIM};")
        top.addWidget(self.summary)
        lay.addLayout(top)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.build_cell()
        self.build_wifi()
        self.build_bt()
        self.build_net()
        self.build_log()
        self.scan()

    # ================================================================== tabs
    def build_cell(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        for label, fn in (("Mobile data on", lambda: self.action("svc data enable", "Turn mobile data on")),
                          ("Mobile data off", lambda: self.action("svc data disable", "Turn mobile data off", risky=True)),
                          ("Airplane mode on", lambda: self.action(
                              "cmd connectivity airplane-mode enable", "Turn airplane mode on", risky=True)),
                          ("Airplane mode off", lambda: self.action(
                              "cmd connectivity airplane-mode disable", "Turn airplane mode off"))):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.modem_tree = QTreeWidget()
        self.modem_tree.setHeaderLabels(["Modem", "Value"])
        self.modem_tree.setColumnWidth(0, 170)
        split.addWidget(self.modem_tree)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.sim_lbl = QLabel("Reading…")
        self.sim_lbl.setWordWrap(True)
        self.sim_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.sim_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        rl.addWidget(self.sim_lbl)
        rl.addWidget(QLabel("Cells (serving first, then neighbours)"))
        self.cells = table(["SIM", "Status", "Tech", "Band", "ARFCN", "MHz", "PCI/PSC", "TAC/LAC", "Cell ID",
                            "eNB-sector", "RSRP/RSCP", "RSRQ/EcNo", "SINR/SNR", "RSSI", "TA (≈ m)", "Level", "PLMN"])
        rl.addWidget(self.cells, 1)
        split.addWidget(right)
        split.setSizes([330, 950])
        lay.addWidget(split, 1)
        self.tabs.addTab(w, "Cellular")

    def build_wifi(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.wifi_lbl = QLabel("")
        self.wifi_lbl.setWordWrap(True)
        self.wifi_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.wifi_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.wifi_lbl)
        row = QHBoxLayout()
        for label, fn in (("Wi-Fi on", lambda: self.action("cmd wifi set-wifi-enabled enabled || svc wifi enable", "Turn Wi-Fi on")),
                          ("Wi-Fi off", lambda: self.action("cmd wifi set-wifi-enabled disabled || svc wifi disable",
                                                            "Turn Wi-Fi off", risky=True)),
                          ("Scan", self.wifi_scan), ("Connect to…", self.wifi_connect_dialog),
                          ("Disconnect", lambda: self.helper_action("wifi-disconnect", "Disconnect Wi-Fi", risky=True)),
                          ("Reconnect", lambda: self.helper_action("wifi-reconnect", "Reconnect Wi-Fi"))):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        sub = QTabWidget()
        self.aps = table(["SSID", "BSSID", "Band", "Channel", "MHz", "Width", "RSSI dBm", "Standard", "Security",
                          "Seen s ago"])
        sub.addTab(self.aps, "Nearby networks")
        cw = QWidget()
        cl = QVBoxLayout(cw)
        cl.setContentsMargins(0, 0, 0, 0)
        brow = QHBoxLayout()
        self.chan_band = QComboBox()
        self.chan_band.addItems(["2.4 GHz", "5 GHz", "6 GHz"])
        brow.addWidget(QLabel("Band"))
        brow.addWidget(self.chan_band)
        self.chan_info = QLabel("")
        brow.addWidget(self.chan_info, 1)
        cl.addLayout(brow)
        self.chan = ChannelGraph(self.api.theme)
        self.chan_band.currentTextChanged.connect(lambda b: (setattr(self.chan, "band", b), self.chan.update(),
                                                             self.update_chan_info()))
        cl.addWidget(self.chan, 1)
        sub.addTab(cw, "Channels")
        sw = QWidget()
        sl = QVBoxLayout(sw)
        sl.setContentsMargins(0, 0, 0, 0)
        self.saved = table(["ID", "SSID", "Security", "Hidden"])
        sl.addWidget(self.saved, 1)
        srow = QHBoxLayout()
        b = QPushButton("Connect to selected")
        b.clicked.connect(self.wifi_select_saved)
        srow.addWidget(b)
        b = QPushButton("Forget selected")
        b.clicked.connect(self.wifi_forget)
        srow.addWidget(b)
        srow.addStretch(1)
        sl.addLayout(srow)
        sub.addTab(sw, "Saved networks")
        self.wifi_tree = QTreeWidget()
        self.wifi_tree.setHeaderLabels(["Wi-Fi hardware", "Value"])
        self.wifi_tree.setColumnWidth(0, 280)
        sub.addTab(self.wifi_tree, "Hardware & features")
        lay.addWidget(sub, 1)
        self.tabs.addTab(w, "Wi-Fi")

    def build_bt(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.bt_lbl = QLabel("")
        self.bt_lbl.setWordWrap(True)
        self.bt_lbl.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.bt_lbl)
        row = QHBoxLayout()
        for label, fn in (("Bluetooth on", lambda: self.action("cmd bluetooth_manager enable || svc bluetooth enable",
                                                               "Turn Bluetooth on")),
                          ("Bluetooth off", lambda: self.action("cmd bluetooth_manager disable || svc bluetooth disable",
                                                                "Turn Bluetooth off")),
                          ("Connect selected", lambda: self.bt_connect(True)),
                          ("Disconnect selected", lambda: self.bt_connect(False))):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addWidget(QLabel("  Nearby scan"))
        self.bt_secs = QSpinBox()
        self.bt_secs.setRange(3, 60)
        self.bt_secs.setValue(10)
        self.bt_secs.setSuffix(" s")
        row.addWidget(self.bt_secs)
        b = QPushButton("Scan")
        b.clicked.connect(self.bt_scan)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        sub = QTabWidget()
        self.paired = table(["Name", "Address", "Type", "Class", "Connected", "Battery %"])
        sub.addTab(self.paired, "Paired devices")
        self.nearby = table(["Name", "Address", "RSSI dBm", "Adverts", "Connectable", "TX power", "Manufacturer ID"])
        sub.addTab(self.nearby, "Nearby (LE scan)")
        self.bt_tree = QTreeWidget()
        self.bt_tree.setHeaderLabels(["Bluetooth hardware", "Value"])
        self.bt_tree.setColumnWidth(0, 300)
        sub.addTab(self.bt_tree, "Hardware & features")
        lay.addWidget(sub, 1)
        self.tabs.addTab(w, "Bluetooth")

    def build_net(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.nets = table(["Default", "Transports", "Interface", "Addresses", "DNS", "Gateway", "Validated",
                           "Metered", "Down Mbps", "Up Mbps", "Private DNS", "MTU"])
        lay.addWidget(self.nets, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Ping from the device:"))
        self.ping_host = QLineEdit(self.api.get_setting("ping_host", "8.8.8.8"))
        self.ping_host.setMaximumWidth(220)
        row.addWidget(self.ping_host)
        self.ping_n = QSpinBox()
        self.ping_n.setRange(1, 100)
        self.ping_n.setValue(5)
        self.ping_n.setSuffix(" pings")
        row.addWidget(self.ping_n)
        b = QPushButton("Ping")
        b.clicked.connect(self.ping)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.ping_out = QPlainTextEdit()
        self.ping_out.setReadOnly(True)
        self.ping_out.setMaximumHeight(200)
        lay.addWidget(self.ping_out)
        self.tabs.addTab(w, "Networks")

    def build_log(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        row.addWidget(QLabel("Graph"))
        self.span = QComboBox()
        for label, s in (("last 5 min", 300), ("last 15 min", 900), ("last hour", 3600), ("last 6 h", 21600)):
            self.span.addItem(label, s)
        self.span.currentIndexChanged.connect(lambda _i: (setattr(self.graph, "span", float(self.span.currentData())),
                                                          self.graph.update()))
        row.addWidget(self.span)
        self.logfile_cb = QCheckBox("Also write every sample to a CSV file")
        self.logfile_cb.toggled.connect(self.on_logfile)
        row.addWidget(self.logfile_cb)
        b = QPushButton(self.api.icon("download"), " Export samples CSV")
        b.clicked.connect(self.export_csv)
        row.addWidget(b)
        b = QPushButton("Clear")
        b.clicked.connect(self.clear_log)
        row.addWidget(b)
        row.addStretch(1)
        self.log_lbl = QLabel("")
        row.addWidget(self.log_lbl)
        lay.addLayout(row)
        split = QSplitter(Qt.Orientation.Vertical)
        self.graph = LineGraph(self.api.theme)
        self.graph.span = 300.0
        split.addWidget(self.graph)
        self.ev_table = table(["Time", "Event"])
        split.addWidget(self.ev_table)
        split.setSizes([380, 260])
        lay.addWidget(split, 1)
        self.tabs.addTab(w, "Log & graph")

    # ================================================================== detection
    def scan(self):
        self.b_scan.setEnabled(False)
        self.summary.setText("Reading hardware…")

        def done(data):
            try:
                self.b_scan.setEnabled(True)
                self.fill(data)
                if not self.watcher:
                    self.restart_watch()
            except RuntimeError:
                pass

        def fail(msg):
            try:
                self.b_scan.setEnabled(True)
                self.summary.setText("Failed")
                QMessageBox.warning(self, "Cell, Wi-Fi & Bluetooth", f"Could not read the hardware:\n\n{msg}")
            except RuntimeError:
                pass

        self.api.task(lambda: detect(self.dev), done, fail)

    def fill(self, data: dict):
        self.data = data
        hw = data.get("hw", {})
        props = hw.get("props", {})
        self.summary.setText(f"{data.get('model', '')} · Android API {data.get('sdk')} · "
                             f"{'as system user' if data.get('uid') == 1000 else 'as shell user'}")
        # ---- modem tree
        t = self.modem_tree
        t.clear()

        def add(parent, k, v, tip=""):
            it = QTreeWidgetItem(parent, [k, "" if v is None else str(v)])
            if tip:
                it.setToolTip(1, tip)
            return it

        def group(tree, title, expanded=True):
            g = QTreeWidgetItem(tree, [title])
            f = g.font(0)
            f.setBold(True)
            g.setFont(0, f)
            g.setExpanded(expanded)
            return g

        m = data.get("modem", {})
        g = group(t, "Modem")
        for k, label in (("gsm.version.baseband", "Baseband"), ("vendor.gsm.version.baseband", "Baseband (vendor)"),
                         ("ro.baseband", "Baseband type"), ("gsm.version.ril-impl", "RIL"),
                         ("ril.modem.board", "Modem board"), ("persist.radio.multisim.config", "Multi-SIM config")):
            if props.get(k):
                add(g, label, props[k])
        if m.get("radio_hal"):
            hv = re.findall(r"\d+", m["radio_hal"])
            add(g, "Radio HAL", ".".join(hv) if hv else m["radio_hal"])
        fam = m.get("radio_family") or 0
        if fam:
            rats = [NET_TYPES[i] for i in sorted(NET_TYPES) if fam & (1 << (i - 1))]
            gens = []
            if any(r in rats for r in ("GSM", "GPRS", "EDGE")):
                gens.append("2G")
            if any(r in rats for r in ("UMTS", "HSPA", "HSPA+", "TD-SCDMA", "CDMA", "EVDO_A")):
                gens.append("3G")
            if "LTE" in rats:
                gens.append("4G")
            if "NR (5G)" in rats:
                gens.append("5G")
            add(g, "Supported generations", " / ".join(gens))
            add(g, "Supported technologies", ", ".join(rats))
        for k, label in (("sim_slots", "SIM slots in use"), ("active_modems", "Active modems"),
                         ("supported_modems", "Supported modems (dual SIM capable if 2)"),
                         ("concurrent_voice_data", "Voice + data at once"), ("voice_capable", "Voice capable"),
                         ("sms_capable", "SMS capable"), ("world_phone", "World phone")):
            if k in m:
                add(g, label, m[k])
        if hw.get("procs"):
            pg = group(t, "Co-processors (remoteproc)")
            names = {"mss": "modem", "modem": "modem", "adsp": "audio DSP", "cdsp": "compute DSP",
                     "slpi": "sensors DSP", "wpss": "Wi-Fi DSP", "spss": "secure processor", "md": "modem"}
            for n, st in hw["procs"]:
                tag = next((v for k, v in names.items() if re.search(rf"(^|[-_.]){k}", n)), "")
                add(pg, n + (f" ({tag})" if tag else ""), st)
        rh = [h for h in hw.get("hal", []) if re.search(r"radio|ims|telephony", h, re.I)]
        if rh:
            hg = group(t, f"Radio services ({len(rh)})", expanded=False)
            for h in rh:
                add(hg, h, "")
        # ---- Wi-Fi tree
        wt = self.wifi_tree
        wt.clear()
        wifi = data.get("wifi", {})
        g = group(wt, "Chip / driver")
        for wl in hw.get("wlan", []):
            add(g, f"Interface {wl['iface']}", f"driver {wl['driver']}", wl.get("ids", ""))
        for k, label in (("vendor.wlan.driver.version", "Driver version"), ("vendor.wlan.firmware.version", "Firmware"),
                         ("ro.hardware.wlan", "WLAN hardware"), ("ro.vendor.wlan.chip", "WLAN chip"),
                         ("ro.boot.wificountrycode", "Boot country code")):
            if props.get(k):
                add(g, label, props[k])
        for ln in hw.get("dumpwifi", []):
            a, _, b = ln.partition(":")
            if b.strip():
                add(g, a.strip(), b.strip())
        if wifi.get("country"):
            add(g, "Regulatory country", wifi["country"])
        feats = wifi.get("features", {})
        if feats:
            bands = [b for b, k in (("2.4 GHz", "is24GHzBandSupported"), ("5 GHz", "is5GHzBandSupported"),
                                    ("6 GHz", "is6GHzBandSupported"), ("60 GHz", "is60GHzBandSupported")) if feats.get(k)]
            add(g, "Bands", ", ".join(bands) or "—")
            stds = [WIFI_STD[s] for s in (4, 5, 6, 8, 7) if feats.get(f"standard_{s}")]
            add(g, "Standards", ", ".join(stds) or "—")
            fg = group(wt, f"Features ({sum(1 for v in feats.values() if v is True)} on)", expanded=False)
            for k in sorted(feats):
                if k.startswith("standard_"):
                    continue
                it = add(fg, re.sub(r"^is", "", k).replace("Supported", " supported").replace("Enabled", " enabled"),
                         "yes" if feats[k] else "no")
                if not feats[k]:
                    for c in range(2):
                        it.setForeground(c, QColor(self.api.theme.TEXT_FAINT))
        whal = [h for h in hw.get("hal", []) if re.search(r"wifi|wlan|supplicant", h, re.I)]
        if whal:
            hg = group(wt, f"Wi-Fi services ({len(whal)})", expanded=False)
            for h in whal:
                add(hg, h, "")
        fill_table(self.saved, [[s.get("id"), (s.get("ssid") or "").strip('"'), s.get("security", ""),
                                 "yes" if s.get("hidden") else ""] for s in wifi.get("saved", [])])
        # ---- Bluetooth
        self.fill_bt(data.get("bluetooth", {}), props, hw)
        # ---- networks
        self.fill_nets(data.get("networks", []))
        # first live view from the list data
        self.on_snapshot({"t": int(time.time() * 1000), "sims": data.get("sims", []),
                          "wifi": wifi.get("link", {}), "bt_on": data.get("bluetooth", {}).get("enabled")},
                         record=False)

    def fill_bt(self, b: dict, props: dict, hw: dict):
        bt = self.bt_tree
        bt.clear()
        if b.get("absent"):
            self.bt_lbl.setText("No Bluetooth adapter reported.")
            return
        on = b.get("enabled")
        self.bt_lbl.setText(f"<b>{b.get('name', '')}</b> · {b.get('address', '')} · Bluetooth "
                            f"<b>{'ON' if on else 'OFF'}</b>"
                            + ("" if on else " – controller features are only reported while Bluetooth is on"))
        g = QTreeWidgetItem(bt, ["Controller"])
        g.setExpanded(True)
        for k, label in (("persist.vendor.qcom.bluetooth.soc", "Qualcomm SoC"), ("vendor.bluetooth.soc", "SoC"),
                         ("ro.vendor.bt.chip", "Chip"), ("persist.vendor.bt.soc", "SoC"),
                         ("ro.bluetooth.a2dp_offload.supported", "A2DP offload"),
                         ("persist.bluetooth.a2dp_offload.cap", "Offloaded codecs")):
            if props.get(k):
                QTreeWidgetItem(g, [label, props[k]])
        if b.get("max_adv_bytes"):
            QTreeWidgetItem(g, ["Max LE advertising data", f"{b['max_adv_bytes']} bytes"])
        if b.get("profiles"):
            QTreeWidgetItem(g, ["Supported profile ids", b["profiles"]])
        f = QTreeWidgetItem(bt, ["Features"])
        f.setExpanded(True)
        for k, v in sorted(b.get("features", {}).items()):
            name = re.sub(r"^is", "", k).replace("Supported", " supported")
            it = QTreeWidgetItem(f, [name, ("yes" if v else "no") if on else "(Bluetooth off)"])
            if not v:
                for c in range(2):
                    it.setForeground(c, QColor(self.api.theme.TEXT_FAINT))
        bhal = [h for h in hw.get("hal", []) if re.search(r"bluetooth", h, re.I)]
        if bhal:
            hg = QTreeWidgetItem(bt, [f"Bluetooth services ({len(bhal)})"])
            for h in bhal:
                QTreeWidgetItem(hg, [h, ""])
        fill_table(self.paired, [[d.get("alias") or d.get("name") or "", d.get("address", ""), d.get("type", ""),
                                  d.get("class", ""), "yes" if d.get("connected") else "", d.get("battery", "")]
                                 for d in b.get("bonded", [])])

    def fill_nets(self, nets: list):
        rows = []
        for n in nets:
            rows.append(["★" if n.get("default") else "", ", ".join(n.get("transports", [])), n.get("iface", ""),
                         ", ".join(n.get("addresses", [])), ", ".join(n.get("dns", [])), n.get("gateway", ""),
                         "yes" if n.get("validated") else "no", "yes" if n.get("metered") else "no",
                         round(n.get("down_kbps", 0) / 1000, 1), round(n.get("up_kbps", 0) / 1000, 1),
                         n.get("private_dns_server") or ("on" if n.get("private_dns") else "off"), n.get("mtu", "")])
        fill_table(self.nets, rows)

    # ================================================================== live
    def restart_watch(self):
        self.api.set_setting("interval", self.interval.value())
        prev = self.token if self.watcher else ""
        if self.watcher:
            self.watcher.stop()
        self.token = f"t{os.getpid()}x{int(time.time() * 1000) % 10**9}"
        w = Watcher(self.dev, self.interval.value() * 1000, self.token, prev)
        w.snapshot.connect(self.on_snapshot)
        w.failed.connect(lambda m, w=w: self.on_watch_failed(w, m))
        w.ended.connect(lambda w=w: self.on_watch_ended(w))
        self.watcher = w
        self.live_lbl.setText("● live")
        w.start()

    def on_watch_failed(self, w, msg):
        if self.watcher is w:
            self.live_lbl.setText(f"live update stopped: {msg[:120]}")

    def on_watch_ended(self, w):
        if self.watcher is w:
            self.watcher = None
            try:
                if not self.live_lbl.text().startswith("live update stopped"):
                    self.live_lbl.setText("live update stopped")
            except RuntimeError:
                pass

    def on_snapshot(self, s: dict, record: bool = True):
        try:
            self.show_cell(s.get("sims", []))
            self.show_wifi(s.get("wifi", {}), s.get("default_transport"))
        except RuntimeError:
            return
        if record:
            self.record(s)
        self.last = s

    def show_cell(self, sims: list):
        parts = []
        rows, colours = [], {}
        for sim in sims:
            sc = serving(sim)
            title = (f"<b>SIM {sim.get('slot', 0) + 1}</b>: {sim.get('carrier') or sim.get('sim_name') or '—'}"
                     + (" (eSIM)" if sim.get("esim") else ""))
            net = sim.get("operator") or "no network"
            dt = sim.get("data_type", "")
            five = ""
            if dt == "NR":
                five = "5G standalone"
            elif sim.get("nr_state") in ("CONNECTED",):
                five = "5G non-standalone (LTE anchor + NR)"
            elif sim.get("nr_state") in ("NOT_RESTRICTED",):
                five = "5G available (not active)"
            ca = sim.get("cell_bandwidths") or []
            line = (f"{title} · network <b>{net}</b> ({sim.get('plmn', '')}) · data <b>{dt}</b> "
                    f"{DATA_STATE.get(sim.get('data_state'), '')} · voice {sim.get('voice_type', '')} · "
                    f"{SERVICE.get(sim.get('service'), '')}"
                    + (" · <b>roaming</b>" if sim.get("roaming") else "")
                    + (f" · {five}" if five else "")
                    + (f" · carrier aggregation {len(ca)} carriers ({' + '.join(str(b // 1000) for b in ca)} MHz)"
                       if len(ca) > 1 else "")
                    + ("" if sim.get("data_enabled", True) else " · <span style='color:#e5484d'>mobile data OFF</span>"))
            regs = sim.get("registrations") or []
            if any(r.get("transport", "").startswith("Wi-Fi") and r.get("registered") for r in regs):
                line += " · Wi-Fi calling registered"
            if sc:
                band, mhz = cell_band_freq(sc)
                big = []
                if "rsrp" in sc:
                    big.append(f"RSRP <b style='color:{col_rsrp(sc['rsrp'])}'>{sc['rsrp']} dBm</b>")
                if "rsrq" in sc:
                    big.append(f"RSRQ <b>{sc['rsrq']} dB</b>")
                sinr = sc.get("sinr", sc.get("rssnr"))
                if sinr is not None:
                    big.append(f"SINR <b style='color:{col_sinr(sinr)}'>{sinr} dB</b>")
                if "rscp" in sc:
                    big.append(f"RSCP <b>{sc['rscp']} dBm</b>")
                if "rssi" in sc and "rsrp" not in sc:
                    big.append(f"RSSI <b>{sc['rssi']} dBm</b>")
                line += (f"<br>&nbsp;&nbsp;Serving: <b>{sc.get('type')}</b> band <b>{band or '?'}</b>"
                         f"{f' ({mhz} MHz)' if mhz else ''} · PCI {sc.get('pci', sc.get('psc', '—'))} · "
                         + " · ".join(big) + f" · <span style='font-size:13pt'>{bars(sc.get('level', 0))}</span>")
                if sc.get("type") == "LTE" and sc.get("ta") is not None:
                    line += f" · ≈ {lte_ta_m(sc['ta']) / 1000:.2f} km to the tower (timing advance {sc['ta']})"
            elif sim.get("cells_error"):
                line += f"<br>&nbsp;&nbsp;cells unavailable: {sim['cells_error']}"
            parts.append(line)
            for c in sorted(sim.get("cells") or [], key=lambda c: (c is not sc, -(c.get("rsrp") or c.get("dbm") or -200))):
                band, mhz = cell_band_freq(c)
                ci = c.get("ci")
                enb = f"{ci >> 8}-{ci & 255}" if c.get("type") == "LTE" and isinstance(ci, int) else ""
                status = "serving" if c is sc else ("secondary" if c.get("status") == 2 else
                                                    "registered" if c.get("registered") else "neighbour")
                ta = c.get("ta")
                r = len(rows)
                rows.append([sim.get("slot", 0) + 1, status, c.get("type", ""), band, c.get("arfcn", ""),
                             float(mhz) if mhz else "", c.get("pci", c.get("psc", c.get("cpid", ""))),
                             c.get("tac", c.get("lac", "")), ci if ci is not None else "", enb,
                             c.get("rsrp", c.get("rscp", "")), c.get("rsrq", c.get("ecno", "")),
                             c.get("sinr", c.get("rssnr", "")), c.get("rssi", ""),
                             (round(lte_ta_m(ta)) if c.get("type") == "LTE" and ta is not None else ta if ta is not None else ""),
                             c.get("level", ""), (c.get("mcc", "") + c.get("mnc", ""))])
                if isinstance(c.get("rsrp"), int):
                    colours[(r, 10)] = col_rsrp(c["rsrp"])
                s_ = c.get("sinr", c.get("rssnr"))
                if isinstance(s_, int):
                    colours[(r, 12)] = col_sinr(s_)
        self.sim_lbl.setText("<br><br>".join(parts) or "No SIM / no cellular modem reported.")
        sel = self.cells.currentRow()
        fill_table(self.cells, rows, colours)
        if 0 <= sel < self.cells.rowCount():
            self.cells.selectRow(sel)

    def show_wifi(self, link: dict, default_transport):
        if not link.get("enabled"):
            self.wifi_lbl.setText("Wi-Fi is <b>OFF</b>.")
            return
        if not link.get("bssid") or link.get("rssi", -127) <= -127:
            self.wifi_lbl.setText("Wi-Fi is on, <b>not connected</b>.")
            self.chan.connected_bssid = ""
            return
        f = link.get("freq", 0)
        std = WIFI_STD.get(link.get("standard"), "")
        sec = WIFI_SEC.get(link.get("security"), "")
        self.chan.connected_bssid = link.get("bssid", "")
        self.wifi_lbl.setText(
            f"Connected to <b>{(link.get('ssid') or '').strip(chr(34))}</b> ({link.get('bssid')}) · RSSI "
            f"<b style='color:{col_rssi(link.get('rssi', -100))}'>{link.get('rssi')} dBm</b> · {wifi_band(f)} "
            f"channel {wifi_channel(f)} ({f} MHz) · {std} · {sec} · link {link.get('tx_mbps', link.get('link_mbps'))}"
            f"/{link.get('rx_mbps', '?')} Mbps (tx/rx, max {link.get('max_tx_mbps', '?')}) · IP {link.get('ip', '—')}"
            f" · MAC {link.get('mac', '')}"
            + (" · <b>carries the device's internet</b>" if default_transport == "wifi" else ""))

    # ================================================================== logging
    def sample_of(self, s: dict) -> dict:
        row = {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.get("t", 0) / 1000)), "t": s.get("t", 0) / 1000}
        sims = s.get("sims") or []
        if sims:
            sim = sims[0]
            sc = serving(sim) or {}
            band, mhz = cell_band_freq(sc) if sc else ("", "")
            row.update({"operator": sim.get("operator", ""), "data_type": sim.get("data_type", ""),
                        "cell_type": sc.get("type", ""), "band": band, "arfcn": sc.get("arfcn", ""), "mhz": mhz,
                        "pci": sc.get("pci", sc.get("psc", "")), "cell_id": sc.get("ci", ""),
                        "tac": sc.get("tac", sc.get("lac", "")), "rsrp": sc.get("rsrp", sc.get("rscp", "")),
                        "rsrq": sc.get("rsrq", ""), "sinr": sc.get("sinr", sc.get("rssnr", "")),
                        "level": sc.get("level", sim.get("level", "")), "neighbours": max(0, len(sim.get("cells") or []) - 1)})
        w = s.get("wifi") or {}
        row.update({"wifi_on": w.get("enabled", ""), "wifi_ssid": (w.get("ssid") or "").strip('"'),
                    "wifi_bssid": w.get("bssid", ""), "wifi_rssi": w.get("rssi", ""), "wifi_freq": w.get("freq", ""),
                    "wifi_tx_mbps": w.get("tx_mbps", ""), "bt_on": s.get("bt_on", ""),
                    "default_transport": s.get("default_transport", "")})
        return row

    def record(self, s: dict):
        row = self.sample_of(s)
        prev = self.samples[-1] if self.samples else None
        self.samples.append(row)
        if len(self.samples) > 50000:
            del self.samples[:10000]
        if self.log_writer:
            self.log_writer.writerow(row)
            self.log_file.flush()
        if prev:
            ev = []
            if (row.get("pci"), row.get("arfcn"), row.get("cell_type")) != (prev.get("pci"), prev.get("arfcn"), prev.get("cell_type")):
                ev.append(f"Serving cell changed: {prev.get('cell_type')} PCI {prev.get('pci')} (ARFCN {prev.get('arfcn')})"
                          f" → {row.get('cell_type')} PCI {row.get('pci')} (ARFCN {row.get('arfcn')}, band {row.get('band')})")
            if row.get("data_type") != prev.get("data_type"):
                ev.append(f"Data network type {prev.get('data_type')} → {row.get('data_type')}")
            if row.get("operator") != prev.get("operator"):
                ev.append(f"Network operator {prev.get('operator') or '—'} → {row.get('operator') or '—'}")
            if row.get("wifi_on") != prev.get("wifi_on"):
                ev.append("Wi-Fi turned " + ("on" if row.get("wifi_on") else "off"))
            if row.get("wifi_bssid") != prev.get("wifi_bssid"):
                ev.append(f"Wi-Fi access point {prev.get('wifi_ssid') or '—'} ({prev.get('wifi_bssid') or '—'}) → "
                          f"{row.get('wifi_ssid') or '—'} ({row.get('wifi_bssid') or '—'})")
            if row.get("bt_on") != prev.get("bt_on"):
                ev.append("Bluetooth turned " + ("on" if row.get("bt_on") else "off"))
            if row.get("default_transport") != prev.get("default_transport"):
                ev.append(f"Internet now over {row.get('default_transport') or 'nothing'} "
                          f"(was {prev.get('default_transport') or 'nothing'})")
            for e in ev:
                self.add_event(row["time"], e)
        self.update_graph()
        self.log_lbl.setText(f"{len(self.samples)} samples · {len(self.events)} events")

    def add_event(self, t: str, text: str):
        self.events.append((t, text))
        r = self.ev_table.rowCount()
        self.ev_table.setSortingEnabled(False)
        self.ev_table.insertRow(r)
        self.ev_table.setItem(r, 0, QTableWidgetItem(t))
        self.ev_table.setItem(r, 1, QTableWidgetItem(text))
        self.ev_table.scrollToBottom()

    def update_graph(self):
        def series(key):
            return [(s["t"], float(s[key])) for s in self.samples if isinstance(s.get(key), (int, float))]
        self.graph.series = [("Cell RSRP/RSCP", "#3e8ed0", "dBm", series("rsrp")),
                             ("Wi-Fi RSSI", "#46a758", "dBm", series("wifi_rssi")),
                             ("Cell SINR", "#f0c040", "dB", series("sinr"))]
        self.graph.update()

    def clear_log(self):
        self.samples.clear()
        self.events.clear()
        self.ev_table.setRowCount(0)
        self.update_graph()
        self.log_lbl.setText("")

    def log_folder(self) -> str:
        folder = os.path.join(os.path.expanduser("~"), "Documents", "AndroUnderTheHood", "Network")
        os.makedirs(folder, exist_ok=True)
        return folder

    FIELDS = ["time", "operator", "data_type", "cell_type", "band", "arfcn", "mhz", "pci", "cell_id", "tac", "rsrp",
              "rsrq", "sinr", "level", "neighbours", "wifi_on", "wifi_ssid", "wifi_bssid", "wifi_rssi", "wifi_freq",
              "wifi_tx_mbps", "bt_on", "default_transport"]

    def on_logfile(self, on: bool):
        if on:
            name = re.sub(r"[^\w.-]+", "_", self.api.device_name or "device")
            path = os.path.join(self.log_folder(), f"netlog_{name}_{time.strftime('%Y%m%d_%H%M%S')}.csv")
            self.log_file = open(path, "w", encoding="utf-8", newline="")
            self.log_writer = csv.DictWriter(self.log_file, fieldnames=self.FIELDS, extrasaction="ignore")
            self.log_writer.writeheader()
            self.api.message(f"Logging to {path}")
        elif self.log_file:
            self.log_file.close()
            self.log_file = self.log_writer = None

    def export_csv(self):
        if not self.samples:
            self.api.message("Nothing logged yet")
            return
        name = re.sub(r"[^\w.-]+", "_", self.api.device_name or "device")
        default = os.path.join(self.log_folder(), f"netlog_{name}_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export samples", default, "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(self.samples)
        with open(os.path.splitext(path)[0] + "_events.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "event"])
            w.writerows(self.events)
        self.api.message(f"Saved {path} (+ _events.csv)")

    # ================================================================== actions
    def over_network(self) -> bool:
        return not self.dev.is_adb

    def confirm(self, what: str, risky: bool) -> bool:
        msg = f"{what} on {self.api.device_name}?"
        if risky and self.over_network():
            msg += ("\n\nAndroUnderTheHood is connected to this device over the network. This can cut the connection "
                    "(for example when it runs over this Wi-Fi or mobile link), and you may have to reconnect – or "
                    "undo it on the device itself.")
        return QMessageBox.question(self, "Confirm", msg) == QMessageBox.StandardButton.Yes

    def action(self, cmd: str, what: str, risky: bool = False):
        if not self.confirm(what, risky):
            return
        self.api.message(f"{what}…")
        self.api.task(lambda: self.dev.run_action(cmd), lambda out: self.after_action(what, out),
                      lambda m: QMessageBox.warning(self, what, f"{what} failed:\n\n{m}"))

    def helper_action(self, args: str, what: str, risky: bool = False, system: bool = True):
        if not self.confirm(what, risky):
            return
        use_system = system and self.dev.root_ok()

        def work():
            rc, out, err = self.dev.run_helper(args, system=use_system, timeout=60)
            line = (out.strip().splitlines() or [""])[-1]
            if not line.startswith("OK"):
                raise RuntimeError(line[2:] if line.startswith("E ") else (err.strip()[-600:] or out.strip() or f"exit {rc}"))
            return line[3:]
        self.api.task(work, lambda out: self.after_action(what, out),
                      lambda m: QMessageBox.warning(self, what, f"{what} failed:\n\n{m}"
                                                    + ("" if use_system else "\n\nThis usually needs Root.")))

    def after_action(self, what: str, out: str):
        self.api.message(f"{what}: done {('– ' + out[:120]) if out else ''}")
        try:
            self.add_event(time.strftime("%Y-%m-%d %H:%M:%S"), f"Action: {what}")
        except RuntimeError:
            return
        QTimer.singleShot(2500, self.scan)

    def wifi_scan(self):
        self.api.message("Scanning Wi-Fi…")

        def work():
            rc, out, err = self.dev.run_helper("wifi-scan", timeout=40)
            start = out.find("[")
            if start < 0:
                raise RuntimeError((err or out).strip()[-600:])
            return json.loads(out[start:])

        self.api.task(work, self.show_scan)

    def show_scan(self, aps: list):
        self.scan_aps = aps
        rows, colours = [], {}
        for i, a in enumerate(sorted(aps, key=lambda a: -a.get("level", -100))):
            caps = a.get("caps", "")
            sec = ("WPA3" if "SAE" in caps else "WPA2" if "RSN" in caps or "WPA2" in caps else "WPA" if "WPA" in caps
                   else "WEP" if "WEP" in caps else "OWE" if "OWE" in caps else "open")
            if "EAP" in caps:
                sec += "-Enterprise"
            rows.append([a.get("ssid") or "(hidden)", a.get("bssid", ""), wifi_band(a.get("freq", 0)),
                         wifi_channel(a.get("freq", 0)), a.get("freq", 0), f"{WIDTH.get(a.get('width', 0), 20)} MHz",
                         a.get("level", ""), WIFI_STD.get(a.get("standard"), ""), sec, a.get("age_s", "")])
            colours[(i, 6)] = col_rssi(a.get("level", -100))
        try:
            fill_table(self.aps, rows, colours)
            self.chan.aps = aps
            self.chan.update()
            self.update_chan_info()
            self.api.message(f"Wi-Fi scan: {len(aps)} access points")
        except RuntimeError:
            pass

    def update_chan_info(self):
        band = self.chan_band.currentText()
        aps = [a for a in self.scan_aps if wifi_band(a.get("freq", 0)) == band]
        if not aps:
            self.chan_info.setText("")
            return
        load: dict[int, int] = {}
        for a in aps:
            load[wifi_channel(a["freq"])] = load.get(wifi_channel(a["freq"]), 0) + 1
        if band == "2.4 GHz":
            # the non-overlapping channels and how many networks overlap each one (±4 channels)
            best = min((1, 6, 11), key=lambda c: sum(n for ch, n in load.items() if abs(ch - c) < 5))
            hint = f" · least crowded of 1/6/11: channel {best}"
        else:
            hint = ""
        busiest = sorted(load.items(), key=lambda kv: -kv[1])[:3]
        self.chan_info.setText(f"{len(aps)} networks · busiest channels: "
                               + ", ".join(f"{c} ({n})" for c, n in busiest) + hint)

    def wifi_connect_dialog(self):
        d = QDialog(self)
        d.setWindowTitle("Connect to Wi-Fi")
        form = QFormLayout(d)
        ssid = QComboBox()
        ssid.setEditable(True)
        for a in sorted(self.scan_aps, key=lambda a: -a.get("level", -100)):
            if a.get("ssid") and ssid.findText(a["ssid"]) < 0:
                ssid.addItem(a["ssid"])
        sec = QComboBox()
        for k, label in (("wpa2", "WPA2 (personal)"), ("wpa3", "WPA3 (personal)"), ("open", "Open"), ("owe", "Enhanced open (OWE)")):
            sec.addItem(label, k)
        pw = QLineEdit()
        pw.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Network (SSID)", ssid)
        form.addRow("Security", sec)
        form.addRow("Password", pw)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        form.addRow(bb)
        if not self.scan_aps:
            form.insertRow(0, QLabel("Tip: press Scan first to fill the network list."))
        if d.exec() != QDialog.DialogCode.Accepted or not ssid.currentText().strip():
            return
        s, k, p = ssid.currentText().strip(), sec.currentData(), pw.text()
        cmd = f"cmd wifi connect-network {shq(s)} {k}" + (f" {shq(p)}" if k in ("wpa2", "wpa3") else "")
        self.action(cmd, f"Connect Wi-Fi to {s}", risky=True)

    def selected_saved(self):
        r = self.saved.currentRow()
        if r < 0:
            self.api.message("Select a saved network first")
            return None
        return int(self.saved.item(r, 0).data(Qt.ItemDataRole.DisplayRole)), self.saved.item(r, 1).text()

    def wifi_select_saved(self):
        s = self.selected_saved()
        if s:
            self.helper_action(f"wifi-select {s[0]}", f"Connect Wi-Fi to saved network {s[1]}", risky=True)

    def wifi_forget(self):
        s = self.selected_saved()
        if s:
            self.action(f"cmd wifi forget-network {s[0]}", f"Forget saved Wi-Fi network {s[1]}", risky=True)

    def bt_connect(self, on: bool):
        r = self.paired.currentRow()
        if r < 0:
            self.api.message("Select a paired device first")
            return
        addr, name = self.paired.item(r, 1).text(), self.paired.item(r, 0).text()
        self.helper_action(f"bt-{'connect' if on else 'disconnect'} {addr}",
                           f"{'Connect' if on else 'Disconnect'} Bluetooth device {name}")

    def bt_scan(self):
        """Android refuses an open (unfiltered) LE scan while the screen is off, so check that first."""
        secs = self.bt_secs.value()

        def check():
            rc, out, _ = self.dev.conn.exec("dumpsys power 2>/dev/null | grep -m1 -E 'mWakefulness='", timeout=20,
                                            as_root=self.dev.via_root)
            return out.decode(errors="replace")

        def go(state: str):
            wake = False
            if state.strip() and "Awake" not in state:
                wake = QMessageBox.question(
                    self, "Bluetooth scan",
                    "The device's screen is off. Android only scans for all nearby Bluetooth devices while the "
                    "screen is on.\n\nWake the screen for the scan?") == QMessageBox.StandardButton.Yes
            self.bt_scan_run(secs, wake)

        self.api.task(check, go, lambda _m: self.bt_scan_run(secs, False))

    def bt_scan_run(self, secs: int, wake: bool):
        self.api.message(f"Scanning for Bluetooth LE devices for {secs} s…")
        use_system = self.dev.root_ok()

        def work():
            if wake:
                self.dev.run_action("input keyevent KEYCODE_WAKEUP")
                time.sleep(1.0)
            rc, out, err = self.dev.run_helper(f"bt-scan {secs}", system=use_system, timeout=secs + 40)
            start = out.find("[")
            if start < 0:
                raise RuntimeError(((out.strip().splitlines() or [""])[-1][2:] or err.strip()[-600:]) or f"exit {rc}")
            return json.loads(out[start:])

        def done(devs):
            try:
                fill_table(self.nearby, [[d.get("name", ""), d.get("address", ""), d.get("rssi", ""), d.get("count", ""),
                                          "yes" if d.get("connectable") else "", d.get("tx_power", ""),
                                          (f"0x{d['manufacturer_id']:04X} {BT_COMPANY.get(d['manufacturer_id'], '')}".strip()
                                           if "manufacturer_id" in d else "")]
                                         for d in sorted(devs, key=lambda d: -d.get("rssi", -200))])
                self.api.message(f"Bluetooth scan: {len(devs)} devices")
                if not devs:
                    QMessageBox.information(self, "Bluetooth scan",
                                            "No devices were heard. Android hides Bluetooth scan results while "
                                            "Location is turned off on the device – check that first.")
            except RuntimeError:
                pass

        self.api.task(work, done, lambda m: QMessageBox.warning(self, "Bluetooth scan", m))

    def ping(self):
        host = self.ping_host.text().strip()
        if not re.fullmatch(r"[A-Za-z0-9.:\-]+", host):
            self.api.message("Enter a host name or IP address")
            return
        self.api.set_setting("ping_host", host)
        n = self.ping_n.value()
        self.ping_out.setPlainText(f"ping {host} …")
        six = ":" in host

        def work():
            rc, out, err = self.dev.conn.exec(f"{'ping6' if six else 'ping'} -c {n} -i 0.5 -W 3 {host}",
                                              timeout=n * 4 + 20, as_root=self.dev.via_root)
            return (out + err).decode("utf-8", "replace")

        self.api.task(work, lambda t: self.ping_out.setPlainText(t.strip()))

    def closeEvent(self, e):
        if self.watcher:
            self.watcher.stop()
            dev, token = self.dev, self.token
            threading.Thread(target=lambda: (time.sleep(1.5), dev.kill_leftovers(token)), daemon=True).start()
        if self.log_file:
            self.log_file.close()
        super().closeEvent(e)
