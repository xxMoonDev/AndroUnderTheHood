"""PhoneDesktop – Ubuntu 24.04 + KDE Plasma + RustDesk on a rooted phone (AndroUnderTheHood plugin).

Window: setup options, a step checklist with progress bars, and a live terminal with the device's
output. The install runs on the device as a detached job, so closing this window or losing the
connection does not stop it – reopen the window to follow it again.
"""
import os
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextCursor
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QVBoxLayout, QWidget)

from . import engine as E

_windows = {}


def register(api):
    api.add_tool("Phone desktop", lambda: open_window(api), icon="power",
                 tooltip="Ubuntu + KDE Plasma desktop on the phone, reachable with RustDesk (plugin)",
                 needs="connection")   # root: chroot; no root: proot (shell user)


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
    w = PhoneDesktopWindow(api)
    _windows["main"] = w
    w.show()


def default_files_dir(api):
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


class PhoneDesktopWindow(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        t = api.theme
        self.setWindowTitle(f"Phone desktop – {api.device_name}")
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
        for key, label in (("direct", "Direct IP (port 21118, no ID server)"),
                           ("public", "Public RustDesk servers (connect by ID)"),
                           ("both", "Public servers + direct IP"),
                           ("custom", "My own RustDesk server")):
            self.mode.addItem(label, key)
        self.mode.setCurrentIndex(max(0, self.mode.findData(api.get_setting("mode", "direct"))))
        fl.addRow("RustDesk", self.mode)
        self.server = QLineEdit(api.get_setting("server", ""))
        self.server.setPlaceholderText("hbbs host")
        self.key = QLineEdit(api.get_setting("key", ""))
        self.key.setPlaceholderText("public key")
        srow = QHBoxLayout()
        srow.addWidget(self.server, 2)
        srow.addWidget(self.key, 3)
        fl.addRow("Own server", srow)
        self.mode.currentIndexChanged.connect(self.update_mode)
        self.autostart = QCheckBox("Start the desktop at every boot (Magisk service.d)")
        fl.addRow("", self.autostart)
        self.with_app = QCheckBox("Install the home-screen widget app (Start / Shut down / Power off)")
        self.with_app.setChecked(bool(api.get_setting("with_app", True)))
        fl.addRow("", self.with_app)
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
        stop_menu.addAction("Shut down – Plasma logs out cleanly, stays mounted").triggered.connect(
            lambda: self.simple("stop"))
        stop_menu.addAction("Power off – stop everything now and unmount").triggered.connect(
            lambda: self.simple("poweroff"))
        self.b_stop.setMenu(stop_menu)
        self.b_app = QPushButton(api.icon("phone"), " Widget app")
        self.b_app.setToolTip("Install or update the Android app with the home-screen widget "
                              "(Start / Shut down / Power off)")
        self.b_app.clicked.connect(lambda: self.simple("app"))
        self.b_link = QPushButton(api.icon("connect"), " Phone bridge…")
        self.b_link.setToolTip("Clipboard both ways, phone notifications as Plasma popups, and Ubuntu's sound on "
                               "the phone's speaker")
        self.b_link.clicked.connect(self.do_phone_bridge)
        self.b_droid = QPushButton(api.icon("grid"), " Android apps…")
        self.b_droid.setToolTip("Open this phone's apps inside Ubuntu (scrcpy over SSH, one window per app) and "
                                "list them in the Plasma start menu")
        self.b_droid.clicked.connect(self.do_android_apps)
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
        for w in (self.b_install, self.b_start, self.b_stop, self.b_status, self.b_droid, self.b_link, self.b_pw,
                  self.b_app, self.b_bundle):
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
                  self.b_app, self.b_link, self.b_droid, self.b_uninstall, self.with_app,
                  self.profile, self.res, self.user, self.mode, self.files, self.offline, self.autostart):
            w.setEnabled(not busy)
        if not busy:
            self.update_mode()
            if not self.offline_info.text().startswith("No bundle"):
                self.offline.setEnabled(True)
            else:
                self.offline.setEnabled(False)
            self.apply_root_only()
        self.b_cancel.setEnabled(busy)

    ROOT_ONLY_TIPS = {"autostart": "Needs root (a Magisk boot hook)",
                      "b_link": "Needs root: the bridge helper enters Ubuntu with chroot"}

    def apply_root_only(self):
        """No-root (proot) mode: grey out what needs root, and say why."""
        proot = getattr(self, "proot_mode", False)
        if not hasattr(self, "_tips"):
            self._tips = {n: getattr(self, n).toolTip() for n in self.ROOT_ONLY_TIPS}
        busy = bool(self.worker and self.worker.is_alive())
        for n, tip in self.ROOT_ONLY_TIPS.items():
            w = getattr(self, n)
            w.setEnabled(not busy and not proot)
            w.setToolTip(tip if proot else self._tips[n])

    def options(self):
        o = E.Options(profile=self.profile.currentData(), res=self.res.text().strip(), user=self.user.text().strip(),
                      mode=self.mode.currentData(), server=self.server.text().strip(), key=self.key.text().strip(),
                      autostart=self.autostart.isChecked(), offline=self.offline.isChecked() and self.offline.isEnabled(),
                      app=self.with_app.isChecked())
        o.validate()
        for k, v in (("profile", o.profile), ("res", o.res), ("user", o.user), ("mode", o.mode),
                     ("server", o.server), ("key", o.key), ("offline", self.offline.isChecked()),
                     ("with_app", o.app)):
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
            self.phase_bar.setRange(0, 0)           # busy indicator
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
            self.api.message(f"Phone desktop: {msg}")
            for bar in (self.overall_bar, self.phase_bar):
                bar.setRange(0, 1000)
                bar.setValue(1000)
                bar.setFormat("100 %")
            self.phase_label.setText(msg[:1].upper() + msg[1:])
        else:
            self.on_log(f"\n*** {msg}\n")
            self.api.message(f"Phone desktop: {msg}", error=True)
        if isinstance(result, dict) and "id" in result:
            self.show_status(result)
        if isinstance(result, dict) and "aoh" in result:
            self.on_log("Android apps: " + (f"{result['apps']} apps in the start menu, SSH {result['target']}, watcher "
                                            + ("running" if result["watcher"] else "stopped")
                                            if result["setup"] else "not set up") + "\n")
        if isinstance(result, dict) and "app_version" in result:
            self.on_log("Phone bridge: " + ("running" if result["running"] else "stopped")
                        + (f"\n{result['log']}" if result.get("log") else "") + "\n")
        if self.op == "bundle":
            self.update_offline()
        if self.op != "probe":
            self.probe()

    # ---- worker plumbing -----------------------------------------------------------------------------
    def run(self, op, fn, done_msg):
        if self.worker and self.worker.is_alive():
            return
        if not self.api.connected():
            QMessageBox.warning(self, "Phone desktop", "Connect to a device first.")
            return
        self.op = op
        self.engine = E.Engine(self.api, UiSink(self.bridge), self.files.text().strip())
        eng = self.engine
        self.set_busy(op != "probe")

        def body():
            try:
                result = fn(eng)
            except E.Cancelled:
                self.bridge.finished.emit(False, "cancelled", None)
            except Exception as e:  # noqa: BLE001
                self.api.log.exception("phone desktop %s failed", op)
                self.bridge.finished.emit(False, str(e) or e.__class__.__name__, None)
            else:
                self.bridge.finished.emit(True, done_msg, result)
        self.worker = threading.Thread(target=body, daemon=True, name=f"phonedesktop-{op}")
        self.worker.start()

    def probe(self):
        """Read the device state for the header (own thread; doesn't block the buttons)."""
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
        threading.Thread(target=body, daemon=True, name="phonedesktop-probe").start()

    def show_probe(self, st):
        if "error" in st:
            self.header.setText("Could not read the device state")
            self.sub.setText(st["error"])
            return
        proot = st.get("mode") == "proot"
        self.proot_mode = proot
        if st.get("no_root_reason"):
            self.header.setText("No root, and not the shell user")
            self.sub.setText(st["no_root_reason"])
            return
        tag = "  (no root – proot)" if proot else ""
        if not st.get("installed"):
            self.header.setText("Not installed on this device" + tag)
            self.sub.setText("Press Install to set up Ubuntu 24.04 + KDE Plasma + RustDesk "
                             f"(about 3.5 GB in {st.get('folder', '/data/local/phonedesktop')})."
                             + (" Without root it runs under proot: slower, the phone bridge and start-at-boot "
                                "need root, and the widget app works through Shizuku." if proot else ""))
        else:
            state = "running" if st.get("running") else "stopped"
            self.header.setText(f"Ubuntu + KDE Plasma installed – desktop {state}{tag}")
            last = getattr(self, "last_status", None)
            if last and st.get("running"):
                self.show_status(last)
                if st.get("autostart"):
                    self.sub.setText(self.sub.text() + "  –  starts at boot")
            else:
                self.sub.setText(("RustDesk is listening on port 21118. " if st.get("rd_listen") else "")
                                 + ("Starts at boot. " if st.get("autostart") else "")
                                 + "Press Status for the RustDesk ID and addresses.")
        self.autostart.setChecked(bool(st.get("autostart")))
        # root-only parts: the widget app and the bridge work through su, autostart is a Magisk hook
        self.apply_root_only()
        self.b_app.setText(" Update widget app" if st.get("app") else " Install widget app")
        self.b_uninstall.setVisible(bool(st.get("installed")) or bool(st.get("app")))
        # a job still running on the device (window was closed, or the connection dropped)?
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
        elif name == "android":
            self.set_steps([("upload", "Upload scrcpy-ssh and scripts"), ("android", "Set up Android apps")])
            self.on_step("upload", "ok", "")

            def fn(eng):
                eng.ui.step("android", "run")
                if eng.follow("android") != 0:
                    raise RuntimeError("Setting up Android apps failed - see the terminal")
                eng.ui.step("android", "ok")
                return eng.android_state()
            self.run("android", fn, "Android apps are in the start menu")
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
        addrs = ", ".join(f"{a} ({d})" for a, d in info.get("addrs", []))
        self.sub.setText(f"RustDesk ID {info.get('id') or '?'} – connect by IP: {addrs or '?'}"
                         + ("" if info.get("listening") else "  –  RustDesk is NOT listening"))

    # ---- actions ------------------------------------------------------------------------------------------
    def do_install(self):
        try:
            opts = self.options()
        except ValueError as e:
            QMessageBox.warning(self, "Phone desktop", str(e))
            return
        self.set_steps(E.INSTALL_STEPS)
        self.on_log(f"\n=== Install / update: profile {opts.profile}, {opts.res}, user {opts.user}, "
                    f"RustDesk {opts.mode}, {'offline' if opts.offline else 'online'}\n")
        self.run("install", lambda eng: eng.install(opts), "desktop installed and running")

    def simple(self, what):
        labels = {"start": "Start the desktop", "stop": "Shut down the desktop", "poweroff": "Power off Ubuntu",
                  "status": "Status", "app": "Widget app"}
        if what == "poweroff" and QMessageBox.question(
                self, "Power off", "Stop everything in Ubuntu immediately (unsaved work is lost) and unmount it?"
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
            if what == "app":
                eng.check_device(None)
                eng.install_app(launch=True)
                return None
            return eng.status()
        self.run(what, fn, {"start": "desktop started", "stop": "desktop shut down", "poweroff": "Ubuntu powered off",
                            "status": "status read", "app": "widget app installed – allow root (Magisk) or Shizuku on the phone, "
                                                            "then add the widget"}[what])

    def do_uninstall(self):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Uninstall Ubuntu")
        box.setText(f"Remove the Ubuntu desktop from {self.api.device_name}?")
        box.setInformativeText(
            "This stops it, unmounts it and permanently deletes /data/local/phonedesktop: the Ubuntu system, "
            "everything in /home inside it, the RustDesk settings and the boot hook (about 3.5 GB). "
            "It can't be undone.\n\nYour phone's own files are not touched: the uninstaller checks that "
            "nothing is mounted any more and stops without deleting anything if something still is.")
        keep = QCheckBox("Keep the widget app installed")
        box.setCheckBox(keep)
        box.setStandardButtons(QMessageBox.StandardButton.Cancel)
        go = box.addButton("Uninstall", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not go:
            return
        self.set_steps([("check", "Check device"), ("uninstall", "Stop, unmount and delete")])
        self.on_log("\n=== Uninstall\n")
        keep_app = keep.isChecked()
        self.last_status = None
        self.run("uninstall", lambda eng: eng.uninstall(keep_app), "Ubuntu desktop removed")

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

    def do_android_apps(self):
        if not self.api.connected():
            QMessageBox.warning(self, "Phone desktop", "Connect to a device first.")
            return
        try:
            st = E.Engine(self.api, UiSink(self.bridge), self.files.text().strip()).android_state()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Android apps", f"Could not read the state: {e}")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Android apps in Ubuntu")
        lay = QVBoxLayout(dlg)
        if st["setup"]:
            head = (f"<b>Set up</b> – {st['apps']} apps in the start menu (Android), SSH {st['target']}, "
                    f"menu watcher {'running' if st['watcher'] else 'stopped'}.")
        else:
            head = "<b>Not set up yet.</b>"
        info = QLabel(head + "<br><br>Every app of this phone gets an entry in the Plasma start menu under "
                      "<b>Android</b>. Clicking one opens that app in its own window inside Ubuntu: scrcpy-ssh "
                      "reaches the phone over SSH through <b>AndroOpenHeart</b> (127.0.0.1), on a virtual display "
                      "of its own, so several apps can be open at once. The menu follows apps being installed "
                      "or removed (checked every minute).<br><br>"
                      "<b>Set up</b> installs scrcpy-ssh in Ubuntu, makes an SSH key for the desktop user and "
                      "authorizes it in AndroOpenHeart (needs version 1.4 – this phone has "
                      f"{st['aoh_version'] or 'none'}). AndroOpenHeart's SSH server restarts once to load a new "
                      "key: SSH sessions to the phone drop for a moment (ADB doesn't).")
        info.setWordWrap(True)
        info.setMinimumWidth(560)
        lay.addWidget(info)
        row = QHBoxLayout()
        row.addStretch(1)
        b_setup = QPushButton(" Set up / repair" if st["setup"] else " Set up")
        b_sync = QPushButton(" Refresh the menu now")
        b_remove = QPushButton(" Remove")
        b_close = QPushButton(" Close")
        b_sync.setEnabled(st["setup"])
        b_remove.setEnabled(st["setup"])
        b_setup.setEnabled(st["aoh"])
        for b in (b_setup, b_sync, b_remove, b_close):
            row.addWidget(b)
        lay.addLayout(row)
        choice = {}
        b_setup.clicked.connect(lambda: (choice.update(v="setup"), dlg.accept()))
        b_sync.clicked.connect(lambda: (choice.update(v="sync"), dlg.accept()))
        b_remove.clicked.connect(lambda: (choice.update(v="remove"), dlg.accept()))
        b_close.clicked.connect(dlg.reject)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        what = choice["v"]
        if what == "setup":
            self.set_steps([("check", "Check device"), ("upload", "Upload scrcpy-ssh and scripts"),
                            ("android", "Set up Android apps")])
            self.on_log("\n=== Android apps: set up\n")
            self.run("android", lambda eng: eng.android_setup(), "Android apps are in the start menu")
        else:
            self.set_steps([("android", "Android apps: " + ("refresh" if what == "sync" else "remove"))])
            self.on_log(f"\n=== Android apps: {what}\n")
            self.run("android", lambda eng: eng.android_cmd(what),
                     "Android menu refreshed" if what == "sync" else "Android apps removed")

    def do_phone_bridge(self):
        if not self.api.connected():
            QMessageBox.warning(self, "Phone desktop", "Connect to a device first.")
            return
        try:
            st = E.Engine(self.api, UiSink(self.bridge), self.files.text().strip()).bridge_state()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Phone bridge", f"Could not read the bridge state: {e}")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Phone ↔ Ubuntu bridge")
        lay = QVBoxLayout(dlg)
        info = QLabel(f"<b>Bridge {'running' if st['running'] else 'stopped'}</b><br>"
                      "A small helper from the widget app runs as root on the phone while the desktop runs.")
        info.setWordWrap(True)
        lay.addWidget(info)
        boxes = {}
        for key, text in (("clipboard", "Shared clipboard – copy on the phone, paste in Ubuntu and back (text)"),
                          ("notifications", "Phone notifications appear as Plasma popups"),
                          ("audio", "Ubuntu's sound plays on the phone's speaker (keeps the audio chip awake "
                                    "while the bridge runs – uses some battery)"),
                          ("autostart", "Start the bridge together with the desktop")):
            cb = QCheckBox(text)
            cb.setChecked(bool(st.get(key)))
            lay.addWidget(cb)
            boxes[key] = cb
        if st.get("log"):
            lg = QLabel(st["log"])
            lg.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
            lg.setWordWrap(True)
            lay.addWidget(lg)
        row = QHBoxLayout()
        row.addStretch(1)
        b_run = QPushButton(" Save and restart" if st["running"] else " Save and start")
        b_stop = QPushButton(" Save and stop" if st["running"] else " Save")
        b_close = QPushButton(" Close")
        for b in (b_run, b_stop, b_close):
            row.addWidget(b)
        lay.addLayout(row)
        choice = {}
        b_run.clicked.connect(lambda: (choice.update(run=True), dlg.accept()))
        b_stop.clicked.connect(lambda: (choice.update(run=False), dlg.accept()))
        b_close.clicked.connect(dlg.reject)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        settings = {k: cb.isChecked() for k, cb in boxes.items()}
        run = choice["run"]
        self.set_steps([("bridge", "Phone bridge: " + ("start" if run else "save / stop"))])
        self.on_log("\n=== Phone bridge: " + ", ".join(k for k, v in settings.items() if v) + "\n")
        self.run("bridge", lambda eng: eng.bridge_apply(settings, run),
                 "phone bridge started" if run else "phone bridge settings saved")

    def do_bundle(self):
        prof = self.profile.currentData()
        if QMessageBox.question(
                self, "Build offline bundle",
                f"This device downloads every package a fresh '{prof}' install needs (about 1 GB, it needs "
                f"internet), and the bundle is copied to\n{os.path.join(self.files.text(), 'offline', 'repo')}\n\n"
                "Afterwards, installs can run without internet. Continue?") != QMessageBox.StandardButton.Yes:
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
            self.engine.cancel = True        # stop following; device jobs keep running
            if self.op in ("install", "bundle"):
                self.api.message("Phone desktop: the job keeps running on the device – reopen the window to follow it")
        _windows.pop("main", None)
        super().closeEvent(e)


def _safe(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001
        pass
