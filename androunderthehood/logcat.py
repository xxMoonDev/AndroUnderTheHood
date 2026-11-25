"""Live logcat viewer streamed over a dedicated SSH channel."""
from __future__ import annotations

import collections
import datetime as dt
import logging
import re
import threading
import time

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
                               QSpinBox, QTableView, QVBoxLayout)

from . import backend as B
from . import theme

log = logging.getLogger("androunderthehood")

LEVELS = "VDIWEF"
LINE_RE = re.compile(r"^(\d\d-\d\d) (\d\d:\d\d:\d\d\.\d+)\s+(\d+)\s+(\d+) ([VDIWEFS]) (.*?)\s*: ?(.*)$")
BUFFERS = [("main + system + crash", "main,system,crash"), ("all", "all"), ("main", "main"), ("system", "system"),
           ("crash", "crash"), ("events", "events"), ("radio", "radio"), ("kernel", "kernel")]
LEVEL_COLORS = {"V": "#7d838c", "D": "#c9cdd3", "I": "#7cc4ff", "W": "#e0a84f", "E": "#ff6b6b", "F": "#ff4f9a",
                "S": "#6c727b"}


class LogStream:
    """Runs `logcat` on the device and collects parsed lines from a background thread."""

    def __init__(self, conn: B.Connection, buffers: str, history: int):
        self.conn = conn
        self.cmd = f"logcat -v threadtime -b {buffers} -T {max(1, history)}"
        self.queue: collections.deque = collections.deque()
        self.stop = False
        self.chan = None
        self.error: str | None = None
        self.running = False
        threading.Thread(target=self._run, daemon=True).start()

    def close(self):
        self.stop = True
        try:
            if self.chan:
                self.chan.close()
        except Exception:  # noqa: BLE001
            pass
        p = getattr(self, "proc", None)
        if p is not None:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass

    def _run_adb(self):
        p = self.proc = self.conn.popen(self.cmd)
        self.running = True
        for raw in iter(p.stdout.readline, b""):
            if self.stop:
                break
            self.queue.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        if not self.stop:
            err = p.stderr.read().decode(errors="replace").strip()
            self.error = err or "logcat stopped"

    def _run(self):
        if getattr(self.conn, "is_adb", False):
            try:
                self._run_adb()
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
            finally:
                self.running = False
            return
        try:
            if self.conn.use_pty:
                raise B.BackendError("This SSH server does not allow running commands directly (exec channel); "
                                     "live logcat needs it.")
            chan = self.conn.open_channel()
            self.chan = chan
            chan.exec_command(self.conn.wrap_root(self.cmd))
            chan.settimeout(0.5)
            self.running = True
            buf = b""
            while not self.stop:
                try:
                    data = chan.recv(65536)
                except Exception:  # noqa: BLE001  (timeout)
                    if chan.closed or chan.exit_status_ready():
                        break
                    continue
                if not data:
                    break
                buf += data
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    self.queue.append(raw.decode("utf-8", errors="replace").rstrip("\r"))
            if not self.stop:
                err = b""
                while chan.recv_stderr_ready():
                    err += chan.recv_stderr(4096)
                self.error = err.decode(errors="replace").strip() or "logcat stopped"
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
        finally:
            self.running = False
            try:
                if self.chan:
                    self.chan.close()
            except Exception:  # noqa: BLE001
                pass


class LogModel(QAbstractTableModel):
    COLS = ["Time", "PID", "TID", "Lvl", "Tag", "Message"]

    def __init__(self, max_rows=100_000):
        super().__init__()
        self.rows: list[tuple] = []   # (date, time, pid, tid, level, tag, msg)
        self.max_rows = max_rows
        self.colors = {k: QColor(v) for k, v in LEVEL_COLORS.items()}
        self.mono = QFont("Consolas", 9)

    def append(self, new: list[tuple]):
        if not new:
            return
        overflow = len(self.rows) + len(new) - self.max_rows
        if overflow > 0:
            drop = min(overflow, len(self.rows))
            if drop:
                self.beginRemoveRows(QModelIndex(), 0, drop - 1)
                del self.rows[:drop]
                self.endRemoveRows()
            new = new[-self.max_rows:]
        n = len(self.rows)
        self.beginInsertRows(QModelIndex(), n, n + len(new) - 1)
        self.rows.extend(new)
        self.endInsertRows()

    def clear(self):
        self.beginResetModel()
        self.rows = []
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 6

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLS[s]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r = self.rows[index.row()]
        c = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return (f"{r[0]} {r[1]}", r[2], r[3], r[4], r[5], r[6])[c]
        if role == Qt.ItemDataRole.ForegroundRole:
            if c in (0, 1, 2):
                return QColor(theme.TEXT_FAINT)
            return self.colors.get(r[4])
        if role == Qt.ItemDataRole.FontRole:
            return self.mono
        if role == Qt.ItemDataRole.ToolTipRole and c == 5 and len(r[6]) > 120:
            return r[6]
        return None


class LogFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.min_level = 0
        self.tags_in: set[str] = set()
        self.tags_out: set[str] = set()
        self.text = ""
        self.text_raw = ""
        self.use_regex = False
        self.regex: re.Pattern | None = None
        self.pids: set[str] | None = None

    def update(self, level=None, tags=None, text=None, use_regex=None, pids=...):
        if level is not None:
            self.min_level = LEVELS.index(level) if level in LEVELS else 0
        if tags is not None:
            parts = [t.strip() for t in tags.split(",") if t.strip()]
            self.tags_in = {t.lower() for t in parts if not t.startswith("-")}
            self.tags_out = {t[1:].lower() for t in parts if t.startswith("-")}
        if text is not None or use_regex is not None:
            if text is not None:
                self.text_raw = text
            if use_regex is not None:
                self.use_regex = use_regex
            self.text = self.text_raw.lower()
            self.regex = None
            if self.use_regex and self.text_raw:
                try:
                    self.regex = re.compile(self.text_raw, re.IGNORECASE)
                except re.error:
                    self.regex = None   # incomplete pattern while typing: plain search
        if pids is not ...:
            self.pids = pids
        self.invalidateFilter() if hasattr(self, "invalidateFilter") else self.invalidate()

    def filterAcceptsRow(self, row, parent):
        r = self.sourceModel().rows[row]
        lvl = r[4]
        if lvl in LEVELS and LEVELS.index(lvl) < self.min_level:
            return False
        if self.pids is not None and r[2] not in self.pids:
            return False
        tag = r[5].lower()
        if self.tags_in and tag not in self.tags_in:
            return False
        if tag in self.tags_out:
            return False
        if self.regex is not None:
            return bool(self.regex.search(r[5]) or self.regex.search(r[6]))
        if self.text:
            return self.text in r[6].lower() or self.text in tag
        return True


class LogcatDialog(QDialog):
    def __init__(self, win, conn: B.Connection, package: str | None = None):
        super().__init__(win)
        from .ui import Task

        self.Task = Task
        self.win = win
        self.conn = conn
        self.stream: LogStream | None = None
        self.paused = False
        self.held: list[tuple] = []
        self.proc_names: dict[str, str] = {}   # pid -> process name
        self.lines_total = 0
        self.rate_mark = (time.time(), 0)
        self.setWindowTitle(f"Logcat – {conn.profile.name}")
        self.resize(1400, 820)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(8)
        r1 = QHBoxLayout()
        self.app = QComboBox()
        self.app.setEditable(True)
        self.app.setMinimumWidth(280)
        self.app.lineEdit().setPlaceholderText("All apps (type or pick a package)")
        self.app.addItem("")
        if package:
            self.app.addItem(package)
            self.app.setCurrentText(package)
        self.app.currentTextChanged.connect(lambda _t: self.update_pids())
        r1.addWidget(QLabel("App"))
        r1.addWidget(self.app)
        self.level = QComboBox()
        for name, code in (("Verbose", "V"), ("Debug", "D"), ("Info", "I"), ("Warning", "W"), ("Error", "E"),
                           ("Fatal", "F")):
            self.level.addItem(name, code)
        self.level.currentIndexChanged.connect(lambda _i: self.proxy.update(level=self.level.currentData()))
        r1.addWidget(QLabel("Level ≥"))
        r1.addWidget(self.level)
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("Tags: ActivityManager, -chatty")
        self.tags.setMaximumWidth(260)
        self.tags.textChanged.connect(lambda t: self.proxy.update(tags=t))
        r1.addWidget(self.tags)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search message / tag…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda t: self.proxy.update(text=t))
        r1.addWidget(self.search, 1)
        self.regex = QCheckBox("Regex")
        self.regex.toggled.connect(lambda on: self.proxy.update(use_regex=on))
        r1.addWidget(self.regex)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        self.buffers = QComboBox()
        for label, b in BUFFERS:
            self.buffers.addItem(label, b)
        self.buffers.currentIndexChanged.connect(lambda _i: self.restart())
        r2.addWidget(QLabel("Buffer"))
        r2.addWidget(self.buffers)
        self.history = QSpinBox()
        self.history.setRange(0, 100000)
        self.history.setSingleStep(1000)
        self.history.setValue(5000)
        self.history.setPrefix("history ")
        self.history.setSuffix(" lines")
        self.history.setToolTip("Lines of existing log loaded when (re)starting")
        r2.addWidget(self.history)
        self.btn_pause = QPushButton(theme.line_icon("lock"), " Pause")
        self.btn_pause.setCheckable(True)
        self.btn_pause.toggled.connect(self.set_paused)
        self.btn_restart = QPushButton(theme.line_icon("refresh"), " Restart")
        self.btn_restart.clicked.connect(self.restart)
        self.btn_clear = QPushButton(theme.line_icon("broom"), " Clear")
        self.btn_clear.setToolTip("Clear the view (Shift+click also clears the device's log buffers)")
        self.btn_clear.clicked.connect(self.clear)
        self.btn_save = QPushButton(theme.line_icon("save"), " Save…")
        self.btn_save.clicked.connect(self.save)
        self.autoscroll = QCheckBox("Auto-scroll")
        self.autoscroll.setChecked(True)
        self.wrap_hint = QLabel("")
        for b in (self.btn_pause, self.btn_restart, self.btn_clear, self.btn_save):
            b.setAutoDefault(False)
            r2.addWidget(b)
        r2.addWidget(self.autoscroll)
        r2.addStretch(1)
        self.state = QLabel("")
        self.state.setStyleSheet(f"color: {theme.TEXT_DIM};")
        r2.addWidget(self.state)
        lay.addLayout(r2)

        self.model = LogModel()
        self.proxy = LogFilter()
        self.proxy.setSourceModel(self.model)
        v = self.view = QTableView()
        v.setModel(self.proxy)
        v.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        v.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        v.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.setShowGrid(False)
        v.setWordWrap(False)
        v.setAlternatingRowColors(True)
        v.verticalHeader().hide()
        v.verticalHeader().setDefaultSectionSize(21)
        v.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        h = v.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for c, w in enumerate((150, 60, 60, 34, 190)):
            v.setColumnWidth(c, w)
        h.setStretchLastSection(True)
        v.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        v.customContextMenuRequested.connect(self.menu)
        # a filter change rebuilds the view at the top: follow the newest line again
        self.proxy.layoutChanged.connect(self._follow_after_filter)
        self.proxy.modelReset.connect(self._follow_after_filter)
        lay.addWidget(v, 1)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(self.status)

        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.search.setFocus)
        QShortcut(QKeySequence.StandardKey.Copy, v, activated=self.copy_rows)
        QShortcut(QKeySequence(Qt.Key.Key_Space), v, activated=lambda: self.btn_pause.toggle())

        self.pump = QTimer(self)
        self.pump.timeout.connect(self.drain)
        self.pump.start(200)
        self.ps_timer = QTimer(self)
        self.ps_timer.timeout.connect(self.refresh_processes)
        self.ps_timer.start(4000)
        self.refresh_processes()
        self.restart()

    # ---- stream ------------------------------------------------------------
    def restart(self):
        if self.stream:
            self.stream.close()
        self.model.clear()
        self.held = []
        self.stream = LogStream(self.conn, self.buffers.currentData(), self.history.value())
        self.state.setText("● live")
        self.state.setStyleSheet(f"color: {theme.ACCENT};")

    def parse(self, line: str):
        m = LINE_RE.match(line)
        if m:
            return m.groups()
        if not line.strip():
            return None
        return ("", "", "", "", "S", "", line)   # "--------- beginning of main" etc.

    def drain(self):
        st = self.stream
        if not st:
            return
        batch = []
        qd = st.queue
        n = 0
        while qd and n < 20000:
            p = self.parse(qd.popleft())
            n += 1
            if p:
                batch.append(p)
        self.lines_total += n
        if self.paused:
            self.held.extend(batch)
            if len(self.held) > 100_000:
                del self.held[:len(self.held) - 100_000]
        elif batch:
            sb = self.view.verticalScrollBar()
            at_bottom = sb.value() >= sb.maximum() - 2
            self.model.append(batch)
            if self.autoscroll.isChecked() and at_bottom:
                self.view.scrollToBottom()
        if st.error and not st.running:
            self.state.setText("● stopped")
            self.state.setStyleSheet("color: #ff6b6b;")
            self.status.setText(f"logcat stopped: {st.error}   (Restart to reconnect)")
            st.error = None
        now = time.time()
        if now - self.rate_mark[0] >= 1.0:
            rate = (self.lines_total - self.rate_mark[1]) / (now - self.rate_mark[0])
            self.rate_mark = (now, self.lines_total)
            pkg = self.app.currentText().strip()
            extra = f" · {len(self.held):,} held while paused" if self.paused else ""
            self.status.setText(f"{self.proxy.rowCount():,} shown of {self.model.rowCount():,} lines · {rate:,.0f} lines/s"
                                + (f" · app {pkg}: pids {', '.join(sorted(self.proxy.pids or []))[:80] or 'not running'}"
                                   if pkg else "") + extra)

    def _follow_after_filter(self, *_):
        if self.autoscroll.isChecked():
            QTimer.singleShot(0, self.view.scrollToBottom)

    def set_paused(self, on: bool):
        self.paused = on
        self.btn_pause.setText(" Resume" if on else " Pause")
        self.state.setText("❚❚ paused" if on else "● live")
        self.state.setStyleSheet(f"color: {'#e0a84f' if on else theme.ACCENT};")
        if not on and self.held:
            self.model.append(self.held)
            self.held = []
            if self.autoscroll.isChecked():
                self.view.scrollToBottom()

    def clear(self):
        also_device = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.model.clear()
        self.held = []
        if also_device:
            conn = self.conn
            self.Task.start(lambda: conn.exec(f"logcat -b {self.buffers.currentData()} -c", timeout=30),
                            lambda _r: self.win.statusBar().showMessage("Device log buffers cleared", 4000),
                            lambda m: QMessageBox.warning(self, "Logcat", m))

    # ---- app filter ---------------------------------------------------------
    def refresh_processes(self):
        conn = self.conn

        def work():
            rc, out, _ = conn.exec("ps -A -o PID,NAME 2>/dev/null", timeout=20)
            res = {}
            for line in out.decode(errors="replace").splitlines()[1:]:
                f = line.split(None, 1)
                if len(f) == 2 and f[0].isdigit():
                    res[f[0]] = f[1].strip()
            return res

        def done(res):
            self.proc_names = res
            # offer running app packages in the combo
            names = sorted({n.split(":", 1)[0] for n in res.values() if "." in n and not n.startswith("/")})
            cur = self.app.currentText()
            existing = {self.app.itemText(i) for i in range(self.app.count())}
            for n in names:
                if n not in existing:
                    self.app.addItem(n)
            if self.app.currentText() != cur:
                self.app.setCurrentText(cur)
            self.update_pids()
        self.Task.start(work, done, lambda m: None)

    def update_pids(self):
        pkg = self.app.currentText().strip()
        if not pkg:
            if self.proxy.pids is not None:
                self.proxy.update(pids=None)
            return
        pids = {pid for pid, n in self.proc_names.items() if n == pkg or n.startswith(pkg + ":")}
        if pids != self.proxy.pids:
            self.proxy.update(pids=pids)

    # ---- copy / save ---------------------------------------------------------
    def row_text(self, r) -> str:
        return f"{r[0]} {r[1]} {r[2]:>5} {r[3]:>5} {r[4]} {r[5]}: {r[6]}" if r[0] else r[6]

    def copy_rows(self):
        rows = sorted({self.proxy.mapToSource(i).row() for i in self.view.selectionModel().selectedRows()})
        if rows:
            QApplication.clipboard().setText("\n".join(self.row_text(self.model.rows[r]) for r in rows))

    def menu(self, pos):
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        r = self.model.rows[self.proxy.mapToSource(idx).row()]
        m = QMenu(self)
        m.addAction(theme.line_icon("copy"), "Copy selected lines", self.copy_rows)
        if r[5]:
            m.addAction(theme.line_icon("sliders"), f"Show only tag “{r[5]}”", lambda: self.tags.setText(r[5]))
            m.addAction(theme.line_icon("disconnect"), f"Hide tag “{r[5]}”",
                        lambda: self.tags.setText(", ".join(x for x in (self.tags.text(), f"-{r[5]}") if x)))
        if r[2] and r[2] in self.proc_names:
            name = self.proc_names[r[2]].split(":", 1)[0]
            m.addAction(theme.line_icon("package"), f"Show only app “{name}”", lambda: self.app.setCurrentText(name))
        m.exec(self.view.viewport().mapToGlobal(pos))

    def save(self):
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path, _ = QFileDialog.getSaveFileName(self, "Save log", f"logcat-{stamp}.txt", "Text (*.txt);;All files (*)")
        if not path:
            return
        n = self.proxy.rowCount()
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for i in range(n):
                f.write(self.row_text(self.model.rows[self.proxy.mapToSource(self.proxy.index(i, 0)).row()]) + "\n")
        self.win.statusBar().showMessage(f"Saved {n:,} lines to {path}", 6000)

    def closeEvent(self, ev):
        self.pump.stop()
        self.ps_timer.stop()
        if self.stream:
            self.stream.close()
        super().closeEvent(ev)
