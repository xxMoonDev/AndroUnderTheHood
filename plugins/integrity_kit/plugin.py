"""Play Integrity plugin: install and configure Tricky Store + Play Integrity Fix on any root manager."""
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                               QVBoxLayout)

from . import engine as E

_open = {}


def register(api):
    api.add_tool("Play Integrity", lambda: open_dialog(api), icon="badge",
                 tooltip="Install and configure Tricky Store / Play Integrity Fix (plugin)", needs="root")
    api.on("disconnected", lambda: _close_all())


def unload(api):
    _close_all()


def _close_all():
    for d in list(_open.values()):
        try:
            d.close()
        except RuntimeError:
            pass
    _open.clear()


def open_dialog(api):
    d = _open.get("dlg")
    try:
        if d is not None and d.isVisible():
            d.raise_()
            d.activateWindow()
            return
    except RuntimeError:
        pass
    d = IntegrityDialog(api)
    _open["dlg"] = d
    d.show()


class _Bridge(QObject):
    line = Signal(str)


ROWS = [("device", "Device"), ("root", "Root manager"), ("zygisk", "Zygisk"), ("tricky", "Keystore spoof"),
        ("pif", "Play Integrity Fix"), ("fp", "PIF fingerprint"), ("hide", "Root hiding"), ("targets", "Target apps"),
        ("patch", "Patch level spoof"), ("keybox", "Keybox"), ("tee", "TEE status"), ("boot", "Boot state props"),
        ("verdict", "Last verdict")]


class IntegrityDialog(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.conn = api.conn
        self.st = None
        self.busy = False
        self.cache = api.data_dir
        self.setWindowTitle(f"Play Integrity – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(900, 820)
        self.bridge = _Bridge()
        self.bridge.line.connect(self._append)
        t = api.theme
        lay = QVBoxLayout(self)

        # ---- status ----------------------------------------------------------
        box = QGroupBox("Status")
        g = QGridLayout(box)
        self.vals = {}
        for r, (k, label) in enumerate(ROWS):
            a = QLabel(label)
            a.setStyleSheet(f"color: {t.TEXT_DIM};")
            b = QLabel("…")
            b.setWordWrap(True)
            b.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            g.addWidget(a, r, 0, Qt.AlignmentFlag.AlignTop)
            g.addWidget(b, r, 1)
            self.vals[k] = b
        g.setColumnStretch(1, 1)
        lay.addWidget(box)

        # ---- install -----------------------------------------------------------
        box = QGroupBox("Install / update (official GitHub releases, SHA-256 checked)")
        g = QGridLayout(box)
        self.c_tricky = self._combo([k for k, c in E.COMPONENTS.items() if c.kind == "tricky"])
        self.c_pif = self._combo([k for k, c in E.COMPONENTS.items() if c.kind == "pif"])
        self.c_zyg = self._combo([k for k, c in E.COMPONENTS.items() if c.kind == "zygisk"])
        self.l_zyg = QLabel("Zygisk provider")
        self.chk_hide = QCheckBox("Also install Zygisk Assistant (root hiding)")
        self.chk_conf = QCheckBox("Remove conflicting modules")
        self.chk_conf.setChecked(True)
        g.addWidget(QLabel("Keystore spoof"), 0, 0)
        g.addWidget(self.c_tricky, 0, 1)
        g.addWidget(QLabel("Play Integrity Fix"), 1, 0)
        g.addWidget(self.c_pif, 1, 1)
        g.addWidget(self.l_zyg, 2, 0)
        g.addWidget(self.c_zyg, 2, 1)
        row = QHBoxLayout()
        row.addWidget(self.chk_hide)
        row.addWidget(self.chk_conf)
        row.addStretch(1)
        self.b_install = QPushButton(api.icon("download"), " Install / update")
        self.b_install.clicked.connect(self.do_install)
        row.addWidget(self.b_install)
        g.addLayout(row, 3, 0, 1, 2)
        g.setColumnStretch(1, 1)
        lay.addWidget(box)

        # ---- configure ------------------------------------------------------------
        box = QGroupBox("Configure")
        g = QGridLayout(box)
        b1 = QPushButton("Add Google apps")
        b1.setToolTip("Play services, Play Store, GSF, Wallet, Google Pay")
        b1.clicked.connect(lambda: self.add_targets(False))
        b2 = QPushButton("Add all user apps")
        b2.setToolTip("Every installed (non-system) app, plus the Google apps")
        b2.clicked.connect(lambda: self.add_targets(True))
        b3 = QPushButton("Edit list…")
        b3.clicked.connect(self.edit_targets)
        g.addWidget(QLabel("Target apps"), 0, 0)
        r = QHBoxLayout()
        for b in (b1, b2, b3):
            r.addWidget(b)
        r.addStretch(1)
        g.addLayout(r, 0, 1)
        self.c_patch = QComboBox()
        self.c_patch.addItem(f"Recent: {E.default_patch_date()}", "recent")
        self.c_patch.addItem("Device's real patch level (no spoof)", "device")
        self.c_patch.addItem("Custom date…", "custom")
        bp = QPushButton("Apply")
        bp.clicked.connect(self.apply_patch)
        g.addWidget(QLabel("Patch level spoof"), 1, 0)
        r = QHBoxLayout()
        r.addWidget(self.c_patch)
        r.addWidget(bp)
        r.addStretch(1)
        g.addLayout(r, 1, 1)
        bk = QPushButton(api.icon("upload"), " Import keybox.xml…")
        bk.setToolTip("Use your own keybox. The old one is kept as keybox.xml.bak-<date>.")
        bk.clicked.connect(self.import_keybox)
        g.addWidget(QLabel("Keybox"), 2, 0)
        r = QHBoxLayout()
        r.addWidget(bk)
        r.addStretch(1)
        g.addLayout(r, 2, 1)
        g.setColumnStretch(1, 1)
        lay.addWidget(box)

        # ---- actions ---------------------------------------------------------------
        r = QHBoxLayout()
        self.b_fp = QPushButton("Generate fingerprint")
        self.b_fp.setToolTip("Runs the PIF module's own autopif script (latest Pixel Canary build)")
        self.b_fp.clicked.connect(self.gen_fp)
        b_kill = QPushButton("Restart Play services")
        b_kill.setToolTip("Kill DroidGuard (gms.unstable) and the Play Store so the next check uses the new setup")
        b_kill.clicked.connect(lambda: self.shell(E.KILL_PI, "Restart Play services"))
        self.b_check = QPushButton(api.icon("shield"), " Check verdict")
        self.b_check.clicked.connect(self.check)
        self.b_daemon = QPushButton("Start daemon")
        self.b_daemon.setToolTip("The Tricky Store daemon is not running – start it without rebooting")
        self.b_daemon.clicked.connect(self.start_daemon)
        self.b_daemon.setVisible(False)
        b_reboot = QPushButton(api.icon("refresh"), " Reboot")
        b_reboot.clicked.connect(self.reboot)
        b_ref = QPushButton(api.icon("refresh"), " Refresh")
        b_ref.clicked.connect(self.refresh)
        for b in (self.b_daemon, self.b_fp, b_kill, self.b_check, b_reboot):
            r.addWidget(b)
        r.addStretch(1)
        r.addWidget(b_ref)
        lay.addLayout(r)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        self.log.setFont(f)
        lay.addWidget(self.log, 1)
        self.buttons = [self.b_install, b1, b2, b3, bp, bk, self.b_fp, b_kill, self.b_check, b_reboot, b_ref,
                        self.b_daemon]
        self.refresh()

    # ---- helpers ------------------------------------------------------------------
    def _combo(self, keys):
        c = QComboBox()
        for k in keys:
            c.addItem(E.COMPONENTS[k].name, k)
            c.setItemData(c.count() - 1, E.COMPONENTS[k].note, Qt.ItemDataRole.ToolTipRole)
        c.addItem("Don't install", "")
        return c

    def _append(self, s):
        self.log.moveCursor(QTextCursor.MoveOperation.End)
        self.log.insertPlainText(s)
        self.log.moveCursor(QTextCursor.MoveOperation.End)

    def say(self, s):
        self.bridge.line.emit(s)

    def _busy(self, on):
        self.busy = on
        for b in self.buttons:
            b.setEnabled(not on)

    def run(self, title, fn, done=None, refresh=True):
        if self.busy:
            return
        if not self.api.connected():
            self.api.message("Not connected", error=True)
            return
        self._busy(True)
        self.say(f"\n### {title}\n")

        def ok(res):
            self._busy(False)
            if done:
                done(res)
            if refresh:
                self.refresh()

        def fail(msg):
            self._busy(False)
            self.say(f"ERROR: {msg}\n")
            QMessageBox.warning(self, title, msg)
        self.api.task(fn, ok, fail)

    def shell(self, cmd, title, timeout=300):
        def work():
            rc, out = E.sh(self.conn, cmd, timeout=timeout)
            self.say(out if out.endswith("\n") else out + "\n")
            if rc != 0:
                raise RuntimeError(f"{title} failed (exit {rc})")
        self.run(title, work)

    def ask(self, title, text):
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    # ---- status -----------------------------------------------------------------------
    def refresh(self):
        if self.busy:
            return
        self._busy(True)
        for v in self.vals.values():
            if v.text() == "…":
                v.setText("reading…")

        def work():
            st = E.detect(self.conn)
            if st.keybox and not st.keybox.error:
                try:
                    E.check_revocation(st.keybox, self.cache)
                except Exception as e:  # noqa: BLE001 – offline is fine
                    self.api.log.info("revocation check failed: %s", e)
            return st

        def done(st):
            self._busy(False)
            self.st = st
            self.show_state()

        def fail(msg):
            self._busy(False)
            self.say(f"ERROR: {msg}\n")
            self.vals["root"].setText(msg)
        self.api.task(work, done, fail)

    def _set(self, key, text, bad=False, good=False):
        t = self.api.theme
        lbl = self.vals[key]
        lbl.setText(text)
        lbl.setStyleSheet(f"color: {t.DANGER};" if bad else ("color: #3fb950;" if good else ""))

    def show_state(self):
        st, p = self.st, self.st.props
        self._set("device", f"{p.get('model', '')} · Android {p.get('rel', '')} (SDK {p.get('sdk', '')}) · "
                            f"{p.get('abi', '')} · patch {p.get('patch', '')}")
        m = st.mgr
        dl = ""
        if m.magisk_family:
            dl = " · DenyList " + ("enforced" if m.denylist_on else "not enforced")
        self._set("root", f"{m.name} {m.version}{dl}")
        z = st.zygisk
        self._set("zygisk", z or "none – Play Integrity Fix needs Zygisk", bad=not z, good=bool(z))
        procs = st.procs.lower()
        self.b_daemon.setVisible(False)
        for kind, key in (("tricky", "tricky"), ("pif", "pif")):
            k, mod = st.installed(kind)
            if not mod:
                self._set(key, "not installed", bad=True)
                continue
            text = f"{mod.name} {mod.props.get('version', '')} – {mod.status}"
            if kind == "tricky" and not mod.pending_install and mod.enabled:
                running = ("tricky" in procs) or ("teesim" in procs)
                text += " · daemon running" if running else " · daemon NOT running"
                self.b_daemon.setVisible(not running)
                if not running:
                    self._set(key, text, bad=True)
                    continue
            if k == "?":
                text += " (unknown variant)"
            self._set(key, text, good=mod.enabled and not mod.pending_install)
        self._set("fp", p.get("pif_fp") or ("(none yet – press Generate fingerprint)"
                                            if st.installed("pif")[1] else "—"))
        hide = [mod.name for mod in st.modules if mod.id in E.CONFLICTS["hide"] and not mod.removing]
        self._set("hide", ", ".join(hide) or ("Magisk DenyList" if m.denylist_on else "none"))
        k, _ = st.installed("tricky")
        if k == "teesim" and st.teesim_cfg:
            prof = st.teesim_cfg.get("profiles", {}).get("default", {})
            self._set("targets", f"{len(prof.get('apps', []))} apps (TEESimulator config.json)")
            self._set("patch", str(prof.get("patchLevel", "")))
        else:
            n = len([x for x in st.targets if x.strip() and not x.startswith("#")])
            core = [c for c in ("com.google.android.gms", "com.android.vending")
                    if not any(x.split("#")[0].strip().rstrip("?!") == c for x in st.targets)]
            self._set("targets", f"{n} packages" + (f" – missing {', '.join(core)}" if core else ""), bad=bool(core))
            self._set("patch", st.security_patch.replace("\n", " · ") or "not set (real patch levels)")
        kb = st.keybox
        if kb:
            self._set("keybox", kb.summary(), bad=bool(kb.revoked) or bool(kb.error), good=kb.checked and not
                      kb.revoked and kb.kind == "hardware")
        else:
            self._set("keybox", "none (Tricky Store uses its built-in AOSP keybox – BASIC/DEVICE only)")
        self._set("tee", st.tee_status or "—")
        self._set("boot", f"verifiedbootstate={p.get('vbs', '')} · flash.locked={p.get('locked', '')} · "
                          f"vbmeta={p.get('vbmeta', '')}")
        self.b_check.setText(" Check verdict" if p.get("checker") else " Install checker")
        zyg_needed = not m.magisk_family and not any(
            st.module(x) and not st.module(x).removing for x in E.CONFLICTS["zygisk"])
        self.l_zyg.setVisible(zyg_needed)
        self.c_zyg.setVisible(zyg_needed)
        for combo, kind in ((self.c_tricky, "tricky"), (self.c_pif, "pif")):
            k, mod = st.installed(kind)
            if k and k != "?":
                combo.setCurrentIndex(max(0, combo.findData(k)))

    # ---- install --------------------------------------------------------------------------
    def do_install(self):
        if not self.st:
            return
        keys = [k for k in (self.c_tricky.currentData(), self.c_pif.currentData()) if k]
        if self.c_zyg.isVisible() and self.c_zyg.currentData():
            keys.insert(0, self.c_zyg.currentData())
        if self.chk_hide.isChecked():
            keys.append("zygisk_assistant")
        if not keys:
            return
        st = self.st
        conflicts = []
        if self.chk_conf.isChecked():
            for k in keys:
                c = E.COMPONENTS[k]
                conflicts += [f"{mod.name} ({mod.id})" for mod in st.modules
                              if mod.id in E.CONFLICTS[c.kind] and mod.id != c.module_id and not mod.removing]
        text = ("Download from GitHub and install with " + st.mgr.name + ":\n\n" +
                "\n".join("•  " + E.COMPONENTS[k].name for k in keys))
        if conflicts:
            text += "\n\nRemove at next reboot:\n" + "\n".join("•  " + c for c in conflicts)
        if st.mgr.magisk_family and st.props.get("_zygisk_setting") != 1:
            text += "\n\nMagisk's Zygisk will be switched on."
        text += "\n\nModules run as root at every boot. A reboot is needed afterwards."
        if not self.ask("Install", text):
            return
        remove = self.chk_conf.isChecked()

        def work():
            E.install(self.conn, st, keys, remove, self.cache, self.say)
            # make sure the essentials are targeted
            if any(E.COMPONENTS[k].kind == "tricky" for k in keys):
                if keys and "teesim" in keys:
                    E.write_teesim_config(self.conn, st.teesim_cfg, None, None)
                else:
                    new = E.merge_targets(st.targets, E.CORE_TARGETS)
                    if new != [x for x in st.targets if x.strip()]:
                        E.write_tricky_config(self.conn, new, None)
                        self.say(f"target.txt: added {len(new) - len([x for x in st.targets if x.strip()])} "
                                 "Google packages\n")
                    if not st.security_patch:
                        E.write_tricky_config(self.conn, None, E.default_patch_date())
                        self.say(f"security_patch.txt: {E.default_patch_date()}\n")
            self.say("\nDone. Reboot the phone, then press 'Generate fingerprint' and 'Check verdict'.\n")

        def done(_):
            if self.ask("Reboot", "Installed. Reboot the phone now?"):
                self.reboot(confirm=False)
        self.run("Install", work, done)

    # ---- configure ----------------------------------------------------------------------
    def _teesim(self):
        return self.st and self.st.installed("tricky")[0] == "teesim"

    def add_targets(self, all_user):
        st = self.st
        if not st:
            return

        def work():
            add = list(E.CORE_TARGETS)
            if all_user:
                add += E.user_packages(self.conn)
            if self._teesim():
                cur = st.teesim_cfg.get("profiles", {}).get("default", {}).get("apps", []) if st.teesim_cfg else []
                new = E.merge_targets(cur, add)
                E.write_teesim_config(self.conn, st.teesim_cfg, new, None)
                self.say(f"TEESimulator apps: {len(cur)} → {len(new)}\n")
            else:
                cur = [x for x in st.targets if x.strip()]
                new = E.merge_targets(cur, add)
                E.write_tricky_config(self.conn, new, None)
                self.say(f"target.txt: {len(cur)} → {len(new)} lines (old file kept as target.txt.bak)\n")
        self.run("Target apps", work)

    def edit_targets(self):
        st = self.st
        if not st:
            return
        if self._teesim():
            cur = st.teesim_cfg.get("profiles", {}).get("default", {}).get("apps", []) if st.teesim_cfg else []
        else:
            cur = st.targets
        d = QDialog(self)
        d.setWindowTitle("Target apps")
        d.resize(520, 600)
        v = QVBoxLayout(d)
        v.addWidget(QLabel("One package per line. Tricky Store suffixes: none = automatic, "
                           "? = leaf hack, ! = generate certificate."))
        ed = QPlainTextEdit("\n".join(cur))
        v.addWidget(ed)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        v.addWidget(bb)
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        lines = [x.strip() for x in ed.toPlainText().splitlines() if x.strip()]

        def work():
            if self._teesim():
                E.write_teesim_config(self.conn, st.teesim_cfg, lines, None)
            else:
                E.write_tricky_config(self.conn, lines, None)
            self.say(f"Saved {len(lines)} lines.\n")
        self.run("Target apps", work)

    def apply_patch(self):
        mode = self.c_patch.currentData()
        if mode == "recent":
            val = E.default_patch_date()
        elif mode == "device":
            val = ""
        else:
            val, ok = QInputDialog.getText(self, "Patch level", "Date (YYYY-MM-DD):", text=E.default_patch_date())
            if not ok or not __import__("re").fullmatch(r"\d{4}-\d{2}-\d{2}", val.strip()):
                return
            val = val.strip()

        def work():
            if self._teesim():
                E.write_teesim_config(self.conn, self.st.teesim_cfg, None, val)
            else:
                E.write_tricky_config(self.conn, None, val)
            self.say(f"Patch level spoof: {val or 'off (real values)'}\n")
        self.run("Patch level", work)

    def import_keybox(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import keybox", "", "Keybox (*.xml);;All files (*)")
        if not path:
            return
        with open(path, "rb") as f:
            data = f.read()
        try:
            info = E.parse_keybox(data)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Keybox", f"This is not a valid keybox:\n{e}")
            return
        teesim = self._teesim()

        def work():
            try:
                E.check_revocation(info, self.cache)
            except Exception as e:  # noqa: BLE001
                self.say(f"(revocation list not available: {e})\n")
            return info

        def checked(info):
            text = info.summary()
            if info.revoked:
                text += "\n\nGoogle has revoked this keybox; STRONG integrity will fail with it."
            if not self.ask("Import keybox", text + "\n\nInstall it on the phone?"):
                return

            def put():
                p = E.install_keybox(self.conn, data, teesim)
                self.say(f"Keybox written to {p} (previous one backed up).\n")
            self.run("Keybox", put)
        self.run("Keybox check", work, checked, refresh=False)

    # ---- actions ----------------------------------------------------------------------------
    def gen_fp(self):
        if not self.st:
            return
        try:
            cmd = E.autopif_command(self.st)
        except Exception as e:  # noqa: BLE001
            QMessageBox.information(self, "Fingerprint", str(e))
            return
        self.shell(cmd + "; rc=$?; " + E.KILL_PI + "; exit $rc", "Generate fingerprint", timeout=240)

    def check(self):
        if not self.st:
            return
        if not self.st.props.get("checker"):
            if self.ask("Install checker", "Install Simple Play Integrity Checker (open source, "
                                           "github.com/herzhenr/spic-android) on the phone?"):
                self.run("Install checker", lambda: E.install_checker(self.conn, self.cache, self.say))
            return

        def done(res):
            v = res["verdicts"]
            short = ", ".join(x.replace("MEETS_", "").replace("_INTEGRITY", "") for x in v) or "NONE"
            self._set("verdict", short, bad="MEETS_DEVICE_INTEGRITY" not in v, good="MEETS_DEVICE_INTEGRITY" in v)
            self.say(f"Verdict: {short}\n")
        self.run("Check verdict", lambda: E.run_checker(self.conn, self.say), done, refresh=False)

    def start_daemon(self):
        if self.st:
            self.run("Start daemon", lambda: self.say(E.start_daemon(self.conn, self.st) + "\n"))

    def reboot(self, confirm=True):
        if confirm and not self.ask("Reboot", "Reboot the phone now?"):
            return

        def work():
            try:
                E.sh(self.conn, "(sleep 1; svc power reboot || reboot) >/dev/null 2>&1 &", timeout=15)
            except Exception:  # noqa: BLE001 – the connection drops while rebooting
                pass
            self.say("Rebooting… reconnect when the phone is back, then reopen this window.\n")
        self.run("Reboot", work, refresh=False)
