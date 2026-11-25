"""Accelerometer test plugin.

Detection:
  * every Android sensor that measures acceleration: accelerometer, uncalibrated, limited-axes (and uncalibrated),
    gravity, linear acceleration, their wake-up twins, dynamic (external) sensors and vendor-specific accel sensors
  * the hardware behind them where the kernel shows it: IIO devices with accel channels, input devices,
    MediaTek gsensor (chip, calibration, orientation), /sys/class/sensors (Samsung and others),
    Qualcomm sensor configs, I2C chip names
Streaming: a small dex helper (acctest.jar, source in helper/AccTest.java) registers a SensorManager listener as the
shell user (or root) and sends every event. The window draws a live graph, tilt level, measured sample rate, noise,
and runs a six-side test that measures each axis' offset and scale against gravity.
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QComboBox, QDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

JAR_REMOTE = "/data/local/tmp/androunderthehood-acctest.jar"
_NOWIN = 0x08000000 if sys.platform == "win32" else 0
G = 9.80665
MODES = [("auto", "Auto (shell user, then root)"), ("shell", "Shell user"), ("root", "Root")]
ACC_TYPES = {1: "Accelerometer", 35: "Accelerometer (uncalibrated)", 38: "Accelerometer (limited axes)",
             40: "Accelerometer (limited axes, uncalibrated)", 9: "Gravity", 10: "Linear acceleration"}
WITH_GRAVITY = {1, 35, 38, 40, 9}
ACCEL_RE = re.compile(r"accel|g-?sensor|gravity|linear.?acc|bma\d|bmi\d|bmc\d|bmx\d|bno\d|lsm6|lsm303|lis[23]d|"
                      r"icm\d|icm4|mpu\d|mxc\d|sc7a|kxtj|kx0\d|kx1\d|kx02|stk8|da2\d\d|qma\d|msa\d|mir3da|adxl", re.I)
RATES = [("Fastest", 0), ("400 Hz", 2500), ("200 Hz", 5000), ("100 Hz", 10000), ("Game (50 Hz)", 20000),
         ("UI (15 Hz)", 66667), ("Normal (5 Hz)", 200000)]
FACES = [("+X", 0, 1), ("−X", 0, -1), ("+Y", 1, 1), ("−Y", 1, -1), ("+Z", 2, 1), ("−Z", 2, -1)]
FACE_HINT = {"+X": "right edge down", "−X": "left edge down", "+Y": "standing upright (top up)",
             "−Y": "upside down (top down)", "+Z": "flat, screen up", "−Z": "flat, screen down"}
AXIS_COLORS = ["#e5484d", "#46a758", "#3e8ed0"]
HW_SCRIPT = r"""
echo "//IIO//"
for d in /sys/bus/iio/devices/iio:device*; do [ -e "$d" ] || continue
  echo "${d##*/}|$(cat $d/name 2>/dev/null)|$(ls $d 2>/dev/null | grep -c '^in_accel')"; done
echo "//INPUT//"
grep -E '^N: Name=' /proc/bus/input/devices 2>/dev/null
echo "//MTK//"
for f in chipinfo cali chip_orientation sensordata; do
  [ -r /sys/bus/platform/drivers/gsensor/$f ] && echo "$f=$(head -c 300 /sys/bus/platform/drivers/gsensor/$f | tr '\n' ' ')"; done
echo "//CLASS//"
for d in /sys/class/sensors/*; do [ -e "$d" ] || continue
  echo "${d##*/}|$(cat $d/name 2>/dev/null)|$(cat $d/vendor 2>/dev/null)"; done
echo "//QREG//"
for R in /mnt/vendor/persist/sensors/registry/registry /persist/sensors/registry/registry; do [ -d $R ] || continue
  for f in $R/*_[0-9].accel; do [ -e "$f" ] || continue; c=${f##*/}; c=${c%.accel}
    echo "$c|$(cat $R/${c}_platform.accel.fac_cal.bias 2>/dev/null)"; done; break; done
echo "//QCOM//"
ls /vendor/etc/sensors/config /odm/etc/sensors/config 2>/dev/null
echo "//I2C//"
for d in /sys/bus/i2c/devices/*-* /sys/bus/spi/devices/*; do [ -e "$d" ] || continue
  echo "${d##*/}|$(cat $d/name 2>/dev/null)|$(tr '\0' ' ' < $d/of_node/compatible 2>/dev/null)"; done
"""


def register(api):
    api.add_tool("Accelerometer", lambda: AccelDialog(api).show(), icon="accel",
                 tooltip="Find every acceleration sensor and test it live (plugin)", needs="connection")


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------------------------- device side
class Device:
    def __init__(self, api):
        self.api = api
        self.conn = api.conn
        self.jar = api.read_file("acctest.jar")
        self.user_uid0 = None
        self.user_uid = None
        self.via_root = False

    @property
    def is_adb(self) -> bool:
        return getattr(self.conn, "transport", None) is None

    def _root_ok(self) -> bool:
        try:
            return bool(self.api.is_root() or getattr(self.conn, "native_root", False))
        except Exception:  # noqa: BLE001
            return False

    def ensure_jar(self):
        """Upload the helper if missing/outdated and work out how to reach the shell user (uid 2000)."""
        want = hashlib.md5(self.jar).hexdigest()
        rc, out, _ = self.conn.exec("id -u", timeout=20, as_root=False)
        try:
            self.user_uid = int(out.decode(errors="replace").split()[0])
        except (ValueError, IndexError):
            self.user_uid = -1
        self.user_uid0 = self.user_uid == 0
        # A session running as an ordinary app (e.g. AndroOpenHeart without Shizuku) can neither use
        # /data/local/tmp nor the hardware APIs: go through root and drop to the shell user from there.
        self.via_root = self.user_uid not in (0, 2000)
        if self.via_root and not self._root_ok():
            raise RuntimeError(f"The connection runs as an app (uid {self.user_uid}), which Android does not let use "
                               "this hardware. Turn Root on, or start Shizuku on the device so the session runs as "
                               "the shell user, then try again.")
        rc, out, _ = self.conn.exec(f"md5sum {JAR_REMOTE} 2>/dev/null", timeout=20, as_root=self.via_root)
        if want in out.decode(errors="replace").split():
            return
        # an outdated copy owned by another user can't be overwritten; delete it first
        for root in (False, True):
            try:
                self.conn.exec(f"rm -f {JAR_REMOTE}", timeout=20, as_root=root)
            except Exception:  # noqa: BLE001
                pass
        if self.is_adb:
            os.makedirs(self.api.data_dir, exist_ok=True)
            tmp = os.path.join(self.api.data_dir, "acctest.jar")
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

    def command(self, args: str, root: bool) -> tuple[str, bool]:
        """Shell command + whether it must go through a root channel."""
        base = f"CLASSPATH={JAR_REMOTE} exec app_process / AccTest {args}"
        if root:
            return base, True
        if self.user_uid0 or self.via_root:     # drop to the shell user, the uid these APIs expect
            return f"su 2000 -c {shq(base)}", self.via_root
        return base, False

    def run(self, args: str, root: bool, timeout: float = 60) -> tuple[int, str, str]:
        cmd, as_root = self.command(args, root)
        rc, out, err = self.conn.exec(cmd, timeout=timeout, as_root=as_root)
        return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def open_stream(self, args: str, root: bool):
        cmd, as_root = self.command(args, root)
        if self.is_adb:
            full = self.conn.wrap_root(cmd) if as_root else cmd
            p = subprocess.Popen([self.conn.exe, "-s", self.conn.serial, "exec-out", full], stdout=subprocess.PIPE,
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
        """Kill one run's helper if it outlived its connection; root only if the shell user could not."""
        pat = shq("AccTest stream .* " + token + "$")
        for root in (False, True):
            try:
                rc, out, _ = self.conn.exec(f"pkill -f {pat} 2>/dev/null; sleep 0.3; pgrep -f {pat} >/dev/null && echo alive",
                                            timeout=15, as_root=root)
                if b"alive" not in out:
                    return
            except Exception:  # noqa: BLE001
                pass


def is_accel(s: dict) -> bool:
    if s["type"] in ACC_TYPES:
        return True
    if s["type"] < 65536:          # other standard types (gyroscope, magnetometer…) even if the chip name matches
        return False
    return bool(re.search(r"accel|gravity|linear.?acc", f"{s.get('string_type', '')} {s.get('name', '')}", re.I))


def parse_hw(text: str) -> list[dict]:
    """Hardware hints about the accelerometer chip(s)."""
    sec = {}
    cur = None
    for ln in text.splitlines():
        m = re.match(r"//(\w+)//", ln)
        if m:
            cur = m.group(1)
            sec[cur] = []
        elif cur and ln.strip():
            sec[cur].append(ln.strip())
    hw = []
    for ln in sec.get("IIO", []):
        dev, name, n = (ln.split("|") + ["", "", "0"])[:3]
        if n.strip() not in ("", "0") or ACCEL_RE.search(name):
            hw.append({"what": name or dev, "where": f"IIO {dev}", "detail": f"{n} accel channel file(s)"})
    for ln in sec.get("INPUT", []):
        name = ln.split("=", 1)[-1].strip('"')
        if ACCEL_RE.search(name):
            hw.append({"what": name, "where": "input device", "detail": "accelerometer exposed as an input device"})
    mtk = dict(x.split("=", 1) for x in sec.get("MTK", []) if "=" in x)
    if mtk:
        det = []
        if mtk.get("cali", "").strip():
            det.append("factory calibration " + mtk["cali"].strip())
        if mtk.get("chip_orientation", "").strip():
            det.append("orientation " + mtk["chip_orientation"].strip())
        hw.append({"what": mtk.get("chipinfo", "").strip() or "gsensor", "where": "MediaTek gsensor driver",
                   "detail": ", ".join(det) or "present"})
    for ln in sec.get("CLASS", []):
        dev, name, vendor = (ln.split("|") + ["", "", ""])[:3]
        if ACCEL_RE.search(dev + " " + name):
            hw.append({"what": " ".join(x for x in (vendor, name) if x) or dev, "where": f"/sys/class/sensors/{dev}",
                       "detail": ""})
    fitted = set()
    for ln in sec.get("QREG", []):          # Qualcomm sensor registry: what the sensor hub actually probed
        inst, _, bias = ln.partition("|")
        chip = re.sub(r"_\d+$", "", inst)
        fitted.add(chip)
        det = "fitted (found by the sensor hub)"
        try:
            b = next(iter(json.loads(bias).values()))
            det += " · factory bias " + ", ".join(f"{a} {float(b[a]['data']):+.4f}" for a in "xyz") + " m/s²"
        except Exception:  # noqa: BLE001
            pass
        hw.append({"what": chip, "where": f"Qualcomm sensor registry ({inst})", "detail": det})
    configs: dict[str, int] = {}
    for ln in sec.get("QCOM", []):          # driver configs ship for every board variant, fitted or not
        m = re.match(r"(?:.*_)?([a-z]+\d\w*?)_\d+\.json$", ln)
        if m and ACCEL_RE.search(m.group(1)):
            configs[m.group(1)] = configs.get(m.group(1), 0) + 1
    for chip, n in configs.items():
        if chip not in fitted:
            hw.append({"what": chip, "where": "Qualcomm sensors config",
                       "detail": f"driver config for {n} board variant(s) – may not be fitted in this device"})
    for ln in sec.get("I2C", []):
        dev, name, compat = (ln.split("|") + ["", "", ""])[:3]
        if ACCEL_RE.search(name + " " + compat) and not re.search(r"camera|ccu_", name, re.I):
            hw.append({"what": name, "where": f"bus {dev}", "detail": compat})
    return hw


def detect(dev: Device, mode: str, root_ok: bool) -> tuple[dict, bool]:
    dev.ensure_jar()
    tries = {"auto": [False, True] if root_ok else [False], "shell": [False], "root": [True]}[mode]
    data, used, last = None, False, ""
    for root in tries:
        rc, out, err = dev.run("list", root)
        start = out.find("{")
        if rc == 0 and start >= 0:
            data, used = json.loads(out[start:]), root
            break
        last = (err.strip() or out.strip() or f"exit code {rc}")[-1500:]
    if data is None:
        raise RuntimeError(last)
    hw = []
    for root in ([True, False] if root_ok else [False]):     # chip details are usually root-only
        try:
            rc, out, _ = dev.conn.exec(HW_SCRIPT, timeout=60, as_root=root)
            hw = parse_hw(out.decode("utf-8", "replace"))
            if hw:
                break
        except Exception:  # noqa: BLE001
            pass
    names = " ".join(s.get("name", "") for s in data.get("sensors", []) if is_accel(s)).lower()
    for h in hw:
        if h["where"].startswith("Qualcomm sensor registry"):
            if h["what"].lower() in names:
                h["detail"] = h["detail"].replace("fitted (found by the sensor hub)", "in use – fitted and used by Android")
            else:
                h["detail"] = h["detail"].replace("fitted (found by the sensor hub)",
                                                  "registered, not used by Android (second-source part, likely not fitted)")
    data["hardware"] = hw
    return data, used


# ---------------------------------------------------------------------------------------------- streaming
class Streamer(QObject):
    info = Signal(dict)
    accuracy = Signal(int)
    failed = Signal(str)
    ended = Signal()

    def __init__(self, dev: Device, args: str, root: bool, prev_token: str = ""):
        super().__init__()
        self.dev, self.args, self.root, self.prev_token = dev, args, root, prev_token
        self._stop = threading.Event()
        self._stopfn = None
        self.lock = threading.Lock()
        self.events = collections.deque(maxlen=20000)     # (t seconds, values tuple)
        self.count = 0
        self.csv = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._stopfn:
            try:
                self._stopfn()
            except Exception:  # noqa: BLE001
                pass

    def start_csv(self, path: str, labels: list[str]):
        with self.lock:
            self.csv = open(path, "w", encoding="utf-8", newline="")
            self.csv.write("timestamp_ns," + ",".join(labels) + "\n")

    def stop_csv(self):
        with self.lock:
            if self.csv:
                self.csv.close()
                self.csv = None

    def _run(self):
        got = False
        try:
            if self.prev_token:
                self.dev.kill_leftovers(self.prev_token)
            read, self._stopfn, stderr = self.dev.open_stream(self.args, self.root)
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
                batch = []
                for raw in lines:
                    if raw.startswith(b"D "):
                        p = raw.split()
                        try:
                            batch.append((int(p[1]), tuple(float(x) for x in p[2:])))
                        except ValueError:
                            continue
                    elif raw.startswith(b"I "):
                        self.info.emit(json.loads(raw[2:]))
                    elif raw.startswith(b"A "):
                        self.accuracy.emit(int(raw[2:]))
                    elif raw.startswith(b"E "):
                        raise RuntimeError(raw[2:].decode(errors="replace"))
                if batch:
                    got = True
                    with self.lock:
                        for ts, v in batch:
                            self.events.append((ts / 1e9, v))
                        self.count += len(batch)
                        if self.csv:
                            self.csv.write("".join(f"{ts}," + ",".join(f"{x:.6g}" for x in v) + "\n" for ts, v in batch))
            if not self._stop.is_set():
                msg = stderr().strip()[-1500:]
                raise RuntimeError(msg or ("The helper stopped." if got else "The sensor sent no data."))
        except Exception as e:  # noqa: BLE001
            if not self._stop.is_set():
                self.failed.emit(str(e) or repr(e))
        finally:
            if self._stopfn:
                try:
                    self._stopfn()
                except Exception:  # noqa: BLE001
                    pass
            self.stop_csv()
            self.ended.emit()


# ---------------------------------------------------------------------------------------------- widgets
class Graph(QWidget):
    """Rolling graph of X/Y/Z and the magnitude."""

    def __init__(self, theme):
        super().__init__()
        self.theme = theme
        self.events: list = []
        self.span = 5.0
        self.unit_g = False
        self.text = "Pick a sensor and press Start."
        self.setMinimumSize(380, 220)

    def sizeHint(self):
        return QSize(640, 300)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#101418"))
        W, H = self.width(), self.height()
        if not self.events:
            p.setPen(QColor(self.theme.TEXT_DIM))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.text)
            return
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)
        x0, x1, top, bot = 44, W - 10, 10, H - 22
        t_end = self.events[-1][0]
        ev = [e for e in self.events if e[0] >= t_end - self.span]
        k = 1 / G if self.unit_g else 1.0
        peak = max((abs(v) * k for _t, vals in ev for v in vals[:3]), default=1.0)
        mags = [math.sqrt(sum(v * v for v in vals[:3])) * k for _t, vals in ev]
        peak = max(peak, max(mags, default=0))
        lim = 1.2 if self.unit_g else 12.0
        while lim < peak * 1.05:
            lim *= 2
        ymid = (top + bot) / 2

        def Y(v):
            return ymid - v / lim * (bot - top) / 2

        p.setPen(QColor("#2a333c"))
        steps = [lim, lim / 2, 0, -lim / 2, -lim]
        for s in steps:
            p.drawLine(x0, int(Y(s)), x1, int(Y(s)))
        p.setPen(QColor("#7d8790"))
        for s in steps:
            p.drawText(QRectF(0, Y(s) - 7, x0 - 4, 14), Qt.AlignmentFlag.AlignRight, f"{s:g}")
        p.drawText(QRectF(x0, bot + 4, 200, 14), Qt.AlignmentFlag.AlignLeft, f"last {self.span:g} s · "
                                                                             f"{'g' if self.unit_g else 'm/s²'}")
        if not self.unit_g:
            p.setPen(QPen(QColor("#5b6670"), 1, Qt.PenStyle.DotLine))
            for s in (G, -G):
                if abs(s) < lim:
                    p.drawLine(x0, int(Y(s)), x1, int(Y(s)))

        def X(t):
            return x1 - (t_end - t) / self.span * (x1 - x0)

        stride = max(1, len(ev) // int(x1 - x0))
        series = [(c, [(X(t), vals[c] * k) for t, vals in ev[::stride] if len(vals) > c]) for c in range(3)]
        series.append((3, [(X(t), m) for (t, _v), m in list(zip(ev, mags))[::stride]]))
        for c, pts in series:
            if len(pts) < 2:
                continue
            path = QPainterPath(QPointF(pts[0][0], Y(pts[0][1])))
            for x, v in pts[1:]:
                path.lineTo(QPointF(x, Y(v)))
            pen = QPen(QColor(AXIS_COLORS[c] if c < 3 else "#d0d0d0"), 1.6 if c < 3 else 1.2)
            if c == 3:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawPath(path)
        for i, (lbl, col) in enumerate((("X", AXIS_COLORS[0]), ("Y", AXIS_COLORS[1]), ("Z", AXIS_COLORS[2]),
                                        ("|a|", "#d0d0d0"))):
            p.setPen(QColor(col))
            p.drawText(QRectF(x1 - 160 + i * 40, top, 40, 14), Qt.AlignmentFlag.AlignCenter, lbl)


class Level(QWidget):
    """Bubble level from X/Y; shows pitch/roll."""

    def __init__(self):
        super().__init__()
        self.x = self.y = self.z = 0.0
        self.active = False
        self.setMinimumSize(170, 170)

    def sizeHint(self):
        return QSize(190, 190)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = min(self.width(), self.height()) - 8
        cx, cy, r = self.width() / 2, self.height() / 2, s / 2
        p.setBrush(QColor("#161c22"))
        p.setPen(QPen(QColor("#3a444e"), 2))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setPen(QPen(QColor("#3a444e"), 1))
        p.drawEllipse(QPointF(cx, cy), r * 0.1, r * 0.1)
        p.drawEllipse(QPointF(cx, cy), r * 0.5, r * 0.5)
        p.drawLine(QPointF(cx - r, cy), QPointF(cx + r, cy))
        p.drawLine(QPointF(cx, cy - r), QPointF(cx, cy + r))
        if not self.active:
            return
        n = math.sqrt(self.x ** 2 + self.y ** 2 + self.z ** 2) or 1
        # the bubble rises to the higher edge, like a real level: the accelerometer reads +g along an axis that
        # points up, so +X (right edge up) moves it right and +Y (top edge up) moves it up (Qt's y grows downwards)
        bx, by = self.x / n, -self.y / n
        d = math.hypot(bx, by)
        if d > 1:
            bx, by = bx / d, by / d
        level = d < 0.035
        p.setBrush(QColor("#46a758" if level else "#f0c040"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx + bx * (r - 12), cy + by * (r - 12)), 11, 11)


# ---------------------------------------------------------------------------------------------- window
class AccelDialog(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"Accelerometer – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1200, 760)
        self.dev = Device(api)
        self.data: dict = {}
        self.used_root = False
        self.streamer: Streamer | None = None
        self.token = ""
        self.sensor: dict | None = None
        self.acc = -1
        self.faces: dict[str, list] = {}
        self.minmax = None
        self.peak_dyn = 0.0
        self.t_start = 0.0
        self.csv_path = ""

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Run as"))
        self.mode = QComboBox()
        for k, label in MODES:
            self.mode.addItem(label, k)
        self.mode.setCurrentIndex([m[0] for m in MODES].index(api.get_setting("mode", "auto")))
        self.mode.currentIndexChanged.connect(lambda _i: api.set_setting("mode", self.mode.currentData()))
        top.addWidget(self.mode)
        self.b_scan = QPushButton(api.icon("refresh"), " Detect sensors")
        self.b_scan.clicked.connect(self.scan)
        top.addWidget(self.b_scan)
        top.addStretch(1)
        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color: {api.theme.TEXT_DIM};")
        top.addWidget(self.summary)
        lay.addLayout(top)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Sensor", "Vendor", "Details"])
        self.tree.setColumnWidth(0, 210)
        self.tree.setColumnWidth(1, 80)
        self.tree.currentItemChanged.connect(lambda *_: self.on_select())
        self.tree.itemDoubleClicked.connect(lambda it, _c: self.start() if it.data(0, Qt.ItemDataRole.UserRole) else None)
        ll.addWidget(self.tree, 1)
        self.details = QLabel("")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.setStyleSheet(f"color: {api.theme.TEXT_DIM};")
        ll.addWidget(self.details)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.graph = Graph(api.theme)
        rl.addWidget(self.graph, 3)
        mid = QHBoxLayout()
        self.level = Level()
        mid.addWidget(self.level)
        grid = QGridLayout()
        self.vals: dict[str, QLabel] = {}
        rows = [("x", "X"), ("y", "Y"), ("z", "Z"), ("mag", "|a|"), ("tilt", "Pitch / roll"), ("rate", "Sample rate"),
                ("noise", "Noise (σ, 1 s)"), ("range", "Min / max"), ("peak", "Peak movement"), ("acc", "Accuracy"),
                ("extra", "Other values")]
        for r, (k, label) in enumerate(rows):
            a = QLabel(label)
            a.setStyleSheet(f"color: {api.theme.TEXT_DIM};")
            b = QLabel("—")
            b.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if k in ("x", "y", "z"):
                b.setStyleSheet(f"font-weight: 600; font-size: 12pt; color: {AXIS_COLORS['xyz'.index(k)]};")
            elif k == "mag":
                b.setStyleSheet("font-weight: 600; font-size: 12pt;")
            grid.addWidget(a, r, 0)
            grid.addWidget(b, r, 1)
            self.vals[k] = b
        grid.setColumnStretch(1, 1)
        mid.addLayout(grid, 1)
        self.facebox = QGroupBox("Six-side test – hold the device still on each side")
        fg = QGridLayout(self.facebox)
        self.face_lbls: dict[str, QLabel] = {}
        for i, (name, _ax, _sg) in enumerate(FACES):
            l = QLabel(f"{name}: {FACE_HINT[name]}")
            l.setToolTip(FACE_HINT[name])
            fg.addWidget(l, i, 0)
            self.face_lbls[name] = l
        self.face_result = QLabel("")
        self.face_result.setWordWrap(True)
        fg.addWidget(self.face_result, 6, 0)
        b_reset = QPushButton("Reset test")
        b_reset.clicked.connect(self.reset_faces)
        fg.addWidget(b_reset, 7, 0)
        mid.addWidget(self.facebox, 1)
        rl.addLayout(mid, 2)

        row = QHBoxLayout()
        row.addWidget(QLabel("Rate"))
        self.rate = QComboBox()
        for label, us in RATES:
            self.rate.addItem(label, us)
        self.rate.setCurrentIndex(int(api.get_setting("rate_idx", 3)))
        row.addWidget(self.rate)
        row.addWidget(QLabel("Units"))
        self.units = QComboBox()
        self.units.addItems(["m/s²", "g"])
        self.units.setCurrentIndex(int(api.get_setting("units_g", 0)))
        self.units.currentIndexChanged.connect(lambda i: (api.set_setting("units_g", i), setattr(self.graph, "unit_g", i == 1)))
        self.graph.unit_g = self.units.currentIndex() == 1
        row.addWidget(self.units)
        row.addWidget(QLabel("Window"))
        self.span = QComboBox()
        for s in (2, 5, 10, 30):
            self.span.addItem(f"{s} s", s)
        self.span.setCurrentIndex(1)
        self.span.currentIndexChanged.connect(lambda _i: setattr(self.graph, "span", float(self.span.currentData())))
        row.addWidget(self.span)
        self.b_start = QPushButton("Start")
        self.b_start.clicked.connect(self.toggle)
        row.addWidget(self.b_start)
        b_rs = QPushButton("Reset stats")
        b_rs.clicked.connect(self.reset_stats)
        row.addWidget(b_rs)
        self.b_csv = QPushButton(api.icon("download"), " Record CSV")
        self.b_csv.setCheckable(True)
        self.b_csv.setEnabled(False)
        self.b_csv.toggled.connect(self.on_csv)
        row.addWidget(self.b_csv)
        row.addStretch(1)
        rl.addLayout(row)
        split.addWidget(right)
        split.setSizes([420, 780])
        lay.addWidget(split, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(40)
        self.reset_faces()
        self.scan()

    # ---- detection
    def root_ok(self) -> bool:
        return self.api.is_root() or bool(getattr(self.api.conn, "native_root", False))

    def scan(self):
        self.stop()
        self.b_scan.setEnabled(False)
        self.summary.setText("Detecting sensors…")
        self.tree.clear()
        mode, root_ok = self.mode.currentData(), self.root_ok()

        def done(res):
            try:
                self.b_scan.setEnabled(True)
                self.fill(*res)
            except RuntimeError:
                pass

        def fail(msg):
            try:
                self.b_scan.setEnabled(True)
                self.summary.setText("Detection failed")
                QMessageBox.warning(self, "Accelerometer", f"Could not list the sensors:\n\n{msg}")
            except RuntimeError:
                pass

        self.api.task(lambda: detect(self.dev, mode, root_ok), done, fail)

    def fill(self, data: dict, used_root: bool):
        self.data, self.used_root = data, used_root
        sensors = data.get("sensors", [])
        accs = [s for s in sensors if is_accel(s)]
        dim = QColor(self.api.theme.TEXT_FAINT)

        def group(title):
            g = QTreeWidgetItem([title])
            f = g.font(0)
            f.setBold(True)
            g.setFont(0, f)
            g.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(g)
            g.setExpanded(True)
            return g

        first = None
        if accs:
            g = group(f"Acceleration sensors ({len(accs)})")
            order = {1: 0, 35: 1, 38: 2, 40: 3, 9: 4, 10: 5}
            for s in sorted(accs, key=lambda s: (order.get(s["type"], 9), s["wakeup"], s["index"])):
                kind = ACC_TYPES.get(s["type"], s.get("string_type") or f"type {s['type']}")
                tags = [kind]
                if s["wakeup"]:
                    tags.append("wake-up")
                if s.get("dynamic"):
                    tags.append("external")
                if s["type"] in (9, 10):
                    tags.append("virtual (fused)")
                maxhz = f"{1e6 / s['min_delay_us']:.0f} Hz max" if s.get("min_delay_us", 0) > 0 else ""
                rng = f"±{s['max_range'] / G:.0f} g" if s.get("max_range") else ""
                it = QTreeWidgetItem(g, [s["name"], s.get("vendor", ""), ", ".join(t for t in tags + [rng, maxhz] if t)])
                it.setData(0, Qt.ItemDataRole.UserRole, s)
                first = first or it
        hw = data.get("hardware", [])
        if hw:
            g = group(f"Hardware ({len(hw)})")
            for h in hw:
                it = QTreeWidgetItem(g, [h["what"], h["where"], h["detail"]])
                it.setToolTip(0, "Information from the kernel; the sensor itself is streamed through the rows above.")
                for c in range(3):
                    it.setForeground(c, dim)
        others = [s for s in sensors if not is_accel(s)]
        if others:
            g = group(f"Other sensors on the device ({len(others)})")
            g.setExpanded(False)
            for s in others:
                it = QTreeWidgetItem(g, [s["name"], s.get("vendor", ""), s.get("string_type", "")])
                for c in range(3):
                    it.setForeground(c, dim)
        txt = (f"{data.get('model', '')} · Android API {data.get('sdk')} · {len(accs)} acceleration sensor(s) "
               f"of {len(sensors)}")
        if hw:
            txt += f" · {len(hw)} hardware hint(s)"
        txt += " · as root" if used_root else " · as shell user"
        if not accs:
            txt += " · NO accelerometer reported"
        self.summary.setText(txt)
        if first:
            self.tree.setCurrentItem(first)

    def target(self) -> dict | None:
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def on_select(self):
        s = self.target()
        if not s:
            self.details.setText("")
            return
        md = s.get("min_delay_us", 0)
        maxhz = f" ({1e6 / md:.0f} Hz)" if md > 0 else ""
        lines = [f"<b>{s['name']}</b> – {s.get('vendor')} v{s.get('version')} · {s.get('string_type')} ({s['type']})",
                 f"Range ±{s.get('max_range', 0):.2f} m/s² (±{s.get('max_range', 0) / G:.1f} g) · resolution "
                 f"{s.get('resolution', 0):.5g} m/s² · power {s.get('power_ma', 0):g} mA",
                 f"Min period {md} µs{maxhz} · max period {s.get('max_delay_us')} µs · {s.get('reporting')} · "
                 f"FIFO {s.get('fifo_reserved')}/{s.get('fifo_max')}{' · wake-up' if s.get('wakeup') else ''}"]
        if s["type"] == 10:
            lines.append("Linear acceleration has gravity removed: at rest all axes should read ≈ 0. "
                         "The six-side test does not apply.")
        if s["type"] == 9:
            lines.append("Gravity is computed (fused) from the accelerometer and gyroscope.")
        self.details.setText("<br>".join(lines))
        self.facebox.setEnabled(s["type"] in WITH_GRAVITY or s["type"] not in ACC_TYPES)

    # ---- streaming
    def toggle(self):
        self.stop() if self.streamer else self.start()

    def start(self, root: bool | None = None):
        s = self.target()
        if not s:
            return
        prev = self.token if self.streamer else ""
        self.stop()
        if root is None:
            root = self.used_root or self.mode.currentData() == "root"
        self.api.set_setting("rate_idx", self.rate.currentIndex())
        self.sensor = s
        self.token = f"t{os.getpid()}x{int(time.time() * 1000) % 10**9}"
        st = Streamer(self.dev, f"stream {s['index']} {self.rate.currentData()} {self.token}", root, prev)
        st.accuracy.connect(lambda a: setattr(self, "acc", a))
        st.failed.connect(lambda m, st=st, root=root: self.on_failed(st, m, root))
        st.ended.connect(lambda st=st: self.on_ended(st))
        self.streamer = st
        self.acc = -1
        self.reset_stats()
        self.graph.events = []
        self.graph.text = "Waiting for the sensor…"
        self.graph.update()
        self.b_start.setText("Stop")
        self.b_csv.setEnabled(True)
        st.start()

    def stop(self):
        st, self.streamer = self.streamer, None
        if st:
            st.stop()
            dev, token = self.dev, self.token
            threading.Thread(target=lambda: (time.sleep(1.5), dev.kill_leftovers(token)), daemon=True).start()
        self.b_start.setText("Start")
        if self.b_csv.isChecked():
            self.b_csv.setChecked(False)
        self.b_csv.setEnabled(False)
        self.level.active = False
        self.level.update()

    def on_failed(self, st, msg, root):
        if self.streamer is not st:
            return
        if not root and self.mode.currentData() == "auto" and self.root_ok():
            self.streamer = None
            QTimer.singleShot(300, lambda: self.start(root=True))
            return
        self.graph.events = []
        self.graph.text = f"Sensor stream failed:\n{msg}"
        self.graph.update()

    def on_ended(self, st):
        if self.streamer is st:
            self.streamer = None
            try:
                self.b_start.setText("Start")
                self.b_csv.setChecked(False)
                self.b_csv.setEnabled(False)
            except RuntimeError:
                pass

    def on_csv(self, on: bool):
        st = self.streamer
        if not st:
            return
        if on:
            folder = os.path.join(os.path.expanduser("~"), "Documents", "AndroUnderTheHood", "Sensors")
            os.makedirs(folder, exist_ok=True)
            name = re.sub(r"[^\w.-]+", "_", (self.sensor or {}).get("name", "sensor"))
            self.csv_path = os.path.join(folder, f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.csv")
            n = max((len(v) for _t, v in list(st.events)[-1:]), default=3)
            labels = ["x", "y", "z", "v3", "v4", "v5", "v6", "v7", "v8"][:max(3, n)]
            st.start_csv(self.csv_path, labels)
            self.b_csv.setText(" Stop recording")
        else:
            st.stop_csv()
            self.b_csv.setText(" Record CSV")
            if self.csv_path:
                self.api.message(f"Saved {self.csv_path}")

    def reset_stats(self):
        self.minmax = None
        self.peak_dyn = 0.0
        self.t_start = time.monotonic()

    def reset_faces(self):
        self.faces = {}
        for name, lbl in self.face_lbls.items():
            lbl.setText(f"○ {name}: {FACE_HINT[name]}")
            lbl.setStyleSheet("")
        self.face_result.setText("Turn the device so each side faces up in turn; a side is captured after ~1 s "
                                 "of stillness.")

    # ---- live update
    def tick(self):
        st = self.streamer
        if not st:
            return
        with st.lock:
            ev = list(st.events)
        if not ev:
            return
        self.graph.events = ev
        self.graph.update()
        g = self.units.currentIndex() == 1
        k, unit = (1 / G, "g") if g else (1.0, "m/s²")
        t_end, last = ev[-1]
        x, y, z = (list(last) + [0, 0, 0])[:3]
        mag = math.sqrt(x * x + y * y + z * z)
        for key, v in (("x", x), ("y", y), ("z", z)):
            self.vals[key].setText(f"{v * k:+.3f} {unit}")
        self.vals["mag"].setText(f"{mag * k:.3f} {unit}"
                                 + (f"  ({(mag - G) / G * 100:+.1f}% vs 1 g)" if self.sensor and self.sensor["type"]
                                    in WITH_GRAVITY else ""))
        if mag > 0.5:
            pitch = math.degrees(math.atan2(-x, math.hypot(y, z)))
            roll = math.degrees(math.atan2(y, z))
            self.vals["tilt"].setText(f"{pitch:+.1f}° / {roll:+.1f}°")
        recent = [e for e in ev if e[0] >= t_end - 1.0]
        if len(recent) >= 2:
            dts = [b[0] - a[0] for a, b in zip(recent, recent[1:]) if b[0] > a[0]]
            if dts:
                hz = (len(recent) - 1) / (recent[-1][0] - recent[0][0]) if recent[-1][0] > recent[0][0] else 0
                jit = statistics.pstdev(dts) * 1000 if len(dts) > 1 else 0
                self.vals["rate"].setText(f"{hz:.1f} Hz (jitter {jit:.2f} ms)")
            cols = list(zip(*[v[:3] for _t, v in recent if len(v) >= 3]))
            if len(cols) == 3 and len(cols[0]) > 2:
                sig = [statistics.pstdev(c) * k for c in cols]
                self.vals["noise"].setText(" / ".join(f"{s:.4f}" for s in sig) + f" {unit}")
        if self.minmax is None:
            self.minmax = [[v, v] for v in (x, y, z)]
        for i, v in enumerate((x, y, z)):
            self.minmax[i][0] = min(self.minmax[i][0], v)
            self.minmax[i][1] = max(self.minmax[i][1], v)
        self.vals["range"].setText("  ".join(f"{a}: {lo * k:+.2f}…{hi * k:+.2f}" for a, (lo, hi)
                                             in zip("XYZ", self.minmax)))
        ref = G if self.sensor and self.sensor["type"] in WITH_GRAVITY else 0.0
        for _t, v in recent:
            m = math.sqrt(sum(c * c for c in v[:3]))
            self.peak_dyn = max(self.peak_dyn, abs(m - ref))
        self.vals["peak"].setText(f"{self.peak_dyn * k:.3f} {unit} away from {'1 g' if ref else 'rest'}")
        self.vals["acc"].setText({-1: "—", 0: "unreliable", 1: "low", 2: "medium", 3: "high"}.get(self.acc, str(self.acc)))
        extra = list(last[3:])
        if extra:
            names = {35: "bias", 40: "bias / axis flags", 38: "axis supported flags"}.get(self.sensor["type"], "values")
            self.vals["extra"].setText(f"{names}: " + ", ".join(f"{v:.4g}" for v in extra))
        else:
            self.vals["extra"].setText("—")
        self.level.x, self.level.y, self.level.z = x, y, z
        self.level.active = True
        self.level.update()
        if self.facebox.isEnabled():
            self.check_face(ev, t_end)

    def check_face(self, ev, t_end):
        win = [v[:3] for t, v in ev if t >= t_end - 1.0 and len(v) >= 3]
        if len(win) < 5:
            return
        mags = [math.sqrt(a * a + b * b + c * c) for a, b, c in win]
        if statistics.pstdev(mags) > 0.08 or abs(statistics.fmean(mags) - G) > 2.0:
            return                                          # moving
        mean = [statistics.fmean(c) for c in zip(*win)]
        for name, ax, sign in FACES:
            other = [abs(mean[i]) for i in range(3) if i != ax]
            if mean[ax] * sign > 0.85 * G and max(other) < 0.3 * G:
                first = name not in self.faces
                self.faces[name] = mean
                lbl = self.face_lbls[name]
                lbl.setText(f"✔ {name}: {mean[ax]:+.3f} m/s² ({FACE_HINT[name]})")
                lbl.setStyleSheet("color: #46a758; font-weight: 600;")
                if first:
                    self.update_face_result()
                break

    def update_face_result(self):
        res = []
        verdicts = []
        for i, a in enumerate("XYZ"):
            p, n = self.faces.get("+" + a), self.faces.get("−" + a)
            if p and n:
                off = (p[i] + n[i]) / 2
                scale = (p[i] - n[i]) / (2 * G)
                ok = abs(off) < 0.5 and abs(scale - 1) < 0.03
                verdicts.append(ok)
                res.append(f"{a}: offset {off:+.3f} m/s², scale {scale:.4f} {'✔' if ok else '⚠'}")
            elif p or n:
                v = (p or n)[i]
                res.append(f"{a}: one side {abs(v):.3f} m/s² ({(abs(v) - G) / G * 100:+.1f}% vs 1 g)")
        txt = "<br>".join(res)
        if len(verdicts) == 3:
            txt += "<br><b>" + ("All axes within ±0.5 m/s² offset and ±3% scale – the sensor is healthy."
                                if all(verdicts) else "Some axis is outside ±0.5 m/s² offset or ±3% scale – "
                                                      "it may need calibration or be damaged.") + "</b>"
        self.face_result.setText(txt or "")

    def closeEvent(self, e):
        self.timer.stop()
        self.stop()
        super().closeEvent(e)
