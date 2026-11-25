from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import os
import posixpath
import stat as statmod
import tempfile
import threading
import traceback

from PySide6.QtCore import (QAbstractTableModel, QFileInfo, QModelIndex, QObject, QPoint, QRunnable, QSize,
                            QSortFilterProxyModel, Qt, QThreadPool, QTimer, Signal)
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QDockWidget, QFileDialog, QFileIconProvider, QFormLayout,
                               QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListView,
                               QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QProgressDialog, QPushButton, QSizePolicy, QSlider,
                               QSpinBox, QSplitter, QStackedWidget, QStyle, QTableView, QTableWidget,
                               QTableWidgetItem, QToolBar, QToolButton, QVBoxLayout, QWidget)

from . import backend as B
from . import mirror as M
from . import theme
from . import thumbs as T

APP_NAME = "AndroUnderTheHood"
THUMB_SIZE = 256


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def human_size(n: int | None) -> str:
    if n is None:
        return ""
    f = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{int(f)} bytes" if unit == "bytes" else f"{f:.1f} {unit}"
        f /= 1024
    return str(n)


def fmt_time(t: int | None) -> str:
    if not t:
        return ""
    try:
        return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return str(t)


class _Sig(QObject):
    done = Signal(object)
    failed = Signal(str)


class Task(QRunnable):
    """Run fn in the global thread pool and deliver result on the GUI thread."""
    _alive: set = set()

    def __init__(self, fn, on_done=None, on_fail=None):
        super().__init__()
        self.fn = fn
        self.sig = _Sig()
        if on_done:
            self.sig.done.connect(on_done)
        if on_fail:
            self.sig.failed.connect(on_fail)
        self.setAutoDelete(False)

    def run(self):
        try:
            r = self.fn()
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            logging.getLogger("androunderthehood").error("task failed: %s", traceback.format_exc())
            self._emit(self.sig.failed, str(e) or e.__class__.__name__)
        else:
            self._emit(self.sig.done, r)

    @staticmethod
    def _emit(signal, value):
        try:
            signal.emit(value)
        except RuntimeError:  # receiver gone (window closed while the task ran)
            pass

    @classmethod
    def start(cls, fn, on_done=None, on_fail=None):
        t = cls(fn, on_done, on_fail)
        cls._alive.add(t)
        t.sig.done.connect(lambda *_: cls._alive.discard(t))
        t.sig.failed.connect(lambda *_: cls._alive.discard(t))
        QThreadPool.globalInstance().start(t)
        return t


class Cancelled(Exception):
    pass


def close_progress(p: QProgressDialog):
    """Hide a progress dialog without firing its canceled() signal (close() would)."""
    try:
        p.canceled.disconnect()
    except (RuntimeError, TypeError):
        pass
    p.hide()
    p.deleteLater()


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
class Config:
    def __init__(self):
        self.path = os.path.join(B.app_dir(), "config.json")
        self.data = {"profiles": [], "last": None, "bookmarks": [], "view": "icons", "icon_size": 128,
                     "show_hidden": True}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))
        except Exception:  # noqa: BLE001
            pass

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception:  # noqa: BLE001
            pass

    def profiles(self) -> list[B.Profile]:
        return [B.Profile.from_dict(d) for d in self.data.get("profiles", [])]

    def upsert_profile(self, p: B.Profile):
        lst = [d for d in self.data.get("profiles", []) if d.get("name") != p.name]
        lst.insert(0, p.to_dict())
        self.data["profiles"] = lst
        self.data["last"] = p.name
        self.save()

    def delete_profile(self, name: str):
        self.data["profiles"] = [d for d in self.data.get("profiles", []) if d.get("name") != name]
        self.save()


# --------------------------------------------------------------------------
# Icons
# --------------------------------------------------------------------------
class IconCache:
    def __init__(self):
        self.provider = QFileIconProvider()
        self.cache: dict[str, QIcon] = {}
        style = QApplication.style()
        self.folder = self.provider.icon(QFileIconProvider.IconType.Folder)
        self.file = self.provider.icon(QFileIconProvider.IconType.File)
        self.link_folder = self._overlay(self.folder, "↪")
        self.device = style.standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon)
        self.tmpdir = os.path.join(tempfile.gettempdir(), "AndroUnderTheHood", "icons")
        os.makedirs(self.tmpdir, exist_ok=True)

    @staticmethod
    def _overlay(icon: QIcon, txt: str) -> QIcon:
        pm = icon.pixmap(64, 64)
        p = QPainter(pm)
        f = QFont()
        f.setPixelSize(28)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#5ee69b"))
        p.drawText(pm.rect().adjusted(2, 0, 0, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, txt)
        p.end()
        return QIcon(pm)

    def for_entry(self, e: B.Entry) -> QIcon:
        if e.is_dir:
            return self.link_folder if e.is_link else self.folder
        if statmod.S_ISCHR(e.mode) or statmod.S_ISBLK(e.mode):
            return self.device
        ext = T.ext_of(e.name)
        if not ext or len(ext) > 10:
            return self.file
        if ext not in self.cache:
            # Windows shell icons need a real file with that extension
            fake = os.path.join(self.tmpdir, "x." + ext)
            try:
                if not os.path.exists(fake):
                    open(fake, "wb").close()
                ic = self.provider.icon(QFileInfo(fake))
            except Exception:  # noqa: BLE001
                ic = QIcon()
            self.cache[ext] = ic if not ic.isNull() else self.file
        return self.cache[ext]


# --------------------------------------------------------------------------
# Thumbnail manager
# --------------------------------------------------------------------------
class ThumbnailManager(QObject):
    ready = Signal(str, QImage, dict)    # path, image (may be null), info
    progress = Signal(int)               # remaining in queue

    WORKERS = 3

    def __init__(self):
        super().__init__()
        self.cache = T.ThumbCache()
        self.conn: B.Connection | None = None
        self.gen = 0
        self.queue: collections.deque = collections.deque()
        self.cond = threading.Condition()
        self.local = threading.local()
        self.enabled = True
        self.threads = [threading.Thread(target=self._loop, daemon=True) for _ in range(self.WORKERS)]
        for t in self.threads:
            t.start()

    def set_connection(self, conn):
        with self.cond:
            self.conn = conn
            self.gen += 1
            self.queue.clear()

    def host_key(self):
        p = self.conn.profile if self.conn else None
        return f"{p.host}:{p.port}:{p.mode}" if p else ""

    def request(self, entries: list[B.Entry]):
        with self.cond:
            self.gen += 1
            self.queue.clear()
            if self.enabled:
                for e in entries:
                    if T.media_kind(e):
                        self.queue.append((self.gen, e))
            self.cond.notify_all()
        self.progress.emit(len(self.queue))

    def cancel(self):
        with self.cond:
            self.gen += 1
            self.queue.clear()
        self.progress.emit(0)

    def bump(self, paths: list[str]):
        """Move these paths to the front of the queue (visible items first)."""
        if not paths:
            return
        want = set(paths)
        with self.cond:
            front = [q for q in self.queue if q[1].path in want]
            if not front:
                return
            rest = [q for q in self.queue if q[1].path not in want]
            self.queue = collections.deque(front + rest)

    def _fs(self, conn):
        if getattr(self.local, "conn", None) is not conn:
            self.local.conn = conn
            self.local.fs = conn.new_fs()
        return self.local.fs

    def _loop(self):
        while True:
            with self.cond:
                while not self.queue:
                    self.cond.wait()
                gen, e = self.queue.popleft()
                conn = self.conn
                left = len(self.queue)
            try:
                self.progress.emit(left)
            except RuntimeError:  # manager deleted (app closing)
                return
            if gen != self.gen or conn is None or not conn.alive:
                continue
            key = self.cache.key(self.host_key(), e, THUMB_SIZE)
            hit = self.cache.get(key)
            if hit is not None:
                png, info = hit
            else:
                try:
                    fs = self._fs(conn)
                    png, info = T.make_thumbnail(fs, e, THUMB_SIZE)
                except Exception as ex:  # noqa: BLE001
                    logging.getLogger("androunderthehood").warning("thumbnail %s failed: %r", e.path, ex)
                    png, info = None, {"Thumbnail error": str(ex)[:200]}
                    self.local.conn = None  # recreate session next time
                    failed = True
                else:
                    failed = False
                # never cache failures: they are often transient (connection dropped)
                if not failed and (gen == self.gen or png):
                    self.cache.put(key, png, info)
            img = QImage()
            if png:
                img.loadFromData(png, "PNG")
            try:
                self.ready.emit(e.path, img, info)
            except RuntimeError:  # manager deleted (app closing)
                return


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------
COLS = ["Name", "Size", "Type", "Modified", "Permissions", "Owner", "Group"]


class FileModel(QAbstractTableModel):
    def __init__(self, icons: IconCache):
        super().__init__()
        self.icons = icons
        self.entries: list[B.Entry] = []
        self.thumbs: dict[str, QIcon] = {}
        self.row_of: dict[str, int] = {}

    def set_entries(self, entries):
        self.beginResetModel()
        self.entries = entries
        self.thumbs = {}
        self.row_of = {e.path: i for i, e in enumerate(entries)}
        self.endResetModel()

    def set_thumb(self, path: str, img: QImage):
        r = self.row_of.get(path)
        if r is None or img.isNull():
            return
        self.thumbs[path] = QIcon(QPixmap.fromImage(img))
        idx = self.index(r, 0)
        self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLS)

    def headerData(self, section, orient, role=Qt.ItemDataRole.DisplayRole):
        if orient == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLS[section]
        return None

    def type_name(self, e: B.Entry) -> str:
        if not statmod.S_ISREG(e.mode) and not (e.is_link and not e.target_is_dir):
            return e.kind
        ext = T.ext_of(e.name)
        base = f"{ext.upper()} file" if ext else "File"
        return base + (" (link)" if e.is_link else "")

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        e = self.entries[index.row()]
        c = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if c == 0:
                return e.name
            if c == 1:
                return "" if e.is_dir else human_size(T.file_size(e))
            if c == 2:
                return self.type_name(e)
            if c == 3:
                return fmt_time(e.mtime)
            if c == 4:
                return f"{e.perm_string}  {e.perm_octal}"
            if c == 5:
                return e.owner or (str(e.uid) if e.uid is not None else "")
            if c == 6:
                return e.group or (str(e.gid) if e.gid is not None else "")
        elif role == Qt.ItemDataRole.DecorationRole and c == 0:
            return self.thumbs.get(e.path) or self.icons.for_entry(e)
        elif role == Qt.ItemDataRole.ToolTipRole:
            tip = f"{e.path}\n{self.type_name(e)}"
            if not e.is_dir:
                tip += f"\n{human_size(T.file_size(e))}"
            if e.link_target:
                tip += f"\n→ {e.link_target}"
            return tip
        elif role == Qt.ItemDataRole.UserRole:
            return e
        elif role == Qt.ItemDataRole.UserRole + 1:  # sort key
            if c == 0:
                return e.name.lower()
            if c == 1:
                return T.file_size(e)
            if c == 2:
                return self.type_name(e).lower()
            if c == 3:
                return e.mtime or 0
            if c == 4:
                return e.mode
            if c == 5:
                return (e.owner or "").lower()
            if c == 6:
                return (e.group or "").lower()
        elif role == Qt.ItemDataRole.TextAlignmentRole and c == 1:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        elif role == Qt.ItemDataRole.ForegroundRole and e.name.startswith("."):
            return QColor("#7d838c")
        return None


class SortProxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.show_hidden = True
        self.setSortRole(Qt.ItemDataRole.UserRole + 1)

    def set_filter(self, text, show_hidden):
        self.text = text.lower()
        self.show_hidden = show_hidden
        self.invalidateFilter() if hasattr(self, "invalidateFilter") else self.invalidate()

    def filterAcceptsRow(self, row, parent):
        e = self.sourceModel().entries[row]
        if not self.show_hidden and e.name.startswith("."):
            return False
        return not self.text or self.text in e.name.lower()

    def lessThan(self, a, b):
        ea = self.sourceModel().entries[a.row()]
        eb = self.sourceModel().entries[b.row()]
        asc = self.sortOrder() == Qt.SortOrder.AscendingOrder
        if ea.is_dir != eb.is_dir:
            # folders always first regardless of direction
            return ea.is_dir if asc else eb.is_dir
        ka = a.data(self.sortRole())
        kb = b.data(self.sortRole())
        if ka == kb:
            return ea.name.lower() < eb.name.lower()
        try:
            return ka < kb
        except TypeError:
            return str(ka) < str(kb)


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------
class DropMixin:
    files_dropped = None

    def _setup_drop(self):
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        else:
            super().dragMoveEvent(ev)

    def dropEvent(self, ev):
        if ev.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
            if paths and self.on_drop:
                self.on_drop(paths)
            ev.acceptProposedAction()
        else:
            super().dropEvent(ev)


class IconView(DropMixin, QListView):
    def __init__(self, on_drop):
        super().__init__()
        self.on_drop = on_drop
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSpacing(4)
        self._setup_drop()

    def set_icon_size(self, s):
        self.setIconSize(QSize(s, s))
        self.setGridSize(QSize(max(s + 28, 96), s + 52))


class DetailView(DropMixin, QTableView):
    def __init__(self, on_drop):
        super().__init__()
        self.on_drop = on_drop
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSortingEnabled(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setAlternatingRowColors(True)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(24)
        self.setIconSize(QSize(20, 20))
        h = self.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h.setStretchLastSection(True)
        h.setHighlightSections(False)
        self._setup_drop()


# --------------------------------------------------------------------------
# Properties panel
# --------------------------------------------------------------------------
class PropertiesPanel(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.entry: B.Entry | None = None
        self.setObjectName("propsPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        self.preview = QLabel()
        self.preview.setObjectName("preview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(200)
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(self.preview)
        self.title = QLabel()
        self.title.setObjectName("propsTitle")
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.title)
        self.table = QTableWidget(0, 2)
        self.table.setObjectName("propsTable")
        self.table.horizontalHeader().hide()
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self.table, 2)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(QFont("Consolas", 9))
        self.text.hide()
        lay.addWidget(self.text, 1)
        btns = QHBoxLayout()
        self.btn_size = QPushButton("Calculate size")
        self.btn_size.clicked.connect(self.calc_size)
        self.btn_open = QPushButton("Open")
        self.btn_open.clicked.connect(lambda: self.entry and self.win.open_entry(self.entry))
        self.btn_dl = QPushButton("Download…")
        self.btn_dl.clicked.connect(self.win.download_selected)
        for b in (self.btn_size, self.btn_open, self.btn_dl):
            btns.addWidget(b)
        lay.addLayout(btns)
        self.extra: dict = {}
        self.show_entry(None)

    def _rows(self):
        e = self.entry
        rows = []
        if not e:
            return rows
        rows += [("Location", posixpath.dirname(e.path) or "/"), ("Type", self.win.model.type_name(e))]
        if not e.is_dir:
            sz = T.file_size(e)
            rows.append(("Size", f"{human_size(sz)} ({sz:,} bytes)"))
        if e.is_link:
            rows.append(("Link target", e.link_target or "?"))
        rows += [
            ("Modified", fmt_time(e.mtime)),
            ("Accessed", fmt_time(e.atime)),
            ("Permissions", f"{e.perm_string}  ({e.perm_octal})"),
            ("Owner", f"{e.owner or '?'}  (uid {e.uid})" if e.uid is not None else (e.owner or "?")),
            ("Group", f"{e.group or '?'}  (gid {e.gid})" if e.gid is not None else (e.group or "?")),
        ]
        m = e.mode
        flags = [n for bit, n in ((statmod.S_ISUID, "setuid"), (statmod.S_ISGID, "setgid"),
                                  (statmod.S_ISVTX, "sticky")) if m & bit]
        if flags:
            rows.append(("Special bits", ", ".join(flags)))
        for k, v in self.extra.items():
            rows.append((k, str(v)))
        return rows

    def refresh_table(self):
        rows = self._rows()
        self.table.setRowCount(len(rows))
        for i, (k, v) in enumerate(rows):
            ki = QTableWidgetItem(k)
            f = ki.font()
            f.setBold(True)
            ki.setFont(f)
            self.table.setItem(i, 0, ki)
            vi = QTableWidgetItem(v)
            vi.setToolTip(v)
            self.table.setItem(i, 1, vi)

    def show_entry(self, e: B.Entry | None, count: int = 1):
        self.entry = e
        self.extra = {}
        self.text.hide()
        self.text.clear()
        if e is None:
            self.title.setText("No selection" if count == 0 else f"{count} items selected")
            self.preview.clear()
            self.preview.setText("")
            self.table.setRowCount(0)
            for b in (self.btn_size, self.btn_open):
                b.setEnabled(False)
            self.btn_dl.setEnabled(count > 0)
            return
        self.title.setText(e.name)
        self.btn_size.setEnabled(e.is_dir)
        self.btn_open.setEnabled(True)
        self.btn_dl.setEnabled(True)
        info = self.win.thumb_info.get(e.path)
        if info:
            self.extra.update(info)
        self.set_preview(self.win.model.thumbs.get(e.path))
        self.refresh_table()
        if e.is_dir:
            self.win.fetch_dir_summary(e)
        elif T.ext_of(e.name) in T.TEXT_EXT or (T.file_size(e) < 64 * 1024 and not T.media_kind(e)):
            self.win.fetch_text_preview(e)

    def set_preview(self, icon: QIcon | None):
        e = self.entry
        if icon is not None:
            pm = icon.pixmap(QSize(THUMB_SIZE, THUMB_SIZE))
        elif e is not None:
            pm = self.win.icons.for_entry(e).pixmap(QSize(96, 96))
        else:
            pm = QPixmap()
        self.preview.setPixmap(pm)

    def add_info(self, path: str, info: dict):
        if self.entry and self.entry.path == path:
            self.extra.update(info)
            self.refresh_table()

    def set_text(self, path: str, text: str):
        if self.entry and self.entry.path == path and text:
            self.text.setPlainText(text)
            self.text.show()

    def calc_size(self):
        e = self.entry
        if not e or not e.is_dir:
            return
        self.btn_size.setEnabled(False)
        self.extra["Size on disk"] = "calculating…"
        self.refresh_table()
        fs = self.win.fs

        def work():
            return fs.folder_usage(e.path)

        def done(r):
            size, files, dirs = r
            self.add_info(e.path, {"Size on disk": f"{human_size(size)} ({size:,} bytes)",
                                   "Contains": f"{files:,} files, {dirs:,} folders"})
            self.btn_size.setEnabled(True)

        def fail(msg):
            self.add_info(e.path, {"Size on disk": f"failed: {msg}"})
            self.btn_size.setEnabled(True)
        Task.start(work, done, fail)


# --------------------------------------------------------------------------
# Connect dialog
# --------------------------------------------------------------------------
class ConnectDialog(QDialog):
    def __init__(self, parent, cfg: Config):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Connect to Android device")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Saved:"))
        self.saved = QComboBox()
        self.saved.addItem("(new connection)")
        for p in cfg.profiles():
            self.saved.addItem(p.name)
        self.saved.currentIndexChanged.connect(self.load_saved)
        top.addWidget(self.saved, 1)
        self.btn_del = QPushButton("Delete")
        self.btn_del.clicked.connect(self.delete_saved)
        top.addWidget(self.btn_del)
        lay.addLayout(top)

        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Pixel 7 (Magisk)")
        self.host = QLineEdit()
        self.host.setPlaceholderText("192.168.1.50")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        hp = QHBoxLayout()
        hp.addWidget(self.host, 1)
        hp.addWidget(QLabel("Port"))
        hp.addWidget(self.port)
        self.user = QLineEdit("root")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("not saved – leave empty to log in with your SSH key")
        self.key = QLineEdit()
        self.key.setPlaceholderText("optional – ~/.ssh/id_ed25519 / id_ecdsa / id_rsa are tried automatically")
        kb = QPushButton("…")
        kb.setObjectName("browse")
        kb.setToolTip("Browse for a private key")
        kb.clicked.connect(self.pick_key)
        kh = QHBoxLayout()
        kh.addWidget(self.key, 1)
        kh.addWidget(kb)
        self.passphrase = QLineEdit()
        self.passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase.setPlaceholderText("key passphrase, if any")
        self.mode = QComboBox()
        self.mode.addItem("SFTP", B.MODE_SFTP)
        self.mode.addItem("Shell commands (no SFTP needed)", B.MODE_SHELL)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.su = QLineEdit("su -c")
        self.sftp_server = QLineEdit()
        self.sftp_server.setPlaceholderText("auto-detect")
        self.start = QLineEdit("/sdcard")
        form.addRow("Name", self.name)
        form.addRow("Host", hp)
        form.addRow("Username", self.user)
        form.addRow("Password", self.password)
        form.addRow("Private key", kh)
        form.addRow("Passphrase", self.passphrase)
        form.addRow("Access mode", self.mode)
        form.addRow("su command (Root)", self.su)
        form.addRow("sftp-server path", self.sftp_server)
        form.addRow("Start folder", self.start)
        lay.addLayout(form)
        hint = QLabel(
            "<small>Root is <b>off</b> by default – switch it on with the <b>Root</b> button in the toolbar "
            "(uses the su command above). Termux sshd uses port <b>8022</b>.</small>")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.save_cb = QCheckBox("Save this connection (password is never stored)")
        self.save_cb.setChecked(True)
        lay.addWidget(self.save_cb)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        self.use_adb = False
        adb_btn = bb.addButton("ADB device…", QDialogButtonBox.ButtonRole.ActionRole)
        adb_btn.setIcon(theme.line_icon("phone"))
        adb_btn.setToolTip("Connect over ADB (USB cable or adb over Wi-Fi) instead of SSH")
        adb_btn.clicked.connect(self.choose_adb)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        last = cfg.data.get("last")
        if last:
            i = self.saved.findText(last)
            if i > 0:
                self.saved.setCurrentIndex(i)
        self.mode_changed()

    def choose_adb(self):
        self.use_adb = True
        super().accept()

    def pick_key(self):
        p, _ = QFileDialog.getOpenFileName(self, "Private key", os.path.expanduser("~/.ssh"))
        if p:
            self.key.setText(p)

    def mode_changed(self):
        self.sftp_server.setEnabled(self.mode.currentData() == B.MODE_SFTP)

    def load_saved(self, i):
        self.btn_del.setEnabled(i > 0)
        if i <= 0:
            return
        p = next((x for x in self.cfg.profiles() if x.name == self.saved.currentText()), None)
        if not p:
            return
        self.name.setText(p.name)
        self.host.setText(p.host)
        self.port.setValue(p.port)
        self.user.setText(p.username)
        self.key.setText(p.key_path)
        self.mode.setCurrentIndex(max(0, self.mode.findData(B.MODE_SFTP if p.mode == B.MODE_SU_SFTP else p.mode)))
        self.su.setText(p.su_command or "su -c")
        self.sftp_server.setText(p.sftp_server)
        self.start.setText(p.start_dir)
        self.password.setFocus()

    def delete_saved(self):
        i = self.saved.currentIndex()
        if i > 0:
            self.cfg.delete_profile(self.saved.currentText())
            self.saved.removeItem(i)

    def profile(self) -> B.Profile:
        host = self.host.text().strip()
        return B.Profile(
            name=self.name.text().strip() or f"{self.user.text().strip()}@{host}",
            host=host, port=self.port.value(), username=self.user.text().strip(),
            key_path=self.key.text().strip(), mode=self.mode.currentData(),
            su_command=self.su.text().strip() or "su -c", sftp_server=self.sftp_server.text().strip(),
            start_dir=self.start.text().strip() or "/")

    def accept(self):
        if not self.host.text().strip():
            QMessageBox.warning(self, APP_NAME, "Enter the device's IP address or host name.")
            return
        super().accept()


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
INTERNAL_STORAGE = "/storage/emulated/0"

# storage volume kinds -> (label, icon)
VOLUME_KINDS = {"sd": ("SD card", "sdcard"), "usb": ("USB drive", "usb"), "ext": ("External storage", "sdcard")}


def detect_volumes(conn: B.Connection) -> list[tuple[str, str, str]]:
    """Removable storage on the device: [(label, path, icon)].

    Uses Android's storage manager (`sm list-volumes`): public volumes on block
    major 179 are SD cards (mmcblk), major 8 are USB/OTG drives (sd*). Falls
    back to UUID-looking folders under /storage.
    """
    rc, out, _ = conn.exec("sm list-volumes all 2>/dev/null; echo '//LS//'; ls /storage 2>/dev/null",
                           timeout=30)
    text = out.decode(errors="replace")
    sm_part, _, ls_part = text.partition("//LS//")
    found: list[tuple[str, str]] = []  # (kind, uuid)
    sm_ok = False
    for line in sm_part.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        sm_ok = True
        vid, state, uuid = parts[0], parts[1], parts[2]
        if not vid.startswith("public:") or not state.startswith("mounted") or uuid == "null":
            continue
        major = vid.split(":", 1)[1].split(",")[0]
        found.append(({"179": "sd", "8": "usb"}.get(major, "ext"), uuid))
    if not sm_ok:
        import re
        for name in ls_part.split():
            if re.fullmatch(r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}|[0-9a-f]{16}", name):
                found.append(("ext", name))
    counts: dict[str, int] = {}
    for kind, _u in found:
        counts[kind] = counts.get(kind, 0) + 1
    vols = []
    for kind, uuid in found:
        label, icon = VOLUME_KINDS[kind]
        if counts[kind] > 1:
            label += f" ({uuid})"
        vols.append((label, f"/storage/{uuid}", icon))
    return vols


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = Config()
        self.icons = IconCache()
        self.conn: B.Connection | None = None
        self.fs: B.FileSystem | None = None
        self.cwd = ""
        self.back_stack: list[str] = []
        self.fwd_stack: list[str] = []
        self.thumb_info: dict[str, dict] = {}
        self.list_gen = 0
        self.setWindowTitle(APP_NAME)
        self.resize(1320, 820)

        self.model = FileModel(self.icons)
        self.proxy = SortProxy()
        self.proxy.setSourceModel(self.model)
        self.proxy.sort(0, Qt.SortOrder.AscendingOrder)

        self.thumbs = ThumbnailManager()
        self.thumbs.ready.connect(self.on_thumb)
        self.thumbs.progress.connect(self.on_thumb_progress)

        # views
        self.icon_view = IconView(self.upload_paths)
        self.icon_view.setModel(self.proxy)
        self.detail_view = DetailView(self.upload_paths)
        self.detail_view.setModel(self.proxy)
        self.detail_view.setSelectionModel(self.icon_view.selectionModel())
        self.detail_view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        for c, w in enumerate([320, 90, 110, 150, 130, 90, 90]):
            self.detail_view.setColumnWidth(c, w)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.icon_view)
        self.stack.addWidget(self.detail_view)
        for v in (self.icon_view, self.detail_view):
            v.doubleClicked.connect(self.on_activate)
            v.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            v.customContextMenuRequested.connect(self.context_menu)
            v.verticalScrollBar().valueChanged.connect(lambda *_: self.visible_timer.start(120))
        self.icon_view.selectionModel().selectionChanged.connect(self.on_selection)

        self.visible_timer = QTimer(self)
        self.visible_timer.setSingleShot(True)
        self.visible_timer.timeout.connect(self.bump_visible)

        # places
        self.places = QListWidget()
        self.places.setObjectName("places")
        self.places.itemActivated.connect(lambda it: self.navigate(it.data(Qt.ItemDataRole.UserRole)))
        self.places.itemClicked.connect(lambda it: self.navigate(it.data(Qt.ItemDataRole.UserRole)))
        self.places.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.places.customContextMenuRequested.connect(self.places_menu)
        self.fill_places()

        self.props = PropertiesPanel(self)

        split = QSplitter()
        split.addWidget(self.places)
        split.addWidget(self.stack)
        split.addWidget(self.props)
        split.setSizes([200, 800, 340])
        split.setStretchFactor(1, 1)
        split.setHandleWidth(8)  # gap between the rounded "cards"
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(10, 4, 10, 4)
        cl.addWidget(split)
        self.setCentralWidget(central)

        self.build_toolbar()
        self.status_conn = QLabel("Not connected")
        self.status_thumbs = QLabel("")
        self.statusBar().addPermanentWidget(self.status_thumbs)
        self.statusBar().addPermanentWidget(self.status_conn)

        self.set_view(self.cfg.data.get("view", "icons"))
        self.size_slider.setValue(int(self.cfg.data.get("icon_size", 128)))
        self.hidden_cb.setChecked(bool(self.cfg.data.get("show_hidden", True)))
        self.update_actions()

    # ---- UI construction ------------------------------------------------
    def build_toolbar(self):
        from .dragbar import TEXT_BESIDE, DragToolBar
        tb = DragToolBar("Main", "main", self)
        tb.setIconSize(QSize(20, 20))
        self.addToolBar(tb)

        self._icon_names = set()

        def act(text, icon, slot, shortcut=None, tip=None):
            if isinstance(icon, str):
                self._icon_names.add(icon)
            a = QAction(theme.line_icon(icon) if isinstance(icon, str) else
                        theme.tinted_icon(icon) if icon is not None else QIcon(), text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcuts(shortcut if isinstance(shortcut, list) else [QKeySequence(shortcut)])
            if tip:
                a.setToolTip(tip)
            self.addAction(a)
            return a
        SP = QStyle.StandardPixmap
        self.a_connect = act("Connect", "connect", self.connect_dialog, "Ctrl+N")
        self.a_disconnect = act("Disconnect", "disconnect", self.disconnect)
        self.a_back = act("Back", "back", self.go_back, [QKeySequence("Alt+Left"), QKeySequence(Qt.Key.Key_Back)])
        self.a_fwd = act("Forward", "forward", self.go_forward, "Alt+Right")
        self.a_up = act("Up", "up", self.go_up, [QKeySequence("Alt+Up"), QKeySequence(Qt.Key.Key_Backspace)])
        self.a_refresh = act("Refresh", "refresh", self.refresh, "F5")
        self.a_root = act("Root", "hash", lambda on: self.toggle_root(on), "Ctrl+Shift+R",
                          "Root mode (off by default). When on, AndroUnderTheHood runs its commands and file access "
                          "through su – approve the root prompt on the phone if it asks. Toggling reconnects.")
        self.a_root.setCheckable(True)
        self.a_root.setChecked(False)
        self.a_root.setProperty(TEXT_BESIDE, True)
        for a in (self.a_connect, self.a_disconnect, self.a_root):
            tb.addAction(a)
        tb.add_separator("sep_main_1")
        for a in (self.a_back, self.a_fwd, self.a_up, self.a_refresh):
            tb.addAction(a)
        self.addr = QLineEdit()
        self.addr.setPlaceholderText("Path on device")
        self.addr.returnPressed.connect(lambda: self.navigate(self.addr.text().strip() or "/"))
        tb.add_widget(self.addr, "w_addr")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter…")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(180)
        self.search.textChanged.connect(self.apply_filter)
        tb.add_widget(self.search, "w_filter")
        tb.add_separator("sep_main_2")
        self.a_icons = act("Thumbnails", "grid", lambda: self.set_view("icons"), "Ctrl+1")
        self.a_details = act("Details", "list", lambda: self.set_view("details"), "Ctrl+2")
        self.a_icons.setCheckable(True)
        self.a_details.setCheckable(True)
        tb.addAction(self.a_icons)
        tb.addAction(self.a_details)
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(48, THUMB_SIZE)
        self.size_slider.setFixedWidth(110)
        self.size_slider.setToolTip("Thumbnail size")
        self.size_slider.valueChanged.connect(self.set_icon_size)
        tb.add_widget(self.size_slider, "w_size")
        self.hidden_cb = QCheckBox("Hidden")
        self.hidden_cb.setToolTip("Show dot-files")
        self.hidden_cb.toggled.connect(self.apply_filter)
        tb.add_widget(self.hidden_cb, "w_hidden")
        self.thumbs_cb = QCheckBox("Thumbs")
        self.thumbs_cb.setChecked(True)
        self.thumbs_cb.setToolTip("Generate thumbnails for images, videos, music and APKs")
        self.thumbs_cb.toggled.connect(self.toggle_thumbs)
        tb.add_widget(self.thumbs_cb, "w_thumbs")

        tb2 = DragToolBar("Actions", "actions", self)
        self.tb2 = tb2
        self.toolbars = [tb, tb2]
        tb2.setIconSize(QSize(18, 18))
        self.addToolBarBreak()
        self.addToolBar(tb2)
        self.a_download = act("Download…", "download", self.download_selected, "Ctrl+D")
        self.a_upload = act("Upload…", "upload", self.upload_dialog, "Ctrl+U")
        self.a_mkdir = act("New folder", "folder-plus", self.new_folder, "Ctrl+Shift+N")
        self.a_rename = act("Rename", "rename", self.rename_selected, "F2")
        self.a_delete = act("Delete", "trash", self.delete_selected, "Delete")
        self.a_copy_path = act("Copy path", "copy", self.copy_path, "Ctrl+Shift+C")
        self.a_bookmark = act("Add to shortcuts", "star", lambda: self.add_shortcut(), "Ctrl+B",
                              "Add the current folder to the shortcuts on the left")
        self.a_shell = act("Run command…", "terminal", self.run_command, "Ctrl+R")
        self.a_apps = act("Apps", "package", self.open_apps, "Ctrl+Shift+A",
                          "Installed apps: back up, uninstall, open data folders, install APK/APKS/APKM/XAPK")
        self.a_parts = act("Partitions", "pie", self.open_partitions, "Ctrl+Shift+P",
                           "Partitions: view usage, back up to PC, flash images, mount/unmount")
        self.a_device = act("Device", "chip", self.open_device, "Ctrl+I",
                            "Hardware info, bootloader/security state, reboot modes, ADB and USB modes")
        self.a_logcat = act("Logcat", "log", self.open_logcat, "Ctrl+L",
                            "Live system log with level/tag/app filters")
        self.a_rootmgr = act("Root manager", "shield", self.open_rootmgr, "Ctrl+Shift+K",
                             "Magisk / Kitsune / KernelSU / APatch: Zygisk, DenyList, superuser grants and "
                             "modules (install, enable/disable, uninstall)")
        self.a_modem = act("Modem", "antenna", self.open_modem, "Ctrl+Shift+M",
                           "Qualcomm modem tools over DIAG: NV items, EFS files, live modem log (QXDM-style) "
                           "and a raw command console")
        self.a_sysfs = act("sysfs", "tree", self.open_sysfs, "Ctrl+K",
                           "Kernel tunables: browse and edit /sys and /proc/sys (CPU/GPU frequencies and "
                           "governors, thermal, battery, I/O scheduler, LEDs, vm settings…)")
        self.a_props = act("Properties", "sliders", self.open_props, "Ctrl+P",
                           "View, filter and edit system properties (getprop / setprop / resetprop)")
        self.a_clear_cache = act("Clear thumbnail cache", "broom", self.clear_cache)
        self.a_flash = act("Flash tools", "flash", self.open_flashtools, "Ctrl+Shift+F",
                           "Download / add and launch flash tools: fastboot, edl, mtkclient, Thor, spd_dump, "
                           "plus your own QPST, Odin, SP Flash Tool, MSM, Mi Flash…")
        self.a_drivers = act("USB drivers…", "usb", self.open_drivers, None,
                             "Install the Windows drivers for every phone USB mode (ADB, fastboot, Qualcomm EDL / "
                             "diag, MediaTek VCOM, Samsung) with one click")
        # left: file actions
        for a in (self.a_download, self.a_upload, self.a_mkdir, self.a_rename, self.a_delete):
            tb2.addAction(a)
        # spacer pushes the device tools to the right edge
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb2.add_widget(spacer, "w_spacer")
        # Screen mirror (scrcpy-ssh) with an options drop-down
        self.a_mirror = act("Screen mirror", "phone", self.start_mirror, "Ctrl+M",
                            "Mirror and control the device screen (scrcpy over SSH)")
        mmenu = QMenu(self)
        opts = self.cfg.data.setdefault("mirror_opts", {"stay_awake": True})
        for key, label, _args in M.MIRROR_OPTIONS:
            a = mmenu.addAction(label)
            a.setCheckable(True)
            a.setChecked(bool(opts.get(key)))
            a.toggled.connect(lambda on, k=key: (self.cfg.data["mirror_opts"].__setitem__(k, on),
                                                 self.cfg.save()))
        mmenu.addSeparator()
        mmenu.addAction("Locate scrcpy-ssh…", self.locate_scrcpy)
        mmenu.addAction("Show last mirror log", self.show_mirror_log)
        btn = QToolButton()
        btn.setDefaultAction(self.a_mirror)
        btn.setMenu(mmenu)
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.a_mirror_w = tb2.add_widget(btn, "a_mirror_w")
        self.mirror_procs: list = []
        # right: device tools
        for a in (self.a_device, self.a_apps, self.a_parts, self.a_rootmgr, self.a_modem, self.a_sysfs,
                  self.a_props, self.a_logcat, self.a_shell, self.a_flash):
            tb2.addAction(a)
        # "More" menu for rarely used actions
        more = QMenu(self)
        for a in (self.a_bookmark, self.a_copy_path):
            more.addAction(a)
        more.addSeparator()
        more.addAction(self.a_flash)
        more.addAction(self.a_drivers)
        from .plugins import PluginManager
        self.plugins = PluginManager(self)
        pm = more.addMenu(theme.line_icon("puzzle"), "Plugins")
        pm.addSeparator()
        pm.addAction(theme.line_icon("gear"), "Manage plugins…", self.open_plugins)
        self.plugins.menu = pm
        more.addAction(self.a_clear_cache)
        more_btn = QToolButton()
        more_btn.setIcon(theme.line_icon("more"))
        more_btn.setToolTip("More actions")
        more_btn.setMenu(more)
        more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more_btn.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0; }")
        self.plugins.toolbar = tb2
        self.plugins.toolbar_before = tb2.add_widget(more_btn, "w_more")
        tb2.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        # every button gets a key so its place can be saved; then put back the user's order
        for k, v in vars(self).items():
            if k.startswith("a_") and isinstance(v, QAction) and not v.objectName():
                v.setObjectName(k)
        self._icon_names |= {"puzzle", "gear", "more"}
        self.plugins.icon_owners.update({n: "app" for n in self._icon_names})
        self._toolbar_defaults = {t.key: [a.objectName() for a in t.actions() if a.objectName()]
                                  for t in self.toolbars}
        self.apply_toolbar_order()

    # ---- toolbar order (drag and drop) -----------------------------------------
    def apply_toolbar_order(self):
        from . import dragbar
        saved = self.cfg.data.get("toolbar_order")
        if isinstance(saved, dict) and dragbar.apply_order(self.toolbars, saved)                 and hasattr(self, "_tb2_full_width"):
            del self._tb2_full_width
            QTimer.singleShot(0, self._fit_toolbar)

    def save_toolbar_order(self):
        from . import dragbar
        saved = self.cfg.data.get("toolbar_order")
        self.cfg.data["toolbar_order"] = dragbar.merge_saved(self.toolbars, saved if isinstance(saved, dict) else {})
        self.cfg.save()

    def reset_toolbar_order(self):
        from . import dragbar
        order = {k: list(v) for k, v in self._toolbar_defaults.items()}
        acts = order["actions"]
        acts[acts.index("w_more"):acts.index("w_more")] = [
            a.objectName() for a in self.plugins.actions() if a.property("aoh_toolbar") and a.objectName()]
        self.cfg.data.pop("toolbar_order", None)
        self.cfg.save()
        dragbar.apply_order(self.toolbars, order)
        self.toolbar_changed()

    # ---- places panel: storage + shortcuts ----------------------------------
    def _device_key(self) -> str | None:
        if not self.conn:
            return None
        p = self.conn.profile
        return f"{p.username}@{p.host}:{p.port}"

    def shortcuts(self) -> list[dict]:
        """Shortcuts of the connected device ([{path, name}]), saved in the config."""
        key = self._device_key()
        if not key:
            return []
        allsc = self.cfg.data.setdefault("shortcuts", {})
        if key not in allsc:
            # migrate old global bookmarks to the first device used after the update
            legacy = self.cfg.data.pop("bookmarks", None) or []
            allsc[key] = [{"path": p, "name": posixpath.basename(p.rstrip("/")) or p} for p in legacy]
            self.cfg.save()
        return allsc[key]

    def _places_header(self, text: str):
        it = QListWidgetItem(text.upper())
        it.setFlags(Qt.ItemFlag.NoItemFlags)
        f = it.font()
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1.5))
        f.setBold(True)
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
        it.setFont(f)
        it.setForeground(QColor(theme.TEXT_FAINT))
        it.setData(Qt.ItemDataRole.UserRole + 1, "header")
        self.places.addItem(it)

    def _places_item(self, icon: QIcon, name: str, path: str, kind: str):
        it = QListWidgetItem(icon, name)
        it.setData(Qt.ItemDataRole.UserRole, path)
        it.setData(Qt.ItemDataRole.UserRole + 1, kind)
        it.setToolTip(path)
        self.places.addItem(it)

    def fill_places(self):
        self.places.clear()
        self._places_header("Storage")
        self._places_item(theme.line_icon("phone"), "Internal storage", INTERNAL_STORAGE, "storage")
        for label, path, icon in getattr(self, "volumes", []):
            self._places_item(theme.line_icon(icon), label, path, "storage")
        self._places_item(theme.line_icon("root"), "Root  /", "/", "storage")
        self._places_header("Shortcuts")
        scs = self.shortcuts()
        for sc in scs:
            self._places_item(self.icons.folder, sc.get("name") or sc["path"], sc["path"], "shortcut")
        if not scs:
            hint = QListWidgetItem("Right-click a folder →\n“Add to shortcuts”" if self.conn else "Connect to see shortcuts")
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            hint.setForeground(QColor(theme.TEXT_FAINT))
            f = hint.font()
            f.setItalic(True)
            hint.setFont(f)
            self.places.addItem(hint)
        self.sync_places()

    def sync_places(self):
        """Highlight the entry for the current folder, if there is one."""
        self.places.blockSignals(True)
        self.places.clearSelection()
        for i in range(self.places.count()):
            it = self.places.item(i)
            p = it.data(Qt.ItemDataRole.UserRole)
            if p and self.cwd and posixpath.normpath(p) == posixpath.normpath(self.cwd):
                it.setSelected(True)
                break
        self.places.blockSignals(False)

    def refresh_volumes(self):
        conn = self.conn
        if not conn:
            return

        def done(vols):
            if conn is self.conn:
                self.volumes = vols
                self.fill_places()
        Task.start(lambda: detect_volumes(conn), done,
                   lambda m: logging.getLogger("androunderthehood").info("volume detection failed: %s", m))

    def add_shortcut(self, path: str | None = None):
        path = path or self.cwd
        if not path or not self.conn:
            return
        scs = self.shortcuts()
        if any(sc["path"] == path for sc in scs):
            self.statusBar().showMessage(f"Already a shortcut: {path}", 4000)
            return
        scs.append({"path": path, "name": posixpath.basename(path.rstrip("/")) or path})
        self.cfg.save()
        self.fill_places()
        self.statusBar().showMessage(f"Added to shortcuts: {path}", 4000)

    def places_menu(self, pos):
        it = self.places.itemAt(pos)
        kind = it.data(Qt.ItemDataRole.UserRole + 1) if it else None
        m = QMenu(self)
        if kind == "shortcut":
            path = it.data(Qt.ItemDataRole.UserRole)
            m.addAction(theme.line_icon("forward"), "Open", lambda: self.navigate(path))
            m.addAction(theme.line_icon("rename"), "Rename shortcut…", lambda: self.rename_shortcut(path))
            row = [sc["path"] for sc in self.shortcuts()].index(path)
            up = m.addAction(theme.line_icon("up"), "Move up", lambda: self.move_shortcut(path, -1))
            down = m.addAction(theme.line_icon("down"), "Move down", lambda: self.move_shortcut(path, 1))
            up.setEnabled(row > 0)
            down.setEnabled(row < len(self.shortcuts()) - 1)
            m.addSeparator()
            m.addAction(theme.line_icon("trash"), "Remove shortcut", lambda: self.remove_shortcut(path))
        elif self.conn:
            m.addAction(theme.line_icon("refresh"), "Rescan SD card / USB drives", self.refresh_volumes)
        else:
            return
        m.exec(self.places.mapToGlobal(pos))

    def rename_shortcut(self, path: str):
        sc = next((s for s in self.shortcuts() if s["path"] == path), None)
        if not sc:
            return
        name, ok = QInputDialog.getText(self, "Rename shortcut", f"Name for {path}:", text=sc.get("name") or "")
        if ok and name.strip():
            sc["name"] = name.strip()
            self.cfg.save()
            self.fill_places()

    def move_shortcut(self, path: str, delta: int):
        scs = self.shortcuts()
        i = [sc["path"] for sc in scs].index(path)
        j = i + delta
        if 0 <= j < len(scs):
            scs[i], scs[j] = scs[j], scs[i]
            self.cfg.save()
            self.fill_places()

    def remove_shortcut(self, path: str):
        key = self._device_key()
        if not key:
            return
        allsc = self.cfg.data.setdefault("shortcuts", {})
        allsc[key] = [sc for sc in allsc.get(key, []) if sc["path"] != path]
        self.cfg.save()
        self.fill_places()

    # ---- view state ------------------------------------------------------
    # ---- responsive toolbar ----------------------------------------------------
    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        QTimer.singleShot(0, self._fit_toolbar)

    def showEvent(self, ev):
        super().showEvent(ev)
        QTimer.singleShot(0, self._fit_toolbar)

    def _fit_toolbar(self):
        """Icon + text when there is room, icon-only (with tooltips) when the window is narrow."""
        tb = getattr(self, "tb2", None)
        if tb is None:
            return
        if not hasattr(self, "_tb2_full_width"):
            tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            tb.adjustSize()
            self._tb2_full_width = tb.sizeHint().width()
        want = (Qt.ToolButtonStyle.ToolButtonTextBesideIcon if self.width() - 24 >= self._tb2_full_width
                else Qt.ToolButtonStyle.ToolButtonIconOnly)
        if tb.toolButtonStyle() != want:
            tb.setToolButtonStyle(want)

    def set_view(self, which):
        self.stack.setCurrentIndex(0 if which == "icons" else 1)
        self.a_icons.setChecked(which == "icons")
        self.a_details.setChecked(which != "icons")
        self.size_slider.setEnabled(which == "icons")
        self.cfg.data["view"] = which
        self.cfg.save()
        QTimer.singleShot(50, self.bump_visible)

    def set_icon_size(self, s):
        self.icon_view.set_icon_size(s)
        self.cfg.data["icon_size"] = s
        self.cfg.save()

    def apply_filter(self, *_):
        self.proxy.set_filter(self.search.text(), self.hidden_cb.isChecked())
        self.cfg.data["show_hidden"] = self.hidden_cb.isChecked()
        self.cfg.save()
        self.update_status()

    def toggle_thumbs(self, on):
        self.thumbs.enabled = on
        if on:
            self.request_thumbs()
        else:
            self.thumbs.cancel()

    def active_view(self):
        return self.stack.currentWidget()

    def update_actions(self):
        c = self.conn is not None
        for a in (self.a_disconnect, self.a_up, self.a_refresh, self.a_download, self.a_upload, self.a_mkdir,
                  self.a_rename, self.a_delete, self.a_bookmark, self.a_shell, self.a_copy_path,
                  self.a_mirror, self.a_props, self.a_apps, self.a_parts, self.a_logcat, self.a_device,
                  self.a_sysfs, self.a_modem, self.a_rootmgr):
            a.setEnabled(c)
        # root tools are hidden while Root is off (ADB's shell user can still use some of them)
        root = self.a_root.isChecked() and (not c or bool(self.conn.is_root or self.conn.native_root))
        caps = self.conn.caps if c else set()
        sh = "shell" in caps   # ADB shell user, or AndroOpenHeart running commands through Shizuku
        changed = False
        for a, show in ((self.a_parts, root), (self.a_props, root), (self.a_sysfs, root or sh), (self.a_rootmgr, root),
                        (self.a_modem, root and "diag" in caps),
                        (self.a_device, root or sh or "dump" in caps),
                        (self.a_apps, root or sh or "packages" in caps),
                        (self.a_logcat, root or sh or "logs" in caps),
                        (self.a_mirror, root or sh), (self.a_mirror_w, root or sh)):
            if a.isVisible() != show:
                a.setVisible(show)
                changed = True
            if not show:
                a.setEnabled(False)
        if changed and hasattr(self, "_tb2_full_width"):
            del self._tb2_full_width      # toolbar width changed: re-measure for icon/text mode
            QTimer.singleShot(0, self._fit_toolbar)
        if getattr(self, "plugins", None):
            self.plugins.update_actions(c, root, root or sh)
        self.a_back.setEnabled(c and bool(self.back_stack))
        self.a_fwd.setEnabled(c and bool(self.fwd_stack))
        self.places.setEnabled(c)

    def toolbar_changed(self):
        """Buttons were added / removed (plugins): re-measure the toolbar for icon / text mode."""
        if getattr(self, "toolbars", None):
            from . import dragbar
            saved = self.cfg.data.get("toolbar_order")
            if isinstance(saved, dict):
                dragbar.apply_order(self.toolbars, saved)
        if hasattr(self, "_tb2_full_width"):
            del self._tb2_full_width
        QTimer.singleShot(0, self._fit_toolbar)

    def update_status(self):
        n = self.proxy.rowCount()
        sel = self.selected_entries()
        msg = f"{n} items"
        if sel:
            sz = sum(T.file_size(e) for e in sel if not e.is_dir)
            msg += f"   |   {len(sel)} selected ({human_size(sz)})"
        self.statusBar().showMessage(msg)

    # ---- connection ----------------------------------------------------
    def connect_dialog(self):
        dlg = ConnectDialog(self, self.cfg)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if dlg.use_adb:
            self.adb_dialog()
            return
        prof = dlg.profile()
        if dlg.save_cb.isChecked():
            self.cfg.upsert_profile(prof)
        self.start_connect(prof, dlg.password.text(), dlg.passphrase.text())

    def _apply_root(self, prof: B.Profile) -> B.Profile:
        """Copy of `prof` adjusted for the Root toggle (off = never use su)."""
        p = B.Profile.from_dict(prof.to_dict())
        if self.a_root.isChecked():
            if p.mode == B.MODE_SFTP:
                p.mode = B.MODE_SU_SFTP          # falls back to root shell commands if sftp-server is missing
            if not p.su_command.strip():
                p.su_command = "su -c"
        else:
            if p.mode == B.MODE_SU_SFTP:
                p.mode = B.MODE_SFTP
            p.su_command = ""
        return p

    def toggle_root(self, on: bool, path: str | None = None):
        if not self.conn:
            self.statusBar().showMessage(f"Root {'on' if on else 'off'} – applies to the next connection.", 6000)
            self.update_actions()
            return
        path = path or self.cwd or None
        if not on and path and not path.startswith(("/sdcard", "/storage", "/mnt/media_rw")):
            path = None   # a root-only folder would be unreadable now: go back to the start folder
        if getattr(self.conn, "is_adb", False):
            self.start_adb_connection(self.conn.serial, self.conn.profile.name, path)
            return
        prof = B.Profile.from_dict(getattr(self, "_base_prof", self.conn.profile).to_dict())
        if path:
            prof.start_dir = path
        elif not on:
            prof.start_dir = "/sdcard"
        password, passphrase = getattr(self, "_secret", ("", ""))
        self.start_connect(prof, password, passphrase)

    def start_connect(self, prof: B.Profile, password: str, passphrase: str):
        self.disconnect()
        self._base_prof = B.Profile.from_dict(prof.to_dict())
        prof = self._apply_root(prof)
        want_root = self.a_root.isChecked()
        conn = B.Connection(prof)
        prog = QProgressDialog(f"Connecting to {prof.host}:{prof.port}…", "Cancel", 0, 0, self)
        prog.setWindowTitle(APP_NAME)
        prog.setMinimumDuration(0)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.show()
        cancelled = [False]
        prog.canceled.connect(lambda: (cancelled.__setitem__(0, True), conn.close()))

        def fail(msg):
            close_progress(prog)
            conn.close()
            if not cancelled[0]:
                QMessageBox.critical(self, APP_NAME, f"Connection failed:\n\n{msg}")

        self._secret = (password, passphrase)  # kept in memory only, for "reconnect as root"
        note = [""]

        def phase2():
            conn.authenticate(password, passphrase)
            conn.probe_exec()
            conn.detect_caps()
            is_root = conn.check_root() if prof.mode != B.MODE_SFTP else None
            try:
                fs = conn.new_fs()
            except Exception as e:  # noqa: BLE001
                if prof.mode == B.MODE_SHELL:
                    raise
                # no usable SFTP on the device: fall back to shell commands (via su if needed)
                logging.getLogger("androunderthehood").info("SFTP unavailable (%r) - falling back to shell", e)
                note[0] = f"SFTP unavailable ({(str(e) or type(e).__name__).splitlines()[0]}); using shell mode instead."
                was_plain = prof.mode == B.MODE_SFTP
                prof.mode = B.MODE_SHELL
                is_root = conn.check_root()
                if was_plain and not is_root:
                    prof.su_command = ""  # user didn't ask for su; browse as the login user
                fs = conn.new_fs()
            try:
                start = fs.realpath(prof.start_dir or "/")
            except Exception as ex:  # noqa: BLE001
                logging.getLogger("androunderthehood").warning("realpath %s failed: %r", prof.start_dir, ex)
                start = "/"
            return fs, start, is_root

        def connected(r):
            close_progress(prog)
            if cancelled[0]:
                conn.close()
                return
            fs, start, is_root = r
            logging.getLogger("androunderthehood").info("connected mode=%s root=%s start=%s note=%s sftp_server=%s",
                                                  prof.mode, is_root, start, note[0], conn.sftp_server_path)
            self.conn, self.fs = conn, fs
            self.thumbs.set_connection(conn)
            self.thumb_info.clear()
            self.back_stack.clear()
            self.fwd_stack.clear()
            who = prof.username
            if conn.native_root:
                who += " → root" if want_root else " (server runs commands as root)"
            elif want_root:
                who += " → root" if is_root else " (su FAILED – not root)"
            unpriv = ("shell user via Shizuku" if conn.openheart_mode == "shizuku" else
                      "shell user" if "shell" in conn.caps else
                      "app user" if conn.openheart_mode == "app" else "shell")
            mode = {"sftp": "SFTP", "su-sftp": "root SFTP",
                    "shell": "root shell" if (is_root or conn.native_root) else unpriv}[prof.mode]
            if conn.use_pty:
                mode += " (interactive)"
            self.status_conn.setText(f"  {who}@{prof.host}:{prof.port}  [{mode}]  ")
            self.setWindowTitle(f"{prof.name} – {APP_NAME}")
            self.update_actions()
            self.volumes = []
            self.fill_places()
            self.refresh_volumes()
            if not want_root and conn.native_root:
                note[0] = ("Root is off, but this SSH server runs every command as root (root login, or "
                           "AndroOpenHeart's own “run as root” setting).")
            if note[0]:
                self.statusBar().showMessage(note[0], 15000)
            if want_root and not is_root and not conn.native_root:
                QMessageBox.warning(self, APP_NAME,
                                    "Connected, but the su command did not give root (id -u ≠ 0).\n"
                                    "Grant the SSH app root access in Magisk / KernelSU, or check the su command.")
            self.navigate(start)
            self.plugins.emit("connected", conn)

        def key_received(key):
            if cancelled[0]:
                return
            kh = B.KnownHosts()
            state = kh.check(prof.host, prof.port, key)
            fp = B.fingerprint(key)
            if state == "changed":
                r = QMessageBox.warning(
                    self, "HOST KEY CHANGED",
                    f"WARNING: the host key for {prof.host}:{prof.port} has CHANGED.\n\n"
                    f"New key: {key.get_name()} {fp}\n\nThis could mean someone is intercepting the "
                    "connection – or the SSH server was reinstalled.\n\nTrust the new key?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if r != QMessageBox.StandardButton.Yes:
                    fail("Host key rejected")
                    return
                kh.add(prof.host, prof.port, key)
            elif state == "unknown":
                r = QMessageBox.question(
                    self, "New device",
                    f"First connection to {prof.host}:{prof.port}.\n\nHost key fingerprint:\n"
                    f"{key.get_name()} {fp}\n\nTrust this device and continue?")
                if r != QMessageBox.StandardButton.Yes:
                    fail("Host key not trusted")
                    return
                kh.add(prof.host, prof.port, key)
            prog.setLabelText("Authenticating…")
            Task.start(phase2, connected, fail)

        Task.start(conn.open, key_received, fail)

    # ---- ADB transport ---------------------------------------------------------
    def adb_dialog(self):
        AdbConnectDialog(self).exec()

    def start_adb_connection(self, serial: str, name: str | None = None, start_dir: str | None = None):
        from . import adbconn
        self.disconnect()
        want_root = self.a_root.isChecked()
        prof = B.Profile(name=name or serial, host=serial, port=0, username="adb", mode=B.MODE_SHELL,
                         su_command="su -c" if want_root else "", start_dir=start_dir or "/sdcard")
        conn = adbconn.AdbConnection(prof)
        prog = QProgressDialog(f"Connecting to {serial} over ADB…", "Cancel", 0, 0, self)
        prog.setWindowTitle(APP_NAME)
        prog.setMinimumDuration(0)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.show()

        def work():
            conn.open()
            conn.probe_exec()
            conn.detect_caps()
            is_root = conn.check_root()
            if not is_root:
                prof.su_command = ""   # no root: browse as the shell user
            fs = conn.new_fs()
            try:
                start = fs.realpath(prof.start_dir)
            except Exception:  # noqa: BLE001
                start = "/"
            return fs, start, is_root

        def done(r):
            close_progress(prog)
            fs, start, is_root = r
            self.conn, self.fs = conn, fs
            self.thumbs.set_connection(conn)
            self.thumb_info.clear()
            self.back_stack.clear()
            self.fwd_stack.clear()
            self.status_conn.setText(f"  adb {serial}{' → root' if is_root else ' (shell user)'}  [ADB]  ")
            self.setWindowTitle(f"{prof.name} – {APP_NAME}")
            self.update_actions()
            self.volumes = []
            self.fill_places()
            self.refresh_volumes()
            if want_root and not is_root:
                QMessageBox.information(self, APP_NAME, "Connected over ADB without root (su was not granted). "
                                        "Grant root to “Shell” in Magisk for full access.")
            self.navigate(start)
            self.plugins.emit("connected", conn)

        def fail(m):
            close_progress(prog)
            QMessageBox.critical(self, APP_NAME, f"ADB connection failed:\n\n{m}")
        Task.start(work, done, fail)

    def setup_openheart(self, serial: str, parent=None):
        """Install + configure AndroOpenHeart over ADB, then connect AndroUnderTheHood to it over SSH."""
        from . import openheart_setup as R
        dlg = SetupLogDialog(parent or self, "Set up AndroOpenHeart")
        dlg.show()
        sig = dlg.sig

        def work():
            return R.setup(serial, lambda line: sig.line.emit(line))

        def done(res):
            dlg.finish(True, f"Done – connecting over SSH to {res['host']}:{res['port']} ({res['via']}).")
            prof = B.Profile(name=res["name"], host=res["host"], port=res["port"], username=res["username"],
                             key_path=res["key_path"], mode=B.MODE_SFTP, start_dir="/sdcard")
            self.cfg.upsert_profile(prof)
            QTimer.singleShot(600, lambda: self.start_connect(prof, "", ""))
            if res.get("shizuku_boot") == "ready":
                QTimer.singleShot(1500, lambda: self.offer_shizuku_pairing(serial, dlg))

        def fail(m):
            dlg.finish(False, f"Setup stopped: {m}")
        Task.start(work, done, fail)

    def offer_shizuku_pairing(self, serial: str, parent=None):
        """No root: Shizuku stops at every reboot. Paired once with Wireless debugging, it starts itself."""
        if QMessageBox.question(
                parent or self, "Shizuku start on boot",
                "Without root, Shizuku stops when the phone reboots, and AndroOpenHeart loses shell access "
                "until it is started again.\n\n"
                "Shizuku can start itself after a reboot if it is paired with Wireless debugging once. "
                "This opens the pairing screens on the phone; you type a 6-digit code shown there into "
                "Shizuku's notification. The phone must be on Wi-Fi (also at every boot).\n\n"
                "Pair Shizuku now?") != QMessageBox.StandardButton.Yes:
            return
        from . import openheart_setup as R
        dlg = SetupLogDialog(parent or self, "Pair Shizuku with Wireless debugging")
        dlg.show()
        sig = dlg.sig
        Task.start(lambda: R.pair_shizuku(serial, lambda line: sig.line.emit(line)),
                   lambda ok: dlg.finish(True, "Done – Shizuku will start itself after a reboot (on Wi-Fi)."),
                   lambda m: dlg.finish(False, f"Pairing stopped: {m}"))

    def open_device(self):
        if not self.conn:
            return
        from .device import DeviceDialog
        DeviceDialog(self, self.conn).show()

    def disconnect(self):
        self.list_gen += 1   # ignore folder listings still running on the old connection
        if self.conn:
            self.plugins.emit("disconnected")
            self.thumbs.set_connection(None)
            conn = self.conn
            self.conn = None
            self.fs = None
            threading.Thread(target=conn.close, daemon=True).start()
        self.model.set_entries([])
        self.cwd = ""
        self.addr.setText("")
        self.status_conn.setText("Not connected")
        self.setWindowTitle(APP_NAME)
        self.props.show_entry(None, 0)
        self.volumes = []
        self.fill_places()
        self.update_actions()

    # ---- navigation ------------------------------------------------------
    def navigate(self, path: str, record=True):
        if not self.fs or not path:
            return
        if not path.startswith("/"):
            path = posixpath.join(self.cwd or "/", path)
        path = posixpath.normpath(path)
        if path.startswith("//"):
            path = path[1:]
        self.list_gen += 1
        gen = self.list_gen
        fs = self.fs
        self.statusBar().showMessage(f"Loading {path}…")
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        prev = self.cwd

        def done(entries):
            QApplication.restoreOverrideCursor()
            logging.getLogger("androunderthehood").info("listdir %s -> %d entries: %s", path, len(entries),
                                                  ", ".join(e.name for e in entries[:25]))
            if gen != self.list_gen:
                return
            if record and prev and prev != path:
                self.back_stack.append(prev)
                self.fwd_stack.clear()
            self.cwd = path
            self.addr.setText(path)
            self.model.set_entries(entries)
            self.plugins.emit("folder_changed", path)
            for v in (self.icon_view, self.detail_view):
                v.scrollToTop()
            self.props.show_entry(None, 0)
            self.update_actions()
            self.update_status()
            self.sync_places()
            self.request_thumbs()

        def fail(msg):
            QApplication.restoreOverrideCursor()
            if gen != self.list_gen:
                return
            self.addr.setText(self.cwd)
            self.statusBar().showMessage(f"Cannot open {path}: {msg}", 8000)
            logging.getLogger("androunderthehood").warning("listdir %s failed: %s (mode=%s)", path, msg,
                                                     self.conn.profile.mode if self.conn else None)
            if self.conn and not self.a_root.isChecked() and not self.conn.native_root and \
                    ("denied" in msg.lower() or "errno 13" in msg.lower()):
                r = QMessageBox.question(
                    self, APP_NAME,
                    f"Permission denied: {path}\n\nRoot is off.\nTurn Root on and open this folder?\n\n"
                    "Approve the root request on the phone (Magisk / KernelSU) if it asks.")
                if r == QMessageBox.StandardButton.Yes:
                    self.reconnect_as_root(path)
                return
            QMessageBox.warning(self, APP_NAME, f"Cannot open {path}\n\n{msg}")
        Task.start(lambda: fs.listdir(path), done, fail)

    def reconnect_as_root(self, path: str):
        self.a_root.setChecked(True)
        self.toggle_root(True, path)

    def refresh(self):
        if self.cwd:
            self.navigate(self.cwd, record=False)

    def go_up(self):
        if self.cwd and self.cwd != "/":
            self.navigate(posixpath.dirname(self.cwd.rstrip("/")) or "/")

    def go_back(self):
        if self.back_stack:
            self.fwd_stack.append(self.cwd)
            self.navigate(self.back_stack.pop(), record=False)

    def go_forward(self):
        if self.fwd_stack:
            self.back_stack.append(self.cwd)
            self.navigate(self.fwd_stack.pop(), record=False)

    def on_activate(self, index):
        e = self.proxy.data(index, Qt.ItemDataRole.UserRole)
        if e is None:
            return
        if e.is_dir:
            self.navigate(e.path)
        else:
            self.open_entry(e)

    # ---- thumbnails ------------------------------------------------------
    def request_thumbs(self):
        if not self.thumbs.enabled:
            return
        ents = [self.proxy.data(self.proxy.index(r, 0), Qt.ItemDataRole.UserRole)
                for r in range(self.proxy.rowCount())]
        self.thumbs.request([e for e in ents if e])
        QTimer.singleShot(60, self.bump_visible)

    def bump_visible(self):
        v = self.active_view()
        vp = v.viewport().rect()
        paths = []
        first = v.indexAt(QPoint(4, 4))
        start = first.row() if first.isValid() else 0
        for r in range(start, min(start + 400, self.proxy.rowCount())):
            idx = self.proxy.index(r, 0)
            rect = v.visualRect(idx)
            if rect.top() > vp.bottom():
                break
            if rect.intersects(vp):
                e = self.proxy.data(idx, Qt.ItemDataRole.UserRole)
                if e:
                    paths.append(e.path)
        self.thumbs.bump(paths)

    def on_thumb(self, path, img, info):
        if info:
            self.thumb_info[path] = info
        self.model.set_thumb(path, img)
        if self.props.entry and self.props.entry.path == path:
            if not img.isNull():
                self.props.set_preview(self.model.thumbs.get(path))
            self.props.add_info(path, info)

    def on_thumb_progress(self, left):
        self.status_thumbs.setText(f"  thumbnails: {left} queued  " if left else "")

    def clear_cache(self):
        size = self.thumbs.cache.usage()
        if QMessageBox.question(self, APP_NAME, f"Delete the local thumbnail cache ({human_size(size)})?") \
                == QMessageBox.StandardButton.Yes:
            self.thumbs.cache.clear()
            self.thumb_info.clear()
            self.refresh()

    # ---- selection / properties ----------------------------------------
    def selected_entries(self) -> list[B.Entry]:
        rows = self.icon_view.selectionModel().selectedRows(0)
        if not rows:
            rows = [i for i in self.icon_view.selectionModel().selectedIndexes() if i.column() == 0]
        return [e for e in (self.proxy.data(i, Qt.ItemDataRole.UserRole) for i in rows) if e]

    def on_selection(self, *_):
        sel = self.selected_entries()
        self.props.show_entry(sel[0] if len(sel) == 1 else None, len(sel))
        if len(sel) > 1:
            files = [e for e in sel if not e.is_dir]
            self.props.extra = {"Files": str(len(files)), "Folders": str(len(sel) - len(files)),
                                "Total size (files)": human_size(sum(T.file_size(e) for e in files))}
            self.props.table.setRowCount(0)
            rows = list(self.props.extra.items())
            self.props.table.setRowCount(len(rows))
            for i, (k, v) in enumerate(rows):
                self.props.table.setItem(i, 0, QTableWidgetItem(k))
                self.props.table.setItem(i, 1, QTableWidgetItem(v))
        self.update_status()

    def fetch_dir_summary(self, e: B.Entry):
        fs = self.fs

        def work():
            items = fs.listdir(e.path)
            d = sum(1 for x in items if x.is_dir)
            return {"Contains": f"{len(items) - d} files, {d} folders (direct)"}
        Task.start(work, lambda info: self.props.add_info(e.path, info),
                   lambda msg: self.props.add_info(e.path, {"Contents": f"unreadable: {msg}"}))

    def fetch_text_preview(self, e: B.Entry):
        fs = self.fs
        n = min(T.file_size(e), 16 * 1024)
        if n <= 0:
            return

        def work():
            data = fs.read_range(e.path, 0, n)
            if b"\x00" in data[:4096]:
                return ""
            return data.decode("utf-8", errors="replace")
        Task.start(work, lambda txt: self.props.set_text(e.path, txt), lambda m: None)

    def copy_path(self):
        sel = self.selected_entries()
        txt = "\n".join(e.path for e in sel) if sel else self.cwd
        QApplication.clipboard().setText(txt)
        self.statusBar().showMessage("Copied: " + txt.splitlines()[0] if txt else "", 3000)

    # ---- context menu ------------------------------------------------
    def context_menu(self, pos):
        if not self.conn:
            return
        v = self.active_view()
        idx = v.indexAt(pos)
        if idx.isValid() and not v.selectionModel().isSelected(idx):
            v.selectionModel().select(idx, v.selectionModel().SelectionFlag.ClearAndSelect |
                                      v.selectionModel().SelectionFlag.Rows)
        sel = self.selected_entries()
        m = QMenu(self)
        if len(sel) == 1:
            e = sel[0]
            m.addAction("Open folder" if e.is_dir else "Open", lambda: self.on_activate(idx) if idx.isValid() else None)
            if not e.is_dir and e.is_regular:
                ow = m.addMenu(theme.line_icon("forward"), "Open with")
                self.fill_open_with(ow, lambda app, e=e: self.open_entry(e, app))
            if not e.is_dir and (e.is_regular or statmod.S_ISBLK(e.mode)):
                m.addAction(theme.line_icon("list"), "Open in hex editor", lambda e=e: self.open_hex_entry(e))
            if e.is_link and e.link_target:
                tgt = e.link_target if e.link_target.startswith("/") else posixpath.join(self.cwd, e.link_target)
                m.addAction("Go to link target", lambda: self.navigate(tgt if e.target_is_dir else posixpath.dirname(tgt)))
        if sel:
            m.addAction(self.a_download)
            m.addSeparator()
            if len(sel) == 1:
                m.addAction(self.a_rename)
            m.addAction(self.a_delete)
            m.addAction(self.a_copy_path)
            if len(sel) == 1 and not sel[0].is_dir:
                m.addAction("Regenerate thumbnail", lambda: self.regen_thumb(sel[0]))
            dirs = [e for e in sel if e.is_dir]
            if dirs:
                m.addSeparator()
                m.addAction(theme.line_icon("star"),
                            "Add to shortcuts" if len(dirs) == 1 else f"Add {len(dirs)} folders to shortcuts",
                            lambda: [self.add_shortcut(e.path) for e in dirs])
            m.addSeparator()
        else:
            m.addAction(theme.line_icon("star"), "Add this folder to shortcuts", lambda: self.add_shortcut())
            m.addSeparator()
        extra = self.plugins.file_actions(sel)
        if extra:
            for title, fn, icon, api in extra:
                m.addAction(api.icon(icon) if icon else QIcon(), title, fn)
            m.addSeparator()
        m.addAction(self.a_upload)
        m.addAction(self.a_mkdir)
        m.addAction(self.a_refresh)
        m.exec(v.viewport().mapToGlobal(pos))

    def regen_thumb(self, e):
        key = self.thumbs.cache.key(self.thumbs.host_key(), e, THUMB_SIZE)
        p = os.path.join(self.thumbs.cache.dir, key[:2], key)
        for ext in (".png", ".json"):
            try:
                os.remove(p + ext)
            except OSError:
                pass
        with self.thumbs.cond:
            self.thumbs.queue.appendleft((self.thumbs.gen, e))
            self.thumbs.cond.notify()

    # ---- file operations ---------------------------------------------
    def _transfer(self, title, total, job, on_done=None):
        """Run job(fs, progress_cb) on a dedicated FS session with a progress dialog."""
        conn = self.conn
        prog = QProgressDialog(title, "Cancel", 0, 1000, self)
        prog.setWindowTitle(APP_NAME)
        prog.setMinimumDuration(300)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        state = {"done": 0, "cancel": False, "label": title}
        prog.canceled.connect(lambda: state.__setitem__("cancel", True))
        timer = QTimer(prog)

        def tick():
            t = max(total[0], 1)
            prog.setValue(min(1000, int(state["done"] * 1000 / t)))
            prog.setLabelText(f"{state['label']}\n{human_size(state['done'])} of {human_size(total[0])}")
        timer.timeout.connect(tick)
        timer.start(150)

        def progress(n, label=None):
            if state["cancel"]:
                raise Cancelled()
            state["done"] += n
            if label:
                state["label"] = label

        def work():
            fs = conn.new_fs()
            try:
                return job(fs, progress)
            finally:
                fs.close()

        def finish(r=None):
            timer.stop()
            close_progress(prog)
            if on_done:
                on_done(r)

        def fail(msg):
            timer.stop()
            close_progress(prog)
            if not state["cancel"]:
                QMessageBox.warning(self, APP_NAME, f"{title} failed:\n\n{msg}")
            elif on_done:
                on_done(None)
        Task.start(work, finish, fail)

    def download_selected(self):
        sel = self.selected_entries()
        if not sel or not self.conn:
            return
        dest = QFileDialog.getExistingDirectory(self, "Download to folder",
                                                self.cfg.data.get("last_download", os.path.expanduser("~/Downloads")))
        if not dest:
            return
        self.cfg.data["last_download"] = dest
        self.cfg.save()
        total = [sum(T.file_size(e) for e in sel if not e.is_dir)]

        def job(fs, progress):
            n = 0
            for e in sel:
                local = os.path.join(dest, _safe_name(e.name))
                if e.is_dir:
                    os.makedirs(local, exist_ok=True)
                    items = list(fs.walk_files(e.path))
                    total[0] += sum(x.size for x, _ in items if not statmod.S_ISDIR(x.mode))
                    for x, rel in items:
                        lp = os.path.join(local, *[_safe_name(p) for p in rel.split("/")])
                        if statmod.S_ISDIR(x.mode):
                            os.makedirs(lp, exist_ok=True)
                        else:
                            os.makedirs(os.path.dirname(lp), exist_ok=True)
                            progress(0, x.path)
                            fs.download(x.path, lp, progress)
                            n += 1
                else:
                    progress(0, e.path)
                    fs.download(e.path, local, progress)
                    n += 1
            return n

        def done(n):
            if n is not None:
                self.statusBar().showMessage(f"Downloaded {n} file(s) to {dest}", 8000)
        self._transfer("Downloading", total, job, done)

    def open_entry(self, e: B.Entry, app: str | None = None):
        """Download to a temp folder and open with the default app, a chosen program, or
        Windows' "Open with" dialog (app="__choose__"). Saved changes can be uploaded back."""
        if e.is_dir:
            self.navigate(e.path)
            return
        if not self.conn:
            return
        host = _safe_name(f"{self.conn.profile.host}_{self.conn.profile.port}")
        d = os.path.join(tempfile.gettempdir(), "AndroUnderTheHood", "open", host,
                         *[_safe_name(p) for p in posixpath.dirname(e.path).strip("/").split("/") if p])
        os.makedirs(d, exist_ok=True)
        local = os.path.join(d, _safe_name(e.name))

        def job(fs, progress):
            progress(0, e.path)
            fs.download(e.path, local, progress)
            return local

        def done(p):
            if p and self.launch_local(p, app):
                self.watch_local(p, e.path)
        self._transfer(f"Opening {e.name}", [T.file_size(e)], job, done)

    # ---- external apps -------------------------------------------------------
    def launch_local(self, path: str, app: str | None) -> bool:
        import subprocess
        try:
            if not app:
                os.startfile(path)  # type: ignore[attr-defined]
            elif app == "__choose__":
                subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", path])
            else:
                subprocess.Popen([app, path])
            return True
        except OSError as ex:
            QMessageBox.warning(self, APP_NAME, f"Could not open {path}:\n{ex}")
            return False

    def fill_open_with(self, menu: QMenu, callback):
        """Programs the user added + Windows' chooser + add/remove entries."""
        progs = self.cfg.data.setdefault("open_with", [])
        prov = QFileIconProvider()
        for prog in progs:
            icon = prov.icon(QFileInfo(prog["exe"])) if os.path.exists(prog["exe"]) else QIcon()
            menu.addAction(icon, prog["name"], lambda x=prog["exe"]: callback(x))
        if progs:
            menu.addSeparator()
        menu.addAction(theme.line_icon("forward"), "Default app", lambda: callback(None))
        menu.addAction(theme.line_icon("more"), "Choose another app…", lambda: callback("__choose__"))
        menu.addSeparator()

        def add():
            exe, _ = QFileDialog.getOpenFileName(self, "Add a program to “Open with”",
                                                 os.environ.get("ProgramFiles", "C:\\"), "Programs (*.exe)")
            if not exe:
                return
            name = os.path.splitext(os.path.basename(exe))[0]
            name, ok = QInputDialog.getText(self, "Program name", "Name shown in the menu:", text=name)
            if not ok or not name.strip():
                return
            progs.append({"name": name.strip(), "exe": exe})
            self.cfg.save()
            callback(exe)
        menu.addAction(theme.line_icon("plus"), "Add program…", add)
        if progs:
            rm = menu.addMenu(theme.line_icon("trash"), "Remove program")
            for prog in progs:
                rm.addAction(prog["name"], lambda p=prog: (progs.remove(p), self.cfg.save()))

    def watch_local(self, local: str, remote: str):
        """Offer to upload the file when an external app saves it."""
        from PySide6.QtCore import QFileSystemWatcher
        if not hasattr(self, "watcher"):
            self.watcher = QFileSystemWatcher(self)
            self.watcher.fileChanged.connect(self._local_changed)
            self.watched: dict[str, dict] = {}
            self._changed_pending: set[str] = set()
        try:
            st = os.stat(local)
        except OSError:
            return
        self.watched[local] = {"remote": remote, "mtime": st.st_mtime, "size": st.st_size,
                               "device": self._device_key()}
        if local not in self.watcher.files():
            self.watcher.addPath(local)

    def _local_changed(self, path: str):
        self._changed_pending.add(path)
        QTimer.singleShot(1200, self._process_local_changes)   # editors write in several steps

    def _process_local_changes(self):
        pending, self._changed_pending = self._changed_pending, set()
        for local in pending:
            info = self.watched.get(local)
            if not info:
                continue
            if not os.path.exists(local):
                continue
            if local not in self.watcher.files():   # replaced via rename by the editor
                self.watcher.addPath(local)
            st = os.stat(local)
            if st.st_mtime == info["mtime"] and st.st_size == info["size"]:
                continue
            info["mtime"], info["size"] = st.st_mtime, st.st_size
            if not self.conn or self._device_key() != info["device"]:
                self.statusBar().showMessage(f"{os.path.basename(local)} changed, but its device is not "
                                             "connected – not uploaded", 8000)
                continue
            r = QMessageBox.question(
                self, "File changed",
                f"“{os.path.basename(local)}” was saved on this PC.\n\nUpload it to the device and replace\n"
                f"{info['remote']} ?")
            if r != QMessageBox.StandardButton.Yes:
                continue
            remote = info["remote"]

            def job(fs, progress, local=local, remote=remote):
                progress(0, remote)
                fs.upload(local, remote, progress)
                return remote
            self._transfer("Uploading changes", [st.st_size], job,
                           lambda r: (self.statusBar().showMessage(f"Uploaded {r}", 6000), self.refresh())
                           if r else None)

    # ---- hex editor ------------------------------------------------------------
    def open_hex_entry(self, e: B.Entry):
        if not self.conn:
            return
        conn = self.conn
        is_block = statmod.S_ISBLK(e.mode)
        path = e.path

        def work():
            if is_block or (e.is_link and not e.target_is_dir):
                rc, out, _ = conn.exec(f"blockdev --getsize64 {B.shq(path)} 2>/dev/null || stat -L -c %s "
                                       f"{B.shq(path)}", timeout=30)
                s = out.strip().split()
                return int(s[0]) if s and s[0].isdigit() else 0
            return T.file_size(e)

        def done(size):
            if size <= 0:
                QMessageBox.warning(self, APP_NAME, f"{path} is empty or its size is unknown.")
                return
            self.open_hex(path, size, readonly=is_block, title=e.name)
        Task.start(work, done, lambda m: QMessageBox.warning(self, APP_NAME, m))

    def open_hex(self, path: str, size: int, readonly: bool, title: str | None = None):
        from .hexedit import HexDialog
        HexDialog(self, self.conn, path, size, readonly, title=title).show()

    def upload_dialog(self):
        if not self.cwd:
            return
        files, _ = QFileDialog.getOpenFileNames(self, f"Upload to {self.cwd}")
        if files:
            self.upload_paths(files)

    def upload_paths(self, paths: list[str]):
        if not self.conn or not self.cwd:
            return
        cwd = self.cwd
        plan: list[tuple[str, str | None]] = []  # (local, remote) ; remote None for dirs
        total = 0
        for p in paths:
            if os.path.isdir(p):
                base = os.path.basename(p.rstrip("\\/"))
                for root, dirs, files in os.walk(p):
                    rel = os.path.relpath(root, p)
                    rdir = posixpath.join(cwd, base, *([] if rel == "." else rel.split(os.sep)))
                    plan.append((rdir, None))
                    for f in files:
                        lp = os.path.join(root, f)
                        plan.append((lp, posixpath.join(rdir, f)))
                        total += os.path.getsize(lp)
            elif os.path.isfile(p):
                plan.append((p, posixpath.join(cwd, os.path.basename(p))))
                total += os.path.getsize(p)
        if not plan:
            return
        n_files = sum(1 for _, r in plan if r)
        if QMessageBox.question(self, APP_NAME, f"Upload {n_files} file(s) ({human_size(total)}) to\n{cwd} ?") \
                != QMessageBox.StandardButton.Yes:
            return

        def job(fs, progress):
            for local, remote in plan:
                if remote is None:
                    try:
                        fs.mkdir(local)
                    except Exception:  # noqa: BLE001  (already exists)
                        pass
                else:
                    progress(0, remote)
                    fs.upload(local, remote, progress)
            return True
        self._transfer("Uploading", [total], job, lambda r: self.refresh())

    def new_folder(self):
        if not self.fs or not self.cwd:
            return
        name, ok = QInputDialog.getText(self, "New folder", f"Folder name in {self.cwd}:")
        if not ok or not name.strip():
            return
        path = posixpath.join(self.cwd, name.strip())
        fs = self.fs
        Task.start(lambda: fs.mkdir(path), lambda _: self.refresh(),
                   lambda m: QMessageBox.warning(self, APP_NAME, f"Could not create folder:\n{m}"))

    def rename_selected(self):
        sel = self.selected_entries()
        if len(sel) != 1 or not self.fs:
            return
        e = sel[0]
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=e.name)
        if not ok or not name.strip() or name == e.name:
            return
        new = posixpath.join(posixpath.dirname(e.path), name.strip())
        fs = self.fs
        Task.start(lambda: fs.rename(e.path, new), lambda _: self.refresh(),
                   lambda m: QMessageBox.warning(self, APP_NAME, f"Rename failed:\n{m}"))

    def delete_selected(self):
        sel = self.selected_entries()
        if not sel or not self.fs:
            return
        names = "\n".join(e.path for e in sel[:12]) + ("\n…" if len(sel) > 12 else "")
        dirs = sum(1 for e in sel if e.is_dir and not e.is_link)
        warn = f"\n\n{dirs} folder(s) will be deleted with ALL their contents." if dirs else ""
        if QMessageBox.warning(self, "Delete", f"Permanently delete {len(sel)} item(s) on the device?\n\n{names}{warn}\n\n"
                               "This cannot be undone.",
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                               QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        fs = self.fs

        def work():
            for e in sel:
                fs.remove(e.path, e.is_dir and not e.is_link)
        Task.start(work, lambda _: self.refresh(),
                   lambda m: (QMessageBox.warning(self, APP_NAME, f"Delete failed:\n{m}"), self.refresh()))

    def run_command(self):
        if not self.conn:
            return
        dlg = CommandDialog(self, self.conn, self.cwd)
        dlg.show()

    def open_logcat(self):
        if not self.conn:
            return
        from .logcat import LogcatDialog
        LogcatDialog(self, self.conn).show()

    def open_partitions(self):
        if not self.conn:
            return
        from .partitions import PartitionsDialog
        PartitionsDialog(self, self.conn).show()

    def open_apps(self):
        if not self.conn:
            return
        from .apps import AppsDialog
        AppsDialog(self, self.conn).show()

    def open_plugins(self):
        from .pluginsui import PluginsDialog
        PluginsDialog(self).show()

    def open_flashtools(self):
        from .flashtoolsui import FlashToolsDialog
        FlashToolsDialog(self).show()

    def open_drivers(self):
        from .driversui import DriversDialog
        DriversDialog(self).show()

    def open_rootmgr(self):
        if not self.conn:
            return
        from .rootmgrui import RootManagerDialog
        RootManagerDialog(self, self.conn).show()

    def open_modem(self):
        if not self.conn:
            return
        from .diagui import DiagDialog
        DiagDialog(self, self.conn).show()

    def open_sysfs(self):
        if not self.conn:
            return
        from .sysfs import SysfsDialog
        SysfsDialog(self, self.conn).show()

    def open_props(self):
        if not self.conn:
            return
        from .props import PropsDialog
        dlg = PropsDialog(self, self.conn)
        dlg.show()

    # ---- screen mirror (scrcpy-ssh) -----------------------------------
    def locate_scrcpy(self) -> str | None:
        p, _ = QFileDialog.getOpenFileName(
            self, "Locate scrcpy.exe of the scrcpy-ssh build",
            os.path.join(os.path.expanduser("~"), "Desktop"), "scrcpy (scrcpy.exe)")
        if not p:
            return None
        if not M.supports_ssh(p):
            QMessageBox.warning(self, APP_NAME, "That scrcpy.exe has no --ssh option.\n"
                                "Pick the scrcpy-ssh build (e.g. Desktop\\scrcpy-ssh\\scrcpy.exe).")
            return None
        self.cfg.data["scrcpy_path"] = p
        self.cfg.save()
        return p

    def show_mirror_log(self):
        txt = M.log_tail(60) or "(no log yet)"
        dlg = QDialog(self)
        dlg.setWindowTitle("Screen mirror log")
        dlg.resize(760, 420)
        lay = QVBoxLayout(dlg)
        t = QPlainTextEdit(txt)
        t.setReadOnly(True)
        t.setFont(QFont("Consolas", 9))
        lay.addWidget(t)
        dlg.show()

    def start_mirror(self, adb_serial: str | None = None):
        if not self.conn:
            return
        prof = self.conn.profile
        if adb_serial is None and getattr(self.conn, "is_adb", False):
            adb_serial = self.conn.serial
        scrcpy = M.find_scrcpy(self.cfg.data.get("scrcpy_path"))
        if not scrcpy:
            QMessageBox.information(self, APP_NAME, "Screen mirroring uses the scrcpy-ssh build.\n"
                                    "Please locate its scrcpy.exe.")
            scrcpy = self.locate_scrcpy()
            if not scrcpy:
                return
        known_hosts = B.KnownHosts().path
        opts = self.cfg.data.get("mirror_opts", {})
        extra = [arg for key, _l, args in M.MIRROR_OPTIONS if opts.get(key) for arg in args]
        self.statusBar().showMessage("Starting screen mirror…")
        logging.getLogger("androunderthehood").info("mirror: %s %s", scrcpy, extra)

        def work():
            if adb_serial:   # plain scrcpy over ADB (USB or adb connect)
                return True, M.launch_adb(scrcpy, adb_serial, prof.name, extra)
            ok, msg = M.check_key_login(prof, known_hosts)
            if not ok:
                return False, msg
            conn = self.conn
            if conn and conn.alive:
                # copies left by another user (e.g. pushed while the server ran as root) can't be
                # overwritten by scrcpy's push; the folder's owner (shell) may still delete them
                try:
                    conn.exec("cd /data/local/tmp 2>/dev/null && for f in scrcpy-server.jar scrcpy-ssh-relay.jar; "
                              "do [ -e $f ] && [ ! -O $f ] && rm -f $f; done; true", timeout=20, as_root=False)
                except Exception as e:  # noqa: BLE001
                    logging.getLogger("androunderthehood").info("mirror pre-clean failed: %r", e)
            return True, M.launch(scrcpy, prof, known_hosts, extra)

        def done(r):
            ok, val = r
            if not ok:
                self.statusBar().clearMessage()
                self.mirror_login_help(prof, val)
                return
            self.mirror_procs = [p for p in self.mirror_procs if p.poll() is None] + [val]
            self.statusBar().showMessage("Screen mirror starting – the window opens in a few seconds", 8000)
            self.watch_mirror(val)

        Task.start(work, done, lambda m: QMessageBox.warning(self, APP_NAME, f"Screen mirror failed:\n{m}"))

    def watch_mirror(self, proc):
        """Report an early exit (connection/server errors) with the scrcpy log."""
        def work():
            try:
                return proc.wait(timeout=25)
            except Exception:  # noqa: BLE001  (still running = fine)
                return None

        def done(code):
            if code not in (None, 0):
                QMessageBox.warning(self, APP_NAME, f"Screen mirror stopped (exit code {code}).\n\n"
                                    f"{M.log_tail(12)}")
        Task.start(work, done, lambda m: None)

    def mirror_login_help(self, prof: B.Profile, msg: str):
        key = M.public_key_text()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Screen mirror needs key login")
        text = (f"Screen mirroring opens several SSH connections, so it can't use a password.\n"
                f"Key login to {prof.username}@{prof.host}:{prof.port} failed:\n\n{msg}\n\n")
        if key:
            text += ("Add this PC's public key to the SSH server's authorized keys "
                     "(e.g. in AndroOpenHeart) and try again.")
            copy = box.addButton("Copy public key", QMessageBox.ButtonRole.ActionRole)
        else:
            text += "Create a key with  ssh-keygen -t ed25519  and add it to the device."
            copy = None
        box.setText(text)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if copy is not None and box.clickedButton() is copy:
            QApplication.clipboard().setText(key)
            self.statusBar().showMessage("Public key copied to the clipboard", 5000)


class CommandDialog(QDialog):
    """Tiny shell: runs one command at a time (as root when the profile uses su)."""

    def __init__(self, parent, conn: B.Connection, cwd: str):
        super().__init__(parent)
        self.conn = conn
        self.cwd = cwd
        self.setWindowTitle("Run command on device")
        self.resize(760, 460)
        lay = QVBoxLayout(self)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFont(QFont("Consolas", 9))
        lay.addWidget(self.out)
        h = QHBoxLayout()
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("e.g. df -h   (runs in current folder" +
                                    (", as root)" if (conn.is_root or conn.native_root) else ")"))
        self.cmd.returnPressed.connect(self.run)
        h.addWidget(self.cmd)
        b = QPushButton("Run")
        b.clicked.connect(self.run)
        h.addWidget(b)
        lay.addLayout(h)

    def run(self):
        c = self.cmd.text().strip()
        if not c:
            return
        self.out.appendPlainText(f"$ {c}")
        self.cmd.clear()
        conn, cwd = self.conn, self.cwd

        def work():
            return conn.exec(f"cd {B.shq(cwd)} 2>/dev/null; {c}", timeout=120)

        def done(r):
            rc, out, err = r
            txt = (out + err).decode(errors="replace").rstrip()
            if txt:
                self.out.appendPlainText(txt)
            if rc:
                self.out.appendPlainText(f"[exit {rc}]")
        Task.start(work, done, lambda m: self.out.appendPlainText(f"[error] {m}"))


def _safe_name(n: str) -> str:
    bad = '<>:"/\\|?*'
    s = "".join("_" if ch in bad or ord(ch) < 32 else ch for ch in n).rstrip(" .")
    return s or "_"


class AdbConnectDialog(QDialog):
    """Pick an ADB device (USB or network) or `adb connect` to an address."""

    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Connect via ADB")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Devices seen by adb:"))
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _it: self.connect_selected())
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.addr = QLineEdit(win.cfg.data.get("last_adb_address", ""))
        self.addr.setPlaceholderText("adb over Wi-Fi: 192.168.1.50:5555")
        b_conn = QPushButton(theme.line_icon("connect"), " adb connect")
        b_conn.setAutoDefault(False)
        b_conn.clicked.connect(self.adb_connect)
        row.addWidget(self.addr, 1)
        row.addWidget(b_conn)
        lay.addLayout(row)
        hint = QLabel("<small>Enable USB debugging on the phone. On first use the phone asks to allow this PC. "
                      "Root works through Magisk's su (grant it to “Shell”). The Device window can switch on "
                      "wireless ADB for you while connected over SSH.</small>")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.b_openheart = QPushButton(theme.line_icon("download"), " Install AndroOpenHeart + Shizuku && connect via SSH")
        self.b_openheart.setToolTip("Installs the bundled AndroOpenHeart app on the selected device, authorizes this PC's "
                                  "SSH key, grants its permissions, sets up Shizuku (shell-user commands without root), starts the server and connects AndroUnderTheHood over SSH. "
                                  "Works with or without root.")
        self.b_openheart.setAutoDefault(False)
        self.b_openheart.clicked.connect(self.openheart)
        lay.addWidget(self.b_openheart)
        b_drv = QPushButton(theme.line_icon("usb"), " Phone not listed? Install USB drivers…")
        b_drv.setToolTip("Drivers for ADB, fastboot, Qualcomm EDL / diag, MediaTek VCOM and Samsung modes")
        b_drv.setAutoDefault(False)
        b_drv.clicked.connect(lambda: win.open_drivers())
        lay.addWidget(b_drv)
        bb = QDialogButtonBox()
        self.b_refresh = bb.addButton("Refresh", QDialogButtonBox.ButtonRole.ActionRole)
        self.b_refresh.clicked.connect(self.refresh)
        ok = bb.addButton("Connect", QDialogButtonBox.ButtonRole.AcceptRole)
        ok.clicked.connect(self.connect_selected)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel).clicked.connect(self.reject)
        lay.addWidget(bb)
        self.refresh()

    def refresh(self):
        from . import adbconn
        self.list.clear()
        try:
            devs = adbconn.adb_devices()
        except Exception as e:  # noqa: BLE001
            self.list.addItem(f"adb error: {e}")
            return
        if not devs:
            it = QListWidgetItem("No devices – plug in a phone with USB debugging, or use adb connect below")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(it)
        for d in devs:
            label = f"{d['model'] or d['serial']}   ·   {d['serial']}   ·   {d['transport']}   ·   {d['state']}"
            it = QListWidgetItem(theme.line_icon("phone"), label)
            it.setData(Qt.ItemDataRole.UserRole, d)
            if d["state"] != "device":
                it.setForeground(QColor(theme.TEXT_FAINT))
                it.setToolTip("unauthorized: accept the prompt on the phone, then Refresh")
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)

    def adb_connect(self):
        from . import adbconn
        addr = self.addr.text().strip()
        if not addr:
            return
        if ":" not in addr:
            addr += ":5555"
        self.win.cfg.data["last_adb_address"] = addr
        self.win.cfg.save()
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        try:
            ok, msg = adbconn.adb_connect(addr)
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            QMessageBox.warning(self, "adb connect", msg)
        self.refresh()

    def openheart(self):
        it = self.list.currentItem()
        d = it.data(Qt.ItemDataRole.UserRole) if it else None
        if not d or d["state"] != "device":
            QMessageBox.warning(self, "AndroOpenHeart", "Select an authorized ADB device first.")
            return
        self.accept()
        self.win.setup_openheart(d["serial"])

    def connect_selected(self):
        it = self.list.currentItem()
        d = it.data(Qt.ItemDataRole.UserRole) if it else None
        if not d:
            return
        if d["state"] != "device":
            QMessageBox.warning(self, "ADB", f"{d['serial']} is {d['state']}. Accept “Allow USB debugging?” on the "
                                "phone, then Refresh.")
            return
        self.accept()
        self.win.start_adb_connection(d["serial"], d["model"] or d["serial"])


class _LineSig(QObject):
    line = Signal(str)


class SetupLogDialog(QDialog):
    """Live log for a multi-step background operation."""

    def __init__(self, parent, title: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(640, 360)
        lay = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(QFont("Consolas", 9))
        lay.addWidget(self.text, 1)
        self.state = QLabel("Working…")
        lay.addWidget(self.state)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.close_btn = bb.button(QDialogButtonBox.StandardButton.Close)
        self.close_btn.setEnabled(False)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        self.close_btn.clicked.connect(self.accept)
        lay.addWidget(bb)
        self.sig = _LineSig()
        self.sig.line.connect(self.add)

    def add(self, line: str):
        self.text.appendPlainText("• " + line)

    def finish(self, ok: bool, msg: str):
        self.add(msg)
        self.state.setText(("✔ " if ok else "✘ ") + msg)
        self.state.setStyleSheet(f"color: {theme.ACCENT if ok else '#ff6b6b'}; font-weight: 600;")
        self.close_btn.setEnabled(True)
        if ok:
            QTimer.singleShot(2500, self.accept)
