"""Kernel tunables explorer/editor: /sys (sysfs) and /proc/sys.

Every node is one small value that is read with `cat` and changed by writing to it, so a
folder is shown as a table of attribute → value, with in-place editing, choice lists taken
from sibling "available_*" nodes, readable hints (°C, MHz, %, mA…), an "overview" table
that compares all sub-folders (thermal zones, CPU policies, batteries…), live refresh and
a name search.
"""
from __future__ import annotations

import base64
import logging
import posixpath
import re
import shlex

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMenu, QMessageBox, QPushButton, QSpinBox, QSplitter, QStackedWidget,
                               QStyledItemDelegate, QTableView, QTableWidget, QTableWidgetItem, QTabWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import backend as B
from . import theme

log = logging.getLogger("androunderthehood")
q = B.shq

ROOTS = ("/sys", "/proc/sys")

# (label, candidate paths – the first that exists is used)
QUICK = [
    ("CPU frequency", ["/sys/devices/system/cpu/cpufreq"]),
    ("CPU cores", ["/sys/devices/system/cpu"]),
    ("GPU", ["/sys/class/kgsl/kgsl-3d0", "/sys/class/misc/mali0/device", "/sys/kernel/gpu",
             "/sys/class/devfreq"]),
    ("Devfreq (GPU / bus scaling)", ["/sys/class/devfreq"]),
    ("Thermal zones", ["/sys/class/thermal"]),
    ("Battery & power", ["/sys/class/power_supply"]),
    ("Display backlight", ["/sys/class/backlight"]),
    ("LEDs", ["/sys/class/leds"]),
    ("Block devices / I/O", ["/sys/block"]),
    ("Kernel module parameters", ["/sys/module"]),
    ("Wakeup / power", ["/sys/power"]),
    ("Memory & swap (vm)", ["/proc/sys/vm"]),
    ("Kernel (sched, printk…)", ["/proc/sys/kernel"]),
    ("Networking", ["/proc/sys/net"]),
]

# names that are huge, binary or that block when read – never read automatically
SKIP_READ = {"trace_pipe", "trace_pipe_raw", "config", "rom", "nvmem", "eeprom", "firmware", "kmsg",
             "data", "raw_data", "descriptors", "blob", "resource", "resource0", "resource1", "resource2",
             "image", "snapshot", "registers", "regs"}

# writes to these can reboot, suspend, crash, detach a disk or disable hardware: always ask first
DANGER = re.compile(r"(^/sys/power/|sysrq|/reboot|shutdown|/kernel/debug/|/(un)?bind$|/delete$|/remove$|"
                    r"/reset$|/rescan$|/trigger$|/panic|/offline$|/power/control$|/state$|/crash|/halt)")

READ_SCRIPT = r'''
set -o pipefail 2>/dev/null
cd -- %(dir)s 2>/dev/null || { echo "@@NODIR"; exit 0; }
echo "@@REAL	$(pwd -P)"
for f in * ; do
  [ -e "$f" ] || [ -L "$f" ] || continue
  if [ -d "$f" ]; then
    if [ -L "$f" ]; then echo "@@D	L	$f"; else echo "@@D	D	$f"; fi
    continue
  fi
  m=$(stat -L -c %%A -- "$f" 2>/dev/null)
  case " %(skip)s " in *" $f "*) echo "@@F	$f	$m	S	"; continue;; esac
  if v=$(timeout 2 head -c 4096 -- "$f" 2>/dev/null | base64 -w 0); then
    echo "@@F	$f	$m	O	$v"
  else
    echo "@@F	$f	$m	E	"
  fi
done
'''

OVERVIEW_SCRIPT = r'''
set -o pipefail 2>/dev/null
cd -- %(dir)s 2>/dev/null || exit 0
n=0
for c in * ; do
  [ -d "$c" ] || continue
  n=$((n+1)); [ $n -gt 400 ] && break
  echo "@@C	$c"
  k=0
  for f in "$c"/* ; do
    [ -f "$f" ] || continue
    k=$((k+1)); [ $k -gt 48 ] && break
    b=${f##*/}
    case " %(skip)s " in *" $b "*) continue;; esac
    if v=$(timeout 1 head -c 256 -- "$f" 2>/dev/null | base64 -w 0); then echo "@@V	$b	$v"; fi
  done
done
'''


# ---------------------------------------------------------------------------
# value helpers
# ---------------------------------------------------------------------------
def decode(b64: str) -> tuple[str, bool]:
    """(display text, is_binary)"""
    raw = base64.b64decode(b64) if b64 else b""
    if raw and (b"\0" in raw.rstrip(b"\0") or sum(c < 9 or 13 < c < 32 for c in raw) > len(raw) // 10):
        return f"‹binary, {len(raw)} bytes› " + raw[:24].hex(" "), True
    return raw.decode("utf-8", errors="replace").rstrip("\n\0"), False


def is_writable(mode: str) -> bool:
    return len(mode) >= 9 and "w" in mode[1:10]


def bracket_options(value: str) -> tuple[list[str], str] | None:
    """'none [mq-deadline] kyber' -> (['none','mq-deadline','kyber'], 'mq-deadline')"""
    if "[" not in value or "\n" in value:
        return None
    toks = value.split()
    sel = [t[1:-1] for t in toks if t.startswith("[") and t.endswith("]")]
    if len(sel) != 1 or len(toks) < 2:
        return None
    return [t.strip("[]") for t in toks], sel[0]


def option_list(name: str, attrs: dict[str, str]) -> list[str] | None:
    """Choices for attribute `name` from a sibling "available_*" node."""
    cands = []
    if "governor" in name:
        cands += [k for k in attrs if "available_governors" in k]
    if name.endswith("freq") or name.endswith("_freq_mhz") or name in ("scaling_setspeed",):
        cands += [k for k in attrs if "available_frequencies" in k or k.endswith("freq_table")]
    cands += [f"available_{name}", f"{name}_available", f"available_{name}s", f"{name}s_available"]
    for c in cands:
        v = attrs.get(c)
        if v and len(v) < 2000:
            opts = [t for t in re.split(r"[\s,]+", v.replace("[", " ").replace("]", " ")) if t]
            if 1 < len(opts) <= 128:
                return opts
    return None


def hint(name: str, value: str, path: str) -> str:
    """Human-readable meaning of common values."""
    v = value.strip()
    if not re.fullmatch(r"-?\d+", v):
        return ""
    n = int(v)
    p = path
    if name == "temp" or name.startswith("temp") and "input" in name or name.endswith("_temp"):
        if "power_supply" in p and abs(n) < 2000:
            return f"{n / 10:.1f} °C"
        return f"{n / 1000:.1f} °C" if abs(n) >= 1000 else f"{n} °C"
    if "trip_point" in name and name.endswith("_temp"):
        return f"{n / 1000:.1f} °C"
    if "freq" in name:
        if "cpufreq" in p or "/cpu" in p and "scaling" in name or name.startswith("cpuinfo"):
            return f"{n / 1000:.0f} MHz"          # cpufreq uses kHz
        if n > 1_000_000:
            return f"{n / 1e6:.0f} MHz"            # devfreq uses Hz
    if "power_supply" in p:
        if name.startswith("voltage"):
            return f"{n / 1e6:.3f} V"
        if name.startswith("current"):
            return f"{n / 1000:.0f} mA"
        if name.startswith("charge") or name.startswith("energy"):
            return f"{n / 1000:.0f} mAh" if name.startswith("charge") else f"{n / 1e6:.2f} Wh"
        if name == "capacity":
            return f"{n} %"
        if name.startswith("power"):
            return f"{n / 1e6:.2f} W"
        if name.startswith("time_to"):
            return f"{n // 3600} h {n % 3600 // 60} min"
    if name in ("read_ahead_kb", "max_sectors_kb", "max_hw_sectors_kb"):
        return f"{n} KiB"
    if name == "swappiness":
        return f"{n} (0 = avoid swap … 200 = prefer swap)"
    if name in ("dirty_ratio", "dirty_background_ratio") or name.endswith("_ratio"):
        return f"{n} %"
    if name.endswith("_bytes") and n > 1024:
        from .ui import human_size
        return human_size(n)
    if name in ("online", "enable", "enabled") and n in (0, 1):
        return "on" if n else "off"
    return ""


def dangerous(path: str) -> bool:
    return bool(DANGER.search(path))


# ---------------------------------------------------------------------------
# attribute table
# ---------------------------------------------------------------------------
class Attr:
    __slots__ = ("name", "mode", "state", "value", "binary", "changed")

    def __init__(self, name, mode, state, value, binary):
        self.name, self.mode, self.state, self.value, self.binary = name, mode, state, value, binary
        self.changed = False


class AttrModel(QAbstractTableModel):
    COLS = ["Attribute", "Value", "Meaning", "Access"]

    def __init__(self, dlg: "SysfsDialog"):
        super().__init__()
        self.dlg = dlg
        self.dir = ""
        self.rows: list[Attr] = []
        self.mono = QFont("Consolas", 9)
        self.lock = theme.line_icon("lock", theme.TEXT_FAINT)
        self.boot_icon = theme.line_icon("star", theme.ACCENT)

    def set_rows(self, d: str, rows: list[Attr]):
        self.beginResetModel()
        self.dir, self.rows = d, rows
        self.endResetModel()

    def values(self) -> dict[str, str]:
        return {a.name: a.value for a in self.rows if a.state == "O"}

    def update_values(self, rows: list[Attr]):
        """Live refresh: change values in place (keeps selection/scroll)."""
        new = {a.name: a for a in rows}
        for i, a in enumerate(self.rows):
            n = new.get(a.name)
            if n and (n.value != a.value or n.state != a.state):
                a.value, a.state, a.binary = n.value, n.state, n.binary
                a.changed = True
                self.dataChanged.emit(self.index(i, 1), self.index(i, 2))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLS)

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLS[s]
        return None

    def editable(self, a: Attr) -> bool:
        return self.dlg.can_write and is_writable(a.mode) and not a.binary

    def flags(self, index):
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 1 and self.editable(self.rows[index.row()]):
            f |= Qt.ItemFlag.ItemIsEditable
        return f

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        a = self.rows[index.row()]
        c = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if c == 0:
                return a.name
            if c == 1:
                if a.state == "E":
                    return "‹not readable›"
                if a.state == "S":
                    return "‹not read automatically – right-click → Read now›"
                return a.value.replace("\n", "  ⏎  ")
            if c == 2:
                return "" if a.state != "O" else hint(a.name, a.value, self.dir)
            if c == 3:
                return ("read/write" if is_writable(a.mode) and "r" in a.mode[1:10] else
                        "write-only" if is_writable(a.mode) else "read-only") + f"  {a.mode}"
        if role == Qt.ItemDataRole.EditRole and c == 1:
            return a.value
        if role == Qt.ItemDataRole.FontRole and c in (1, 3):
            return self.mono
        if role == Qt.ItemDataRole.ForegroundRole:
            if c == 1 and a.state != "O":
                return QColor(theme.TEXT_FAINT)
            if c == 1 and a.changed:
                return QColor(theme.ACCENT)
            if c in (2, 3):
                return QColor(theme.TEXT_DIM)
        if role == Qt.ItemDataRole.DecorationRole and c == 0:
            if posixpath.join(self.dir, a.name) in self.dlg.boot.entries:
                return self.boot_icon
            if not is_writable(a.mode):
                return self.lock
        if role == Qt.ItemDataRole.ToolTipRole:
            p = posixpath.join(self.dir, a.name)
            b = self.dlg.boot.entries.get(p)
            boot = f"\n★ set to {b!r} at every boot" if b is not None else ""
            return (f"{p}\n{a.value[:2000]}" if a.state == "O" else p) + boot
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or index.column() != 1:
            return False
        a = self.rows[index.row()]
        new = str(value)
        opts = bracket_options(a.value)
        cur = opts[1] if opts else a.value
        if new.strip() == cur.strip():
            return False
        self.dlg.write(posixpath.join(self.dir, a.name), new, cur)
        return False   # the table updates after the device confirms the write


class AttrFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.only_rw = False
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set(self, text=None, only_rw=None):
        if text is not None:
            self.text = text.lower()
        if only_rw is not None:
            self.only_rw = only_rw
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        a = self.sourceModel().rows[row]
        if self.only_rw and not is_writable(a.mode):
            return False
        if self.text:
            hay = f"{a.name} {a.value}".lower()
            return all(w in hay for w in self.text.split())
        return True


class ValueDelegate(QStyledItemDelegate):
    """Combo box with the allowed values when we know them, else a line edit."""

    def __init__(self, dlg: "SysfsDialog", model_fn):
        super().__init__(dlg)
        self.dlg = dlg
        self.model_fn = model_fn   # proxy index -> (attr name, current value, sibling values)

    def createEditor(self, parent, option, index):
        name, value, sib = self.model_fn(index)
        br = bracket_options(value)
        opts = br[0] if br else option_list(name, sib)
        if opts:
            cb = QComboBox(parent)
            cb.setEditable(True)
            cb.addItems(opts)
            cur = br[1] if br else value.strip()
            i = cb.findText(cur)
            if i >= 0:
                cb.setCurrentIndex(i)
            else:
                cb.setEditText(cur)
            return cb
        ed = QLineEdit(parent)
        ed.setFont(QFont("Consolas", 9))
        return ed

    def setEditorData(self, editor, index):
        if isinstance(editor, QLineEdit):
            editor.setText(index.data(Qt.ItemDataRole.EditRole) or "")

    def setModelData(self, editor, model, index):
        val = editor.currentText() if isinstance(editor, QComboBox) else editor.text()
        model.setData(index, val, Qt.ItemDataRole.EditRole)


# ---------------------------------------------------------------------------
# overview (one row per sub-folder)
# ---------------------------------------------------------------------------
class OverviewModel(QAbstractTableModel):
    def __init__(self, dlg: "SysfsDialog"):
        super().__init__()
        self.dlg = dlg
        self.dir = ""
        self.children: list[str] = []
        self.cols: list[str] = []
        self.vals: dict[str, dict[str, str]] = {}
        self.mono = QFont("Consolas", 9)

    def set_data(self, d, children, vals):
        self.beginResetModel()
        self.dir, self.children, self.vals = d, children, vals
        self._columns()
        self.endResetModel()

    def _columns(self):
        children, vals = self.children, self.vals
        n = max(1, len(children))
        count: dict[str, int] = {}
        for c in children:
            for k, v in vals.get(c, {}).items():
                if k != "uevent" and len(v) <= 96 and "\n" not in v:
                    count[k] = count.get(k, 0) + 1
        common = [k for k, cnt in count.items() if cnt >= max(2, n * 0.5)]
        pri = ("type", "name", "status", "temp", "capacity", "scaling_governor", "scaling_cur_freq",
               "cur_freq", "governor", "brightness", "max_brightness", "online", "health", "mode")
        common.sort(key=lambda k: (pri.index(k) if k in pri else len(pri), k))
        self.cols = common[:16]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.children)

    def columnCount(self, parent=QModelIndex()):
        return 1 + len(self.cols)

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return "Folder" if s == 0 else self.cols[s - 1]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        child = self.children[index.row()]
        if index.column() == 0 and role != Qt.ItemDataRole.UserRole:
            if role == Qt.ItemDataRole.DisplayRole:
                return child
            return None
        if index.column() == 0 and role == Qt.ItemDataRole.UserRole:
            return index.row()          # natural order (zone2 before zone10)
        col = self.cols[index.column() - 1]
        v = self.vals.get(child, {}).get(col)
        if role == Qt.ItemDataRole.UserRole:
            try:
                return float((v or "").split()[0])
            except (ValueError, IndexError):
                return (v or "").lower()
        if role == Qt.ItemDataRole.DisplayRole:
            if v is None:
                return ""
            h = hint(col, v, posixpath.join(self.dir, child))
            br = bracket_options(v)
            shown = br[1] if br else v
            return f"{shown}  ({h})" if h else shown
        if role == Qt.ItemDataRole.FontRole:
            return self.mono
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{posixpath.join(self.dir, child, col)}\n{v or ''}"
        return None


# ---------------------------------------------------------------------------
# dialog
# ---------------------------------------------------------------------------
class SysfsDialog(QDialog):
    def __init__(self, win, conn: B.Connection, start: str = "/sys"):
        super().__init__(win)
        from .ui import Task  # late import (ui imports this module)

        self.Task = Task
        self.win = win
        self.conn = conn
        self.can_write = bool(conn.is_root or conn.native_root) and win.a_root.isChecked()
        self.cur = ""
        self.gen = 0
        self.ov_dir = None
        self._ov_for = None
        self.setWindowTitle(f"Kernel tunables (sysfs) – {conn.profile.name}")
        self.resize(1240, 760)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        # path bar
        top = QHBoxLayout()
        self.btn_up = QPushButton(theme.line_icon("up"), "")
        self.btn_up.setToolTip("Parent folder")
        self.btn_up.clicked.connect(lambda: self.open_dir(posixpath.dirname(self.cur) if self.cur not in ROOTS
                                                          else self.cur))
        top.addWidget(self.btn_up)
        self.path = QLineEdit()
        self.path.setPlaceholderText("/sys/…")
        self.path.returnPressed.connect(lambda: self.open_dir(self.path.text().strip()))
        top.addWidget(self.path, 1)
        self.real = QLabel("")
        self.real.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        top.addWidget(self.real)
        self.live = QCheckBox("Live")
        self.live.setToolTip("Re-read the values every 2 seconds")
        self.live.toggled.connect(lambda on: self.timer.start(2000) if on else self.timer.stop())
        top.addWidget(self.live)
        self.btn_refresh = QPushButton(theme.line_icon("refresh"), " Refresh")
        self.btn_refresh.clicked.connect(lambda: self.open_dir(self.cur, select_tree=False))
        top.addWidget(self.btn_refresh)
        lay.addLayout(top)

        split = QSplitter(Qt.Orientation.Horizontal)
        lay.addWidget(split, 1)

        # ---- left: quick places + tree / search results
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        self.quick = QComboBox()
        self.quick.addItem("Quick: jump to…", None)
        self.quick.activated.connect(self.quick_jump)
        ll.addWidget(self.quick)
        self.find = QLineEdit()
        self.find.setPlaceholderText("Search names in /sys and /proc/sys…")
        self.find.setClearButtonEnabled(True)
        self.find.returnPressed.connect(self.search)
        self.find.textChanged.connect(lambda t: self.left_stack.setCurrentIndex(0) if not t else None)
        ll.addWidget(self.find)
        self.left_stack = QStackedWidget()
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemExpanded.connect(self.expand_item)
        self.tree.currentItemChanged.connect(lambda it, _p: it and self.open_dir(it.data(0, Qt.ItemDataRole.UserRole),
                                                                                select_tree=False))
        self.left_stack.addWidget(self.tree)
        self.results = QListWidget()
        self.results.itemActivated.connect(self.open_result)
        self.results.itemClicked.connect(self.open_result)
        self.left_stack.addWidget(self.results)
        ll.addWidget(self.left_stack, 1)
        split.addWidget(left)

        # ---- right: tabs (attributes / overview)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        fl = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter attributes by name or value…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(lambda t: self.proxy.set(text=t))
        fl.addWidget(self.filter, 1)
        self.only_rw = QCheckBox("Writable only")
        self.only_rw.toggled.connect(lambda on: self.proxy.set(only_rw=on))
        fl.addWidget(self.only_rw)
        rl.addLayout(fl)

        self.tabs = QTabWidget()
        self.model = AttrModel(self)
        self.proxy = AttrFilter()
        self.proxy.setSourceModel(self.model)
        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                  | QAbstractItemView.EditTrigger.EditKeyPressed
                                  | QAbstractItemView.EditTrigger.SelectedClicked)
        self.view.setAlternatingRowColors(True)
        self.view.setShowGrid(False)
        self.view.setWordWrap(False)
        self.view.verticalHeader().hide()
        self.view.verticalHeader().setDefaultSectionSize(26)
        h = self.view.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.view.setColumnWidth(0, 260)
        self.view.setItemDelegateForColumn(1, ValueDelegate(self, self._edit_info))
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.attr_menu)
        self.tabs.addTab(self.view, "Attributes")

        self.ov_model = OverviewModel(self)
        self.ov_proxy = QSortFilterProxyModel()
        self.ov_proxy.setSourceModel(self.ov_model)
        self.ov_proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.overview = QTableView()
        self.overview.setModel(self.ov_proxy)
        self.overview.setSortingEnabled(True)
        self.overview.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.overview.setAlternatingRowColors(True)
        self.overview.setShowGrid(False)
        self.overview.verticalHeader().hide()
        self.overview.verticalHeader().setDefaultSectionSize(26)
        self.overview.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.overview.doubleClicked.connect(self.overview_open)
        ov = QWidget()
        ovl = QVBoxLayout(ov)
        ovl.setContentsMargins(0, 6, 0, 0)
        gl = QHBoxLayout()
        gl.addWidget(QLabel("Group:"))
        self.group = QComboBox()
        self.group.setMinimumWidth(260)
        self.group.activated.connect(lambda _i: self.show_group())
        gl.addWidget(self.group)
        gl.addStretch(1)
        ovl.addLayout(gl)
        ovl.addWidget(self.overview, 1)
        self.ov_all = ([], {})
        self.tabs.addTab(ov, "Overview of sub-folders")
        self.tabs.currentChanged.connect(lambda _i: self.load_overview())
        rl.addWidget(self.tabs, 1)
        split.addWidget(right)
        split.setSizes([330, 910])

        bottom = QHBoxLayout()
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setStyleSheet(f"color: {theme.TEXT_DIM};")
        bottom.addWidget(self.info, 1)
        self.boot = BootSet()
        self.at_boot = QCheckBox("Also apply at boot")
        self.at_boot.setToolTip("Values you change are also saved to a boot script, so they are set again after "
                                "every reboot (needs Magisk / KernelSU)")
        self.at_boot.setEnabled(False)
        bottom.addWidget(self.at_boot)
        self.btn_boot = QPushButton(theme.line_icon("star"), " At boot…")
        self.btn_boot.setToolTip("Values that are set again after every boot")
        self.btn_boot.clicked.connect(lambda: BootDialog(self).exec())
        self.btn_boot.setAutoDefault(False)
        self.btn_boot.setEnabled(False)
        bottom.addWidget(self.btn_boot)
        lay.addLayout(bottom)

        for b in (self.btn_up, self.btn_refresh):
            b.setAutoDefault(False)
            b.setDefault(False)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.live_refresh)
        QShortcut(QKeySequence(Qt.Key.Key_F5), self, activated=lambda: self.open_dir(self.cur, select_tree=False))
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.filter.setFocus)
        QShortcut(QKeySequence(Qt.Key.Key_Backspace), self.view, activated=self.btn_up.click)

        for r in ROOTS:
            it = QTreeWidgetItem([r])
            it.setData(0, Qt.ItemDataRole.UserRole, r)
            it.setIcon(0, theme.line_icon("tree"))
            it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
            self.tree.addTopLevelItem(it)
        self.detect_quick()
        self.load_boot()
        self.open_dir(start)

    # ---- helpers ----------------------------------------------------------
    def status_text(self, extra: str = ""):
        mode = ("Root: changes are written immediately and last until reboot." if self.can_write else
                "Read-only – turn Root on in the main window to change values.")
        n = len(self.model.rows)
        w = sum(1 for a in self.model.rows if is_writable(a.mode))
        self.info.setText(f"{n} attributes · {w} writable · {mode}" + (f"\n{extra}" if extra else ""))

    def load_boot(self):
        def done(b):
            ok = b.supported and self.can_write
            self.at_boot.setEnabled(ok)
            self.btn_boot.setEnabled(b.supported and self.can_write)
            if not b.supported:
                self.btn_boot.setToolTip("Needs Magisk, KernelSU or APatch (runs scripts from /data/adb/service.d)")
            self.boot_changed()
        self.Task.start(lambda: self.boot.load(self.conn), done, lambda m: None)

    def boot_changed(self, msg: str = ""):
        n = len(self.boot.entries)
        self.btn_boot.setText(f" At boot ({n})…" if n else " At boot…")
        self.model.layoutChanged.emit()
        if msg:
            self.info.setText(msg)

    def set_boot(self, path: str, value: str | None):
        """Add/update (value) or remove (None) one boot entry and save the script."""
        if value is None:
            self.boot.entries.pop(path, None)
        else:
            self.boot.entries[path] = value
        self.Task.start(lambda: self.boot.save(self.conn),
                        lambda _r: self.boot_changed(
                            f"{posixpath.basename(path)}: " + ("removed from boot" if value is None else
                                                               f"{value!r} will be set at every boot") +
                            f"  ({len(self.boot.entries)} value(s) in {BOOT_SCRIPT})"),
                        lambda m: QMessageBox.warning(self, "sysfs", f"Could not save the boot script:\n{m}"))

    def _edit_info(self, proxy_index):
        a = self.model.rows[self.proxy.mapToSource(proxy_index).row()]
        return a.name, a.value, self.model.values()

    def detect_quick(self):
        paths = sorted({p for _l, ps in QUICK for p in ps})

        def work():
            rc, out, _ = self.conn.exec("for p in " + " ".join(q(p) for p in paths) +
                                        "; do [ -d \"$p\" ] && echo \"$p\"; done", timeout=30)
            return set(out.decode(errors="replace").split())

        def done(have):
            for label, ps in QUICK:
                p = next((x for x in ps if x in have), None)
                if p:
                    self.quick.addItem(f"{label}   ({p})", p)
        self.Task.start(work, done, lambda m: None)

    def quick_jump(self, i):
        p = self.quick.itemData(i)
        self.quick.setCurrentIndex(0)
        if p:
            self.open_dir(p)

    # ---- reading ------------------------------------------------------------
    def read_dir(self, d: str):
        script = READ_SCRIPT % {"dir": q(d), "skip": " ".join(sorted(SKIP_READ))}
        rc, out, err = self.conn.exec(script, timeout=90)
        text = out.decode("utf-8", errors="replace")
        if "@@NODIR" in text:
            raise B.BackendError(f"{d}: no such folder (or not allowed)")
        real, dirs, attrs = d, [], []
        for line in text.split("\n"):
            p = line.split("\t")
            if p[0] == "@@REAL" and len(p) > 1:
                real = p[1]
            elif p[0] == "@@D" and len(p) >= 3:
                dirs.append((p[2], p[1] == "L"))
            elif p[0] == "@@F" and len(p) >= 5:
                val, binary = decode(p[4]) if p[3] == "O" else ("", False)
                attrs.append(Attr(p[1], p[2], p[3], val, binary))
        return real, dirs, attrs

    def open_dir(self, d: str, select_tree: bool = True):
        d = posixpath.normpath(d or "/sys")
        if not d.startswith(("/sys", "/proc/sys")):
            QMessageBox.information(self, "sysfs", "This tool is for /sys and /proc/sys.")
            return
        self.gen += 1
        gen = self.gen
        self.path.setText(d)
        self.btn_refresh.setEnabled(False)

        def done(r):
            if gen != self.gen:
                return
            real, dirs, attrs = r
            self.btn_refresh.setEnabled(True)
            self.cur = d
            self.real.setText(f"→ {real}" if real != d else "")
            self.model.set_rows(d, attrs)
            self.view.resizeColumnToContents(0)
            self.view.setColumnWidth(0, min(max(self.view.columnWidth(0), 160), 380))
            self.ov_dir = None
            self.tabs.setTabEnabled(1, len(dirs) >= 2)
            if self.tabs.currentIndex() == 1:
                if len(dirs) >= 2:
                    self.load_overview()
                else:
                    self.tabs.setCurrentIndex(0)
            if select_tree:
                self.select_in_tree(d)
            self.status_text(f"{len(dirs)} sub-folders" + (" – see “Overview of sub-folders”" if len(dirs) >= 2
                                                            else ""))

        def fail(m):
            if gen == self.gen:
                self.btn_refresh.setEnabled(True)
                self.info.setText(f"Cannot open {d}: {m}")
        self.Task.start(lambda: self.read_dir(d), done, fail)

    def live_refresh(self):
        if not self.cur or self.view.state() == QAbstractItemView.State.EditingState or not self.isVisible():
            return
        d, gen = self.cur, self.gen

        def done(r):
            if gen == self.gen and d == self.cur:
                self.model.update_values(r[2])
        self.Task.start(lambda: self.read_dir(d), done, lambda m: None)
        if self.tabs.currentIndex() == 1:
            self.ov_dir = None
            self.load_overview()

    def load_overview(self):
        if self.tabs.currentIndex() != 1 or not self.cur or self.ov_dir == self.cur:
            return
        d = self.cur
        self.ov_dir = d

        def work():
            script = OVERVIEW_SCRIPT % {"dir": q(d), "skip": " ".join(sorted(SKIP_READ))}
            rc, out, _ = self.conn.exec(script, timeout=120)
            children, vals, cur = [], {}, None
            for line in out.decode("utf-8", errors="replace").split("\n"):
                p = line.split("\t")
                if p[0] == "@@C" and len(p) > 1:
                    cur = p[1]
                    children.append(cur)
                    vals[cur] = {}
                elif p[0] == "@@V" and len(p) >= 3 and cur:
                    v, binary = decode(p[2])
                    if not binary:
                        vals[cur][p[1]] = v
            return children, vals

        def done(r):
            if d != self.cur:
                return
            live = self.ov_all[0] and self.group.currentData() is not None and self._ov_for == d
            self.ov_all = r
            self._ov_for = d
            children = r[0]
            groups: dict[str, int] = {}
            for c in children:
                groups[re.sub(r"\d+$", "", c) or c] = groups.get(re.sub(r"\d+$", "", c) or c, 0) + 1
            keep = self.group.currentData() if live else None
            self.group.blockSignals(True)
            self.group.clear()
            self.group.addItem(f"All ({len(children)})", "")
            for g, cnt in sorted(groups.items(), key=lambda x: -x[1]):
                if cnt >= 2 and len(groups) > 1:
                    self.group.addItem(f"{g}*  ({cnt})", g)
            if keep is None:
                keep = self.best_group(groups)
            i = self.group.findData(keep)
            self.group.setCurrentIndex(max(0, i))
            self.group.blockSignals(False)
            self.show_group()
        self.Task.start(work, done, lambda m: self.info.setText(f"Overview failed: {m}"))

    def best_group(self, groups: dict[str, int]) -> str:
        """Group whose folders share the most interesting attributes (thermal_zone over cooling_device…)."""
        children, vals = self.ov_all
        pri = ("temp", "type", "capacity", "status", "scaling_governor", "scaling_cur_freq", "cur_freq",
               "governor", "brightness", "online")
        best, score = "", -1.0
        for g, cnt in groups.items():
            if cnt < 2:
                continue
            members = [c for c in children if (re.sub(r"\d+$", "", c) or c) == g]
            keys = set.intersection(*(set(vals.get(c, {})) for c in members)) if members else set()
            sc = sum(3 if k == "temp" else 1 for k in keys if k in pri) + cnt / 1000
            if sc > score:
                best, score = g, sc
        return best

    def show_group(self):
        g = self.group.currentData() or ""
        children, vals = self.ov_all
        members = [c for c in children if not g or (re.sub(r"\d+$", "", c) or c) == g]
        members.sort(key=lambda c: (re.sub(r"\d+$", "", c), int(re.search(r"(\d+)$", c).group(1))
                                    if re.search(r"(\d+)$", c) else -1, c))
        self.ov_model.set_data(self.cur, members, vals)
        self.overview.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.overview.resizeColumnsToContents()

    def overview_open(self, idx):
        src = self.ov_proxy.mapToSource(idx)
        child = self.ov_model.children[src.row()]
        self.tabs.setCurrentIndex(0)
        self.open_dir(posixpath.join(self.ov_model.dir, child))

    # ---- tree -------------------------------------------------------------
    def expand_item(self, it: QTreeWidgetItem):
        if it.data(0, Qt.ItemDataRole.UserRole + 1):
            return
        it.setData(0, Qt.ItemDataRole.UserRole + 1, "loading")
        d = it.data(0, Qt.ItemDataRole.UserRole)

        def work():
            rc, out, _ = self.conn.exec(
                f"cd -- {q(d)} 2>/dev/null && for f in * ; do [ -d \"$f\" ] || continue; "
                f"if [ -L \"$f\" ]; then echo \"L\t$f\"; else echo \"D\t$f\"; fi; done", timeout=60)
            return [ln.split("\t", 1) for ln in out.decode(errors="replace").split("\n") if "\t" in ln]

        def done(rows):
            it.setData(0, Qt.ItemDataRole.UserRole + 1, "loaded")
            for kind, name in sorted(rows, key=lambda r: r[1].lower()):
                c = QTreeWidgetItem([name + ("  ↗" if kind == "L" else "")])
                c.setData(0, Qt.ItemDataRole.UserRole, posixpath.join(d, name))
                c.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                if kind == "L":
                    c.setToolTip(0, "symbolic link")
                    c.setForeground(0, QColor(theme.TEXT_DIM))
                it.addChild(c)
            if not rows:
                it.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator)
            pend = getattr(self, "_pending_select", None)
            if pend:
                self.select_in_tree(pend)
        self.Task.start(work, done, lambda m: None)

    def select_in_tree(self, d: str):
        """Expand the tree down to `d` (lazily) and select it without re-loading the table."""
        self._pending_select = d
        root = next((self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())
                     if d == self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
                     or d.startswith(self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) + "/")), None)
        if root is None:
            return
        it = root
        while True:
            p = it.data(0, Qt.ItemDataRole.UserRole)
            if p == d:
                self.tree.blockSignals(True)
                self.tree.setCurrentItem(it)
                self.tree.blockSignals(False)
                self.tree.scrollToItem(it)
                self._pending_select = None
                return
            state = it.data(0, Qt.ItemDataRole.UserRole + 1)
            if state != "loaded":
                it.setExpanded(True)    # loads children (if not already loading), then calls us again
                if not it.isExpanded() or state is None:
                    self.expand_item(it)
                return
            it.setExpanded(True)
            nxt = next((it.child(i) for i in range(it.childCount())
                        if d == it.child(i).data(0, Qt.ItemDataRole.UserRole)
                        or d.startswith(it.child(i).data(0, Qt.ItemDataRole.UserRole) + "/")), None)
            if nxt is None:
                self._pending_select = None
                return
            it = nxt

    # ---- search -----------------------------------------------------------
    def search(self):
        pat = self.find.text().strip()
        if not pat:
            return
        self.results.clear()
        self.results.addItem("Searching…")
        self.left_stack.setCurrentIndex(1)
        glob = pat if any(ch in pat for ch in "*?[") else f"*{pat}*"

        def work():
            rc, out, _ = self.conn.exec(
                f"find /sys /proc/sys -name {q(glob)} 2>/dev/null | head -n 600", timeout=180)
            return [ln for ln in out.decode(errors="replace").split("\n") if ln]

        def done(hits):
            self.results.clear()
            if not hits:
                self.results.addItem("No matches")
            for h_ in hits:
                it = QListWidgetItem(h_)
                it.setData(Qt.ItemDataRole.UserRole, h_)
                self.results.addItem(it)
            self.info.setText(f"{len(hits)} matches for “{pat}”" + (" (first 600)" if len(hits) >= 600 else "") +
                              " – click one to open it. Clear the search box to return to the tree.")
        self.Task.start(work, done, lambda m: self.info.setText(f"Search failed: {m}"))

    def open_result(self, it: QListWidgetItem):
        p = it.data(Qt.ItemDataRole.UserRole)
        if not p:
            return

        def work():
            rc, out, _ = self.conn.exec(f"[ -d {q(p)} ] && echo D", timeout=20)
            return out.strip() == b"D"

        def done(is_dir):
            if is_dir:
                self.open_dir(p)
            else:
                self.filter.clear()
                self._select_after = posixpath.basename(p)
                self.open_dir(posixpath.dirname(p))
                QTimer.singleShot(1500, self._select_pending_attr)
        self.Task.start(work, done, lambda m: None)

    def _select_pending_attr(self):
        name = getattr(self, "_select_after", None)
        self._select_after = None
        if not name:
            return
        for r in range(self.proxy.rowCount()):
            if self.proxy.index(r, 0).data() == name:
                self.view.selectRow(r)
                self.view.scrollTo(self.proxy.index(r, 0))
                return

    # ---- writing ------------------------------------------------------------
    def write(self, path: str, value: str, old: str):
        if not self.can_write:
            QMessageBox.information(self, "sysfs", "Turn Root on in the main window to change kernel values.")
            return
        if dangerous(path):
            r = QMessageBox.warning(
                self, "Change kernel value?",
                f"{path}\n\n{old!r}  →  {value!r}\n\nWriting here can reboot, suspend or crash the device, or "
                "switch hardware off. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                return

        def work():
            rc, out, err = self.conn.exec(
                f"{{ printf '%s' {q(value)} > {q(path)}; }} 2>&1; r=$?; echo; echo \"@@RC $r\"; "
                f"timeout 2 head -c 4096 -- {q(path)} 2>/dev/null | base64 -w 0", timeout=30)
            text = (out + err).decode(errors="replace")
            pre, _, post = text.partition("@@RC ")
            rcode, _, b64 = post.partition("\n")
            msg = pre.strip()
            return int(rcode.strip() or 1), msg, decode(b64.strip())[0]

        def done(r):
            rcode, msg, now = r
            name = posixpath.basename(path)
            br = bracket_options(now)
            eff = br[1] if br else now.strip()
            if rcode != 0:
                m = re.sub(r"^.*?: ", "", msg.splitlines()[-1]) if msg else f"error {rcode}"
                QMessageBox.warning(self, "sysfs", f"Could not write {path}:\n\n{m}\n\nThe kernel rejected the "
                                    "value (wrong format, out of range, or this node is not writable now).")
            elif eff != value.strip():
                self.info.setText(f"{name}: wrote {value!r}, the kernel now reports {eff!r} "
                                  "(it may have rounded or limited the value).")
            else:
                self.info.setText(f"{name} = {eff}   ✔  (until reboot)")
            if rcode == 0 and (self.at_boot.isChecked() or path in self.boot.entries):
                self.set_boot(path, value.strip())
            log.info("sysfs write %s = %r -> rc=%s now=%r", path, value, rcode, now)
            if posixpath.dirname(path) == self.cur:
                for a in self.model.rows:
                    if a.name == name:
                        a.value, a.state, a.changed = now, "O", True
                i = next((k for k, a in enumerate(self.model.rows) if a.name == name), None)
                if i is not None:
                    self.model.dataChanged.emit(self.model.index(i, 1), self.model.index(i, 2))
                # siblings may change too (e.g. scaling_max_freq clamps scaling_cur_freq)
                QTimer.singleShot(400, self.live_refresh)
        self.Task.start(work, done, lambda m: QMessageBox.warning(self, "sysfs", f"Write failed: {m}"))

    # ---- context menu -------------------------------------------------------
    def attr_menu(self, pos):
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        a = self.model.rows[self.proxy.mapToSource(idx).row()]
        path = posixpath.join(self.cur, a.name)
        m = QMenu(self)
        if self.model.editable(a):
            m.addAction(theme.line_icon("rename"), "Edit value…", lambda: self.view.edit(self.proxy.index(idx.row(), 1)))
        if a.state in ("S", "E"):
            m.addAction(theme.line_icon("refresh"), "Read now", lambda: self.read_one(a))
        if self.btn_boot.isEnabled() and is_writable(a.mode) and a.state == "O" and not a.binary:
            cur_v = (bracket_options(a.value) or (None, a.value))[1].strip()
            if path in self.boot.entries:
                m.addAction(theme.line_icon("star"), f"Remove from boot (now {self.boot.entries[path]!r})",
                            lambda: self.set_boot(path, None))
                if self.boot.entries[path] != cur_v:
                    m.addAction(theme.line_icon("star"), f"Set at boot to the current value {cur_v!r}",
                                lambda: self.set_boot(path, cur_v))
            else:
                m.addAction(theme.line_icon("star"), f"Apply at boot with the current value {cur_v!r}",
                            lambda: self.set_boot(path, cur_v))
            m.addSeparator()
        m.addAction(theme.line_icon("copy"), "Copy path", lambda: QApplication.clipboard().setText(path))
        m.addAction(theme.line_icon("copy"), "Copy value", lambda: QApplication.clipboard().setText(a.value))
        cur = (bracket_options(a.value) or (None, a.value))[1]
        m.addAction(theme.line_icon("terminal"), "Copy as shell command",
                    lambda: QApplication.clipboard().setText(f"echo {q(cur.strip())} > {path}"))
        m.addSeparator()
        m.addAction(theme.line_icon("folder-plus"), "Show folder in the file browser",
                    lambda: (self.win.navigate(self.cur), self.win.raise_(), self.win.activateWindow()))
        m.exec(self.view.viewport().mapToGlobal(pos))

    def read_one(self, a: Attr):
        path = posixpath.join(self.cur, a.name)

        def work():
            rc, out, err = self.conn.exec(f"timeout 5 head -c 65536 -- {q(path)} | base64 -w 0", timeout=30)
            if not out.strip():
                raise B.BackendError((err.decode(errors="replace").strip() or "no data"))
            return decode(out.decode().strip())

        def done(r):
            a.value, a.binary = r
            a.state = "O"
            self.model.layoutChanged.emit()
        self.Task.start(work, done, lambda m: QMessageBox.information(self, "sysfs", f"{path}: {m}"))

    def closeEvent(self, e):
        self.timer.stop()
        super().closeEvent(e)


# ---------------------------------------------------------------------------
# apply at boot (Magisk / KernelSU / APatch service.d script)
# ---------------------------------------------------------------------------
BOOT_DIR = "/data/adb/service.d"
BOOT_SCRIPT = BOOT_DIR + "/androunderthehood-sysfs.sh"
BOOT_LOG = "/data/adb/androunderthehood-sysfs.log"
# the app used to be called DroidBrowser: adopt a boot script written under the old name
OLD_BOOT = BOOT_DIR + "/droidbrowser-sysfs.sh"
MIGRATE_BOOT = (f"if [ -f {OLD_BOOT} ] && [ ! -f {BOOT_SCRIPT} ]; then "
                f"sed -e 's/DROIDBROWSER_NOW/ANDROUNDERTHEHOOD_NOW/g; s/DroidBrowser/AndroUnderTheHood/g; "
                f"s/droidbrowser-sysfs/androunderthehood-sysfs/g' {OLD_BOOT} > {BOOT_SCRIPT} && "
                f"chmod 755 {BOOT_SCRIPT} && rm -f {OLD_BOOT} /data/adb/droidbrowser-sysfs.log; fi; ")

BOOT_HEAD = """#!/system/bin/sh
# Written by AndroUnderTheHood (sysfs > At boot). Re-applies kernel values after every boot.
# Edit it from AndroUnderTheHood; one value per "w" line: w '<path>' '<value>'
DELAY=%(delay)d
LOG=%(log)s
if [ -z "$ANDROUNDERTHEHOOD_NOW" ]; then
  until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 2; done
  sleep $DELAY
fi
echo "# $(date '+%%Y-%%m-%%d %%H:%%M:%%S') ${ANDROUNDERTHEHOOD_NOW:+(applied from AndroUnderTheHood)}" > $LOG
w() {
  if [ ! -e "$1" ]; then echo "skip	$1	missing" >> $LOG; return; fi
  if { printf '%%s' "$2" > "$1"; } 2>/dev/null; then echo "ok	$1	$2" >> $LOG
  else echo "FAIL	$1	$2" >> $LOG; fi
}
"""


class BootSet:
    """Kernel values re-applied at boot, stored in (and read back from) the device's boot script."""

    def __init__(self):
        self.supported = False
        self.entries: dict[str, str] = {}
        self.delay = 20
        self.last: dict[str, str] = {}   # path -> "ok" / "FAIL" / "skip" from the last run
        self.last_when = ""

    def load(self, conn):
        rc, out, _ = conn.exec(
            MIGRATE_BOOT + "if [ -d /data/adb/magisk ] || [ -d /data/adb/ksu ] || [ -d /data/adb/ap ] || "
            f"[ -d {BOOT_DIR} ]; then echo @@OK; fi; cat {BOOT_SCRIPT} 2>/dev/null; echo @@LOG; "
            f"cat {BOOT_LOG} 2>/dev/null", timeout=30)
        text = out.decode("utf-8", errors="replace")
        script, _, logtxt = text.partition("@@LOG")
        self.supported = "@@OK" in script
        self.entries, self.last = {}, {}
        for line in script.split("\n"):
            line = line.strip()
            m = re.match(r"^DELAY=(\d+)", line)
            if m:
                self.delay = int(m.group(1))
            if line.startswith("w "):
                try:
                    parts = shlex.split(line)
                except ValueError:
                    continue
                if len(parts) == 3:
                    self.entries[parts[1]] = parts[2]
        for line in logtxt.split("\n"):
            if line.startswith("# "):
                self.last_when = line[2:].strip()
            f = line.split("\t")
            if len(f) >= 2 and f[0] in ("ok", "FAIL", "skip"):
                self.last[f[1]] = f[0]
        return self

    def script(self) -> str:
        body = BOOT_HEAD % {"delay": self.delay, "log": BOOT_LOG}
        for path, value in sorted(self.entries.items()):
            body += f"w {q(path)} {q(value)}\n"
        return body

    def save(self, conn):
        if not self.entries:
            rc, out, err = conn.exec(f"rm -f {BOOT_SCRIPT} && echo @@OK", timeout=30)
        else:
            blob = base64.b64encode(self.script().encode()).decode()
            rc, out, err = conn.exec(
                f"mkdir -p {BOOT_DIR} && echo {blob} | base64 -d > {BOOT_SCRIPT}.new && "
                f"chmod 755 {BOOT_SCRIPT}.new && mv -f {BOOT_SCRIPT}.new {BOOT_SCRIPT} && echo @@OK", timeout=30)
        if b"@@OK" not in out:
            raise B.BackendError((err or out).decode(errors="replace").strip() or "could not write the boot script")

    def apply_now(self, conn) -> str:
        rc, out, err = conn.exec(f"ANDROUNDERTHEHOOD_NOW=1 sh {BOOT_SCRIPT}; cat {BOOT_LOG}", timeout=60)
        return out.decode(errors="replace")


class BootDialog(QDialog):
    """List / edit the values applied at boot."""

    def __init__(self, dlg: "SysfsDialog"):
        super().__init__(dlg)
        self.dlg = dlg
        self.boot = dlg.boot
        self.setWindowTitle("Kernel values applied at boot")
        self.resize(900, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        intro = QLabel(
            f"These values are written again after every boot by <code>{BOOT_SCRIPT}</code> "
            "(run as root by Magisk / KernelSU after the system has started). Some ROMs reset CPU/GPU limits "
            "shortly after boot – raise the delay if a value does not stick.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(intro)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Path", "Value", "Last boot"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemDoubleClicked.connect(self.open_entry)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Delay after boot:"))
        self.delay = QSpinBox()
        self.delay.setRange(0, 600)
        self.delay.setSuffix(" s")
        self.delay.setValue(self.boot.delay)
        row.addWidget(self.delay)
        self.when = QLabel("")
        self.when.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        row.addWidget(self.when, 1)
        self.btn_remove = QPushButton(theme.line_icon("trash"), " Remove")
        self.btn_remove.clicked.connect(self.remove)
        self.btn_apply = QPushButton(theme.line_icon("refresh"), " Apply all now")
        self.btn_apply.setToolTip("Run the boot script now (without waiting) to check every value is accepted")
        self.btn_apply.clicked.connect(self.apply_now)
        self.btn_save = QPushButton(theme.line_icon("save"), " Save")
        self.btn_save.clicked.connect(self.save)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        for b in (self.btn_remove, self.btn_apply, self.btn_save, close):
            b.setAutoDefault(False)
            row.addWidget(b)
        lay.addLayout(row)
        self.fill()

    def fill(self):
        self.table.setRowCount(0)
        for path, value in sorted(self.boot.entries.items()):
            r = self.table.rowCount()
            self.table.insertRow(r)
            it = QTableWidgetItem(path)
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, 0, it)
            self.table.setItem(r, 1, QTableWidgetItem(value))
            st = self.boot.last.get(path, "")
            si = QTableWidgetItem({"ok": "✔ applied", "FAIL": "✘ rejected", "skip": "– path missing"}.get(st, "–"))
            si.setFlags(si.flags() & ~Qt.ItemFlag.ItemIsEditable)
            si.setForeground(QColor(theme.ACCENT if st == "ok" else "#ff6b6b" if st == "FAIL" else theme.TEXT_DIM))
            self.table.setItem(r, 2, si)
        self.when.setText(f"   last run: {self.boot.last_when}" if self.boot.last_when else "   not run yet")
        self.btn_apply.setEnabled(bool(self.boot.entries))

    def collect(self):
        self.boot.entries = {self.table.item(r, 0).text(): self.table.item(r, 1).text()
                             for r in range(self.table.rowCount())}
        self.boot.delay = self.delay.value()

    def remove(self):
        for r in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(r)

    def save(self, then=None):
        self.collect()

        def done(_):
            self.dlg.boot_changed(f"Boot script saved: {len(self.boot.entries)} value(s).")
            self.fill()
            if then:
                then()
        self.dlg.Task.start(lambda: self.boot.save(self.dlg.conn), done,
                            lambda m: QMessageBox.warning(self, "sysfs", f"Could not save the boot script:\n{m}"))

    def apply_now(self):
        def run():
            def done(_out):
                self.dlg.Task.start(lambda: self.boot.load(self.dlg.conn), lambda _b: (self.fill(),
                                    self.dlg.boot_changed("Boot values applied now."), self.dlg.live_refresh()))
            self.dlg.Task.start(lambda: self.boot.apply_now(self.dlg.conn), done,
                                lambda m: QMessageBox.warning(self, "sysfs", m))
        self.save(then=run)

    def open_entry(self, it):
        path = self.table.item(it.row(), 0).text()
        if it.column() == 0:
            self.dlg._select_after = posixpath.basename(path)
            self.dlg.open_dir(posixpath.dirname(path))
            QTimer.singleShot(1500, self.dlg._select_pending_attr)
