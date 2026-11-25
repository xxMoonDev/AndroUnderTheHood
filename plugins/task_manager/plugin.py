"""Task Manager & Resource Monitor plugin (Windows-style) for Android.

Tabs
  Processes         apps / Android system / kernel threads, grouped per app, with CPU, memory, disk and threads
  Details           every process flat: PID, user, state, CPU, CPU time, memory, threads, priority, nice, start time
  Performance       CPU (overall or per core + frequencies), memory, storage, each network interface, GPU, temperatures
  Resource Monitor  CPU, Memory, Disk, Network (connections + listening ports) and Thermal & battery views
  Thermal recorder  records temperatures + cluster frequencies, finds throttling, optional stress load, CSV
  Disks             eMMC/UFS wear and reserve blocks, lifetime writes, sequential speed test
Actions (right-click a process): End task, Kill, End process tree, Suspend / Resume, Set priority, Properties.
Data comes from engine.py (one shell loop on the device). With root every process's disk I/O, disk activity,
temperatures and GPU load are visible; as the shell user those parts are hidden.
"""
from __future__ import annotations

import os
import re
import threading
import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QGridLayout, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import engine as E
from .extras import ExtrasMixin

SPEEDS = [("High (1 s)", 1), ("Normal (2 s)", 2), ("Low (5 s)", 5), ("Paused", 0)]
CRITICAL = {"init", "system_server", "zygote", "zygote64", "surfaceflinger", "servicemanager", "hwservicemanager",
            "vold", "lmkd", "ueventd", "logd", "netd", "adbd", "app_process", "installd", "keystore2"}
PRIORITIES = [("Realtime-ish (−20)", -20), ("High (−10)", -10), ("Above normal (−5)", -5), ("Normal (0)", 0),
              ("Below normal (5)", 5), ("Low (10)", 10), ("Idle (19)", 19)]
C_CPU, C_MEM, C_DISK, C_NET, C_GPU, C_TEMP = "#3e8ed0", "#a070e0", "#46a758", "#e08a3c", "#3fb8c4", "#e5484d"


def register(api):
    api.add_tool("Task Manager", lambda: TaskManager(api).show(), icon="pulse",
                 tooltip="Processes, performance graphs and resource monitor (plugin)", needs="connection")


def fmt_time(ticks: int, hz: int) -> str:
    s = ticks // max(1, hz)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def heat(frac: float) -> QColor:
    """Windows-style cell shading: pale yellow -> orange as the value grows."""
    frac = max(0.0, min(1.0, frac))
    return QColor(255, int(236 - 120 * frac), int(170 - 170 * frac), int(40 + 150 * frac))


# ---------------------------------------------------------------------------------------------- items that sort by value
class SortItem(QTableWidgetItem):
    def __init__(self, text: str, key=None):
        super().__init__(text)
        self.setData(Qt.ItemDataRole.UserRole, key if key is not None else text)

    def __lt__(self, other):
        a, b = self.data(Qt.ItemDataRole.UserRole), other.data(Qt.ItemDataRole.UserRole)
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


class SortTreeItem(QTreeWidgetItem):
    def __lt__(self, other):
        c = self.treeWidget().sortColumn() if self.treeWidget() else 0
        a, b = self.data(c, Qt.ItemDataRole.UserRole), other.data(c, Qt.ItemDataRole.UserRole)
        if a is None or b is None:
            return self.text(c).lower() < other.text(c).lower()
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


def make_table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(22)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    t.setSortingEnabled(True)
    t.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def fill(t: QTableWidget, rows: list[list], key_col: int | None = None, heats: dict | None = None):
    """rows: [(text, sort_key), ...]; keeps the selected row (by key_col) and the scroll position."""
    sel = None
    if key_col is not None and t.currentRow() >= 0 and t.item(t.currentRow(), key_col):
        sel = t.item(t.currentRow(), key_col).data(Qt.ItemDataRole.UserRole)
    scroll = t.verticalScrollBar().value()
    t.setUpdatesEnabled(False)
    t.setSortingEnabled(False)
    t.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            text, key = cell if isinstance(cell, tuple) else (cell, cell)
            it = SortItem("" if text is None else str(text), key)
            if isinstance(key, (int, float)) and not isinstance(key, bool):
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if heats and c in heats and isinstance(key, (int, float)):
                it.setBackground(heat(key / heats[c]))
                it.setForeground(QColor("#000000"))
            t.setItem(r, c, it)
    t.setSortingEnabled(True)
    if sel is not None:
        for r in range(t.rowCount()):
            if t.item(r, key_col) and t.item(r, key_col).data(Qt.ItemDataRole.UserRole) == sel:
                t.selectRow(r)
                break
    t.verticalScrollBar().setValue(scroll)
    t.setUpdatesEnabled(True)


# ---------------------------------------------------------------------------------------------- graphs
class Graph(QWidget):
    """Area graph of one or two series (0..vmax, or auto-scaled for rates)."""

    def __init__(self, colour: str, title: str = "", vmax: float | None = 100.0, compact: bool = False):
        super().__init__()
        self.colour = colour
        self.title = title
        self.vmax = vmax
        self.compact = compact
        self.series: list[tuple[list, str, str]] = []      # (values, colour, label)
        self.unit = "%"
        self.right = ""
        self.setMinimumHeight(40 if compact else 140)

    def sizeHint(self):
        return QSize(300, 60 if self.compact else 260)

    def set(self, *series, unit="%", right=""):
        self.series = list(series)
        self.unit = unit
        self.right = right
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        W, H = self.width(), self.height()
        base = QColor(self.colour)
        p.fillRect(self.rect(), QColor("#101418"))
        top = 2 if self.compact else 18
        bot = H - 2
        x0, x1 = 1, W - 1
        f = QFont(self.font())
        f.setPointSizeF(7.5 if self.compact else 8.5)
        p.setFont(f)
        grid = QColor(base)
        grid.setAlpha(45)
        p.setPen(QPen(grid, 1))
        rows = 4 if self.compact else 10
        for i in range(1, rows):
            y = top + (bot - top) * i / rows
            p.drawLine(QPointF(x0, y), QPointF(x1, y))
        cols = 6 if self.compact else 20
        for i in range(1, cols):
            x = x0 + (x1 - x0) * i / cols
            p.drawLine(QPointF(x, top), QPointF(x, bot))
        p.setPen(QPen(base, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(x0, top, x1 - x0 - 1, bot - top - 1))
        vmax = self.vmax
        if vmax is None:
            peak = max((v for vals, _c, _l in self.series for v in vals), default=0)
            vmax = 1024.0
            while vmax < peak * 1.1:
                vmax *= 2
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for vals, colour, _label in self.series:
            if not vals:
                continue
            n = E.HIST
            step = (x1 - x0) / (n - 1)
            pts = [QPointF(x1 - (len(vals) - 1 - i) * step, bot - min(1.0, max(0.0, v / vmax)) * (bot - top))
                   for i, v in enumerate(vals)]
            path = QPainterPath(QPointF(pts[0].x(), bot))
            for q in pts:
                path.lineTo(q)
            path.lineTo(QPointF(pts[-1].x(), bot))
            if len(self.series) <= 2:              # many series (per-core): lines only, fills would turn to mud
                fillc = QColor(colour)
                fillc.setAlpha(55)
                p.fillPath(path, fillc)
            line = QPainterPath(pts[0])
            for q in pts[1:]:
                line.lineTo(q)
            p.setPen(QPen(QColor(colour), 1.4))
            p.drawPath(line)
        if not self.compact:
            p.setPen(QColor("#c9d1d9"))
            p.drawText(QRectF(4, 0, W / 2, 16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.title)
            label = self.right or (f"{vmax:g} %" if self.unit == "%" else E.human_bytes(vmax, rate=True))
            p.drawText(QRectF(W / 2, 0, W / 2 - 4, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)
            lx = 6
            for _vals, colour, lbl in self.series:
                if lbl:
                    p.setPen(QColor(colour))
                    p.drawText(QPointF(lx, H - 6), "● " + lbl)
                    lx += 14 + len(lbl) * 7


class MemBar(QWidget):
    """Memory composition, like Resource Monitor: in use / modified (dirty) / cached / free."""

    def __init__(self):
        super().__init__()
        self.parts: list[tuple[str, float, str]] = []
        self.setMinimumHeight(44)

    def paintEvent(self, _e):
        p = QPainter(self)
        W, H = self.width(), self.height()
        total = sum(v for _n, v, _c in self.parts) or 1
        x = 0.0
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)
        for name, v, col in self.parts:
            w = (W - 2) * v / total
            p.fillRect(QRectF(1 + x, 1, w, H - 20), QColor(col))
            x += w
        p.setPen(QColor("#888888"))
        p.drawRect(QRectF(0, 0, W - 1, H - 19))
        lx = 2
        for name, v, col in self.parts:
            p.setPen(QColor(col))
            txt = f"■ {name} {E.human_bytes(v)}"
            p.drawText(QPointF(lx, H - 4), txt)
            lx += 16 + len(txt) * 6.2


class Tile(QWidget):
    """Left-hand entry of the Performance tab: name, value and a small graph."""
    clicked = Signal(str)

    def __init__(self, key: str, title: str, colour: str):
        super().__init__()
        self.key = key
        self.selected = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.graph = Graph(colour, compact=True)
        self.graph.setFixedSize(70, 46)
        lay.addWidget(self.graph)
        col = QVBoxLayout()
        self.t = QLabel(f"<b>{title}</b>")
        self.v = QLabel("")
        self.v.setStyleSheet("color: gray;")
        col.addWidget(self.t)
        col.addWidget(self.v)
        lay.addLayout(col, 1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, _e):
        self.clicked.emit(self.key)

    def paintEvent(self, e):
        if self.selected:
            p = QPainter(self)
            p.fillRect(self.rect(), QColor(62, 142, 208, 60))
        super().paintEvent(e)


# ---------------------------------------------------------------------------------------------- window
class TaskManager(ExtrasMixin, QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"Task Manager – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1320, 860)
        self.dev = E.Device(api)
        self.static: dict = {}
        self.model: E.Model | None = None
        self.sampler: E.Sampler | None = None
        self.token = ""
        self.perf_key = "cpu"
        self.tiles: dict[str, Tile] = {}
        self.tree_items: dict = {}

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Update speed"))
        self.speed = QComboBox()
        for label, s in SPEEDS:
            self.speed.addItem(label, s)
        self.speed.setCurrentIndex(int(api.get_setting("speed_idx", 1)))
        self.speed.currentIndexChanged.connect(self.on_speed)
        top.addWidget(self.speed)
        self.status = QLabel("Starting…")
        self.status.setStyleSheet(f"color: {api.theme.TEXT_DIM};")
        top.addWidget(self.status, 1)
        lay.addLayout(top)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.build_processes()
        self.build_details()
        self.build_performance()
        self.build_resmon()
        self.build_thermal(Graph, C_TEMP)
        self.build_disks()
        self.tabs.currentChanged.connect(lambda _i: self.refresh())

        def work():
            return self.dev.static()

        def done(st):
            try:
                self.static = st
                self.model = E.Model(st)
                root = "root" if st.get("root") else "shell user – disk, temperatures and GPU need Root"
                self.status.setText(f"{st.get('soc_vendor', '')} {st.get('soc_model') or st.get('hardware', '')} "
                                    f"({st.get('platform', '')}) · kernel {st.get('kernel', '')} · {root}")
                self.build_tiles()
                self.on_speed()
            except RuntimeError:
                pass

        def fail(m):
            try:
                self.status.setText(f"Failed: {m}")
                QMessageBox.warning(self, "Task Manager", m)
            except RuntimeError:
                pass

        api.task(work, done, fail)

    # ================================================================== sampling
    def on_speed(self, *_):
        self.api.set_setting("speed_idx", self.speed.currentIndex())
        if not self.model:
            return
        prev = self.token if self.sampler else ""
        if self.sampler:
            self.sampler.stop()
            self.sampler = None
        s = self.speed.currentData()
        if not s:
            if prev:
                dev = self.dev
                threading.Thread(target=lambda: dev.kill_leftovers(prev), daemon=True).start()
            return
        self.token = f"tm{os.getpid()}x{int(time.time() * 1000) % 10**9}"
        smp = E.Sampler(self.dev, s, self.token, prev)
        smp.snapshot.connect(self.on_snapshot)
        smp.failed.connect(lambda m, smp=smp: self.status.setText(f"Sampling stopped: {m}") if self.sampler is smp else None)
        self.sampler = smp
        smp.start()

    def on_snapshot(self, snap: dict):
        if not self.model:
            return
        self.model.update(snap)
        try:
            self.thermal_tick()
        except RuntimeError:
            pass
        if self.model.prev:
            try:
                self.refresh()
            except RuntimeError:
                pass

    def refresh(self):
        m = self.model
        if not m or not m.prev:
            return
        self.update_tiles()
        i = self.tabs.currentIndex()
        if i == 0:
            self.update_processes()
        elif i == 1:
            self.update_details()
        elif i == 2:
            self.update_perf()
        elif i == 3:
            self.update_resmon()

    # ================================================================== helpers about processes
    def proc_group(self, p: dict) -> str:
        if p["ppid"] == 2 or p.get("pid") == 2 or (p["name"].startswith("[") and p["name"].endswith("]")):
            return "kernel"
        if p["uid"] % 100000 >= 10000:
            return "app"
        return "system"

    def app_key(self, p: dict) -> str:
        n = p["name"].split(":")[0]
        if p["uid"] % 100000 >= 99000 or "." not in n:     # isolated / unnamed: group under the owning package
            pk = self.static.get("packages", {}).get(p["uid"] % 100000 + 0)
            n = pk[0] if pk else n
        return n

    def label(self, pkg: str) -> str:
        return self.static.get("labels", {}).get(pkg, "")

    def user_of(self, p: dict) -> str:
        return E.user_name(p["uid"], self.static.get("packages", {}))

    # ================================================================== Processes tab
    def build_processes(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search processes (name, package, PID)")
        self.search.textChanged.connect(lambda _t: self.update_processes(force=True))
        row.addWidget(self.search, 1)
        self.show_kernel = QCheckBox("Show kernel threads")
        self.show_kernel.toggled.connect(lambda _b: self.update_processes(force=True))
        row.addWidget(self.show_kernel)
        self.b_end = QPushButton("End task")
        self.b_end.clicked.connect(lambda: self.act_selected("end"))
        row.addWidget(self.b_end)
        lay.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "PID", "Status", "User", "CPU", "Memory", "Disk", "Threads"])
        self.tree.setColumnWidth(0, 380)
        self.tree.setColumnWidth(3, 90)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(4, Qt.SortOrder.DescendingOrder)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(lambda pos: self.menu_for(self.tree_selection(), self.tree, pos))
        self.tree.setUniformRowHeights(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c, w_ in ((1, 70), (2, 90), (3, 90), (4, 80), (5, 100), (6, 90), (7, 70)):
            self.tree.setColumnWidth(c, w_)
        lay.addWidget(self.tree, 1)
        self.proc_summary = QLabel("")
        lay.addWidget(self.proc_summary)
        self.tabs.addTab(w, "Processes")

    def tree_selection(self) -> list[dict]:
        it = self.tree.currentItem()
        if not it or not self.model or not self.model.cur:
            return []
        d = it.data(0, Qt.ItemDataRole.UserRole + 1)
        if not d:
            return []
        kind, val = d
        procs = self.model.cur["procs"]
        if kind == "pid" and val in procs:
            p = dict(procs[val])
            p["pid"] = val
            return [p]
        if kind == "app":
            out = []
            for pid, p in procs.items():
                q = dict(p, pid=pid)
                if self.proc_group(q) == "app" and self.app_key(q) == val:
                    out.append(q)
            return out
        return []

    def set_row(self, it: QTreeWidgetItem, name, pid, status, user, cpu, mem, disk, thr, maxima):
        vals = [(name, name.lower()), (str(pid) if pid else "", pid or 0), (status, status), (user, user),
                (f"{cpu:.1f} %", cpu), (E.human_bytes(mem), mem),
                ((E.human_bytes(disk, True) if disk is not None else "—"), disk or 0), (str(thr), thr)]
        for c, (text, key) in enumerate(vals):
            if it.text(c) != text:
                it.setText(c, text)
            it.setData(c, Qt.ItemDataRole.UserRole, key)
            if c >= 1:
                it.setTextAlignment(c, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                                    if c in (1, 4, 5, 6, 7) else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for c, v, mx in ((4, cpu, maxima[0]), (5, mem, maxima[1]), (6, disk or 0, maxima[2])):
            it.setBackground(c, heat(v / mx if mx else 0))
            it.setForeground(c, QColor("#000000"))

    def update_processes(self, force: bool = False):
        m = self.model
        if not m or not m.cur or "cpu" not in next(iter(m.cur["procs"].values()), {}):
            return
        q = self.search.text().strip().lower()
        procs = m.cur["procs"]
        groups = {"app": {}, "system": [], "kernel": []}
        for pid, p in procs.items():
            p["pid"] = pid
            g = self.proc_group(p)
            if q and q not in p["name"].lower() and q not in str(pid) and q not in self.label(self.app_key(p)).lower():
                continue
            if g == "app":
                groups["app"].setdefault(self.app_key(p), []).append(p)
            else:
                groups[g].append(p)
        total_mem = m.cur["mem"].get("MemTotal", 1)
        maxima = (25.0, total_mem * 0.1, 5 * 1024 * 1024)
        root_items = {}
        titles = {"app": "Apps", "system": "Android system & native processes", "kernel": "Kernel threads"}
        self.tree.setUpdatesEnabled(False)
        self.tree.setSortingEnabled(False)
        seen = set()
        for g in ("app", "system", "kernel"):
            if g == "kernel" and not self.show_kernel.isChecked():
                gi = self.tree_items.pop(("group", g), None)
                if gi is not None:
                    self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(gi))
                continue
            gi = self.tree_items.get(("group", g))
            if gi is None:
                gi = SortTreeItem(self.tree)
                f = gi.font(0)
                f.setBold(True)
                gi.setFont(0, f)
                gi.setFirstColumnSpanned(False)
                gi.setExpanded(g != "kernel")
                self.tree_items[("group", g)] = gi
            seen.add(("group", g))
            root_items[g] = gi
            entries = groups[g]
            count = len(entries)
            gi.setText(0, f"{titles[g]} ({count})")
            # sort groups in a fixed order regardless of the column sort
            for c in range(8):
                gi.setData(c, Qt.ItemDataRole.UserRole, None)
            if g == "app":
                for key, plist in entries.items():
                    cpu = sum(p.get("cpu", 0) for p in plist)
                    mem = sum(p["mem"] for p in plist)
                    disk = (sum(p.get("io_rs", 0) + p.get("io_ws", 0) for p in plist) if m.cur["has_io"] else None)
                    thr = sum(p["threads"] for p in plist)
                    lab = self.label(key)
                    name = f"{lab}  ({key})" if lab else key
                    states = {p["state"] for p in plist}
                    status = "Suspended" if states == {"T"} else ""
                    ai = self.tree_items.get(("app", key))
                    if ai is None:
                        ai = SortTreeItem(gi)
                        ai.setData(0, Qt.ItemDataRole.UserRole + 1, ("app", key))
                        self.tree_items[("app", key)] = ai
                    seen.add(("app", key))
                    self.set_row(ai, name + (f"  [{len(plist)}]" if len(plist) > 1 else ""), 0, status,
                                 self.user_of(plist[0]), cpu, mem, disk, thr, maxima)
                    if len(plist) > 1:
                        for p in plist:
                            ci = self.tree_items.get(("pid", p["pid"]))
                            if ci is None or ci.parent() is not ai:
                                if ci is not None and ci.parent():
                                    ci.parent().removeChild(ci)
                                ci = SortTreeItem(ai)
                                ci.setData(0, Qt.ItemDataRole.UserRole + 1, ("pid", p["pid"]))
                                self.tree_items[("pid", p["pid"])] = ci
                            seen.add(("pid", p["pid"]))
                            self.set_row(ci, p["name"], p["pid"], E.STATE_NAMES.get(p["state"], p["state"]),
                                         self.user_of(p), p.get("cpu", 0), p["mem"],
                                         (p.get("io_rs", 0) + p.get("io_ws", 0)) if m.cur["has_io"] else None,
                                         p["threads"], maxima)
                    else:
                        ai.setText(1, str(plist[0]["pid"]))
                        ai.setData(1, Qt.ItemDataRole.UserRole, plist[0]["pid"])
                        ai.setText(2, E.STATE_NAMES.get(plist[0]["state"], ""))
            else:
                for p in entries:
                    ci = self.tree_items.get(("pid", p["pid"]))
                    if ci is None or ci.parent() is not gi:
                        if ci is not None and ci.parent():
                            ci.parent().removeChild(ci)
                        ci = SortTreeItem(gi)
                        ci.setData(0, Qt.ItemDataRole.UserRole + 1, ("pid", p["pid"]))
                        self.tree_items[("pid", p["pid"])] = ci
                    seen.add(("pid", p["pid"]))
                    self.set_row(ci, p["name"], p["pid"], E.STATE_NAMES.get(p["state"], p["state"]), self.user_of(p),
                                 p.get("cpu", 0), p["mem"],
                                 (p.get("io_rs", 0) + p.get("io_ws", 0)) if m.cur["has_io"] else None,
                                 p["threads"], maxima)
        for k in [k for k in self.tree_items if k not in seen]:
            it = self.tree_items.pop(k)
            par = it.parent()
            if par is not None:
                par.removeChild(it)
            else:
                idx = self.tree.indexOfTopLevelItem(it)
                if idx >= 0:
                    self.tree.takeTopLevelItem(idx)
        self.tree.setSortingEnabled(True)
        self.tree.setUpdatesEnabled(True)
        cpu = m.cpu_pct.get("all", 0)
        mem_pct = 100 * m.mem_used / max(1, total_mem)
        st = m.storage_rates()
        self.proc_summary.setText(
            f"{len(procs)} processes · {sum(p['threads'] for p in procs.values())} threads · CPU {cpu:.0f} % · "
            f"memory {mem_pct:.0f} % ({E.human_bytes(m.mem_used)} of {E.human_bytes(total_mem)})"
            + (f" · storage {E.human_bytes(st['read'] + st['write'], True)}" if m.whole_disks() else "")
            + (f" · GPU {m.cur['gpu']['busy']:.0f} %" if "busy" in m.cur["gpu"] else ""))

    # ================================================================== Details tab
    def build_details(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.details = make_table(["Name", "PID", "Status", "User", "CPU %", "CPU time", "Memory", "Virtual",
                                   "Threads", "Priority", "Nice", "PPID", "Started", "Read total", "Written total"])
        self.details.setColumnWidth(0, 300)
        self.details.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.details.customContextMenuRequested.connect(
            lambda pos: self.menu_for(self.table_selection(self.details, 1), self.details, pos))
        lay.addWidget(self.details, 1)
        self.tabs.addTab(w, "Details")

    def table_selection(self, t: QTableWidget, pid_col: int) -> list[dict]:
        r = t.currentRow()
        if r < 0 or not self.model or not t.item(r, pid_col):
            return []
        pid = t.item(r, pid_col).data(Qt.ItemDataRole.UserRole)
        p = self.model.cur["procs"].get(pid)
        return [dict(p, pid=pid)] if p else []

    def update_details(self):
        m = self.model
        hz = m.hz
        bt = self.static.get("btime", 0)
        rows = []
        for pid, p in m.cur["procs"].items():
            started = time.strftime("%m-%d %H:%M:%S", time.localtime(bt + p["start"] / hz)) if bt else ""
            rows.append([p["name"], (str(pid), pid), E.STATE_NAMES.get(p["state"], p["state"]), self.user_of(p),
                         (f"{p.get('cpu', 0):.1f}", p.get("cpu", 0)), (fmt_time(p["ticks"], hz), p["ticks"]),
                         (E.human_bytes(p["mem"]), p["mem"]), (E.human_bytes(p["vsize"]), p["vsize"]),
                         (str(p["threads"]), p["threads"]), (str(p["prio"]), p["prio"]), (str(p["nice"]), p["nice"]),
                         (str(p["ppid"]), p["ppid"]), (started, p["start"]),
                         (E.human_bytes(p["io_r"]) if "io_r" in p else "", p.get("io_r", 0)),
                         (E.human_bytes(p["io_w"]) if "io_w" in p else "", p.get("io_w", 0))])
        fill(self.details, rows, key_col=1, heats={4: 25.0, 6: m.cur["mem"].get("MemTotal", 1) * 0.1})

    # ================================================================== Performance tab
    def build_performance(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        self.tile_box = QVBoxLayout()
        self.tile_box.setSpacing(2)
        holder = QWidget()
        holder.setLayout(self.tile_box)
        scroll = QScrollArea()
        scroll.setWidget(holder)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(260)
        lay.addWidget(scroll)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.perf_title = QLabel("")
        f = self.perf_title.font()
        f.setPointSizeF(f.pointSizeF() * 1.6)
        self.perf_title.setFont(f)
        head.addWidget(self.perf_title)
        head.addStretch(1)
        self.perf_sub = QLabel("")
        self.perf_sub.setStyleSheet("color: gray;")
        head.addWidget(self.perf_sub)
        self.cpu_view = QComboBox()
        self.cpu_view.addItems(["Overall utilization", "Logical processors"])
        self.cpu_view.currentIndexChanged.connect(lambda _i: self.update_perf())
        head.addWidget(self.cpu_view)
        rl.addLayout(head)
        self.perf_stack = QStackedWidget()
        # page 0: one or two big graphs
        p0 = QWidget()
        p0l = QVBoxLayout(p0)
        p0l.setContentsMargins(0, 0, 0, 0)
        self.big1 = Graph(C_CPU)
        self.big2 = Graph(C_CPU, vmax=None)
        p0l.addWidget(self.big1, 3)
        p0l.addWidget(self.big2, 2)
        self.membar = MemBar()
        p0l.addWidget(self.membar)
        self.perf_stack.addWidget(p0)
        # page 1: per-core grid
        p1 = QWidget()
        self.core_grid = QGridLayout(p1)
        self.core_graphs: list[tuple[Graph, QLabel]] = []
        self.perf_stack.addWidget(p1)
        rl.addWidget(self.perf_stack, 1)
        self.perf_stats = QLabel("")
        self.perf_stats.setTextFormat(Qt.TextFormat.RichText)
        self.perf_stats.setWordWrap(True)
        self.perf_stats.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        rl.addWidget(self.perf_stats)
        lay.addWidget(right, 1)
        self.tabs.addTab(w, "Performance")

    def build_tiles(self):
        m = self.model
        for t in self.tiles.values():
            t.setParent(None)
        self.tiles = {}
        entries = [("cpu", "CPU", C_CPU), ("mem", "Memory", C_MEM)]
        for i, d in enumerate(m.drives()):
            entries.append((f"drive{i}", f"Storage {i} ({d['kind'] or 'disk'})", C_DISK))
        entries.append(("net", "Network", C_NET))
        if self.static.get("gpu_model") or True:
            entries.append(("gpu", "GPU", C_GPU))
        entries.append(("temp", "Temperatures", C_TEMP))
        for key, title, col in entries:
            t = Tile(key, title, col)
            t.clicked.connect(self.select_tile)
            self.tile_box.addWidget(t)
            self.tiles[key] = t
        self.tile_box.addStretch(1)
        self.select_tile("cpu")

    def select_tile(self, key: str):
        self.perf_key = key
        for k, t in self.tiles.items():
            t.selected = k == key
            t.update()
        self.cpu_view.setVisible(key == "cpu")
        self.update_perf()

    def update_tiles(self):
        m = self.model
        h = m.hist
        if "cpu" in self.tiles:
            ghz = max(m.cur["freq"].values(), default=0) / 1e6
            self.tiles["cpu"].v.setText(f"{m.cpu_pct.get('all', 0):.0f} %  {ghz:.2f} GHz")
            self.tiles["cpu"].graph.set((list(h["cpu"]), C_CPU, ""))
        if "mem" in self.tiles:
            tot = m.cur["mem"].get("MemTotal", 1)
            self.tiles["mem"].v.setText(f"{E.human_bytes(m.mem_used)} / {E.human_bytes(tot)} ({100 * m.mem_used / tot:.0f} %)")
            self.tiles["mem"].graph.set((list(h["mem"]), C_MEM, ""))
        for i, d in enumerate(m.drives()):
            t = self.tiles.get(f"drive{i}")
            if t:
                if not m.cur["disk"]:
                    t.v.setText("needs Root")
                    continue
                r = m.drive_rates(d["devs"])
                t.v.setText(f"{r['busy']:.0f} % · R {E.human_bytes(r['read'], True)} W {E.human_bytes(r['write'], True)}")
                hist = [max(vals) for vals in zip(*[h[f"disk:{x}"] for x in d["devs"] if h[f"disk:{x}"]])] if any(
                    h[f"disk:{x}"] for x in d["devs"]) else []
                t.graph.set((hist, C_DISK, ""))
        if "net" in self.tiles:
            ifs = m.interfaces()
            rx = sum(m.net_rates.get(n, {}).get("rx", 0) for n in ifs if not n.startswith("tun"))
            tx = sum(m.net_rates.get(n, {}).get("tx", 0) for n in ifs if not n.startswith("tun"))
            main = ifs[0] if ifs else "—"
            self.tiles["net"].v.setText(f"{main} · ↓ {E.human_bytes(rx, True)} ↑ {E.human_bytes(tx, True)}")
            self.tiles["net"].graph.vmax = None
            self.tiles["net"].graph.set((list(h[f"rx:{main}"]), C_NET, ""), (list(h[f"tx:{main}"]), "#f0c040", ""))
        if "gpu" in self.tiles:
            g = m.cur["gpu"]
            self.tiles["gpu"].v.setText((f"{g['busy']:.0f} %" + (f"  {g['mhz']:.0f} MHz" if "mhz" in g else ""))
                                        if "busy" in g else ("needs Root" if not self.static.get("root") else "not reported"))
            self.tiles["gpu"].graph.set((list(h["gpu"]), C_GPU, ""))
        if "temp" in self.tiles:
            ct = m.cpu_temps()
            self.tiles["temp"].v.setText(f"CPU {max(ct):.0f} °C" if ct else
                                         (f"max {max(m.cur['therm'].values()):.0f} °C" if m.cur["therm"] else "needs Root"))
            self.tiles["temp"].graph.vmax = 100.0
            self.tiles["temp"].graph.set((list(h["temp"]), C_TEMP, ""))

    def update_perf(self):
        m = self.model
        if not m or not m.prev or self.tabs.currentIndex() != 2:
            return
        k, h, st, cur = self.perf_key, m.hist, self.static, m.cur
        self.perf_stack.setCurrentIndex(1 if k == "cpu" and self.cpu_view.currentIndex() == 1 else 0)
        self.membar.setVisible(k == "mem")
        self.big2.setVisible(k in ("drive", "net", "cpu", "mem", "gpu") or k.startswith("drive"))
        stats = []
        if k == "cpu":
            self.perf_title.setText("CPU")
            self.perf_sub.setText(f"{st.get('soc_vendor', '')} {st.get('soc_model') or st.get('hardware', '')}")
            self.big1.colour = C_CPU
            self.big1.vmax = 100.0
            self.big1.title = "% utilization over the last " + f"{int(E.HIST * m.dt)} s"
            self.big1.set((list(h["cpu"]), C_CPU, ""))
            freqs = cur["freq"]
            self.big2.colour = C_CPU
            self.big2.vmax = 100.0
            self.big2.title = "Per-core utilization"
            cols = ["#3e8ed0", "#46a758", "#f0c040", "#e5484d", "#a070e0", "#3fb8c4", "#e08a3c", "#d0d0d0"]
            self.big2.set(*[(list(h[f"core{i}"]), cols[i % len(cols)], f"cpu{i}") for i in range(min(m.ncpu, 8))])
            maxk = st.get("max_khz", {})
            clusters: dict[int, list[int]] = {}
            for c, mx in maxk.items():
                clusters.setdefault(mx, []).append(c)
            ups = time.time() - st.get("btime", time.time())
            procs = cur["procs"]
            stats = [("Utilization", f"{m.cpu_pct.get('all', 0):.0f} %"),
                     ("Speed", f"{max(freqs.values(), default=0) / 1e6:.2f} GHz (fastest core)"),
                     ("Processes", len(procs)), ("Threads", sum(p["threads"] for p in procs.values())),
                     ("Running now", cur.get("running", "")), ("Up time", fmt_time(int(ups * 100), 100)),
                     ("Logical processors", m.ncpu), ("Online", cur.get("online", "")),
                     ("Clusters", " · ".join(f"{len(c)}× up to {mx / 1e6:.2f} GHz" for mx, c in sorted(clusters.items()))),
                     ("Kernel", st.get("kernel", ""))]
            if m.dt and m.prev.get("ctxt"):
                stats.insert(4, ("Context switches", f"{(cur.get('ctxt', 0) - m.prev.get('ctxt', 0)) / m.dt:,.0f}/s"))
            if self.cpu_view.currentIndex() == 1:
                self.update_core_grid()
        elif k == "mem":
            mi = cur["mem"]
            tot = mi.get("MemTotal", 1)
            self.perf_title.setText("Memory")
            self.perf_sub.setText(f"{E.human_bytes(tot)} RAM")
            self.big1.colour = C_MEM
            self.big1.vmax = 100.0
            self.big1.title = "Memory usage"
            self.big1.set((list(h["mem"]), C_MEM, ""))
            swap_used = mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)
            self.big2.colour = C_MEM
            self.big2.vmax = 100.0
            self.big2.title = "Memory in use (%)"
            self.big2.set((list(h["mem"]), C_MEM, "in use"))
            cached = mi.get("Cached", 0) + mi.get("Buffers", 0) - mi.get("Dirty", 0)
            free = mi.get("MemFree", 0)
            dirty = mi.get("Dirty", 0)
            used = max(0, tot - free - cached - dirty)
            self.membar.parts = [("In use", used, "#a070e0"), ("Modified", dirty, "#e08a3c"),
                                 ("Cached", cached, "#3e8ed0"), ("Free", free, "#7d8790")]
            self.membar.update()
            stats = [("In use", E.human_bytes(m.mem_used)), ("Available", E.human_bytes(mi.get("MemAvailable", 0))),
                     ("Cached", E.human_bytes(mi.get("Cached", 0))), ("Free", E.human_bytes(free)),
                     ("Buffers", E.human_bytes(mi.get("Buffers", 0))),
                     ("Swap (zram) used", f"{E.human_bytes(swap_used)} of {E.human_bytes(mi.get('SwapTotal', 0))}"
                      + (f" · {st.get('zram_algo')}" if st.get("zram_algo") else "")),
                     ("Kernel stack + slab", E.human_bytes(mi.get("KernelStack", 0) + mi.get("Slab", 0))),
                     ("Page tables", E.human_bytes(mi.get("PageTables", 0))),
                     ("Shared (shmem)", E.human_bytes(mi.get("Shmem", 0))),
                     ("ION / DMA-BUF", E.human_bytes(mi.get("ION_heap", 0) + mi.get("DmaBufTotal", 0)) if (
                         "ION_heap" in mi or "DmaBufTotal" in mi) else "—")]
        elif k.startswith("drive"):
            i = int(k[5:])
            drives = m.drives()
            d = drives[i] if i < len(drives) else {"name": "", "kind": "", "size": 0, "devs": []}
            self.perf_title.setText(f"Storage {i} ({d['kind']})")
            self.perf_sub.setText(d["name"])
            r = m.drive_rates(d["devs"])
            hist = [max(vals) for vals in zip(*[h[f"disk:{x}"] for x in d["devs"] if h[f"disk:{x}"]])] if any(
                h[f"disk:{x}"] for x in d["devs"]) else []
            self.big1.colour = C_DISK
            self.big1.vmax = 100.0
            self.big1.title = "Active time"
            self.big1.set((hist, C_DISK, ""))
            rd = [sum(v) for v in zip(*[h[f"diskr:{x}"] for x in d["devs"] if h[f"diskr:{x}"]])] if d["devs"] else []
            wr = [sum(v) for v in zip(*[h[f"diskw:{x}"] for x in d["devs"] if h[f"diskw:{x}"]])] if d["devs"] else []
            self.big2.colour = C_DISK
            self.big2.vmax = None
            self.big2.title = "Disk transfer rate"
            self.big2.set((rd, C_DISK, "read"), (wr, "#e08a3c", "write"), unit="B/s")
            data = next((x for x in st.get("df", []) if x["mount"] == "/data"), None)
            stats = [("Active time", f"{r['busy']:.0f} %"), ("Read speed", E.human_bytes(r["read"], True)),
                     ("Write speed", E.human_bytes(r["write"], True)),
                     ("Reads / writes", f"{r['rio']:.0f}/s · {r['wio']:.0f}/s"), ("Queue (in flight)", r["queue"]),
                     ("Capacity", E.human_bytes(d["size"])), ("Type", d["kind"]),
                     ("Logical units", ", ".join(d["devs"]))]
            if data:
                stats.append(("User data (/data)", f"{E.human_bytes(data['used'])} used of {E.human_bytes(data['size'])}"))
            if not cur["disk"]:
                stats.append(("Note", "disk statistics need Root"))
        elif k == "net":
            ifs = m.interfaces()
            name = ifs[0] if ifs else ""
            self.perf_title.setText("Network")
            self.perf_sub.setText(", ".join(ifs[:6]))
            self.big1.colour = C_NET
            self.big1.vmax = None
            self.big1.title = f"Throughput – {name}"
            self.big1.set((list(h[f"rx:{name}"]), C_NET, "receive"), (list(h[f"tx:{name}"]), "#f0c040", "send"),
                          unit="B/s")
            other = ifs[1] if len(ifs) > 1 else ""
            self.big2.colour = C_NET
            self.big2.vmax = None
            self.big2.title = f"Throughput – {other}" if other else ""
            self.big2.set((list(h[f"rx:{other}"]), C_NET, "receive"), (list(h[f"tx:{other}"]), "#f0c040", "send"),
                          unit="B/s")
            for n in ifs[:8]:
                r = m.net_rates.get(n, {})
                stats.append((n, f"↓ {E.human_bytes(r.get('rx', 0), True)} ↑ {E.human_bytes(r.get('tx', 0), True)} · "
                                 f"total ↓ {E.human_bytes(r.get('rx_total', 0))} ↑ {E.human_bytes(r.get('tx_total', 0))}"))
        elif k == "gpu":
            g = cur["gpu"]
            self.perf_title.setText("GPU")
            self.perf_sub.setText(st.get("gpu_model", ""))
            self.big1.colour = C_GPU
            self.big1.vmax = 100.0
            self.big1.title = "GPU utilization"
            self.big1.set((list(h["gpu"]), C_GPU, ""))
            self.big2.setVisible(False)
            stats = [("Utilization", f"{g['busy']:.0f} %" if "busy" in g else "not available"),
                     ("Clock", f"{g['mhz']:.0f} MHz" if "mhz" in g else "—"),
                     ("Max clock", f"{st.get('gpu_max_mhz')} MHz" if st.get("gpu_max_mhz") else "—"),
                     ("Source", cur.get("gpu_kind", ""))]
            if "busy" not in g and not st.get("root"):
                stats.append(("Note", "GPU load needs Root on this device"))
        elif k == "temp":
            types = st.get("therm_types", {})
            self.perf_title.setText("Temperatures")
            self.perf_sub.setText(f"{len(cur['therm'])} sensors")
            self.big1.colour = C_TEMP
            self.big1.vmax = 100.0
            self.big1.title = "Hottest CPU sensor (°C)"
            self.big1.set((list(h["temp"]), C_TEMP, ""), right="100 °C")
            self.big2.setVisible(False)
            bat = cur.get("bat", {})
            if bat:
                try:
                    cur_ma = int(bat.get("current_now", 0))
                    cur_ma = cur_ma / 1000 if abs(cur_ma) > 20000 else cur_ma
                    volt = int(bat.get("voltage_now", 0)) / 1e6
                    stats.append(("Battery", f"{bat.get('capacity', '?')} % · {bat.get('status', '')} · "
                                             f"{cur_ma:.0f} mA · {volt:.2f} V · {abs(cur_ma) * volt / 1000:.2f} W"))
                except ValueError:
                    pass
            for z, c in sorted(cur["therm"].items(), key=lambda kv: -kv[1])[:24]:
                stats.append((types.get(z, f"zone {z}"), f"{c:.1f} °C"))
            if not cur["therm"]:
                stats.append(("Note", "temperatures need Root on this device"))
        cells = "".join(f"<tr><td style='color:gray;padding-right:14px'>{a}</td><td><b>{b}</b></td></tr>" for a, b in stats)
        half = (len(stats) + 1) // 2
        left = "".join(f"<tr><td style='color:gray;padding-right:14px'>{a}</td><td><b>{b}</b></td></tr>" for a, b in stats[:half])
        right = "".join(f"<tr><td style='color:gray;padding-right:14px'>{a}</td><td><b>{b}</b></td></tr>" for a, b in stats[half:])
        self.perf_stats.setText(f"<table width='100%'><tr><td valign='top'><table>{left}</table></td>"
                                f"<td valign='top'><table>{right}</table></td></tr></table>" if len(stats) > 6 else
                                f"<table>{cells}</table>")

    def update_core_grid(self):
        m = self.model
        n = m.ncpu
        if len(self.core_graphs) != n:
            for g, l in self.core_graphs:
                g.setParent(None)
                l.setParent(None)
            self.core_graphs = []
            cols = 4 if n > 4 else n
            for i in range(n):
                g = Graph(C_CPU)
                g.setMinimumHeight(90)
                lab = QLabel("")
                lab.setStyleSheet("color: gray;")
                self.core_grid.addWidget(g, (i // cols) * 2, i % cols)
                self.core_grid.addWidget(lab, (i // cols) * 2 + 1, i % cols)
                self.core_graphs.append((g, lab))
        for i, (g, lab) in enumerate(self.core_graphs):
            g.title = f"cpu{i}"
            g.set((list(m.hist[f"core{i}"]), C_CPU, ""))
            f = m.cur["freq"].get(i)
            mx = self.static.get("max_khz", {}).get(i)
            lab.setText(f"cpu{i}: {m.cpu_pct.get(i, 0):.0f} % · {f / 1e6:.2f} GHz" + (f" of {mx / 1e6:.2f}" if mx else "")
                        if f else f"cpu{i}: offline")

    # ================================================================== Resource Monitor tab
    def build_resmon(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.rm = QTabWidget()
        lay.addWidget(self.rm)
        # CPU
        cw = QSplitter(Qt.Orientation.Horizontal)
        self.rm_cpu = make_table(["Image", "PID", "Status", "Threads", "CPU %", "Average CPU % (1 min)"])
        cw.addWidget(self.rm_cpu)
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        self.rm_cpu_graph = Graph(C_CPU)
        self.rm_cpu_graph.title = "CPU – total"
        sl.addWidget(self.rm_cpu_graph)
        self.rm_freq = QLabel("")
        self.rm_freq.setWordWrap(True)
        sl.addWidget(self.rm_freq)
        sl.addStretch(1)
        cw.addWidget(side)
        cw.setSizes([800, 450])
        self.rm.addTab(cw, "CPU")
        # Memory
        mw = QWidget()
        ml = QVBoxLayout(mw)
        self.rm_membar = MemBar()
        ml.addWidget(self.rm_membar)
        msplit = QSplitter(Qt.Orientation.Horizontal)
        self.rm_mem = make_table(["Image", "PID", "Hard faults/s", "Working set (RSS)", "Virtual", "Threads"])
        msplit.addWidget(self.rm_mem)
        self.rm_meminfo = make_table(["meminfo", "Value"])
        msplit.addWidget(self.rm_meminfo)
        msplit.setSizes([800, 400])
        ml.addWidget(msplit, 1)
        self.rm.addTab(mw, "Memory")
        # Disk
        dw = QSplitter(Qt.Orientation.Vertical)
        self.rm_disk = make_table(["Image", "PID", "Read/s", "Write/s", "Total/s", "Read total", "Written total"])
        dw.addWidget(self.rm_disk)
        self.rm_devs = make_table(["Device", "Model", "Size", "Active %", "Read/s", "Write/s", "Reads/s", "Writes/s",
                                   "Queue"])
        dw.addWidget(self.rm_devs)
        self.rm_fs = make_table(["Path", "Filesystem", "Size", "Used", "Free", "Use %"])
        dw.addWidget(self.rm_fs)
        dw.setSizes([420, 200, 160])
        self.rm.addTab(dw, "Disk")
        # Network
        nw = QSplitter(Qt.Orientation.Vertical)
        self.rm_ifs = make_table(["Interface", "Receive/s", "Send/s", "Received total", "Sent total"])
        nw.addWidget(self.rm_ifs)
        self.rm_tcp = make_table(["App / user", "UID", "Protocol", "Local address", "Local port", "Remote address",
                                  "Remote port", "State"])
        nw.addWidget(self.rm_tcp)
        self.rm_listen = make_table(["App / user", "UID", "Protocol", "Address", "Port"])
        nw.addWidget(self.rm_listen)
        nw.setSizes([150, 380, 220])
        self.rm.addTab(nw, "Network")
        # Thermal & battery
        tw = QWidget()
        tl = QVBoxLayout(tw)
        self.rm_bat = QLabel("")
        tl.addWidget(self.rm_bat)
        self.rm_therm = make_table(["Zone", "Sensor", "°C"])
        tl.addWidget(self.rm_therm, 1)
        self.rm.addTab(tw, "Thermal & battery")
        self.rm.currentChanged.connect(lambda _i: self.update_resmon())
        for t in (self.rm_cpu, self.rm_mem, self.rm_disk):
            t.setColumnWidth(0, 300)
            t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            t.customContextMenuRequested.connect(lambda pos, t=t: self.menu_for(self.table_selection(t, 1), t, pos))
        self.tabs.addTab(w, "Resource Monitor")

    def uid_label(self, uid: int) -> str:
        pk = self.static.get("packages", {}).get(uid % 100000 if uid % 100000 >= 10000 else uid)
        if pk:
            lab = self.label(pk[0])
            return (lab + f" ({pk[0]})" if lab else pk[0]) + (f" +{len(pk) - 1}" if len(pk) > 1 else "")
        return E.user_name(uid, {})

    def update_resmon(self):
        m = self.model
        if not m or not m.prev or self.tabs.currentIndex() != 3:
            return
        cur = m.cur
        i = self.rm.currentIndex()
        procs = [dict(p, pid=pid) for pid, p in cur["procs"].items()]
        if i == 0:
            rows = [[p["name"], (str(p["pid"]), p["pid"]), E.STATE_NAMES.get(p["state"], p["state"]),
                     (str(p["threads"]), p["threads"]), (f"{p.get('cpu', 0):.2f}", p.get("cpu", 0)),
                     (f"{p.get('cpu_avg', 0):.2f}", p.get("cpu_avg", 0))]
                    for p in sorted(procs, key=lambda p: -p.get("cpu", 0)) if self.proc_group(p) != "kernel" or p.get("cpu")]
            fill(self.rm_cpu, rows, 1, heats={4: 25.0, 5: 25.0})
            self.rm_cpu_graph.set((list(m.hist["cpu"]), C_CPU, ""))
            self.rm_freq.setText("<br>".join(
                f"cpu{c}: <b>{m.cpu_pct.get(c, 0):3.0f} %</b> at {f / 1e6:.2f} GHz"
                for c, f in sorted(cur["freq"].items())))
        elif i == 1:
            mi = cur["mem"]
            cached = mi.get("Cached", 0) + mi.get("Buffers", 0) - mi.get("Dirty", 0)
            free = mi.get("MemFree", 0)
            dirty = mi.get("Dirty", 0)
            used = max(0, mi.get("MemTotal", 0) - free - cached - dirty)
            self.rm_membar.parts = [("In use", used, "#a070e0"), ("Modified", dirty, "#e08a3c"),
                                    ("Cached", cached, "#3e8ed0"), ("Free", free, "#7d8790")]
            self.rm_membar.update()
            rows = [[p["name"], (str(p["pid"]), p["pid"]), (f"{p.get('faults', 0):.0f}", p.get("faults", 0)),
                     (E.human_bytes(p["mem"]), p["mem"]), (E.human_bytes(p["vsize"]), p["vsize"]),
                     (str(p["threads"]), p["threads"])] for p in procs if p["mem"]]
            fill(self.rm_mem, rows, 1, heats={3: mi.get("MemTotal", 1) * 0.1})
            fill(self.rm_meminfo, [[k, (E.human_bytes(v), v)] for k, v in mi.items()])
        elif i == 2:
            if cur["has_io"]:
                rows = [[p["name"], (str(p["pid"]), p["pid"]), (E.human_bytes(p.get("io_rs", 0), True), p.get("io_rs", 0)),
                         (E.human_bytes(p.get("io_ws", 0), True), p.get("io_ws", 0)),
                         (E.human_bytes(p.get("io_rs", 0) + p.get("io_ws", 0), True), p.get("io_rs", 0) + p.get("io_ws", 0)),
                         (E.human_bytes(p.get("io_r", 0)), p.get("io_r", 0)), (E.human_bytes(p.get("io_w", 0)), p.get("io_w", 0))]
                        for p in procs if p.get("io_r") or p.get("io_w")]
            else:
                rows = [["Per-process disk activity needs Root", ("", 0), "", "", "", "", ""]]
            fill(self.rm_disk, rows, 1, heats={4: 5 * 1024 * 1024})
            info = {d["dev"]: d for d in self.static.get("disks", [])}
            drows = []
            for n in m.whole_disks():
                r = m.disk_rates.get(n, {})
                d = info.get(n, {})
                drows.append([n, " ".join(x for x in (d.get("vendor"), d.get("model")) if x),
                              (E.human_bytes(d.get("size", 0)), d.get("size", 0)), (f"{r.get('busy', 0):.0f}", r.get("busy", 0)),
                              (E.human_bytes(r.get("read", 0), True), r.get("read", 0)),
                              (E.human_bytes(r.get("write", 0), True), r.get("write", 0)),
                              (f"{r.get('rio', 0):.0f}", r.get("rio", 0)), (f"{r.get('wio', 0):.0f}", r.get("wio", 0)),
                              (str(r.get("queue", 0)), r.get("queue", 0))])
            fill(self.rm_devs, drows or [["Disk statistics need Root", "", "", "", "", "", "", "", ""]])
            fill(self.rm_fs, [[d["mount"], d["fs"], (E.human_bytes(d["size"]), d["size"]), (E.human_bytes(d["used"]), d["used"]),
                               (E.human_bytes(d["free"]), d["free"]),
                               (f"{100 * d['used'] / d['size']:.0f} %" if d["size"] else "", d["used"] / d["size"] if d["size"] else 0)]
                              for d in self.static.get("df", [])])
        elif i == 3:
            fill(self.rm_ifs, [[n, (E.human_bytes(r["rx"], True), r["rx"]), (E.human_bytes(r["tx"], True), r["tx"]),
                                (E.human_bytes(r["rx_total"]), r["rx_total"]), (E.human_bytes(r["tx_total"]), r["tx_total"])]
                               for n, r in sorted(m.net_rates.items(), key=lambda kv: -(kv[1]["rx_total"] + kv[1]["tx_total"]))
                               if r["rx_total"] + r["tx_total"] > 0])
            socks = cur["sockets"]
            conns = [s for s in socks if s["proto"].startswith("TCP") and s["state"] != "LISTEN"]
            listen = [s for s in socks if s["state"] == "LISTEN" or (s["proto"].startswith("UDP") and s["state"] == "UNCONN")]
            fill(self.rm_tcp, [[self.uid_label(s["uid"]), (str(s["uid"]), s["uid"]), s["proto"], s["local"], (str(s["lport"]), s["lport"]),
                                s["remote"], (str(s["rport"]), s["rport"]), s["state"]] for s in conns])
            fill(self.rm_listen, [[self.uid_label(s["uid"]), (str(s["uid"]), s["uid"]), s["proto"], s["local"],
                                   (str(s["lport"]), s["lport"])] for s in listen])
        elif i == 4:
            types = self.static.get("therm_types", {})
            fill(self.rm_therm, [[(str(z), z), types.get(z, ""), (f"{c:.1f}", c)] for z, c in sorted(cur["therm"].items())]
                 or [["", "Temperatures need Root on this device", ""]])
            bat = cur.get("bat", {})
            try:
                cur_ma = int(bat.get("current_now", 0))
                cur_ma = cur_ma / 1000 if abs(cur_ma) > 20000 else cur_ma
                volt = int(bat.get("voltage_now", 0)) / 1e6
                self.rm_bat.setText(f"Battery {bat.get('capacity', '?')} % · {bat.get('status', '')} · current "
                                    f"{cur_ma:.0f} mA · {volt:.3f} V · power {abs(cur_ma) * volt / 1000:.2f} W")
            except ValueError:
                self.rm_bat.setText("")

    # ================================================================== process actions
    def act_selected(self, what: str):
        sel = self.tree_selection() if self.tabs.currentIndex() == 0 else self.table_selection(self.details, 1)
        if sel:
            self.do_action(what, sel)
        else:
            self.api.message("Select a process first")

    def menu_for(self, sel: list[dict], widget, pos):
        if not sel:
            return
        menu = QMenu(widget)
        one = sel[0]
        is_app = self.proc_group(one) == "app"
        title = (self.label(self.app_key(one)) or self.app_key(one)) if len(sel) > 1 or is_app else one["name"]
        hdr = QAction(f"{title}" + (f" ({len(sel)} processes)" if len(sel) > 1 else f" – PID {one['pid']}"), menu)
        hdr.setEnabled(False)
        menu.addAction(hdr)
        menu.addSeparator()
        for label, what in (("End task", "end"), ("Kill (force)", "kill"), ("End process tree", "tree"),
                            ("Suspend", "stop"), ("Resume", "cont")):
            menu.addAction(label, lambda w=what: self.do_action(w, sel))
        pm = menu.addMenu("Set priority")
        for label, n in PRIORITIES:
            pm.addAction(label, lambda n=n: self.do_action("nice", sel, n))
        menu.addSeparator()
        if len(sel) == 1:
            menu.addAction("Properties…", lambda: self.properties(one))
        menu.addAction("Copy name", lambda: QApplication.clipboard().setText(title))
        menu.addAction("Copy PID(s)", lambda: QApplication.clipboard().setText(" ".join(str(p["pid"]) for p in sel)))
        menu.exec(widget.viewport().mapToGlobal(pos))

    def descendants(self, pids: set[int]) -> list[int]:
        procs = self.model.cur["procs"]
        out, todo = [], list(pids)
        while todo:
            x = todo.pop()
            for pid, p in procs.items():
                if p["ppid"] == x and pid not in out and pid not in pids:
                    out.append(pid)
                    todo.append(pid)
        return out

    def do_action(self, what: str, sel: list[dict], arg: int = 0):
        sel = [p for p in sel if p["pid"] > 2]
        if not sel:
            return
        kernel = [p for p in sel if self.proc_group(p) == "kernel"]
        if kernel and what in ("end", "kill", "tree", "stop"):
            QMessageBox.information(self, "Task Manager", "Kernel threads can't be ended or suspended.")
            return
        is_app = self.proc_group(sel[0]) == "app"
        pkg = self.app_key(sel[0]) if is_app else ""
        names = ", ".join(sorted({p["name"] for p in sel}))[:300]
        pids = [p["pid"] for p in sel]
        crit = sorted({p["name"] for p in sel if p["name"] in CRITICAL or p["pid"] == 1})
        verb = {"end": "End", "kill": "Kill", "tree": "End the process tree of", "stop": "Suspend", "cont": "Resume",
                "nice": f"Set priority {arg:+d} for"}[what]
        if what == "tree":
            pids = pids + self.descendants(set(pids))
        msg = f"{verb} {names} (PID {', '.join(map(str, pids[:20]))}{'…' if len(pids) > 20 else ''})?"
        if crit and what in ("end", "kill", "tree", "stop"):
            msg += (f"\n\n⚠ {', '.join(crit)} is a core part of Android. Ending or suspending it restarts or freezes the "
                    "whole system (all apps close, the screen may go black for a while).")
        if what == "stop":
            msg += "\n\nA suspended process stops completely until you choose Resume (apps may show 'not responding')."
        if QMessageBox.question(self, "Task Manager", msg) != QMessageBox.StandardButton.Yes:
            return
        if what == "end" and is_app and "." in pkg:
            cmd = f"am force-stop {E.shq(pkg)}"
        elif what in ("end", "tree"):
            cmd = f"kill -15 {' '.join(map(str, pids))}"
        elif what == "kill":
            cmd = f"kill -9 {' '.join(map(str, pids))}"
        elif what == "stop":
            cmd = f"kill -STOP {' '.join(map(str, pids))}"
        elif what == "cont":
            cmd = f"kill -CONT {' '.join(map(str, pids))}"
        else:
            cmd = f"renice -n {arg} -p {' '.join(map(str, pids))}"

        def work():
            rc, out, err = self.dev.run(cmd, timeout=30)
            text = (out + err).strip()
            if rc != 0 and self.dev.root_ok() and not self.dev.use_root:
                rc, out, err = self.dev.run(cmd, timeout=30, root=True)
                text = (out + err).strip()
            if rc != 0:
                raise RuntimeError(text or f"exit code {rc}" + ("" if self.dev.use_root else " – this usually needs Root"))
            return text

        self.api.task(work, lambda _t: self.api.message(f"{verb} {names}: done"),
                      lambda m: QMessageBox.warning(self, "Task Manager", f"{verb} {names} failed:\n\n{m}"))

    def properties(self, p: dict):
        pid = p["pid"]
        cmd = (f"cat /proc/{pid}/status 2>&1; echo '@@CMD'; tr '\\0' ' ' < /proc/{pid}/cmdline 2>/dev/null; echo; "
               f"echo '@@OOM'; cat /proc/{pid}/oom_score_adj /proc/{pid}/oom_score 2>/dev/null; echo '@@CG'; "
               f"cat /proc/{pid}/cgroup 2>/dev/null; echo '@@FD'; ls /proc/{pid}/fd 2>/dev/null | wc -l; "
               f"echo '@@EXE'; readlink /proc/{pid}/exe 2>/dev/null; echo '@@TASK'; "
               f"for t in /proc/{pid}/task/*; do cat $t/stat 2>/dev/null; echo; done | head -400")

        def work():
            return self.dev.run(cmd, timeout=30)[1]

        def done(text):
            d = QDialog(self)
            d.setWindowTitle(f"Properties – {p['name']} ({pid})")
            d.resize(780, 620)
            lay = QVBoxLayout(d)
            tabs = QTabWidget()
            lay.addWidget(tabs)
            sec = {"STATUS": []}
            cur = "STATUS"
            for ln in text.splitlines():
                if ln.startswith("@@"):
                    cur = ln[2:]
                    sec[cur] = []
                else:
                    sec[cur].append(ln)
            st = dict((ln.split(":", 1)[0], ln.split(":", 1)[1].strip()) for ln in sec["STATUS"] if ":" in ln)
            mdl = self.model
            info = [("Name", st.get("Name", p["name"])), ("Process name", p["name"]),
                    ("Command line", " ".join(sec.get("CMD", [])).strip()), ("Executable", " ".join(sec.get("EXE", []))),
                    ("PID / parent", f"{pid} / {st.get('PPid', '')}"), ("User", f"{self.user_of(p)} (uid {p['uid']})"),
                    ("App", self.uid_label(p["uid"]) if p["uid"] % 100000 >= 10000 else "—"),
                    ("State", st.get("State", "")), ("Threads", st.get("Threads", "")),
                    ("CPU now", f"{p.get('cpu', 0):.1f} %"), ("CPU time", fmt_time(p["ticks"], mdl.hz)),
                    ("Memory (RSS)", E.human_bytes(p["mem"])), ("Peak RSS", st.get("VmHWM", "")),
                    ("Swapped", st.get("VmSwap", "")), ("Virtual", E.human_bytes(p["vsize"])),
                    ("Priority / nice", f"{p['prio']} / {p['nice']}"),
                    ("OOM score adj / score", " / ".join(x.strip() for x in sec.get("OOM", []) if x.strip())),
                    ("Open files", " ".join(sec.get("FD", [])).strip() or "needs Root"),
                    ("Voluntary / involuntary switches", f"{st.get('voluntary_ctxt_switches', '')} / "
                                                         f"{st.get('nonvoluntary_ctxt_switches', '')}"),
                    ("SELinux / cgroups", " · ".join(x.split(":")[-1] for x in sec.get("CG", []) if x.strip())[:200])]
            g = make_table(["Property", "Value"])
            fill(g, [[a, b] for a, b in info])
            g.setColumnWidth(0, 230)
            tabs.addTab(g, "General")
            th = make_table(["TID", "Thread name", "State", "CPU time", "Priority", "Nice", "Core"])
            rows = []
            for ln in sec.get("TASK", []):
                a, b = ln.find("("), ln.rfind(")")
                if a < 0 or b < 0:
                    continue
                r = ln[b + 2:].split()
                try:
                    ticks = int(r[11]) + int(r[12])
                    rows.append([(ln[:a].strip(), int(ln[:a])), ln[a + 1:b], E.STATE_NAMES.get(r[0], r[0]),
                                 (fmt_time(ticks, mdl.hz), ticks), (r[15], int(r[15])), (r[16], int(r[16])),
                                 (r[36], int(r[36])) if len(r) > 36 else ""])
                except (ValueError, IndexError):
                    continue
            fill(th, rows)
            tabs.addTab(th, f"Threads ({len(rows)})")
            raw = QPlainTextEdit("\n".join(sec["STATUS"]))
            raw.setReadOnly(True)
            tabs.addTab(raw, "/proc status")
            d.show()

        self.api.task(work, done)

    def closeEvent(self, e):
        if getattr(self, "rec", None) is not None and getattr(self, "rec_stress_until", 0) > time.time():
            from . import thermal as TH
            dev = self.dev
            threading.Thread(target=lambda: dev.run(TH.stop_stress_cmd(), timeout=30), daemon=True).start()
        if self.sampler:
            self.sampler.stop()
            dev, token = self.dev, self.token
            threading.Thread(target=lambda: (time.sleep(1.0), dev.kill_leftovers(token)), daemon=True).start()
        super().closeEvent(e)
