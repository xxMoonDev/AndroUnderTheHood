"""“USB drivers” window: status of every Android USB mode's driver and one-click install."""
from __future__ import annotations

import os

import urllib.parse

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMenu, QPlainTextEdit,
                               QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from . import drivers as D
from . import theme


class _Sig(QObject):
    say = Signal(str)
    prog = Signal(str, int, int)


class DriversDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        from .ui import Task
        self.Task = Task
        self.win = win
        self.busy = False
        self.setWindowTitle("USB drivers")
        self.resize(1040, 680)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)
        head = QLabel("Drivers for every USB mode a phone can be in: ADB, fastboot, Qualcomm EDL (9008) and "
                      "diagnostic ports, MediaTek PreLoader / VCOM, Samsung Download mode, file transfer and "
                      "tethering. Downloads come only from Google and the Microsoft Update Catalog; each one is "
                      "checked against its published checksum and Authenticode signature before it is installed.")
        head.setWordWrap(True)
        head.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(head)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Driver", "USB modes", "Source", "Status"])
        self.table.verticalHeader().hide()
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 3)

        dev_lbl = QLabel("Connected phones / USB devices")
        dev_lbl.setStyleSheet("font-weight: 600;")
        lay.addWidget(dev_lbl)
        self.devs = QTableWidget(0, 4)
        self.devs.setHorizontalHeaderLabels(["Device", "USB ID", "Driver", "Covered by"])
        self.devs.verticalHeader().hide()
        self.devs.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.devs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        dh = self.devs.horizontalHeader()
        dh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        dh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.devs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.devs.customContextMenuRequested.connect(self.device_menu)
        self.devs.setToolTip("Right-click a device to search its USB ID on Google")
        self.dev_list: list[D.UsbDevice] = []
        lay.addWidget(self.devs, 2)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(130)
        self.log.setStyleSheet("font-family: Consolas, monospace;")
        self.log.hide()
        lay.addWidget(self.log)
        self.bar = QProgressBar()
        self.bar.hide()
        lay.addWidget(self.bar)

        row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.b_folder = QPushButton(theme.line_icon("folder-plus"), " Downloads folder")
        self.b_folder.clicked.connect(lambda: os.startfile(D.work_dir()))  # noqa: S606
        row.addWidget(self.b_folder)
        self.b_refresh = QPushButton(theme.line_icon("refresh"), " Refresh")
        self.b_refresh.clicked.connect(self.refresh)
        row.addWidget(self.b_refresh)
        self.b_install = QPushButton(theme.line_icon("download"), " Install all drivers")
        self.b_install.setObjectName("primary")
        self.b_install.setStyleSheet(f"QPushButton {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; "
                                     "font-weight: 600; padding: 6px 16px; }")
        self.b_install.clicked.connect(self.install_all)
        row.addWidget(self.b_install)
        lay.addLayout(row)
        for b in (self.b_folder, self.b_refresh, self.b_install):
            b.setAutoDefault(False)

        self.sig = _Sig()
        self.sig.say.connect(self.say)
        self.sig.prog.connect(self.on_progress)
        self.refresh()

    # ---------------------------------------------------------------------------
    def say(self, text: str, color: str = ""):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color or theme.TEXT_DIM};")

    def refresh(self):
        self.say("Checking installed drivers and connected devices…")
        self.Task.start(lambda: (D.store_versions(), D.connected_devices()), self.fill,
                        lambda m: self.say(f"✘ {m}", theme.DANGER))

    def fill(self, res):
        store, devs = res
        self.state = {p.key: D.installed(p, store) for p in D.PACKAGES}
        t = self.table
        t.setRowCount(0)
        for p in D.PACKAGES:
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, QTableWidgetItem(p.title))
            t.setItem(r, 1, QTableWidgetItem(p.modes))
            t.setItem(r, 2, QTableWidgetItem(p.source))
            ok = self.state[p.key]
            st = QTableWidgetItem("✔ Built into Windows" if p.builtin and ok else
                                  "✔ Installed" if ok else
                                  "✘ Missing from Windows" if p.builtin else "Not installed")
            st.setForeground(QBrush(QColor(theme.ACCENT if ok else ("#ffd479" if not p.builtin else theme.DANGER))))
            t.setItem(r, 3, st)
        t.resizeRowsToContents()

        d = self.devs
        d.setRowCount(0)
        self.dev_list = list(devs)
        for dev in devs:
            r = d.rowCount()
            d.insertRow(r)
            d.setItem(r, 0, QTableWidgetItem(dev.name))
            d.setItem(r, 1, QTableWidgetItem(f"{dev.vid}:{dev.pid}"))
            it = QTableWidgetItem("✘ No working driver" if dev.problem else "✔ OK")
            it.setForeground(QBrush(QColor(theme.DANGER if dev.problem else theme.ACCENT)))
            d.setItem(r, 2, it)
            pkg = D.package_for(dev.vid)
            d.setItem(r, 3, QTableWidgetItem(pkg.title if pkg else "—"))
            d.item(r, 0).setToolTip(dev.instance)
        if not devs:
            d.insertRow(0)
            d.setItem(0, 0, QTableWidgetItem("No phone connected over USB (plug one in and press Refresh)"))
        missing = [p for p in D.PACKAGES if not p.builtin and not self.state[p.key]]
        self.b_install.setText(" Install all drivers" if missing else " Reinstall all drivers")
        self.say(f"{len(missing)} driver package(s) to install." if missing else
                 "All drivers are installed.", "" if missing else theme.ACCENT)
        final, self._final = getattr(self, "_final", None), None
        if final:
            self.say(*final)

    def device_menu(self, pos):
        r = self.devs.rowAt(pos.y())
        if r < 0 or r >= len(self.dev_list):
            return
        dev = self.dev_list[r]
        vp = f"VID_{dev.vid}&PID_{dev.pid}"
        menu = QMenu(self)
        menu.addAction(f"Search “{dev.vid}:{dev.pid}” on Google",
                       lambda: self.google(f"USB {vp} driver"))
        menu.addAction("Search the device name on Google",
                       lambda: self.google(f"{dev.name} {vp} driver")).setEnabled(dev.name != "(unknown device)")
        menu.addSeparator()
        menu.addAction(f"Copy USB ID ({dev.vid}:{dev.pid})", lambda: QGuiApplication.clipboard().setText(f"{dev.vid}:{dev.pid}"))
        menu.addAction(f"Copy hardware ID (USB\\{vp})", lambda: QGuiApplication.clipboard().setText("USB\\" + vp))
        menu.addAction("Copy device instance path", lambda: QGuiApplication.clipboard().setText(dev.instance))
        menu.exec(self.devs.viewport().mapToGlobal(pos))

    @staticmethod
    def google(q: str):
        QDesktopServices.openUrl(QUrl("https://www.google.com/search?q=" + urllib.parse.quote_plus(q)))

    def on_progress(self, key, done, total):
        self.bar.setMaximum(max(total, 1))
        self.bar.setValue(done)

    # ---------------------------------------------------------------------------
    def install_all(self):
        if self.busy:
            return
        missing = [p for p in D.PACKAGES if not p.builtin and not self.state.get(p.key)]
        todo = missing or [p for p in D.PACKAGES if not p.builtin]
        self.busy = True
        for b in (self.b_install, self.b_refresh):
            b.setEnabled(False)
        self.bar.show()
        self.log.show()
        self.log.clear()
        sig = self.sig

        def work():
            folders = {}
            for p in todo:
                folders[p.title] = D.fetch(p, sig.say.emit, sig.prog.emit)
                sig.say.emit(f"✔ {p.title}: downloaded, checksum and signature OK")
            sig.say.emit("Installing – approve the Windows administrator prompt…")
            return D.install(folders)

        self.Task.start(work, self.installed, self.failed)

    def installed(self, res):
        rc, text = res
        self.busy = False
        self.bar.hide()
        for b in (self.b_install, self.b_refresh):
            b.setEnabled(True)
        if rc == -1:
            self.say("Cancelled at the Windows administrator prompt – nothing was installed.", theme.DANGER)
            return
        self.log.setPlainText(text.strip() or f"(no log – installer exit code {rc})")
        codes = [int(x) for x in __import__("re").findall(r"@@RC (-?\d+)", text)]
        bad = [c for c in codes if c not in (0, 3010, 259)]
        if "@@DONE" not in text:
            self._final = (f"✘ The installer did not finish (exit code {rc}). See the log above.", theme.DANGER)
        elif bad:
            self._final = ("Some drivers reported errors – see the log above.", "#ffd479")
        else:
            self._final = ("✔ Drivers installed." + (" Windows asks for a restart for some of them." if 3010 in codes
                                                     else " Re-plug the phone if it was connected."), theme.ACCENT)
        self.refresh()

    def failed(self, msg):
        self.busy = False
        self.bar.hide()
        for b in (self.b_install, self.b_refresh):
            b.setEnabled(True)
        self.say(f"✘ {msg}", theme.DANGER)
