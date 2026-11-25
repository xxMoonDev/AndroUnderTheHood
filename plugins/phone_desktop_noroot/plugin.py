"""Phone desktop (no root) – Ubuntu 24.04 + KDE Plasma + RustDesk under proot (AndroUnderTheHood plugin).

The no-root twin of the phone_desktop plugin: same window, but everything runs as Android's shell
user (ADB, or SSH through Shizuku) instead of a root chroot. The install runs on the device as a
detached job, so closing this window or losing the connection does not stop it.
"""
import os
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextCursor
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QSplitter,
                               QVBoxLayout, QWidget)

from . import engine as E

TITLE = "Phone desktop (no root)"
_windows = {}


def register(api):
    api.add_tool(TITLE, lambda: open_window(api), icon="icon.png",
                 tooltip="Ubuntu + KDE Plasma desktop on the phone without root (proot), reachable with RustDesk "
                         "(plugin)",
                 needs="shell")


def unload(api):
    for w in list(_windows.values()):
        try:
            w.close()
        except RuntimeError:
            pass
    _windows.clear()


def open_window(api):
    w = _windows.get("main")
    try:
        if w is not None and w.isVisible():
            w.raise_()
            w.activateWindow()
            return
    except RuntimeError:
        pass
    w = NoRootDesktopWindow(api)
    _windows["main"] = w
    w.show()


def default_files_dir(api):
    """Shares the root plugin's folder (same Ubuntu tarball, .debs and offline bundle)."""
    desk = os.path.join(os.path.expanduser("~"), "Desktop", "PhoneDesktop")
    return desk if os.path.isdir(os.path.join(desk, "cache")) else api.data_dir


class Bridge(QObject):
    log = Signal(str)
    step = Signal(str, str, str)
    progress = Signal(float, str, float)
    finished = Signal(bool, str, object)
    probed = Signal(object)


class UiSink:
    """What the engine calls from the worker thread; forwards through Qt signals."""

    def __init__(self, bridge):
        self.b = bridge

    def log(self, text):
        self.b.log.emit(text)

    def step(self, key, state, detail=""):
        self.b.step.emit(key, state, detail)

    def progress(self, overall, label, phase):
        self.b.progress.emit(float(overall), label, float(phase))


MARK = {"wait": "○", "run": "◐", "ok": "✓", "fail": "✗", "skip": "–"}


class NoRootDesktopWindow(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        t = api.theme
        self.setWindowTitle(f"{TITLE} – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1000, 820)
        self.bridge = Bridge()
        self.bridge.log.connect(self.on_log)
        self.bridge.step.connect(self.on_step)
        self.bridge.progress.connect(self.on_progress)
        self.bridge.finished.connect(self.on_finished)
        self.bridge.probed.connect(self.show_probe)
        self.engine = None
        self.worker = None
        self.op = ""

        root = QVBoxLayout(self)
        self.header = QLabel("Checking the device…")
        self.header.setStyleSheet("font-size: 12pt; font-weight: 600;")
        self.header.setWordWrap(True)
        root.addWidget(self.header)
        self.sub = QLabel("")
        self.sub.setStyleSheet(f"color: {t.TEXT_DIM};")
        self.sub.setWordWrap(True)
        self.sub.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.sub)

        split = QSplitter(Qt.Orientation.Vertical)
        root.addWidget(split, 1)
        top = QWidget()
        tl = QHBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        split.addWidget(top)

        # ---- options
        opt = QGroupBox("Setup")
        fl = QFormLayout(opt)
        self.profile = QComboBox()
        for key, label in (("standard", "Standard – Plasma + KDE apps (kde-standard)"),
                           ("minimal", "Minimal – Plasma, Konsole, Dolphin, Kate"),
                           ("full", "Full – every KDE application (kde-full)")):
            self.profile.addItem(label, key)
        self.profile.setCurrentIndex(max(0, self.profile.findData(api.get_setting("profile", "standard"))))
        fl.addRow("KDE packages", self.profile)
        self.res = QLineEdit(api.get_setting("res", "1920x1080"))
        fl.addRow("Screen size", self.res)
        self.user = QLineEdit(api.get_setting("user", "kde"))
        fl.addRow("Ubuntu user", self.user)
        self.mode = QComboBox()
        for key, label in (("direct", "Direct IP (no ID server)"),
                           ("public", "Public RustDesk servers (connect by ID)"),
                           ("both", "Public servers + direct IP"),
                           ("custom", "My own RustDesk server")):
            self.mode.addItem(label, key)
        self.mode.setCurrentIndex(max(0, self.mode.findData(api.get_setting("mode", "direct"))))
        fl.addRow("RustDesk", self.mode)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(api.get_setting("port", 21118)))
        self.port.setToolTip("RustDesk's direct IP port. Use another one than 21118 if the root Phone desktop "
                             "runs on the same phone at the same time.")
        fl.addRow("Direct IP port", self.port)
        self.server = QLineEdit(api.get_setting("server", ""))
        self.server.setPlaceholderText("hbbs host")
        self.key = QLineEdit(api.get_setting("key", ""))
        self.key.setPlaceholderText("public key")
        srow = QHBoxLayout()
        srow.addWidget(self.server, 2)
        srow.addWidget(self.key, 3)
        fl.addRow("Own server", srow)
        self.mode.currentIndexChanged.connect(self.update_mode)
        frow = QHBoxLayout()
        self.files = QLineEdit(api.get_setting("files_dir", "") or default_files_dir(api))
        self.files.editingFinished.connect(self.update_offline)
        b = QPushButton("…")
        b.setFixedWidth(32)
        b.clicked.connect(self.pick_files)
        frow.addWidget(self.files, 1)
        frow.addWidget(b)
        fl.addRow("Files folder", frow)
        self.offline = QCheckBox("Install offline from the bundle")
        self.offline_info = QLabel("")
        self.offline_info.setStyleSheet(f"color: {t.TEXT_DIM};")
        orow = QVBoxLayout()
        orow.addWidget(self.offline)
        orow.addWidget(self.offline_info)
        fl.addRow("Offline", orow)
        note = QLabel("Runs as Android's shell user under proot – connect over ADB (USB or wireless debugging) "
                      "or SSH with Shizuku. Slower than the root version, and it doesn't survive a phone reboot "
                      "(press Start again).")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {t.TEXT_DIM};")
        fl.addRow("", note)
        tl.addWidget(opt, 3)

        # ---- steps + progress
        right = QVBoxLayout()
        sg = QGroupBox("Progress")
        sl = QVBoxLayout(sg)
        self.steps = QListWidget()
        self.steps.setStyleSheet("QListWidget { border: none; background: transparent; }")
        sl.addWidget(self.steps, 1)
        self.phase_label = QLabel("")
        self.phase_label.setWordWrap(True)
        sl.addWidget(self.phase_label)
        self.phase_bar = QProgressBar()
        self.phase_bar.setTextVisible(True)
        sl.addWidget(self.phase_bar)
        ol = QLabel("Overall")
        ol.setStyleSheet(f"color: {t.TEXT_DIM};")
        sl.addWidget(ol)
        self.overall_bar = QProgressBar()
        sl.addWidget(self.overall_bar)
        right.addWidget(sg, 1)
        tl.addLayout(right, 2)

        # ---- terminal
        term_box = QWidget()
        tb = QVBoxLayout(term_box)
        tb.setContentsMargins(0, 0, 0, 0)
        trow = QHBoxLayout()
        tlab = QLabel("Terminal (device output)")
        tlab.setStyleSheet("font-weight: 600;")
        trow.addWidget(tlab)
        trow.addStretch(1)
        self.follow_tail = QCheckBox("Auto-scroll")
        self.follow_tail.setChecked(True)
        trow.addWidget(self.follow_tail)
        cb = QPushButton("Copy")
        cb.clicked.connect(lambda: QApplication.clipboard().setText(self.term.toPlainText()))
        trow.addWidget(cb)
        clb = QPushButton("Clear")
        clb.clicked.connect(lambda: self.term.clear())
        trow.addWidget(clb)
        tb.addLayout(trow)
        self.term = QPlainTextEdit()
        self.term.setReadOnly(True)
        self.term.setMaximumBlockCount(30000)
        self.term.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self.term.setFont(f)
        self.term.setStyleSheet("QPlainTextEdit { background: #0c1117; color: #d5dee8; "
                                "border: 1px solid #222b36; border-radius: 4px; }")
        tb.addWidget(self.term, 1)
        split.addWidget(term_box)
        split.setSizes([360, 460])

        # ---- buttons
        brow = QHBoxLayout()
        self.b_install = QPushButton(api.icon("download"), " Install / update")
        self.b_install.setToolTip("Installs everything that is missing (safe to re-run), configures RustDesk "
                                  "and starts the desktop")
        self.b_install.clicked.connect(self.do_install)
        self.b_start = QPushButton(api.icon("power"), " Start")
        self.b_start.clicked.connect(lambda: self.simple("start"))
        self.b_stop = QPushButton(" Stop")
        stop_menu = QMenu(self.b_stop)
        stop_menu.addAction("Shut down – Plasma logs out cleanly").triggered.connect(lambda: self.simple("stop"))
        stop_menu.addAction("Power off – stop everything now").triggered.connect(lambda: self.simple("poweroff"))
        self.b_stop.setMenu(stop_menu)
        self.b_uninstall = QPushButton(api.icon("trash"), " Uninstall…")
        self.b_uninstall.setStyleSheet(f"color: {t.DANGER};")
        self.b_uninstall.clicked.connect(self.do_uninstall)
        self.b_status = QPushButton(api.icon("refresh"), " Status")
        self.b_status.clicked.connect(lambda: self.simple("status"))
        self.b_pw = QPushButton(api.icon("lock"), " RustDesk password…")
        self.b_pw.clicked.connect(self.do_password)
        self.b_bundle = QPushButton(api.icon("package"), " Build offline bundle…")
        self.b_bundle.setToolTip("Downloads every package a fresh install needs (on this device, which must "
                                 "already have the desktop) into the files folder, for offline installs")
        self.b_bundle.clicked.connect(self.do_bundle)
        self.b_cancel = QPushButton(" Cancel")
        self.b_cancel.clicked.connect(self.do_cancel)
        for w in (self.b_install, self.b_start, self.b_stop, self.b_status, self.b_pw, self.b_bundle):
            brow.addWidget(w)
        brow.addStretch(1)
        brow.addWidget(self.b_uninstall)
        brow.addWidget(self.b_cancel)
        root.addLayout(brow)

        self.update_mode()
        self.update_offline()
        self.set_steps(E.INSTALL_STEPS)
        self.set_busy(False)
        self.probe()

    # ---- small UI helpers ---------------------------------------------------------------------
    def update_mode(self):
        custom = self.mode.currentData() == "custom"
        self.server.setEnabled(custom)
        self.key.setEnabled(custom)
        self.port.setEnabled(self.mode.currentData() != "public")

    def pick_files(self):
        d = QFileDialog.getExistingDirectory(self, "Folder for downloads and the offline bundle", self.files.text())
        if d:
            self.files.setText(os.path.normpath(d))
            self.update_offline()

    def update_offline(self):
        self.api.set_setting("files_dir", self.files.text().strip())
        info = E.Engine(self.api, None, self.files.text().strip()).offline_info()
        if info:
            self.offline.setEnabled(True)
            self.offline.setChecked(bool(self.api.get_setting("offline", True)))
            self.offline_info.setText(f"{info['packages']} packages, {info['size'] >> 20} MB, profile "
                                      f"{info['profile']}, built {info['built'] or '?'}")
        else:
            self.offline.setChecked(False)
            self.offline.setEnabled(False)
            self.offline_info.setText("No bundle in this folder (offline\\repo). Installs download packages.")

    def set_steps(self, steps):
        self.steps.clear()
        self.step_items = {}
        for key, label in steps:
            it = QListWidgetItem(f"{MARK['wait']}  {label}")
            it.setData(Qt.ItemDataRole.UserRole, label)
            it.setForeground(Qt.GlobalColor.gray)
            self.steps.addItem(it)
            self.step_items[key] = it
        self.overall_bar.setValue(0)
        self.phase_bar.setValue(0)
        self.phase_label.setText("")

    def set_busy(self, busy):
        for w in (self.b_install, self.b_start, self.b_stop, self.b_status, self.b_pw, self.b_bundle,
                  self.b_uninstall, self.profile, self.res, self.user, self.mode, self.port, self.files,
                  self.offline):
            w.setEnabled(not busy)
        if not busy:
            self.update_mode()
            self.offline.setEnabled(not self.offline_info.text().startswith("No bundle"))
        self.b_cancel.setEnabled(busy)

    def options(self):
        o = E.Options(profile=self.profile.currentData(), res=self.res.text().strip(), user=self.user.text().strip(),
                      mode=self.mode.currentData(), server=self.server.text().strip(), key=self.key.text().strip(),
                      port=self.port.value(), offline=self.offline.isChecked() and self.offline.isEnabled())
        o.validate()
        for k, v in (("profile", o.profile), ("res", o.res), ("user", o.user), ("mode", o.mode),
                     ("server", o.server), ("key", o.key), ("port", o.port), ("offline", self.offline.isChecked())):
            self.api.set_setting(k, v)
        return o

    # ---- signals from the worker -------------------------------------------------------------------
    def on_log(self, text):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        sb = self.term.verticalScrollBar()
        at_end = sb.value() >= sb.maximum() - 4
        cur = self.term.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertText(text)
        if self.follow_tail.isChecked() or at_end:
            sb.setValue(sb.maximum())

    def on_step(self, key, state, detail):
        it = self.step_items.get(key)
        if not it:
            return
        label = it.data(Qt.ItemDataRole.UserRole)
        it.setText(f"{MARK.get(state, '○')}  {label}" + (f"   –  {detail}" if detail else ""))
        t = self.api.theme
        color = {"run": t.ACCENT, "ok": "#3fb950", "fail": t.DANGER, "skip": t.TEXT_FAINT}.get(state, t.TEXT_DIM)
        it.setForeground(QColor(color))
        if state == "run":
            self.steps.scrollToItem(it)

    def on_progress(self, overall, label, phase):
        if label:
            self.phase_label.setText(label)
        if phase < 0:
            self.phase_bar.setRange(0, 0)
        else:
            self.phase_bar.setRange(0, 1000)
            self.phase_bar.setValue(int(phase * 1000))
            self.phase_bar.setFormat(f"{phase * 100:.0f} %")
        if overall >= 0:
            self.overall_bar.setRange(0, 1000)
            self.overall_bar.setValue(int(overall * 1000))
            self.overall_bar.setFormat(f"{overall * 100:.0f} %")

    def on_finished(self, ok, msg, result):
        self.set_busy(False)
        self.phase_bar.setRange(0, 1000)
        if ok:
            self.api.message(f"{TITLE}: {msg}")
            for bar in (self.overall_bar, self.phase_bar):
                bar.setRange(0, 1000)
                bar.setValue(1000)
                bar.setFormat("100 %")
            self.phase_label.setText(msg[:1].upper() + msg[1:])
        else:
            self.on_log(f"\n*** {msg}\n")
            self.api.message(f"{TITLE}: {msg}", error=True)
        if isinstance(result, dict) and "id" in result:
            self.show_status(result)
        if self.op == "bundle":
            self.update_offline()
        if self.op != "probe":
            self.probe()

    # ---- worker plumbing -----------------------------------------------------------------------------
    def run(self, op, fn, done_msg):
        if self.worker and self.worker.is_alive():
            return
        if not self.api.connected():
            QMessageBox.warning(self, TITLE, "Connect to a device first.")
            return
        self.op = op
        self.engine = E.Engine(self.api, UiSink(self.bridge), self.files.text().strip())
        eng = self.engine
        self.set_busy(True)

        def body():
            try:
                result = fn(eng)
            except E.Cancelled:
                self.bridge.finished.emit(False, "cancelled", None)
            except Exception as e:  # noqa: BLE001
                self.api.log.exception("no-root phone desktop %s failed", op)
                self.bridge.finished.emit(False, str(e) or e.__class__.__name__, None)
            else:
                self.bridge.finished.emit(True, done_msg, result)
        self.worker = threading.Thread(target=body, daemon=True, name=f"pdnoroot-{op}")
        self.worker.start()

    def probe(self):
        if not self.api.connected():
            self.header.setText("Not connected")
            return
        eng = E.Engine(self.api, UiSink(self.bridge), self.files.text().strip())

        def body():
            try:
                st = eng.probe()
            except Exception as e:  # noqa: BLE001
                st = {"error": str(e)}
            self.bridge.probed.emit(st)
        threading.Thread(target=body, daemon=True, name="pdnoroot-probe").start()

    def show_probe(self, st):
        if "error" in st:
            self.header.setText("Could not read the device state")
            self.sub.setText(st["error"])
            return
        if not st.get("ok_user"):
            self.header.setText("This connection can't run the no-root desktop")
            self.sub.setText(E.Engine.uid_problem(st.get("uid", -1)))
            return
        job = next((n for n, i in st.get("jobs", {}).items() if i and i[0] == "running"), None)
        if job:
            self.header.setText("Installing on the device…" if job == "install" else "Building the offline bundle…")
            self.sub.setText("The job runs on the phone by itself – this window follows its output.")
        elif not st.get("installed"):
            self.header.setText("Not installed on this device")
            self.sub.setText("Press Install to set up Ubuntu 24.04 + KDE Plasma + RustDesk without root "
                             f"(about 4 GB in {E.PD}).")
        else:
            state = "running" if st.get("running") else "stopped"
            self.header.setText(f"Ubuntu + KDE Plasma installed (no root) – desktop {state}")
            last = getattr(self, "last_status", None)
            if last and st.get("running"):
                self.show_status(last)
            else:
                self.sub.setText((f"RustDesk is listening on port {st.get('port')}. " if st.get("rd_listen") else "")
                                 + "Press Status for the RustDesk ID and addresses.")
        if st.get("root_desktop"):
            self.sub.setText(self.sub.text() + "  –  The root Phone desktop is installed here too: give this one "
                                               "another direct IP port if both run at once.")
        self.b_uninstall.setVisible(bool(st.get("installed")))
        for name, info in st.get("jobs", {}).items():
            if info and info[0] == "running" and not (self.worker and self.worker.is_alive()):
                self.on_log(f"[a '{name}' job is running on the device – following it]\n")
                self.attach(name)
                break

    def attach(self, name):
        if name == "install":
            self.set_steps(E.INSTALL_STEPS)
            for k in ("check", "files", "upload"):
                self.on_step(k, "ok", "")

            def fn(eng):
                eng.ui.step("install", "run")
                eng.run_install_follow()
                return None
            self.run("install", fn, "install finished – press Install again to configure and start, or Start")
        else:
            self.set_steps(E.BUNDLE_STEPS)

            def fn(eng):
                eng.ui.step("bundle", "run")
                rc = eng.follow("bundle", E.BundleProgress(), "bundle")
                if rc != 0:
                    raise RuntimeError(f"bundle job ended with exit code {rc}")
                eng.ui.step("bundle", "ok")
                return None
            self.run("bundle", fn, "bundle built on the device – press Build offline bundle again to copy it")

    def show_status(self, info):
        self.last_status = info
        addrs = ", ".join(f"{a}:{info.get('port', 21118)} ({d})" for a, d in info.get("addrs", []))
        self.sub.setText(f"RustDesk ID {info.get('id') or '?'} – connect by IP: {addrs or '?'}"
                         + ("" if info.get("listening") else "  –  RustDesk is NOT listening"))

    # ---- actions ------------------------------------------------------------------------------------------
    def do_install(self):
        try:
            opts = self.options()
        except ValueError as e:
            QMessageBox.warning(self, TITLE, str(e))
            return
        self.set_steps(E.INSTALL_STEPS)
        self.on_log(f"\n=== Install / update (no root): profile {opts.profile}, {opts.res}, user {opts.user}, "
                    f"RustDesk {opts.mode} port {opts.port}, {'offline' if opts.offline else 'online'}\n")
        self.run("install", lambda eng: eng.install(opts), "desktop installed and running")

    def simple(self, what):
        labels = {"start": "Start the desktop", "stop": "Shut down the desktop", "poweroff": "Power off Ubuntu",
                  "status": "Status"}
        if what == "poweroff" and QMessageBox.question(
                self, "Power off", "Stop everything in Ubuntu immediately (unsaved work is lost)?"
        ) != QMessageBox.StandardButton.Yes:
            return
        self.set_steps([(what, labels[what])] + ([("status", "Status")] if what == "start" else []))

        def fn(eng):
            if what == "start":
                eng.start()
                return eng.status()
            if what in ("stop", "poweroff"):
                eng.ui.step(what, "run")
                if eng.stop(power_off=what == "poweroff") != 0:
                    raise RuntimeError("stopping failed – see the terminal")
                eng.ui.step(what, "ok")
                return None
            return eng.status()
        self.run(what, fn, {"start": "desktop started", "stop": "desktop shut down", "poweroff": "Ubuntu powered off",
                            "status": "status read"}[what])

    def do_uninstall(self):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Uninstall Ubuntu (no root)")
        box.setText(f"Remove the no-root Ubuntu desktop from {self.api.device_name}?")
        box.setInformativeText(
            f"This stops it and permanently deletes {E.PD}: the Ubuntu system, everything in /home inside it and "
            "the RustDesk settings (about 4 GB). It can't be undone.\n\nThe phone's own files are not touched: "
            "proot only shows them inside Ubuntu, nothing is mounted.")
        box.setStandardButtons(QMessageBox.StandardButton.Cancel)
        go = box.addButton("Uninstall", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not go:
            return
        self.set_steps([("check", "Check device"), ("uninstall", "Stop and delete")])
        self.on_log("\n=== Uninstall (no root)\n")
        self.last_status = None
        self.run("uninstall", lambda eng: eng.uninstall(), "no-root Ubuntu desktop removed")

    def do_password(self):
        pw, ok = QInputDialog.getText(self, "RustDesk password",
                                      "Permanent RustDesk password for this device (at least 8 characters):",
                                      QLineEdit.EchoMode.Password)
        if not ok:
            return
        if len(pw) < 8:
            QMessageBox.warning(self, "RustDesk password", "Use at least 8 characters.")
            return
        pw2, ok = QInputDialog.getText(self, "RustDesk password", "Repeat the password:", QLineEdit.EchoMode.Password)
        if not ok:
            return
        if pw != pw2:
            QMessageBox.warning(self, "RustDesk password", "The passwords don't match.")
            return
        self.set_steps([("password", "Set RustDesk password")])

        def fn(eng):
            eng.ui.step("password", "run")
            eng.set_password(pw)
            eng.ui.step("password", "ok")
        self.run("password", fn, "RustDesk password set")

    def do_bundle(self):
        prof = self.profile.currentData()
        if QMessageBox.question(
                self, "Build offline bundle",
                f"This device downloads every package a fresh '{prof}' install needs (about 1 GB, it needs "
                f"internet), and the bundle is copied to\n{os.path.join(self.files.text(), 'offline', 'repo')}\n\n"
                "The same bundle works for the root and the no-root desktop. Continue?"
        ) != QMessageBox.StandardButton.Yes:
            return
        self.set_steps(E.BUNDLE_STEPS)
        self.on_log(f"\n=== Build offline bundle (profile {prof})\n")
        self.run("bundle", lambda eng: eng.build_bundle(prof), "offline bundle ready")

    def do_cancel(self):
        if not (self.worker and self.worker.is_alive()):
            return
        job = {"install": "install", "bundle": "bundle"}.get(self.op)
        kill = False
        if job:
            r = QMessageBox.question(
                self, "Cancel",
                f"Stop the {job} on the device too?\n\nYes – stop it.\nNo – keep it running on the device "
                f"(reopen this window to follow it again).",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Cancel:
                return
            kill = r == QMessageBox.StandardButton.Yes
        eng = self.engine
        eng.cancel = True
        if kill:
            threading.Thread(target=lambda: _safe(lambda: E.Engine(self.api, UiSink(self.bridge),
                                                                   eng.files_dir).kill_job(job)),
                             daemon=True).start()
            self.on_log(f"\n[stopping the {job} job on the device]\n")

    def closeEvent(self, e):
        if self.worker and self.worker.is_alive() and self.engine:
            self.engine.cancel = True
            if self.op in ("install", "bundle"):
                self.api.message(f"{TITLE}: the job keeps running on the device – reopen the window to follow it")
        _windows.pop("main", None)
        super().closeEvent(e)


def _safe(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001
        pass
