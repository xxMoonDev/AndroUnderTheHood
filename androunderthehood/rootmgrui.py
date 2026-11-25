"""Root manager window: Magisk / Kitsune Mask / KernelSU / APatch settings, Zygisk, DenyList,
superuser grants and modules (install from a zip, enable/disable, uninstall, run action)."""
from __future__ import annotations

import logging
import os
import urllib.parse

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import backend as B
from . import rootmgr as R
from . import theme

log = logging.getLogger("androunderthehood")


def human_kb(kb: int) -> str:
    if kb >= 1024 * 1024:
        return f"{kb / 1024 / 1024:.1f} GB"
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb} KB"


def google(query: str):
    QDesktopServices.openUrl(QUrl("https://www.google.com/search?q=" + urllib.parse.quote_plus(query)))


class _Stream(QObject):
    text = Signal(str)


class OutputDialog(QDialog):
    """Runs one long command (module install / action) and shows its output live."""

    def __init__(self, parent, title: str, conn: B.Connection, cmd: str, on_finish=None, prepare=None):
        super().__init__(parent)
        from .ui import Task
        self.setWindowTitle(title)
        self.resize(820, 520)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setStyleSheet("font-family: Consolas, monospace;")
        lay.addWidget(self.out, 1)
        row = QHBoxLayout()
        self.state = QLabel("Running…")
        row.addWidget(self.state, 1)
        self.btn = QPushButton("Close")
        self.btn.setEnabled(False)
        self.btn.clicked.connect(self.accept)
        row.addWidget(self.btn)
        lay.addLayout(row)
        self.on_finish = on_finish
        self.sig = _Stream()
        self.sig.text.connect(self.append)
        dec = {"buf": b""}

        def sink(b: bytes):
            dec["buf"] += b
            try:
                s = dec["buf"].decode("utf-8")
                dec["buf"] = b""
            except UnicodeDecodeError:
                return
            try:
                self.sig.text.emit(s)
            except RuntimeError:
                pass

        def work():
            if prepare:
                prepare(lambda s: self.sig.text.emit(s))
            return conn.exec_stream_out(cmd, sink, timeout=900)

        Task.start(work, lambda rc: self.finish(True, ""), lambda m: self.finish(False, m))

    def append(self, s: str):
        from PySide6.QtGui import QTextCursor
        self.out.moveCursor(QTextCursor.MoveOperation.End)
        self.out.insertPlainText(s.replace("\r", ""))
        self.out.ensureCursorVisible()

    def finish(self, ok: bool, msg: str):
        self.btn.setEnabled(True)
        self.state.setText("✔ Done" if ok else f"✘ Failed: {msg[:300]}")
        self.state.setStyleSheet(f"color: {theme.ACCENT if ok else theme.DANGER}; font-weight: 600;")
        if self.on_finish:
            self.on_finish(ok)


class RootManagerDialog(QDialog):
    def __init__(self, win, conn: B.Connection):
        super().__init__(win)
        from .ui import Task
        self.Task = Task
        self.win = win
        self.conn = conn
        self.m: R.Manager | None = None
        self.mods: list[R.Module] = []
        self.settings: dict[str, int] = {}
        self.apps: dict[str, R.AppProcs] | None = None
        self.deny: set = set()
        self.initial_enabled: dict[str, bool] = {}
        self._mute = False
        self.setWindowTitle(f"Root manager – {conn.profile.name}")
        self.resize(1180, 780)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAcceptDrops(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)
        head = QHBoxLayout()
        self.head = QLabel("Detecting the root manager…")
        self.head.setStyleSheet("font-weight: 600; font-size: 11pt;")
        head.addWidget(self.head, 1)
        self.btn_refresh = QPushButton(theme.line_icon("refresh"), " Refresh")
        self.btn_refresh.clicked.connect(self.reload)
        head.addWidget(self.btn_refresh)
        self.btn_reboot = QPushButton(theme.line_icon("power"), " Reboot")
        rm = QMenu(self.btn_reboot)
        rm.addAction("Reboot", lambda: self.reboot(""))
        rm.addAction("Reboot to recovery", lambda: self.reboot("recovery"))
        rm.addAction("Reboot to bootloader", lambda: self.reboot("bootloader"))
        self.btn_reboot.setMenu(rm)
        head.addWidget(self.btn_reboot)
        lay.addLayout(head)
        self.banner = QLabel("")
        self.banner.setStyleSheet(f"background: #3a3220; color: #ffd479; padding: 6px 10px; border-radius: 6px;")
        self.banner.hide()
        lay.addWidget(self.banner)

        self.tabs = QTabWidget()
        self.tab_over = self.build_overview()
        self.tab_mods = self.build_modules()
        self.tab_deny = self.build_denylist()
        self.tab_su = self.build_superuser()
        self.tabs.addTab(self.tab_over, "Overview && Zygisk")
        self.tabs.addTab(self.tab_mods, "Modules")
        self.tabs.addTab(self.tab_deny, "DenyList")
        self.tabs.addTab(self.tab_su, "Superuser")
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
        self.reload()

    # ---- helpers ---------------------------------------------------------------
    def say(self, text: str, error=False):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {theme.DANGER if error else theme.TEXT_DIM};")

    def run(self, fn, done=None, what="Working…"):
        self.say(what)
        self.Task.start(fn, done or (lambda _r: None), lambda m: self.say(f"✘ {m}", True))

    def update_banner(self):
        """Show what is waiting for a reboot, worked out from the current state."""
        why = []
        m = self.m
        if m and m.magisk_family and self.settings and bool(self.settings.get("zygisk")) != m.zygisk_loaded:
            why.append("Zygisk was turned " + ("on" if self.settings.get("zygisk") else "off"))
        for mod in self.mods:
            if mod.pending_install:
                why.append(f"{mod.name} is installed")
            elif mod.removing:
                why.append(f"{mod.name} will be removed")
            elif "update" in mod.flags:
                why.append(f"{mod.name} was updated")
            elif mod.id in self.initial_enabled and mod.enabled != self.initial_enabled[mod.id]:
                why.append(f"{mod.name} was {'enabled' if mod.enabled else 'disabled'}")
        self.banner.setText("⟳  Reboot to apply: " + "; ".join(why) + ".")
        self.banner.setVisible(bool(why))

    def ask(self, title, text, danger=False) -> bool:
        b = QMessageBox.warning if danger else QMessageBox.question
        return b(self, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                 QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def reboot(self, target: str):
        what = {"": "reboot", "recovery": "reboot to recovery", "bootloader": "reboot to the bootloader"}[target]
        if not self.ask("Reboot", f"{what.capitalize()} the device now? AndroUnderTheHood will lose the connection."):
            return
        cmd = f"svc power reboot {target} 2>/dev/null || reboot {target}".replace("  ", " ")
        self.Task.start(lambda: self.conn.exec(f"(sleep 1; {cmd}) >/dev/null 2>&1 &", timeout=15),
                        lambda _r: self.say("Rebooting…"), lambda m: self.say("Reboot command sent"))

    # ---- loading ----------------------------------------------------------------
    def reload(self):
        self.say("Detecting the root manager…")

        def work():
            m = R.detect(self.conn)
            res = {"m": m}
            if m.kind:
                res["mods"] = R.list_modules(self.conn)
            if m.magisk_family:
                res["settings"] = R.magisk_settings(self.conn, m)
            return res
        self.Task.start(work, self.loaded, lambda e: (self.head.setText("Could not read the root manager"),
                                                      self.say(f"✘ {e}", True)))

    def loaded(self, res):
        m = self.m = res["m"]
        if not m.kind:
            self.head.setText("No supported root manager found")
            self.say("Looked for Magisk (and Kitsune Mask / Magisk Alpha), KernelSU (and forks) and APatch. "
                     "Turn Root on in AndroUnderTheHood – the tools need a root shell.", True)
            return
        self.tabs.setEnabled(True)
        bits = [f"{m.name} {m.version}" + (f" ({m.version_code})" if m.version_code else "")]
        if m.extra.get("kernel_version"):
            bits.append(f"kernel {m.extra['kernel_version']}")
        if m.app_pkg:
            bits.append(f"app {m.app_pkg}" + (" (hidden)" if m.app_hidden else ""))
        bits.append("Zygisk running" if m.zygisk_loaded else "Zygisk not running")
        self.head.setText("   ·   ".join(bits))
        self.mods = res.get("mods", [])
        self.settings = res.get("settings", {})
        self.initial_enabled = {x.id: x.enabled for x in self.mods if not x.pending_install}
        self.fill_overview()
        self.fill_modules()
        self.tabs.setTabVisible(3, m.magisk_family)
        self.tabs.setTabText(2, "Exclude list" if m.kind == R.APATCH else "DenyList")
        self.say(f"{len(self.mods)} modules. Right-click a module to search for it on Google.")
        self.apps = None
        self.tab_changed(self.tabs.currentIndex())

    def tab_changed(self, i):
        if not self.m or not self.m.kind:
            return
        w = self.tabs.widget(i)
        if w is self.tab_deny and self.apps is None:
            self.load_denylist()
        elif w is self.tab_su and self.m.magisk_family:
            self.load_superuser()

    # =========================================================================
    # Overview & Zygisk
    # =========================================================================
    def build_overview(self):
        w = QWidget()
        self.ov_lay = QVBoxLayout(w)
        self.ov_lay.setContentsMargins(12, 12, 12, 12)
        self.ov_grid_host = QWidget()
        self.ov_lay.addWidget(self.ov_grid_host)
        self.ov_lay.addStretch(1)
        return w

    def fill_overview(self):
        m = self.m
        old = self.ov_grid_host
        self.ov_grid_host = QWidget()
        self.ov_lay.replaceWidget(old, self.ov_grid_host)
        old.deleteLater()
        g = QGridLayout(self.ov_grid_host)
        g.setHorizontalSpacing(16)
        g.setVerticalSpacing(10)
        g.setColumnStretch(2, 1)
        row = 0

        def info(label, value):
            nonlocal row
            a = QLabel(label)
            a.setStyleSheet(f"color: {theme.TEXT_DIM};")
            b = QLabel(value)
            b.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            b.setWordWrap(True)
            g.addWidget(a, row, 0)
            g.addWidget(b, row, 1, 1, 2)
            row += 1

        info("Root manager", f"{m.name} {m.version}" + (f"  (version code {m.version_code})" if m.version_code else ""))
        info("Binary", m.binary)
        if m.app_pkg:
            info("Manager app", m.app_pkg + ("  – hidden (repackaged with a random name)" if m.app_hidden else ""))
        if m.extra.get("kernel_version"):
            info("Kernel driver version", m.extra["kernel_version"])
        info("Zygisk right now", "loaded in zygote" if m.zygisk_loaded else "not loaded")
        sep = QLabel("")
        g.addWidget(sep, row, 0)
        row += 1

        if m.magisk_family:
            for key, label, dflt, choices, hlp in R.MAGISK_SETTINGS:
                a = QLabel(label)
                a.setStyleSheet("font-weight: 600;")
                cb = QComboBox()
                for v, t in choices.items():
                    cb.addItem(t, v)
                cur = self.settings.get(key, dflt)
                ix = cb.findData(cur)
                if ix < 0:
                    cb.addItem(f"Unknown ({cur})", cur)
                    ix = cb.count() - 1
                cb.setCurrentIndex(ix)
                cb.activated.connect(lambda _i, k=key, c=cb: self.change_setting(k, c))
                h = QLabel(hlp)
                h.setWordWrap(True)
                h.setStyleSheet(f"color: {theme.TEXT_FAINT};")
                g.addWidget(a, row, 0)
                g.addWidget(cb, row, 1)
                g.addWidget(h, row, 2)
                row += 1
        else:
            prov = [x for x in self.mods if x.id.lower() in R.ZYGISK_PROVIDERS or
                    ("zygisk" in x.id.lower() and "zygisk" not in x.flags and "lsposed" not in x.id.lower())]
            a = QLabel("Zygisk")
            a.setStyleSheet("font-weight: 600;")
            g.addWidget(a, row, 0)
            if prov:
                p = prov[0]
                cb = QCheckBox(f"Provided by the module “{p.name}” {p.props.get('version', '')}")
                cb.setChecked(p.enabled and not p.removing)
                cb.toggled.connect(lambda on, p=p: self.toggle_module(p, on))
                g.addWidget(cb, row, 1, 1, 2)
            else:
                h = QLabel(f"{m.name} has no built-in Zygisk. Install a Zygisk provider module such as "
                           "ZygiskNext, ReZygisk or NeoZygisk from the Modules tab.")
                h.setWordWrap(True)
                g.addWidget(h, row, 1, 1, 2)
            row += 1
            if m.kind == R.KERNELSU:
                h = QLabel("Superuser grants and per-app “umount modules” (KernelSU's DenyList) are stored in "
                           "KernelSU's binary allowlist; change those in the KernelSU app (App Profile).")
                h.setWordWrap(True)
                h.setStyleSheet(f"color: {theme.TEXT_FAINT};")
                g.addWidget(h, row, 0, 1, 3)
                row += 1

    def change_setting(self, key, cb: QComboBox):
        v = cb.currentData()
        old = self.settings.get(key)
        if v == old:
            return
        if key == "root_access" and v in (0, 2):
            if not self.ask("Superuser access", "AndroOpenHeart is an app. With this setting it can no longer get root, "
                                                "so AndroUnderTheHood loses root until you change it back in the "
                                                f"{self.m.name} app.\n\nChange it anyway?", danger=True):
                cb.setCurrentIndex(cb.findData(old))
                return

        def done(_r):
            self.settings[key] = v
            self.say(f"✔ {key} = {v}")
            if key == "zygisk":
                self.update_banner()
            elif key in ("mnt_ns", "multiuser_mode"):
                self.say(f"✔ Saved. Applies to new root sessions.")
        self.run(lambda: R.set_magisk_setting(self.conn, self.m, key, v), done, f"Saving {key}…")

    # =========================================================================
    # Modules
    # =========================================================================
    def build_modules(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        bar = QHBoxLayout()
        self.btn_install = QPushButton(theme.line_icon("plus"), " Install from zip…")
        self.btn_install.setToolTip("Install a module zip from this PC (you can also drop a .zip on this window)")
        self.btn_install.clicked.connect(self.install_dialog)
        bar.addWidget(self.btn_install)
        self.btn_toggle = QPushButton("Disable")
        self.btn_toggle.clicked.connect(lambda: self.toggle_module(self.cur_mod()))
        bar.addWidget(self.btn_toggle)
        self.btn_remove = QPushButton(theme.line_icon("trash"), " Uninstall")
        self.btn_remove.clicked.connect(lambda: self.remove_module(self.cur_mod()))
        bar.addWidget(self.btn_remove)
        self.btn_action = QPushButton("▶ Run action")
        self.btn_action.setToolTip("Run the module's action.sh (the Action button in the manager app)")
        self.btn_action.clicked.connect(lambda: self.run_action(self.cur_mod()))
        bar.addWidget(self.btn_action)
        self.btn_search = QPushButton("Search on Google")
        self.btn_search.clicked.connect(lambda: self.search_module(self.cur_mod()))
        bar.addWidget(self.btn_search)
        bar.addStretch(1)
        self.mod_filter = QLineEdit()
        self.mod_filter.setPlaceholderText("Filter modules…")
        self.mod_filter.setClearButtonEnabled(True)
        self.mod_filter.textChanged.connect(self.filter_modules)
        bar.addWidget(self.mod_filter)
        lay.addLayout(bar)

        split = QSplitter(Qt.Orientation.Vertical)
        self.mod_table = QTableWidget(0, 7)
        self.mod_table.setHorizontalHeaderLabels(["On", "Module", "Version", "Author", "Status", "Uses", "Size"])
        self.mod_table.verticalHeader().hide()
        self.mod_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.mod_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.mod_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mod_table.setAlternatingRowColors(True)
        hh = self.mod_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.mod_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.mod_table.customContextMenuRequested.connect(self.module_menu)
        self.mod_table.itemSelectionChanged.connect(self.module_selected)
        self.mod_table.itemChanged.connect(self.module_check_changed)
        self.mod_table.doubleClicked.connect(lambda _i: self.open_folder(self.cur_mod()))
        split.addWidget(self.mod_table)
        self.mod_info = QLabel("")
        self.mod_info.setWordWrap(True)
        self.mod_info.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.mod_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.mod_info.setOpenExternalLinks(True)
        self.mod_info.setStyleSheet(f"background: {theme.CARD}; padding: 10px; border-radius: 6px;")
        split.addWidget(self.mod_info)
        split.setSizes([480, 150])
        lay.addWidget(split, 1)
        return w

    def fill_modules(self):
        sel = self.cur_mod().id if self.cur_mod() else None
        self._mute = True
        t = self.mod_table
        t.setRowCount(0)
        for mod in self.mods:
            r = t.rowCount()
            t.insertRow(r)
            on = QTableWidgetItem("")
            on.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable |
                        (Qt.ItemFlag.ItemIsUserCheckable if not (mod.removing or mod.pending_install) else
                         Qt.ItemFlag.NoItemFlags))
            on.setCheckState(Qt.CheckState.Checked if mod.enabled else Qt.CheckState.Unchecked)
            on.setData(Qt.ItemDataRole.UserRole, mod.id)
            t.setItem(r, 0, on)
            name = QTableWidgetItem(mod.name)
            name.setToolTip(f"{mod.id}\n{mod.path}")
            t.setItem(r, 1, name)
            ver = mod.props.get("version", "")
            if mod.props.get("_new_version"):
                ver += f" → {mod.props['_new_version']}"
            t.setItem(r, 2, QTableWidgetItem(ver))
            t.setItem(r, 3, QTableWidgetItem(mod.props.get("author", "")))
            st = QTableWidgetItem(mod.status)
            if mod.removing:
                st.setForeground(theme_color(theme.DANGER))
            elif mod.pending_install or "update" in mod.flags:
                st.setForeground(theme_color("#ffd479"))
            elif not mod.enabled:
                st.setForeground(theme_color(theme.TEXT_FAINT))
            t.setItem(r, 4, st)
            t.setItem(r, 5, QTableWidgetItem(", ".join(mod.features)))
            sz = QTableWidgetItem(human_kb(mod.size_kb))
            sz.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            t.setItem(r, 6, sz)
            if mod.id == sel:
                t.selectRow(r)
        self._mute = False
        self.filter_modules(self.mod_filter.text())
        if t.rowCount() and not t.selectedItems():
            t.selectRow(0)
        self.module_selected()
        self.update_banner()

    def filter_modules(self, text):
        text = text.lower().strip()
        for r in range(self.mod_table.rowCount()):
            mod = self.mod_at(r)
            hay = f"{mod.name} {mod.id} {mod.props.get('author', '')} {mod.props.get('description', '')}".lower()
            self.mod_table.setRowHidden(r, bool(text) and text not in hay)

    def mod_at(self, r) -> R.Module | None:
        it = self.mod_table.item(r, 0)
        mid = it.data(Qt.ItemDataRole.UserRole) if it else None
        return next((x for x in self.mods if x.id == mid), None)

    def cur_mod(self) -> R.Module | None:
        rows = self.mod_table.selectionModel().selectedRows() if self.mod_table.selectionModel() else []
        return self.mod_at(rows[0].row()) if rows else None

    def module_selected(self):
        mod = self.cur_mod()
        for b in (self.btn_toggle, self.btn_remove, self.btn_action, self.btn_search):
            b.setEnabled(mod is not None)
        if not mod:
            self.mod_info.setText("No module selected." if self.mods else
                                  "No modules installed. Use “Install from zip…” or drop a module .zip here.")
            return
        self.btn_toggle.setText("Enable" if not mod.enabled else "Disable")
        self.btn_toggle.setEnabled(not (mod.removing or mod.pending_install))
        self.btn_remove.setText(" Restore" if mod.removing else " Uninstall")
        self.btn_action.setEnabled("action.sh" in mod.flags and mod.enabled and not mod.pending_install)
        esc = lambda s: (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
        url = mod.project_url()
        parts = [f"<b>{esc(mod.name)}</b> &nbsp;<span style='color:{theme.TEXT_DIM}'>{esc(mod.id)} · "
                 f"{esc(mod.props.get('version', ''))} (code {esc(mod.props.get('versionCode', '?'))}) · "
                 f"by {esc(mod.props.get('author', '?'))}</span>",
                 esc(mod.props.get("description", "")),
                 f"<span style='color:{theme.TEXT_FAINT}'>{esc(mod.path)}</span>"]
        if url:
            parts.append(f"<a href='{esc(url)}' style='color:{theme.ACCENT}'>{esc(url)}</a>")
        self.mod_info.setText("<br>".join(p for p in parts if p))

    def module_check_changed(self, item: QTableWidgetItem):
        if self._mute or item.column() != 0:
            return
        mod = self.mod_at(item.row())
        if mod:
            self.toggle_module(mod, item.checkState() == Qt.CheckState.Checked)

    def module_menu(self, pos):
        r = self.mod_table.rowAt(pos.y())
        if r < 0:
            return
        self.mod_table.selectRow(r)
        mod = self.mod_at(r)
        if not mod:
            return
        menu = QMenu(self)
        menu.addAction(f"Search “{mod.name}” on Google", lambda: self.search_module(mod))
        menu.addAction(f"Search “{mod.id}” on Google", lambda: google(f"\"{mod.id}\" {self.m.name} module"))
        url = mod.project_url()
        if url:
            menu.addAction("Open project page", lambda: QDesktopServices.openUrl(QUrl(url)))
        menu.addSeparator()
        if not (mod.removing or mod.pending_install):
            menu.addAction("Disable" if mod.enabled else "Enable", lambda: self.toggle_module(mod))
        menu.addAction("Restore (cancel uninstall)" if mod.removing else "Uninstall", lambda: self.remove_module(mod))
        if "action.sh" in mod.flags and mod.enabled:
            menu.addAction("Run action", lambda: self.run_action(mod))
        menu.addSeparator()
        menu.addAction("Open module folder", lambda: self.open_folder(mod))
        menu.addAction("Copy module ID", lambda: QGuiApplication.clipboard().setText(mod.id))
        menu.exec(self.mod_table.viewport().mapToGlobal(pos))

    def search_module(self, mod):
        if mod:
            google(f"{mod.name} {self.m.name if self.m else 'Magisk'} module")

    def open_folder(self, mod):
        if mod:
            self.win.navigate(mod.path)
            self.win.raise_()
            self.win.activateWindow()

    def refresh_modules(self, *_):
        self.Task.start(lambda: R.list_modules(self.conn), self._mods_loaded, lambda e: self.say(f"✘ {e}", True))

    def _mods_loaded(self, mods):
        self.mods = mods
        self.fill_modules()
        if self.m and not self.m.magisk_family:
            self.fill_overview()

    def toggle_module(self, mod, on: bool | None = None):
        if not mod:
            return
        if on is None:
            on = not mod.enabled
        if on == mod.enabled:
            return

        def done(_r):
            self.say(f"✔ {mod.name} {'enabled' if on else 'disabled'} – takes effect after a reboot.")
            self.refresh_modules()
        self.run(lambda: R.set_module_enabled(self.conn, self.m, mod, on), done,
                 f"{'Enabling' if on else 'Disabling'} {mod.name}…")

    def remove_module(self, mod):
        if not mod:
            return
        if mod.removing:
            self.run(lambda: R.restore_module(self.conn, self.m, mod),
                     lambda _r: (self.say(f"✔ {mod.name} will be kept."), self.refresh_modules()),
                     f"Restoring {mod.name}…")
            return
        msg = (f"Uninstall “{mod.name}” ({mod.id})?\n\nIt is removed at the next reboot (its uninstall.sh runs "
               "then). Until you reboot you can still restore it here.")
        if mod.pending_install:
            msg = f"Discard the not-yet-activated install of “{mod.name}”?"
        if not self.ask("Uninstall module", msg, danger=True):
            return

        def done(_r):
            self.say(f"✔ {mod.name} will be removed at the next reboot.")
            self.refresh_modules()
        self.run(lambda: R.uninstall_module(self.conn, self.m, mod), done, f"Uninstalling {mod.name}…")

    def run_action(self, mod):
        if not mod or "action.sh" not in mod.flags:
            return
        if not self.ask("Run action", f"Run the action of “{mod.name}” (its action.sh) as root?"):
            return
        OutputDialog(self, f"Action – {mod.name}", self.conn, R.action_command(self.m, mod),
                     on_finish=lambda ok: self.refresh_modules()).show()

    # ---- install -------------------------------------------------------------
    def install_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Install module", os.path.expanduser("~/Downloads"),
                                                "Module zip (*.zip)")
        if paths:
            self.install_zips(paths)

    def dragEnterEvent(self, e):
        if self.m and self.m.kind and e.mimeData().hasUrls() and all(
                u.toLocalFile().lower().endswith(".zip") for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.toLocalFile().lower().endswith(".zip")]
        if paths:
            self.tabs.setCurrentWidget(self.tab_mods)
            self.install_zips(paths)

    def install_zips(self, paths: list[str]):
        names = "\n".join("•  " + os.path.basename(p) for p in paths)
        if not self.ask("Install module", f"Install with {self.m.name}?\n\n{names}\n\nModules run as root at "
                                          "every boot – only install modules you trust."):
            return
        conn, m = self.conn, self.m
        script = []
        for i, p in enumerate(paths):
            dev = f"/data/local/tmp/androunderthehood-module-{i}.zip"
            script.append(f"echo; echo '=== {os.path.basename(p).replace(chr(39), '')} ==='; "
                          f"{R.install_command(m, dev)}; rc=$?; rm -f {dev}; [ $rc = 0 ] || fail=1")
        cmd = "fail=0; " + "; ".join(script) + "; exit $fail"

        def prepare(say):
            for i, p in enumerate(paths):
                with open(p, "rb") as f:
                    data = f.read()
                say(f"Copying {os.path.basename(p)} ({len(data) / 1024 / 1024:.1f} MB) to the device…\n")
                rc, out, err = conn.exec(f"cat > /data/local/tmp/androunderthehood-module-{i}.zip", stdin=data,
                                         timeout=300)
                if rc != 0:
                    raise B.BackendError((err or out).decode(errors="replace") or "copy failed")

        def finished(ok):
            self.refresh_modules()
        OutputDialog(self, f"Install module – {self.m.name}", conn, cmd, on_finish=finished, prepare=prepare).show()

    # =========================================================================
    # DenyList / APatch exclude list
    # =========================================================================
    def build_denylist(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        bar = QHBoxLayout()
        self.dl_enforce = QCheckBox("Enforce DenyList")
        self.dl_enforce.setToolTip("Hide root/modules from the processes ticked below (Magisk needs Zygisk for this)")
        self.dl_enforce.clicked.connect(self.toggle_enforce)
        bar.addWidget(self.dl_enforce)
        self.dl_filter = QLineEdit()
        self.dl_filter.setPlaceholderText("Search apps…")
        self.dl_filter.setClearButtonEnabled(True)
        self.dl_filter.textChanged.connect(lambda _t: self.filter_deny())
        bar.addWidget(self.dl_filter, 1)
        self.dl_system = QCheckBox("System apps")
        self.dl_system.toggled.connect(lambda _t: self.filter_deny())
        bar.addWidget(self.dl_system)
        self.dl_only = QCheckBox("Only selected")
        self.dl_only.toggled.connect(lambda _t: self.filter_deny())
        bar.addWidget(self.dl_only)
        lay.addLayout(bar)
        self.dl_note = QLabel("")
        self.dl_note.setWordWrap(True)
        self.dl_note.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        lay.addWidget(self.dl_note)
        self.dl_tree = QTreeWidget()
        self.dl_tree.setHeaderLabels(["App / process", "Package"])
        self.dl_tree.setUniformRowHeights(True)
        self.dl_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.dl_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.dl_tree.header().resizeSection(1, 380)
        self.dl_tree.itemChanged.connect(self.deny_item_changed)
        self.dl_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.dl_tree.customContextMenuRequested.connect(self.deny_menu)
        lay.addWidget(self.dl_tree, 1)
        return w

    def load_denylist(self):
        m = self.m
        self.dl_tree.clear()
        if m.kind == R.KERNELSU:
            self.dl_enforce.hide()
            self.dl_note.setText("KernelSU has no DenyList. Its equivalent is the per-app “Umount modules” option "
                                 "in the KernelSU app (App Profile) – it is stored in a binary file that only "
                                 "the app can change safely.")
            self.apps = {}
            return
        if m.kind == R.APATCH:
            self.dl_enforce.hide()
            self.dl_note.setText("Ticked apps are excluded: APatch hides root and unmounts modules for them. "
                                 "Saved to /data/adb/ap/package_config (a backup is kept next to it); "
                                 "restart the app – or reboot – for a change to apply.")
        else:
            self.dl_enforce.show()
            self.dl_enforce.setChecked(bool(m.denylist_on))
            extra = "" if self.settings.get("zygisk") else "  Zygisk is off, so the DenyList is not used right now."
            self.dl_note.setText("Tick an app to hide root and module changes from all of its processes, or expand "
                                 "it to pick single processes. Changes apply to newly started processes." + extra)
        self.dl_note.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        self.say("Loading apps…")
        fs = self.win.fs

        def work():
            apps = R.load_app_procs(self.conn, fs)
            if m.kind == R.APATCH:
                header, rows = R.apatch_config(self.conn)
                ix = header.index("exclude") if "exclude" in header else 1
                deny = {(p, p) for p, r in rows.items() if len(r) > ix and r[ix] == "1"}
            else:
                deny = R.denylist(self.conn, m)
            return apps, deny
        self.Task.start(work, self.deny_loaded, lambda e: self.say(f"✘ {e}", True))

    def deny_loaded(self, res):
        self.apps, self.deny = res
        # entries for apps that are not installed (or processes the app no longer declares)
        for pkg, proc in self.deny:
            owner = pkg
            if pkg == "isolated":
                owner = next((p for p in self.apps if proc.startswith(p + ":")), proc.split(":")[0])
            if owner not in self.apps:
                self.apps[owner] = R.AppProcs(owner, f"{owner} (not installed)")
            self.apps[owner].procs.setdefault(proc, pkg == "isolated")
        self.fill_deny()
        n = len({p if p != "isolated" else pr.split(":")[0] for p, pr in self.deny})
        self.say(f"{len(self.apps)} apps · {n} with entries on the {'exclude list' if self.m.kind == R.APATCH else 'DenyList'}.")

    def _key(self, pkg, proc, iso):
        return ("isolated", proc) if iso else (pkg, proc)

    def fill_deny(self):
        self._mute = True
        t = self.dl_tree
        t.clear()
        apatch = self.m.kind == R.APATCH
        for a in sorted(self.apps.values(), key=lambda x: ((x.pkg, x.pkg) not in self.deny and not any(
                self._key(x.pkg, p, i) in self.deny for p, i in x.procs.items()), x.name.lower())):
            it = QTreeWidgetItem([a.name, a.pkg])
            it.setData(0, Qt.ItemDataRole.UserRole, ("app", a.pkg))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if not apatch:
                for proc, iso in sorted(a.procs.items(), key=lambda kv: (kv[1], kv[0] != a.pkg, kv[0])):
                    c = QTreeWidgetItem([proc + ("  (isolated)" if iso else ""), "isolated" if iso else a.pkg])
                    c.setData(0, Qt.ItemDataRole.UserRole, ("proc", a.pkg, proc, iso))
                    c.setFlags(c.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    c.setCheckState(0, Qt.CheckState.Checked if self._key(a.pkg, proc, iso) in self.deny
                                    else Qt.CheckState.Unchecked)
                    if iso:
                        c.setForeground(0, theme_color(theme.TEXT_DIM))
                    it.addChild(c)
            t.addTopLevelItem(it)
            self._update_parent(it)
        self._mute = False
        self.filter_deny()

    def _update_parent(self, it):
        if self.m.kind == R.APATCH:
            pkg = it.data(0, Qt.ItemDataRole.UserRole)[1]
            it.setCheckState(0, Qt.CheckState.Checked if (pkg, pkg) in self.deny else Qt.CheckState.Unchecked)
            return
        n = it.childCount()
        on = sum(1 for i in range(n) if it.child(i).checkState(0) == Qt.CheckState.Checked)
        it.setCheckState(0, Qt.CheckState.Checked if n and on == n else
                         Qt.CheckState.PartiallyChecked if on else Qt.CheckState.Unchecked)

    def filter_deny(self):
        if not self.apps:
            return
        text = self.dl_filter.text().lower().strip()
        sysok = self.dl_system.isChecked()
        only = self.dl_only.isChecked()
        for i in range(self.dl_tree.topLevelItemCount()):
            it = self.dl_tree.topLevelItem(i)
            pkg = it.data(0, Qt.ItemDataRole.UserRole)[1]
            a = self.apps.get(pkg)
            sel = it.checkState(0) != Qt.CheckState.Unchecked
            hide = (text and text not in f"{a.name} {a.pkg}".lower()) or (only and not sel) or \
                (a.system and not sysok and not sel)
            it.setHidden(bool(hide))

    def deny_item_changed(self, item: QTreeWidgetItem, col: int):
        if self._mute or col != 0:
            return
        d = item.data(0, Qt.ItemDataRole.UserRole)
        on = item.checkState(0) == Qt.CheckState.Checked
        if self.m.kind == R.APATCH:
            pkg = d[1]
            self.run(lambda: R.apatch_set_excluded(self.conn, {pkg: on}),
                     lambda _r: (self.deny.add((pkg, pkg)) if on else self.deny.discard((pkg, pkg)),
                                 self.say(f"✔ {pkg} {'excluded' if on else 'no longer excluded'} – restart the app.")),
                     "Saving…")
            return
        add, rm = [], []
        self._mute = True
        if d[0] == "app":
            pkg = d[1]
            if item.checkState(0) == Qt.CheckState.PartiallyChecked:
                item.setCheckState(0, Qt.CheckState.Checked)
                on = True
            for i in range(item.childCount()):
                c = item.child(i)
                _, _, proc, iso = c.data(0, Qt.ItemDataRole.UserRole)
                k = self._key(pkg, proc, iso)
                if on and k not in self.deny:
                    add.append(k)
                elif not on and k in self.deny:
                    rm.append(k)
                c.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            label = self.apps[pkg].name
        else:
            _, pkg, proc, iso = d
            k = self._key(pkg, proc, iso)
            (add if on else rm).append(k)
            self._update_parent(item.parent())
            label = proc
        self._mute = False
        if not add and not rm:
            return

        def work():
            return R.denylist_change(self.conn, self.m, add, rm)

        def done(errs):
            self.deny.update(add)
            self.deny.difference_update(rm)
            if errs:
                self.say(f"Some entries could not be changed:\n{errs[:400]}", True)
            else:
                self.say(f"✔ {label}: {'added to' if add else 'removed from'} the DenyList "
                         f"({len(add) + len(rm)} process{'es' if len(add) + len(rm) != 1 else ''}).")
        self.run(work, done, "Updating the DenyList…")

    def deny_menu(self, pos):
        it = self.dl_tree.itemAt(pos)
        if not it:
            return
        d = it.data(0, Qt.ItemDataRole.UserRole)
        pkg = d[1]
        menu = QMenu(self)
        if d[0] == "app" and self.m.kind != R.APATCH:
            menu.addAction("Add a custom process name…", lambda: self.add_custom_proc(it, pkg))
        menu.addAction("Copy package name", lambda: QGuiApplication.clipboard().setText(pkg))
        menu.addAction(f"Search “{self.apps[pkg].name}” on Google", lambda: google(self.apps[pkg].name + " " + pkg))
        menu.exec(self.dl_tree.viewport().mapToGlobal(pos))

    def add_custom_proc(self, it, pkg):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Add process", f"Process name for {pkg}:", text=pkg + ":")
        name = name.strip()
        if not ok or not name or name in self.apps[pkg].procs:
            return
        self.apps[pkg].procs[name] = False
        self._mute = True
        c = QTreeWidgetItem([name, pkg])
        c.setData(0, Qt.ItemDataRole.UserRole, ("proc", pkg, name, False))
        c.setFlags(c.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        c.setCheckState(0, Qt.CheckState.Unchecked)
        it.addChild(c)
        it.setExpanded(True)
        self._mute = False
        c.setCheckState(0, Qt.CheckState.Checked)

    def toggle_enforce(self, on):
        def done(_r):
            self.m.denylist_on = on
            self.settings["denylist"] = int(on)
            self.say(f"✔ DenyList {'enforced' if on else 'no longer enforced'}.")
            self.fill_overview()
        self.run(lambda: R.set_magisk_setting(self.conn, self.m, "denylist", int(on)), done, "Saving…")

    # =========================================================================
    # Superuser (Magisk family)
    # =========================================================================
    def build_superuser(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        note = QLabel("Apps that asked for root. Changes apply to the next su request; “Revoke” removes the entry so "
                      "the app has to ask again.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        lay.addWidget(note)
        self.su_table = QTableWidget(0, 6)
        self.su_table.setHorizontalHeaderLabels(["App", "UID", "Access", "Log", "Notify", ""])
        self.su_table.verticalHeader().hide()
        self.su_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.su_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.su_table.setAlternatingRowColors(True)
        hh = self.su_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.su_table, 1)
        return w

    def load_superuser(self):
        self.Task.start(lambda: R.su_policies(self.conn, self.m), self.fill_superuser,
                        lambda e: self.say(f"✘ {e}", True))

    def _vital(self, p: R.Policy) -> str:
        if "dev.rootssh" in p.packages:
            return "AndroOpenHeart (AndroUnderTheHood's connection)"
        if p.uid % 100000 == 2000:
            return "the ADB shell"
        return ""

    def fill_superuser(self, pols):
        t = self.su_table
        t.setRowCount(0)
        labels = {a.pkg: a.name for a in (self.apps or {}).values()}
        for p in pols:
            r = t.rowCount()
            t.insertRow(r)
            name = ", ".join(labels.get(x, x) for x in p.packages) or f"uid {p.uid} (not installed)"
            it = QTableWidgetItem(name)
            it.setToolTip("\n".join(p.packages))
            t.setItem(r, 0, it)
            t.setItem(r, 1, QTableWidgetItem(str(p.uid)))
            cb = QComboBox()
            for v, txt in R.POLICY_TEXT.items():
                cb.addItem(txt, v)
            cb.setCurrentIndex(max(0, cb.findData(p.policy)))
            cb.activated.connect(lambda _i, p=p, cb=cb: self.su_change(p, cb))
            t.setCellWidget(r, 2, cb)
            for col, attr in ((3, "logging"), (4, "notification")):
                ch = QCheckBox()
                ch.setChecked(getattr(p, attr))
                ch.toggled.connect(lambda on, p=p, a=attr: self.run(
                    lambda: R.set_policy(self.conn, self.m, p.uid, **{a: on}),
                    lambda _r: self.say(f"✔ saved"), "Saving…"))
                box = QWidget()
                bl = QHBoxLayout(box)
                bl.setContentsMargins(8, 0, 8, 0)
                bl.addWidget(ch)
                t.setCellWidget(r, col, box)
            b = QPushButton("Revoke")
            b.setAutoDefault(False)
            b.clicked.connect(lambda _c=False, p=p, n=name: self.su_revoke(p, n))
            t.setCellWidget(r, 5, b)
        self.say(f"{len(pols)} superuser entries.")

    def su_change(self, p: R.Policy, cb: QComboBox):
        v = cb.currentData()
        vital = self._vital(p)
        if vital and v != 2 and not self.ask("Superuser", f"This is {vital}. Without root access AndroUnderTheHood's "
                                                          "root tools stop working.\n\nChange it anyway?", danger=True):
            cb.setCurrentIndex(cb.findData(p.policy))
            return

        def done(_r):
            p.policy = v
            self.say(f"✔ uid {p.uid}: {R.POLICY_TEXT.get(v, v)}")
        self.run(lambda: R.set_policy(self.conn, self.m, p.uid, policy=v), done, "Saving…")

    def su_revoke(self, p: R.Policy, name: str):
        vital = self._vital(p)
        text = f"Revoke root for {name}? It will have to ask again."
        if vital:
            text = f"This is {vital}. Revoking it can cut AndroUnderTheHood off from root.\n\n" + text
        if not self.ask("Revoke", text, danger=bool(vital)):
            return
        self.run(lambda: R.set_policy(self.conn, self.m, p.uid, delete=True),
                 lambda _r: self.load_superuser(), "Revoking…")


def theme_color(c: str):
    from PySide6.QtGui import QBrush, QColor
    return QBrush(QColor(c))
