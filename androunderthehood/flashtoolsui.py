"""“Flash tools” window: install / add flash tools and launch them with one button each."""
from __future__ import annotations

import os
import urllib.parse

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QMenu, QMessageBox, QProgressBar, QPushButton, QTableWidget,
                               QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from . import flashtools as F
from . import theme


class _Sig(QObject):
    say = Signal(str)
    prog = Signal(int, int)


class FlashToolsDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        from .ui import Task
        self.Task = Task
        self.win = win
        self.state = F.load_state()
        self.busy = False
        self.setWindowTitle("Flash tools")
        self.resize(1120, 720)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        top = QHBoxLayout()
        intro = QLabel("PC tools for flashing phones. Official and open-source tools are downloaded from where their "
                       "makers publish them and verified; for tools that are never published officially, add the "
                       "file you downloaded yourself.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_DIM};")
        top.addWidget(intro, 1)
        self.b_term = QPushButton(theme.line_icon("terminal"), " Flash terminal")
        self.b_term.setToolTip("Command prompt with fastboot, adb, edl, mtk, thor and spd_dump on PATH")
        self.b_term.clicked.connect(lambda: self.safe(lambda: F.open_terminal(self.state)))
        top.addWidget(self.b_term)
        self.b_drivers = QPushButton(theme.line_icon("usb"), " USB drivers…")
        self.b_drivers.clicked.connect(win.open_drivers)
        top.addWidget(self.b_drivers)
        lay.addLayout(top)

        self.tabs = QTabWidget()
        self.t_off = self._table()
        self.t_own = self._table()
        w1 = QWidget()
        l1 = QVBoxLayout(w1)
        l1.setContentsMargins(6, 6, 6, 6)
        row = QHBoxLayout()
        self.b_all = QPushButton(theme.line_icon("download"), " Install all official tools")
        self.b_all.setStyleSheet(f"QPushButton {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; "
                                 "font-weight: 600; padding: 6px 16px; }")
        self.b_all.clicked.connect(self.install_all)
        row.addWidget(QLabel("Downloaded from Google, GitHub, python.org and PyPI – every download is checked."), 1)
        row.addWidget(self.b_all)
        l1.addLayout(row)
        l1.addWidget(self.t_off)
        w2 = QWidget()
        l2 = QVBoxLayout(w2)
        l2.setContentsMargins(6, 6, 6, 6)
        note = QLabel("These tools are not published by their makers, so AndroUnderTheHood can't download them. "
                      "Use <b>Add…</b> with the zip / 7z / rar, program or folder you got: archives are unpacked into "
                      "the tools folder and scanned with Microsoft Defender, and the program's signature is shown.")
        note.setWordWrap(True)
        l2.addWidget(note)
        l2.addWidget(self.t_own)
        self.tabs.addTab(w1, "Official && open source")
        self.tabs.addTab(w2, "Your own tools (QPST, Odin, SP Flash…)")
        lay.addWidget(self.tabs, 1)

        self.bar = QProgressBar()
        self.bar.hide()
        lay.addWidget(self.bar)
        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        bottom.addWidget(self.status, 1)
        b_folder = QPushButton(theme.line_icon("folder-plus"), " Tools folder")
        b_folder.clicked.connect(lambda: os.startfile(F.tools_dir()))  # noqa: S606
        bottom.addWidget(b_folder)
        lay.addLayout(bottom)
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
        self.sig = _Sig()
        self.sig.say.connect(self.say)
        self.sig.prog.connect(lambda d, t: (self.bar.setMaximum(max(t, 1)), self.bar.setValue(d)))
        self.fill()

    def _table(self) -> QTableWidget:
        t = QTableWidget(0, 5)
        t.setHorizontalHeaderLabels(["Tool", "For", "Status", "", ""])
        t.verticalHeader().hide()
        t.verticalHeader().setDefaultSectionSize(54)
        t.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        t.setTextElideMode(Qt.TextElideMode.ElideRight)
        t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setAlternatingRowColors(True)
        t.setWordWrap(False)
        h = t.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        return t

    # ---------------------------------------------------------------------------
    def say(self, text: str, color: str = ""):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color or theme.TEXT_DIM};")

    def safe(self, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            self.say(f"✘ {e}", theme.DANGER)

    def ready(self, key: str) -> bool:
        e = self.state.get(key, {})
        return bool(e.get("exe")) and os.path.exists(e["exe"])

    def fill(self):
        for table, tools in ((self.t_off, F.OFFICIAL), (self.t_own, F.OWN)):
            table.setRowCount(0)
            for t in tools:
                r = table.rowCount()
                table.insertRow(r)
                name = QTableWidgetItem(t.name)
                name.setToolTip(t.what)
                table.setItem(r, 0, name)
                fr = QTableWidgetItem(f"{t.brands}\n{t.what}")
                fr.setForeground(QBrush(QColor(theme.TEXT_DIM)))
                fr.setToolTip(f"{t.brands}\n{t.what}" + (f"\nSource: {t.source}" if t.source else ""))
                table.setItem(r, 1, fr)
                table.setItem(r, 2, self._status_item(t))
                table.setCellWidget(r, 3, self._main_buttons(t))
                table.setCellWidget(r, 4, self._more_button(t))
        self.b_all.setText(" Install all official tools" if not all(
            self.ready(t.key) for t in F.OFFICIAL) else " Update all official tools")

    def _status_item(self, t: F.Tool) -> QTableWidgetItem:
        e = self.state.get(t.key, {})
        if t.key == "usbdk" and self.ready("usbdk"):
            ok = F.usbdk_installed()
            it = QTableWidgetItem(("✔ Installed" if ok else "Downloaded – press Install") + f"\n{e.get('version', '')}")
            it.setForeground(QBrush(QColor(theme.ACCENT if ok else "#ffd479")))
        elif self.ready(t.key):
            sub = e.get("version") or os.path.basename(e["exe"])
            it = QTableWidgetItem(f"✔ {sub}\n{e.get('verified') or e.get('signature', '')}")
            it.setForeground(QBrush(QColor(theme.ACCENT)))
            if e.get("scan_clean") is False:
                it.setText(f"⚠ Defender found threats\n{sub}")
                it.setForeground(QBrush(QColor(theme.DANGER)))
        else:
            it = QTableWidgetItem("Not installed" if t.kind == "official" else "Not added")
            it.setForeground(QBrush(QColor(theme.TEXT_FAINT)))
        it.setToolTip(e.get("exe", ""))
        return it

    def _main_buttons(self, t: F.Tool) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(4, 2, 4, 2)
        ready = self.ready(t.key)
        if t.kind == "official":
            b = QPushButton(("Update" if ready else "Install") if t.key != "usbdk" else
                            ("Install" if ready and not F.usbdk_installed() else "Download" if not ready else "Update"))
            b.clicked.connect(lambda _c=False, k=t.key: self.install(k))
        else:
            b = QPushButton(theme.line_icon("plus"), " Add…" if not ready else " Replace…")
            b.clicked.connect(lambda _c=False, k=t.key: self.add_own(k))
        b.setAutoDefault(False)
        b.setMinimumWidth(92)
        b.setEnabled(not self.busy)
        h.addWidget(b)
        if t.key != "usbdk":
            go = QPushButton("▶  Launch")
            go.setMinimumWidth(96)
            go.setEnabled(ready and not self.busy)
            go.setStyleSheet(f"QPushButton:enabled {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; "
                             "font-weight: 600; }")
            go.setToolTip("Opens a terminal with the tool on PATH" if t.console else "Starts the program")
            go.clicked.connect(lambda _c=False, k=t.key: self.launch(k))
            go.setAutoDefault(False)
            h.addWidget(go)
        return w

    def _more_button(self, t: F.Tool) -> QWidget:
        b = QToolButton()
        b.setIcon(theme.line_icon("more"))
        b.setMinimumSize(32, 30)
        b.setToolTip("More: open folder, remove, search on Google…")
        b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        b.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")
        m = QMenu(b)
        e = self.state.get(t.key, {})
        if self.ready(t.key):
            m.addAction("Open folder", lambda: os.startfile(os.path.dirname(e["exe"])))  # noqa: S606
        if t.kind == "own":
            if e.get("candidates"):
                m.addAction("Choose a different program…", lambda: self.pick_program(t.key))
            if t.search:
                m.addAction(f"Search “{t.name}” on Google", lambda: QDesktopServices.openUrl(
                    QUrl("https://www.google.com/search?q=" + urllib.parse.quote_plus(t.search))))
            if self.ready(t.key):
                m.addAction("Scan again with Microsoft Defender", lambda: self.scan(t.key))
        if self.ready(t.key):
            m.addSeparator()
            m.addAction("Remove", lambda: self.remove(t.key))
        if m.isEmpty():
            b.setEnabled(False)
        b.setMenu(m)
        return b

    def set_busy(self, on: bool):
        self.busy = on
        self.bar.setVisible(on)
        self.b_all.setEnabled(not on)
        self.fill()

    # ---------------------------------------------------------------------------
    def install(self, key: str, then=None):
        if key == "usbdk" and self.ready("usbdk") and not F.usbdk_installed():
            return self.install_usbdk()
        self.set_busy(True)
        sig = self.sig

        def done(entry):
            self.state[key] = entry
            F.save_state(self.state)
            F.write_shims(self.state)
            self.set_busy(False)
            self.say(f"✔ {F.by_key(key).name} {entry.get('version', '')} installed – {entry.get('verified', '')}",
                     theme.ACCENT)
            if key == "usbdk" and not F.usbdk_installed():
                self.say("UsbDk downloaded – approve the Windows administrator prompt to install it…")
                self.install_usbdk(then)
                return
            if then:
                then()

        def fail(m):
            self.set_busy(False)
            self.say(f"✘ {F.by_key(key).name}: {m}", theme.DANGER)

        self.Task.start(lambda: F.install_official(key, sig.say.emit, sig.prog.emit), done, fail)

    def install_all(self):
        keys = [t.key for t in F.OFFICIAL]

        def step(i=0):
            if i < len(keys):
                self.install(keys[i], then=lambda: step(i + 1))
            else:
                self.say("✔ All official tools are installed." if F.usbdk_installed() else
                         "Official tools installed; UsbDk was not – press Install on its row.",
                         theme.ACCENT if F.usbdk_installed() else "#ffd479")
        step()

    def install_usbdk(self, then=None):
        self.set_busy(True)

        def done(r):
            rc, logf = r
            self.set_busy(False)
            if then:
                then()
            if rc == -1:
                self.say("Cancelled at the Windows administrator prompt.", theme.DANGER)
            elif F.usbdk_installed():
                self.say("✔ UsbDk installed. edl and mtkclient can now reach phones in EDL / BROM mode.", theme.ACCENT)
            else:
                self.say(f"✘ UsbDk did not install (code {rc}) – log: {logf}", theme.DANGER)

        self.Task.start(lambda: F.install_usbdk(self.state), done,
                        lambda m: (self.set_busy(False), self.say(f"✘ {m}", theme.DANGER)))

    def launch(self, key: str):
        self.safe(lambda: F.launch(key, self.state))
        if not self.status.text().startswith("✘"):
            self.say(f"Started {F.by_key(key).name}.")

    # ---- your own tools ---------------------------------------------------------
    def add_own(self, key: str):
        t = F.by_key(key)
        menu = QMenu(self)
        a_file = menu.addAction("From an archive or program (.zip, .7z, .rar, .exe)…")
        a_dir = menu.addAction("From a folder (already unpacked / installed)…")
        act = menu.exec(self.cursor().pos())
        if act is a_file:
            src, _ = QFileDialog.getOpenFileName(self, f"Add {t.name}", os.path.expanduser("~/Downloads"),
                                                 "Tool (*.zip *.7z *.rar *.exe);;All files (*)")
        elif act is a_dir:
            src = QFileDialog.getExistingDirectory(self, f"Add {t.name} – choose its folder",
                                                   os.path.expanduser("~/Downloads"))
        else:
            return
        if not src:
            return
        self.set_busy(True)
        sig = self.sig

        def work():
            entry = F.add_own(key, src, sig.say.emit)
            sig.say.emit("Scanning with Microsoft Defender…")
            # scan what the user picked, where it is (Defender also looks inside archives), then the unpacked copy
            clean, msg = F.defender_scan(src)
            if clean is not False and entry.get("copied"):
                c2, m2 = F.defender_scan(entry["root"])
                if c2 is False or clean is None:
                    clean, msg = c2, m2
            entry["scan_clean"], entry["scan"] = clean, msg
            if entry.get("exe"):
                st, signer = F.signature(entry["exe"])
                entry["signature"] = f"signed by {signer}" if st == "Valid" else "unsigned" \
                    if st == "NotSigned" else f"signature {st}"
            return entry

        def done(entry):
            self.set_busy(False)
            self.state[key] = entry
            F.save_state(self.state)
            self.fill()
            if entry.get("scan_clean") is False:
                QMessageBox.warning(self, t.name, entry["scan"] + "\n\nThe tool was added, but don't run it.")
            if not entry.get("exe"):
                self.say("No program found in it – use ⋯ → Choose a different program.", "#ffd479")
            elif len(entry.get("candidates", [])) > 1 and key == "other":
                self.pick_program(key)
            else:
                self.say(f"✔ {t.name} added: {os.path.basename(entry['exe'])} ({entry.get('signature', '')}). "
                         f"{entry.get('scan', '')}", theme.ACCENT)

        self.Task.start(work, done, lambda m: (self.set_busy(False), self.say(f"✘ {m}", theme.DANGER)))

    def pick_program(self, key: str):
        e = self.state.get(key, {})
        cands = e.get("candidates") or F.find_exes(e.get("root", ""), ("*.exe",))
        if not cands:
            return
        root = e.get("root", "")
        labels = [os.path.relpath(c, root) if root else c for c in cands]
        cur = labels.index(os.path.relpath(e["exe"], root)) if e.get("exe") in cands else 0
        choice, ok = QInputDialog.getItem(self, F.by_key(key).name, "Program to launch:", labels, cur, False)
        if ok:
            e["exe"] = cands[labels.index(choice)]
            st, signer = F.signature(e["exe"])
            e["signature"] = f"signed by {signer}" if st == "Valid" else "unsigned"
            F.save_state(self.state)
            self.fill()

    def scan(self, key: str):
        e = self.state.get(key, {})
        self.set_busy(True)
        self.say("Scanning with Microsoft Defender…")

        def done(r):
            clean, msg = r
            e["scan_clean"], e["scan"] = clean, msg
            F.save_state(self.state)
            self.set_busy(False)
            self.say(msg, theme.ACCENT if clean else theme.DANGER if clean is False else "")
        self.Task.start(lambda: F.defender_scan(e.get("root") or os.path.dirname(e["exe"])), done,
                        lambda m: (self.set_busy(False), self.say(f"✘ {m}", theme.DANGER)))

    def remove(self, key: str):
        import shutil
        e = self.state.get(key, {})
        t = F.by_key(key)
        owned = t.kind == "official" or e.get("copied")
        text = (f"Remove {t.name}?" + (" Its files in the tools folder are deleted." if owned else
                                       " Only the link is removed; your own folder is left as it is."))
        if QMessageBox.question(self, "Remove", text) != QMessageBox.StandardButton.Yes:
            return
        if owned:
            shutil.rmtree(os.path.join(F.tools_dir(), key), ignore_errors=True)
        self.state.pop(key, None)
        F.save_state(self.state)
        F.write_shims(self.state)
        self.fill()
        self.say(f"Removed {t.name}.")
