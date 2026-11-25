"""Reset & Restore – back up a phone, factory reset it, walk through the fresh setup, reinstall
AndroOpenHeart and restore everything. Each step can also be used on its own (backup only / restore only).

The wizard remembers where it is (api settings), so after the reset you can reopen it and continue.
"""
import datetime as dt
import os
import re
import shutil

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from . import engine as E

STEPS = ["Start", "1  Back up", "2  Check backup", "3  Factory reset", "4  Set up the phone",
         "5  USB, Magisk, AndroOpenHeart", "6  Root", "7  Restore", "Done"]
ITEMS = [  # key, label, needs root
    ("apps", "Apps (user-installed APKs)", False),
    ("app_data", "App data (/data/data, /data/user_de, Android/data)", True),
    ("storage", "Internal storage (photos, downloads, documents…)", False),
    ("telephony", "Messages (SMS / MMS)", True),
    ("contacts", "Contacts and call log", True),
    ("wifi", "Saved Wi-Fi networks", True),
    ("magisk", "Magisk modules, Zygisk / DenyList settings", True),
]

DEV_STEPS = {
    "oneplus": ("Settings → About device → Version → tap <b>Build number</b> 7 times",
                "Settings → System settings → Developer options → <b>USB debugging</b>"),
    "samsung": ("Settings → About phone → Software information → tap <b>Build number</b> 7 times",
                "Settings → Developer options (at the bottom) → <b>USB debugging</b>"),
    "xiaomi": ("Settings → About phone → tap <b>OS version</b> (MIUI / HyperOS version) 7 times",
               "Settings → Additional settings → Developer options → <b>USB debugging</b> "
               "(and <b>USB debugging (Security settings)</b>)"),
    "motorola": ("Settings → About phone → tap <b>Build number</b> 7 times",
                 "Settings → System → Developer options → <b>USB debugging</b>"),
    "default": ("Settings → About phone (or tablet) → tap <b>Build number</b> 7 times "
                "(you'll see “You are now a developer”)",
                "Settings → System → Developer options → <b>USB debugging</b>"),
}
for b in ("redmi", "poco"):
    DEV_STEPS[b] = DEV_STEPS["xiaomi"]
for b in ("oppo", "realme"):
    DEV_STEPS[b] = DEV_STEPS["oneplus"]


def register(api):
    api.add_tool("Reset & Restore", lambda: open_wizard(api), icon="restore", needs=None,
                 tooltip="Back up everything, factory reset, set the phone up again and restore (plugin)")


_open = {}


def open_wizard(api):
    w = _open.get("w")
    try:
        if w is not None and w.isVisible():
            w.raise_()
            w.activateWindow()
            return
    except RuntimeError:
        pass
    _open["w"] = Wizard(api)
    _open["w"].show()


class _Sig(QObject):
    say = Signal(str)
    prog = Signal(int)


class Wizard(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.th = api.theme
        self.setWindowTitle("Reset & Restore")
        self.resize(1020, 700)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.sig = _Sig()
        self.sig.say.connect(self.log_line)
        self.sig.prog.connect(self.add_progress)
        self.cancel_flag = False
        self.busy = False
        self.info, self.apps, self.est = {}, [], {}
        self.backup_dir = api.get_setting("backup_dir", "")
        self.mode = api.get_setting("mode", "full")

        outer = QHBoxLayout(self)
        self.steps = QListWidget()
        self.steps.setFixedWidth(210)
        self.steps.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for s in STEPS:
            self.steps.addItem(QListWidgetItem(s))
        self.steps.currentRowChanged.connect(lambda r: self.go(r, from_list=True))
        outer.addWidget(self.steps)
        right = QVBoxLayout()
        self.stack = QStackedWidget()
        right.addWidget(self.stack, 1)
        self.bar = QProgressBar()
        self.bar.hide()
        right.addWidget(self.bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        self.log.setStyleSheet("font-family: Consolas, monospace;")
        right.addWidget(self.log)
        nav = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        nav.addWidget(self.status, 1)
        self.b_cancel = QPushButton("Cancel")
        self.b_cancel.hide()
        self.b_cancel.clicked.connect(self.cancel)
        nav.addWidget(self.b_cancel)
        self.b_back = QPushButton("◀ Back")
        self.b_back.clicked.connect(lambda: self.go(self.stack.currentIndex() - 1))
        nav.addWidget(self.b_back)
        self.b_next = QPushButton("Next ▶")
        self.b_next.clicked.connect(lambda: self.go(self.stack.currentIndex() + 1))
        nav.addWidget(self.b_next)
        right.addLayout(nav)
        outer.addLayout(right, 1)

        for build in (self.page_start, self.page_backup, self.page_verify, self.page_reset, self.page_setup,
                      self.page_usb, self.page_root, self.page_restore, self.page_done):
            self.stack.addWidget(build())
        for b in self.findChildren(QPushButton):
            b.setAutoDefault(False)
        self.adb_timer = QTimer(self)
        self.adb_timer.timeout.connect(self.poll_adb)
        self.go(0)

    # ---------------------------------------------------------------------------
    # helpers
    # ---------------------------------------------------------------------------
    def say(self, text, color=""):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color or self.th.TEXT_DIM};")

    def log_line(self, text):
        self.log.appendPlainText(f"{dt.datetime.now():%H:%M:%S}  {text}")
        if self.backup_dir and os.path.isdir(self.backup_dir):
            try:
                with open(os.path.join(self.backup_dir, "wizard.log"), "a", encoding="utf-8") as f:
                    f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n")
            except OSError:
                pass

    def add_progress(self, n):
        self.bar.setValue(min(self.bar.maximum(), self.bar.value() + max(1, n // 1024)))

    def save_state(self, page=None):
        self.api.set_setting("backup_dir", self.backup_dir)
        self.api.set_setting("mode", self.mode)
        if page is not None:
            self.api.set_setting("page", page)

    def dev(self):
        c = self.api.conn
        if not c:
            raise RuntimeError("Not connected – connect to the phone first")
        return E.Device(c)

    def root(self) -> bool:
        return self.api.is_root()

    @staticmethod
    def text(html, dim=False, color=None):
        lbl = QLabel(html)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setOpenExternalLinks(True)
        if dim or color:
            lbl.setStyleSheet(f"color: {color or '#9aa0a8'};")
        return lbl

    def page(self, title):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 4, 8, 4)
        h = QLabel(title)
        h.setStyleSheet("font-size: 15pt; font-weight: 600;")
        lay.addWidget(h)
        return w, lay

    def run(self, fn, done, total_kb=0, what="Working…"):
        """Run fn() in the background with the progress bar; done(result) on success."""
        self.busy = True
        self.cancel_flag = False
        self.bar.setMaximum(max(1, int(total_kb)))
        self.bar.setValue(0)
        self.bar.setVisible(bool(total_kb))
        self.b_cancel.show()
        self.update_nav()
        self.say(what)

        def ok(r):
            self.busy = False
            self.b_cancel.hide()
            self.bar.hide()
            self.update_nav()
            done(r)

        def fail(m):
            self.busy = False
            self.b_cancel.hide()
            self.bar.hide()
            self.update_nav()
            self.say("Cancelled." if "Cancelled" in m else f"✘ {m}", self.th.DANGER)
            self.log_line(f"ERROR: {m}")

        self.api.task(fn, ok, fail)

    def cancel(self):
        self.cancel_flag = True
        self.say("Cancelling after the current item…")

    def update_nav(self):
        i = self.stack.currentIndex()
        self.b_back.setEnabled(not self.busy and i > 0)
        self.b_next.setEnabled(not self.busy and i < len(STEPS) - 1 and self.can_leave(i))
        self.steps.setEnabled(not self.busy)

    def can_leave(self, i) -> bool:
        if i == 0:
            return True
        if i == 1:
            return bool(self.backup_dir and os.path.isfile(os.path.join(self.backup_dir, "manifest.json")))
        if i == 2:
            return getattr(self, "verified_ok", False)
        return True

    def go(self, i, from_list=False):
        if self.busy:
            return
        i = max(0, min(i, len(STEPS) - 1))
        # skip pages that don't belong to the chosen mode
        if self.mode == "backup" and i >= 3:
            i = 8
        if self.mode == "restore" and 1 <= i <= 5:
            i = 6 if i > self.stack.currentIndex() else 0
        self.stack.setCurrentIndex(i)
        self.steps.blockSignals(True)
        self.steps.setCurrentRow(i)
        self.steps.blockSignals(False)
        self.adb_timer.stop()
        enter = {1: self.enter_backup, 2: self.enter_verify, 3: self.enter_reset, 5: self.enter_usb,
                 6: self.enter_root, 7: self.enter_restore}.get(i)
        if enter:
            enter()
        if i == 8:
            self.dn_text.setText(self.go_done_text())
            self.dn_reboot.setVisible(self.mode != "backup")
        if i >= 3:
            self.save_state(i)
        self.update_nav()

    # ---------------------------------------------------------------------------
    # pages
    # ---------------------------------------------------------------------------
    def page_start(self):
        w, lay = self.page("Reset & Restore")
        lay.addWidget(self.text(
            "This wizard backs up your phone to this PC, factory-resets it, walks you through setting it up "
            "again and turning USB debugging back on, reinstalls AndroOpenHeart, and restores everything.<br><br>"
            "<b>What can be backed up:</b> apps, app data and internal storage, plus (with root) messages, "
            "contacts and call log, saved Wi-Fi networks and Magisk modules. Without root, only apps and internal "
            "storage can be saved.<br><br>"
            "<b>Not included:</b> eSIM profiles, fingerprints / face unlock, screen lock and Google account "
            "sign-in, payment cards, Signal / WhatsApp chats (back those up in their own apps), and "
            "Superuser grants (apps ask again)."))
        self.resume = self.text("", color="#ffd479")
        lay.addWidget(self.resume)
        grid = QGridLayout()
        for r, (mode, title, desc) in enumerate((
                ("full", "Full: back up → reset → restore", "The whole guided process"),
                ("backup", "Back up only", "Save a backup to this PC and stop"),
                ("restore", "Restore a backup", "Restore an existing backup to this phone"))):
            b = QPushButton(title)
            b.setMinimumHeight(40)
            b.clicked.connect(lambda _c=False, m=mode: self.start(m))
            grid.addWidget(b, r, 0)
            grid.addWidget(self.text(desc, dim=True), r, 1)
        lay.addLayout(grid)
        self.b_resume = QPushButton("Continue where I left off")
        self.b_resume.setMinimumHeight(40)
        self.b_resume.clicked.connect(lambda: self.go(int(self.api.get_setting("page", 0))))
        lay.addWidget(self.b_resume)
        lay.addStretch(1)
        page = int(self.api.get_setting("page", 0) or 0)
        if page >= 3 and self.backup_dir and os.path.isdir(self.backup_dir):
            self.resume.setText(f"A {self.mode} run is in progress (step “{STEPS[page].strip()}”), backup: "
                                f"{self.backup_dir}")
        else:
            self.resume.hide()
            self.b_resume.hide()
        return w

    def start(self, mode):
        self.mode = mode
        if mode == "restore":
            d = QFileDialog.getExistingDirectory(self, "Choose the backup folder (it contains manifest.json)",
                                                 self.backups_root())
            if not d:
                return
            if not os.path.isfile(os.path.join(d, "manifest.json")):
                QMessageBox.warning(self, "Restore", "That folder has no manifest.json – not a backup made here.")
                return
            self.backup_dir = d
            self.verified_ok = True
            self.save_state(6)
            self.go(6)
        else:
            self.backup_dir = ""
            self.save_state(1)
            self.go(1)

    def backups_root(self):
        return os.path.join(os.path.expanduser("~"), "Documents", "AndroUnderTheHood", "Backups")

    # ---- 1 backup ----------------------------------------------------------------
    def page_backup(self):
        w, lay = self.page("1  Back up to this PC")
        self.bk_info = self.text("", dim=True)
        lay.addWidget(self.bk_info)
        self.bk_checks = {}
        grid = QGridLayout()
        for r, (key, label, need_root) in enumerate(ITEMS):
            cb = QCheckBox(label)
            cb.setChecked(True)
            self.bk_checks[key] = cb
            grid.addWidget(cb, r, 0)
            sz = QLabel("")
            sz.setStyleSheet(f"color: {self.th.TEXT_DIM};")
            cb.setProperty("size_label", sz)
            grid.addWidget(sz, r, 1)
        lay.addLayout(grid)
        row = QHBoxLayout()
        row.addWidget(QLabel("Save to:"))
        self.bk_dest = QLineEdit()
        row.addWidget(self.bk_dest, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(self.pick_dest)
        row.addWidget(b)
        lay.addLayout(row)
        row = QHBoxLayout()
        self.bk_apps_btn = QPushButton("Choose apps…")
        self.bk_apps_btn.clicked.connect(self.choose_apps)
        row.addWidget(self.bk_apps_btn)
        self.bk_space = self.text("", dim=True)
        row.addWidget(self.bk_space, 1)
        self.bk_start = QPushButton("Start backup")
        self.bk_start.setStyleSheet(f"QPushButton {{ background: {self.th.ACCENT}; color: #0d1f14; "
                                    "font-weight: 600; padding: 6px 18px; }")
        self.bk_start.clicked.connect(self.do_backup)
        row.addWidget(self.bk_start)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def enter_backup(self):
        if not self.api.connected():
            self.bk_info.setText("<b>Connect to the phone first</b> (over SSH / AndroOpenHeart or ADB), with Root on for "
                                 "a complete backup.")
            self.bk_start.setEnabled(False)
            return
        root = self.root()
        for key, label, need_root in ITEMS:
            cb = self.bk_checks[key]
            cb.setEnabled(root or not need_root)
            if need_root and not root:
                cb.setChecked(False)
                cb.setToolTip("Needs root – turn Root on")
        self.bk_start.setEnabled(False)
        self.bk_info.setText(f"Device: <b>{self.api.device_name}</b> – root: "
                             f"<b>{'yes' if root else 'no (turn Root on for app data, messages…)'}</b>. Measuring…")

        def work():
            dev = self.dev()
            info = E.device_info(dev)
            apps = E.list_apps(dev)
            return info, apps, E.estimate(dev, root, apps)

        def done(r):
            self.info, all_apps, self.est = r
            if not self.apps or {a["pkg"] for a in self.apps} - {a["pkg"] for a in all_apps}:
                self.apps = all_apps
            self.all_apps = all_apps
            maker = self.info.get("manufacturer", "")
            maker = "" if maker.lower() in ("", "unknown") else maker + " "
            self.bk_info.setText(f"<b>{maker}{self.info.get('model', '')}</b>, Android "
                                 f"{self.info.get('android', '')}, Magisk {self.info.get('magisk') or '—'} – "
                                 f"root: <b>{'yes' if root else 'no'}</b>. {len(all_apps)} user apps.")
            mb = lambda kb: f"~{kb / 1024:,.0f} MB" if kb < 1048576 else f"~{kb / 1048576:,.1f} GB"  # noqa: E731
            for key, kb in (("apps", self.est.get("apps", 0)), ("app_data", self.est.get("app_data", 0)),
                            ("storage", self.est.get("storage", 0))):
                self.bk_checks[key].property("size_label").setText(mb(kb))
            name = re.sub(r"[^\w.-]+", "_", f"{self.info.get('model', 'phone')}") + f"_{dt.datetime.now():%Y-%m-%d_%H%M}"
            if not self.bk_dest.text():
                self.bk_dest.setText(os.path.join(self.backups_root(), name))
            self.update_space()
            self.bk_start.setEnabled(True)
            self.say("Choose what to back up, then press Start backup.")

        self.run(work, done, what="Looking at the phone…")

    def update_space(self):
        need = sum(self.est.get(k, 0) for k in ("apps", "app_data", "storage") if self.bk_checks[k].isChecked())
        dest = self.bk_dest.text() or self.backups_root()
        probe = dest
        while probe and not os.path.exists(probe):
            probe = os.path.dirname(probe)
        free = shutil.disk_usage(probe or os.path.expanduser("~")).free // 1024
        ok = free > need * 1.1 + 1048576
        self.bk_space.setText(f"Needs about {need / 1048576:.1f} GB, {free / 1048576:.1f} GB free on this PC"
                              + ("" if ok else " – NOT ENOUGH SPACE"))
        self.bk_space.setStyleSheet(f"color: {self.th.TEXT_DIM if ok else self.th.DANGER};")
        return ok

    def pick_dest(self):
        d = QFileDialog.getExistingDirectory(self, "Folder to put the backup in", self.backups_root())
        if d:
            self.bk_dest.setText(os.path.join(d, os.path.basename(self.bk_dest.text() or "backup")))
            self.update_space()

    def choose_apps(self):
        if not getattr(self, "all_apps", None):
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Apps to back up")
        dlg.resize(460, 560)
        v = QVBoxLayout(dlg)
        lst = QListWidget()
        chosen = {a["pkg"] for a in self.apps}
        for a in self.all_apps:
            it = QListWidgetItem(a["pkg"])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if a["pkg"] in chosen else Qt.CheckState.Unchecked)
            lst.addItem(it)
        v.addWidget(lst)
        ok = QPushButton("OK")
        ok.clicked.connect(dlg.accept)
        v.addWidget(ok)
        if dlg.exec():
            keep = {lst.item(i).text() for i in range(lst.count())
                    if lst.item(i).checkState() == Qt.CheckState.Checked}
            self.apps = [a for a in self.all_apps if a["pkg"] in keep]
            self.say(f"{len(self.apps)} of {len(self.all_apps)} apps selected.")

    def do_backup(self):
        dest = self.bk_dest.text().strip()
        if not dest:
            return
        if not self.update_space() and QMessageBox.question(
                self, "Back up", "There may not be enough free space on this PC. Continue anyway?") \
                != QMessageBox.StandardButton.Yes:
            return
        if os.path.exists(os.path.join(dest, "manifest.json")):
            QMessageBox.warning(self, "Back up", "That folder already holds a backup – choose another one.")
            return
        items = {k: cb.isChecked() and cb.isEnabled() for k, cb in self.bk_checks.items()}
        items["root"] = self.root()
        self.backup_dir = dest
        os.makedirs(dest, exist_ok=True)
        self.save_state()
        total = sum(self.est.get(k, 0) for k in ("apps", "app_data", "storage") if items.get(k)) + 4096
        sig, apps, info = self.sig, list(self.apps), dict(self.info)

        def work():
            bk = E.Backup(self.dev(), dest, sig.say.emit, sig.prog.emit, lambda: self.cancel_flag)
            return bk.run(items, apps, info)

        def done(man):
            n = man["total_bytes"] / 1048576
            self.log_line(f"Backup finished: {len(man['files'])} files, {n:,.1f} MB, {len(man['errors'])} warning(s)")
            for e in man["errors"]:
                self.log_line(f"  warning: {e}")
            self.say(f"✔ Backup saved ({n:,.1f} MB). Next: check it.", self.th.ACCENT)
            self.go(2)

        self.run(work, done, total, "Backing up…")

    # ---- 2 verify ----------------------------------------------------------------
    def page_verify(self):
        w, lay = self.page("2  Check the backup")
        lay.addWidget(self.text("Every file is read back and compared with what was received from the phone, "
                                "and each archive and APK is opened. The factory reset is only offered after "
                                "this passes."))
        self.vf_result = self.text("")
        lay.addWidget(self.vf_result)
        b = QPushButton("Check again")
        b.clicked.connect(self.enter_verify)
        lay.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
        b2 = QPushButton("Open backup folder")
        b2.clicked.connect(lambda: os.startfile(self.backup_dir) if self.backup_dir else None)  # noqa: S606
        lay.addWidget(b2, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addStretch(1)
        return w

    def enter_verify(self):
        self.verified_ok = False
        if not self.backup_dir or not os.path.isfile(os.path.join(self.backup_dir, "manifest.json")):
            self.vf_result.setText("No backup yet – go back to step 1.")
            return
        man = E.load_manifest(self.backup_dir)
        total = sum(f["size"] for f in man["files"].values()) // 1024 + 1
        sig, folder = self.sig, self.backup_dir

        def done(r):
            ok, problems = r
            self.verified_ok = ok
            items = ", ".join(k for k, v in man["items"].items() if v)
            apps = sum(1 for a in man["apps"] if a.get("apks"))
            data = sum(1 for a in man["apps"] if a.get("data"))
            html = (f"<b>{'✔ Backup is complete and readable' if ok else '✘ Problems found'}</b><br>"
                    f"Folder: {folder}<br>Contains: {items}<br>{apps} apps, {data} with data, "
                    f"{man['total_bytes'] / 1048576:,.1f} MB in {len(man['files'])} files.")
            if man.get("errors"):
                html += "<br><br>Warnings while backing up:<br>" + "<br>".join(man["errors"][:10])
            if problems:
                html += "<br><br><span style='color:#ff6b6b'>" + "<br>".join(problems[:20]) + "</span>"
            self.vf_result.setText(html)
            self.say("✔ Verified." if ok else "✘ The backup has problems – back up again before resetting.",
                     self.th.ACCENT if ok else self.th.DANGER)
            self.update_nav()

        self.run(lambda: E.verify(folder, sig.say.emit, sig.prog.emit, lambda: self.cancel_flag), done, total,
                 "Checking the backup…")

    # ---- 3 reset -----------------------------------------------------------------
    def page_reset(self):
        w, lay = self.page("3  Factory reset")
        lay.addWidget(self.text(
            "<span style='color:#ff6b6b'><b>This erases everything on the phone.</b></span> Before you go on:<ul>"
            "<li><b>Google account:</b> after the reset the phone asks for the Google account that was on it "
            "(Factory Reset Protection). Make sure you know its password.</li>"
            "<li><b>Root:</b> your patched boot image stays, so root comes back once the Magisk app is "
            "reinstalled (step 6). Magisk modules and settings come back from the backup.</li>"
            "<li><b>AndroOpenHeart and Shizuku</b> are removed; step 6 reinstalls them over USB.</li>"
            "<li>Keep the phone charged and the backup folder on this PC.</li></ul>"))
        self.rs_check = QCheckBox("I know the Google account password and the backup above passed the check")
        lay.addWidget(self.rs_check)
        self.rs_label = QLabel("")
        lay.addWidget(self.rs_label)
        self.rs_confirm = QLineEdit()
        self.rs_confirm.setMaximumWidth(420)
        lay.addWidget(self.rs_confirm)
        self.rs_btn = QPushButton("Erase the phone now")
        self.rs_btn.setStyleSheet(f"QPushButton:enabled {{ background: {self.th.DANGER}; color: white; "
                                  "font-weight: 600; padding: 8px 18px; }")
        self.rs_btn.clicked.connect(self.do_reset)
        lay.addWidget(self.rs_btn, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.text(
            "Or reset it yourself on the phone: <b>Settings → System → Reset options → Erase all data "
            "(factory reset)</b> (Samsung: General management → Reset). Then press Next.", dim=True))
        self.rs_check.toggled.connect(self.update_reset)
        self.rs_confirm.textChanged.connect(self.update_reset)
        lay.addStretch(1)
        return w

    def enter_reset(self):
        model = (self.info.get("model") or (E.load_manifest(self.backup_dir)["device"].get("model")
                                            if self.backup_dir else "") or "ERASE")
        self.reset_word = model
        self.rs_label.setText(f"Type the model name <b>{model}</b> to confirm:")
        self.rs_label.setTextFormat(Qt.TextFormat.RichText)
        self.update_reset()

    def update_reset(self):
        root = self.root()
        ok = (self.rs_check.isChecked() and self.rs_confirm.text().strip() == getattr(self, "reset_word", "?")
              and getattr(self, "verified_ok", False) and root)
        self.rs_btn.setEnabled(ok)
        self.rs_btn.setToolTip("" if root else "Needs root – otherwise reset from the phone's Settings")

    def do_reset(self):
        if QMessageBox.warning(self, "Factory reset", f"Erase {self.api.device_name} now? This cannot be undone.",
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                               QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.save_state(4)
        self.log_line("Factory reset requested")

        def done(out):
            self.log_line(f"Phone answered: {out[:200]}")
            self.say("The phone is erasing itself and will restart into setup. That takes a few minutes.",
                     self.th.ACCENT)
            self.api.window.disconnect()
            self.go(4)

        self.run(lambda: E.factory_reset(self.dev()), done, what="Sending the factory reset…")

    # ---- 4 setup -----------------------------------------------------------------
    def page_setup(self):
        w, lay = self.page("4  Set up the phone")
        lay.addWidget(self.text(
            "When the phone shows its welcome screen, go through setup:<ol>"
            "<li>Choose your language and connect to <b>Wi-Fi</b>.</li>"
            "<li>Sign in with the <b>same Google account</b> as before (Factory Reset Protection asks for it).</li>"
            "<li>When it offers to <b>copy apps & data</b> or restore from a Google backup, choose "
            "<b>Don't copy / Skip</b>. This wizard restores everything, including app data Google doesn't "
            "back up.</li>"
            "<li>Set a screen lock if you want. You can skip the other offers.</li>"
            "<li>Finish until you reach the home screen, then press <b>Next</b>.</li></ol>"))
        lay.addStretch(1)
        return w

    # ---- 5 USB, Magisk, AndroOpenHeart --------------------------------------------------
    def page_usb(self):
        w, lay = self.page("5  USB debugging, Magisk and AndroOpenHeart")
        self.usb_text = self.text("")
        lay.addWidget(self.usb_text)
        self.usb_state = self.text("")
        self.usb_state.setStyleSheet("font-size: 12pt;")
        lay.addWidget(self.usb_state)
        grid = QGridLayout()
        self.usb_magisk = QPushButton("①  Install Magisk app over USB")
        self.usb_magisk.clicked.connect(self.usb_install_magisk)
        grid.addWidget(self.usb_magisk, 0, 0)
        grid.addWidget(self.text("From your backup. Then open <b>Magisk</b> on the phone; if it says <i>Requires "
                                 "additional setup</i> tap <b>OK</b> and let it reboot.", dim=True), 0, 1)
        self.usb_root = QPushButton("②  Check root over USB")
        self.usb_root.clicked.connect(self.usb_check_root)
        grid.addWidget(self.usb_root, 1, 0)
        grid.addWidget(self.text("Tap <b>Grant</b> when Magisk asks about <b>Shell</b>.", dim=True), 1, 1)
        self.usb_btn = QPushButton("③  Install AndroOpenHeart + Shizuku && connect")
        self.usb_btn.setStyleSheet(f"QPushButton:enabled {{ background: {self.th.ACCENT}; color: #0d1f14; "
                                   "font-weight: 600; padding: 8px 18px; }")
        self.usb_btn.clicked.connect(self.setup_openheart)
        grid.addWidget(self.usb_btn, 2, 0)
        grid.addWidget(self.text("Installs and configures them, then reconnects AndroUnderTheHood over SSH "
                                 "(through the USB cable if the phone's Wi-Fi address can't be reached).", dim=True),
                       2, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        b = QPushButton("USB drivers…")
        b.clicked.connect(self.api.window.open_drivers)
        lay.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addStretch(1)
        for x in (self.usb_magisk, self.usb_root, self.usb_btn):
            x.setEnabled(False)
        return w

    def enter_usb(self):
        dev = {}
        try:
            dev = E.load_manifest(self.backup_dir)["device"] if self.backup_dir else self.info
        except Exception:  # noqa: BLE001
            dev = self.info
        brand = (dev.get("brand") or dev.get("manufacturer") or "").lower()
        a, b = DEV_STEPS.get(brand, DEV_STEPS["default"])
        self.usb_text.setText(
            "<b>USB debugging</b> (skip this if the status below already says <i>Connected over USB</i>; some "
            "builds have it on after a reset):<ol>"
            "<li><b>Developer options:</b> " + a + ".</li>"
            "<li><b>USB debugging:</b> " + b + " → turn it on.</li>"
            "<li>Connect the phone to this PC with a <b>USB cable</b> and allow <b>“Allow USB debugging?”</b> "
            "(tick <b>Always allow from this computer</b>).</li></ol>"
            "Then do ① ② ③ in order. AndroOpenHeart's setup needs root, and root comes back with the Magisk app.")
        self.adb_serial = None
        self.usb_root_ok = False
        self.adb_timer.start(2000)
        self.poll_adb()

    def poll_adb(self):
        def work():
            from androunderthehood import adbconn
            devs = adbconn.adb_devices()
            usb = [d for d in devs if d["transport"] == "usb"]
            magisk = ""
            if usb and usb[0]["state"] == "device":
                r = adbconn.adb(["-s", usb[0]["serial"], "shell",
                                 "dumpsys package com.topjohnwu.magisk | grep -m1 versionName"], timeout=20)
                magisk = r.stdout.decode(errors="replace").strip().replace("versionName=", "")
            return usb, magisk

        def done(r):
            usb, magisk = r
            ready = bool(usb) and usb[0]["state"] == "device"
            if not usb:
                self.usb_state.setText("⏳ Waiting for the phone on USB…")
            elif usb[0]["state"] == "unauthorized":
                self.usb_state.setText("📱 Phone found – <b>allow USB debugging on its screen</b>")
            elif ready:
                self.adb_serial = usb[0]["serial"]
                m = ("Magisk app " + magisk) if magisk and magisk != "1.0" else \
                    "Magisk placeholder app only – press ①" if magisk else "no Magisk app – press ①"
                self.usb_state.setText(f"✔ Connected over USB: <b>{usb[0]['model'] or usb[0]['serial']}</b> – {m}"
                                       + (" – root ✔" if self.usb_root_ok else ""))
            else:
                self.usb_state.setText(f"Phone state: {usb[0]['state']}")
            has_full = bool(magisk) and magisk != "1.0"
            in_backup = os.path.isfile(os.path.join(self.backup_dir or "", "apps", "com.topjohnwu.magisk", "base.apk"))
            self.usb_magisk.setEnabled(ready and not self.busy and in_backup)
            self.usb_magisk.setText("①  Magisk app installed ✔" if has_full else "①  Install Magisk app over USB")
            self.usb_root.setEnabled(ready and not self.busy)
            self.usb_btn.setEnabled(ready and not self.busy)
            if self.api.connected() and not getattr(self.api.conn, "is_adb", False):
                self.usb_state.setText("✔ AndroUnderTheHood is connected to the phone over SSH – press Next")
                self.adb_timer.stop()

        from androunderthehood.ui import Task
        Task.start(work, done, lambda m: self.usb_state.setText(f"ADB: {m}"))

    def usb_install_magisk(self):
        apk = os.path.join(self.backup_dir, "apps", "com.topjohnwu.magisk", "base.apk")
        serial = self.adb_serial

        def work():
            from androunderthehood import adbconn
            r = adbconn.adb(["-s", serial, "install", "-r", apk], timeout=180)
            out = (r.stdout + r.stderr).decode(errors="replace")
            if "UPDATE_INCOMPATIBLE" in out or "signatures do not match" in out:
                # Magisk's own placeholder app is signed differently: replace it
                adbconn.adb(["-s", serial, "uninstall", "com.topjohnwu.magisk"], timeout=60)
                r = adbconn.adb(["-s", serial, "install", apk], timeout=180)
                out = (r.stdout + r.stderr).decode(errors="replace")
            if "Success" not in out:
                raise RuntimeError(out.strip()[-300:])
            return out

        self.run(work, lambda _r: self.say("✔ Magisk app installed. Open it on the phone (accept the additional "
                                           "setup and reboot if it asks), then press ②.", self.th.ACCENT),
                 what="Installing the Magisk app over USB…")

    def usb_check_root(self):
        serial = self.adb_serial
        self.say("Asking for root – tap Grant on the phone when Magisk asks about Shell…")

        def work():
            from androunderthehood import adbconn
            r = adbconn.adb(["-s", serial, "shell", "su -c id -u"], timeout=90)
            return r.stdout.decode(errors="replace").strip()

        def done(out):
            self.usb_root_ok = out.endswith("0")
            self.say("✔ Root works over USB – press ③." if self.usb_root_ok else
                     "✘ No root yet – open Magisk (finish its setup / reboot) and press ② again.",
                     self.th.ACCENT if self.usb_root_ok else self.th.DANGER)
            self.poll_adb()

        self.run(work, done, what="Waiting for root…")

    def setup_openheart(self):
        if self.adb_serial:
            self.api.window.setup_openheart(self.adb_serial, self)

    # ---- 6 root over SSH -----------------------------------------------------------------
    def page_root(self):
        w, lay = self.page("6  Root for AndroUnderTheHood")
        self.rt_state = self.text("")
        lay.addWidget(self.rt_state)
        lay.addWidget(self.text(
            "App data, messages, contacts, Wi-Fi and Magisk modules are restored as root. Press <b>Turn Root "
            "on</b> (tap <b>Grant</b> if Magisk asks about AndroOpenHeart), then <b>Next</b>.<br><br>"
            "Without root you can still continue: apps and internal storage are restored, the rest is skipped."))
        row = QHBoxLayout()
        self.rt_root = QPushButton("Turn Root on")
        self.rt_root.clicked.connect(self.turn_root_on)
        row.addWidget(self.rt_root)
        b = QPushButton("Check again")
        b.clicked.connect(self.enter_root)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def enter_root(self):
        if not self.api.connected():
            self.rt_state.setText("<b>Not connected</b> – finish step 5 first.")
            self.rt_root.setEnabled(False)
            return
        root = self.root()
        self.rt_state.setText(f"Connected to <b>{self.api.device_name}</b>. Root: "
                              f"<b>{'✔ working' if root else 'not yet'}</b>")
        self.rt_root.setEnabled(not root)

    def turn_root_on(self):
        win = self.api.window
        if not win.a_root.isChecked():
            win.a_root.setChecked(True)
        win.toggle_root(True)
        self.say("Reconnecting with Root on – tap Grant if Magisk asks on the phone, then press Check again.")

    # ---- 7 restore -----------------------------------------------------------------
    def page_restore(self):
        w, lay = self.page("7  Restore")
        self.rr_info = self.text("", dim=True)
        lay.addWidget(self.rr_info)
        self.rr_checks = {}
        for key, label, need_root in ITEMS:
            cb = QCheckBox(label)
            self.rr_checks[key] = cb
            lay.addWidget(cb)
        row = QHBoxLayout()
        row.addStretch(1)
        self.rr_start = QPushButton("Start restore")
        self.rr_start.setStyleSheet(f"QPushButton:enabled {{ background: {self.th.ACCENT}; color: #0d1f14; "
                                    "font-weight: 600; padding: 6px 18px; }")
        self.rr_start.clicked.connect(self.do_restore)
        row.addWidget(self.rr_start)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def enter_restore(self):
        if not self.backup_dir:
            self.rr_info.setText("No backup chosen.")
            self.rr_start.setEnabled(False)
            return
        man = E.load_manifest(self.backup_dir)
        root = self.root()
        connected = self.api.connected()
        same = man["device"].get("model") == (self.info.get("model") or man["device"].get("model"))
        self.rr_info.setText(f"Backup of <b>{man['device'].get('manufacturer', '')} {man['device'].get('model', '')}"
                             f"</b> (Android {man['device'].get('android', '')}) from {man['created']}.<br>"
                             f"Connected: <b>{'yes' if connected else 'no'}</b>, root: "
                             f"<b>{'yes' if root else 'no'}</b>" + ("" if same else
                                                                    " – <b>a different model</b>: app data and "
                                                                    "system data may not fit it."))
        for key, label, need_root in ITEMS:
            cb = self.rr_checks[key]
            have = bool(man["items"].get(key))
            cb.setEnabled(have and (root or not need_root))
            cb.setChecked(have and (root or not need_root))
            cb.setText(label + ("" if have else "  – not in this backup") +
                       ("  – needs root" if have and need_root and not root else ""))
        self.rr_start.setEnabled(connected)

    def do_restore(self):
        items = {k: cb.isChecked() and cb.isEnabled() for k, cb in self.rr_checks.items()}
        if not any(items.values()):
            return
        man = E.load_manifest(self.backup_dir)
        total = sum(m["size"] for rel, m in man["files"].items() if
                    (rel.startswith("apps/") and items["apps"]) or (rel.startswith("data/") and items["app_data"])
                    or (rel == "storage.tar" and items["storage"])) // 1024 + 1
        folder, sig, root = self.backup_dir, self.sig, self.root()

        def work():
            r = E.Restore(self.dev(), folder, sig.say.emit, sig.prog.emit, lambda: self.cancel_flag)
            return r.run(items, root)

        def done(r):
            done_items, errors = r
            self.log_line(f"Restore finished: {', '.join(done_items)}; {len(errors)} problem(s)")
            for e in errors:
                self.log_line(f"  {e}")
            self.restore_result = (done_items, errors)
            self.go(8)

        self.run(work, done, total, "Restoring…")

    # ---- done ----------------------------------------------------------------------
    def page_done(self):
        w, lay = self.page("Done")
        self.dn_text = self.text("")
        lay.addWidget(self.dn_text)
        row = QHBoxLayout()
        self.dn_reboot = QPushButton("Reboot the phone now")
        self.dn_reboot.clicked.connect(self.reboot)
        row.addWidget(self.dn_reboot)
        b = QPushButton("Open backup folder")
        b.clicked.connect(lambda: os.startfile(self.backup_dir) if self.backup_dir else None)  # noqa: S606
        row.addWidget(b)
        b2 = QPushButton("Finish")
        b2.clicked.connect(self.finish)
        row.addWidget(b2)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def finish(self):
        self.api.set_setting("page", 0)
        self.close()

    def reboot(self):
        if QMessageBox.question(self, "Reboot", "Reboot the phone now?") != QMessageBox.StandardButton.Yes:
            return
        self.api.task(lambda: self.dev().sh("(sleep 1; svc power reboot || reboot) >/dev/null 2>&1 &", root=True),
                      lambda _r: self.say("Rebooting…"))

    def go_done_text(self):
        done_items, errors = getattr(self, "restore_result", ([], []))
        if self.mode == "backup":
            return f"✔ Backup saved in <b>{self.backup_dir}</b>."
        txt = "<b>Restore finished.</b> Restored: " + (", ".join(done_items) or "nothing") + "."
        if errors:
            txt += "<br><br>Problems:<br>" + "<br>".join(errors[:15])
        txt += ("<br><br><b>Reboot the phone</b> so Wi-Fi networks, Magisk modules and messages are picked up. "
                "Then open your apps; ones that use Google sign-in may ask you to sign in again.")
        return txt

