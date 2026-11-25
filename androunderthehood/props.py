"""System property editor (getprop / setprop / Magisk resetprop)."""
from __future__ import annotations

import datetime as dt
import logging
import re

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QTableView, QVBoxLayout)

from . import backend as B
from . import theme

log = logging.getLogger("androunderthehood")

PROP_VALUE_MAX = 92  # bionic limit (incl. NUL) for props that are not ro.*

# props whose change has an immediate, possibly disruptive, effect
DANGEROUS_PREFIXES = ("ctl.", "sys.powerctl", "persist.sys.usb", "sys.usb.config", "ro.", "security.",
                      "selinux.", "service.adb", "persist.adb")


def parse_getprop(text: str) -> list[tuple[str, str]]:
    """Parse `getprop` output ("[key]: [value]" lines; values may span lines)."""
    out = []
    cur = None
    for line in text.split("\n"):
        if cur is None:
            m = re.match(r"^\[([^\]]+)\]: \[(.*)$", line)
            if not m:
                continue
            key, rest = m.group(1), m.group(2)
            if rest.endswith("]"):
                out.append((key, rest[:-1]))
            else:
                cur = [key, rest]
        else:
            if line.endswith("]"):
                cur[1] += "\n" + line[:-1]
                out.append((cur[0], cur[1]))
                cur = None
            else:
                cur[1] += "\n" + line
    return out


def prop_kind(key: str) -> str:
    if key.startswith("ro."):
        return "read-only"
    if key.startswith("persist."):
        return "persistent"
    if key.startswith("ctl."):
        return "control"
    return "runtime"


# --------------------------------------------------------------------------
class PropModel(QAbstractTableModel):
    COLS = ["Property", "Value", "Type"]

    def __init__(self, dlg: "PropsDialog"):
        super().__init__()
        self.dlg = dlg
        self.rows: list[list[str]] = []   # [key, value, kind]
        self.changed: set[str] = set()
        self.lock_icon = theme.line_icon("lock", theme.TEXT_FAINT)
        self.mono = QFont("Consolas", 9)

    def set_rows(self, pairs):
        self.beginResetModel()
        self.rows = [[k, v, prop_kind(k)] for k, v in pairs]
        self.endResetModel()

    def row_of(self, key):
        for i, r in enumerate(self.rows):
            if r[0] == key:
                return i
        return -1

    def update_value(self, key, value):
        i = self.row_of(key)
        if value is None:  # deleted
            if i >= 0:
                self.beginRemoveRows(QModelIndex(), i, i)
                del self.rows[i]
                self.endRemoveRows()
            return
        if i < 0:
            self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows))
            self.rows.append([key, value, prop_kind(key)])
            self.endInsertRows()
        else:
            self.rows[i][1] = value
            self.dataChanged.emit(self.index(i, 0), self.index(i, 2))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 3

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLS[s]
        return None

    def flags(self, index):
        f = super().flags(index)
        if index.column() == 1 and self.rows[index.row()][2] != "control":
            f |= Qt.ItemFlag.ItemIsEditable
        return f

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        key, value, kind = self.rows[index.row()]
        c = index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if c == 0:
                return key
            if c == 1:
                return value if role == Qt.ItemDataRole.EditRole else value.replace("\n", "⏎")
            return kind
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = f"{key}\n{value}"
            if kind == "read-only":
                tip += "\n\nRead-only: can only be changed with Magisk resetprop (until reboot)."
            elif kind == "persistent":
                tip += "\n\nPersistent: survives reboots."
            return tip
        if role == Qt.ItemDataRole.DecorationRole and c == 0 and kind == "read-only":
            return self.lock_icon
        if role == Qt.ItemDataRole.FontRole and c == 1:
            return self.mono
        if role == Qt.ItemDataRole.ForegroundRole:
            if key in self.changed:
                return QColor(theme.ACCENT)
            if c == 2 or (kind == "read-only" and c == 0):
                return QColor(theme.TEXT_DIM)
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or index.column() != 1:
            return False
        key, old, _kind = self.rows[index.row()]
        if value == old:
            return False
        self.dlg.change(key, value, old)
        return False  # the model is updated once the device confirms the change


class PropFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.kind = "all"
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set(self, text=None, kind=None):
        if text is not None:
            self.text = text.lower()
        if kind is not None:
            self.kind = kind
        self.invalidateFilter() if hasattr(self, "invalidateFilter") else self.invalidate()

    def filterAcceptsRow(self, row, parent):
        m: PropModel = self.sourceModel()
        key, value, kind = m.rows[row]
        if self.kind == "editable" and kind == "read-only":
            return False
        if self.kind == "readonly" and kind != "read-only":
            return False
        if self.kind == "persistent" and kind != "persistent":
            return False
        if self.kind == "changed" and key not in m.changed:
            return False
        if not self.text:
            return True
        words = self.text.split()
        hay = f"{key.lower()} {value.lower()}"
        return all(w in hay for w in words)


# --------------------------------------------------------------------------
class PropsDialog(QDialog):
    def __init__(self, win, conn: B.Connection):
        super().__init__(win)
        from .ui import Task  # late import (ui imports this module)

        self.Task = Task
        self.win = win
        self.conn = conn
        self.resetprop: str | None = None
        self.setWindowTitle(f"Properties – {conn.profile.name}")
        self.resize(1040, 680)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        top = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name or value…  (space-separated words must all match)")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(lambda t: (self.proxy.set(text=t), self.update_count()))
        top.addWidget(self.filter, 1)
        self.kind = QComboBox()
        for label, k in (("All properties", "all"), ("Editable (not ro.*)", "editable"),
                         ("Read-only (ro.*)", "readonly"), ("Persistent (persist.*)", "persistent"),
                         ("Changed this session", "changed")):
            self.kind.addItem(label, k)
        self.kind.currentIndexChanged.connect(
            lambda _i: (self.proxy.set(kind=self.kind.currentData()), self.update_count()))
        top.addWidget(self.kind)
        self.btn_refresh = QPushButton(theme.line_icon("refresh"), " Refresh")
        self.btn_refresh.clicked.connect(self.load)
        top.addWidget(self.btn_refresh)
        lay.addLayout(top)

        self.model = PropModel(self)
        self.proxy = PropFilter()
        self.proxy.setSourceModel(self.model)
        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                  | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.view.setAlternatingRowColors(True)
        self.view.setShowGrid(False)
        self.view.setWordWrap(False)
        self.view.verticalHeader().hide()
        self.view.verticalHeader().setDefaultSectionSize(26)
        h = self.view.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.view.setColumnWidth(0, 400)
        self.view.doubleClicked.connect(self.on_double_click)
        lay.addWidget(self.view, 1)

        bottom = QHBoxLayout()
        self.info = QLabel("Loading…")
        self.info.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.info.setWordWrap(True)
        bottom.addWidget(self.info, 1)
        self.btn_new = QPushButton(theme.line_icon("plus"), " New…")
        self.btn_new.clicked.connect(self.new_prop)
        self.btn_edit = QPushButton(theme.line_icon("rename"), " Edit…")
        self.btn_edit.clicked.connect(self.edit_selected)
        self.btn_delete = QPushButton(theme.line_icon("trash"), " Delete")
        self.btn_delete.clicked.connect(self.delete_selected)
        self.btn_copy = QPushButton(theme.line_icon("copy"), " Copy")
        self.btn_copy.clicked.connect(self.copy_selected)
        self.btn_export = QPushButton(theme.line_icon("save"), " Export…")
        self.btn_export.clicked.connect(self.export)
        for b in (self.btn_new, self.btn_edit, self.btn_delete, self.btn_copy, self.btn_export):
            bottom.addWidget(b)
        # no default button: Enter in the filter box must not trigger Refresh/New
        for b in (self.btn_refresh, self.btn_new, self.btn_edit, self.btn_delete, self.btn_copy,
                  self.btn_export):
            b.setAutoDefault(False)
            b.setDefault(False)
        lay.addLayout(bottom)

        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.filter.setFocus)
        QShortcut(QKeySequence.StandardKey.Copy, self.view, activated=self.copy_selected)
        QShortcut(QKeySequence(Qt.Key.Key_F5), self, activated=self.load)
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self.view, activated=self.delete_selected)
        self.view.selectionModel().selectionChanged.connect(lambda *_: self.update_buttons())
        self.update_buttons()
        self.load()
        self.filter.setFocus()

    # ---- loading --------------------------------------------------------
    def load(self):
        conn = self.conn
        self.btn_refresh.setEnabled(False)

        def work():
            rc, out, err = conn.exec(
                "getprop; echo '//RESETPROP//'; "
                "for p in resetprop /debug_ramdisk/resetprop /sbin/resetprop /data/adb/magisk/resetprop "
                "/data/adb/ksu/bin/resetprop /data/adb/ap/bin/resetprop; do "
                "q=$(command -v \"$p\" 2>/dev/null) && [ -x \"$q\" ] && { echo \"$q\"; break; }; done",
                timeout=60)
            text = out.decode("utf-8", errors="replace")
            props, _, rp = text.partition("//RESETPROP//")
            pairs = parse_getprop(props)
            if not pairs:
                raise B.BackendError((err or out).decode(errors="replace").strip() or "getprop returned nothing")
            return pairs, (rp.strip().splitlines() or [None])[0]

        def done(r):
            pairs, rp = r
            self.resetprop = rp
            self.model.set_rows(pairs)
            self.btn_refresh.setEnabled(True)
            self.update_count()

        def fail(msg):
            self.btn_refresh.setEnabled(True)
            self.info.setText(f"Could not read properties: {msg}")
        self.Task.start(work, done, fail)

    def update_count(self):
        total = len(self.model.rows)
        shown = self.proxy.rowCount()
        ro = sum(1 for r in self.model.rows if r[2] == "read-only")
        root = "root" if self.conn.is_root or self.conn.native_root else "not root – changes will likely fail"
        rp = (f"Magisk resetprop available ({self.resetprop}): read-only props can be changed until reboot"
              if self.resetprop else "resetprop not found: read-only (ro.*) props cannot be changed")
        self.info.setText(f"{shown} of {total} shown · {ro} read-only · {root}\n{rp}.  "
                          "Double-click a value to edit. Runtime changes last until reboot; persist.* props are saved.")

    def update_buttons(self):
        keys = self.selected_keys()
        self.btn_edit.setEnabled(len(keys) == 1)
        self.btn_copy.setEnabled(bool(keys))
        self.btn_delete.setEnabled(bool(keys) and bool(self.resetprop))
        self.btn_delete.setToolTip("" if self.resetprop else "Deleting properties needs Magisk resetprop")

    def selected_keys(self) -> list[str]:
        rows = {self.proxy.mapToSource(i).row() for i in self.view.selectionModel().selectedRows(0)}
        return [self.model.rows[r][0] for r in sorted(rows) if 0 <= r < len(self.model.rows)]

    # ---- editing ----------------------------------------------------------
    def on_double_click(self, idx):
        if idx.column() != 1:
            self.edit_selected()

    def edit_selected(self):
        keys = self.selected_keys()
        if len(keys) != 1:
            return
        key = keys[0]
        old = self.model.rows[self.model.row_of(key)][1]
        r = PropEditDialog(self, key, old, new=False)
        if r.exec() == QDialog.DialogCode.Accepted:
            self.change(key, r.value.text(), old)

    def new_prop(self):
        r = PropEditDialog(self, "", "", new=True)
        if r.exec() == QDialog.DialogCode.Accepted:
            key = r.key.text().strip()
            i = self.model.row_of(key)
            self.change(key, r.value.text(), self.model.rows[i][1] if i >= 0 else None)

    def _confirm(self, key: str, value: str, old: str | None, force: str | None = None) -> str | None:
        """Ask for confirmation where needed; return the tool to use or None."""
        if not re.fullmatch(r"[A-Za-z0-9_.\-:@]+", key):
            QMessageBox.warning(self, "Invalid name", f"'{key}' is not a valid property name.")
            return None
        kind = prop_kind(key)
        tool = force or "setprop"
        if force:
            pass
        elif kind == "read-only":
            if not self.resetprop:
                QMessageBox.warning(self, "Read-only property",
                                    f"{key} is read-only. Changing it needs Magisk's resetprop, "
                                    "which was not found on the device.")
                return None
            tool = "resetprop"
        elif len(value.encode()) >= PROP_VALUE_MAX:
            if not self.resetprop:
                QMessageBox.warning(self, "Value too long",
                                    f"Values of non-ro properties are limited to {PROP_VALUE_MAX - 1} bytes.")
                return None
            tool = "resetprop"
        if key.startswith(DANGEROUS_PREFIXES):
            what = ("It is read-only for a reason: apps and the system may behave differently "
                    "until the next reboot." if kind == "read-only" else
                    "This property can take effect immediately (services, USB, ADB, power…).")
            r = QMessageBox.warning(
                self, "Change system property?",
                f"{key}\n\n{old if old is not None else '(new)'}\n→ {value}\n\n{what}\n\nApply with {tool}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return None
        return tool

    def change(self, key: str, value: str, old: str | None, force: str | None = None):
        tool = self._confirm(key, value, old, force)
        if not tool:
            return
        conn = self.conn
        rp = self.resetprop
        q = B.shq

        def work():
            if tool == "resetprop":
                persist = " -p" if key.startswith("persist.") else ""
                cmd = f"{q(rp)}{persist} {q(key)} {q(value)}"
            else:
                cmd = f"setprop {q(key)} {q(value)}"
            rc, out, err = conn.exec(f"{cmd}; r=$?; echo '//NOW//'; getprop {q(key)}; exit $r", timeout=30)
            text = out.decode("utf-8", errors="replace")
            msg, _, now = text.partition("//NOW//\n")
            now = now[:-1] if now.endswith("\n") else now
            log.info("prop %s: %s %r -> rc=%s now=%r", key, tool, value, rc, now)
            return rc, now, (msg + err.decode(errors="replace")).strip()

        def done(r):
            rc, now, msg = r
            if now == value:
                self.model.changed.add(key)
                self.model.update_value(key, now)
                self.win.statusBar().showMessage(f"{key} = {value}", 6000)
                self.update_count()
                return
            self.model.update_value(key, now if now or old is not None else None)
            box = QMessageBox(QMessageBox.Icon.Warning, "Property not changed",
                              f"{key} is still: {now or '(empty)'}\n\n"
                              f"{tool} exit code {rc}{(': ' + msg) if msg else ''}", parent=self)
            retry = None
            if tool == "setprop" and rp:
                box.setInformativeText("The property service refused the change (SELinux policy or a "
                                       "restricted name). Magisk resetprop writes the value directly.")
                retry = box.addButton("Retry with resetprop", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Close)
            box.exec()
            if retry is not None and box.clickedButton() is retry:
                self.change(key, value, old, force="resetprop")
        self.Task.start(work, done, lambda m: QMessageBox.warning(self, "Error", m))

    def delete_selected(self):
        keys = self.selected_keys()
        if not keys or not self.resetprop:
            return
        listing = "\n".join(keys[:15]) + ("\n…" if len(keys) > 15 else "")
        r = QMessageBox.warning(self, "Delete properties?",
                                f"Delete {len(keys)} propert{'y' if len(keys) == 1 else 'ies'} with resetprop?\n\n"
                                f"{listing}\n\nPersistent props are also removed from storage.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        conn, rp, q = self.conn, self.resetprop, B.shq

        def work():
            gone = []
            for k in keys:
                persist = " -p" if k.startswith("persist.") else ""
                conn.exec(f"{q(rp)}{persist} --delete {q(k)}", timeout=30)
                rc, out, _ = conn.exec(f"getprop {q(k)}", timeout=30)
                if not out.strip():
                    gone.append(k)
            return gone

        def done(gone):
            for k in gone:
                self.model.update_value(k, None)
            self.update_count()
            if len(gone) != len(keys):
                QMessageBox.warning(self, "Delete", f"{len(keys) - len(gone)} propert(ies) could not be deleted.")
        self.Task.start(work, done, lambda m: QMessageBox.warning(self, "Error", m))

    # ---- copy / export --------------------------------------------------
    def copy_selected(self):
        keys = self.selected_keys()
        if not keys:
            return
        vals = {r[0]: r[1] for r in self.model.rows}
        QApplication.clipboard().setText("\n".join(f"[{k}]: [{vals[k]}]" for k in keys))
        self.win.statusBar().showMessage(f"Copied {len(keys)} propert{'y' if len(keys) == 1 else 'ies'}", 4000)

    def export(self):
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
        path, _ = QFileDialog.getSaveFileName(self, "Export properties",
                                              f"{self.conn.profile.name}-props-{stamp}.txt",
                                              "Text (*.txt);;All files (*)")
        if not path:
            return
        rows = [self.proxy.mapToSource(self.proxy.index(i, 0)).row() for i in range(self.proxy.rowCount())]
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                k, v, _ = self.model.rows[r]
                f.write(f"[{k}]: [{v}]\n")
        self.win.statusBar().showMessage(f"Exported {len(rows)} properties to {path}", 6000)


class PropEditDialog(QDialog):
    def __init__(self, parent, key: str, value: str, new: bool):
        super().__init__(parent)
        self.setWindowTitle("New property" if new else "Edit property")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.key = QLineEdit(key)
        self.key.setReadOnly(not new)
        self.key.setPlaceholderText("e.g. debug.myapp.enabled")
        self.value = QLineEdit(value)
        self.value.setFont(QFont("Consolas", 10))
        form.addRow("Name", self.key)
        if not new:
            cur = QLabel(value if value else "(empty)")
            cur.setStyleSheet(f"color: {theme.TEXT_DIM};")
            cur.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            cur.setWordWrap(True)
            form.addRow("Current", cur)
        form.addRow("New value", self.value)
        lay.addLayout(form)
        kind = prop_kind(key) if key else "runtime"
        note = {"read-only": "Read-only property: changed with Magisk resetprop, reverts on reboot.",
                "persistent": "Persistent property: the new value survives reboots.",
                "runtime": "Runtime property: the new value lasts until reboot."}.get(kind, "")
        self.note = QLabel(note)
        self.note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(self.note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        (self.key if new else self.value).setFocus()
        if not new:
            self.value.selectAll()

    def accept(self):
        if not self.key.text().strip():
            return
        super().accept()
