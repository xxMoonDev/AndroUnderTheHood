"""Per-app permissions and App Ops manager."""
from __future__ import annotations

import logging
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
                               QWidget)

from . import backend as B
from . import theme

log = logging.getLogger("androunderthehood")
q = B.shq

APPOP_MODES = ["allow", "ignore", "deny", "foreground", "default"]
COMMON_OPS = ["SYSTEM_ALERT_WINDOW", "WRITE_SETTINGS", "REQUEST_INSTALL_PACKAGES", "MANAGE_EXTERNAL_STORAGE",
              "GET_USAGE_STATS", "RUN_IN_BACKGROUND", "RUN_ANY_IN_BACKGROUND", "SCHEDULE_EXACT_ALARM",
              "PICTURE_IN_PICTURE", "CAMERA", "RECORD_AUDIO", "COARSE_LOCATION", "FINE_LOCATION",
              "READ_CLIPBOARD", "POST_NOTIFICATION", "ACCESS_NOTIFICATIONS", "BLUETOOTH_SCAN", "WAKE_LOCK",
              "READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO", "READ_MEDIA_AUDIO", "MANAGE_MEDIA", "TOAST_WINDOW",
              "PROJECT_MEDIA", "ACTIVATE_VPN", "AUTO_REVOKE_PERMISSIONS_IF_UNUSED"]


def load(conn: B.Connection, pkg: str) -> dict:
    rc, out, _ = conn.exec(f"dumpsys package {q(pkg)}; echo '//APPOPS//'; cmd appops get {q(pkg)} 2>&1", timeout=60)
    text = out.decode("utf-8", errors="replace")
    dump, _, ops_text = text.partition("//APPOPS//")
    # only the package's own block (dumpsys also lists other packages' references)
    m = re.search(rf"\n  Package \[{re.escape(pkg)}\][^\n]*\n(.*?)(?=\n  Package \[|\n\S|\Z)", dump, re.S)
    block = m.group(1) if m else dump
    requested, install, runtime = [], {}, {}
    sec = None
    for line in block.split("\n"):
        s = line.strip()
        if s == "requested permissions:":
            sec = "req"
            continue
        if s == "install permissions:":
            sec = "inst"
            continue
        if s == "runtime permissions:":
            sec = "rt"
            continue
        if s.endswith(":") and not s.startswith(("android.", "com.", "org.")) and ":" not in s[:-1] and "." not in s:
            sec = None
            continue
        if sec == "req":
            if re.match(r"^[\w.]+(: restricted=\w+)?$", s):
                requested.append(s.split(":")[0])
            else:
                sec = None
        elif sec in ("inst", "rt"):
            mm = re.match(r"^([\w.]+): granted=(\w+)(?:, flags=\[ ?(.*?) ?\])?", s)
            if mm:
                (install if sec == "inst" else runtime)[mm.group(1)] = (mm.group(2) == "true", mm.group(3) or "")
            elif s:
                sec = None
    ops = []
    for line in ops_text.splitlines():
        mm = re.match(r"^(?:Uid mode: )?([A-Z_0-9]+): (\w+)(?:; (.*))?$", line.strip())
        if mm:
            ops.append((mm.group(1), mm.group(2), mm.group(3) or "", line.strip().startswith("Uid mode")))
    return {"requested": requested, "install": install, "runtime": runtime, "ops": ops}


class PermissionsDialog(QDialog):
    def __init__(self, win, conn: B.Connection, pkg: str, label: str = ""):
        super().__init__(win)
        from .ui import Task

        self.Task = Task
        self.win = win
        self.conn = conn
        self.pkg = pkg
        self.data: dict = {}
        self.setWindowTitle(f"Permissions – {label or pkg}")
        self.resize(980, 720)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 12)
        top = QHBoxLayout()
        t = QLabel(f"{label}  ·  {pkg}" if label else pkg)
        t.setObjectName("propsTitle")
        top.addWidget(t, 1)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter…")
        self.filter.setClearButtonEnabled(True)
        self.filter.setMaximumWidth(260)
        self.filter.textChanged.connect(self.fill)
        top.addWidget(self.filter)
        b = QPushButton(theme.line_icon("refresh"), " Refresh")
        b.setAutoDefault(False)
        b.clicked.connect(self.load)
        top.addWidget(b)
        lay.addLayout(top)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.rt = self._table(["Permission", "Granted", "Flags"])
        self.inst = self._table(["Permission", "Granted"])
        self.ops = self._table(["Operation", "Mode", "Details"])
        opsw = QWidget()
        ol = QVBoxLayout(opsw)
        ol.setContentsMargins(0, 6, 0, 0)
        ol.addWidget(self.ops, 1)
        row = QHBoxLayout()
        self.op_name = QComboBox()
        self.op_name.setEditable(True)
        self.op_name.addItems(COMMON_OPS)
        self.op_mode = QComboBox()
        self.op_mode.addItems(APPOP_MODES)
        bset = QPushButton(theme.line_icon("forward"), " Set op")
        bset.setAutoDefault(False)
        bset.clicked.connect(lambda: self.set_op(self.op_name.currentText().strip(), self.op_mode.currentText()))
        row.addWidget(QLabel("Set any App Op:"))
        row.addWidget(self.op_name, 1)
        row.addWidget(self.op_mode)
        row.addWidget(bset)
        ol.addLayout(row)
        self.tabs.addTab(self.rt, "Runtime permissions")
        self.tabs.addTab(opsw, "App Ops")
        self.tabs.addTab(self.inst, "Install-time permissions")
        lay.addWidget(self.tabs, 1)
        self.status = QLabel("Loading…")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.load()

    def _table(self, headers):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().hide()
        t.verticalHeader().setDefaultSectionSize(30)
        t.setShowGrid(False)
        t.setAlternatingRowColors(True)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, len(headers)):
            t.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        return t

    def load(self):
        conn, pkg = self.conn, self.pkg

        def done(d):
            self.data = d
            self.fill()
        self.Task.start(lambda: load(conn, pkg), done, lambda m: self.status.setText(f"Could not read: {m}"))

    def fill(self, *_):
        d = self.data
        if not d:
            return
        f = self.filter.text().lower()
        ok = lambda s: not f or f in s.lower()  # noqa: E731
        # runtime
        rows = [(p, g, fl) for p, (g, fl) in sorted(d["runtime"].items()) if ok(p)]
        t = self.rt
        t.setRowCount(len(rows))
        for i, (perm, granted, flags) in enumerate(rows):
            a = QTableWidgetItem(perm.replace("android.permission.", ""))
            a.setToolTip(perm)
            t.setItem(i, 0, a)
            btn = QPushButton("Granted ✔" if granted else "Denied")
            btn.setCheckable(True)
            btn.setChecked(granted)
            btn.setAutoDefault(False)
            btn.setStyleSheet(f"QPushButton:checked{{background:{theme.ACCENT};color:{theme.ACCENT_TEXT};"
                              f"border-color:{theme.ACCENT};}}")
            fixed = "SYSTEM_FIXED" in flags or "POLICY_FIXED" in flags
            btn.setEnabled(not fixed)
            if fixed:
                btn.setToolTip("Fixed by the system/policy")
            btn.clicked.connect(lambda on, p=perm: self.set_perm(p, on))
            t.setCellWidget(i, 1, btn)
            fi = QTableWidgetItem(flags.replace("|", " · "))
            fi.setForeground(QColor(theme.TEXT_DIM))
            fi.setToolTip(flags)
            t.setItem(i, 2, fi)
        # install
        inst = {p: g for p, (g, _f) in d["install"].items()}
        for p in d["requested"]:
            if p not in inst and p not in d["runtime"]:
                inst[p] = None   # requested but not granted (signature/privileged)
        rows = [(p, g) for p, g in sorted(inst.items()) if ok(p)]
        t = self.inst
        t.setRowCount(len(rows))
        for i, (perm, granted) in enumerate(rows):
            t.setItem(i, 0, QTableWidgetItem(perm))
            g = QTableWidgetItem("yes" if granted else ("no" if granted is False else "not granted"))
            g.setForeground(QColor(theme.ACCENT if granted else theme.TEXT_FAINT))
            t.setItem(i, 1, g)
        # ops
        rows = [o for o in d["ops"] if ok(o[0])]
        t = self.ops
        t.setRowCount(len(rows))
        for i, (op, mode, details, uid_mode) in enumerate(rows):
            t.setItem(i, 0, QTableWidgetItem(op + ("  (uid)" if uid_mode else "")))
            cb = QComboBox()
            cb.addItems(APPOP_MODES if mode in APPOP_MODES else APPOP_MODES + [mode])
            cb.setCurrentText(mode)
            cb.currentTextChanged.connect(lambda m, o=op: self.set_op(o, m))
            t.setCellWidget(i, 1, cb)
            di = QTableWidgetItem(details)
            di.setForeground(QColor(theme.TEXT_DIM))
            t.setItem(i, 2, di)
        granted = sum(1 for g, _f in d["runtime"].values() if g)
        self.status.setText(f"{granted} of {len(d['runtime'])} runtime permissions granted · {len(d['install'])} "
                            f"install-time · {len(d['ops'])} App Ops   —   changes apply immediately (pm grant/revoke, "
                            "cmd appops set); the app may need restarting to notice.")

    def set_perm(self, perm: str, on: bool):
        cmd = f"pm {'grant' if on else 'revoke'} {q(self.pkg)} {q(perm)}"
        self.run(cmd)

    def set_op(self, op: str, mode: str):
        if not re.fullmatch(r"[A-Za-z0-9_:.]+", op or ""):
            return
        self.run(f"cmd appops set {q(self.pkg)} {q(op)} {q(mode)}")

    def run(self, cmd: str):
        conn = self.conn

        def done(r):
            rc, out, err = r
            msg = (out + err).decode(errors="replace").strip()
            if rc != 0 or "Exception" in msg or "Error" in msg:
                QMessageBox.warning(self, "Permissions", f"{cmd}\n\n{msg or f'exit code {rc}'}")
            else:
                self.win.statusBar().showMessage(f"{cmd}", 5000)
            self.load()
        self.Task.start(lambda: conn.exec(cmd + " 2>&1", timeout=30), done,
                        lambda m: QMessageBox.warning(self, "Permissions", m))
