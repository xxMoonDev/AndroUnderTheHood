"""App manager: list, back up, uninstall and install Android packages."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import logging
import os
import posixpath
import re
import shutil
import sys
import tempfile
import urllib.parse
import zipfile
from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSize, QSortFilterProxyModel, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QTableView,
                               QVBoxLayout)

from . import backend as B
from . import theme

log = logging.getLogger("androunderthehood")

HELPER_DEVICE_PATH = "/data/local/tmp/androunderthehood-helper.jar"
HELPER_RUN = f"CLASSPATH={HELPER_DEVICE_PATH} app_process / AndroUnderTheHoodHelper"
INSTALL_TMP = "/data/local/tmp/androunderthehood-install"
ICON_SIZE = 96
PACKAGE_EXTS = (".apk", ".apks", ".apkm", ".xapk")


def helper_local_path() -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(base, "androunderthehood", "assets", "androunderthehood-helper.jar"),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "androunderthehood-helper.jar")):
        if os.path.isfile(p):
            return p
    raise FileNotFoundError("androunderthehood-helper.jar is missing from the installation")


def human(n: int | None) -> str:
    if not n:
        return ""
    f = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if f < 1024 or u == "GB":
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return str(n)


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
@dataclass
class App:
    pkg: str
    label: str = ""
    version: str = ""
    version_code: str = ""
    code_path: str = ""
    flags: set = field(default_factory=set)
    installed: bool = True
    enabled_state: int = 0      # 0 default, 1 enabled, 2 disabled, 3 disabled-user, 4 disabled-until-used
    installer: str = ""
    first_install: str = ""
    last_update: str = ""
    splits: list = field(default_factory=list)
    uid: str = ""
    app_size: int = 0
    data_size: int = 0
    cache_size: int = 0

    @property
    def system(self) -> bool:
        return "SYSTEM" in self.flags

    @property
    def updated_system(self) -> bool:
        return "UPDATED_SYSTEM_APP" in self.flags

    @property
    def disabled(self) -> bool:
        return self.enabled_state in (2, 3, 4)

    @property
    def kind(self) -> str:
        if self.updated_system:
            return "System (updated)"
        return "System" if self.system else "User"

    @property
    def state(self) -> str:
        if not self.installed:
            return "Not installed"
        return "Disabled" if self.disabled else "Enabled"

    @property
    def name(self) -> str:
        return self.label or self.pkg

    @property
    def data_dir(self) -> str:
        return f"/data/data/{self.pkg}"


def parse_dumpsys(text: str) -> dict[str, App]:
    apps: dict[str, App] = {}
    section = text.split("\nPackages:\n", 1)[-1]
    section = section.split("\nHidden system packages:", 1)[0]
    cur: App | None = None
    for line in section.split("\n"):
        m = re.match(r"^  Package \[([^\]]+)\]", line)
        if m:
            cur = App(m.group(1))
            apps[cur.pkg] = cur
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("versionName="):
            cur.version = s.split("=", 1)[1]
        elif s.startswith("versionCode="):
            cur.version_code = s.split("=", 1)[1].split()[0]
        elif s.startswith("codePath="):
            cur.code_path = s.split("=", 1)[1]
        elif s.startswith("pkgFlags=[") or (s.startswith("flags=[") and not cur.flags):
            cur.flags = set(s.split("[", 1)[1].rstrip("]").split())
        elif s.startswith("userId="):
            cur.uid = s.split("=", 1)[1]
        elif s.startswith("installerPackageName="):
            cur.installer = s.split("=", 1)[1]
        elif s.startswith("firstInstallTime="):
            cur.first_install = s.split("=", 1)[1]
        elif s.startswith("lastUpdateTime="):
            cur.last_update = s.split("=", 1)[1]
        elif s.startswith("timeStamp=") and not cur.first_install:
            cur.first_install = s.split("=", 1)[1]
        elif s.startswith("splits=["):
            cur.splits = [x.strip() for x in s[8:].rstrip("]").split(",") if x.strip()]
        elif s.startswith("User 0:"):
            mi = re.search(r"\binstalled=(\w+)", s)
            me = re.search(r"\benabled=(\d+)", s)
            if mi:
                cur.installed = mi.group(1) == "true"
            if me:
                cur.enabled_state = int(me.group(1))
    return apps


def parse_diskstats(text: str, apps: dict[str, App]):
    def arr(name):
        m = re.search(rf"^{name}: \[(.*)\]\s*$", text, re.M)
        if not m:
            return []
        body = m.group(1)
        if name == "Package Names":
            return re.findall(r'"([^"]*)"', body)
        return [int(x) if x.strip().lstrip("-").isdigit() else 0 for x in body.split(",")]

    names = arr("Package Names")
    for key, attr in (("App Sizes", "app_size"), ("App Data Sizes", "data_size"), ("Cache Sizes", "cache_size")):
        vals = arr(key)
        for n, v in zip(names, vals):
            if n in apps:
                setattr(apps[n], attr, v)


def ensure_helper(conn: B.Connection, fs: B.FileSystem):
    """Push the helper jar to the device if missing or outdated."""
    local = helper_local_path()
    size = os.path.getsize(local)
    rc, out, _ = conn.exec(f"stat -c %s {HELPER_DEVICE_PATH} 2>/dev/null", timeout=20)
    if out.strip() != str(size).encode():
        # an outdated copy owned by another user (root vs shell) can't be overwritten; delete it first
        p = HELPER_DEVICE_PATH
        for as_root in (False, True):
            try:
                conn.exec(f"[ -e {p} ] && [ ! -O {p} ] && rm -f {p}; true", timeout=20, as_root=as_root)
            except Exception:  # noqa: BLE001
                pass
        fs.upload(local, HELPER_DEVICE_PATH, lambda n: None)


def load_apps(conn: B.Connection, fs: B.FileSystem) -> list[App]:
    ensure_helper(conn, fs)
    rc, out, err = conn.exec(
        "dumpsys package packages; echo '//DISK//'; "
        "dumpsys diskstats | grep -E '^(Package Names|App Sizes|App Data Sizes|Cache Sizes):'; "
        f"echo '//LABELS//'; {HELPER_RUN} labels 2>/dev/null", timeout=120)
    text = out.decode("utf-8", errors="replace")
    pk, _, rest = text.partition("//DISK//")
    disk, _, labels = rest.partition("//LABELS//")
    apps = parse_dumpsys(pk)
    if not apps:
        raise B.BackendError((err or out[:300]).decode(errors="replace").strip() or "dumpsys returned nothing")
    parse_diskstats(disk, apps)
    for line in labels.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == "L" and parts[1] in apps:
            apps[parts[1]].label = parts[2]
    return sorted(apps.values(), key=lambda a: a.name.lower())


def measure_missing_sizes(conn: B.Connection, apps: list[App]) -> dict[str, tuple[int, int]]:
    """diskstats is a periodic snapshot (and reports 0 for many system apps):
    measure the remaining apps with du, in batches. Returns {pkg: (apk bytes, data bytes)}."""
    # also re-measure system apps: for apps that live in the system image, diskstats only
    # counts part of the code (e.g. Settings: 146 KB instead of 150 MB)
    missing = [a for a in apps if a.installed and a.code_path
               and (not a.app_size or (a.system and not a.updated_system))]
    res: dict[str, tuple[int, int]] = {}
    for i in range(0, len(missing), 40):
        chunk = missing[i:i + 40]
        # APK size = sum of file sizes (du counts blocks, which is misleading on compressed
        # EROFS system partitions); data size = du of the data dir
        script = "; ".join(
            f"echo \"{a.pkg} $(find {B.shq(a.code_path)} -type f -exec stat -c %s {{}} + 2>/dev/null "
            f"| awk '{{s+=$1}} END {{print int(s/1024)}}') "
            f"$(du -sk {B.shq(a.data_dir)} 2>/dev/null | cut -f1)\"" for a in chunk)
        rc, out, _ = conn.exec(script, timeout=180)
        for line in out.decode(errors="replace").splitlines():
            p = line.split()
            if len(p) >= 2 and p[1].isdigit():
                res[p[0]] = (int(p[1]) * 1024, int(p[2]) * 1024 if len(p) >= 3 and p[2].isdigit() else 0)
    return res


class IconCache:
    def __init__(self):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        self.dir = os.path.join(base, "AndroUnderTheHood", "appicons")
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, host: str, a: App) -> str:
        k = hashlib.sha1(f"{host}|{a.pkg}|{a.version_code}|{a.last_update}|{a.code_path}".encode()).hexdigest()
        return os.path.join(self.dir, k + ".png")

    def get(self, host, a: App) -> bytes | None:
        try:
            with open(self._path(host, a), "rb") as f:
                return f.read()
        except OSError:
            return None

    def put(self, host, a: App, png: bytes):
        try:
            with open(self._path(host, a), "wb") as f:
                f.write(png)
        except OSError:
            pass


def fetch_icons(conn: B.Connection, pkgs: list[str]) -> dict[str, bytes]:
    out_icons: dict[str, bytes] = {}
    for i in range(0, len(pkgs), 80):
        chunk = pkgs[i:i + 80]
        rc, out, _ = conn.exec(f"{HELPER_RUN} icons {ICON_SIZE} {' '.join(B.shq(p) for p in chunk)} 2>/dev/null",
                               timeout=120)
        for line in out.decode(errors="replace").splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0] == "I":
                try:
                    out_icons[parts[1]] = base64.b64decode(parts[2])
                except ValueError:
                    pass
    return out_icons


# --------------------------------------------------------------------------
# Installing
# --------------------------------------------------------------------------
ABIS = ("arm64_v8a", "armeabi_v7a", "armeabi", "x86_64", "x86", "mips64", "mips")
DENSITIES = {"ldpi": 120, "mdpi": 160, "tvdpi": 213, "hdpi": 240, "xhdpi": 320, "xxhdpi": 480, "xxxhdpi": 640}


def _split_config(name: str) -> str | None:
    """'split_config.arm64_v8a.apk' / 'config.xxhdpi.apk' -> 'arm64_v8a' / 'xxhdpi'."""
    base = posixpath.basename(name)[:-4].lower()
    m = re.search(r"(?:^|[._])config\.(.+)$", base)
    return m.group(1) if m else None


def choose_splits(names: list[str], abilist: list[str], density: int) -> list[str]:
    """Keep base/feature/language splits; only the best ABI split and the best density split."""
    abis = [a.replace("-", "_") for a in abilist]
    abi_splits = {n: _split_config(n) for n in names if _split_config(n) in ABIS}
    dens_splits = {n: _split_config(n) for n in names if _split_config(n) in DENSITIES}
    keep = [n for n in names if n not in abi_splits and n not in dens_splits]
    for a in abis:  # device preference order
        hit = [n for n, c in abi_splits.items() if c == a]
        if hit:
            keep += hit[:1]
            break
    if dens_splits:
        best = min(dens_splits, key=lambda n: (DENSITIES[dens_splits[n]] < density,
                                               abs(DENSITIES[dens_splits[n]] - density)))
        keep.append(best)
    return keep


def unpack_package(path: str, workdir: str) -> tuple[list[str], list[tuple[str, str]], str]:
    """Return (local apk files, [(local obb file, device path)], description)."""
    low = path.lower()
    if low.endswith(".apk"):
        return [path], [], "APK"
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise B.BackendError(f"{os.path.basename(path)} is not a valid {os.path.splitext(path)[1]} (zip) file")
    names = [n for n in z.namelist() if n.lower().endswith(".apk")]
    if not names:
        raise B.BackendError("No APK files found inside the archive")
    for info in z.infolist():
        if info.flag_bits & 0x1:
            raise B.BackendError("This archive is encrypted (old APKMirror .apkm format). "
                                 "Re-download it, or install it with the APKMirror Installer.")
    files = []
    for n in names:
        dst = os.path.join(workdir, re.sub(r"[^A-Za-z0-9._-]", "_", posixpath.basename(n)))
        with z.open(n) as src, open(dst, "wb") as out:
            shutil.copyfileobj(src, out)
        files.append(dst)
    obbs = []
    for n in z.namelist():
        if n.lower().endswith(".obb") and "android/obb/" in n.lower():
            rel = n[n.lower().index("android/obb/"):]
            dst = os.path.join(workdir, re.sub(r"[^A-Za-z0-9._-]", "_", posixpath.basename(n)))
            with z.open(n) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
            obbs.append((dst, "/sdcard/" + rel))
    kind = {".apks": "split APKs (.apks)", ".apkm": "APKMirror bundle (.apkm)",
            ".xapk": "XAPK"}.get(os.path.splitext(low)[1], "bundle")
    return files, obbs, kind


def install_package(conn: B.Connection, fs: B.FileSystem, path: str, progress) -> str:
    """Install .apk/.apks/.apkm/.xapk through a pm install session. Returns pm's message."""
    work = tempfile.mkdtemp(prefix="androunderthehood-install-")
    try:
        files, obbs, kind = unpack_package(path, work)
        if len(files) > 1:
            rc, out, _ = conn.exec("getprop ro.product.cpu.abilist; getprop ro.sf.lcd_density", timeout=20)
            lines = out.decode().splitlines() + ["", ""]
            abilist = [a for a in lines[0].split(",") if a]
            density = int(lines[1]) if lines[1].strip().isdigit() else 480
            names = [os.path.basename(f) for f in files]
            chosen = set(choose_splits(names, abilist, density))
            files = [f for f in files if os.path.basename(f) in chosen]
        q = B.shq
        conn.exec(f"rm -rf {q(INSTALL_TMP)}; mkdir -p {q(INSTALL_TMP)}", timeout=30)
        remote = []
        for f in files:
            rp = f"{INSTALL_TMP}/{os.path.basename(f)}"
            progress(0, f"Copying {os.path.basename(f)}")
            fs.upload(f, rp, progress)
            remote.append((rp, os.path.getsize(f)))
        total = sum(s for _, s in remote)
        progress(0, f"Installing {len(remote)} APK(s)…")
        rc, out, err = conn.exec(f"pm install-create -r -S {total}", timeout=60)
        m = re.search(rb"\[(\d+)\]", out)
        if not m:
            raise B.BackendError("pm install-create failed: " + (out + err).decode(errors="replace").strip())
        sid = m.group(1).decode()
        for i, (rp, size) in enumerate(remote):
            rc, out, err = conn.exec(f"pm install-write -S {size} {sid} {i}_{q(posixpath.basename(rp))} {q(rp)}",
                                     timeout=300)
            if rc != 0 or b"Success" not in out:
                conn.exec(f"pm install-abandon {sid}", timeout=30)
                raise B.BackendError("pm install-write failed: " + (out + err).decode(errors="replace").strip())
        rc, out, err = conn.exec(f"pm install-commit {sid}", timeout=600)
        msg = (out + err).decode(errors="replace").strip()
        if "Success" not in msg:
            raise B.BackendError(msg or f"pm install-commit exit code {rc}")
        for local, dev in obbs:
            progress(0, f"Copying OBB {os.path.basename(local)}")
            conn.exec(f"mkdir -p {q(posixpath.dirname(dev))}", timeout=30)
            fs.upload(local, dev, progress)
        return f"{kind}: installed {len(remote)} APK(s)" + (f" + {len(obbs)} OBB file(s)" if obbs else "")
    finally:
        try:
            conn.exec(f"rm -rf {B.shq(INSTALL_TMP)}", timeout=30)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
COLS = ["App", "Package", "Version", "Type", "State", "Size", "Updated"]


class AppModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.apps: list[App] = []
        self.icons: dict[str, QIcon] = {}
        self.placeholder = theme.line_icon("package", theme.TEXT_FAINT)

    def set_apps(self, apps):
        self.beginResetModel()
        self.apps = apps
        self.endResetModel()

    def set_icon(self, pkg: str, png: bytes):
        img = QImage()
        if not img.loadFromData(png, "PNG"):
            return
        self.icons[pkg] = QIcon(QPixmap.fromImage(img))
        for i, a in enumerate(self.apps):
            if a.pkg == pkg:
                idx = self.index(i, 0)
                self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])
                break

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.apps)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLS)

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLS[s]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        a = self.apps[index.row()]
        c = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return [a.name, a.pkg, a.version, a.kind, a.state, human(a.app_size + a.data_size),
                    a.last_update[:16]][c]
        if role == Qt.ItemDataRole.UserRole:  # sort key
            return [a.name.lower(), a.pkg, a.version, a.kind, a.state, a.app_size + a.data_size,
                    a.last_update][c]
        if role == Qt.ItemDataRole.UserRole + 1:
            return a
        if role == Qt.ItemDataRole.DecorationRole and c == 0:
            return self.icons.get(a.pkg, self.placeholder)
        if role == Qt.ItemDataRole.ForegroundRole:
            if not a.installed or a.disabled:
                return QColor(theme.TEXT_FAINT)
            if c in (1, 3, 6):
                return QColor(theme.TEXT_DIM)
        if role == Qt.ItemDataRole.TextAlignmentRole and c == 5:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return (f"{a.name}\n{a.pkg}\nVersion {a.version} ({a.version_code})\n{a.kind} · {a.state}\n"
                    f"APK: {human(a.app_size)} · Data: {human(a.data_size)} · Cache: {human(a.cache_size)}\n"
                    f"Installed: {a.first_install}\nUpdated: {a.last_update}\n"
                    f"Installer: {a.installer or '?'}\nUID: {a.uid}\nPath: {a.code_path}"
                    + (f"\nSplits: {', '.join(a.splits)}" if len(a.splits) > 1 else ""))
        return None


class AppFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.kind = "user"
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def set(self, text=None, kind=None):
        if text is not None:
            self.text = text.lower()
        if kind is not None:
            self.kind = kind
        self.invalidateFilter() if hasattr(self, "invalidateFilter") else self.invalidate()

    def filterAcceptsRow(self, row, parent):
        a: App = self.sourceModel().apps[row]
        k = self.kind
        if k == "user" and (a.system or not a.installed):
            return False
        if k == "system" and (not a.system or not a.installed):
            return False
        if k == "updated" and not a.updated_system:
            return False
        if k == "disabled" and not (a.disabled and a.installed):
            return False
        if k == "uninstalled" and a.installed:
            return False
        if not self.text:
            return True
        hay = f"{a.label.lower()} {a.pkg.lower()}"
        return all(w in hay for w in self.text.split())


class AppsDialog(QDialog):
    def __init__(self, win, conn: B.Connection):
        super().__init__(win)
        from .ui import Task

        self.Task = Task
        self.win = win
        self.conn = conn
        self.host = f"{conn.profile.host}:{conn.profile.port}"
        self.icon_cache = IconCache()
        self.setWindowTitle(f"Apps – {conn.profile.name}")
        self.resize(1180, 740)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAcceptDrops(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)
        top = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Search app name or package…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(lambda t: (self.proxy.set(text=t), self.update_count()))
        top.addWidget(self.filter, 1)
        self.kind = QComboBox()
        for label, k in (("User apps", "user"), ("System apps", "system"), ("Updated system apps", "updated"),
                         ("Disabled", "disabled"), ("Not installed (user 0)", "uninstalled"), ("All", "all")):
            self.kind.addItem(label, k)
        self.kind.currentIndexChanged.connect(
            lambda _i: (self.proxy.set(kind=self.kind.currentData()), self.update_count()))
        top.addWidget(self.kind)
        self.btn_refresh = QPushButton(theme.line_icon("refresh"), " Refresh")
        self.btn_refresh.clicked.connect(self.load)
        top.addWidget(self.btn_refresh)
        self.btn_install = QPushButton(theme.line_icon("download", theme.ACCENT_TEXT), " Install…")
        self.btn_install.setObjectName("accent")
        self.btn_install.setStyleSheet(f"QPushButton{{background:{theme.ACCENT};color:{theme.ACCENT_TEXT};"
                                       f"border-color:{theme.ACCENT};font-weight:600;}}"
                                       f"QPushButton:hover{{background:{theme.ACCENT_HOVER};}}")
        self.btn_install.clicked.connect(self.install_dialog)
        top.addWidget(self.btn_install)
        self.btn_acts = QPushButton(theme.line_icon("forward"), " Activities")
        self.btn_acts.setToolTip("Browse and launch the activities of all apps")
        self.btn_acts.clicked.connect(lambda: self.open_activities(None))
        top.insertWidget(top.count() - 1, self.btn_acts)
        self.btn_favs = QPushButton(theme.line_icon("star"), " Favorites")
        self.btn_favs.setToolTip("Launch a favorite activity")
        fav_menu = QMenu(self)
        fav_menu.aboutToShow.connect(lambda: self.fill_favorites(fav_menu))
        self.btn_favs.setMenu(fav_menu)
        top.insertWidget(top.count() - 1, self.btn_favs)
        for b in (self.btn_refresh, self.btn_install, self.btn_acts, self.btn_favs):
            b.setAutoDefault(False)
        lay.addLayout(top)

        self.model = AppModel()
        self.proxy = AppFilter()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        v = self.view = QTableView()
        v.setModel(self.proxy)
        v.setSortingEnabled(True)
        v.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        v.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        v.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        v.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.setAlternatingRowColors(True)
        v.setShowGrid(False)
        v.setWordWrap(False)
        v.setIconSize(QSize(28, 28))
        v.verticalHeader().hide()
        v.verticalHeader().setDefaultSectionSize(38)
        h = v.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h.setStretchLastSection(False)
        for c, w in enumerate((250, 300, 140, 130, 90, 90, 130)):
            v.setColumnWidth(c, w)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        v.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        v.customContextMenuRequested.connect(self.context_menu)
        v.doubleClicked.connect(lambda idx: self.open_data_folder(self.app_at(idx)))
        lay.addWidget(v, 1)

        self.info = QLabel("Loading apps…")
        self.info.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(self.info)

        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.filter.setFocus)
        QShortcut(QKeySequence(Qt.Key.Key_F5), self, activated=self.load)
        QShortcut(QKeySequence.StandardKey.Copy, v, activated=self.copy_names)
        self.load()
        self.filter.setFocus()

    # ---- loading --------------------------------------------------------
    def load(self):
        conn, fs = self.conn, self.win.fs
        self.btn_refresh.setEnabled(False)
        self.info.setText("Loading apps…")

        def done(apps):
            self.btn_refresh.setEnabled(True)
            self.model.set_apps(apps)
            self.update_count()
            self.load_icons(apps)
            self.Task.start(lambda: measure_missing_sizes(conn, apps), sizes_done,
                            lambda m: log.info("measuring app sizes failed: %s", m))

        def sizes_done(sizes):
            if not sizes or self.model.apps is not apps_ref[0]:
                return
            for a in self.model.apps:
                if a.pkg in sizes:
                    apk, data = sizes[a.pkg]
                    a.app_size = apk or a.app_size
                    # diskstats data also covers device-protected storage: prefer it
                    a.data_size = a.data_size or data
            n = len(self.model.apps)
            if n:
                self.model.dataChanged.emit(self.model.index(0, 5), self.model.index(n - 1, 5))
                self.proxy.invalidate()

        apps_ref = [None]

        def loaded(apps):
            apps_ref[0] = apps
            done(apps)

        def fail(msg):
            self.btn_refresh.setEnabled(True)
            self.info.setText(f"Could not list apps: {msg}")
        self.Task.start(lambda: load_apps(conn, fs), loaded, fail)

    def load_icons(self, apps):
        missing = []
        for a in apps:
            png = self.icon_cache.get(self.host, a)
            if png:
                self.model.set_icon(a.pkg, png)
            elif a.installed:
                missing.append(a)
        if not missing:
            return
        conn = self.conn
        by_pkg = {a.pkg: a for a in missing}

        def done(icons):
            for pkg, png in icons.items():
                self.icon_cache.put(self.host, by_pkg[pkg], png)
                self.model.set_icon(pkg, png)
        self.Task.start(lambda: fetch_icons(conn, list(by_pkg)), done,
                        lambda m: log.info("app icons failed: %s", m))

    def update_count(self):
        apps = self.model.apps
        user = sum(1 for a in apps if not a.system and a.installed)
        sysc = sum(1 for a in apps if a.system and a.installed)
        dis = sum(1 for a in apps if a.disabled and a.installed)
        self.info.setText(f"{self.proxy.rowCount()} shown · {user} user apps · {sysc} system apps · "
                          f"{dis} disabled   —   right-click an app for actions · double-click opens its data "
                          "folder · drop .apk/.apks/.apkm/.xapk here to install")

    # ---- helpers ----------------------------------------------------------
    def app_at(self, idx) -> App | None:
        return self.proxy.data(self.proxy.index(idx.row(), 0), Qt.ItemDataRole.UserRole + 1) if idx.isValid() else None

    def selected(self) -> list[App]:
        rows = self.view.selectionModel().selectedRows(0)
        return [a for a in (self.proxy.data(r, Qt.ItemDataRole.UserRole + 1) for r in rows) if a]

    def run(self, title: str, cmds: list[str], refresh=True, ok_text=None):
        conn = self.conn

        def work():
            results = []
            for c in cmds:
                rc, out, err = conn.exec(c, timeout=180)
                results.append((c, rc, (out + err).decode(errors="replace").strip()))
            return results

        def done(results):
            bad = [r for r in results if r[1] != 0 or "Failure" in r[2] or "Exception" in r[2]]
            if bad:
                QMessageBox.warning(self, title, "\n\n".join(f"{c}\n→ {o or f'exit {rc}'}" for c, rc, o in bad))
            elif ok_text:
                self.win.statusBar().showMessage(ok_text, 6000)
            if refresh:
                self.load()
        self.Task.start(work, done, lambda m: QMessageBox.warning(self, title, m))

    def confirm(self, title: str, text: str) -> bool:
        return QMessageBox.warning(self, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    # ---- context menu ---------------------------------------------------
    def context_menu(self, pos):
        idx = self.view.indexAt(pos)
        if idx.isValid() and not self.view.selectionModel().isSelected(idx):
            self.view.selectRow(idx.row())
        sel = self.selected()
        if not sel:
            return
        a = sel[0]
        one = len(sel) == 1
        L = theme.line_icon
        m = QMenu(self)
        if one:
            hdr = m.addAction(f"{a.name}  ·  {a.version}")
            hdr.setEnabled(False)
            m.addSeparator()
            if a.installed and not a.disabled:
                m.addAction(L("forward"), "Launch", lambda: self.launch(a))
            m.addAction(L("list"), "Activities…", lambda: self.open_activities(a.pkg))
            m.addAction(L("lock"), "Permissions…", lambda: self.open_perms(a))
            m.addAction(L("log"), "Logcat for this app", lambda: self.open_logcat(a.pkg))
            if a.installed:
                m.addAction(L("disconnect"), "Force stop", lambda: self.run(
                    "Force stop", [f"am force-stop {B.shq(a.pkg)}"], refresh=False, ok_text=f"Stopped {a.name}"))
            m.addSeparator()
            m.addAction(L("folder-plus"), "Open data folder", lambda: self.open_data_folder(a))
            m.addAction(L("folder-plus"), "Open APK folder", lambda: self.open_folder(a.code_path))
            m.addSeparator()
        m.addAction(L("save"), "Backup APK…" if one else f"Backup {len(sel)} APKs…",
                    lambda: self.backup(sel, with_data=False))
        m.addAction(L("save"), "Backup APK + data…" if one else f"Backup {len(sel)} APKs + data…",
                    lambda: self.backup(sel, with_data=True))
        m.addSeparator()
        if one:
            if a.installed:
                if a.disabled:
                    m.addAction(L("refresh"), "Enable", lambda: self.run(
                        "Enable", [f"pm enable {B.shq(a.pkg)}"], ok_text=f"Enabled {a.name}"))
                else:
                    m.addAction(L("lock"), "Disable", lambda: self.disable(a))
                m.addAction(L("broom"), "Clear data…", lambda: self.clear_data(a))
            else:
                m.addAction(L("download"), "Re-install for user 0", lambda: self.run(
                    "Re-install", [f"pm install-existing --user 0 {B.shq(a.pkg)}"], ok_text=f"Re-installed {a.name}"))
        if any(x.installed for x in sel):
            m.addAction(L("trash"), "Uninstall…" if one else f"Uninstall {len(sel)} apps…",
                        lambda: self.uninstall([x for x in sel if x.installed]))
        m.addSeparator()
        m.addAction(L("copy"), "Copy package name" if one else "Copy package names", self.copy_names)
        if one:
            m.addAction(L("star"), "Search on Google", lambda: QDesktopServices.openUrl(QUrl(
                "https://www.google.com/search?q=" + urllib.parse.quote(a.pkg))))
            m.addAction(L("star"), "Play Store page", lambda: QDesktopServices.openUrl(QUrl(
                "https://play.google.com/store/apps/details?id=" + urllib.parse.quote(a.pkg))))
        m.exec(self.view.viewport().mapToGlobal(pos))

    # ---- activities / logcat ------------------------------------------------
    def open_activities(self, pkg: str | None):
        from .activities import ActivitiesDialog
        labels = {a.pkg: a.name for a in self.model.apps}
        ActivitiesDialog(self.win, self.conn, pkg, icons=self.model.icons, labels=labels).show()

    def open_perms(self, a):
        from .perms import PermissionsDialog
        PermissionsDialog(self.win, self.conn, a.pkg, a.name).show()

    def fill_favorites(self, menu):
        from .activities import favorites_menu
        favorites_menu(self.win, self, menu)

    def open_logcat(self, pkg: str):
        from .logcat import LogcatDialog
        LogcatDialog(self.win, self.conn, package=pkg).show()

    # ---- actions --------------------------------------------------------
    def launch(self, a: App):
        self.run("Launch", [f"monkey -p {B.shq(a.pkg)} -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 "
                            f"|| am start -a android.intent.action.MAIN -n "
                            f"$(cmd package resolve-activity --brief {B.shq(a.pkg)} | tail -n1)"],
                 refresh=False, ok_text=f"Launched {a.name}")

    def open_folder(self, path: str):
        if not path:
            return
        self.win.navigate(path)
        self.win.raise_()
        self.win.activateWindow()

    def open_data_folder(self, a: App | None):
        if a:
            self.open_folder(a.data_dir)

    def disable(self, a: App):
        if a.system and not self.confirm("Disable app", f"Disable the system app {a.name} ({a.pkg})?\n\n"
                                         "Disabling some system apps can break features. "
                                         "You can enable it again from here."):
            return
        self.run("Disable", [f"pm disable-user --user 0 {B.shq(a.pkg)}"], ok_text=f"Disabled {a.name}")

    def clear_data(self, a: App):
        if self.confirm("Clear data", f"Delete ALL data of {a.name} ({a.pkg})?\n\n"
                        "Accounts, settings, databases and files of the app are erased. This cannot be undone."):
            self.run("Clear data", [f"pm clear {B.shq(a.pkg)}"], ok_text=f"Cleared data of {a.name}")

    def uninstall(self, apps: list[App]):
        if not apps:
            return
        sys_apps = [a for a in apps if a.system]
        lines = "\n".join(f"• {a.name} ({a.pkg}){'  [system]' if a.system else ''}" for a in apps[:15])
        more = f"\n… and {len(apps) - 15} more" if len(apps) > 15 else ""
        note = ("\n\nSystem apps are removed for user 0 only (the app stays in the system image); "
                "they can be restored with “Re-install for user 0”." if sys_apps else "")
        if not self.confirm("Uninstall", f"Uninstall {len(apps)} app(s)?\n\n{lines}{more}{note}"):
            return
        cmds = [f"pm uninstall --user 0 {B.shq(a.pkg)}" if a.system else f"pm uninstall {B.shq(a.pkg)}"
                for a in apps]
        self.run("Uninstall", cmds, ok_text=f"Uninstalled {len(apps)} app(s)")

    def copy_names(self):
        sel = self.selected()
        if sel:
            QApplication.clipboard().setText("\n".join(a.pkg for a in sel))
            self.win.statusBar().showMessage(f"Copied {len(sel)} package name(s)", 4000)

    def backup(self, apps: list[App], with_data: bool):
        last = self.win.cfg.data.get("last_app_backup", os.path.join(os.path.expanduser("~"), "Downloads"))
        dest = QFileDialog.getExistingDirectory(self, "Back up to folder", last)
        if not dest:
            return
        self.win.cfg.data["last_app_backup"] = dest
        self.win.cfg.save()
        conn = self.conn
        total = [sum(a.app_size + (a.data_size if with_data else 0) for a in apps) or 1]

        def safe(s):
            return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .") or "app"

        def job(fs, progress):
            written = []
            for a in apps:
                base = safe(f"{a.name}_{a.version}") if a.version else safe(a.name)
                rc, out, _ = conn.exec(f"ls {B.shq(a.code_path)}/*.apk 2>/dev/null || ls {B.shq(a.code_path)}",
                                       timeout=30)
                apks = [l for l in out.decode(errors="replace").split() if l.endswith(".apk")]
                if not apks:
                    raise B.BackendError(f"No APK found for {a.pkg} in {a.code_path}")
                tmp = tempfile.mkdtemp(prefix="androunderthehood-backup-")
                try:
                    local = []
                    for p in apks:
                        progress(0, f"{a.name}: {posixpath.basename(p)}")
                        lp = os.path.join(tmp, posixpath.basename(p))
                        fs.download(p, lp, progress)
                        local.append(lp)
                    if len(local) == 1:
                        out_path = os.path.join(dest, base + ".apk")
                        shutil.move(local[0], out_path)
                    else:
                        out_path = os.path.join(dest, base + ".apks")
                        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as z:
                            for lp in local:
                                name = os.path.basename(lp)
                                if name != "base.apk" and not name.startswith("split_"):
                                    name = "base.apk" if len(local) == 1 else name
                                z.write(lp, name)
                    written.append(out_path)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                if with_data:
                    progress(0, f"{a.name}: data")
                    data_path = os.path.join(dest, base + "_data.tar.gz")
                    dirs = " ".join(B.shq(d) for d in (f"data/data/{a.pkg}", f"data/user_de/0/{a.pkg}"))
                    cmd = (f"cd / && for d in {dirs}; do [ -d \"$d\" ] && printf '%s\\n' \"$d\"; done "
                           # tar warns (exit 1) about sockets/pipes it skips: not an error
                           f"| xargs tar -czf - 2>/dev/null; exit 0")
                    with open(data_path, "wb") as f:
                        def sink(b):
                            f.write(b)
                            progress(len(b))
                        conn.exec_stream_out(cmd, sink, timeout=1800)
                    written.append(data_path)
            return written

        def done(written):
            if written:
                QMessageBox.information(self, "Backup complete",
                                        f"Saved {len(written)} file(s) to\n{dest}\n\n"
                                        + "\n".join(os.path.basename(w) for w in written[:20]))
        self.win._transfer("Backing up apps", total, job, done)

    # ---- install --------------------------------------------------------
    def install_dialog(self):
        last = self.win.cfg.data.get("last_install_dir", os.path.join(os.path.expanduser("~"), "Downloads"))
        files, _ = QFileDialog.getOpenFileNames(self, "Install app", last,
                                                "Android packages (*.apk *.apks *.apkm *.xapk);;All files (*)")
        if files:
            self.win.cfg.data["last_install_dir"] = os.path.dirname(files[0])
            self.win.cfg.save()
            self.install(files)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls() and any(u.toLocalFile().lower().endswith(PACKAGE_EXTS)
                                           for u in ev.mimeData().urls()):
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        files = [u.toLocalFile() for u in ev.mimeData().urls() if u.toLocalFile().lower().endswith(PACKAGE_EXTS)]
        if files:
            self.install(files)

    def install(self, files: list[str]):
        names = "\n".join(os.path.basename(f) for f in files)
        if not self.confirm("Install", f"Install {len(files)} package(s) on {self.conn.profile.name}?\n\n{names}"):
            return
        conn = self.conn
        total = [sum(os.path.getsize(f) for f in files) or 1]

        def job(fs, progress):
            results = []
            for f in files:
                try:
                    results.append((f, True, install_package(conn, fs, f, progress)))
                except Exception as e:  # noqa: BLE001
                    log.warning("install %s failed: %r", f, e)
                    results.append((f, False, str(e)))
            return results

        def done(results):
            if not results:
                return
            ok = all(r[1] for r in results)
            text = "\n\n".join(f"{'✔' if r[1] else '✘'} {os.path.basename(r[0])}\n{r[2]}" for r in results)
            (QMessageBox.information if ok else QMessageBox.warning)(self, "Install", text)
            self.load()
        self.win._transfer("Installing", total, job, done)
