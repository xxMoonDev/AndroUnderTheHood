"""Root workbench - boot images, Magisk module development and SELinux denials (AndroUnderTheHood plugin).

Boot images: list the boot chain per slot, inspect (magiskboot), save to the PC, patch with Magisk's own
             boot_patch.sh (in a temp folder - no partition is written), compare slots, and flash an image
             through the app's partitions code (automatic backup, verify, read-back) after typed confirmation.
Modules:     module projects in Documents\\AndroUnderTheHood\\ModuleDev - create, edit, pack to a flashable zip,
             test-run a script as root without installing, install, Magisk log.
SELinux:     avc denials grouped into magiskpolicy rules - copy, add to a module's sepolicy.rule, or apply live.
"""
import os
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QTextCursor
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from . import boot as BT
from . import modules as MD
from . import selinux as SE

_win = {}


def register(api):
    api.add_tool("Root workbench", lambda: open_window(api), icon="root",
                 tooltip="Boot images (inspect, save, Magisk-patch, flash), Magisk module development, "
                         "SELinux denials (plugin)", needs="root")


def unload(api):
    w = _win.pop("w", None)
    if w:
        try:
            w.close()
        except RuntimeError:
            pass


def open_window(api):
    w = _win.get("w")
    try:
        if w is not None and w.isVisible():
            w.raise_()
            w.activateWindow()
            return
    except RuntimeError:
        pass
    w = _win["w"] = Workbench(api)
    w.show()


class Bridge(QObject):
    log = Signal(str)
    progress = Signal(int, int, str)        # done, total (0 = busy), label
    done = Signal(str, object)
    fail = Signal(str, str)
    installed = Signal(object)


def mono():
    f = QFont("Consolas")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSize(9)
    return f


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


class Workbench(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"Root workbench – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1100, 820)
        self.b = Bridge()
        self.b.log.connect(self.on_log)
        self.b.progress.connect(self.on_progress)
        self.b.done.connect(self.on_done)
        self.b.fail.connect(self.on_fail)
        self.b.installed.connect(self.done_installed)
        self.busy = False
        self.queue = []
        self.target = "boot"
        self.parts, self.slot, self.infos = [], "", {}
        self.groups = []

        root = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Vertical)
        root.addWidget(split, 1)
        self.tabs = QTabWidget()
        split.addWidget(self.tabs)
        self.tabs.addTab(self.build_boot(), "Boot images")
        self.tabs.addTab(self.build_modules(), "Modules")
        self.tabs.addTab(self.build_selinux(), "SELinux")

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.status = QLabel("")
        row.addWidget(self.status, 1)
        self.bar = QProgressBar()
        self.bar.setMaximumWidth(320)
        self.bar.setVisible(False)
        row.addWidget(self.bar)
        bl.addLayout(row)
        self.logv = QPlainTextEdit()
        self.logv.setReadOnly(True)
        self.logv.setFont(mono())
        self.logv.setMaximumBlockCount(20000)
        self.logv.setStyleSheet("QPlainTextEdit { background: #0c1117; color: #d5dee8; border: 1px solid #222b36; }")
        bl.addWidget(self.logv, 1)
        split.addWidget(bottom)
        split.setSizes([560, 240])
        self.refresh_boot()
        self.refresh_projects()

    # ---- worker plumbing ------------------------------------------------------------------------------
    def work(self, op, fn, label=""):
        if self.busy:                       # one device job at a time: queue it, never drop it
            self.queue.append((op, fn, label))
            return
        if not self.api.connected():
            QMessageBox.warning(self, "Root workbench", "Connect to a device first.")
            return
        self.busy = True
        self.status.setText(label)
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)

        def body():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                self.api.log.exception("root workbench %s", op)
                self.b.fail.emit(op, str(e) or e.__class__.__name__)
            else:
                self.b.done.emit(op, res)
        threading.Thread(target=body, daemon=True, name=f"rwb-{op}").start()

    def log(self, text):
        self.b.log.emit(text)

    def prog(self, total, label):
        done = [0]

        def cb(n, *a):
            done[0] += n
            self.b.progress.emit(done[0], total, (a[0] if a and isinstance(a[0], str) else label))
        return cb

    def on_log(self, text):
        cur = self.logv.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertText(text.replace("\r\n", "\n"))
        self.logv.verticalScrollBar().setValue(self.logv.verticalScrollBar().maximum())

    def on_progress(self, done, total, label):
        if label:
            self.status.setText(label)
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(min(done, total) * 1000 / total))
        else:
            self.bar.setRange(0, 0)

    def finish(self):
        self.busy = False
        self.bar.setVisible(False)
        if self.queue:
            QTimer.singleShot(0, lambda: self.queue and not self.busy and self.work(*self.queue.pop(0)))

    def on_fail(self, op, msg):
        self.finish()
        self.status.setText(f"{op} failed")
        self.on_log(f"*** {op}: {msg}\n")
        QMessageBox.warning(self, "Root workbench", msg)

    def on_done(self, op, res):
        self.finish()
        handler = getattr(self, "done_" + op, None)
        if handler:
            handler(res)
        else:
            self.status.setText(f"{op}: done")

    @property
    def conn(self):
        return self.api.conn

    # =========================================================================================================
    # Boot images
    # =========================================================================================================
    def build_boot(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.boot_head = QLabel("Reading partitions…")
        self.boot_head.setWordWrap(True)
        lay.addWidget(self.boot_head)
        self.btab = QTableWidget(0, 5)
        self.btab.setHorizontalHeaderLabels(["Partition", "Slot", "Size", "Contents", "SHA-256"])
        self.btab.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.btab.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.btab.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.btab.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.btab.verticalHeader().setVisible(False)
        self.btab.doubleClicked.connect(lambda *_: self.do_inspect())
        lay.addWidget(self.btab, 1)
        row = QHBoxLayout()
        for text, fn, tip in (
                ("Refresh", self.refresh_boot, ""),
                ("Inspect", self.do_inspect, "Read the partition and show what's in it (magiskboot unpack)"),
                ("Inspect active slot", self.do_inspect_all, "Inspect every boot-chain partition of the active slot"),
                ("Compare slots", self.do_compare, "SHA-256 of slot A vs B for every boot-chain partition"),
                ("Save to PC", self.do_save, "Copy the partition to Documents\\AndroUnderTheHood\\Boot images "
                                             "(checksum-verified)"),
                ("Patch with Magisk", self.do_patch, "Run Magisk's own boot_patch.sh on a COPY of the partition and "
                                                     "save the patched image on the PC. Nothing is flashed."),
                ("Flash image…", self.do_flash, "Write an image file to the selected partition "
                                               "(backup first, verified, typed confirmation)"),
                ("Open folder", lambda: os.startfile(BT.pc_dir(self.api.device_name)), "")):
            b = QPushButton(text)
            if tip:
                b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        return w

    def refresh_boot(self):
        def fn():
            parts, slot = BT.boot_parts(self.conn)
            target = BT.magisk_target(self.conn, parts, slot)
            return parts, slot, target
        self.work("boot_list", fn, "Reading boot partitions…")

    def done_boot_list(self, res):
        self.parts, self.slot, self.target = res
        self.status.setText(f"{len(self.parts)} boot-chain partitions")
        self.boot_head.setText(
            f"Active slot: <b>{self.slot.upper() or 'none (A-only device)'}</b> · Magisk patches "
            f"<b>{self.target}</b> on this device · Images are saved in "
            f"{BT.pc_dir(self.api.device_name)}")
        self.fill_boot()

    def fill_boot(self):
        self.btab.setRowCount(len(self.parts))
        for r, p in enumerate(self.parts):
            info = self.infos.get(p.name, {})
            active = p.slot and p.slot == self.slot
            cells = [p.name, (p.slot.upper() + ("  (active)" if active else "")) if p.slot else "–", human(p.size),
                     info.get("kind", "") + (f" · {info['kernel']}" if info.get("kernel") else "")
                     + (f" · patch level {info['header'].get('OS_PATCH_LEVEL')}"
                        if info.get("header", {}).get("OS_PATCH_LEVEL") else ""),
                     info.get("sha256", "")[:16]]
            for c, v in enumerate(cells):
                it = QTableWidgetItem(v)
                if active:
                    it.setForeground(QColor(self.api.theme.ACCENT))
                self.btab.setItem(r, c, it)
        self.btab.resizeColumnsToContents()
        self.btab.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

    def selected_part(self):
        r = self.btab.currentRow()
        if r < 0 or r >= len(self.parts):
            QMessageBox.information(self, "Root workbench", "Select a partition first.")
            return None
        return self.parts[r]

    def do_inspect(self):
        p = self.selected_part()
        if p:
            self.work("inspect", lambda: [BT.inspect(self.conn, p)], f"Inspecting {p.name}…")

    def do_inspect_all(self):
        targets = [p for p in self.parts if p.slot in (self.slot, "")]

        def fn():
            out = []
            for i, p in enumerate(targets):
                self.b.progress.emit(i, len(targets), f"Inspecting {p.name}…")
                out.append(BT.inspect(self.conn, p))
            return out
        self.work("inspect", fn, "Inspecting…")

    def done_inspect(self, infos):
        for i in infos:
            self.infos[i["partition"]] = i
            self.on_log(f"\n== {i['partition']}: {i['kind']}\n   sha256 {i.get('sha256', '')}\n")
            for k in ("HEADER_VER", "KERNEL_SZ", "RAMDISK_SZ", "OS_VERSION", "OS_PATCH_LEVEL", "KERNEL_FMT",
                      "RAMDISK_FMT", "CMDLINE"):
                if k in i.get("header", {}):
                    self.on_log(f"   {k:15} {i['header'][k]}\n")
            for k in ("kernel", "ramdisk"):
                if i.get(k):
                    self.on_log(f"   {k:15} {i[k]}\n")
            if i.get("magisk_backup"):
                self.on_log("   Magisk keeps the original ramdisk in .backup (restorable)\n")
        self.fill_boot()
        self.status.setText("Inspected")

    def do_compare(self):
        self.work("compare", lambda: BT.compare_slots(self.conn, self.parts), "Hashing both slots…")

    def done_compare(self, rows):
        self.on_log("\n== Slot A vs B\n")
        for base, a, b, same in rows:
            self.on_log(f"   {base:16} {'same     ' if same else 'DIFFERENT'}  A {a[:16]}  B {b[:16]}\n")
        self.status.setText("Slots compared")

    def do_save(self):
        p = self.selected_part()
        if p:
            self.work("save", lambda: BT.save_to_pc(self.conn, p, self.api.device_name,
                                                    self.prog(p.size, f"Saving {p.name}…")), f"Saving {p.name}…")

    def done_save(self, path):
        self.on_log(f"\nsaved {path}\n")
        self.status.setText(f"Saved {os.path.basename(path)}")

    def do_patch(self):
        p = self.selected_part()
        if not p:
            return
        if p.base != self.target and QMessageBox.question(
                self, "Patch with Magisk",
                f"On this device Magisk patches {self.target}, not {p.base}. Patch {p.name} anyway?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.work("patch", lambda: BT.patch(self.conn, self.conn.new_fs(), p, self.api.device_name, self.log,
                                            self.prog(p.size, "Downloading the patched image…")),
                  f"Patching a copy of {p.name} with Magisk…")

    def done_patch(self, path):
        self.on_log(f"\npatched image saved: {path}\nNothing was flashed. Use 'Flash image…' to write it.\n")
        self.status.setText(f"Patched image: {os.path.basename(path)}")

    def do_flash(self):
        from androunderthehood import partitions as P
        p = self.selected_part()
        if not p:
            return
        tier, why = P.flash_tier(p)
        if tier == "forbidden":
            QMessageBox.warning(self, "Flash", why)
            return
        img, _ = QFileDialog.getOpenFileName(self, f"Image to write to {p.name}", BT.pc_dir(self.api.device_name),
                                             "Images (*.img *.bin);;All files (*)")
        if not img:
            return
        if p.slot and p.slot == self.slot and QMessageBox.warning(
                self, "Active slot",
                f"{p.name} is in the ACTIVE slot – the phone boots from it next time.\n\nSafer: flash the "
                f"inactive slot ({'B' if self.slot == 'a' else 'A'}) and switch slots only once you are sure.\n\n"
                "Continue with the active slot?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        kind = P.image_kind(img)
        size = os.path.getsize(img)
        dlg = P.FlashConfirm(self, p, img, kind, size, tier, why, self.slot)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.work("flash", lambda: P.flash_partition(self.conn, self.conn.new_fs(), p, img, self.api.device_name,
                                                     self.prog(p.size, f"Flashing {p.name}…")),
                  f"Flashing {p.name}…")

    def done_flash(self, report):
        self.on_log("\n" + report + "\n")
        QMessageBox.information(self, "Flash", report)
        self.infos.clear()
        self.refresh_boot()

    # =========================================================================================================
    # Modules
    # =========================================================================================================
    def build_modules(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()
        left.addWidget(QLabel("Module projects"))
        self.plist = QListWidget()
        self.plist.currentRowChanged.connect(self.load_project)
        left.addWidget(self.plist, 1)
        r1 = QHBoxLayout()
        for text, fn in (("New…", self.new_project), ("Refresh", self.refresh_projects),
                         ("Folder", lambda: os.startfile(self.cur_project() or MD.project_root()))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            r1.addWidget(b)
        left.addLayout(r1)
        self.installed_lbl = QLabel("")
        self.installed_lbl.setWordWrap(True)
        self.installed_lbl.setStyleSheet(f"color: {self.api.theme.TEXT_DIM};")
        left.addWidget(self.installed_lbl)
        lay.addLayout(left, 1)

        right = QVBoxLayout()
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("File"))
        self.fcombo = QComboBox()
        self.fcombo.setEditable(True)
        self.fcombo.currentTextChanged.connect(self.load_file)
        r2.addWidget(self.fcombo, 1)
        sb = QPushButton("Save")
        sb.clicked.connect(self.save_file)
        r2.addWidget(sb)
        right.addLayout(r2)
        self.editor = QPlainTextEdit()
        self.editor.setFont(mono())
        right.addWidget(self.editor, 1)
        r3 = QHBoxLayout()
        for text, fn, tip in (
                ("Test-run this script", self.test_run, "Runs the script NOW as root from a temporary copy, the way "
                                                        "Magisk would (MODPATH set, busybox ash). Nothing is installed."),
                ("Pack zip", self.pack, "Builds a flashable <id>-<version>.zip next to the project"),
                ("Install on device…", self.install, "Pack and install with Magisk (active after a reboot)"),
                ("Magisk log", lambda: self.work("mlog", lambda: MD.magisk_log(self.conn), "Reading the Magisk log…"),
                 "Magisk's own log (logcat tag Magisk). Your scripts can log with: log -t <id> \"text\"")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            r3.addWidget(b)
        r3.addStretch(1)
        right.addLayout(r3)
        lay.addLayout(right, 3)
        return w

    def refresh_projects(self):
        cur = self.cur_project()
        self.projects = MD.list_projects()
        self.plist.blockSignals(True)
        self.plist.clear()
        for d, prop in self.projects:
            self.plist.addItem(QListWidgetItem(f"{prop.get('name', os.path.basename(d))}  ({prop.get('version', '')})"))
        self.plist.blockSignals(False)
        idx = next((i for i, (d, _) in enumerate(self.projects) if d == cur), 0 if self.projects else -1)
        self.plist.setCurrentRow(idx)
        if idx >= 0:
            self.load_project(idx)
        if self.api.connected():
            threading.Thread(target=lambda: self.b.installed.emit(MD.installed(self.conn)), daemon=True).start()

    def done_installed(self, mods):
        names = ", ".join(m.id for m in mods) or "none"
        self.installed_lbl.setText(f"Installed on the device: {names}\n(enable/disable/remove them in Root manager)")

    def cur_project(self):
        r = self.plist.currentRow()
        return self.projects[r][0] if getattr(self, "projects", None) and 0 <= r < len(self.projects) else None

    def load_project(self, _row):
        d = self.cur_project()
        self.fcombo.blockSignals(True)
        self.fcombo.clear()
        if d:
            present = [s for s in MD.SCRIPTS if os.path.isfile(os.path.join(d, s))]
            self.fcombo.addItems(present + [s for s in MD.SCRIPTS if s not in present])
        self.fcombo.blockSignals(False)
        self.load_file(self.fcombo.currentText())

    def load_file(self, name):
        d = self.cur_project()
        path = os.path.join(d, name) if d and name else None
        text = ""
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        self.editor.setPlainText(text)
        self.editor.setPlaceholderText(f"{name} doesn't exist yet – type and press Save to create it" if name else "")

    def save_file(self):
        d, name = self.cur_project(), self.fcombo.currentText().strip()
        if not d or not name or "/" in name or "\\" in name or name.startswith("."):
            return
        with open(os.path.join(d, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(self.editor.toPlainText())
        self.status.setText(f"Saved {name}")

    def new_project(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("New Magisk module")
        fl = QFormLayout(dlg)
        fields = {k: QLineEdit(v) for k, v in (("id", "my_module"), ("name", "My module"), ("version", "v1.0"),
                                                ("author", ""), ("description", ""))}
        for k, e in fields.items():
            fl.addRow(k.capitalize(), e)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        fl.addRow(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            d = MD.create(*(fields[k].text().strip() for k in ("id", "name", "version", "author", "description")))
        except ValueError as e:
            QMessageBox.warning(self, "New module", str(e))
            return
        self.on_log(f"created module project {d}\n")
        self.refresh_projects()
        self.plist.setCurrentRow(next(i for i, (x, _) in enumerate(self.projects) if x == d))

    def test_run(self):
        d, name = self.cur_project(), self.fcombo.currentText().strip()
        if not d or not name.endswith(".sh"):
            QMessageBox.information(self, "Test-run", "Pick a .sh script of a module project.")
            return
        self.save_file()
        self.on_log(f"\n=== test-run {name} as root (temporary copy, not installed)\n")
        self.work("testrun", lambda: MD.test_run(self.conn, self.conn.new_fs(), d, name,
                                                 lambda b: self.log(b.decode("utf-8", "replace"))),
                  f"Running {name}…")

    def pack(self):
        d = self.cur_project()
        if d:
            self.save_file()
            z = MD.pack(d)
            self.on_log(f"packed {z}\n")
            self.status.setText(f"Packed {os.path.basename(z)}")

    def install(self):
        d = self.cur_project()
        if not d:
            return
        prop = MD.read_prop(d)
        if QMessageBox.question(self, "Install module",
                                f"Install '{prop.get('name')}' ({prop.get('id')} {prop.get('version')}) with Magisk on "
                                f"{self.api.device_name}?\n\nIt becomes active after the next reboot. Remove it again "
                                "in Root manager.") != QMessageBox.StandardButton.Yes:
            return
        self.save_file()
        z = MD.pack(d)
        self.on_log(f"\n=== installing {os.path.basename(z)}\n")
        self.work("minstall", lambda: MD.install(self.conn, self.conn.new_fs(), z,
                                                 lambda b: self.log(b.decode("utf-8", "replace"))),
                  "Installing the module…")

    def done_minstall(self, rc):
        self.status.setText("Module installed – reboot to activate" if rc == 0 else "Install failed – see the log")
        self.refresh_projects()

    def done_mlog(self, text):
        self.on_log("\n=== Magisk log (logcat -s Magisk)\n" + (text or "(empty)\n"))
        self.status.setText("Magisk log read")

    # =========================================================================================================
    # SELinux
    # =========================================================================================================
    def build_selinux(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        cb = QPushButton("Collect denials")
        cb.clicked.connect(self.collect)
        row.addWidget(cb)
        self.auto = QCheckBox("Refresh every 5 s")
        self.auto.toggled.connect(lambda on: self.timer.start(5000) if on else self.timer.stop())
        row.addWidget(self.auto)
        self.filt = QLineEdit()
        self.filt.setPlaceholderText("Filter (type, class, permission, process)…")
        self.filt.textChanged.connect(self.fill_se)
        row.addWidget(self.filt, 1)
        lay.addLayout(row)
        self.stab = QTableWidget(0, 6)
        self.stab.setHorizontalHeaderLabels(["Count", "Source", "Target", "Class", "Processes", "Rule"])
        self.stab.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.stab.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.stab.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stab.verticalHeader().setVisible(False)
        self.stab.itemSelectionChanged.connect(self.show_example)
        lay.addWidget(self.stab, 1)
        note = QLabel("Red rows allow something for an app or core domain – that weakens Android's sandbox. "
                      "Prefer fixing the cause; only allow what you understand.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {self.api.theme.TEXT_DIM};")
        lay.addWidget(note)
        r2 = QHBoxLayout()
        for text, fn, tip in (("Copy rules", self.copy_rules, "Copy the selected rules (magiskpolicy syntax)"),
                              ("Add to module…", self.rules_to_module, "Append to a module project's sepolicy.rule "
                                                                      "(applied at every boot once installed)"),
                              ("Apply live…", self.apply_live, "magiskpolicy --live: changes the running policy "
                                                              "until the next reboot")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            r2.addWidget(b)
        r2.addStretch(1)
        lay.addLayout(r2)
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: None if self.busy else self.collect(quiet=True))
        return w

    def collect(self, quiet=False):
        self.work("se", lambda: SE.collect(self.conn), "" if quiet else "Collecting SELinux denials…")

    def done_se(self, groups):
        self.groups = groups
        self.fill_se()
        self.status.setText(f"{len(groups)} rule groups from {sum(g.count for g in groups)} denials")

    def visible_groups(self):
        f = self.filt.text().strip().lower()
        if not f:
            return self.groups
        return [g for g in self.groups if f in (g.rule + " " + " ".join(g.comms)).lower()]

    def fill_se(self):
        gs = self.visible_groups()
        self.stab.setRowCount(len(gs))
        for r, g in enumerate(gs):
            cells = [str(g.count), g.stype, g.ttype, g.tclass, ", ".join(sorted(g.comms))[:60], g.rule]
            for c, v in enumerate(cells):
                it = QTableWidgetItem(v)
                if g.risky:
                    it.setForeground(QColor(self.api.theme.DANGER))
                self.stab.setItem(r, c, it)
        self.stab.resizeColumnsToContents()
        self.stab.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)

    def selected_groups(self):
        gs = self.visible_groups()
        rows = sorted({i.row() for i in self.stab.selectedIndexes()})
        return [gs[r] for r in rows if r < len(gs)]

    def show_example(self):
        sel = self.selected_groups()
        if len(sel) == 1:
            g = sel[0]
            self.status.setText(f"{g.count}× · files/paths: {', '.join(sorted(g.names))[:120] or '–'}")

    def copy_rules(self):
        rules = [g.rule for g in self.selected_groups()]
        if rules:
            QApplication.clipboard().setText("\n".join(rules))
            self.status.setText(f"Copied {len(rules)} rule(s)")

    def rules_to_module(self):
        rules = [g.rule for g in self.selected_groups()]
        if not rules:
            return
        names = [f"{p.get('name')} ({os.path.basename(d)})" for d, p in getattr(self, "projects", [])]
        if not names:
            QMessageBox.information(self, "Add to module", "Create a module project first (Modules tab → New…).")
            return
        name, ok = QInputDialog.getItem(self, "Add to module", "Module project:", names, 0, False)
        if not ok:
            return
        d = self.projects[names.index(name)][0]
        path = SE.append_to_module(d, rules)
        self.on_log(f"added {len(rules)} rule(s) to {path}\n")
        self.refresh_projects()

    def apply_live(self):
        sel = self.selected_groups()
        if not sel:
            return
        risky = [g for g in sel if g.risky]
        msg = ("Apply these rules to the RUNNING SELinux policy (until the next reboot)?\n\n"
               + "\n".join(g.rule for g in sel))
        if risky:
            msg += ("\n\n⚠ Some rules give extra rights to app or core domains (red). Any app in that domain gets "
                    "them, not just the one that was denied.")
        if QMessageBox.question(self, "Apply live", msg) != QMessageBox.StandardButton.Yes:
            return
        self.work("seapply", lambda: SE.apply_live(self.conn, [g.rule for g in sel]), "Applying rules…")

    def done_seapply(self, text):
        self.on_log("\n=== magiskpolicy --live\n" + text)
        self.status.setText("Rules applied until reboot")

    def closeEvent(self, e):
        self.timer.stop()
        _win.pop("w", None)
        super().closeEvent(e)
