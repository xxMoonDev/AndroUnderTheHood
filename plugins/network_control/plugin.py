"""Network control - per-app firewall, port forwards & tunnels, packet capture (AndroUnderTheHood plugin)."""
import os
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton,
                               QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from . import capture as CP
from . import firewall as FW
from . import tunnels as TN

_win = {}


def register(api):
    api.add_tool("Network control", lambda: open_window(api), icon="lock",
                 tooltip="Per-app firewall, port forwards & tunnels, packet capture (plugin)", needs="root")


def unload(api):
    w = _win.pop("w", None)
    if w:
        try:
            w.close()
        except RuntimeError:
            pass
    TN.stop_all_relays()


def open_window(api):
    w = _win.get("w")
    try:
        if w is not None and w.isVisible():
            w.raise_()
            w.activateWindow()
            return
    except RuntimeError:
        pass
    w = _win["w"] = NetWindow(api)
    w.show()


class Bridge(QObject):
    log = Signal(str)
    done = Signal(str, object)
    fail = Signal(str, str)


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


class NetWindow(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"Network control – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1050, 760)
        self.b = Bridge()
        self.b.log.connect(self.on_log)
        self.b.done.connect(self.on_done)
        self.b.fail.connect(self.on_fail)
        self.busy = False
        self.queue = []
        self.tool = ("", "")
        self.apps, self.blocked, self.kind, self.vpn = [], set(), "", ""
        self.cap = None

        root = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Vertical)
        root.addWidget(split, 1)
        self.tabs = QTabWidget()
        split.addWidget(self.tabs)
        self.tabs.addTab(self.build_firewall(), "Firewall")
        self.tabs.addTab(self.build_forwards(), "Forwards && tunnels")
        self.tabs.addTab(self.build_capture(), "Capture")
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel("")
        bl.addWidget(self.status)
        self.logv = QPlainTextEdit()
        self.logv.setReadOnly(True)
        f = QFont("Consolas")
        f.setPointSize(9)
        self.logv.setFont(f)
        self.logv.setMaximumBlockCount(5000)
        self.logv.setStyleSheet("QPlainTextEdit { background: #0c1117; color: #d5dee8; border: 1px solid #222b36; }")
        bl.addWidget(self.logv, 1)
        split.addWidget(bottom)
        split.setSizes([560, 160])
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.load_apps()
        self.refresh_forwards()
        self.detect_capture()

    # ---- plumbing --------------------------------------------------------------------------------------
    @property
    def conn(self):
        return self.api.conn

    def work(self, op, fn, label=""):
        if self.busy:                       # one device job at a time: queue the rest, never drop them
            self.queue.append((op, fn, label))
            return
        if not self.api.connected():
            QMessageBox.warning(self, "Network control", "Connect to a device first.")
            return
        self.busy = True
        self.status.setText(label)

        def body():
            try:
                r = fn()
            except Exception as e:  # noqa: BLE001
                self.api.log.exception("network control %s", op)
                self.b.fail.emit(op, str(e) or e.__class__.__name__)
            else:
                self.b.done.emit(op, r)
        threading.Thread(target=body, daemon=True).start()

    def on_log(self, t):
        c = self.logv.textCursor()
        c.movePosition(QTextCursor.MoveOperation.End)
        c.insertText(t)
        self.logv.verticalScrollBar().setValue(self.logv.verticalScrollBar().maximum())

    def next_job(self):
        if self.queue and not self.busy:
            self.work(*self.queue.pop(0))

    def on_fail(self, op, msg):
        QTimer.singleShot(0, self.next_job)
        self.busy = False
        self.status.setText(f"{op} failed")
        self.on_log(f"*** {op}: {msg}\n")
        QMessageBox.warning(self, "Network control", msg)

    def on_done(self, op, r):
        if op != "capend":                  # the capture's end is not a queued job
            self.busy = False
        getattr(self, "done_" + op, lambda r: self.status.setText(f"{op}: done"))(r)
        self.next_job()

    # =======================================================================================================
    # Firewall
    # =======================================================================================================
    def build_firewall(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.fw_head = QLabel("Loading apps…")
        self.fw_head.setWordWrap(True)
        lay.addWidget(self.fw_head)
        row = QHBoxLayout()
        self.fw_filter = QLineEdit()
        self.fw_filter.setPlaceholderText("Search apps…")
        self.fw_filter.textChanged.connect(self.fill_apps)
        row.addWidget(self.fw_filter, 1)
        self.show_sys = QCheckBox("Show system apps")
        self.show_sys.toggled.connect(self.fill_apps)
        row.addWidget(self.show_sys)
        self.only_blocked = QCheckBox("Only blocked")
        self.only_blocked.toggled.connect(self.fill_apps)
        row.addWidget(self.only_blocked)
        lay.addLayout(row)
        self.atab = QTableWidget(0, 5)
        self.atab.setHorizontalHeaderLabels(["Internet", "App", "Package", "UID", "Type"])
        self.atab.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.atab.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.atab.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.atab.verticalHeader().setVisible(False)
        self.atab.doubleClicked.connect(lambda *_: self.toggle_block())
        lay.addWidget(self.atab, 1)
        row2 = QHBoxLayout()
        for text, fn, tip in (("Block / unblock", self.toggle_block, "Toggle the selected app's network access"),
                              ("Test", self.test_app, "Try a TCP connection as the selected app's uid"),
                              ("Refresh", self.load_apps, "")):
            bt = QPushButton(text)
            bt.setToolTip(tip)
            bt.clicked.connect(fn)
            row2.addWidget(bt)
        self.keep = QCheckBox("Keep blocks after reboot (Magisk boot script)")
        self.keep.toggled.connect(self.set_keep)
        row2.addWidget(self.keep)
        row2.addStretch(1)
        lay.addLayout(row2)
        return w

    def load_apps(self):
        def fn():
            from androunderthehood import apps as A
            apps = A.load_apps(self.conn, self.conn.new_fs())
            # load_apps reads userId= from dumpsys, which newer Android prints as appId= -> take uids from pm
            rc, out, _ = self.conn.exec("pm list packages -U 2>/dev/null", timeout=60, as_root=True)
            uids = {}
            for ln in out.decode("utf-8", "replace").splitlines():
                parts = dict(p.split(":", 1) for p in ln.split() if ":" in p)
                if parts.get("package") and parts.get("uid", "").split(",")[0].isdigit():
                    uids[parts["package"]] = parts["uid"].split(",")[0]
            for a in apps:
                a.uid = a.uid or uids.get(a.pkg, "")
            kind = FW.backend(self.conn)
            blocked, enabled = FW.blocked_uids(self.conn, kind)
            return apps, kind, blocked, FW.vpn_package(self.conn), FW.boot_hook_present(self.conn)
        self.work("apps", fn, "Loading apps…")

    def done_apps(self, r):
        self.apps, self.kind, self.blocked, self.vpn, hook = r
        self.keep.blockSignals(True)
        self.keep.setChecked(hook)
        self.keep.blockSignals(False)
        how = ("Android's own firewall (OEM_DENY_3 chain) – blocks every network, VPN included; resets at reboot"
               if self.kind == "chain3" else "iptables owner rules (older Android) – reset at reboot")
        self.fw_head.setText(f"Backend: {how}. Blocking works per uid: apps sharing a uid are blocked together."
                             + (f" Active VPN app: {self.vpn} (protected)." if self.vpn else ""))
        self.fill_apps()
        self.status.setText(f"{len(self.apps)} apps, {len(self.blocked)} blocked uid(s)")

    def protected(self, a):
        try:
            uid = int(a.uid)
        except (TypeError, ValueError):
            return "no uid"
        if uid < 10000:
            return "core Android uid"
        if a.pkg in FW.PROTECTED:
            return FW.PROTECTED[a.pkg]
        if self.vpn and a.pkg == self.vpn:
            return "the active VPN app"
        return ""

    def visible_apps(self):
        f = self.fw_filter.text().strip().lower()
        out = []
        for a in self.apps:
            if not a.uid:
                continue
            if not self.show_sys.isChecked() and a.system:
                continue
            blocked = int(a.uid) in self.blocked
            if self.only_blocked.isChecked() and not blocked:
                continue
            if f and f not in (a.name + " " + a.pkg).lower():
                continue
            out.append(a)
        return out

    def fill_apps(self):
        vis = self.visible_apps()
        self.atab.setRowCount(len(vis))
        for r, a in enumerate(vis):
            blocked = int(a.uid) in self.blocked
            prot = self.protected(a)
            cells = ["BLOCKED" if blocked else ("allowed" if not prot else f"allowed – {prot}"),
                     a.name, a.pkg, a.uid, a.kind]
            for c, v in enumerate(cells):
                it = QTableWidgetItem(v)
                if blocked:
                    it.setForeground(QColor(self.api.theme.DANGER))
                elif prot:
                    it.setForeground(QColor(self.api.theme.TEXT_FAINT))
                self.atab.setItem(r, c, it)
        self.atab.resizeColumnsToContents()
        self.atab.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

    def selected_app(self):
        vis = self.visible_apps()
        r = self.atab.currentRow()
        return vis[r] if 0 <= r < len(vis) else None

    def toggle_block(self):
        a = self.selected_app()
        if not a:
            return
        uid = int(a.uid)
        block = uid not in self.blocked
        prot = self.protected(a)
        if block and prot:
            QMessageBox.warning(self, "Firewall", f"{a.name} can't be blocked here: {prot}.")
            return
        sharing = [x.pkg for x in self.apps if x.uid == a.uid and x.pkg != a.pkg]
        if block and (a.system or sharing) and QMessageBox.question(
                self, "Firewall", f"Block {a.name}?"
                + (f"\n\nIt shares its uid with: {', '.join(sharing[:8])} – they lose network too." if sharing else "")
                + ("\n\nIt's a system app; blocking it can break features." if a.system else "")) \
                != QMessageBox.StandardButton.Yes:
            return
        self.work("block", lambda: (FW.set_blocked(self.conn, self.kind, a.pkg, uid, block), a, block),
                  f"{'Blocking' if block else 'Unblocking'} {a.name}…")

    def done_block(self, r):
        _, a, block = r
        self.on_log(f"{'blocked' if block else 'unblocked'} {a.name} ({a.pkg}, uid {a.uid})\n")
        uids, _ = FW.blocked_uids(self.conn, self.kind)
        self.blocked = uids
        self.fill_apps()
        if self.keep.isChecked():
            self.set_keep(True)

    def test_app(self):
        a = self.selected_app()
        if a:
            self.work("probe", lambda: (a, FW.probe(self.conn, int(a.uid))), f"Testing {a.name}…")

    def done_probe(self, r):
        a, ok = r
        self.on_log(f"{a.name}: TCP to 1.1.1.1:443 as uid {a.uid} -> {'connected' if ok else 'BLOCKED / failed'}\n")
        self.status.setText(f"{a.name}: {'can' if ok else 'cannot'} reach the internet")

    def set_keep(self, on):
        pk = [a.pkg for a in self.apps if a.uid and int(a.uid) in self.blocked]
        uids = sorted(self.blocked)
        if on and not pk:
            self.on_log("nothing blocked – no boot script needed\n")
        self.work("keep", lambda: FW.set_boot_hook(self.conn, self.kind, pk, uids, on),
                  "Updating the boot script…")

    def done_keep(self, _r):
        self.status.setText("Boot script " + ("updated" if self.keep.isChecked() else "removed"))

    # =======================================================================================================
    # Forwards & tunnels
    # =======================================================================================================
    PRESETS = [("RustDesk direct IP (21118)", "local", 21118, 21118), ("AndroOpenHeart SSH (2222)", "local", 2222, 2222),
               ("Dev server on PC → phone (8080)", "reverse", 8080, 8080)]

    def build_forwards(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.fw_note = QLabel("")
        self.fw_note.setWordWrap(True)
        lay.addWidget(self.fw_note)
        self.ftab = QTableWidget(0, 5)
        self.ftab.setHorizontalHeaderLabels(["State", "Direction", "PC port", "Device", "Via"])
        self.ftab.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.ftab.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.ftab.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.ftab.verticalHeader().setVisible(False)
        lay.addWidget(self.ftab, 1)
        row = QHBoxLayout()
        for text, fn in (("Add…", self.add_forward), ("Start", lambda: self.forward_action(True)),
                         ("Stop", lambda: self.forward_action(False)), ("Remove", self.remove_forward),
                         ("Refresh", self.refresh_forwards)):
            bt = QPushButton(text)
            bt.clicked.connect(fn)
            row.addWidget(bt)
        row.addStretch(1)
        lay.addLayout(row)
        return w

    def saved(self):
        return self.api.get_setting(f"forwards:{self.api.device_name}", [])

    def save(self, rows):
        self.api.set_setting(f"forwards:{self.api.device_name}", rows)

    def refresh_forwards(self):
        adb = TN.is_adb(self.conn) if self.conn else False
        self.fw_note.setText(
            "Connected over ADB: forwards use adb forward / adb reverse." if adb else
            "Connected over SSH: PC → device forwards are relayed through nc on the device (the SSH server "
            "doesn't allow SSH port forwarding). Reverse forwards need ADB.")
        active = set()
        if adb:
            try:
                for r in TN.adb_list(self.conn):
                    if r["kind"] == "local":
                        active.add(("local", int(r["pc"].split(":")[1]), int(r["dev"].split(":")[1])))
                    else:
                        active.add(("reverse", int(r["pc"].split(":")[1]), int(r["dev"].split(":")[1])))
            except Exception as e:  # noqa: BLE001
                self.on_log(f"adb list: {e}\n")
        rows = self.saved()
        self.ftab.setRowCount(len(rows))
        for i, f in enumerate(rows):
            on = (f["kind"], f["pc"], f["dev"]) in active or (f["kind"] == "local" and f["pc"] in TN.RELAYS)
            rel = TN.RELAYS.get(f["pc"])
            state = "ON" + (f" ({rel.active} open, {rel.total} total)" if rel else "") if on else "off"
            cells = [state, "PC → device" if f["kind"] == "local" else "device → PC", str(f["pc"]),
                     f"{f.get('host', '127.0.0.1')}:{f['dev']}", "adb" if adb else "ssh relay"]
            for c, v in enumerate(cells):
                it = QTableWidgetItem(v)
                if on:
                    it.setForeground(QColor(self.api.theme.ACCENT))
                self.ftab.setItem(i, c, it)
        self.ftab.resizeColumnsToContents()
        self.ftab.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

    def add_forward(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Add forward")
        fl = QFormLayout(dlg)
        preset = QComboBox()
        preset.addItem("Custom…")
        for p in self.PRESETS:
            preset.addItem(p[0])
        kind = QComboBox()
        kind.addItem("PC → device (local)", "local")
        kind.addItem("device → PC (reverse, ADB only)", "reverse")
        pc, dev = QSpinBox(), QSpinBox()
        for s in (pc, dev):
            s.setRange(1, 65535)
        pc.setValue(8022)
        dev.setValue(22)
        host = QLineEdit("127.0.0.1")

        def apply_preset(i):
            if i > 0:
                _, k, a, b = self.PRESETS[i - 1]
                kind.setCurrentIndex(0 if k == "local" else 1)
                pc.setValue(a)
                dev.setValue(b)
        preset.currentIndexChanged.connect(apply_preset)
        fl.addRow("Preset", preset)
        fl.addRow("Direction", kind)
        fl.addRow("PC port", pc)
        fl.addRow("Device port", dev)
        fl.addRow("Device host (SSH relay)", host)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        fl.addRow(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        rows = self.saved()
        rows.append({"kind": kind.currentData(), "pc": pc.value(), "dev": dev.value(),
                     "host": host.text().strip() or "127.0.0.1"})
        self.save(rows)
        self.ftab.selectRow(len(rows) - 1)
        self.refresh_forwards()
        self.ftab.selectRow(len(rows) - 1)
        self.forward_action(True)

    def forward_action(self, on):
        r = self.ftab.currentRow()
        rows = self.saved()
        if not 0 <= r < len(rows):
            return
        f = rows[r]
        if on:
            fn = lambda: TN.start(self.conn, f["kind"], f["pc"], f["dev"], f.get("host", "127.0.0.1"), self.b.log.emit)
        else:
            fn = lambda: TN.stop(self.conn, f["kind"], f["pc"], f["dev"]) or "stopped"
        self.work("fwd", fn, "Starting forward…" if on else "Stopping forward…")

    def done_fwd(self, msg):
        self.on_log(f"{msg}\n")
        self.refresh_forwards()

    def remove_forward(self):
        r = self.ftab.currentRow()
        rows = self.saved()
        if 0 <= r < len(rows):
            f = rows.pop(r)
            try:
                TN.stop(self.conn, f["kind"], f["pc"], f["dev"])
            except Exception:  # noqa: BLE001
                pass
            self.save(rows)
            self.refresh_forwards()

    # =======================================================================================================
    # Capture
    # =======================================================================================================
    def build_capture(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.cap_head = QLabel("Looking for tcpdump…")
        self.cap_head.setWordWrap(True)
        lay.addWidget(self.cap_head)
        fl = QFormLayout()
        self.iface = QComboBox()
        fl.addRow("Interface", self.iface)
        self.bpf = QLineEdit()
        self.bpf.setPlaceholderText("capture filter, e.g.  port 443   or   host 192.168.1.50   (empty = everything)")
        fl.addRow("Filter", self.bpf)
        self.snap = QSpinBox()
        self.snap.setRange(64, 262144)
        self.snap.setValue(262144)
        fl.addRow("Bytes per packet", self.snap)
        self.ws = QCheckBox("Show live in Wireshark too")
        fl.addRow("", self.ws)
        lay.addLayout(fl)
        row = QHBoxLayout()
        self.cap_btn = QPushButton("Start capture")
        self.cap_btn.clicked.connect(self.toggle_capture)
        row.addWidget(self.cap_btn)
        self.install_btn = QPushButton("Install tcpdump in Ubuntu")
        self.install_btn.clicked.connect(self.install_tcpdump)
        self.install_btn.setVisible(False)
        row.addWidget(self.install_btn)
        self.cap_stats = QLabel("")
        row.addWidget(self.cap_stats, 1)
        lay.addLayout(row)
        lay.addWidget(QLabel("Saved captures (double-click to open)"))
        self.caps = QListWidget()
        self.caps.doubleClicked.connect(lambda *_: self.open_capture())
        lay.addWidget(self.caps, 1)
        row2 = QHBoxLayout()
        ob = QPushButton("Open folder")
        ob.clicked.connect(lambda: os.startfile(CP.captures_dir(self.api.device_name)))
        row2.addWidget(ob)
        row2.addStretch(1)
        lay.addLayout(row2)
        self.list_captures()
        return w

    def detect_capture(self):
        self.work("tool", lambda: (CP.find_tool(self.conn), CP.interfaces(self.conn)), "Looking for tcpdump…")

    def done_tool(self, r):
        self.tool, ifs = r
        self.iface.clear()
        self.iface.addItems(ifs)
        for pref in ("wlan0", "tun0", "any"):
            if pref in ifs:
                self.iface.setCurrentText(pref)
                break
        kind = self.tool[0]
        ws = CP.wireshark_path()
        self.ws.setEnabled(bool(ws))
        self.ws.setText("Show live in Wireshark too" if ws else "Show live in Wireshark (not installed on this PC)")
        self.install_btn.setVisible(kind == "chroot-missing")
        self.cap_btn.setEnabled(kind in ("system", "chroot"))
        self.cap_head.setText({
            "system": f"tcpdump: {self.tool[1]} (on the device)",
            "chroot": "tcpdump: from the Phone desktop Ubuntu (shares Android's network)",
            "chroot-missing": "No tcpdump on the device. Phone desktop's Ubuntu is installed – press "
                              "'Install tcpdump in Ubuntu' to get it there.",
            "": "No tcpdump on this device (stock Android doesn't ship it). Install Phone desktop (Ubuntu) "
                "to capture through it."}[kind]
            + " Captures are saved as .pcap in " + CP.captures_dir(self.api.device_name))
        self.status.setText("Ready")

    def install_tcpdump(self):
        self.work("tinstall", lambda: CP.install_in_chroot(self.conn), "Installing tcpdump in Ubuntu…")

    def done_tinstall(self, text):
        self.on_log(text + "\n")
        self.detect_capture()

    def toggle_capture(self):
        if self.cap:
            self.cap.stop()
            return
        try:
            self.cap = CP.Capture(self.conn, self.tool, self.iface.currentText(), self.bpf.text().strip(),
                                  self.snap.value(), self.api.device_name, self.ws.isChecked(),
                                  on_end=lambda m: self.b.done.emit("capend", m))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Capture", str(e))
            self.cap = None
            return
        self.cap_t0 = time.time()
        self.cap_btn.setText("Stop capture")
        self.on_log(f"capturing on {self.iface.currentText()} -> {self.cap.path}\n")

    def done_capend(self, msg):
        if self.cap:
            self.on_log(f"{msg}: {self.cap.packets} packets, {human(self.cap.bytes)} -> {self.cap.path}\n")
        self.cap = None
        self.cap_btn.setText("Start capture")
        self.list_captures()

    def tick(self):
        if self.cap:
            self.cap_stats.setText(f"{int(time.time() - self.cap_t0)} s · {self.cap.packets} packets · "
                                   f"{human(self.cap.bytes)}")
        if self.tabs.currentIndex() == 1 and TN.RELAYS:
            self.refresh_forwards()

    def list_captures(self):
        d = CP.captures_dir(self.api.device_name)
        self.caps.clear()
        for n in sorted(os.listdir(d), reverse=True):
            if n.endswith(".pcap"):
                self.caps.addItem(f"{n}   ({human(os.path.getsize(os.path.join(d, n)))})")

    def open_capture(self):
        it = self.caps.currentItem()
        if it:
            path = os.path.join(CP.captures_dir(self.api.device_name), it.text().split("   ")[0])
            try:
                os.startfile(path)
            except OSError:
                QMessageBox.information(self, "Capture", f"No program is set to open .pcap files.\n\n{path}")

    def closeEvent(self, e):
        if self.cap:
            self.cap.stop()
        self.timer.stop()
        _win.pop("w", None)
        super().closeEvent(e)
