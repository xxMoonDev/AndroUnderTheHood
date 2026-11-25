"""Activity inspector / launcher (like the "Activity Launcher" app, on the PC)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import backend as B
from . import theme
from .apps import HELPER_RUN, ensure_helper

log = logging.getLogger("androunderthehood")
q = B.shq

LAUNCH_MODES = {0: "standard", 1: "singleTop", 2: "singleTask", 3: "singleInstance", 4: "singleInstancePerTask"}


@dataclass
class Activity:
    pkg: str
    cls: str
    exported: bool
    enabled: bool
    launcher: bool
    own_label: bool
    label: str
    permission: str
    launch_mode: int
    task_affinity: str
    filters: list = field(default_factory=list)   # [{"action": [...], "category": [...], ...}]

    @property
    def component(self) -> str:
        return f"{self.pkg}/{self.cls}"

    @property
    def short(self) -> str:
        return "." + self.cls[len(self.pkg) + 1:] if self.cls.startswith(self.pkg + ".") else self.cls

    @property
    def simple(self) -> str:
        return self.cls.rsplit(".", 1)[-1]


def load_activities(conn: B.Connection, fs: B.FileSystem, pkgs: list[str] | None = None) -> list[Activity]:
    ensure_helper(conn, fs)
    args = " ".join(q(p) for p in pkgs) if pkgs else ""
    rc, out, err = conn.exec(f"{HELPER_RUN} activities {args} 2>/dev/null", timeout=300)
    acts = []
    for line in out.decode("utf-8", errors="replace").splitlines():
        f = line.split("\t")
        if len(f) >= 11 and f[0] == "A":
            acts.append(Activity(f[1], f[2], f[3] == "1", f[4] == "1", f[5] == "1", f[6] == "1", f[7], f[8],
                                 int(f[9]) if f[9].lstrip("-").isdigit() else 0, f[10]))
    if not acts and not pkgs:
        raise B.BackendError(err.decode(errors="replace").strip() or "no activities returned")
    return acts


def load_filters(conn: B.Connection, pkg: str) -> dict[str, list[dict]]:
    """Intent filters per activity class from `dumpsys package <pkg>` (Activity Resolver Table)."""
    rc, out, _ = conn.exec(f"dumpsys package {q(pkg)}", timeout=60)
    text = out.decode("utf-8", errors="replace")
    sec = text.split("Activity Resolver Table:", 1)
    if len(sec) < 2:
        return {}
    body = re.split(r"\n(?:Receiver Resolver Table|Service Resolver Table|Provider Resolver Table|"
                    r"Domain verification|Registered ContentProviders|Preferred Activities)", sec[1], 1)[0]
    result: dict[str, list[dict]] = {}
    seen: set[str] = set()
    cur: dict | None = None
    for line in body.split("\n"):
        m = re.match(r"^\s+[0-9a-f]+ ([^/\s]+)/(\S+) filter ([0-9a-f]+)", line)
        if m:
            p, c, fid = m.groups()
            if c.startswith("."):
                c = p + c
            if fid in seen:
                cur = None
                continue
            seen.add(fid)
            cur = {}
            result.setdefault(c, []).append(cur)
            continue
        m = re.match(r'^\s+(Action|Category|Scheme|Authority|Path|Type|StaticType|SchemeSpecificPart): "?(.*?)"?$',
                     line)
        if m and cur is not None:
            cur.setdefault(m.group(1).lower(), []).append(m.group(2))
    return result


def am_start(component: str | None, action="", data="", mime="", categories=(), extras=(), flags=0,
             force_stop=False, wait=True, user="0") -> str:
    parts = ["am", "start"]
    if wait:
        parts.append("-W")
    if force_stop:
        parts.append("-S")
    if user:
        parts += ["--user", user]
    if action:
        parts += ["-a", q(action)]
    if data:
        parts += ["-d", q(data)]
    if mime:
        parts += ["-t", q(mime)]
    for c in categories:
        parts += ["-c", q(c)]
    opt = {"string": "--es", "int": "--ei", "bool": "--ez", "long": "--el", "float": "--ef", "uri": "--eu"}
    for key, typ, val in extras:
        parts += [opt.get(typ, "--es"), q(key), q(val)]
    if flags:
        parts += ["-f", f"0x{flags:08x}"]
    if component:
        parts += ["-n", q(component)]
    return " ".join(parts)


def launch(conn: B.Connection, cmd: str) -> tuple[bool, str]:
    rc, out, err = conn.exec(cmd + " 2>&1", timeout=60)
    text = (out + err).decode(errors="replace").strip()
    ok = rc == 0 and "Error" not in text and "Exception" not in text
    return ok, text


# --------------------------------------------------------------------------
class ActivitiesDialog(QDialog):
    def __init__(self, win, conn: B.Connection, pkg: str | None = None, icons: dict | None = None,
                 labels: dict | None = None):
        super().__init__(win)
        from .ui import Task

        self.Task = Task
        self.win = win
        self.conn = conn
        self.pkg = pkg
        self.icons = icons or {}
        self.app_labels = labels or {}
        self.acts: list[Activity] = []
        self.filters_cache: dict[str, dict] = {}
        self.items: list[tuple[QTreeWidgetItem, Activity]] = []
        name = self.app_labels.get(pkg, pkg) if pkg else "all apps"
        self.setWindowTitle(f"Activities – {name}")
        self.resize(1250, 780)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(10)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search activity, label or package…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.apply_filter)
        top.addWidget(self.search, 1)
        self.kind = QComboBox()
        for label, k in (("All activities", "all"), ("Exported only", "exported"), ("Launcher entries", "launcher"),
                         ("★ Favorites", "fav"), ("Disabled", "disabled")):
            self.kind.addItem(label, k)
        self.kind.currentIndexChanged.connect(self.apply_filter)
        top.addWidget(self.kind)
        self.hide_unlabeled = QCheckBox("Only with own label")
        self.hide_unlabeled.setToolTip("Hide activities that just inherit the app's name")
        self.hide_unlabeled.toggled.connect(self.apply_filter)
        top.addWidget(self.hide_unlabeled)
        self.btn_refresh = QPushButton(theme.line_icon("refresh"), " Refresh")
        self.btn_refresh.setAutoDefault(False)
        self.btn_refresh.clicked.connect(self.load)
        top.addWidget(self.btn_refresh)
        lay.addLayout(top)

        split = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Activity", "Label", ""])
        self.tree.setRootIsDecorated(pkg is None)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setIconSize(QSize(20, 20))
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(0, 420)
        self.tree.setColumnWidth(1, 230)
        self.tree.header().setStretchLastSection(True)
        self.tree.currentItemChanged.connect(lambda *_: self.show_details())
        self.tree.itemDoubleClicked.connect(lambda it, _c: self.launch_current())
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.tree_menu)
        split.addWidget(self.tree)

        det = QWidget()
        det.setObjectName("propsPanel")
        det.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        dl = QVBoxLayout(det)
        dl.setContentsMargins(12, 12, 12, 12)
        self.d_title = QLabel("Select an activity")
        self.d_title.setObjectName("propsTitle")
        self.d_title.setWordWrap(True)
        dl.addWidget(self.d_title)
        self.d_info = QTableWidget(0, 2)
        self.d_info.setObjectName("propsTable")
        self.d_info.horizontalHeader().hide()
        self.d_info.verticalHeader().hide()
        self.d_info.setShowGrid(False)
        self.d_info.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.d_info.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.d_info.horizontalHeader().setStretchLastSection(True)
        self.d_info.setWordWrap(True)
        dl.addWidget(self.d_info, 2)
        fl = QLabel("Intent filters")
        fl.setStyleSheet(f"color: {theme.TEXT_DIM}; font-weight: 600;")
        dl.addWidget(fl)
        self.d_filters = QPlainTextEdit()
        self.d_filters.setReadOnly(True)
        self.d_filters.setFont(QFont("Consolas", 9))
        dl.addWidget(self.d_filters, 2)
        grid = QGridLayout()
        self.b_launch = QPushButton(theme.line_icon("forward", theme.ACCENT_TEXT), " Launch")
        self.b_launch.setStyleSheet(f"QPushButton{{background:{theme.ACCENT};color:{theme.ACCENT_TEXT};"
                                    f"border-color:{theme.ACCENT};font-weight:600;}}"
                                    f"QPushButton:hover{{background:{theme.ACCENT_HOVER};}}")
        self.b_launch.clicked.connect(self.launch_current)
        self.b_with = QPushButton(theme.line_icon("sliders"), " Launch with…")
        self.b_with.clicked.connect(self.launch_with)
        self.b_fav = QPushButton(theme.line_icon("star"), " Favorite")
        self.b_fav.setCheckable(True)
        self.b_fav.clicked.connect(self.toggle_fav)
        self.b_copy = QPushButton(theme.line_icon("copy"), " Copy am command")
        self.b_copy.clicked.connect(self.copy_cmd)
        for i, b in enumerate((self.b_launch, self.b_with, self.b_fav, self.b_copy)):
            b.setAutoDefault(False)
            grid.addWidget(b, i // 2, i % 2)
        dl.addLayout(grid)
        split.addWidget(det)
        split.setSizes([800, 440])
        split.setStretchFactor(0, 1)
        lay.addWidget(split, 1)
        self.status = QLabel("Loading activities…")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(self.status)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.search.setFocus)
        QShortcut(QKeySequence(Qt.Key.Key_F5), self, activated=self.load)
        self.show_details()
        self.load()

    # ---- favorites --------------------------------------------------------
    def favs(self) -> list[dict]:
        key = self.win._device_key()
        return self.win.cfg.data.setdefault("activity_favorites", {}).setdefault(key, [])

    def is_fav(self, a: Activity) -> bool:
        return any(f["component"] == a.component for f in self.favs())

    def toggle_fav(self):
        a = self.current()
        if not a:
            return
        favs = self.favs()
        if self.is_fav(a):
            favs[:] = [f for f in favs if f["component"] != a.component]
        else:
            favs.append({"component": a.component, "label": a.label if a.own_label else a.simple,
                         "app": self.app_labels.get(a.pkg, a.pkg)})
        self.win.cfg.save()
        self.refresh_item(a)
        self.show_details()

    # ---- loading -----------------------------------------------------------
    def load(self):
        conn, fs, pkgs = self.conn, self.win.fs, [self.pkg] if self.pkg else None
        self.btn_refresh.setEnabled(False)
        self.status.setText("Loading activities…")

        def done(acts):
            self.btn_refresh.setEnabled(True)
            self.acts = acts
            self.fill()

        def fail(m):
            self.btn_refresh.setEnabled(True)
            self.status.setText(f"Could not list activities: {m}")
        self.Task.start(lambda: load_activities(conn, fs, pkgs), done, fail)

    def fill(self):
        t = self.tree
        t.setUpdatesEnabled(False)
        t.clear()
        self.items = []
        by_pkg: dict[str, list[Activity]] = {}
        for a in self.acts:
            by_pkg.setdefault(a.pkg, []).append(a)
        dim = QColor(theme.TEXT_FAINT)
        for pkg in sorted(by_pkg, key=lambda p: (self.app_labels.get(p, p).lower(), p)):
            acts = sorted(by_pkg[pkg], key=lambda a: (not a.launcher, not a.exported, a.cls.lower()))
            if self.pkg:
                parent = t.invisibleRootItem()
            else:
                parent = QTreeWidgetItem([self.app_labels.get(pkg, pkg), pkg, f"{len(acts)}"])
                parent.setIcon(0, self.icons.get(pkg, theme.line_icon("package", theme.TEXT_FAINT)))
                f = parent.font(0)
                f.setBold(True)
                parent.setFont(0, f)
                parent.setForeground(1, dim)
                parent.setForeground(2, dim)
                t.addTopLevelItem(parent)
            for a in acts:
                it = QTreeWidgetItem()
                self.decorate(it, a)
                parent.addChild(it) if not self.pkg else t.addTopLevelItem(it)
                self.items.append((it, a))
        t.setUpdatesEnabled(True)
        if self.pkg and self.items:
            t.setCurrentItem(self.items[0][0])
        self.apply_filter()

    def decorate(self, it: QTreeWidgetItem, a: Activity):
        flags = []
        if self.is_fav(a):
            flags.append("★")
        if a.launcher:
            flags.append("launcher")
        flags.append("exported" if a.exported else "private")
        if not a.enabled:
            flags.append("disabled")
        if a.permission:
            flags.append("needs permission")
        it.setText(0, a.short)
        it.setText(1, a.label if a.own_label else "")
        it.setText(2, " · ".join(flags))
        it.setToolTip(0, a.component)
        it.setIcon(0, theme.line_icon("star", theme.ACCENT) if self.is_fav(a) else
                   theme.line_icon("forward", theme.ACCENT if a.launcher else
                                   (theme.TEXT if a.exported else theme.TEXT_FAINT)))
        col = QColor(theme.TEXT if (a.exported and a.enabled) else theme.TEXT_FAINT)
        it.setForeground(0, col)
        it.setForeground(2, QColor(theme.TEXT_DIM))
        it.setData(0, Qt.ItemDataRole.UserRole, a)

    def refresh_item(self, a: Activity):
        for it, x in self.items:
            if x is a:
                self.decorate(it, a)

    def apply_filter(self, *_):
        words = self.search.text().lower().split()
        kind = self.kind.currentData()
        own = self.hide_unlabeled.isChecked()
        shown = 0
        visible_pkgs = set()
        favs = {f["component"] for f in self.favs()}
        for it, a in self.items:
            ok = True
            if kind == "exported" and not a.exported:
                ok = False
            elif kind == "launcher" and not a.launcher:
                ok = False
            elif kind == "fav" and a.component not in favs:
                ok = False
            elif kind == "disabled" and a.enabled:
                ok = False
            if ok and own and not a.own_label and not a.launcher:
                ok = False
            if ok and words:
                hay = f"{a.cls} {a.label} {a.pkg} {self.app_labels.get(a.pkg, '')}".lower()
                ok = all(w in hay for w in words)
            it.setHidden(not ok)
            if ok:
                shown += 1
                visible_pkgs.add(a.pkg)
        if not self.pkg:
            root = self.tree.invisibleRootItem()
            for i in range(root.childCount()):
                p = root.child(i)
                p.setHidden(p.text(1) not in visible_pkgs)
                if words or kind in ("fav", "launcher"):
                    p.setExpanded(p.text(1) in visible_pkgs and shown < 400)
        exp = sum(1 for _i, a in self.items if a.exported)
        self.status.setText(f"{shown:,} of {len(self.items):,} activities shown · {exp:,} exported · "
                            f"{len({a.pkg for a in self.acts})} app(s)   —   double-click launches · non-exported "
                            "activities are launched as root")

    # ---- details ----------------------------------------------------------
    def current(self) -> Activity | None:
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def show_details(self):
        a = self.current()
        for b in (self.b_launch, self.b_with, self.b_fav, self.b_copy):
            b.setEnabled(a is not None)
        if not a:
            self.d_title.setText("Select an activity")
            self.d_info.setRowCount(0)
            self.d_filters.setPlainText("")
            return
        self.d_title.setText(a.label if a.own_label else a.simple)
        self.b_fav.setChecked(self.is_fav(a))
        self.b_fav.setText(" Favorite ★" if self.is_fav(a) else " Favorite")
        rows = [("Component", a.component), ("App", f"{self.app_labels.get(a.pkg, a.pkg)} ({a.pkg})"),
                ("Exported", "yes" if a.exported else "no – launched as root"),
                ("Enabled", "yes" if a.enabled else "no (disabled)"),
                ("Launcher entry", "yes" if a.launcher else "no"),
                ("Launch mode", LAUNCH_MODES.get(a.launch_mode, str(a.launch_mode))),
                ("Task affinity", a.task_affinity or "–"),
                ("Permission", a.permission or "none")]
        self.d_info.setRowCount(len(rows))
        for i, (k, v) in enumerate(rows):
            ki = QTableWidgetItem(k)
            ki.setForeground(QColor(theme.TEXT_DIM))
            self.d_info.setItem(i, 0, ki)
            vi = QTableWidgetItem(v)
            vi.setToolTip(v)
            self.d_info.setItem(i, 1, vi)
        if a.pkg in self.filters_cache:
            self.show_filters(a)
        else:
            self.d_filters.setPlainText("loading…")
            conn, pkg = self.conn, a.pkg

            def done(res):
                self.filters_cache[pkg] = res
                if self.current() is a:
                    self.show_filters(a)
            self.Task.start(lambda: load_filters(conn, pkg), done,
                            lambda m: self.d_filters.setPlainText(f"could not read filters: {m}"))

    def show_filters(self, a: Activity):
        fl = self.filters_cache.get(a.pkg, {}).get(a.cls, [])
        if not fl:
            self.d_filters.setPlainText("(no intent filters – reachable only by explicit component)")
            return
        out = []
        for i, f in enumerate(fl, 1):
            out.append(f"Filter {i}")
            for k in ("action", "category", "scheme", "authority", "path", "type", "statictype"):
                for v in f.get(k, []):
                    out.append(f"  {k:9} {v}")
        self.d_filters.setPlainText("\n".join(out))

    def tree_menu(self, pos):
        it = self.tree.itemAt(pos)
        if not it:
            return
        self.tree.setCurrentItem(it)
        a = self.current()
        if not a:
            return
        m = QMenu(self)
        m.addAction(theme.line_icon("forward"), "Launch", self.launch_current)
        m.addAction(theme.line_icon("sliders"), "Launch with…", self.launch_with)
        m.addAction(theme.line_icon("star"), "Remove from favorites" if self.is_fav(a) else "Add to favorites",
                    self.toggle_fav)
        m.addSeparator()
        m.addAction(theme.line_icon("copy"), "Copy component", lambda: QApplication.clipboard().setText(a.component))
        m.addAction(theme.line_icon("copy"), "Copy am command", self.copy_cmd)
        m.exec(self.tree.viewport().mapToGlobal(pos))

    # ---- launching --------------------------------------------------------
    def run_launch(self, cmd: str, what: str):
        conn = self.conn
        self.status.setText(f"Launching {what}…")

        def done(r):
            ok, text = r
            if ok:
                m = re.search(r"TotalTime: (\d+)", text)
                self.status.setText(f"Launched {what}" + (f" in {m.group(1)} ms" if m else "") + ".")
                self.win.statusBar().showMessage(f"Launched {what}", 5000)
            else:
                self.status.setText(f"Launch failed: {what}")
                QMessageBox.warning(self, "Launch failed", f"{cmd}\n\n{text}")
        self.Task.start(lambda: launch(conn, cmd), done, lambda m: QMessageBox.warning(self, "Launch", m))

    def launch_current(self):
        a = self.current()
        if a:
            self.run_launch(am_start(a.component), a.short)

    def copy_cmd(self):
        a = self.current()
        if a:
            QApplication.clipboard().setText(am_start(a.component, wait=False))
            self.status.setText("am command copied.")

    def launch_with(self):
        a = self.current()
        if not a:
            return
        dlg = LaunchWithDialog(self, a, self.filters_cache.get(a.pkg, {}).get(a.cls, []))
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.run_launch(dlg.command(), a.short)


FLAG_CHOICES = [("NEW_TASK", 0x10000000), ("CLEAR_TOP", 0x04000000), ("CLEAR_TASK", 0x00008000),
                ("SINGLE_TOP", 0x20000000), ("NO_HISTORY", 0x40000000), ("MULTIPLE_TASK", 0x08000000),
                ("EXCLUDE_FROM_RECENTS", 0x00800000), ("NO_ANIMATION", 0x00010000)]


class LaunchWithDialog(QDialog):
    """Build an explicit (or implicit) intent like Activity Launcher's advanced mode."""

    def __init__(self, parent, a: Activity, filters: list[dict]):
        super().__init__(parent)
        self.a = a
        self.setWindowTitle(f"Launch {a.short} with…")
        self.setMinimumWidth(720)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.component = QLineEdit(a.component)
        self.explicit = QCheckBox("Explicit component (uncheck for an implicit intent resolved by Android)")
        self.explicit.setChecked(True)
        self.action = QComboBox()
        self.action.setEditable(True)
        acts = sorted({x for f in filters for x in f.get("action", [])})
        self.action.addItems([""] + acts + [x for x in ("android.intent.action.VIEW", "android.intent.action.SEND",
                                                         "android.intent.action.MAIN") if x not in acts])
        self.data = QLineEdit()
        self.data.setPlaceholderText("e.g. https://example.com  ·  content://…  ·  tel:123")
        self.mime = QLineEdit()
        self.mime.setPlaceholderText("e.g. text/plain")
        self.cats = QLineEdit(", ".join(sorted({x for f in filters for x in f.get("category", [])
                                                if x != "android.intent.category.LAUNCHER"})))
        self.cats.setPlaceholderText("comma separated, e.g. android.intent.category.DEFAULT")
        form.addRow("Component", self.component)
        form.addRow("", self.explicit)
        form.addRow("Action", self.action)
        form.addRow("Data URI", self.data)
        form.addRow("MIME type", self.mime)
        form.addRow("Categories", self.cats)
        lay.addLayout(form)

        lay.addWidget(QLabel("Extras"))
        self.extras = QTableWidget(0, 3)
        self.extras.setHorizontalHeaderLabels(["Key", "Type", "Value"])
        self.extras.verticalHeader().hide()
        self.extras.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.extras.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.extras.setColumnWidth(1, 110)
        self.extras.setMaximumHeight(150)
        lay.addWidget(self.extras)
        eb = QHBoxLayout()
        add = QPushButton(theme.line_icon("plus"), " Add extra")
        add.clicked.connect(self.add_extra)
        rm = QPushButton(theme.line_icon("trash"), " Remove")
        rm.clicked.connect(lambda: self.extras.removeRow(self.extras.currentRow()))
        for b in (add, rm):
            b.setAutoDefault(False)
            eb.addWidget(b)
        eb.addStretch(1)
        lay.addLayout(eb)

        lay.addWidget(QLabel("Flags"))
        fg = QGridLayout()
        self.flag_boxes = []
        for i, (name, val) in enumerate(FLAG_CHOICES):
            cb = QCheckBox(name)
            cb.setChecked(name == "NEW_TASK")
            cb.toggled.connect(self.update_preview)
            self.flag_boxes.append((cb, val))
            fg.addWidget(cb, i // 4, i % 4)
        lay.addLayout(fg)
        opts = QHBoxLayout()
        self.force_stop = QCheckBox("Force-stop the app first (-S)")
        self.wait = QCheckBox("Wait and report launch time (-W)")
        self.wait.setChecked(True)
        opts.addWidget(self.force_stop)
        opts.addWidget(self.wait)
        opts.addStretch(1)
        lay.addLayout(opts)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont("Consolas", 9))
        self.preview.setMaximumHeight(70)
        lay.addWidget(self.preview)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Launch")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        for w in (self.component, self.data, self.mime, self.cats):
            w.textChanged.connect(self.update_preview)
        self.action.currentTextChanged.connect(self.update_preview)
        for w in (self.explicit, self.force_stop, self.wait):
            w.toggled.connect(self.update_preview)
        self.extras.itemChanged.connect(self.update_preview)
        self.update_preview()

    def add_extra(self):
        r = self.extras.rowCount()
        self.extras.insertRow(r)
        self.extras.setItem(r, 0, QTableWidgetItem("key"))
        cb = QComboBox()
        cb.addItems(["string", "int", "bool", "long", "float", "uri"])
        cb.currentTextChanged.connect(self.update_preview)
        self.extras.setCellWidget(r, 1, cb)
        self.extras.setItem(r, 2, QTableWidgetItem(""))
        self.update_preview()

    def command(self) -> str:
        extras = []
        for r in range(self.extras.rowCount()):
            k = self.extras.item(r, 0)
            v = self.extras.item(r, 2)
            t = self.extras.cellWidget(r, 1)
            if k and k.text().strip():
                extras.append((k.text().strip(), t.currentText() if t else "string", v.text() if v else ""))
        flags = 0
        for cb, val in self.flag_boxes:
            if cb.isChecked():
                flags |= val
        cats = [c.strip() for c in self.cats.text().split(",") if c.strip()]
        return am_start(self.component.text().strip() if self.explicit.isChecked() else None,
                        action=self.action.currentText().strip(), data=self.data.text().strip(),
                        mime=self.mime.text().strip(), categories=cats, extras=extras, flags=flags,
                        force_stop=self.force_stop.isChecked(), wait=self.wait.isChecked())

    def update_preview(self, *_):
        self.preview.setPlainText(self.command())


def favorites_menu(win, parent, menu: QMenu):
    """Fill a menu with the device's favorite activities (one click launches)."""
    menu.clear()
    favs = win.cfg.data.get("activity_favorites", {}).get(win._device_key(), [])
    if not favs:
        a = menu.addAction("No favorites yet – star activities in the Activities window")
        a.setEnabled(False)
        return
    for f in favs:
        menu.addAction(theme.line_icon("star", theme.ACCENT), f"{f['app']} › {f['label']}",
                       lambda comp=f["component"], lab=f["label"]: _quick_launch(win, parent, comp, lab))


def _quick_launch(win, parent, component: str, label: str):
    from .ui import Task
    conn = win.conn

    def done(r):
        ok, text = r
        if ok:
            win.statusBar().showMessage(f"Launched {label}", 5000)
        else:
            QMessageBox.warning(parent, "Launch failed", f"{component}\n\n{text}")
    Task.start(lambda: launch(conn, am_start(component)), done, lambda m: QMessageBox.warning(parent, "Launch", m))
