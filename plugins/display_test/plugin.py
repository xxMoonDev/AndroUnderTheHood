"""Display & touch test - colour screens for dead/stuck pixels and banding, multi-touch coverage, refresh rate
(AndroUnderTheHood plugin). A small test app (inside this plugin) draws on the device; results come back live."""
import threading

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from . import engine as E

_win = {}
COLOURS = [("Red", "#FF0000"), ("Green", "#00FF00"), ("Blue", "#0000FF"), ("White", "#FFFFFF"),
           ("Black", "#000000"), ("Gray 50 %", "#808080")]


def register(api):
    api.add_tool("Display test", lambda: open_window(api), icon="sdcard",
                 tooltip="Dead/stuck pixels, banding, multi-touch coverage and refresh rate (plugin)",
                 needs="connection")


def unload(api):
    w = _win.pop("w", None)
    if w:
        try:
            w.close()
        except RuntimeError:
            pass


def open_window(api):
    w = _win.get("w")
    try:
        if w is not None and w.isVisible():
            w.raise_()
            return
    except RuntimeError:
        pass
    w = _win["w"] = DisplayWindow(api)
    w.show()


class Bridge(QObject):
    event = Signal(object)
    done = Signal(str, object)
    fail = Signal(str)


class Coverage(QWidget):
    """The touch grid as the device reports it (filled = touched)."""

    def __init__(self):
        super().__init__()
        self.cols, self.rows, self.bits = 9, 16, ""
        self.setMinimumSize(180, 320)

    def set(self, cols, rows, bits):
        self.cols, self.rows, self.bits = cols, rows, bits
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        w, h = self.width(), self.height()
        ratio = self.rows / self.cols
        cw = min(w, h / ratio)
        ch = cw * ratio
        x0, y0 = (w - cw) / 2, (h - ch) / 2
        sw, sh = cw / self.cols, ch / self.rows
        for i in range(self.cols * self.rows):
            r = QRectF(x0 + (i % self.cols) * sw, y0 + (i // self.cols) * sh, sw, sh)
            on = i < len(self.bits) and self.bits[i] == "1"
            p.fillRect(r.adjusted(1, 1, -1, -1), QColor("#3fb950" if on else "#2a2d32"))
        p.end()


class DisplayWindow(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"Display test – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(900, 640)
        self.b = Bridge()
        self.b.event.connect(self.on_event)
        self.b.done.connect(lambda op, r: getattr(self, "done_" + op)(r))
        self.b.fail.connect(lambda m: QMessageBox.warning(self, "Display test", m))
        self.stream = None
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        self.info_box = QGroupBox("Display")
        self.info_form = QFormLayout(self.info_box)
        left.addWidget(self.info_box)
        tests = QGroupBox("Show on the device")
        g = QGridLayout(tests)
        for i, (name, hexc) in enumerate(COLOURS):
            b = QPushButton(name)
            b.setStyleSheet(f"QPushButton {{ border-left: 14px solid {hexc}; }}")
            b.clicked.connect(lambda _=False, h=hexc: self.show_mode(f"color:{h}"))
            g.addWidget(b, i // 3, i % 3)
        for i, (name, mode, tip) in enumerate((
                ("Colour sweep", "sweep", "Solid colours; tap the screen for the next one"),
                ("Gradients", "gradient", "Black → white / red / green / blue: look for banding and colour steps"),
                ("Grid", "grid", "40 px grid: geometry and burn-in"),
                ("Pixel checker", "checker", "Fine checkerboard: sharpness and uniformity"),
                ("Touch test", "touch", "Draw with one or more fingers; coverage shows on the right"),
                ("Refresh rate", "fps", "Measures the real frame rate for a few seconds"))):
            b = QPushButton(name)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, m=mode: self.show_mode(m))
            g.addWidget(b, 2 + i // 3, i % 3)
        self.bright = QCheckBox("Full brightness while testing")
        g.addWidget(self.bright, 4, 0, 1, 3)
        sb = QPushButton("Close the test screen")
        sb.clicked.connect(lambda: self.job("stop", lambda: E.stop(self.api.conn)))
        g.addWidget(sb, 5, 0, 1, 2)
        ub = QPushButton("Remove test app")
        ub.clicked.connect(lambda: self.job("stop", lambda: E.uninstall(self.api.conn)))
        g.addWidget(ub, 5, 2)
        left.addWidget(tests)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        left.addWidget(self.log, 1)
        lay.addLayout(left, 3)
        right = QVBoxLayout()
        self.touch_lbl = QLabel("Touch: –")
        self.touch_lbl.setWordWrap(True)
        right.addWidget(self.touch_lbl)
        self.cov = Coverage()
        right.addWidget(self.cov, 1)
        self.fps_lbl = QLabel("Refresh rate: –")
        self.fps_lbl.setWordWrap(True)
        right.addWidget(self.fps_lbl)
        lay.addLayout(right, 2)
        self.job("info", lambda: E.info(self.api.conn))

    def job(self, op, fn):
        def body():
            try:
                self.b.done.emit(op, fn())
            except Exception as e:  # noqa: BLE001
                self.b.fail.emit(str(e))
        threading.Thread(target=body, daemon=True).start()

    def done_info(self, d):
        self.dinfo = d
        while self.info_form.rowCount():
            self.info_form.removeRow(0)
        for k, label in (("name", "Panel"), ("size", "Resolution"), ("override", "Override"), ("density", "Density"),
                         ("dpi", "Physical dpi"), ("refresh", "Refresh rates"), ("current_fps", "Now"),
                         ("hdr", "HDR"), ("max_nits", "Peak brightness"), ("brightness", "Brightness setting")):
            if k in d:
                v = d[k]
                if k == "refresh":
                    v = ", ".join(f"{x:g} Hz" for x in v)
                elif k == "current_fps":
                    v = f"{v:g} Hz"
                elif k == "max_nits":
                    v = f"{v:g} nits (as reported)"
                elif k == "brightness":
                    v = f"{v}" + (" (auto)" if d.get("auto") else "")
                self.info_form.addRow(label, QLabel(str(v)))
        self.info_form.addRow("Test app", QLabel("installed" if d.get("app") else "installed on first test"))

    def done_stop(self, _r):
        self.log.appendPlainText("test screen closed")

    def show_mode(self, mode):
        bright = self.bright.isChecked()

        def fn():
            if not getattr(self, "dinfo", {}).get("app"):
                E.install_app(self.api.conn, self.api.read_file)
                self.dinfo["app"] = True
            E.show(self.api.conn, mode, bright)
            return mode
        if self.stream is None:
            self.stream = E.LogStream(self.api.conn, self.b.event.emit)
        self.job("shown", fn)

    def done_shown(self, mode):
        self.log.appendPlainText(f"showing {mode} on the device"
                                 + ("  (tap the screen for the next colour)" if mode == "sweep" else ""))

    def on_event(self, ev):
        if "touch" in ev:
            cov = ev.get("cov", "")
            pct = 100 * cov.count("1") / len(cov) if cov else 0
            self.touch_lbl.setText(f"Touch: up to <b>{ev['max']}</b> finger(s) at once · {ev['events']} events · "
                                   f"~{ev['rate']} events/s · coverage <b>{pct:.0f} %</b>"
                                   + ("<br>Untouched cells may be dead zones – draw over them again."
                                      if 0 < pct < 100 else ""))
            self.cov.set(ev.get("cols", 9), ev.get("rows", 16), cov)
        elif "fps" in ev:
            jitter = ev["max_ms"] - ev["min_ms"]
            self.fps_lbl.setText(f"Refresh rate: <b>{ev['fps']:.1f} fps</b> · frame time {ev['min_ms']:.1f}–"
                                 f"{ev['max_ms']:.1f} ms" + (" · uneven (dropped frames)" if jitter > 8 else ""))
            self.log.appendPlainText(f"fps {ev['fps']:.1f} (frames {ev['frames']})")
        elif "mode" in ev:
            self.log.appendPlainText(f"device shows: {ev['mode']} ({ev.get('w')}×{ev.get('h')})")

    def closeEvent(self, e):
        if self.stream:
            self.stream.stop()
        try:
            threading.Thread(target=lambda: E.stop(self.api.conn), daemon=True).start()
        except Exception:  # noqa: BLE001
            pass
        _win.pop("w", None)
        super().closeEvent(e)
