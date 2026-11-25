"""App tools - APK inspector, app data snapshots, notification log (AndroUnderTheHood plugin)."""
import csv
import html
import os
import shutil
import tempfile
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from . import apk as AP
from . import notif as NT
from . import snapshots as SN

_win = {}


def register(api):
    api.add_tool("App tools", lambda: open_window(api), icon="bookmark",
                 tooltip="APK inspector, app data snapshots, notification log (plugin)", needs="root")
    api.add_file_action("Inspect APK", lambda entries: open_window(api, apk_on_device=entries[0].path),
                        when="file")


def unload(api):
    w = _win.pop("w", None)
    if w:
        try:
            w.close()
        except RuntimeError:
            pass


def open_window(api, apk_on_device=None):
    w = _win.get("w")
    try:
        if w is None or not w.isVisible():
            w = _win["w"] = AppTools(api)
            w.show()
        else:
            w.raise_()
            w.activateWindow()
    except RuntimeError:
        w = _win["w"] = AppTools(api)
        w.show()
    if apk_on_device:
        if not apk_on_device.lower().endswith(".apk"):
            QMessageBox.information(w, "Inspect APK", "Pick an .apk file.")
            return
        w.tabs.setCurrentIndex(0)
        w.inspect_device_file(apk_on_device)


class Bridge(QObject):
    done = Signal(str, object)
    fail = Signal(str, str)
    log = Signal(str)


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


class AppTools(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"App tools – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1150, 800)
        self.b = Bridge()
        self.b.done.connect(self.on_done)
        self.b.fail.connect(self.on_fail)
        self.busy, self.queue = False, []
        self.apps = []
        self.cache = os.path.join(api.data_dir or tempfile.gettempdir(), "apk-cache")
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.tabs.addTab(self.build_apk(), "APK inspector")
        self.tabs.addTab(self.build_snap(), "App data snapshots")
        self.tabs.addTab(self.build_notif(), "Notification log")
        self.status = QLabel("")
        lay.addWidget(self.status)
        self.store = NT.Store(api.data_dir or tempfile.gettempdir(), api.device_name)
        self.load_apps()
        self.refresh_notif_status()
        self.fill_notifs()

    # ---- plumbing ---------------------------------------------------------------------------------------
    @property
    def conn(self):
        return self.api.conn

    def work(self, op, fn, label=""):
        if self.busy:
            self.queue.append((op, fn, label))
            return
        self.busy = True
        self.status.setText(label)

        def body():
            try:
                r = fn()
            except Exception as e:  # noqa: BLE001
                self.api.log.exception("app tools %s", op)
                self.b.fail.emit(op, str(e) or e.__class__.__name__)
            else:
                self.b.done.emit(op, r)
        threading.Thread(target=body, daemon=True).start()

    def next_job(self):
        if self.queue and not self.busy:
            self.work(*self.queue.pop(0))

    def on_fail(self, op, msg):
        self.busy = False
        self.status.setText(f"{op} failed: {msg}")
        QMessageBox.warning(self, "App tools", msg)
        self.next_job()

    def on_done(self, op, r):
        self.busy = False
        getattr(self, "done_" + op, lambda r: self.status.setText(f"{op}: done"))(r)
        self.next_job()

    def load_apps(self):
        def fn():
            rc, out, _ = self.conn.exec("pm list packages -U -f 2>/dev/null; echo @@3; pm list packages -3 2>/dev/null",
                                        timeout=60, as_root=True)
            text = out.decode("utf-8", "replace")
            allp, _, third = text.partition("@@3")
            user = {ln.split(":", 1)[1].strip() for ln in third.splitlines() if ln.startswith("package:")}
            apps = []
            for ln in allp.splitlines():
                if not ln.startswith("package:"):
                    continue
                body = ln[8:]
                uid = ""
                if " uid:" in body:
                    body, uid = body.rsplit(" uid:", 1)
                path, _, pkg = body.rpartition("=")
                apps.append({"pkg": pkg, "path": path, "uid": uid.split(",")[0], "user": pkg in user})
            return sorted(apps, key=lambda a: (not a["user"], a["pkg"]))
        self.work("apps", fn, "Loading apps…")

    def done_apps(self, apps):
        self.apps = apps
        self.fill_app_lists()
        self.status.setText(f"{len(apps)} apps")

    def fill_app_lists(self):
        for lst, flt, sysbox in ((self.alist, self.afilter, self.asys), (self.slist, self.sfilter, self.ssys)):
            f = flt.text().strip().lower()
            lst.clear()
            for a in self.apps:
                if not sysbox.isChecked() and not a["user"]:
                    continue
                if f and f not in a["pkg"].lower():
                    continue
                it = QListWidgetItem(a["pkg"])
                it.setData(Qt.ItemDataRole.UserRole, a)
                if not a["user"]:
                    it.setForeground(QColor(self.api.theme.TEXT_DIM))
                lst.addItem(it)

    def app_list_panel(self, title):
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(QLabel(title))
        flt = QLineEdit()
        flt.setPlaceholderText("Search…")
        v.addWidget(flt)
        sysbox = QCheckBox("Show system apps")
        v.addWidget(sysbox)
        lst = QListWidget()
        v.addWidget(lst, 1)
        flt.textChanged.connect(lambda *_: self.fill_app_lists())
        sysbox.toggled.connect(lambda *_: self.fill_app_lists())
        return box, flt, sysbox, lst

    # =======================================================================================================
    # APK inspector
    # =======================================================================================================
    def build_apk(self):
        w = QSplitter()
        box, self.afilter, self.asys, self.alist = self.app_list_panel("Installed apps (double-click to inspect)")
        self.alist.itemDoubleClicked.connect(lambda it: self.inspect_installed(it.data(Qt.ItemDataRole.UserRole)))
        ob = QPushButton("Open APK file on this PC…")
        ob.clicked.connect(self.inspect_local)
        box.layout().addWidget(ob)
        w.addWidget(box)
        self.report = QTextBrowser()
        self.report.setOpenExternalLinks(False)
        self.report.setHtml("<p>Pick an installed app, an APK file on this PC, or right-click an .apk in the file "
                            "browser → <b>Inspect APK</b>.</p>")
        w.addWidget(self.report)
        w.setSizes([300, 850])
        return w

    def inspect_installed(self, a):
        def fn():
            rc, out, _ = self.conn.exec(f"pm path {a['pkg']}", timeout=60, as_root=True)
            remote = [ln.split(":", 1)[1].strip() for ln in out.decode().splitlines() if ln.startswith("package:")]
            remote.sort(key=lambda p: (not p.endswith("/base.apk"), p))
            d = os.path.join(self.cache, a["pkg"])
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d)
            fs = self.conn.new_fs()
            local = []
            for i, rp in enumerate(remote):
                lp = os.path.join(d, os.path.basename(rp))
                self.b.done.emit("progress", f"Copying {os.path.basename(rp)} ({i + 1}/{len(remote)})…")
                fs.download(rp, lp, lambda n: None)
                local.append(lp)
            r = AP.inspect(local)
            r["source"] = f"installed app {a['pkg']} ({len(remote)} APK{'s' if len(remote) > 1 else ''})"
            return r
        self.work("report", fn, f"Inspecting {a['pkg']}…")

    def inspect_device_file(self, path):
        def fn():
            os.makedirs(self.cache, exist_ok=True)
            lp = os.path.join(self.cache, os.path.basename(path))
            self.conn.new_fs().download(path, lp, lambda n: None)
            r = AP.inspect([lp])
            r["source"] = f"device file {path}"
            return r
        self.work("report", fn, f"Inspecting {path}…")

    def inspect_local(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "APK file(s) – base first, then splits", "", "APK (*.apk)")
        if not paths:
            return

        def fn():
            r = AP.inspect(paths)
            r["source"] = paths[0]
            return r
        self.work("report", fn, "Inspecting…")

    def done_progress(self, text):
        self.busy = True          # still running: a progress message, not the end
        self.status.setText(text)

    def done_report(self, r):
        t = self.api.theme
        col = {"bad": t.DANGER, "warn": "#f0b429", "info": t.ACCENT}
        m, sdk, app = r["manifest"], r["uses_sdk"], r["app"]
        e = html.escape
        h = [f"<h2>{e(str(m.get('package', '?')))}</h2>",
             f"<p>{e(r.get('source', ''))}<br>version <b>{e(str(m.get('versionName', '?')))}</b> "
             f"(code {e(str(m.get('versionCode', '?')))}) · min SDK {e(str(sdk.get('minSdkVersion', '?')))} · "
             f"target SDK {e(str(sdk.get('targetSdkVersion', '?')))}</p>"]
        notes = AP.risk_notes(r)
        if notes:
            h.append("<h3>Findings</h3><ul>")
            for lv, txt in notes:
                h.append(f"<li><span style='color:{col[lv]}'>{e(txt)}</span></li>")
            h.append("</ul>")
        s = r["signing"]
        h.append(f"<h3>Signing</h3><p>Schemes: {e(', '.join(s['schemes']) or 'none')}</p>")
        for c in s["certs"]:
            h.append(f"<p><b>{e(c.get('subject', '?'))}</b><br>valid {e(c.get('valid_from', '?'))} → "
                     f"{e(c.get('valid_to', '?'))} · {e(c.get('algorithm', ''))}<br>"
                     f"<code>SHA-256 {e(c['sha256'])}</code></p>")
        h.append(f"<h3>Native code</h3><p>{e(', '.join(r['abis']) or 'none (Java/Kotlin only)')} · "
                 f"{len(r['dex'])} dex file(s)</p>")
        h.append(f"<h3>Permissions ({len(r['permissions'])})</h3><table cellspacing=4>")
        order = {"special": 0, "dangerous": 1, "custom": 2, "normal": 3}
        for p in sorted(r["permissions"], key=lambda p: (order[AP.perm_kind(p)], p)):
            k = AP.perm_kind(p)
            c = t.DANGER if k == "special" else "#f0b429" if k == "dangerous" else t.TEXT_DIM
            h.append(f"<tr><td style='color:{c}'>{k}</td><td>{e(p)}</td></tr>")
        h.append("</table>")
        comps = r["components"]
        h.append(f"<h3>Components ({len(comps)}, exported {sum(c['exported'] for c in comps)})</h3><table cellspacing=4>")
        for c in sorted(comps, key=lambda c: (not c["open"], not c["exported"], c["type"], c["name"])):
            state = ("<span style='color:%s'>open to all apps</span>" % t.DANGER if c["open"] else
                     "exported, needs " + e(str(c["permission"])) if c["exported"] else "internal")
            extra = (" · " + e(", ".join(c["filters"][:4]))) if c["filters"] else ""
            h.append(f"<tr><td>{c['type']}</td><td>{e(c['name'])}</td><td>{state}{extra}</td></tr>")
        h.append("</table>")
        if r["trackers"]:
            h.append("<h3>Tracker / analytics SDKs</h3><p>" + e(", ".join(r["trackers"])) + "</p>")
        self.report.setHtml("".join(h))
        self.status.setText("Inspected " + str(m.get("package", "")))

    # =======================================================================================================
    # Snapshots
    # =======================================================================================================
    def build_snap(self):
        w = QSplitter()
        box, self.sfilter, self.ssys, self.slist = self.app_list_panel("Apps")
        self.slist.currentItemChanged.connect(lambda *_: self.fill_snaps())
        w.addWidget(box)
        right = QWidget()
        v = QVBoxLayout(right)
        self.snap_head = QLabel("Pick an app.")
        self.snap_head.setWordWrap(True)
        v.addWidget(self.snap_head)
        row = QHBoxLayout()
        tb = QPushButton("Take snapshot")
        tb.clicked.connect(self.take_snapshot)
        row.addWidget(tb)
        self.with_ext = QCheckBox("include Android/data on internal storage")
        row.addWidget(self.with_ext)
        row.addStretch(1)
        v.addLayout(row)
        self.snaps = QTableWidget(0, 4)
        self.snaps.setHorizontalHeaderLabels(["Taken", "Version", "Size", "Folders"])
        self.snaps.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.snaps.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.snaps.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.snaps.verticalHeader().setVisible(False)
        v.addWidget(self.snaps, 1)
        row2 = QHBoxLayout()
        for text, fn in (("Restore…", self.restore_snapshot), ("Delete", self.delete_snapshot),
                         ("Open folder", lambda: self.cur_snap_app() and os.startfile(
                             SN.folder(self.api.device_name, self.cur_snap_app()["pkg"])))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row2.addWidget(b)
        row2.addStretch(1)
        v.addLayout(row2)
        note = QLabel("A snapshot holds the app's private data (/data/data, /data/user_de). Restoring stops the app, "
                      "replaces its data and sets owners and SELinux labels for the app as installed now.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {self.api.theme.TEXT_DIM};")
        v.addWidget(note)
        w.addWidget(right)
        w.setSizes([300, 850])
        return w

    def cur_snap_app(self):
        it = self.slist.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def fill_snaps(self):
        a = self.cur_snap_app()
        self.snap_list = SN.list_snapshots(self.api.device_name, a["pkg"]) if a else []
        self.snap_head.setText(f"{a['pkg']} (uid {a['uid']})" if a else "Pick an app.")
        self.snaps.setRowCount(len(self.snap_list))
        for i, m in enumerate(self.snap_list):
            cells = [m["created"], f"{m.get('versionName', '?')} ({m.get('versionCode', '?')})",
                     human(m.get("archive_size", 0)), ", ".join(m["dirs"])]
            for c, v in enumerate(cells):
                self.snaps.setItem(i, c, QTableWidgetItem(v))
        self.snaps.resizeColumnsToContents()
        self.snaps.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

    def take_snapshot(self):
        a = self.cur_snap_app()
        if not a:
            return
        ext = self.with_ext.isChecked()
        self.work("snap", lambda: SN.take(self.conn, self.conn.new_fs(), a["pkg"], self.api.device_name, ext,
                                          lambda t: None, lambda n: None),
                  f"Snapshot of {a['pkg']} (the app is stopped while packing)…")

    def done_snap(self, m):
        self.fill_snaps()
        self.status.setText(f"Snapshot saved: {human(m['archive_size'])}")

    def selected_snap(self):
        r = self.snaps.currentRow()
        return self.snap_list[r] if 0 <= r < len(self.snap_list) else None

    def restore_snapshot(self):
        m = self.selected_snap()
        if not m:
            return
        if QMessageBox.question(self, "Restore snapshot",
                                f"Replace ALL current data of {m['pkg']} with the snapshot from {m['created']} "
                                f"(version {m.get('versionName', '?')})?\n\nThe app is stopped first. Data it has now "
                                "and that isn't in the snapshot is lost – take a snapshot first if you may want it "
                                "back.") != QMessageBox.StandardButton.Yes:
            return
        self.work("restore", lambda: SN.restore(self.conn, self.conn.new_fs(), m, lambda t: None, lambda n: None),
                  f"Restoring {m['pkg']}…")

    def done_restore(self, msg):
        self.status.setText(msg)

    def delete_snapshot(self):
        m = self.selected_snap()
        if m and QMessageBox.question(self, "Delete snapshot", f"Delete the snapshot from {m['created']}?") \
                == QMessageBox.StandardButton.Yes:
            for p in (m["path"], m["path"][:-7] + ".json"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            self.fill_snaps()

    # =======================================================================================================
    # Notification log
    # =======================================================================================================
    def build_notif(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.nstat = QLabel("")
        self.nstat.setWordWrap(True)
        v.addWidget(self.nstat)
        row = QHBoxLayout()
        self.nbtn = QPushButton("Start logging on the device")
        self.nbtn.clicked.connect(self.toggle_logger)
        row.addWidget(self.nbtn)
        ib = QPushButton("Import from device")
        ib.clicked.connect(self.import_notifs)
        row.addWidget(ib)
        cb = QPushButton("Add what's showing now")
        cb.clicked.connect(lambda: self.work("nimport", lambda: self.store.add(NT.current(self.conn)),
                                             "Reading current notifications…"))
        row.addWidget(cb)
        row.addStretch(1)
        eb = QPushButton("Export CSV…")
        eb.clicked.connect(self.export_notifs)
        row.addWidget(eb)
        xb = QPushButton("Clear…")
        xb.clicked.connect(self.clear_notifs)
        row.addWidget(xb)
        v.addLayout(row)
        row2 = QHBoxLayout()
        self.nsearch = QLineEdit()
        self.nsearch.setPlaceholderText("Search title, text or app…")
        self.nsearch.textChanged.connect(lambda *_: self.fill_notifs())
        row2.addWidget(self.nsearch, 1)
        self.napp = QComboBox()
        self.napp.currentIndexChanged.connect(lambda *_: self.fill_notifs())
        row2.addWidget(self.napp)
        v.addLayout(row2)
        self.ntab = QTableWidget(0, 4)
        self.ntab.setHorizontalHeaderLabels(["Time", "App", "Title", "Text"])
        self.ntab.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.ntab.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.ntab.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.ntab.verticalHeader().setVisible(False)
        v.addWidget(self.ntab, 1)
        note = QLabel("The logger runs on the device as root and checks every 10 s; it keeps logging while the PC "
                      "is away (until the device restarts). Entries stay on the device (root-only file) and in a "
                      "database on this PC – nothing is sent anywhere else.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {self.api.theme.TEXT_DIM};")
        v.addWidget(note)
        return w

    def refresh_notif_status(self):
        self.work("nstatus", lambda: NT.status(self.conn), "")

    def done_nstatus(self, st):
        self.nlog_running = st["running"]
        self.nbtn.setText("Stop logging" if st["running"] else "Start logging on the device")
        self.nstat.setText(("Logger running on the device" if st["running"] else "Logger not running")
                           + f" · {st['lines']} entries waiting on the device")
        self.status.setText("")

    def toggle_logger(self):
        if getattr(self, "nlog_running", False):
            self.work("nstatus", lambda: (NT.stop(self.conn), NT.status(self.conn))[1], "Stopping the logger…")
        else:
            self.work("nstatus", lambda: (NT.start(self.conn), time.sleep(1), NT.status(self.conn))[2],
                      "Starting the logger…")

    def import_notifs(self):
        self.work("nimport", lambda: self.store.add(NT.fetch_log(self.conn)), "Importing…")

    def done_nimport(self, n):
        self.status.setText(f"{n} new notification(s) added")
        self.fill_notifs()
        self.refresh_notif_status()

    def fill_notifs(self):
        cur = self.napp.currentData() or ""
        self.napp.blockSignals(True)
        self.napp.clear()
        self.napp.addItem("All apps", "")
        for pkg, n in self.store.apps():
            self.napp.addItem(f"{pkg} ({n})", pkg)
        i = self.napp.findData(cur)
        self.napp.setCurrentIndex(max(0, i))
        self.napp.blockSignals(False)
        rows = self.store.query(self.nsearch.text().strip(), self.napp.currentData() or "")
        self.ntab.setRowCount(len(rows))
        for r, (posted, pkg, title, text, _ch) in enumerate(rows):
            for c, v in enumerate((time.strftime("%Y-%m-%d %H:%M", time.localtime(posted)), pkg, title, text)):
                it = QTableWidgetItem(v)
                it.setToolTip(v)
                self.ntab.setItem(r, c, it)
        self.ntab.resizeColumnsToContents()
        self.ntab.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

    def export_notifs(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export notifications", "notifications.csv", "CSV (*.csv)")
        if path:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["time", "app", "title", "text", "channel"])
                for posted, pkg, title, text, ch in self.store.query(self.nsearch.text().strip(),
                                                                       self.napp.currentData() or "", 10 ** 7):
                    w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(posted)), pkg, title, text, ch])
            self.status.setText(f"Exported to {path}")

    def clear_notifs(self):
        r = QMessageBox.question(self, "Clear", "Delete the logged notifications on this PC AND on the device?",
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            self.store.clear()
            self.work("nimport", lambda: (NT.clear_device(self.conn), 0)[1], "Clearing…")

    def closeEvent(self, e):
        _win.pop("w", None)
        super().closeEvent(e)
