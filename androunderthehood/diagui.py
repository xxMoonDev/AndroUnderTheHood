"""Modem (Qualcomm DIAG) window: NV items, EFS files, live modem log, raw command console."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import posixpath
import re
import threading
from collections import Counter

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton, QSpinBox, QSplitter, QTableView,
                               QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import backend as B
from . import diag as D
from . import theme

log = logging.getLogger("androunderthehood")
MONO = QFont("Consolas", 9)


def mono_edit(read_only=False) -> QPlainTextEdit:
    e = QPlainTextEdit()
    e.setFont(MONO)
    e.setReadOnly(read_only)
    e.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    return e


def parse_hex(text: str) -> bytes:
    """Plain hex ("de ad be ef") or AndroUnderTheHood hexdump lines ("000010  de ad …  ascii")."""
    parts = []
    for ln in text.splitlines():
        m = re.match(r"^\s*[0-9a-fA-F]{6,8}  (.*)$", ln)
        if m:
            ln = m.group(1).split("  ")[0]
        parts.append(ln)
    t = re.sub(r"0x|[\s,:]", "", "".join(parts))
    if len(t) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", t):
        raise ValueError("not valid hex")
    return bytes.fromhex(t)


def plain_hex(b: bytes, per_line: int = 16) -> str:
    return "\n".join(b[i:i + per_line].hex(" ") for i in range(0, len(b), per_line))


def decoded(b: bytes) -> str:
    if not b:
        return ""
    parts = [f"u8 {b[0]}"]
    if len(b) >= 2:
        parts.append(f"u16 {int.from_bytes(b[:2], 'little')}")
    if len(b) >= 4:
        parts.append(f"u32 {int.from_bytes(b[:4], 'little')}")
    if len(b) >= 8:
        parts.append(f"u64 0x{int.from_bytes(b[:8], 'little'):x}")
    txt = b.rstrip(b"\0")
    if txt and all(32 <= c < 127 for c in txt):
        parts.append(f'text "{txt.decode()}"')
    return "   ·   ".join(parts)


class _Bridge(QObject):
    records = Signal()


# ---------------------------------------------------------------------------
class LogModel(QAbstractTableModel):
    COLS = ["Time", "Type", "ID / code", "Level", "Source / name", "Message"]
    LIMIT = 60000

    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self.shown: list[dict] = []
        self.filter = ""
        self.kinds = None

    def add(self, recs: list[dict]):
        self.rows.extend(recs)
        drop = len(self.rows) - self.LIMIT
        if drop > 0:
            del self.rows[:drop]
        new = [r for r in recs if self.accept(r)]
        if drop > 0:
            self.refilter()
            return
        if new:
            n = len(self.shown)
            self.beginInsertRows(QModelIndex(), n, n + len(new) - 1)
            self.shown.extend(new)
            self.endInsertRows()

    def accept(self, r):
        if self.kinds and r["kind"] not in self.kinds and not (r["kind"].startswith("0x") and "OTHER" in self.kinds):
            return False
        if self.filter:
            hay = f"{r['kind']} {r['id']} {r['level']} {r['name']} {r['text']}".lower()
            if isinstance(r["id"], int):
                hay += f" 0x{r['id']:04x}"
            return all(w in hay for w in self.filter.split())
        return True

    def refilter(self):
        self.beginResetModel()
        self.shown = [r for r in self.rows if self.accept(r)]
        self.endResetModel()

    def clear(self):
        self.beginResetModel()
        self.rows, self.shown = [], []
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.shown)

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLS)

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLS[s]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        r = self.shown[index.row()]
        c = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if c == 0:
                return r["time"].strftime("%H:%M:%S.%f")[:-3]
            if c == 1:
                return r["kind"]
            if c == 2:
                i = r["id"]
                return f"0x{i:04X}" if r["kind"] == "LOG" else str(i)
            if c == 3:
                return r["level"]
            if c == 4:
                return r["name"]
            return r["text"]
        if role == Qt.ItemDataRole.FontRole and c in (0, 2, 5):
            return MONO
        if role == Qt.ItemDataRole.ForegroundRole:
            if r["level"] in ("ERROR", "FATAL"):
                return QColor("#ff6b6b")
            if r["level"] == "HIGH":
                return QColor("#e5c07b")
            if r["kind"] == "LOG":
                return QColor(theme.ACCENT)
            if r["kind"] == "EVENT":
                return QColor("#7fb4ff")
            if c in (0, 2):
                return QColor(theme.TEXT_DIM)
        return None


# ---------------------------------------------------------------------------
class DiagDialog(QDialog):
    def __init__(self, win, conn: B.Connection):
        super().__init__(win)
        from .ui import Task
        self.Task = Task
        self.win = win
        self.conn = conn
        self.link = D.DiagLink(conn)
        self.backup_dir = os.path.join(B.app_dir(), "modem-backups",
                                       re.sub(r"[^\w.-]+", "_", conn.profile.name) or "device")
        self.setWindowTitle(f"Modem (DIAG) – {conn.profile.name}")
        self.resize(1280, 820)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)
        head = QHBoxLayout()
        self.head = QLabel("Connecting to the modem…")
        self.head.setStyleSheet("font-weight: 600;")
        head.addWidget(self.head, 1)
        self.btn_backups = QPushButton(theme.line_icon("folder-plus"), " Backups folder")
        self.btn_backups.clicked.connect(self.open_backups)
        head.addWidget(self.btn_backups)
        lay.addLayout(head)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_nv(), "NV items")
        self.tabs.addTab(self.build_efs(), "EFS files")
        self.tabs.addTab(self.build_log(), "Live log")
        self.tabs.addTab(self.build_console(), "Console")
        self.tabs.setEnabled(False)
        self.tabs.currentChanged.connect(self.tab_changed)
        lay.addWidget(self.tabs, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(self.status)
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
            b.setDefault(False)
        self.Task.start(self.link.start, self.connected, self.connect_failed)

    # ---- connection ------------------------------------------------------------
    def connected(self, v):
        self.tabs.setEnabled(True)
        self.head.setText(f"Modem: {v.get('ver_dir', '?')}   ·   built {v.get('comp', '?')}   ·   "
                          f"released {v.get('rel', '?')}   ·   CAI rev {v.get('cai_rev', '?')}")
        self.say("Connected over DIAG (diag_socket_log on the phone). Writes to the modem change it permanently – "
                 "AndroUnderTheHood saves a backup of every item/file before writing it.")
        self.efs_root()

    def connect_failed(self, m):
        self.head.setText("Could not connect to the modem")
        self.say(f"{m}\nThis needs a Qualcomm phone with /vendor/bin/diag_socket_log and Root turned on.")

    def say(self, text):
        self.status.setText(text)

    def run(self, fn, done=None, what="Working…"):
        self.say(what)

        def fail(m):
            self.say(f"✘ {m}")
        self.Task.start(fn, done or (lambda _r: None), fail)

    def closeEvent(self, e):
        self.log_running = False
        self.nv_cancel = True
        threading.Thread(target=self.link.close, daemon=True).start()
        super().closeEvent(e)

    def tab_changed(self, i):
        pass

    def open_backups(self):
        os.makedirs(self.backup_dir, exist_ok=True)
        os.startfile(self.backup_dir)  # noqa: S606 (Windows)

    def backup(self, kind: str, name: str, data: bytes) -> str:
        os.makedirs(self.backup_dir, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^\w.-]+", "_", name).strip("_")
        fn = os.path.join(self.backup_dir, f"{kind}_{safe}_{stamp}.bin")
        with open(fn, "wb") as f:
            f.write(data)
        return fn

    # =========================================================================
    # NV items
    # =========================================================================
    def build_nv(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        top.addWidget(QLabel("Item"))
        self.nv_item = QSpinBox()
        self.nv_item.setRange(0, 65535)
        top.addWidget(self.nv_item)
        b = QPushButton(theme.line_icon("download"), " Read")
        b.clicked.connect(lambda: self.nv_scan([self.nv_item.value()]))
        top.addWidget(b)
        top.addSpacing(20)
        top.addWidget(QLabel("Scan"))
        self.nv_from = QSpinBox()
        self.nv_from.setRange(0, 65535)
        self.nv_to = QSpinBox()
        self.nv_to.setRange(0, 65535)
        self.nv_to.setValue(1000)
        top.addWidget(self.nv_from)
        top.addWidget(QLabel("to"))
        top.addWidget(self.nv_to)
        self.btn_scan = QPushButton(theme.line_icon("refresh"), " Scan")
        self.btn_scan.clicked.connect(self.scan_clicked)
        top.addWidget(self.btn_scan)
        kb = QPushButton(" Known items")
        kb.setToolTip("Read the items AndroUnderTheHood has names for")
        kb.clicked.connect(lambda: self.nv_scan(sorted(D.NV_NAMES)))
        top.addWidget(kb)
        self.nv_prog = QProgressBar()
        self.nv_prog.setMaximumWidth(180)
        self.nv_prog.setVisible(False)
        top.addWidget(self.nv_prog)
        top.addStretch(1)
        self.nv_filter = QLineEdit()
        self.nv_filter.setPlaceholderText("Filter…")
        self.nv_filter.setClearButtonEnabled(True)
        self.nv_filter.textChanged.connect(self.nv_apply_filter)
        top.addWidget(self.nv_filter)
        self.nv_only_set = QCheckBox("Set items only")
        self.nv_only_set.setChecked(True)
        self.nv_only_set.toggled.connect(self.nv_apply_filter)
        top.addWidget(self.nv_only_set)
        lay.addLayout(top)

        split = QSplitter(Qt.Orientation.Vertical)
        self.nv_table = QTableWidget(0, 4)
        self.nv_table.setHorizontalHeaderLabels(["Item", "Name", "Status", "Value (trailing zeros trimmed)"])
        self.nv_table.verticalHeader().hide()
        self.nv_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.nv_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.nv_table.setAlternatingRowColors(True)
        self.nv_table.setShowGrid(False)
        self.nv_table.setSortingEnabled(True)
        h = self.nv_table.horizontalHeader()
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.nv_table.setColumnWidth(0, 70)
        self.nv_table.setColumnWidth(1, 170)
        self.nv_table.setColumnWidth(2, 170)
        self.nv_table.itemSelectionChanged.connect(self.nv_selected)
        split.addWidget(self.nv_table)

        det = QWidget()
        dl = QVBoxLayout(det)
        dl.setContentsMargins(0, 6, 0, 0)
        self.nv_title = QLabel("Select an item")
        self.nv_title.setStyleSheet("font-weight: 600;")
        dl.addWidget(self.nv_title)
        self.nv_hex = mono_edit()
        self.nv_hex.setPlaceholderText("128 bytes, hex")
        self.nv_hex.textChanged.connect(self.nv_decode)
        dl.addWidget(self.nv_hex, 1)
        self.nv_dec = QLabel("")
        self.nv_dec.setFont(MONO)
        self.nv_dec.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        dl.addWidget(self.nv_dec)
        row = QHBoxLayout()
        row.addStretch(1)
        for text, icon, fn in ((" Backup shown items…", "save", self.nv_backup_all),
                               (" Restore from backup…", "upload", self.nv_restore),
                               (" Write item", "rename", self.nv_write)):
            bb = QPushButton(theme.line_icon(icon), text)
            bb.clicked.connect(fn)
            row.addWidget(bb)
            if "Write" in text:
                self.btn_nv_write = bb
        dl.addLayout(row)
        split.addWidget(det)
        split.setSizes([480, 260])
        lay.addWidget(split, 1)
        self.nv_data: dict[int, tuple[int, bytes]] = {}
        self.nv_cancel = False
        self.nv_busy = False
        return w

    def scan_clicked(self):
        if self.nv_busy:
            self.nv_cancel = True
            return
        a, b = self.nv_from.value(), self.nv_to.value()
        if b < a:
            a, b = b, a
        self.nv_scan(list(range(a, b + 1)))

    def nv_scan(self, items: list[int]):
        if self.nv_busy:
            return
        self.nv_busy = True
        self.nv_cancel = False
        self.btn_scan.setText(" Stop")
        self.nv_prog.setVisible(len(items) > 1)
        self.nv_prog.setRange(0, len(items))
        bridge = _Bridge()
        self._nv_bridge = bridge
        prog = [0]
        bridge.records.connect(lambda: self.nv_prog.setValue(prog[0]))

        def progress(n, total):
            prog[0] = n
            bridge.records.emit()

        def work():
            return self.link.nv_read_many(items, progress=progress, cancel=lambda: self.nv_cancel)

        def done(res):
            self.nv_busy = False
            self.btn_scan.setText(" Scan")
            self.nv_prog.setVisible(False)
            self.nv_data.update(res)
            self.nv_fill()
            n_set = sum(1 for s, _ in res.values() if s == 0)
            self.say(f"Read {len(res)} item(s): {n_set} set." + (" (stopped)" if self.nv_cancel else ""))
            if len(items) == 1 and items[0] in res:
                self.nv_select(items[0])

        def fail(m):
            self.nv_busy = False
            self.btn_scan.setText(" Scan")
            self.nv_prog.setVisible(False)
            self.say(f"✘ {m}")
        self.say(f"Reading {len(items)} NV item(s)… (about 30 per second)")
        self.Task.start(work, done, fail)

    def nv_fill(self):
        self.nv_table.setSortingEnabled(False)
        self.nv_table.setRowCount(0)
        for item, (st, data) in sorted(self.nv_data.items()):
            r = self.nv_table.rowCount()
            self.nv_table.insertRow(r)
            it = QTableWidgetItem()
            it.setData(Qt.ItemDataRole.DisplayRole, item)
            self.nv_table.setItem(r, 0, it)
            name = D.NV_NAMES.get(item, "")
            if item in D.PROTECTED_NV:
                name += "  (identity – read-only)"
            self.nv_table.setItem(r, 1, QTableWidgetItem(name))
            stext = D.NV_STATUS.get(st, f"error {st}") if st >= 0 else D.ERRORS.get(-st, f"error {-st}")
            si = QTableWidgetItem(stext)
            si.setForeground(QColor(theme.ACCENT if st == 0 else theme.TEXT_FAINT))
            self.nv_table.setItem(r, 2, si)
            vi = QTableWidgetItem(data.rstrip(b"\0").hex(" ") if st == 0 else "")
            vi.setFont(MONO)
            self.nv_table.setItem(r, 3, vi)
        self.nv_table.setSortingEnabled(True)
        self.nv_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.nv_apply_filter()

    def nv_apply_filter(self):
        t = self.nv_filter.text().lower()
        only = self.nv_only_set.isChecked()
        for r in range(self.nv_table.rowCount()):
            cells = [self.nv_table.item(r, c).text().lower() for c in range(4)]
            hide = (only and cells[2] != "ok") or (t and not all(w in " ".join(cells) for w in t.split()))
            self.nv_table.setRowHidden(r, bool(hide))

    def nv_select(self, item):
        for r in range(self.nv_table.rowCount()):
            if self.nv_table.item(r, 0).data(Qt.ItemDataRole.DisplayRole) == item:
                self.nv_table.setRowHidden(r, False)
                self.nv_table.selectRow(r)
                return

    def nv_current(self) -> int | None:
        rows = self.nv_table.selectionModel().selectedRows(0)
        return rows[0].data(Qt.ItemDataRole.DisplayRole) if rows else None

    def nv_selected(self):
        item = self.nv_current()
        if item is None:
            return
        st, data = self.nv_data.get(item, (5, b""))
        self.nv_item.setValue(item)
        self.nv_title.setText(f"NV {item}  {D.NV_NAMES.get(item, '')}  –  "
                              f"{D.NV_STATUS.get(st, st) if st >= 0 else D.ERRORS.get(-st, st)}")
        self.nv_hex.setPlainText(plain_hex(data if st == 0 else b"\0" * 16))
        self.btn_nv_write.setEnabled(item not in D.PROTECTED_NV)

    def nv_decode(self):
        try:
            self.nv_dec.setText(decoded(parse_hex(self.nv_hex.toPlainText())))
        except ValueError:
            self.nv_dec.setText("‹not valid hex›")

    def nv_write(self):
        item = self.nv_item.value()
        try:
            new = parse_hex(self.nv_hex.toPlainText())
        except ValueError as e:
            QMessageBox.warning(self, "NV", str(e))
            return
        if len(new) > 128:
            QMessageBox.warning(self, "NV", "An NV item holds at most 128 bytes.")
            return
        st, old = self.nv_data.get(item, (5, b""))
        if QMessageBox.question(
                self, "Write NV item",
                f"Write NV {item} {D.NV_NAMES.get(item, '')}?\n\nnow:  {old.rstrip(bytes(1)).hex(' ') or '(not set)'}\n"
                f"new:  {new.rstrip(bytes(1)).hex(' ')}\n\nThis changes the modem's stored configuration "
                "(a backup of the current value is saved first). Some items only apply after a reboot.") \
                != QMessageBox.StandardButton.Yes:
            return

        def work():
            st0, cur = self.link.nv_read(item)
            bk = self.backup("nv", str(item), json.dumps({"item": item, "status": st0, "hex": cur.hex()}).encode())
            stw = self.link.nv_write(item, new)
            return bk, stw, self.link.nv_read(item)

        def done(r):
            bk, stw, now = r
            self.nv_data[item] = now
            self.nv_fill()
            self.nv_select(item)
            self.say((f"✔ NV {item} written." if stw == 0 else f"✘ NV {item}: {D.NV_STATUS.get(stw, stw)}") +
                     f"  Backup: {bk}")
        self.run(work, done, f"Writing NV {item}…")

    def nv_backup_all(self):
        rows = [r for r in range(self.nv_table.rowCount()) if not self.nv_table.isRowHidden(r)]
        items = [self.nv_table.item(r, 0).data(Qt.ItemDataRole.DisplayRole) for r in rows]
        if not items:
            return
        os.makedirs(self.backup_dir, exist_ok=True)
        fn, _ = QFileDialog.getSaveFileName(
            self, "Save NV backup", os.path.join(self.backup_dir, f"nv_{dt.datetime.now():%Y%m%d-%H%M%S}.json"),
            "NV backup (*.json)")
        if not fn:
            return
        data = {str(i): {"status": self.nv_data[i][0], "hex": self.nv_data[i][1].hex()} for i in items}
        with open(fn, "w", encoding="utf-8") as f:
            json.dump({"device": self.conn.profile.name, "modem": self.link.version, "items": data}, f, indent=1)
        self.say(f"Saved {len(items)} item(s) to {fn}")

    def nv_restore(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Restore NV backup", self.backup_dir, "NV backup (*.json)")
        if not fn:
            return
        with open(fn, encoding="utf-8") as f:
            js = json.load(f)
        items = {int(k): bytes.fromhex(v["hex"]) for k, v in js.get("items", {}).items()
                 if v.get("status") == 0 and int(k) not in D.PROTECTED_NV}
        if not items:
            QMessageBox.information(self, "NV", "The backup holds no restorable items.")
            return
        if QMessageBox.question(self, "Restore NV items",
                                f"Write {len(items)} NV item(s) from\n{fn}\n\nback to the modem? Identity items "
                                "(IMEI/MEID/ESN) are skipped.") != QMessageBox.StandardButton.Yes:
            return

        def work():
            cur = self.link.nv_read_many(sorted(items))
            self.backup("nv-before-restore", os.path.basename(fn),
                        json.dumps({str(k): {"status": v[0], "hex": v[1].hex()} for k, v in cur.items()}).encode())
            res = {i: self.link.nv_write(i, d) for i, d in items.items()}
            return res, self.link.nv_read_many(sorted(items))

        def done(r):
            res, now = r
            self.nv_data.update(now)
            self.nv_fill()
            bad = {i: D.NV_STATUS.get(s, s) for i, s in res.items() if s != 0}
            self.say(f"Restored {len(res) - len(bad)} item(s)." + (f" Failed: {bad}" if bad else ""))
        self.run(work, done, f"Restoring {len(items)} NV item(s)…")

    # =========================================================================
    # EFS
    # =========================================================================
    def build_efs(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.efs_path = QLineEdit("/")
        self.efs_path.returnPressed.connect(lambda: self.efs_open(self.efs_path.text().strip() or "/"))
        up = QPushButton(theme.line_icon("up"), "")
        up.clicked.connect(lambda: self.efs_open(posixpath.dirname(self.efs_cur.rstrip("/")) or "/"))
        top.addWidget(up)
        top.addWidget(self.efs_path, 1)
        for path in ("/nv/item_files", "/policyman", "/nv/item_files/ims", "/nv/item_files/modem/mmode"):
            b = QPushButton(path.rsplit("/", 1)[-1] or path)
            b.setToolTip(path)
            b.clicked.connect(lambda _c=False, p=path: self.efs_open(p))
            top.addWidget(b)
        rb = QPushButton(theme.line_icon("refresh"), "")
        rb.clicked.connect(lambda: self.efs_open(self.efs_cur))
        top.addWidget(rb)
        lay.addLayout(top)
        self.efs_table = QTableWidget(0, 4)
        self.efs_table.setHorizontalHeaderLabels(["Name", "Type", "Size", "Modified"])
        self.efs_table.verticalHeader().hide()
        self.efs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.efs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.efs_table.setAlternatingRowColors(True)
        self.efs_table.setShowGrid(False)
        self.efs_table.setSortingEnabled(True)
        self.efs_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.efs_table.doubleClicked.connect(self.efs_activate)
        lay.addWidget(self.efs_table, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Double-click a folder to open it, a file to view/edit it."), 1)
        for text, icon, fn in ((" Open / edit", "rename", self.efs_edit_selected),
                               (" Save to PC…", "download", self.efs_save),
                               (" Upload from PC…", "upload", self.efs_upload),
                               (" Delete", "trash", self.efs_delete)):
            b = QPushButton(theme.line_icon(icon), text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        self.efs_cur = "/"
        return w

    def efs_root(self):
        self.efs_open("/")

    def efs_open(self, path: str):
        path = posixpath.normpath(path) if path != "/" else "/"

        def done(ents):
            self.efs_cur = path
            self.efs_path.setText(path)
            t = self.efs_table
            t.setSortingEnabled(False)
            t.setRowCount(0)
            for e in sorted(ents, key=lambda e: (e["type"] != "dir", e["name"].lower())):
                r = t.rowCount()
                t.insertRow(r)
                it = QTableWidgetItem(("📁 " if e["type"] == "dir" else "") + e["name"])
                it.setData(Qt.ItemDataRole.UserRole, e)
                t.setItem(r, 0, it)
                t.setItem(r, 1, QTableWidgetItem(e["type"]))
                si = QTableWidgetItem()
                si.setData(Qt.ItemDataRole.DisplayRole, e["size"] if e["type"] != "dir" else "")
                t.setItem(r, 2, si)
                when = dt.datetime.fromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M") if e["mtime"] > 315964800 else ""
                t.setItem(r, 3, QTableWidgetItem(when))
            t.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
            t.setSortingEnabled(True)
            self.say(f"{path}: {len(ents)} entries")
        self.run(lambda: self.link.efs_list(path), done, f"Listing {path}…")

    def efs_selected(self) -> dict | None:
        rows = self.efs_table.selectionModel().selectedRows(0)
        return rows[0].data(Qt.ItemDataRole.UserRole) if rows else None

    def efs_full(self, e) -> str:
        return posixpath.join(self.efs_cur, e["name"])

    def efs_activate(self, idx):
        e = self.efs_table.item(idx.row(), 0).data(Qt.ItemDataRole.UserRole)
        if e["type"] == "dir":
            self.efs_open(self.efs_full(e))
        else:
            self.efs_edit(self.efs_full(e))

    def efs_edit_selected(self):
        e = self.efs_selected()
        if e and e["type"] != "dir":
            self.efs_edit(self.efs_full(e))

    def efs_edit(self, path: str):
        self.run(lambda: self.link.efs_read(path), lambda data: EfsEditor(self, path, data).show(),
                 f"Reading {path}…")

    def efs_write(self, path: str, data: bytes, after=None):
        def work():
            try:
                old = self.link.efs_read(path)
                bk = self.backup("efs", path, old)
            except D.DiagError:
                bk = "(new file – nothing to back up)"
            self.link.efs_write(path, data)
            check = self.link.efs_read(path)
            if check != data:
                raise D.DiagError(f"{path}: read-back differs after writing")
            return bk

        def done(bk):
            self.say(f"✔ Wrote {len(data)} bytes to {path}.  Backup: {bk}")
            self.efs_open(self.efs_cur)
            if after:
                after()
        self.run(work, done, f"Writing {path}…")

    def efs_save(self):
        e = self.efs_selected()
        if not e or e["type"] == "dir":
            return
        path = self.efs_full(e)
        fn, _ = QFileDialog.getSaveFileName(self, "Save EFS file", os.path.join(os.path.expanduser("~"), e["name"]))
        if not fn:
            return

        def done(data):
            with open(fn, "wb") as f:
                f.write(data)
            self.say(f"Saved {path} ({len(data)} bytes) to {fn}")
        self.run(lambda: self.link.efs_read(path), done, f"Reading {path}…")

    def efs_upload(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Upload a file to the modem EFS")
        if not fn:
            return
        e = self.efs_selected()
        default = self.efs_full(e) if e and e["type"] != "dir" else posixpath.join(self.efs_cur, os.path.basename(fn))
        with open(fn, "rb") as f:
            data = f.read()
        if QMessageBox.question(self, "Upload to EFS",
                                f"Write {len(data)} bytes from\n{fn}\n\nto the modem file\n{default}?\n\n"
                                "An existing file is backed up first and then replaced.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.efs_write(default, data)

    def efs_delete(self):
        e = self.efs_selected()
        if not e or e["type"] == "dir":
            return
        path = self.efs_full(e)
        if QMessageBox.warning(self, "Delete EFS file", f"Delete {path} from the modem?\n\nA backup is saved "
                               "first, but the modem may behave differently until it is restored.",
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                               QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return

        def work():
            bk = self.backup("efs-deleted", path, self.link.efs_read(path))
            self.link.efs_unlink(path)
            return bk
        self.run(work, lambda bk: (self.say(f"Deleted {path}. Backup: {bk}"), self.efs_open(self.efs_cur)),
                 f"Deleting {path}…")

    # =========================================================================
    # Live log
    # =========================================================================
    def build_log(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        r1 = QHBoxLayout()
        self.lg_f3 = QCheckBox("F3 messages")
        self.lg_f3.setChecked(True)
        r1.addWidget(self.lg_f3)
        self.lg_level = QComboBox()
        for label, m in D.F3_LEVELS:
            self.lg_level.addItem(label, m)
        self.lg_level.setCurrentIndex(2)
        r1.addWidget(self.lg_level)
        self.lg_ev = QCheckBox("Events")
        self.lg_ev.setChecked(True)
        r1.addWidget(self.lg_ev)
        r1.addSpacing(16)
        r1.addWidget(QLabel("Log packets:"))
        self.lg_mode = QComboBox()
        self.lg_mode.addItem("Off", "off")
        self.lg_mode.addItem("Signalling (RRC / NAS messages)", "sig")
        self.lg_mode.addItem("Everything for the ticked technologies (heavy)", "all")
        self.lg_mode.setCurrentIndex(1)
        r1.addWidget(self.lg_mode)
        self.lg_tech = {}
        for eq, name in D.EQUIPMENT.items():
            cb = QCheckBox(name)
            cb.setChecked(eq in (0xB, 4, 5, 7))
            self.lg_tech[eq] = cb
            r1.addWidget(cb)
        r1.addStretch(1)
        lay.addLayout(r1)
        r2 = QHBoxLayout()
        self.btn_log = QPushButton(theme.line_icon("forward"), " Start")
        self.btn_log.clicked.connect(self.log_toggle)
        r2.addWidget(self.btn_log)
        self.lg_pause = QCheckBox("Pause view")
        r2.addWidget(self.lg_pause)
        self.lg_scroll = QCheckBox("Auto-scroll")
        self.lg_scroll.setChecked(True)
        r2.addWidget(self.lg_scroll)
        self.lg_show = QComboBox()
        for label, kinds in (("All types", None), ("F3 only", {"F3"}), ("Events only", {"EVENT"}),
                             ("Log packets only", {"LOG"})):
            self.lg_show.addItem(label, kinds)
        self.lg_show.currentIndexChanged.connect(self.log_refilter)
        r2.addWidget(self.lg_show)
        self.lg_filter = QLineEdit()
        self.lg_filter.setPlaceholderText("Filter (words must all match; e.g. 0xB0C0, rrc, error)…")
        self.lg_filter.setClearButtonEnabled(True)
        self.lg_filter.textChanged.connect(self.log_refilter)
        r2.addWidget(self.lg_filter, 1)
        cl = QPushButton(theme.line_icon("broom"), " Clear")
        cl.clicked.connect(lambda: self.lg_model.clear())
        r2.addWidget(cl)
        sv = QPushButton(theme.line_icon("save"), " Save…")
        sv.clicked.connect(self.log_save)
        r2.addWidget(sv)
        self.lg_rate = QLabel("")
        self.lg_rate.setStyleSheet(f"color: {theme.TEXT_DIM};")
        r2.addWidget(self.lg_rate)
        lay.addLayout(r2)
        self.lg_model = LogModel()
        self.lg_view = QTableView()
        self.lg_view.setModel(self.lg_model)
        self.lg_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.lg_view.setAlternatingRowColors(True)
        self.lg_view.setShowGrid(False)
        self.lg_view.setWordWrap(False)
        self.lg_view.verticalHeader().hide()
        self.lg_view.verticalHeader().setDefaultSectionSize(22)
        h = self.lg_view.horizontalHeader()
        for i, wdt in enumerate((100, 60, 80, 60, 230)):
            self.lg_view.setColumnWidth(i, wdt)
        h.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.lg_view.doubleClicked.connect(self.log_detail)
        lay.addWidget(self.lg_view, 1)
        self.lg_pending: list[dict] = []
        self.lg_lock = threading.Lock()
        self.lg_counts = Counter()
        self.log_running = False
        self.lg_timer = QTimer(self)
        self.lg_timer.timeout.connect(self.log_flush)
        return w

    def log_on_frame(self, p: bytes, sub=None):
        recs = D.parse_stream(p, sub)
        with self.lg_lock:
            self.lg_pending.extend(recs)

    def log_toggle(self):
        if self.log_running:
            self.log_running = False
            self.btn_log.setText(" Start")
            self.btn_log.setIcon(theme.line_icon("forward"))
            if self.log_on_frame in self.link.listeners:
                self.link.listeners.remove(self.log_on_frame)
            self.run(self.link.quiet, lambda _r: self.say("Live log stopped (modem masks cleared)."), "Stopping…")
            self.lg_timer.stop()
            self.log_flush()
            return
        f3 = self.lg_level.currentData() if self.lg_f3.isChecked() else 0
        ev = self.lg_ev.isChecked()
        mode = self.lg_mode.currentData()
        techs = [eq for eq, cb in self.lg_tech.items() if cb.isChecked()]

        def work():
            self.link.clear_log_masks()        # start from a clean slate (masks survive in the modem)
            if mode != "off":
                ranges = self.link.log_ranges()
                for eq in techs:
                    if ranges[eq]:
                        self.link.set_log_mask(eq, D.SIGNALLING_CODES if mode == "sig" else None, ranges[eq])
            self.link.set_f3(f3)
            self.link.set_events(ev)
            return True

        def done(_):
            self.log_running = True
            self.link.listeners.append(self.log_on_frame)
            self.btn_log.setText(" Stop")
            self.btn_log.setIcon(theme.line_icon("disconnect"))
            self.lg_timer.start(250)
            self.say("Live log running. Logging stays on in the modem until you press Stop or close this window.")
        self.run(work, done, "Setting modem log masks…")

    def log_flush(self):
        with self.lg_lock:
            recs, self.lg_pending = self.lg_pending, []
        for r in recs:
            self.lg_counts[r["kind"]] += 1
        if recs and not self.lg_pause.isChecked():
            self.lg_model.add(recs)
            if self.lg_scroll.isChecked():
                self.lg_view.scrollToBottom()
        elif recs:
            self.lg_model.rows.extend(recs)
        c = self.lg_counts
        self.lg_rate.setText(f"F3 {c['F3']} · events {c['EVENT']} · logs {c['LOG']} · rows {len(self.lg_model.rows)}")

    def log_refilter(self):
        self.lg_model.filter = self.lg_filter.text().lower()
        self.lg_model.kinds = self.lg_show.currentData()
        self.lg_model.refilter()

    def log_detail(self, idx):
        r = self.lg_model.shown[idx.row()]
        body = r.get("raw")
        txt = (f"{r['kind']}  {r['name']}  id/code {r['id'] if not isinstance(r['id'], int) or r['kind'] != 'LOG' else hex(r['id'])}"
               f"\n{r['time']}\n\n" + (D.hexdump(body) if body else r["text"]))
        dlg = QDialog(self)
        dlg.setWindowTitle("Log record")
        dlg.resize(820, 520)
        lay = QVBoxLayout(dlg)
        e = mono_edit(True)
        e.setPlainText(txt)
        lay.addWidget(e)
        dlg.show()

    def log_save(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Save modem log",
                                            os.path.join(os.path.expanduser("~"),
                                                         f"modem-log-{dt.datetime.now():%Y%m%d-%H%M%S}.txt"),
                                            "Text (*.txt)")
        if not fn:
            return
        with open(fn, "w", encoding="utf-8") as f:
            for r in self.lg_model.shown:
                i = f"0x{r['id']:04X}" if r["kind"] == "LOG" else r["id"]
                f.write(f"{r['time']:%H:%M:%S.%f}\t{r['kind']}\t{i}\t{r['level']}\t{r['name']}\t{r['text']}\n")
        self.say(f"Saved {len(self.lg_model.shown)} rows to {fn}")

    # =========================================================================
    # Console
    # =========================================================================
    def build_console(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.cn_preset = QComboBox()
        self.cn_preset.addItem("Presets…", "")
        for label, hx in (("Version info (00)", "00"), ("Extended build ID (7C)", "7c"),
                          ("Event mask size (81)", "81"), ("Log ID ranges (73 … 01)", "73 00 00 00 01 00 00 00"),
                          ("NV read item 10 (26 0a 00 …)", "26 0a 00" + " 00" * 130),
                          ("EFS hello (4B 13 00 00)", "4b 13 00 00")):
            self.cn_preset.addItem(label, hx)
        self.cn_preset.activated.connect(lambda i: self.cn_input.setText(self.cn_preset.itemData(i) or
                                                                         self.cn_input.text()))
        row.addWidget(self.cn_preset)
        self.cn_input = QLineEdit()
        self.cn_input.setFont(MONO)
        self.cn_input.setPlaceholderText("DIAG command in hex, without HDLC framing/CRC, e.g. 7c")
        self.cn_input.returnPressed.connect(self.console_send)
        row.addWidget(self.cn_input, 1)
        b = QPushButton(theme.line_icon("forward"), " Send")
        b.clicked.connect(self.console_send)
        row.addWidget(b)
        lay.addLayout(row)
        warn = QLabel("Commands go straight to the modem. Unknown commands can change settings or reset it – "
                      "use the NV/EFS tabs for edits (they back up first).")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(warn)
        self.cn_out = mono_edit(True)
        lay.addWidget(self.cn_out, 1)
        return w

    def console_send(self):
        try:
            req = parse_hex(self.cn_input.text())
        except ValueError:
            QMessageBox.warning(self, "Console", "Enter the command as hex bytes.")
            return
        if not req:
            return
        if req[0] == D.NV_WRITE and len(req) >= 3 and int.from_bytes(req[1:3], "little") in D.PROTECTED_NV:
            QMessageBox.warning(self, "Console", "Writing device identity NV items is blocked.")
            return

        def done(r):
            if not self.isVisible():
                return
            self.cn_out.appendPlainText(f"> {req.hex(' ')}\n< {len(r)} bytes"
                                        + (f"  ({D.ERRORS[r[0]]})" if r[0] in D.ERRORS else "")
                                        + "\n" + D.hexdump(r) + "\n")

        def fail(m):
            if self.isVisible():
                self.cn_out.appendPlainText(f"> {req.hex(' ')}\n✘ {m}\n")
        self.Task.start(lambda: self.link.request(req, timeout=5), done, fail)


# ---------------------------------------------------------------------------
class EfsEditor(QDialog):
    def __init__(self, dlg: DiagDialog, path: str, data: bytes):
        super().__init__(dlg)
        self.dlg, self.path, self.data = dlg, path, data
        self.setWindowTitle(f"EFS: {path}")
        self.resize(900, 640)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel(f"{path}   ·   {len(data)} bytes"), 1)
        try:
            sample = data[:8192].decode("utf-8")
            is_text = len(data) > 8 and not any(ch < " " and ch not in "\t\r\n" for ch in sample)
        except UnicodeDecodeError:
            is_text = False
        self.as_text = QRadioButton("Text")
        self.as_hex = QRadioButton("Hex")
        (self.as_text if is_text else self.as_hex).setChecked(True)
        self.as_text.toggled.connect(self.switch)
        top.addWidget(self.as_text)
        top.addWidget(self.as_hex)
        lay.addLayout(top)
        self.edit = mono_edit()
        lay.addWidget(self.edit, 1)
        self.dec = QLabel(decoded(data[:8]))
        self.dec.setFont(MONO)
        lay.addWidget(self.dec)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_save = QPushButton(theme.line_icon("save"), " Write to modem")
        self.btn_save.clicked.connect(self.save)
        self.btn_save.setEnabled(not D.PROTECTED_EFS.search(path))
        row.addWidget(self.btn_save)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        for b in (self.btn_save, close):
            b.setAutoDefault(False)
        lay.addLayout(row)
        self._mode = None
        self.switch()

    def current(self) -> bytes:
        if self._mode == "text":
            return self.edit.toPlainText().encode("utf-8")
        return parse_hex(self.edit.toPlainText())

    def switch(self):
        if self._mode is not None:
            try:
                self.data = self.current()
            except ValueError:
                QMessageBox.warning(self, "EFS", "The hex is not valid.")
                return
        self._mode = "text" if self.as_text.isChecked() else "hex"
        self.edit.setPlainText(self.data.decode("utf-8", errors="replace") if self._mode == "text"
                               else D.hexdump(self.data))

    def save(self):
        try:
            new = self.current()
        except ValueError:
            QMessageBox.warning(self, "EFS", "The hex is not valid.")
            return
        if QMessageBox.question(self, "Write EFS file",
                                f"Write {len(new)} bytes to\n{self.path}\n(was {len(self.data)} bytes)?\n\n"
                                "The current file is backed up first.") != QMessageBox.StandardButton.Yes:
            return
        self.dlg.efs_write(self.path, new, after=self.accept)
