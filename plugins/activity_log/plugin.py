"""Activity log plugin: which app is used, which screen / website it shows, and every tap on it.

A small dex helper (actlog.jar, source in helper/ActLog.java) runs on the device as the shell user. It connects to
Android's UI automation (the accessibility layer, so it works for every app) to see the foreground app, the screen
title and a browser's URL, and reads the raw touch screen with getevent to see every tap, swipe and button press
together with the element under the finger.

Two ways to run it:
  * Live: the helper streams to this window while it is open; everything is also saved to
    Documents\\AndroUnderTheHood\\ActivityLog\\<device>_<date>.jsonl
  * On the device: the helper keeps recording to a file on the device without the PC; "Fetch" brings it over.

Typed text is never logged. Keyboard key labels are only logged when "Keyboard keys" is ticked, and never while a
password field has focus.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

JAR_REMOTE = "/data/local/tmp/androunderthehood-actlog.jar"
DEV_LOG = "/data/local/tmp/androunderthehood-actlog.jsonl"
_NOWIN = 0x08000000 if sys.platform == "win32" else 0
MODES = [("auto", "Auto (shell user, then root)"), ("shell", "Shell user"), ("root", "Root")]
LOG_DIR = os.path.join(os.path.expanduser("~"), "Documents", "AndroUnderTheHood", "ActivityLog")


def register(api):
    api.add_tool("Activity log", lambda: ActivityDialog(api).show(), icon="history",
                 tooltip="Log app use, screens / websites and taps (plugin)", needs="connection")


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------------------------- device side
class Device:
    def __init__(self, api):
        self.api = api
        self.conn = api.conn
        self.jar = api.read_file("actlog.jar")
        self.user_uid = None
        self.user_uid0 = False
        self.via_root = False
        self.ready = False

    @property
    def is_adb(self) -> bool:
        return getattr(self.conn, "transport", None) is None

    def _root_ok(self) -> bool:
        try:
            return bool(self.api.is_root() or getattr(self.conn, "native_root", False))
        except Exception:  # noqa: BLE001
            return False

    def ensure_jar(self):
        if self.ready:
            return
        want = hashlib.md5(self.jar).hexdigest()
        rc, out, _ = self.conn.exec("id -u", timeout=20, as_root=False)
        try:
            self.user_uid = int(out.decode(errors="replace").split()[0])
        except (ValueError, IndexError):
            self.user_uid = -1
        self.user_uid0 = self.user_uid == 0
        # A session running as an ordinary app can't use UI automation or /dev/input: go through root and drop to
        # the shell user from there.
        self.via_root = self.user_uid not in (0, 2000)
        if self.via_root and not self._root_ok():
            raise RuntimeError(f"The connection runs as an app (uid {self.user_uid}), which Android does not let "
                               "watch other apps. Turn Root on, or start Shizuku on the device so the session runs "
                               "as the shell user, then try again.")
        rc, out, _ = self.conn.exec(f"md5sum {JAR_REMOTE} 2>/dev/null", timeout=20, as_root=self.via_root)
        if want not in out.decode(errors="replace").split():
            for root in (False, True):
                try:
                    self.conn.exec(f"rm -f {JAR_REMOTE}", timeout=20, as_root=root)
                except Exception:  # noqa: BLE001
                    pass
            if self.is_adb:
                os.makedirs(self.api.data_dir, exist_ok=True)
                tmp = os.path.join(self.api.data_dir, "actlog.jar")
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
        self.ready = True

    def wrap(self, cmd: str, root: bool) -> tuple[str, bool]:
        """Command as the wanted user + whether it needs a root channel."""
        if root:
            return cmd, True
        if self.user_uid0 or self.via_root:
            # shell user with the groups adb's shell has: input (1004) for the touch screen, inet (3003)
            return f"su -g 2000 -G 1004 -G 3003 2000 -c {shq(cmd)}", self.via_root
        return cmd, False

    def helper(self, args: str) -> str:
        return f"CLASSPATH={JAR_REMOTE} exec app_process / ActLog {args}"

    def run(self, cmd: str, root: bool, timeout: float = 60) -> tuple[int, str, str]:
        full, as_root = self.wrap(cmd, root)
        rc, out, err = self.conn.exec(full, timeout=timeout, as_root=as_root)
        return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def open_stream(self, cmd: str, root: bool):
        full, as_root = self.wrap(cmd, root)
        if self.is_adb:
            full = self.conn.wrap_root(full) if as_root else full
            p = subprocess.Popen([self.conn.exe, "-s", self.conn.serial, "exec-out", full], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, creationflags=_NOWIN)
            err = bytearray()
            threading.Thread(target=lambda: [err.extend(b) for b in iter(lambda: p.stderr.read(4096), b"")],
                             daemon=True).start()
            return (lambda n: p.stdout.read1(n)), p.kill, (lambda: err.decode(errors="replace"))
        if getattr(self.conn, "use_pty", False):
            raise RuntimeError("This SSH server only allows interactive shells, which can't carry a live stream. "
                               "Use 'Record on device' instead.")
        chan = self.conn.open_channel(as_root)
        chan.exec_command(self.conn.wrap_root(full) if as_root else full)
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

    def kill(self, pattern: str):
        pat = shq(pattern)
        # an app-uid session can't even see other users' processes, so it must go through root
        for root in ((True,) if self.via_root else (False, True)):
            try:
                rc, out, _ = self.conn.exec(f"pkill -f {pat} 2>/dev/null; sleep 0.4; pgrep -f {pat} >/dev/null && echo alive",
                                            timeout=15, as_root=root)
                if b"alive" not in out:
                    return
            except Exception:  # noqa: BLE001
                pass

    # ---- background recording on the device
    def rec_status(self) -> tuple[bool, int]:
        rc, out, _ = self.conn.exec(f"pgrep -f 'ActLog recor[d]' >/dev/null && echo RUN; wc -c < {DEV_LOG} 2>/dev/null",
                                    timeout=20, as_root=self.via_root)
        t = out.decode(errors="replace")
        m = re.search(r"^\s*(\d+)\s*$", t, re.M)
        return "RUN" in t, int(m.group(1)) if m else 0

    def rec_start(self, keys: bool, root: bool):
        self.ensure_jar()
        running, _ = self.rec_status()
        if running:
            return
        inner = self.helper(f"record {DEV_LOG}{' keys' if keys else ''} rec")
        launch = f"nohup setsid sh -c {shq(inner)} </dev/null >/dev/null 2>>{DEV_LOG}.err &"
        rc, out, err = self.run(launch, root, timeout=30)
        time.sleep(2.5)
        running, _ = self.rec_status()
        if not running:
            _, e, _ = self.run(f"tail -c 1500 {DEV_LOG}.err 2>/dev/null; tail -n 3 {DEV_LOG} 2>/dev/null", root)
            raise RuntimeError(e.strip() or err.strip() or "The recorder did not start.")

    def rec_stop(self):
        self.kill("ActLog recor[d]")

    def rec_fetch(self) -> bytes:
        rc, out, err = self.conn.exec(f"cat {DEV_LOG}", timeout=300, as_root=self.via_root)
        if rc != 0 and not out:
            raise RuntimeError(err.decode(errors="replace").strip() or "No log on the device yet.")
        return out

    def rec_clear(self):
        # truncate in place: the recorder appends (O_APPEND), so it simply continues at the new end
        self.conn.exec(f": > {DEV_LOG}", timeout=20, as_root=True if self.via_root else False)


def parse_line(raw: bytes | str) -> dict | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    raw = raw.strip()
    if not raw or raw == "K":
        return None
    if raw.startswith("{"):                  # a saved .jsonl line
        try:
            return json.loads(raw)
        except ValueError:
            return None
    tag, _, rest = raw.partition(" ")
    if tag in ("W", "E"):
        return {"k": tag, "msg": rest, "t": int(time.time() * 1000)}
    if len(tag) != 1:
        return None
    try:
        d = json.loads(rest)
    except ValueError:
        return None
    d["k"] = tag
    return d


# ---------------------------------------------------------------------------------------------- live stream
class Streamer(QObject):
    record = Signal(dict)
    failed = Signal(str)
    ended = Signal()

    def __init__(self, dev: Device, keys: bool, root: bool):
        super().__init__()
        self.dev, self.keys, self.root = dev, keys, root
        self.token = f"t{os.getpid()}x{int(time.time() * 1000) % 10**9}"
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
        dev, tok = self.dev, self.token
        threading.Thread(target=lambda: (time.sleep(1.5), dev.kill(f"ActLog watc[h].* {tok}$")), daemon=True).start()

    def _run(self):
        got = False
        try:
            self.dev.ensure_jar()
            read, self._stopfn, stderr = self.dev.open_stream(
                self.dev.helper(f"watch{' keys' if self.keys else ''} {self.token}"), self.root)
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
                    rec = parse_line(raw)
                    if rec is None:
                        continue
                    if rec["k"] == "E":
                        raise RuntimeError(rec["msg"])
                    got = True
                    self.record.emit(rec)
            if not self._stop.is_set():
                msg = stderr().strip()[-1500:]
                raise RuntimeError(msg or ("The helper stopped." if got else "The helper sent nothing."))
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


# ---------------------------------------------------------------------------------------------- log view
def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m"


def hms(t_ms: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t_ms / 1000))


class LogModel:
    """Turns records into the App > screen > taps tree and per-app usage totals."""

    def __init__(self, tree: QTreeWidget, theme):
        self.tree = tree
        self.theme = theme
        self.reset()

    def reset(self):
        self.tree.clear()
        self.records: list[dict] = []
        self.app = None            # dict: item, pkg, label, start, taps, screens
        self.screen = None         # QTreeWidgetItem
        self.sig = ""
        self.day = ""
        self.labels: dict[str, str] = {}
        self.stats: dict[str, dict] = {}
        self.last_t = 0
        self.screen_on = True
        self.last_tap = None

    def _row(self, parent, t, what, details="", color=None, bold=False):
        it = QTreeWidgetItem([hms(t) if t else "", what, details])
        if color:
            for c in range(3):
                it.setForeground(c, QBrush(QColor(color)))
        if bold:
            f = it.font(1)
            f.setBold(True)
            it.setFont(1, f)
        if parent is None:
            self.tree.addTopLevelItem(it)
        else:
            parent.addChild(it)
        return it

    def _stat(self, pkg):
        return self.stats.setdefault(pkg, {"label": self.labels.get(pkg, pkg), "time": 0.0, "opens": 0,
                                           "screens": 0, "taps": 0, "last": 0})

    def _close_app(self, t):
        a = self.app
        if not a:
            return
        if self.screen_on:
            self._stat(a["pkg"])["time"] += max(0, t - a["start"]) / 1000
        a["end"] = t
        self._update_app_item(a, t)
        self.app = None
        self.screen = None
        self.sig = ""

    def _update_app_item(self, a, now):
        dur = (a.get("end") or now) - a["start"]
        a["item"].setText(2, f"{a['screens']} screen(s) · {a['taps']} tap(s) · {fmt_dur(dur / 1000)} · {a['pkg']}")

    def _new_app(self, pkg, label, t):
        self._close_app(t)
        it = self._row(None, t, label, "", bold=True)
        self.app = {"item": it, "pkg": pkg, "label": label, "start": t, "taps": 0, "screens": 0}
        s = self._stat(pkg)
        s["label"] = label
        s["opens"] += 1
        s["last"] = t
        it.setExpanded(True)

    def add(self, rec: dict):
        self.records.append(rec)
        k, t = rec.get("k"), int(rec.get("t") or 0)
        if t:
            day = time.strftime("%A %Y-%m-%d", time.localtime(t / 1000))
            if day != self.day:
                self.day = day
                self._row(None, 0, day, "", color=self.theme.ACCENT, bold=True)
            self.last_t = t
        if k == "I":
            det = f"{rec.get('model', '')} · {rec.get('w')}×{rec.get('h')} · touch: {rec.get('touch') or 'not available'}"
            self._row(None, t, "Logging started", det, color=self.theme.TEXT_FAINT)
        elif k == "S":
            on = bool(rec.get("on"))
            if on == self.screen_on and self.records[:-1]:
                return
            if not on:
                self._close_app(t)
            self.screen_on = on
            self._row(None, t, "Screen on" if on else "Screen off", "", color=self.theme.TEXT_FAINT)
            self.app = None
            self.screen = None
            self.sig = ""
        elif k == "C":
            pkg = rec.get("pkg", "?")
            label = rec.get("app") or pkg
            self.labels[pkg] = label
            sig = f"{pkg}\x01{rec.get('title', '')}\x01{rec.get('url', '')}"
            if sig == self.sig:
                return
            self.sig = sig
            if not self.app or self.app["pkg"] != pkg:
                lt = self.last_tap
                if lt and 0 <= t - lt[0] <= 2500 and pkg not in ("com.android.systemui",):
                    lt[1].setText(1, lt[1].text(1) + f" → opened {label}")
                self._new_app(pkg, label, t)
            url, title = rec.get("url"), rec.get("title")
            act = (rec.get("act") or "").rsplit(".", 1)[-1]
            if url:
                what = f"🌐 {url}"
                det = title or act
            else:
                what = title or act or "(main screen)"
                det = act if title and act else ""
            self.screen = self._row(self.app["item"], t, what, det)
            self.screen.setExpanded(True)
            self.app["screens"] += 1
            self._stat(pkg)["screens"] += 1
            self._update_app_item(self.app, t)
        elif k in ("T", "B"):
            if not self.app:
                self._new_app("?", "(unknown app)", t)
            if not self.screen:
                self.screen = self._row(self.app["item"], t, "(screen)", "")
                self.screen.setExpanded(True)
            if k == "T":
                g = rec.get("g", "tap")
                label = rec.get("label") or ""
                what = f"{g} '{label}'" if label and g in ("tap", "long press", "click", "long click") else \
                    (f"{g} on '{label}'" if label else g)
                if rec.get("was"):
                    what += f" (was {rec['was']})"
                bits = []
                if "x" in rec:
                    pos = f"({rec['x']}, {rec['y']})"
                    if "x2" in rec:
                        pos += f" → ({rec['x2']}, {rec['y2']})"
                    bits.append(pos)
                if rec.get("ms") is not None:
                    bits.append(f"{rec['ms']} ms")
                if rec.get("kind"):
                    bits.append(rec["kind"])
                wp = rec.get("pkg")
                if wp and wp != self.app["pkg"] and rec.get("win") != "keyboard":
                    bits.append(f"in {self.labels.get(wp, wp)}")
                if rec.get("disabled"):
                    bits.append("disabled")
                row = self._row(self.screen, t, what, " · ".join(bits))
                # only real taps can open an app (not swipes that dismiss cards in Recents)
                self.last_tap = (t, row) if g in ("tap", "click", "long press", "long click") else None
                self.app["taps"] += 1
                self._stat(self.app["pkg"])["taps"] += 1
            else:
                self._row(self.screen, t, f"🔘 {rec.get('key', 'button')}", rec.get("dev", ""))
            self._update_app_item(self.app, t)
        elif k in ("W", "E"):
            self._row(None, t, "Warning" if k == "W" else "Error", rec.get("msg", ""), color=self.theme.DANGER)

    def usage(self) -> list[dict]:
        out = []
        for pkg, s in self.stats.items():
            d = dict(s, pkg=pkg)
            if self.app and self.app["pkg"] == pkg and self.screen_on:
                d["time"] += max(0, self.last_t - self.app["start"]) / 1000
            out.append(d)
        return sorted(out, key=lambda d: -d["time"])

    def as_text(self) -> str:
        lines = []

        def walk(it, depth):
            cols = [it.text(c) for c in range(3)]
            lines.append("  " * depth + "  ".join(x for x in cols if x))
            for i in range(it.childCount()):
                walk(it.child(i), depth + 1)
        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i), 0)
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------- window
class ActivityDialog(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"Activity log – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1100, 760)
        self.dev = Device(api)
        self.streamer: Streamer | None = None
        self.save_file = None
        self.usage_dirty = False

        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Run as"))
        self.mode = QComboBox()
        for k, label in MODES:
            self.mode.addItem(label, k)
        self.mode.setCurrentIndex([m[0] for m in MODES].index(api.get_setting("mode", "auto")))
        self.mode.currentIndexChanged.connect(lambda _i: api.set_setting("mode", self.mode.currentData()))
        row.addWidget(self.mode)
        self.keys = QCheckBox("Keyboard keys")
        self.keys.setToolTip("Also log which keyboard key was tapped (never in password fields). "
                             "Off: keyboard taps are logged only as 'Keyboard'.")
        self.keys.setChecked(bool(api.get_setting("keys", False)))
        self.keys.toggled.connect(lambda v: api.set_setting("keys", v))
        row.addWidget(self.keys)
        self.b_live = QPushButton("▶ Watch live")
        self.b_live.clicked.connect(self.toggle_live)
        row.addWidget(self.b_live)
        row.addSpacing(16)
        row.addWidget(QLabel("On the device:"))
        self.b_rec = QPushButton("Start recording")
        self.b_rec.setToolTip("Keep logging on the device without the PC (until it is stopped or the device restarts)")
        self.b_rec.clicked.connect(self.toggle_rec)
        row.addWidget(self.b_rec)
        self.b_fetch = QPushButton(api.icon("download"), " Fetch")
        self.b_fetch.setToolTip("Bring the device's log over and show it")
        self.b_fetch.clicked.connect(self.fetch)
        row.addWidget(self.b_fetch)
        self.b_clear_dev = QPushButton(api.icon("trash"), " Clear")
        self.b_clear_dev.setToolTip("Empty the log file on the device")
        self.b_clear_dev.clicked.connect(self.clear_device)
        row.addWidget(self.b_clear_dev)
        row.addStretch(1)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter (app, screen, website, tapped item)…")
        self.filter.textChanged.connect(self.apply_filter)
        row2.addWidget(self.filter, 1)
        self.follow = QCheckBox("Follow")
        self.follow.setChecked(True)
        row2.addWidget(self.follow)
        b = QPushButton("Expand / collapse")
        b.clicked.connect(self.toggle_expand)
        row2.addWidget(b)
        b = QPushButton("Open log…")
        b.clicked.connect(self.open_log)
        row2.addWidget(b)
        b = QPushButton("Export text…")
        b.clicked.connect(self.export_text)
        row2.addWidget(b)
        b = QPushButton("Clear view")
        b.clicked.connect(lambda: (self.model.reset(), self.fill_usage()))
        row2.addWidget(b)
        lay.addLayout(row2)

        self.tabs = QTabWidget()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Time", "Activity", "Details"])
        self.tree.setColumnWidth(0, 120)
        self.tree.setColumnWidth(1, 470)
        self.tree.setUniformRowHeights(True)
        self.tabs.addTab(self.tree, "Timeline")
        self.utree = QTreeWidget()
        self.utree.setHeaderLabels(["App", "Time on screen", "Opened", "Screens", "Taps", "Last used", "Package"])
        self.utree.setRootIsDecorated(False)
        self.utree.setSortingEnabled(True)
        self.utree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.utree, "Apps")
        self.tabs.currentChanged.connect(lambda _i: self.fill_usage())
        lay.addWidget(self.tabs, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {api.theme.TEXT_DIM};")
        lay.addWidget(self.status)
        note = QLabel("Works in every app through Android's accessibility layer. Typed text is never logged; "
                      "password fields only show as 'password field'.")
        note.setStyleSheet(f"color: {api.theme.TEXT_FAINT};")
        lay.addWidget(note)

        self.model = LogModel(self.tree, api.theme)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.check_rec()

    # ---- helpers
    def root_ok(self) -> bool:
        return self.api.is_root() or bool(getattr(self.api.conn, "native_root", False))

    def want_root(self) -> bool:
        return self.mode.currentData() == "root"

    def busy(self, on: bool):
        for b in (self.b_rec, self.b_fetch, self.b_clear_dev):
            b.setEnabled(not on)

    def safe(self, fn):
        def wrapper(*a):
            try:
                fn(*a)
            except RuntimeError:        # window already closed
                pass
        return wrapper

    # ---- live
    def toggle_live(self):
        self.stop_live() if self.streamer else self.start_live()

    def start_live(self, root: bool | None = None):
        if root is None:
            root = self.want_root()
        os.makedirs(LOG_DIR, exist_ok=True)
        if not self.save_file:
            name = re.sub(r"[^\w.-]+", "_", self.api.device_name or "device")
            path = os.path.join(LOG_DIR, f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
            self.save_file = open(path, "a", encoding="utf-8")
            self.save_path = path
        st = Streamer(self.dev, self.keys.isChecked(), root)
        st.record.connect(self.on_record)
        st.failed.connect(lambda m, st=st, root=root: self.on_failed(st, m, root))
        st.ended.connect(lambda st=st: self.on_ended(st))
        self.streamer = st
        self.b_live.setText("■ Stop")
        self.status.setText(f"Watching live{' as root' if root else ''} · saving to {self.save_path}")
        st.start()

    def stop_live(self):
        st, self.streamer = self.streamer, None
        if st:
            st.stop()
        self.b_live.setText("▶ Watch live")
        if self.save_file:
            self.save_file.close()
            self.save_file = None
            self.status.setText(f"Stopped · saved to {self.save_path}")

    def on_record(self, rec: dict):
        if self.save_file:
            self.save_file.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.save_file.flush()
        self.add_records([rec])

    def on_failed(self, st, msg, root):
        if self.streamer is not st:
            return
        if not root and self.mode.currentData() == "auto" and self.root_ok() and "Another tool" not in msg:
            self.streamer = None
            QTimer.singleShot(300, lambda: self.start_live(root=True))
            return
        self.stop_live()
        if "Another tool" in msg and self.b_rec.property("running"):
            msg = ("The device is already recording in the background, and Android allows only one logger at a "
                   "time. Press Fetch to see that log, or Stop recording to watch live.")
        self.model.add({"k": "E", "msg": msg, "t": int(time.time() * 1000)})

    def on_ended(self, st):
        if self.streamer is st:
            self.safe(self.stop_live)()

    def add_records(self, recs):
        hidden = bool(self.filter.text().strip())
        for r in recs:
            self.model.add(r)
        self.usage_dirty = True
        if hidden:
            self.apply_filter()
        if self.follow.isChecked() and self.tree.topLevelItemCount():
            last = self.tree.topLevelItem(self.tree.topLevelItemCount() - 1)
            while last.childCount() and last.isExpanded():
                last = last.child(last.childCount() - 1)
            self.tree.scrollToItem(last)

    # ---- device recording
    def check_rec(self):
        def work():
            self.dev.ensure_jar()
            return self.dev.rec_status()

        def done(res):
            running, size = res
            self.b_rec.setText("Stop recording" if running else "Start recording")
            self.b_rec.setProperty("running", running)
            if running:
                self.status.setText(f"Recording on the device ({size / 1024:.0f} KB so far). Fetch to see it.")
            elif size:
                self.status.setText(f"Not recording on the device; it holds an earlier log ({size / 1024:.0f} KB).")
        self.api.task(work, self.safe(done), self.safe(lambda m: self.status.setText(m)))

    def toggle_rec(self):
        running = bool(self.b_rec.property("running"))
        self.busy(True)
        keys, root = self.keys.isChecked(), self.want_root()

        def work():
            if running:
                self.dev.rec_stop()
            else:
                self.dev.ensure_jar()
                try:
                    self.dev.rec_start(keys, root)
                except RuntimeError:
                    if root or self.mode.currentData() != "auto" or not self.root_ok():
                        raise
                    self.dev.rec_start(keys, True)

        def done(_r):
            self.busy(False)
            self.api.message("Stopped recording on the device." if running else
                             "Recording on the device. It keeps going without the PC until you stop it or the "
                             "device restarts.")
            self.check_rec()

        def fail(msg):
            self.busy(False)
            QMessageBox.warning(self, "Activity log", f"Could not {'stop' if running else 'start'} recording:\n\n{msg}")
            self.check_rec()
        self.api.task(work, self.safe(done), self.safe(fail))

    def fetch(self):
        self.busy(True)

        def work():
            self.dev.ensure_jar()
            return self.dev.rec_fetch()

        def done(data: bytes):
            self.busy(False)
            os.makedirs(LOG_DIR, exist_ok=True)
            name = re.sub(r"[^\w.-]+", "_", self.api.device_name or "device")
            path = os.path.join(LOG_DIR, f"{name}_device_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
            recs = [r for r in (parse_line(ln) for ln in data.splitlines()) if r]
            with open(path, "w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            if self.streamer:
                self.stop_live()
            self.model.reset()
            self.add_records(recs)
            self.fill_usage()
            self.status.setText(f"Fetched {len(recs)} entries from the device · saved to {path}")
            self.check_rec()

        def fail(msg):
            self.busy(False)
            QMessageBox.warning(self, "Activity log", f"Could not fetch the device log:\n\n{msg}")
        self.api.task(work, self.safe(done), self.safe(fail))

    def clear_device(self):
        if QMessageBox.question(self, "Activity log", "Empty the log file on the device? Fetch it first if you "
                                "want to keep it.") != QMessageBox.StandardButton.Yes:
            return
        self.api.task(lambda: (self.dev.ensure_jar(), self.dev.rec_clear()),
                      self.safe(lambda _r: self.check_rec()),
                      self.safe(lambda m: QMessageBox.warning(self, "Activity log", m)))

    # ---- files / view
    def open_log(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open activity log", LOG_DIR, "Activity logs (*.jsonl);;All files (*)")
        if not path:
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            recs = [r for r in (parse_line(ln) for ln in f) if r]
        if self.streamer:
            self.stop_live()
        self.model.reset()
        self.add_records(recs)
        self.fill_usage()
        self.status.setText(f"{len(recs)} entries from {path}")

    def export_text(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "Export as text", os.path.join(
            LOG_DIR, f"activity_{time.strftime('%Y%m%d_%H%M%S')}.txt"), "Text (*.txt)")
        if not path:
            return
        text = self.model.as_text()
        text += "\nApps\n" + "\n".join(f"  {u['label']}: {fmt_dur(u['time'])}, opened {u['opens']}×, "
                                       f"{u['screens']} screen(s), {u['taps']} tap(s)" for u in self.model.usage())
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        self.api.message(f"Saved {path}")

    def toggle_expand(self):
        n = self.tree.topLevelItemCount()
        expand = not any(self.tree.topLevelItem(i).isExpanded() for i in range(n))
        self.tree.expandAll() if expand else self.tree.collapseAll()

    def apply_filter(self):
        needle = self.filter.text().strip().lower()

        def visit(it) -> bool:
            own = not needle or any(needle in it.text(c).lower() for c in range(3))
            child_hit = False
            for i in range(it.childCount()):
                child_hit = visit(it.child(i)) or child_hit
            show = own or child_hit
            it.setHidden(not show)
            if own and needle:      # a matching app / screen shows everything inside it
                for i in range(it.childCount()):
                    unhide(it.child(i))
            return show

        def unhide(it):
            it.setHidden(False)
            for i in range(it.childCount()):
                unhide(it.child(i))
        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def fill_usage(self):
        self.usage_dirty = False
        self.utree.setSortingEnabled(False)
        self.utree.clear()
        for u in self.model.usage():
            it = QTreeWidgetItem([u["label"], fmt_dur(u["time"]), str(u["opens"]), str(u["screens"]), str(u["taps"]),
                                  datetime.datetime.fromtimestamp(u["last"] / 1000).strftime("%m-%d %H:%M")
                                  if u["last"] else "", u["pkg"]])
            it.setData(1, Qt.ItemDataRole.UserRole, u["time"])
            for c in (1, 2, 3, 4):
                it.setTextAlignment(c, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.utree.addTopLevelItem(it)
        self.utree.setSortingEnabled(True)

    def tick(self):
        if self.model.app and self.streamer:
            self.model.last_t = max(self.model.last_t, int(time.time() * 1000))
            self.model._update_app_item(self.model.app, self.model.last_t)
        if self.usage_dirty and self.tabs.currentIndex() == 1:
            self.fill_usage()

    def closeEvent(self, e):
        self.timer.stop()
        self.stop_live()
        if self.b_rec.property("running"):
            self.api.message("Activity log: the device is still recording in the background.")
        super().closeEvent(e)
