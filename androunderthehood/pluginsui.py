"""“Plugins” window: install, create, enable / disable, reload and remove plugins."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout)

from . import pluginpack as PP
from . import plugins as P
from . import theme

WARNING = ("Plugins are Python code that runs inside AndroUnderTheHood with the same access it has: your PC's "
           "files and every phone you connect. Only install plugins from people you trust.")


class PluginsDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.mgr: P.PluginManager = win.plugins
        self.setWindowTitle("Plugins")
        self.resize(1000, 640)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)
        intro = QLabel("Plugins add tools, toolbar buttons and file-menu items without updating AndroUnderTheHood. "
                       "They come as <b>.7z packages</b> (optionally password protected) that are read into memory "
                       "and run from there – never unpacked to disk. Write one with <b>New plugin…</b> (a development "
                       "folder you edit and <b>Reload</b>), then <b>Pack plugin…</b>. Guide: <b>PLUGINS.md</b>.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(intro)

        row = QHBoxLayout()
        for text, icon, fn in ((" Install…", "plus", self.install), (" New plugin…", "rename", self.create),
                               (" Pack plugin…", "package", self.pack),
                               (" Reload all", "refresh", self.reload_all),
                               (" Plugins folder", "folder-plus", lambda: os.startfile(P.user_dir())),  # noqa: S606
                               (" Development folders…", "tree", self.dev_dirs),
                               (" Guide (PLUGINS.md)", "log", self.open_guide)):
            b = QPushButton(theme.line_icon(icon), text)
            b.setAutoDefault(False)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["On", "Plugin", "Version", "Author", "Status", "From"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self.toggled)
        self.table.itemSelectionChanged.connect(self.show_details)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.menu)
        split.addWidget(self.table)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setStyleSheet("font-family: Consolas, monospace;")
        split.addWidget(self.details)
        split.setSizes([380, 180])
        lay.addWidget(split, 1)

        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        bottom.addWidget(self.status, 1)
        self.b_unlock = QPushButton(theme.line_icon("lock"), " Unlock…")
        self.b_unlock.clicked.connect(lambda: self.with_sel(self.unlock))
        self.b_reload = QPushButton(theme.line_icon("refresh"), " Reload")
        self.b_reload.clicked.connect(lambda: self.with_sel(self.reload_one))
        self.b_folder = QPushButton(theme.line_icon("folder-plus"), " Show on disk")
        self.b_folder.clicked.connect(lambda: self.with_sel(self.show_on_disk))
        self.b_remove = QPushButton(theme.line_icon("trash"), " Remove")
        self.b_remove.clicked.connect(lambda: self.with_sel(self.remove))
        for b in (self.b_unlock, self.b_reload, self.b_folder, self.b_remove):
            b.setAutoDefault(False)
            bottom.addWidget(b)
        lay.addLayout(bottom)
        self._mute = False
        self.fill()
        # plugins whose packages are still installing change state later
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1500)

    # ---------------------------------------------------------------------------
    def say(self, text, color=""):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color or theme.TEXT_DIM};")

    def poll(self):
        sig = [(p.id, p.status) for p in self.mgr.plugins.values()]
        if sig != getattr(self, "_last", None):
            self.fill()

    def fill(self):
        sel = self.current().id if self.current() else None
        self._mute = True
        t = self.table
        t.setRowCount(0)
        plugins = sorted(self.mgr.plugins.values(), key=lambda p: p.name.lower())
        self._last = [(p.id, p.status) for p in self.mgr.plugins.values()]
        for p in plugins:
            r = t.rowCount()
            t.insertRow(r)
            on = QTableWidgetItem("")
            on.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            on.setCheckState(Qt.CheckState.Unchecked if p.status == "disabled" else Qt.CheckState.Checked)
            on.setData(Qt.ItemDataRole.UserRole, p.id)
            t.setItem(r, 0, on)
            name = QTableWidgetItem(p.name)
            name.setToolTip(p.meta.get("description", ""))
            t.setItem(r, 1, name)
            t.setItem(r, 2, QTableWidgetItem(p.version))
            t.setItem(r, 3, QTableWidgetItem(str(p.meta.get("author", ""))))
            text = {"loaded": "✔ Loaded", "error": "✘ Error", "disabled": "Off", "locked": "🔒 Locked",
                    "installing packages": "Installing packages…"}.get(p.status, p.status)
            if p.status == "loaded" and p.files is not None:
                text += " (in memory" + (", encrypted)" if p.encrypted else ")")
            st = QTableWidgetItem(text)
            st.setForeground(QBrush(QColor({"loaded": theme.ACCENT, "error": theme.DANGER, "locked": "#ffd479",
                                            "installing packages": "#ffd479"}.get(p.status, theme.TEXT_FAINT))))
            t.setItem(r, 4, st)
            t.setItem(r, 5, QTableWidgetItem({"user": "Installed (.7z)", "bundled": "Built in (.7z)",
                                              "dev": "Development folder" if not p.archive else "Development (.7z)"}
                                             .get(p.origin, p.origin)))
            if p.id == sel:
                t.selectRow(r)
        self._mute = False
        if t.rowCount() and not t.selectedItems():
            t.selectRow(0)
        self.show_details()
        n = sum(1 for p in plugins if p.status == "loaded")
        self.say(f"{len(plugins)} plugin(s), {n} loaded." if plugins else
                 "No plugins yet – use Install… or New plugin….")

    def current(self) -> "P.Plugin | None":
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        pid = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return self.mgr.plugins.get(pid)

    def with_sel(self, fn):
        p = self.current()
        if p:
            try:
                fn(p)
            except Exception as e:  # noqa: BLE001
                self.say(f"✘ {e}", theme.DANGER)

    def show_details(self):
        p = self.current()
        for b in (self.b_unlock, self.b_reload, self.b_folder, self.b_remove):
            b.setEnabled(p is not None)
        if p:
            self.b_unlock.setVisible(bool(p.archive) and (p.status == "locked" or p.encrypted))
            self.b_unlock.setText(" Unlock…" if p.status == "locked" else " Change password…")
        if not p:
            self.details.setPlainText("")
            return
        self.b_remove.setEnabled(p.origin == "user")
        where = (f"Package:  {p.archive}  ({'password protected' if p.encrypted else 'no password'}; run from "
                 f"memory, {len(p.files or {})} files)" if p.archive else f"Folder:   {p.folder}")
        lines = [f"{p.name}  ({p.id})  {p.version}", p.meta.get("description", ""), "",
                 where, f"Requires: {', '.join(p.meta.get('requires') or []) or '—'}"]
        if p.api:
            tools = [a.text() for a in p.api._actions]
            files = [t for t, *_ in p.api._file_actions]
            lines += [f"Adds:     {', '.join(tools + files) or '—'}"]
        if p.error:
            lines += ["", "ERROR:", p.error]
        self.details.setPlainText("\n".join(lines))

    def toggled(self, item):
        if self._mute or item.column() != 0:
            return
        pid = item.data(Qt.ItemDataRole.UserRole)
        self.mgr.set_enabled(pid, item.checkState() == Qt.CheckState.Checked)
        self.fill()

    def menu(self, pos):
        r = self.table.rowAt(pos.y())
        if r < 0:
            return
        self.table.selectRow(r)
        p = self.current()
        m = QMenu(self)
        if p.status == "locked":
            m.addAction("Unlock…", lambda: self.unlock(p))
        m.addAction("Reload", lambda: self.reload_one(p))
        m.addAction("Show on disk", lambda: self.show_on_disk(p))
        if not p.archive:
            m.addAction("Pack this plugin…", lambda: self.pack(p.folder))
        if p.origin == "user":
            m.addAction("Remove", lambda: self.remove(p))
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ---------------------------------------------------------------------------
    def reload_one(self, p):
        self.mgr.reload(p.id)
        self.fill()
        self.say(f"✔ {p.name} reloaded." if p.status == "loaded" else f"✘ {p.name}: {p.error.splitlines()[0]}",
                 theme.ACCENT if p.status == "loaded" else theme.DANGER)

    def reload_all(self):
        self.mgr.load_all()
        self.fill()

    def ask_password(self, title, text, confirm=False) -> str | None:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        form = QFormLayout(dlg)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        form.addRow(lbl)
        e1 = QLineEdit()
        e1.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password:", e1)
        e2 = None
        if confirm:
            e2 = QLineEdit()
            e2.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow("Again:", e2)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        while dlg.exec():
            if e2 is not None and e1.text() != e2.text():
                QMessageBox.warning(self, title, "The two passwords are different.")
                continue
            return e1.text()
        return None

    def install(self):
        m = QMenu(self)
        a_7z = m.addAction("From a plugin package (.7z)…")
        a_dir = m.addAction("From a plugin folder (it is packed into a .7z)…")
        act = m.exec(self.cursor().pos())
        if act is a_7z:
            src, _ = QFileDialog.getOpenFileName(self, "Install plugin", os.path.expanduser("~/Downloads"),
                                                 "Plugin package (*.7z)")
        elif act is a_dir:
            src = QFileDialog.getExistingDirectory(self, "Install plugin – choose its folder (with plugin.json)")
        else:
            return
        if not src:
            return
        if QMessageBox.warning(self, "Install plugin", WARNING + "\n\nInstall it?",
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                               QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.install_attempt(src, None)

    def set_busy(self, on: bool, text: str = ""):
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QApplication
        for b in self.findChildren(QPushButton):
            b.setEnabled(not on)
        self.table.setEnabled(not on)
        if on:
            QApplication.setOverrideCursor(QCursor(Qt.CursorShape.BusyCursor))
            self.say(text)
        else:
            QApplication.restoreOverrideCursor()
            self.show_details()

    def error_box(self, title, msg):
        self.say(f"✘ {msg}", theme.DANGER)
        QMessageBox.critical(self, title, msg)

    def install_attempt(self, src: str, password: str | None):
        """Read + check the package in the background, then install it on the GUI thread."""
        from .ui import Task
        if os.path.isdir(src):
            try:
                pid = self.mgr.install(src)
            except Exception as e:  # noqa: BLE001
                self.error_box("Install plugin", str(e))
                return
            return self.installed(pid)

        def work():
            try:
                return "ok", self.mgr.check_package(src, password)
            except PP.PasswordNeeded:
                return "need", None
            except PP.WrongPassword:
                return "wrong", None
            except Exception as e:  # noqa: BLE001
                return "err", f"{e}"

        def done(r):
            self.set_busy(False)
            kind, val = r
            if kind in ("need", "wrong"):
                pw = self.ask_password("Install plugin", ("This package is password protected." if kind == "need"
                                                          else "Wrong password – try again.") +
                                       " The password is saved encrypted for your Windows account so the plugin "
                                       "loads by itself next time.")
                if pw is not None:
                    self.install_attempt(src, pw)
                return
            if kind == "err":
                self.error_box("Install plugin", f"{os.path.basename(src)} can't be installed: {val}")
                return
            try:
                pid = self.mgr.install(src, password, files=val)
            except Exception as e:  # noqa: BLE001
                self.error_box("Install plugin", str(e))
                return
            self.installed(pid)

        self.set_busy(True, f"Reading {os.path.basename(src)}…")
        Task.start(work, done, lambda m: (self.set_busy(False), self.error_box("Install plugin", m)))

    def installed(self, pid):
        self.fill()
        p = self.mgr.plugins.get(pid)
        if p and p.status in ("loaded", "installing packages"):
            self.say(f"✔ Installed {p.name}.", theme.ACCENT)
        else:
            self.error_box("Install plugin", f"Installed, but it did not load: {(p.error if p else '').strip()[:500]}")

    def unlock(self, p):
        from .ui import Task
        pw = self.ask_password("Unlock plugin", f"Password for {os.path.basename(p.archive)}:")
        if pw is None:
            return

        def done(fresh):
            self.set_busy(False)
            if fresh.status == "locked":
                QMessageBox.warning(self, "Unlock plugin", "Wrong password.")
                return self.unlock(p)
            if fresh.status == "error":
                return self.error_box("Unlock plugin", fresh.error)
            try:
                fresh = self.mgr.unlock(p.id, pw, fresh)
            except Exception as e:  # noqa: BLE001
                return self.error_box("Unlock plugin", str(e))
            self.fill()
            self.say(f"✔ {fresh.name} unlocked." if fresh.status == "loaded" else f"{fresh.name}: {fresh.status}",
                     theme.ACCENT if fresh.status == "loaded" else "")

        self.set_busy(True, f"Reading {os.path.basename(p.archive)}…")
        Task.start(lambda: self.mgr.open_archive(p.archive, p.origin, pw), done,
                   lambda m: (self.set_busy(False), self.error_box("Unlock plugin", m)))

    def show_on_disk(self, p):
        import subprocess
        target = p.archive or p.folder
        subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])  # noqa: S603,S607

    def pack(self, folder: str | None = None):
        if not folder:
            folder = QFileDialog.getExistingDirectory(self, "Plugin folder to pack (with plugin.json)",
                                                      P.dev_default_dir())
        if not folder:
            return
        try:
            meta = P.read_meta(folder)
        except Exception as e:  # noqa: BLE001
            self.say(f"✘ {e}", theme.DANGER)
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Pack {meta.get('name', meta['id'])}")
        form = QFormLayout(dlg)
        out = QLineEdit(os.path.join(os.path.dirname(folder), f"{meta['id']}-{meta.get('version', '1')}.7z"))
        form.addRow("Save as:", out)
        use_pw = QCheckBox("Protect with a password (AES-256)")
        form.addRow(use_pw)
        pw1, pw2 = QLineEdit(), QLineEdit()
        for e in (pw1, pw2):
            e.setEchoMode(QLineEdit.EchoMode.Password)
            e.setEnabled(False)
        form.addRow("Password:", pw1)
        form.addRow("Again:", pw2)
        names = QCheckBox("Also hide the file names (encrypted header)")
        names.setChecked(True)
        names.setEnabled(False)
        form.addRow(names)
        use_pw.toggled.connect(lambda on: [w.setEnabled(on) for w in (pw1, pw2, names)])
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        while dlg.exec():
            if use_pw.isChecked() and (not pw1.text() or pw1.text() != pw2.text()):
                QMessageBox.warning(self, "Pack plugin", "Enter the same password twice.")
                continue
            try:
                PP.pack(folder, out.text().strip(), pw1.text() if use_pw.isChecked() else None, names.isChecked())
            except Exception as e:  # noqa: BLE001
                self.say(f"✘ {e}", theme.DANGER)
                return
            self.say(f"✔ Packed {out.text().strip()} – share that file; install it with Install….", theme.ACCENT)
            return

    def create(self):
        name, ok = QInputDialog.getText(self, "New plugin", "Name of the new plugin:", text="My plugin")
        if not ok or not name.strip():
            return
        try:
            folder = self.mgr.create(name.strip())
        except Exception as e:  # noqa: BLE001
            self.say(f"✘ {e}", theme.DANGER)
            return
        self.fill()
        os.startfile(folder)  # noqa: S606
        self.say(f"✔ Created {folder} (a development folder) – edit plugin.py and press Reload; when it's done use "
                 f"Pack plugin… to make the .7z.", theme.ACCENT)

    def remove(self, p):
        if QMessageBox.question(self, "Remove plugin", f"Remove {p.name}? Its package is deleted:\n{p.folder}") \
                != QMessageBox.StandardButton.Yes:
            return
        self.mgr.remove(p.id)
        self.fill()
        self.say(f"Removed {p.name}.")

    def dev_dirs(self):
        cfg = P.load_cfg()
        dirs = cfg.get("dev_dirs", [])
        m = QMenu(self)
        add = m.addAction("Add a development folder…")
        m.addSeparator()
        rm = {}
        for d in dirs:
            rm[m.addAction(f"Remove {d}")] = d
        if not dirs:
            m.addAction("(none yet: plugins are loaded straight from a development folder, so you can edit "
                        "them in place)").setEnabled(False)
        act = m.exec(self.cursor().pos())
        if act is add:
            d = QFileDialog.getExistingDirectory(self, "Folder containing plugin folders (or one plugin)")
            if d and d not in dirs:
                dirs.append(d)
        elif act in rm:
            dirs.remove(rm[act])
        else:
            return
        cfg["dev_dirs"] = dirs
        P.save_cfg(cfg)
        self.reload_all()

    def open_guide(self):
        for base in (os.path.dirname(P.bundled_dir()), os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
            g = os.path.join(base, "PLUGINS.md")
            if os.path.isfile(g):
                os.startfile(g)  # noqa: S606
                return
        self.say("PLUGINS.md was not found next to the program.", theme.DANGER)
