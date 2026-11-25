"""Camera test plugin.

Lists every camera the device has and streams the selected one as live JPEG frames.

On the device a small dex helper (camtest.jar, source in helper/CamTest.java) runs through app_process and
uses the Camera2 API. Detection covers:
  * the cameras returned by CameraManager.getCameraIdList()
  * "hidden" vendor sensors (ultra-wide, macro, depth…) that many phones leave out of that list: numeric ids are probed
  * physical sensors behind a logical multi-camera, streamed through their logical camera
Streaming works over SSH and ADB, as the shell user (needs nothing else) or as root.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QTransform
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QSlider, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

JAR_REMOTE = "/data/local/tmp/androunderthehood-camtest.jar"
_NOWIN = 0x08000000 if sys.platform == "win32" else 0
MODES = [("auto", "Auto (shell user, then root)"), ("shell", "Shell user"), ("root", "Root")]


def register(api):
    api.add_tool("Cameras", lambda: CameraDialog(api).show(), icon="camera",
                 tooltip="Find every camera and stream it live (plugin)", needs="connection")


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------------------------- device side
class Device:
    """Pushes the helper and builds/runs its commands on the current connection."""

    def __init__(self, api):
        self.api = api
        self.conn = api.conn
        self.jar = api.read_file("camtest.jar")
        self.user_uid0 = None          # True when even the "non-root" channel is root (native-root SSH server)
        self.user_uid = None
        self.via_root = False          # session runs as an app: reach the shell user through root

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
            tmp = os.path.join(self.api.data_dir, "camtest.jar")
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
        base = f"CLASSPATH={JAR_REMOTE} exec app_process / CamTest {args}"
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
        """Start a streaming command. Returns (read(n) -> bytes, stop(), stderr_text())."""
        cmd, as_root = self.command(args, root)
        if self.is_adb:
            full = self.conn.wrap_root(cmd) if as_root else cmd
            # exec-out is binary-clean (adb shell -T rewrites \n as \r\n on Windows)
            p = subprocess.Popen([self.conn.exe, "-s", self.conn.serial, "exec-out", full], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, creationflags=_NOWIN)
            err = bytearray()
            threading.Thread(target=lambda: [err.extend(b) for b in iter(lambda: p.stderr.read(4096), b"")],
                             daemon=True).start()
            return (lambda n: p.stdout.read1(n)), p.kill, (lambda: err.decode(errors="replace"))
        if getattr(self.conn, "use_pty", False):
            raise RuntimeError("This SSH server only allows interactive shells, which can't carry a video stream.")
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
        """Kill one run's helper if it outlived its connection (each run carries a unique token).
        Root is only used when the shell user could not kill it, to avoid a superuser toast on every stop."""
        pat = shq("CamTest stream .* " + token + "$")
        for root in (False, True):
            try:
                rc, out, _ = self.conn.exec(f"pkill -f {pat} 2>/dev/null; sleep 0.3; pgrep -f {pat} >/dev/null && echo alive",
                                            timeout=15, as_root=root)
                if b"alive" not in out:
                    return
            except Exception:  # noqa: BLE001
                pass


def list_cameras(dev: Device, mode: str, root_ok: bool) -> tuple[dict, bool]:
    """Returns (listing, used_root)."""
    dev.ensure_jar()
    tries = {"auto": [False, True] if root_ok else [False], "shell": [False], "root": [True]}[mode]
    last = ""
    for root in tries:
        rc, out, err = dev.run("list", root)
        text = out.strip()
        start = text.find("{")
        if rc == 0 and start >= 0:
            data = json.loads(text[start:])
            if data.get("cameras") or root == tries[-1]:
                return data, root
        last = (err.strip() or out.strip() or f"exit code {rc}")[-1500:]
    raise RuntimeError(last)


# ---------------------------------------------------------------------------------------------- streaming
class Streamer(QObject):
    info = Signal(dict)
    frame = Signal(QImage, int)
    failed = Signal(str)
    ended = Signal()

    def __init__(self, dev: Device, args: str, root: bool):
        super().__init__()
        self.dev, self.args, self.root = dev, args, root
        self._stop = threading.Event()
        self._stopfn = None
        self.last_jpeg = b""
        self.busy = False       # GUI still drawing the previous frame → skip decoding this one

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
        got_frame = False
        try:
            read, self._stopfn, stderr = self.dev.open_stream(self.args, self.root)
            if self._stop.is_set():
                self._stopfn()
                return
            buf = bytearray()
            while not self._stop.is_set():
                chunk = read(1 << 18)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 5:
                    t = buf[0]
                    n = struct.unpack(">I", bytes(buf[1:5]))[0]
                    if n > 64 << 20 or t not in b"IFE":
                        raise RuntimeError("Corrupt stream: " + bytes(buf[:80]).decode(errors="replace"))
                    if len(buf) < 5 + n:
                        break
                    payload = bytes(buf[5:5 + n])
                    del buf[:5 + n]
                    if t == ord("I"):
                        self.info.emit(json.loads(payload))
                    elif t == ord("E"):
                        raise RuntimeError(payload.decode(errors="replace"))
                    else:
                        got_frame = True
                        self.last_jpeg = payload
                        if not self.busy:
                            img = QImage.fromData(payload, "JPG")
                            if not img.isNull():
                                self.busy = True
                                self.frame.emit(img, n)
                        else:
                            self.frame.emit(QImage(), n)      # count it for the statistics only
            if not self._stop.is_set():
                msg = stderr().strip()
                if not got_frame:
                    raise RuntimeError(msg[-1500:] or "The helper ended without sending a picture.")
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


class Video(QWidget):
    def __init__(self, theme):
        super().__init__()
        self.img = QImage()
        self.text = "Pick a camera and press Start."
        self.theme = theme
        self.setMinimumSize(320, 240)

    def show_image(self, img: QImage):
        self.img = img
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000000"))
        if self.img.isNull():
            p.setPen(QColor(self.theme.TEXT_DIM))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.text)
            return
        s = self.img.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x, y = (self.width() - s.width()) / 2, (self.height() - s.height()) / 2
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(QRectF(x, y, s.width(), s.height()), self.img)

    def sizeHint(self):
        return QSize(800, 600)


# ---------------------------------------------------------------------------------------------- window
class CameraDialog(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"Cameras – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1150, 680)
        self.dev = Device(api)
        self.cams: dict[str, dict] = {}
        self.used_root = False
        self.streamer: Streamer | None = None
        self.token = ""
        self.stream_info: dict = {}
        self.extra_rot = int(api.get_setting("extra_rotation", 0))
        self.frames = self.bytes = 0
        self.t0 = time.monotonic()

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Run camera as"))
        self.mode = QComboBox()
        for key, label in MODES:
            self.mode.addItem(label, key)
        self.mode.setCurrentIndex(max(0, [m[0] for m in MODES].index(api.get_setting("mode", "auto"))))
        self.mode.currentIndexChanged.connect(lambda _i: api.set_setting("mode", self.mode.currentData()))
        top.addWidget(self.mode)
        self.b_scan = QPushButton(api.icon("refresh"), " Detect cameras")
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
        self.tree.setHeaderLabels(["Camera", "Facing", "MP", "Details"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 120)
        self.tree.setColumnWidth(1, 70)
        self.tree.setColumnWidth(2, 45)
        self.tree.currentItemChanged.connect(lambda *_: self.on_select())
        self.tree.itemDoubleClicked.connect(lambda *_: self.start())
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
        self.video = Video(api.theme)
        rl.addWidget(self.video, 1)
        ctl = QHBoxLayout()
        ctl.addWidget(QLabel("Size"))
        self.size = QComboBox()
        self.size.setMinimumWidth(110)
        ctl.addWidget(self.size)
        ctl.addWidget(QLabel("Quality"))
        self.quality = QSlider(Qt.Orientation.Horizontal)
        self.quality.setRange(20, 95)
        self.quality.setValue(int(api.get_setting("quality", 70)))
        self.quality.setFixedWidth(90)
        self.quality.valueChanged.connect(lambda v: api.set_setting("quality", v))
        ctl.addWidget(self.quality)
        self.b_start = QPushButton("Start")
        self.b_start.clicked.connect(self.toggle)
        ctl.addWidget(self.b_start)
        b_rot = QPushButton("Rotate 90°")
        b_rot.clicked.connect(self.rotate)
        ctl.addWidget(b_rot)
        self.mirror = QCheckBox("Mirror")
        ctl.addWidget(self.mirror)
        self.b_snap = QPushButton(api.icon("download"), " Snapshot")
        self.b_snap.clicked.connect(self.snapshot)
        self.b_snap.setEnabled(False)
        ctl.addWidget(self.b_snap)
        ctl.addStretch(1)
        self.stats = QLabel("")
        self.stats.setStyleSheet(f"color: {api.theme.TEXT_DIM};")
        ctl.addWidget(self.stats)
        rl.addLayout(ctl)
        split.addWidget(right)
        split.setSizes([380, 770])
        lay.addWidget(split, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_stats)
        self.timer.start(1000)
        self.scan()

    # ---- detection
    def scan(self):
        self.stop()
        self.b_scan.setEnabled(False)
        self.summary.setText("Detecting cameras…")
        self.tree.clear()
        mode = self.mode.currentData()
        root_ok = self.api.is_root() or bool(getattr(self.api.conn, "native_root", False))

        def done(res):
            try:
                self.b_scan.setEnabled(True)
                data, used_root = res
                self.fill(data, used_root)
            except RuntimeError:     # window closed
                pass

        def fail(msg):
            try:
                self.b_scan.setEnabled(True)
                self.summary.setText("Detection failed")
                QMessageBox.warning(self, "Cameras", f"Could not list the cameras:\n\n{msg}")
            except RuntimeError:
                pass

        self.api.task(lambda: list_cameras(self.dev, mode, root_ok), done, fail)

    def fill(self, data: dict, used_root: bool):
        self.used_root = used_root
        self.cams = {c["id"]: c for c in data.get("cameras", [])}
        for c in data.get("cameras", []):
            flags = []
            if c.get("source") == "hidden":
                flags.append("hidden")
            if c.get("source") == "physical":
                flags.append(f"physical of {c.get('parent')}")
            if c.get("logical"):
                flags.append("multi-camera")
            if not c.get("color", True):
                flags.append("depth/aux")
            if "SYSTEM_CAMERA" in c.get("capabilities", []):
                flags.append("system camera")
            if c.get("flash"):
                flags.append("flash")
            if c.get("focal_mm"):
                flags.append(f"{c['focal_mm']:.2f} mm")
            it = QTreeWidgetItem([f"Camera {c['id']}", c.get("facing", "?"), str(c.get("megapixels", "")),
                                  ", ".join(flags)])
            it.setData(0, Qt.ItemDataRole.UserRole, c["id"])
            self.tree.addTopLevelItem(it)
        n = len(self.cams)
        hidden = sum(1 for c in self.cams.values() if c.get("source") != "listed")
        txt = f"{data.get('model', '')} · Android API {data.get('sdk')} · {n} camera(s)"
        if hidden:
            txt += f" ({hidden} not in the public list)"
        txt += " · as root" if used_root else " · as shell user"
        if n == 0:
            txt += f" · legacy API reports {data.get('camera1_count', 0)}"
            if data.get("list_error"):
                txt += f" · {data['list_error']}"
        self.summary.setText(txt)
        if n:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def current(self) -> dict | None:
        it = self.tree.currentItem()
        return self.cams.get(it.data(0, Qt.ItemDataRole.UserRole)) if it else None

    def on_select(self):
        c = self.current()
        self.size.clear()
        if not c:
            self.details.setText("")
            return
        sizes = c.get("sizes", [])
        for s in sizes:
            self.size.addItem(s)
        # default: the largest size whose longer side is at most 1280 px
        pick = next((i for i, s in enumerate(sizes) if max(map(int, s.split("x"))) <= 1280), 0)
        self.size.setCurrentIndex(pick)
        lines = [f"<b>Camera {c['id']}</b> – {c.get('facing')}, sensor {c.get('pixel_array', '?')} "
                 f"({c.get('megapixels', '?')} MP, {c.get('sensor_mm', '?')} mm), rotated {c.get('orientation')}°",
                 f"Hardware level {c.get('level')}, up to {c.get('max_fps')} fps, "
                 f"autofocus {'yes' if c.get('autofocus') else 'no'}, aperture f/{c.get('aperture', '?')}",
                 "Found via " + {"listed": "the camera list", "hidden": "probing (hidden from apps)",
                                 "physical": f"logical camera {c.get('parent')}"}.get(c.get("source"), "?")]
        if c.get("physical_ids"):
            lines.append("Physical sensors: " + ", ".join(c["physical_ids"]))
        lines.append("Capabilities: " + ", ".join(x.lower() for x in c.get("capabilities", [])))
        self.details.setText("<br>".join(lines))
        if not sizes:
            self.details.setText(self.details.text() + "<br><b>No YUV output – this sensor can't be streamed.</b>")
        if "SYSTEM_CAMERA" in c.get("capabilities", []):
            extra = "<br><b>System camera:</b> reserved for the phone's own camera app. Opening it from here can fail " \
                    "and briefly restart the camera driver (all cameras disappear for a few seconds)."
            if c.get("logical") and c.get("physical_ids"):
                extra += " Its sensors are cameras " + ", ".join(c["physical_ids"]) + " – test those instead."
            self.details.setText(self.details.text() + extra)

    # ---- streaming
    def toggle(self):
        self.stop() if self.streamer else self.start()

    def start(self):
        c = self.current()
        if not c or not self.size.currentText():
            return
        self.stop()
        w, h = self.size.currentText().split("x")
        parent = c.get("parent") if c.get("source") == "physical" else "-"
        self.token = f"t{os.getpid()}x{int(time.time() * 1000) % 10**9}"
        args = f"stream {shq(c['id'])} {shq(parent)} {w} {h} {self.quality.value()} {self.token}"
        self.stream_info = {"orientation": c.get("orientation", 0), "front": c.get("facing") == "front"}
        self.mirror.setChecked(c.get("facing") == "front")
        self.video.img = QImage()
        self.video.text = f"Opening camera {c['id']}…"
        self.video.update()
        self.frames = self.bytes = 0
        self.t0 = time.monotonic()
        s = Streamer(self.dev, args, self.used_root)
        s.info.connect(self.on_info)
        s.frame.connect(self.on_frame)
        s.failed.connect(self.on_failed)
        s.ended.connect(lambda s=s: self.on_ended(s))
        self.streamer = s
        self.b_start.setText("Stop")
        s.start()

    def stop(self):
        s, self.streamer = self.streamer, None
        if s:
            s.stop()
            self.b_start.setText("Start")
            self.b_snap.setEnabled(not self.video.img.isNull())
            self.stats.setText("")
            dev, token = self.dev, self.token
            threading.Thread(target=lambda: (time.sleep(1.5), dev.kill_leftovers(token)), daemon=True).start()

    def on_info(self, info: dict):
        self.stream_info.update(info)

    def on_frame(self, img: QImage, n: int):
        s = self.streamer
        if not s:
            return
        self.frames += 1
        self.bytes += n
        if img.isNull():
            return
        # sensor orientation turns the raw frame upright; front cameras are then mirrored (selfie view)
        rot = (int(self.stream_info.get("orientation", 0)) + self.extra_rot) % 360
        if rot:
            img = img.transformed(QTransform().rotate(rot))
        if self.mirror.isChecked():
            img = img.mirrored(True, False)
        self.video.show_image(img)
        self.b_snap.setEnabled(True)
        s.busy = False

    def on_failed(self, msg: str):
        try:
            self.video.img = QImage()
            hint = "" if self.mode.currentData() == "root" else \
                "\n\nIf the camera is refused as the shell user, try “Run camera as: Root”."
            if "CAMERA_DEVICE" in msg:
                hint = ("\n\nThe camera driver failed on this configuration (common for vendor/system cameras). "
                        "It restarts by itself; wait a few seconds before trying another camera.")
            elif "unknown device" in msg:
                hint = "\n\nThe camera driver is restarting – press Detect cameras again in a few seconds."
            self.video.text = f"Camera stream failed:\n{msg}{hint}"
            self.video.update()
        except RuntimeError:
            pass

    def on_ended(self, s):
        if self.streamer is s:
            self.streamer = None
            try:
                self.b_start.setText("Start")
                self.stats.setText("")
            except RuntimeError:
                pass

    def update_stats(self):
        if not self.streamer:
            return
        dt = time.monotonic() - self.t0
        if dt >= 1:
            self.stats.setText(f"{self.stream_info.get('width', '?')}×{self.stream_info.get('height', '?')} · "
                               f"{self.frames / dt:.1f} fps · {self.bytes * 8 / dt / 1e6:.1f} Mbit/s")
            self.frames = self.bytes = 0
            self.t0 = time.monotonic()

    def rotate(self):
        self.extra_rot = (self.extra_rot + 90) % 360
        self.api.set_setting("extra_rotation", self.extra_rot)

    def snapshot(self):
        if self.video.img.isNull():
            return
        folder = os.path.join(os.path.expanduser("~"), "Pictures", "AndroUnderTheHood")
        os.makedirs(folder, exist_ok=True)
        c = self.current() or {"id": "x"}
        path = os.path.join(folder, f"camera{c['id']}_{time.strftime('%Y%m%d_%H%M%S')}.jpg")
        self.video.img.save(path, "JPG", 95)
        self.api.message(f"Saved {path}")

    def closeEvent(self, e):
        self.timer.stop()
        self.stop()
        super().closeEvent(e)
