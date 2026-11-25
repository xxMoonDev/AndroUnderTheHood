"""GPS & location test plugin.

Hardware info: GNSS chip/firmware name and year (LocationManager), GNSS capabilities, location providers and their
properties, GNSS HAL services, vendor GNSS stack (config files / properties), and live: satellite systems and
frequency bands in view, time to first fix.

Tracking: a small dex helper (gpstest.jar, source in helper/GpsTest.java) requests a fix every N seconds (10 by default)
and streams fixes and satellite status. With root it runs as the system user, which Android treats as a foreground
caller (full rate + satellite status); as the plain shell user Android throttles background location.
The map draws OpenStreetMap tiles (cached under the plugin's data folder) or a plain grid, plots every logged fix and
joins them in time order.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from collections import OrderedDict

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

JAR_REMOTE = "/data/local/tmp/androunderthehood-gpstest.jar"
_NOWIN = 0x08000000 if sys.platform == "win32" else 0
MODES = [("auto", "Auto (system user if root, else shell)"), ("system", "System user (needs root)"),
         ("shell", "Shell user (Android throttles it)")]
CONST_COLORS = {"GPS": "#3e8ed0", "GLONASS": "#e5484d", "Galileo": "#f0c040", "BeiDou": "#46a758", "QZSS": "#a070e0",
                "SBAS": "#9aa4ad", "NavIC": "#e08a3c", "Unknown": "#666666"}
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "AndroUnderTheHood-GPS-test-plugin/1.0"
TILE_MAX_ZOOM = 19
MAX_ZOOM = 21
HW_SCRIPT = r"""
echo "//HAL//"
service list 2>/dev/null | grep -i -E 'gnss|location|gps' | sed 's/^[0-9]*[[:space:]]*//'
lshal 2>/dev/null | grep -i -E 'gnss|gps' | awk '{print $3}' | sort -u
echo "//PROPS//"
getprop | grep -i -E '^\[[^]]*(gnss|gps|izat|mnld|agps)[^]]*\]:' | head -40
echo "//FILES//"
ls /vendor/etc /odm/etc /vendor/etc/gnss 2>/dev/null | grep -i -E 'gps|gnss|izat|lowi|xtra|sap\.conf|mnl|agps|lhd|sirf|bcm47|glgps|ublox' | sort -u
echo "//DUMP//"
dumpsys location 2>/dev/null | grep -E 'Hardware Version|Capabilities:|Used-in-fix|Top 4 Avg CN0|Number of GNSS|TTFF|Hardware model' | head -30
"""


def register(api):
    api.add_tool("GPS", lambda: GpsDialog(api).show(), icon="pin",
                 tooltip="GNSS hardware info and a live location track on a map (plugin)", needs="connection")


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def band(mhz: float) -> str:
    """Name of the GNSS frequency band for a carrier frequency in MHz."""
    if not mhz:
        return ""
    for lo, hi, name in ((1559, 1592, "L1/E1/B1"), (1592, 1610, "G1"), (1164, 1189, "L5/E5a/B2a"),
                         (1189, 1214, "E5b/B2I"), (1215, 1240, "L2"), (1240, 1256, "G2"), (1256, 1273, "B3"),
                         (1273, 1290, "E6/L6")):
        if lo <= mhz < hi:
            return name
    return f"{mhz:.0f} MHz"


def haversine(a: dict, b: dict) -> float:
    r = 6371008.8
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# ---------------------------------------------------------------------------------------------- device side
class Device:
    def __init__(self, api):
        self.api = api
        self.conn = api.conn
        self.jar = api.read_file("gpstest.jar")
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
            raise RuntimeError(f"The connection runs as an app (uid {self.user_uid}), which Android does not let use "
                               "location this way. Turn Root on, or start Shizuku on the device so the session runs "
                               "as the shell user, then try again.")
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
            tmp = os.path.join(self.api.data_dir, "gpstest.jar")
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

    def command(self, args: str, system: bool) -> tuple[str, bool]:
        """Shell command + whether it goes through a root channel. system=True runs as uid 1000 (needs root)."""
        base = f"CLASSPATH={JAR_REMOTE} exec app_process / GpsTest {args}"
        if system:
            return f"su 1000 -c {shq(base)}", True
        if self.user_uid0 or self.via_root:
            return f"su 2000 -c {shq(base)}", self.via_root
        return base, False

    def run(self, args: str, system: bool, timeout: float = 60) -> tuple[int, str, str]:
        cmd, as_root = self.command(args, system)
        rc, out, err = self.conn.exec(cmd, timeout=timeout, as_root=as_root)
        return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def open_stream(self, args: str, system: bool):
        cmd, as_root = self.command(args, system)
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
        pat = shq("GpsTest track .* " + token + "$")
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
    hal = []
    for ln in sec.get("HAL", []):
        name = ln.split(":")[0].strip()
        if name and name not in hal and re.search(r"gnss|gps", name, re.I):
            hal.append(name)
    files = sorted(set(sec.get("FILES", [])))
    stack = []
    # match whole config-file names and property *names* only ("utilization" must not look like "izat")
    names = [f.lower() for f in files]
    names += [m.group(1).lower() for m in (re.match(r"\[([^\]]+)\]", x) for x in sec.get("PROPS", [])) if m]
    for pat, label in ((r"(^|[._])izat([._]|$)", "Qualcomm IZat"), (r"(^|[._])lowi([._]|$)", "Qualcomm LOWI"),
                       (r"^xtra|[._]xtra([._]|$)", "Qualcomm XTRA assistance"), (r"mnld|^mnl_|mnl_config", "MediaTek MNL"),
                       (r"(^|[._])agps", "A-GPS config"), (r"glgps|gpsconfig\.xml", "Broadcom GLGPS"),
                       (r"bcm47", "Broadcom BCM47xx"), (r"(^|[._])lhd([._]|$)", "Broadcom LHD"),
                       (r"sirf", "SiRF"), (r"ublox|u-blox", "u-blox")):
        if any(re.search(pat, n) for n in names) and label not in stack:
            stack.append(label)
    return {"hal": hal, "files": files, "stack": stack, "props": sec.get("PROPS", [])[:40],
            "dump": sec.get("DUMP", [])}


def detect(dev: Device, mode: str) -> tuple[dict, bool]:
    """Returns (data, use_system)."""
    dev.ensure_jar()
    system = mode == "system" or (mode == "auto" and dev.root_ok())
    tries = [system, False] if system and mode == "auto" else [system]
    data, used, last = None, False, ""
    for sysm in tries:
        rc, out, err = dev.run("list", sysm)
        start = out.find("{")
        if rc == 0 and start >= 0:
            data, used = json.loads(out[start:]), sysm
            break
        last = (err.strip() or out.strip() or f"exit code {rc}")[-1500:]
    if data is None:
        raise RuntimeError(last)
    hw = {}
    for root in ([False, True] if dev.root_ok() else [False]):
        try:
            rc, out, _ = dev.conn.exec(HW_SCRIPT, timeout=60, as_root=root or dev.via_root)
            hw = parse_hw(out.decode("utf-8", "replace"))
            if hw.get("hal"):
                break
        except Exception:  # noqa: BLE001
            pass
    data["hw"] = hw
    return data, used


# ---------------------------------------------------------------------------------------------- tracking
class Tracker(QObject):
    info = Signal(dict)
    fix = Signal(dict)
    sats = Signal(list)
    first_fix = Signal(int)
    note = Signal(str)
    nofix = Signal()
    failed = Signal(str)
    ended = Signal()

    def __init__(self, dev: Device, args: str, system: bool, prev_token: str = ""):
        super().__init__()
        self.dev, self.args, self.system, self.prev_token = dev, args, system, prev_token
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
        started = False
        try:
            if self.prev_token:
                self.dev.kill_leftovers(self.prev_token)
            read, self._stopfn, stderr = self.dev.open_stream(self.args, self.system)
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
                    ln = raw.decode("utf-8", "replace").strip()
                    kind, _, rest = ln.partition(" ")
                    if kind == "L":
                        self.fix.emit(json.loads(rest))
                    elif kind == "S":
                        self.sats.emit(json.loads(rest).get("sats", []))
                    elif kind == "I":
                        started = True
                        self.info.emit(json.loads(rest))
                    elif kind == "N":
                        self.nofix.emit()
                    elif kind == "F":
                        self.first_fix.emit(int(rest))
                    elif kind == "P":
                        self.note.emit("Location provider turned " + rest)
                    elif kind == "W":
                        self.note.emit(rest)
                    elif kind == "E":
                        raise RuntimeError(rest)
            if not self._stop.is_set():
                raise RuntimeError(stderr().strip()[-1500:] or ("The helper stopped." if started else
                                                                 "The helper did not start."))
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


# ---------------------------------------------------------------------------------------------- map
class TileLoader(QObject):
    ready = Signal(tuple)

    def __init__(self, cache_dir: str):
        super().__init__()
        self.cache_dir = cache_dir
        self.mem: OrderedDict[tuple, QImage] = OrderedDict()
        self.pending: set[tuple] = set()
        self.failed: dict[tuple, float] = {}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)    # OSM asks for few connections
        self.ready.connect(self._got)
        self._bytes: dict[tuple, bytes] = {}
        self.lock = threading.Lock()

    def path(self, k):
        z, x, y = k
        return os.path.join(self.cache_dir, str(z), str(x), f"{y}.png")

    def get(self, k) -> QImage | None:
        img = self.mem.get(k)
        if img is not None:
            self.mem.move_to_end(k)
            return img
        p = self.path(k)
        if os.path.isfile(p) and time.time() - os.path.getmtime(p) < 30 * 86400:
            img = QImage(p)
            if not img.isNull():
                self._remember(k, img)
                return img
        return None

    def request(self, k):
        if k in self.pending or time.time() - self.failed.get(k, 0) < 60:
            return
        self.pending.add(k)
        self.pool.submit(self._fetch, k)

    def _fetch(self, k):
        z, x, y = k
        try:
            req = urllib.request.Request(TILE_URL.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            p = self.path(k)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
            with self.lock:
                self._bytes[k] = data
        except Exception:  # noqa: BLE001
            with self.lock:
                self._bytes[k] = b""
        self.ready.emit(k)

    def _got(self, k):
        self.pending.discard(k)
        with self.lock:
            data = self._bytes.pop(k, b"")
        img = QImage.fromData(data) if data else QImage()
        if img.isNull():
            self.failed[k] = time.time()
        else:
            self._remember(k, img)

    def _remember(self, k, img):
        self.mem[k] = img
        self.mem.move_to_end(k)
        while len(self.mem) > 300:
            self.mem.popitem(last=False)


class MapView(QWidget):
    picked = Signal(int)
    user_moved = Signal()

    def __init__(self, theme, tiles: TileLoader):
        super().__init__()
        self.theme = theme
        self.tiles = tiles
        tiles.ready.connect(lambda _k: self.update())
        self.online = True
        self.z = 3
        self.cx, self.cy = self.world(20.0, 0.0)
        self.points: list[dict] = []
        self.selected = -1
        self.drag = None
        self.setMouseTracking(True)
        self.setMinimumSize(420, 300)
        self.message = ""

    def sizeHint(self):
        return QSize(800, 520)

    # web mercator at the current zoom
    def world(self, lat, lon, z=None):
        n = 256 * 2 ** (self.z if z is None else z)
        lat = max(-85.05112878, min(85.05112878, lat))
        x = (lon + 180) / 360 * n
        s = math.sin(math.radians(lat))
        y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
        return x, y

    def to_screen(self, lat, lon):
        x, y = self.world(lat, lon)
        return QPointF(x - self.cx + self.width() / 2, y - self.cy + self.height() / 2)

    def to_latlon(self, sx, sy):
        n = 256 * 2 ** self.z
        x = sx - self.width() / 2 + self.cx
        y = sy - self.height() / 2 + self.cy
        lon = x / n * 360 - 180
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
        return lat, lon

    def center_on(self, lat, lon, z=None):
        if z is not None:
            self.z = max(2, min(MAX_ZOOM, z))
        self.cx, self.cy = self.world(lat, lon)
        self.update()

    def fit(self):
        if not self.points:
            return
        lats = [p["lat"] for p in self.points]
        lons = [p["lon"] for p in self.points]
        clat, clon = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
        for z in range(MAX_ZOOM, 1, -1):
            x0, y0 = self.world(max(lats), min(lons), z)
            x1, y1 = self.world(min(lats), max(lons), z)
            if x1 - x0 < self.width() - 80 and y1 - y0 < self.height() - 80:
                break
        self.center_on(clat, clon, z)

    def zoom_by(self, d, anchor=None):
        nz = max(2, min(MAX_ZOOM, self.z + d))
        if nz == self.z:
            return
        ax, ay = anchor if anchor else (self.width() / 2, self.height() / 2)
        lat, lon = self.to_latlon(ax, ay)
        self.z = nz
        wx, wy = self.world(lat, lon)
        self.cx = wx - (ax - self.width() / 2)
        self.cy = wy - (ay - self.height() / 2)
        self.update()

    def meters_per_px(self, lat):
        return 156543.03392 * math.cos(math.radians(lat)) / 2 ** self.z

    # events
    def wheelEvent(self, e):
        self.zoom_by(1 if e.angleDelta().y() > 0 else -1, (e.position().x(), e.position().y()))
        self.user_moved.emit()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.drag = (e.position().x(), e.position().y(), self.cx, self.cy, False)

    def mouseMoveEvent(self, e):
        if self.drag:
            x0, y0, cx, cy, _moved = self.drag
            dx, dy = e.position().x() - x0, e.position().y() - y0
            if abs(dx) + abs(dy) > 3:
                self.drag = (x0, y0, cx, cy, True)
                self.cx, self.cy = cx - dx, cy - dy
                self.update()
                self.user_moved.emit()
        else:
            i = self.nearest(e.position())
            if i >= 0:
                p = self.points[i]
                self.setToolTip(self.describe(i, p))
            else:
                self.setToolTip("")

    def mouseReleaseEvent(self, e):
        if self.drag and not self.drag[4]:
            i = self.nearest(e.position())
            if i >= 0:
                self.selected = i
                self.picked.emit(i)
                self.update()
        self.drag = None

    def mouseDoubleClickEvent(self, e):
        self.zoom_by(1, (e.position().x(), e.position().y()))
        self.user_moved.emit()

    def nearest(self, pos) -> int:
        best, bi = 12.0, -1
        for i, p in enumerate(self.points):
            q = self.to_screen(p["lat"], p["lon"])
            d = math.hypot(q.x() - pos.x(), q.y() - pos.y())
            if d < best:
                best, bi = d, i
        return bi

    @staticmethod
    def describe(i, p):
        t = time.strftime("%H:%M:%S", time.localtime(p["time"] / 1000))
        s = f"#{i + 1} at {t}\n{p['lat']:.6f}, {p['lon']:.6f}"
        if "acc" in p:
            s += f"\n±{p['acc']:.1f} m"
        if "alt" in p:
            s += f"  alt {p['alt']:.0f} m"
        return s

    def paintEvent(self, _e):
        p = QPainter(self)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#dfe3e6" if self.online else "#101418"))
        tz = min(self.z, TILE_MAX_ZOOM)          # beyond the tile server's zoom, enlarge its deepest tiles
        ts = 256 * 2 ** (self.z - tz)
        n = 2 ** tz
        x0, y0 = self.cx - W / 2, self.cy - H / 2
        if self.online:
            tx0, ty0 = int(x0 // ts), int(y0 // ts)
            tx1, ty1 = int((x0 + W) // ts), int((y0 + H) // ts)
            for ty in range(ty0, ty1 + 1):
                if ty < 0 or ty >= n:
                    continue
                for tx in range(tx0, tx1 + 1):
                    k = (tz, tx % n, ty)
                    dest = QRectF(tx * ts - x0, ty * ts - y0, ts, ts)
                    img = self.tiles.get(k)
                    if img is not None:
                        p.drawImage(dest, img)
                        continue
                    self.tiles.request(k)
                    # meanwhile show the enlarged parent tile if we have it
                    if tz > 0:
                        pk = (tz - 1, (tx % n) // 2, ty // 2)
                        pimg = self.tiles.get(pk)
                        if pimg is not None:
                            src = QRectF((tx % 2) * 128, (ty % 2) * 128, 128, 128)
                            p.drawImage(dest, pimg, src)
        else:
            self.draw_grid(p)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pts = [self.to_screen(q["lat"], q["lon"]) for q in self.points]
        cnt = len(pts)
        if cnt >= 2:
            for i in range(1, cnt):              # line segments coloured old (blue) -> new (red)
                f = i / (cnt - 1)
                col = QColor.fromHsvF(0.62 * (1 - f), 0.85, 0.9)
                p.setPen(QPen(col, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(pts[i - 1], pts[i])
        for i, q in enumerate(pts):
            f = i / (cnt - 1) if cnt > 1 else 1.0
            col = QColor.fromHsvF(0.62 * (1 - f), 0.85, 0.9)
            last = i == cnt - 1
            if last and "acc" in self.points[i]:
                r = self.points[i]["acc"] / max(1e-6, self.meters_per_px(self.points[i]["lat"]))
                if 3 < r < 4000:
                    p.setPen(QPen(QColor(229, 72, 77, 160), 1))
                    p.setBrush(QColor(229, 72, 77, 40))
                    p.drawEllipse(q, r, r)
            p.setPen(QPen(QColor("#ffffff"), 1.5))
            p.setBrush(col)
            p.drawEllipse(q, 7 if last else 4.5, 7 if last else 4.5)
            if i == self.selected:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#000000"), 2))
                p.drawEllipse(q, 10, 10)
        if cnt:
            f = QFont(self.font())
            f.setPointSizeF(8)
            f.setBold(True)
            p.setFont(f)
            for i in ({0, cnt - 1} if cnt > 1 else {0}):
                label = "start" if i == 0 and cnt > 1 else f"#{i + 1}"
                pos = pts[i] + QPointF(9, -9)
                path = QPainterPath()
                path.addText(pos, f, label)
                p.setPen(QPen(QColor("#ffffff"), 3))
                p.drawPath(path)
                p.fillPath(path, QColor("#111111"))
        self.draw_scale(p)
        p.setPen(QColor("#333333") if self.online else QColor("#aaaaaa"))
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)
        if self.online:
            r = QRectF(W - 190, H - 16, 188, 15)
            p.fillRect(r, QColor(255, 255, 255, 190))
            p.drawText(r, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "© OpenStreetMap contributors ")
        if self.message:
            r = QRectF(0, 6, W, 22)
            p.fillRect(QRectF(W / 2 - 220, 4, 440, 24), QColor(0, 0, 0, 150))
            p.setPen(QColor("#ffffff"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.message)

    def draw_scale(self, p):
        lat, _ = self.to_latlon(self.width() / 2, self.height() / 2)
        mpp = self.meters_per_px(lat)
        target = mpp * 110
        mag = 10 ** math.floor(math.log10(target)) if target > 0 else 1
        for m in (5, 2, 1):
            if m * mag <= target:
                length = m * mag
                break
        else:
            length = mag
        px = length / mpp
        x, y = 12, self.height() - 14
        p.setPen(QPen(QColor("#000000" if self.online else "#cccccc"), 2))
        p.drawLine(QPointF(x, y), QPointF(x + px, y))
        p.drawLine(QPointF(x, y - 4), QPointF(x, y))
        p.drawLine(QPointF(x + px, y - 4), QPointF(x + px, y))
        p.drawText(QPointF(x + 2, y - 5), f"{length / 1000:g} km" if length >= 1000 else f"{length:g} m")

    def draw_grid(self, p):
        W, H = self.width(), self.height()
        lat0, lon0 = self.to_latlon(0, H)
        lat1, lon1 = self.to_latlon(W, 0)
        span = max(lon1 - lon0, 1e-9)
        step = 10 ** math.floor(math.log10(span / 4))
        if span / step > 12:
            step *= 5
        p.setPen(QPen(QColor("#2a333c"), 1))
        f = QFont(self.font())
        f.setPointSizeF(7.5)
        p.setFont(f)
        lon = math.floor(lon0 / step) * step
        while lon <= lon1:
            q = self.to_screen(lat0, lon)
            p.setPen(QPen(QColor("#2a333c"), 1))
            p.drawLine(QPointF(q.x(), 0), QPointF(q.x(), H))
            p.setPen(QColor("#6b7680"))
            p.drawText(QPointF(q.x() + 2, 12), f"{lon:.{max(0, -int(math.floor(math.log10(step))))}f}")
            lon += step
        lat = math.floor(lat0 / step) * step
        while lat <= lat1:
            q = self.to_screen(lat, lon0)
            p.setPen(QPen(QColor("#2a333c"), 1))
            p.drawLine(QPointF(0, q.y()), QPointF(W, q.y()))
            p.setPen(QColor("#6b7680"))
            p.drawText(QPointF(2, q.y() - 2), f"{lat:.{max(0, -int(math.floor(math.log10(step))))}f}")
            lat += step


class SkyPlot(QWidget):
    def __init__(self):
        super().__init__()
        self.sats: list = []
        self.setMinimumSize(200, 200)

    def sizeHint(self):
        return QSize(230, 230)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = min(self.width(), self.height()) - 20
        cx, cy, r = self.width() / 2, self.height() / 2, s / 2
        p.setPen(QPen(QColor("#3a444e"), 1))
        p.setBrush(QColor("#161c22"))
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for el in (30, 60):
            rr = r * (90 - el) / 90
            p.drawEllipse(QPointF(cx, cy), rr, rr)
        p.drawLine(QPointF(cx - r, cy), QPointF(cx + r, cy))
        p.drawLine(QPointF(cx, cy - r), QPointF(cx, cy + r))
        f = QFont(self.font())
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.setPen(QColor("#9aa4ad"))
        p.drawText(QRectF(cx - 10, cy - r - 13, 20, 12), Qt.AlignmentFlag.AlignCenter, "N")
        for sat in self.sats:
            const, svid, cn0, used, el, az = sat[:6]
            if el < 0 or (el == 0 and az == 0):
                continue
            rr = r * (90 - el) / 90
            x = cx + rr * math.sin(math.radians(az))
            y = cy - rr * math.cos(math.radians(az))
            col = QColor(CONST_COLORS.get(const, "#666666"))
            size = 3 + max(0.0, min(1.0, (cn0 - 10) / 35)) * 5
            p.setPen(QPen(col, 1.5))
            p.setBrush(col if used else Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(x, y), size, size)
        if not self.sats:
            p.setPen(QColor("#6b7680"))
            p.drawText(QRectF(0, cy + r * 0.35, self.width(), 16), Qt.AlignmentFlag.AlignCenter, "no satellite data")


# ---------------------------------------------------------------------------------------------- window
class GpsDialog(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"GPS & location – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1320, 820)
        self.dev = Device(api)
        self.data: dict = {}
        self.system = False
        self.tracker: Tracker | None = None
        self.token = ""
        self.points: list[dict] = []
        self.t_started = 0.0
        self.ttff = None
        self.bands_seen: set[str] = set()
        self.consts_seen: set[str] = set()
        self.last_sats: list = []
        self.last_fix_mono = 0.0
        self.missed = 0

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Run as"))
        self.mode = QComboBox()
        for k, label in MODES:
            self.mode.addItem(label, k)
        self.mode.setCurrentIndex([m[0] for m in MODES].index(api.get_setting("mode", "auto")))
        self.mode.currentIndexChanged.connect(lambda _i: api.set_setting("mode", self.mode.currentData()))
        top.addWidget(self.mode)
        self.b_scan = QPushButton(api.icon("refresh"), " Read hardware info")
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
        self.tree.setHeaderLabels(["Item", "Value"])
        self.tree.setColumnWidth(0, 190)
        ll.addWidget(self.tree, 3)
        self.sky = SkyPlot()
        ll.addWidget(self.sky, 2)
        self.sat_lbl = QLabel("")
        self.sat_lbl.setWordWrap(True)
        self.sat_lbl.setTextFormat(Qt.TextFormat.RichText)
        ll.addWidget(self.sat_lbl)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.tiles = TileLoader(os.path.join(api.data_dir, "tiles"))
        self.map = MapView(api.theme, self.tiles)
        self.map.online = api.get_setting("map", "osm") == "osm"
        self.map.picked.connect(self.on_pick)
        self.map.user_moved.connect(lambda: self.follow.setChecked(False))
        rsplit = QSplitter(Qt.Orientation.Vertical)
        rsplit.addWidget(self.map)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Provider"))
        self.provider = QComboBox()
        row.addWidget(self.provider)
        row.addWidget(QLabel("Every"))
        self.interval = QSpinBox()
        self.interval.setRange(1, 3600)
        self.interval.setSuffix(" s")
        self.interval.setValue(int(api.get_setting("interval", 10)))
        row.addWidget(self.interval)
        self.b_start = QPushButton("Start")
        self.b_start.clicked.connect(self.toggle)
        row.addWidget(self.b_start)
        self.follow = QCheckBox("Follow")
        self.follow.setChecked(True)
        row.addWidget(self.follow)
        for label, fn in (("+", lambda: self.map.zoom_by(1)), ("−", lambda: self.map.zoom_by(-1)),
                          ("Fit track", self.map.fit)):
            b = QPushButton(label)
            if len(label) == 1:
                b.setFixedWidth(28)
            b.clicked.connect(fn)
            row.addWidget(b)
        self.mapsel = QComboBox()
        self.mapsel.addItem("OpenStreetMap", "osm")
        self.mapsel.addItem("Grid only (offline)", "grid")
        self.mapsel.setCurrentIndex(0 if self.map.online else 1)
        self.mapsel.currentIndexChanged.connect(self.on_mapsel)
        row.addWidget(self.mapsel)
        b_clear = QPushButton("Clear track")
        b_clear.clicked.connect(self.clear_track)
        row.addWidget(b_clear)
        b_gpx = QPushButton(api.icon("download"), " GPX")
        b_gpx.clicked.connect(lambda: self.export("gpx"))
        row.addWidget(b_gpx)
        b_csv = QPushButton(api.icon("download"), " CSV")
        b_csv.clicked.connect(lambda: self.export("csv"))
        row.addWidget(b_csv)
        row.addStretch(1)
        bl.addLayout(row)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.RichText)
        bl.addWidget(self.status)
        self.table = QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels(["#", "Time", "Latitude", "Longitude", "± m", "Alt m", "Speed km/h",
                                              "Sats", "Provider", "Step m", "Total m"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.on_table_select)
        bl.addWidget(self.table, 1)
        rsplit.addWidget(bottom)
        rsplit.setSizes([520, 260])
        rl.addWidget(rsplit)
        split.addWidget(right)
        split.setSizes([360, 960])
        lay.addWidget(split, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.scan()

    # ---- hardware info
    def scan(self):
        self.b_scan.setEnabled(False)
        self.summary.setText("Reading GNSS hardware…")
        mode = self.mode.currentData()

        def done(res):
            try:
                self.b_scan.setEnabled(True)
                self.fill(*res)
            except RuntimeError:
                pass

        def fail(msg):
            try:
                self.b_scan.setEnabled(True)
                self.summary.setText("Failed")
                QMessageBox.warning(self, "GPS", f"Could not read the location hardware:\n\n{msg}")
            except RuntimeError:
                pass

        self.api.task(lambda: detect(self.dev, mode), done, fail)

    def fill(self, data: dict, system: bool):
        self.data, self.system = data, system
        self.tree.clear()

        def group(title, expanded=True):
            g = QTreeWidgetItem([title])
            f = g.font(0)
            f.setBold(True)
            g.setFont(0, f)
            self.tree.addTopLevelItem(g)
            g.setExpanded(expanded)
            return g

        def add(parent, k, v, tip=""):
            it = QTreeWidgetItem(parent, [k, str(v)])
            if tip:
                it.setToolTip(1, tip)
            return it

        hw = data.get("hw", {})
        g = group("GNSS hardware")
        model = (data.get("gnss_model") or "").strip(";")
        if model:
            parts = [x for x in model.split(";") if x]
            add(g, "Chip / firmware", parts[0] + (f" – {' '.join(parts[1:])}" if len(parts) > 1 else ""), model)
        add(g, "Hardware year", data.get("gnss_year") or "not reported")
        if hw.get("stack"):
            add(g, "Vendor GNSS stack", ", ".join(hw["stack"]))
        for h in hw.get("hal", []):
            add(g, "HAL / service", h)
        for ln in hw.get("dump", []):
            k, _, v = ln.partition(":")
            if v.strip():
                add(g, k.strip(), v.strip())
        self.bands_item = add(g, "Frequency bands seen", "start tracking to see")
        self.consts_item = add(g, "Satellite systems seen", "start tracking to see")
        self.ttff_item = add(g, "Time to first fix", "—")
        caps = data.get("capabilities", {})
        if caps:
            on = sorted(k for k, v in caps.items() if v is True)
            off = sorted(k for k, v in caps.items() if v is False)
            c = group(f"GNSS capabilities ({len(on)} of {len(on) + len(off)})", expanded=False)
            for k in on:
                add(c, k, "yes")
            for k in off:
                it = add(c, k, "no")
                for col in range(2):
                    it.setForeground(col, QColor(self.api.theme.TEXT_FAINT))
        pv = group("Location providers")
        self.provider.clear()
        for prov in data.get("providers", []):
            name = prov["name"]
            reqs = [r for r in ("satellite", "network", "cell") if prov.get(r)]
            val = ("on" if prov.get("enabled") else "OFF") + (f" · {prov.get('accuracy')} · power {prov.get('power')}"
                                                           if "accuracy" in prov else "")
            it = add(pv, name, val)
            if reqs:
                add(it, "needs", ", ".join(reqs))
            last = prov.get("last")
            if last:
                age = last.get("age_ms", 0) / 1000
                add(it, "last fix", f"{last['lat']:.5f}, {last['lon']:.5f} ±{last.get('acc', 0):.0f} m, "
                                    f"{self.age_text(age)} ago")
            if name != "passive":
                self.provider.addItem(name, name)
        want = self.api.get_setting("provider", "gps")
        i = self.provider.findData(want)
        self.provider.setCurrentIndex(i if i >= 0 else 0)
        if hw.get("files"):
            fg = group(f"Vendor GNSS files ({len(hw['files'])})", expanded=False)
            for f in hw["files"]:
                add(fg, f, "")
        if hw.get("props"):
            pg = group(f"GNSS properties ({len(hw['props'])})", expanded=False)
            for ln in hw["props"]:
                m = re.match(r"\[(.*?)\]: \[(.*)\]", ln)
                if m:
                    add(pg, m.group(1), m.group(2))
        en = data.get("location_enabled")
        txt = f"{data.get('model', '')} · Android API {data.get('sdk')} · location {'ON' if en else 'OFF'}"
        txt += " · as system user" if system else " · as shell user"
        self.summary.setText(txt)
        if en is False:
            self.status.setText("<span style='color:#e5484d'><b>Location is turned off on the device</b> – turn it on "
                                "in the device's quick settings, then press Read hardware info.</span>")
        elif not system:
            self.status.setText("Running as the shell user: Android treats it as a background caller and throttles "
                                "location (fixes may arrive minutes apart, no satellite data). Turn Root on for "
                                "fixes every few seconds.")
        # centre the map on the freshest known fix
        lasts = [p["last"] for p in data.get("providers", []) if p.get("last")]
        if lasts and not self.points:
            best = min(lasts, key=lambda x: x.get("age_ms", 1e18))
            self.map.center_on(best["lat"], best["lon"], 15)

    @staticmethod
    def age_text(s: float) -> str:
        if s < 90:
            return f"{s:.0f} s"
        if s < 5400:
            return f"{s / 60:.0f} min"
        if s < 172800:
            return f"{s / 3600:.1f} h"
        return f"{s / 86400:.0f} days"

    # ---- tracking
    def toggle(self):
        self.stop() if self.tracker else self.start()

    def start(self):
        prov = self.provider.currentData()
        if not prov:
            return
        prev = self.token if self.tracker else ""
        self.stop()
        self.api.set_setting("provider", prov)
        self.api.set_setting("interval", self.interval.value())
        self.token = f"t{os.getpid()}x{int(time.time() * 1000) % 10**9}"
        t = Tracker(self.dev, f"track {prov} {self.interval.value() * 1000} {self.token}", self.system, prev)
        t.fix.connect(self.on_fix)
        t.sats.connect(self.on_sats)
        t.first_fix.connect(self.on_ttff)
        t.info.connect(self.on_info)
        t.note.connect(lambda m: self.api.message(f"GPS: {m}"))
        t.nofix.connect(self.on_nofix)
        t.failed.connect(lambda m, t=t: self.on_failed(t, m))
        t.ended.connect(lambda t=t: self.on_ended(t))
        self.tracker = t
        self.t_started = time.monotonic()
        self.ttff = None
        self.last_fix_mono = 0.0
        self.missed = 0
        self.b_start.setText("Stop")
        self.status.setText(f"Starting {prov} – a fix is requested every {self.interval.value()} s…")
        t.start()

    def stop(self):
        t, self.tracker = self.tracker, None
        if t:
            t.stop()
            dev, token = self.dev, self.token
            threading.Thread(target=lambda: (time.sleep(1.5), dev.kill_leftovers(token)), daemon=True).start()
            self.status.setText(f"Stopped. {len(self.points)} fix(es) logged.")
        self.b_start.setText("Start")
        self.map.message = ""
        self.map.update()

    def on_info(self, info: dict):
        if not info.get("enabled", True):
            self.status.setText(f"<span style='color:#e5484d'><b>The {info.get('provider')} provider is turned off.</b>"
                                " Turn location on in the device's settings.</span>")

    def on_fix(self, loc: dict):
        prev = self.points[-1] if self.points else None
        step = haversine(prev, loc) if prev else 0.0
        total = (prev.get("_total", 0.0) if prev else 0.0) + step
        loc["_step"], loc["_total"] = step, total
        self.points.append(loc)
        self.last_fix_mono = time.monotonic()
        self.map.points = self.points
        if self.follow.isChecked():
            z = self.map.z if len(self.points) > 1 else max(self.map.z, 17)
            self.map.center_on(loc["lat"], loc["lon"], z)
        else:
            self.map.update()
        r = self.table.rowCount()
        self.table.insertRow(r)
        vals = [str(r + 1), time.strftime("%H:%M:%S", time.localtime(loc["time"] / 1000)),
                f"{loc['lat']:.6f}", f"{loc['lon']:.6f}", f"{loc['acc']:.1f}" if "acc" in loc else "",
                f"{loc['alt']:.0f}" if "alt" in loc else "", f"{loc['speed'] * 3.6:.1f}" if "speed" in loc else "",
                str(loc.get("sats_used", "")), loc.get("provider", ""), f"{step:.1f}" if prev else "",
                f"{total:.0f}"]
        for c, v in enumerate(vals):
            it = QTableWidgetItem(v)
            if c != 8:
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(r, c, it)
        self.table.scrollToBottom()

    def on_nofix(self):
        self.missed += 1

    def on_sats(self, sats: list):
        self.last_sats = sats
        self.sky.sats = sats
        self.sky.update()
        per: dict[str, list[int]] = {}
        for s in sats:
            const, used, mhz = s[0], s[3], s[6] if len(s) > 6 else 0
            per.setdefault(const, [0, 0])
            per[const][0] += 1
            per[const][1] += 1 if used else 0
            self.consts_seen.add(const)
            b = band(mhz)
            if b:
                self.bands_seen.add(b)
        used = sum(1 for s in sats if s[3])
        good = [s[2] for s in sats if s[2] > 0]
        top4 = sorted(good, reverse=True)[:4]
        parts = [f"<span style='color:{CONST_COLORS.get(k, '#888')}'>●</span> {k} {v[1]}/{v[0]}"
                 for k, v in sorted(per.items(), key=lambda kv: -kv[1][0])]
        self.sat_lbl.setText(f"<b>{len(sats)} in view, {used} used</b>"
                             + (f" · best C/N0 {sum(top4) / len(top4):.0f} dB-Hz (top 4)" if top4 else "")
                             + "<br>" + " &nbsp; ".join(parts) + "<br><span style='color:gray'>filled = used in fix</span>")
        try:
            if self.bands_seen:
                dual = any(b.startswith(("L5", "E5", "L2", "G2", "B3", "E6")) for b in self.bands_seen)
                self.bands_item.setText(1, ", ".join(sorted(self.bands_seen)) + ("  (multi-frequency)" if dual else ""))
            if self.consts_seen:
                self.consts_item.setText(1, ", ".join(sorted(self.consts_seen)))
        except (AttributeError, RuntimeError):
            pass

    def on_ttff(self, ms: int):
        self.ttff = ms
        try:
            self.ttff_item.setText(1, f"{ms / 1000:.1f} s")
        except (AttributeError, RuntimeError):
            pass

    def on_failed(self, t, msg):
        if self.tracker is not t:
            return
        self.status.setText(f"<span style='color:#e5484d'><b>Tracking failed:</b> {msg}</span>")

    def on_ended(self, t):
        if self.tracker is t:
            self.tracker = None
            try:
                self.b_start.setText("Start")
                self.map.message = ""
                self.map.update()
            except RuntimeError:
                pass

    def tick(self):
        if not self.tracker:
            return
        now = time.monotonic()
        if self.points and self.last_fix_mono:
            last = self.points[-1]
            ago = now - self.last_fix_mono
            txt = (f"Tracking <b>{last.get('provider')}</b> every {self.interval.value()} s · "
                   f"{len(self.points)} fix(es) · last {ago:.0f} s ago, ±{last.get('acc', 0):.1f} m · "
                   f"distance {last.get('_total', 0):.0f} m")
            if self.missed:
                txt += f" · {self.missed} interval(s) without a new fix"
            if ago > self.interval.value() * 3 + 10:
                txt += " · <span style='color:#e5484d'>no new fix – weak signal or throttled</span>"
            self.status.setText(txt)
            self.map.message = ""
        else:
            secs = now - self.t_started
            used = sum(1 for s in self.last_sats if s[3])
            self.map.message = f"Waiting for a fix… {secs:.0f} s · {len(self.last_sats)} satellites in view, {used} used"
            self.map.update()

    # ---- map / table
    def on_mapsel(self, _i):
        self.map.online = self.mapsel.currentData() == "osm"
        self.api.set_setting("map", self.mapsel.currentData())
        self.map.update()

    def on_pick(self, i: int):
        self.table.selectRow(i)

    def on_table_select(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        i = rows[0].row()
        if 0 <= i < len(self.points):
            self.map.selected = i
            p = self.points[i]
            self.follow.setChecked(False)
            self.map.center_on(p["lat"], p["lon"])

    def clear_track(self):
        self.points = []
        self.map.points = self.points
        self.map.selected = -1
        self.table.setRowCount(0)
        self.map.update()

    def export(self, kind: str):
        if not self.points:
            self.api.message("GPS: nothing logged yet")
            return
        folder = os.path.join(os.path.expanduser("~"), "Documents", "AndroUnderTheHood", "Location")
        os.makedirs(folder, exist_ok=True)
        name = re.sub(r"[^\w.-]+", "_", self.api.device_name or "device")
        default = os.path.join(folder, f"track_{name}_{time.strftime('%Y%m%d_%H%M%S')}.{kind}")
        path, _ = QFileDialog.getSaveFileName(self, "Save track", default,
                                              "GPX (*.gpx)" if kind == "gpx" else "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as f:
            if kind == "gpx":
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="AndroUnderTheHood" '
                        'xmlns="http://www.topografix.com/GPX/1/1">\n<trk><name>'
                        f'{self.api.device_name}</name><trkseg>\n')
                for p in self.points:
                    t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p["time"] / 1000))
                    f.write(f'<trkpt lat="{p["lat"]:.7f}" lon="{p["lon"]:.7f}">'
                            + (f'<ele>{p["alt"]:.1f}</ele>' if "alt" in p else "")
                            + f'<time>{t}</time>' + (f'<hdop>{p["acc"] / 5:.1f}</hdop>' if "acc" in p else "")
                            + (f'<sat>{p["sats_used"]}</sat>' if "sats_used" in p else "") + '</trkpt>\n')
                f.write("</trkseg></trk>\n</gpx>\n")
            else:
                f.write("time_utc,lat,lon,accuracy_m,alt_m,vert_accuracy_m,speed_mps,bearing,sats_used,provider,"
                        "step_m,total_m\n")
                for p in self.points:
                    t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p["time"] / 1000))
                    f.write(",".join(str(x) for x in (t, p["lat"], p["lon"], p.get("acc", ""), p.get("alt", ""),
                                                      p.get("vacc", ""), p.get("speed", ""), p.get("bearing", ""),
                                                      p.get("sats_used", ""), p.get("provider", ""),
                                                      round(p.get("_step", 0), 2), round(p.get("_total", 0), 1)))
                            + "\n")
        self.api.message(f"Saved {path}")

    def closeEvent(self, e):
        self.timer.stop()
        self.stop()
        self.tiles.pool.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(e)
