"""Partition manager: view, back up, flash and mount Android partitions."""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import posixpath
import re
import struct
import zlib
from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
                               QMessageBox, QPushButton, QStyledItemDelegate, QTableView, QVBoxLayout)

from . import backend as B
from . import theme

log = logging.getLogger("androunderthehood")
q = B.shq

FLASH_TMP = "/data/local/tmp/androunderthehood-flash.img"

# base names (slot suffix removed) that are routinely flashed: boot images & co.
NORMAL_FLASH = {"boot", "init_boot", "vendor_boot", "recovery", "dtbo", "vbmeta", "vbmeta_system",
                "vbmeta_vendor", "splash", "logo"}
# never written by this tool
FORBIDDEN_FLASH = {"userdata", "super", "metadata", "rawdump", "logdump"}
# mount points that must never be unmounted / remounted from here
PROTECTED_MOUNTS = ("/", "/system", "/system_ext", "/vendor", "/product", "/odm", "/data", "/metadata",
                    "/apex", "/linkerconfig", "/mnt/vendor/persist", "/vendor_dlkm", "/odm_dlkm",
                    "/system_dlkm", "/my_")


def human(n: int | None) -> str:
    if not n:
        return "0" if n == 0 else ""
    f = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or u == "TB":
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return str(n)


@dataclass
class Partition:
    name: str
    dev: str                  # resolved block device (/dev/block/sde13, /dev/block/dm-1)
    link: str                 # path used for I/O (/dev/block/by-name/x or /dev/block/mapper/x)
    size: int = 0
    kind: str = "physical"    # physical | logical | disk
    fstype: str = ""
    mounts: list = field(default_factory=list)   # [(mountpoint, fstype, opts)]
    total: int = 0            # df (bytes)
    used: int = 0
    avail: int = 0

    @property
    def base(self) -> str:
        return re.sub(r"_[ab]$", "", self.name)

    @property
    def slot(self) -> str:
        m = re.search(r"_([ab])$", self.name)
        return m.group(1) if m else ""

    @property
    def mounted(self) -> bool:
        return bool(self.mounts)

    @property
    def mountpoint(self) -> str:
        return self.mounts[0][0] if self.mounts else ""

    @property
    def rw(self) -> bool:
        return bool(self.mounts) and "rw" in self.mounts[0][2].split(",")


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------
# Bulk commands only (no per-device processes): ~1 s instead of ~45 s.
DISCOVER = r"""
echo "@SLOT $(getprop ro.boot.slot_suffix)"
echo "@BYNAME"; ls -l /dev/block/by-name/ 2>/dev/null
echo "@MAPPER"; ls -l /dev/block/mapper/ 2>/dev/null
echo "@SIZES"; for d in /sys/class/block/*; do read s < "$d/size" 2>/dev/null && echo "${d##*/} $s"; done
echo "@DM"; for d in /sys/block/dm-*; do n=""; read n < "$d/dm/name" 2>/dev/null; set -- "$d"/slaves/*; echo "${d##*/}|$n|$*"; done
echo "@MOUNTS"; cat /proc/mounts
echo "@DF"; df -k 2>/dev/null
echo "@BLKID"; blkid 2>/dev/null
"""

# dm devices that are layers on top of a partition, not partitions themselves
LAYER_RE = re.compile(r"(-verity|-cow|-cow-img|-base|_\w+-cow)$")


def _links(lines: list[str], base: str) -> dict[str, str]:
    """Parse `ls -l` output into {name: /dev/block/<dev>}."""
    out = {}
    for line in lines:
        if " -> " not in line:
            continue
        left, target = line.rsplit(" -> ", 1)
        name = left.split()[-1]
        target = target.strip()
        if not target.startswith("/"):
            target = posixpath.normpath(base + "/" + target)
        out[name] = "/dev/block/" + target.rsplit("/", 1)[-1]
    return out


def discover(conn: B.Connection) -> tuple[list[Partition], str]:
    rc, out, err = conn.exec(DISCOVER, timeout=120)
    text = out.decode("utf-8", errors="replace")
    sec: dict[str, list[str]] = {}
    cur = None
    slot = ""
    for line in text.splitlines():
        if line.startswith("@SLOT"):
            slot = line[5:].strip().lstrip("_")
        elif line.startswith("@") and line[1:].isalpha():
            cur = line[1:]
            sec[cur] = []
        elif cur:
            sec[cur].append(line)

    byname = _links(sec.get("BYNAME", []), "/dev/block/by-name")
    mapper = {n: d for n, d in _links(sec.get("MAPPER", []), "/dev/block/mapper").items()
              if not n.startswith("com.android.") and n not in ("by-uuid", "control")}
    sizes = {}
    for line in sec.get("SIZES", []):
        f = line.split()
        if len(f) == 2 and f[1].isdigit():
            sizes["/dev/block/" + f[0]] = int(f[1]) * 512
    slaves: dict[str, list[str]] = {}
    for line in sec.get("DM", []):
        f = line.split("|")
        if len(f) >= 3:
            slaves["/dev/block/" + f[0]] = ["/dev/block/" + x.rsplit("/", 1)[-1] for x in f[2].split()
                                            if not x.endswith("*")]

    parts: dict[str, Partition] = {}       # by resolved device
    result: list[Partition] = []
    for name, dev in sorted(byname.items()):
        kind = "disk" if re.fullmatch(r"sd[a-z]", name) else "physical"
        p = Partition(name, dev, "/dev/block/by-name/" + name, sizes.get(dev, 0), kind)
        result.append(p)
        parts.setdefault(dev, p)
    physical_names = set(byname)
    for name, dev in sorted(mapper.items()):
        if LAYER_RE.search(name) or name in physical_names:
            continue   # verity/snapshot/encryption layer: resolved through `slaves`
        p = Partition(name, dev, "/dev/block/mapper/" + name, sizes.get(dev, 0), "logical")
        result.append(p)
        parts.setdefault(dev, p)
    if not result:
        raise B.BackendError(err.decode(errors="replace").strip() or "no partitions found (root needed)")

    def resolve(path: str) -> str:
        if "/by-name/" in path:
            return byname.get(path.rsplit("/", 1)[-1], path)
        if path.startswith("/dev/block/mapper/"):
            return mapper.get(path.rsplit("/", 1)[-1], path)
        return "/dev/block/" + path.rsplit("/", 1)[-1]

    def owner(dev: str, depth=0) -> Partition | None:
        if dev in parts:
            return parts[dev]
        if depth < 8:
            for sdev in slaves.get(dev, []):
                p = owner(sdev, depth + 1)
                if p:
                    return p
        return None

    for line in sec.get("MOUNTS", []):
        f = line.split()
        if len(f) < 4 or not f[0].startswith("/dev/block/"):
            continue
        p = owner(resolve(f[0]))
        if p and not any(m[0] == f[1] for m in p.mounts):
            p.mounts.append((f[1], f[2], f[3]))
            p.fstype = p.fstype or f[2]
    df = {}
    for line in sec.get("DF", [])[1:]:
        f = line.split()
        if len(f) >= 6 and f[1].isdigit():
            df[f[5]] = (int(f[1]) * 1024, int(f[2]) * 1024, int(f[3]) * 1024)
    blk = {}
    for line in sec.get("BLKID", []):
        m = re.match(r"([^:]+):.*?\bTYPE=\"([^\"]+)\"", line)
        if m:
            blk[resolve(m.group(1))] = m.group(2)
    for p in result:
        p.mounts.sort(key=lambda m: (len(m[0]), m[0]))   # main mount first, bind mounts after
        if p.mounts and p.mountpoint in df:
            p.total, p.used, p.avail = df[p.mountpoint]
        if not p.fstype:
            p.fstype = blk.get(p.dev, "")
    return sorted(result, key=lambda p: (p.kind == "disk", p.name)), slot


def flash_tier(p: Partition) -> tuple[str, str]:
    """('forbidden'|'critical'|'normal', reason)."""
    if p.kind == "disk":
        return "forbidden", "This is a whole storage disk, not a partition."
    if p.base in FORBIDDEN_FLASH:
        return "forbidden", f"'{p.base}' holds the system's live data/layout and is never written by this tool."
    if p.mounted:
        return "forbidden", f"It is mounted at {p.mountpoint}. Unmount it first."
    if p.base in NORMAL_FLASH:
        return "normal", ""
    if p.kind == "logical":
        return "critical", "Logical (dynamic) partition inside super."
    return "critical", ("Bootloader/firmware/modem/calibration data. A wrong image here can hard-brick the "
                        "phone or permanently lose IMEI/radio calibration.")


# --------------------------------------------------------------------------
# image helpers
# --------------------------------------------------------------------------
SPARSE_MAGIC = 0xED26FF3A


def image_kind(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(4096)
    if len(head) >= 4 and struct.unpack("<I", head[:4])[0] == SPARSE_MAGIC:
        return "Android sparse image"
    if head.startswith(b"ANDROID!"):
        return "Android boot image"
    if head.startswith(b"VNDRBOOT"):
        return "Vendor boot image"
    if head.startswith(b"AVB0"):
        return "vbmeta (AVB)"
    if head[:4] == b"\xd0\x0d\xfe\xed":
        return "Device tree blob"
    if head[:4] == b"\xd7\xb7\xab\x1e":
        return "DTBO image"
    if len(head) >= 0x43a and head[0x438:0x43a] == b"\x53\xef":
        return "ext4 filesystem"
    if len(head) >= 1028 and head[1024:1028] == b"\xe2\xe1\xf5\xe0":
        return "EROFS filesystem"
    return "raw image"


def unsparse(src: str, dst: str) -> int:
    """Convert an Android sparse image to a raw image. Returns the raw size."""
    with open(src, "rb") as f, open(dst, "wb") as out:
        hdr = f.read(28)
        magic, major, minor, fhs, chs, blk_sz, total_blks, total_chunks, _crc = struct.unpack("<IHHHHIIII", hdr)
        if magic != SPARSE_MAGIC or major != 1:
            raise ValueError("not a supported sparse image")
        f.seek(fhs)
        for _ in range(total_chunks):
            ctype, _r, nblk, total = struct.unpack("<HHII", f.read(12))
            if chs > 12:
                f.read(chs - 12)
            data_len = total - chs
            if ctype == 0xCAC1:      # raw
                remaining = data_len
                while remaining:
                    b = f.read(min(remaining, 4 << 20))
                    out.write(b)
                    remaining -= len(b)
            elif ctype == 0xCAC2:    # fill
                fill = f.read(4) * (blk_sz // 4)
                for _i in range(nblk):
                    out.write(fill)
            elif ctype == 0xCAC3:    # don't care
                out.seek(nblk * blk_sz, 1)
            elif ctype == 0xCAC4:    # crc32
                f.read(4)
            else:
                raise ValueError(f"unknown sparse chunk type {ctype:#x}")
        out.truncate(total_blks * blk_sz)
        return total_blks * blk_sz


def sha256_file(path: str, limit: int | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        remaining = limit
        while True:
            n = 4 << 20 if remaining is None else min(4 << 20, remaining)
            if n == 0:
                break
            b = f.read(n)
            if not b:
                break
            h.update(b)
            if remaining is not None:
                remaining -= len(b)
    return h.hexdigest()


def backup_partition(conn: B.Connection, p: Partition, path: str, progress, verify=True) -> str:
    """Stream a partition to `path` (gzip on the device, unpacked on the PC). Returns sha256."""
    h = hashlib.sha256()
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    written = [0]
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        def sink(b):
            raw = dec.decompress(b)
            if raw:
                f.write(raw)
                h.update(raw)
                written[0] += len(raw)
                progress(len(raw))
        conn.exec_stream_out(f"gzip -1 -c {q(p.link)}", sink, timeout=24 * 3600)
        raw = dec.flush()
        if raw:
            f.write(raw)
            h.update(raw)
            written[0] += len(raw)
    digest = h.hexdigest()
    if p.size and written[0] != p.size:
        os.remove(tmp)
        raise B.BackendError(f"{p.name}: got {written[0]:,} bytes, expected {p.size:,}")
    if verify:
        rc, out, _ = conn.exec(f"sha256sum {q(p.link)}", timeout=24 * 3600)
        dev_digest = out.decode(errors="replace").split()[0] if out.split() else ""
        if dev_digest != digest:
            os.remove(tmp)
            raise B.BackendError(f"{p.name}: checksum mismatch (device {dev_digest[:16]}…, PC {digest[:16]}…)")
    os.replace(tmp, path)
    with open(path + ".sha256", "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{digest}  {os.path.basename(path)}\n")
    return digest


def backup_dir(device_name: str) -> str:
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    safe = re.sub(r'[<>:"/\\|?*]', "_", device_name)
    d = os.path.join(docs, "AndroUnderTheHood", "Partition backups", safe)
    os.makedirs(d, exist_ok=True)
    return d


def flash_partition(conn: B.Connection, fs: B.FileSystem, p: Partition, image: str, device_name: str,
                    progress) -> str:
    """Back up, upload, verify, write and read-back-verify an image. Returns a report."""
    tier, why = flash_tier(p)
    if tier == "forbidden":
        raise B.BackendError(why)
    work = image
    cleanup = []
    try:
        if image_kind(image) == "Android sparse image":
            progress(0, "Unpacking sparse image…")
            work = image + ".raw"
            unsparse(image, work)
            cleanup.append(work)
        size = os.path.getsize(work)
        if size > p.size:
            raise B.BackendError(f"Image is {size:,} bytes but {p.name} is only {p.size:,} bytes.")
        local_sha = sha256_file(work)

        # 1. safety backup of the current contents
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        bpath = os.path.join(backup_dir(device_name), f"{p.name}-before-flash-{stamp}.img")
        progress(0, f"Backing up current {p.name}…")
        backup_partition(conn, p, bpath, progress, verify=True)

        # 2. upload + verify on the device
        progress(0, "Copying image to the device…")
        fs.upload(work, FLASH_TMP, progress)
        rc, out, _ = conn.exec(f"sha256sum {q(FLASH_TMP)}", timeout=3600)
        if not out.split() or out.split()[0].decode() != local_sha:
            raise B.BackendError("Upload verification failed (checksum mismatch); nothing was written.")

        # 3. write
        progress(0, f"Writing {p.name}…")
        rc, out, err = conn.exec(f"blockdev --setrw {q(p.dev)} 2>/dev/null; "
                                 f"dd if={q(FLASH_TMP)} of={q(p.link)} bs=1048576 conv=fsync,notrunc 2>&1; "
                                 f"r=$?; sync; exit $r", timeout=3600)
        if rc != 0:
            raise B.BackendError("dd failed: " + (out + err).decode(errors="replace").strip()
                                 + f"\n\nThe previous contents are saved in:\n{bpath}")

        # 4. read back
        progress(0, "Verifying…")
        rc, out, _ = conn.exec(f"head -c {size} {q(p.link)} | sha256sum", timeout=3600)
        dev_sha = out.split()[0].decode() if out.split() else ""
        if dev_sha != local_sha:
            raise B.BackendError(f"Read-back verification FAILED for {p.name}!\n\nRestore the backup:\n{bpath}")
        return (f"{p.name} flashed and verified ({size:,} bytes, sha256 {local_sha[:16]}…).\n\n"
                f"Previous contents saved to:\n{bpath}")
    finally:
        try:
            conn.exec(f"rm -f {q(FLASH_TMP)}", timeout=30)
        except Exception:  # noqa: BLE001
            pass
        for c in cleanup:
            try:
                os.remove(c)
            except OSError:
                pass


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
COLS = ["Partition", "Slot", "Size", "Filesystem", "Mounted at", "Usage", "Device"]


class PartModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.parts: list[Partition] = []
        self.slot = ""
        self.lock = theme.line_icon("lock", "#e0a84f")
        self.disk = theme.line_icon("root", theme.TEXT_DIM)
        self.logical = theme.line_icon("package", theme.TEXT_DIM)
        self.normal = theme.line_icon("sdcard", theme.TEXT_DIM)

    def set_parts(self, parts, slot):
        self.beginResetModel()
        self.parts, self.slot = parts, slot
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.parts)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLS)

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLS[s]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        p = self.parts[index.row()]
        c = index.column()
        active = p.slot and p.slot == self.slot
        if role == Qt.ItemDataRole.DisplayRole:
            if c == 0:
                return p.name
            if c == 1:
                return (p.slot.upper() + (" ● active" if active else "")) if p.slot else ""
            if c == 2:
                return human(p.size)
            if c == 3:
                return p.fstype
            if c == 4:
                extra = f" (+{len(p.mounts) - 1})" if len(p.mounts) > 1 else ""
                return (p.mountpoint + ("  rw" if p.rw else "  ro") + extra) if p.mounted else ""
            if c == 5:
                return f"{human(p.used)} / {human(p.total)}" if p.total else ""
            if c == 6:
                return p.dev
        if role == Qt.ItemDataRole.UserRole:  # sort keys
            return [p.name, p.slot, p.size, p.fstype, p.mountpoint, (p.used / p.total) if p.total else -1,
                    p.dev][c]
        if role == Qt.ItemDataRole.UserRole + 1:
            return p
        if role == Qt.ItemDataRole.UserRole + 2 and c == 5:
            return (p.used / p.total) if p.total else None
        if role == Qt.ItemDataRole.DecorationRole and c == 0:
            if p.kind == "disk":
                return self.disk
            if p.kind == "logical":
                return self.logical
            return self.lock if flash_tier(p)[0] == "critical" and not p.mounted else self.normal
        if role == Qt.ItemDataRole.ForegroundRole:
            if c == 1 and active:
                return QColor(theme.ACCENT)
            if p.kind == "disk" or c in (2, 6):
                return QColor(theme.TEXT_DIM)
        if role == Qt.ItemDataRole.ToolTipRole:
            tier, why = flash_tier(p)
            lines = [p.name, f"{p.link} → {p.dev}", f"{human(p.size)} ({p.size:,} bytes)",
                     f"Type: {p.kind}" + (f", filesystem {p.fstype}" if p.fstype else "")]
            for mp, fs, opts in p.mounts:
                lines.append(f"Mounted: {mp} ({fs}, {opts.split(',')[0]})")
            lines.append(f"Flash: {tier}" + (f" – {why}" if why else ""))
            return "\n".join(lines)
        if role == Qt.ItemDataRole.TextAlignmentRole and c == 2:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


class UsageDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option, index):
        frac = index.data(Qt.ItemDataRole.UserRole + 2)
        if frac is None:
            super().paint(painter, option, index)
            return
        from PySide6.QtWidgets import QStyleOptionViewItem
        text_opt = QStyleOptionViewItem(option)
        text_opt.rect = option.rect.adjusted(0, -3, 0, -7)   # text above the bar
        super().paint(painter, text_opt, index)
        r = option.rect.adjusted(6, option.rect.height() - 7, -6, -3)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.BORDER_STRONG))
        painter.drawRoundedRect(QRectF(r), 2, 2)
        col = "#ff6b6b" if frac > 0.9 else ("#e0a84f" if frac > 0.75 else theme.ACCENT)
        painter.setBrush(QColor(col))
        w = max(3.0, r.width() * min(1.0, frac))
        painter.drawRoundedRect(QRectF(r.x(), r.y(), w, r.height()), 2, 2)
        painter.restore()


class PartFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.kind = "all"
        self.slot = ""
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def set(self, text=None, kind=None):
        if text is not None:
            self.text = text.lower()
        if kind is not None:
            self.kind = kind
        self.invalidateFilter() if hasattr(self, "invalidateFilter") else self.invalidate()

    def filterAcceptsRow(self, row, parent):
        p: Partition = self.sourceModel().parts[row]
        k = self.kind
        if k == "mounted" and not p.mounted:
            return False
        if k == "active" and p.slot and p.slot != self.slot:
            return False
        if k == "inactive" and p.slot != ("a" if self.slot == "b" else "b"):
            return False
        if k == "logical" and p.kind != "logical":
            return False
        if k == "physical" and p.kind != "physical":
            return False
        if not self.text:
            return True
        hay = f"{p.name} {p.fstype} {p.mountpoint} {p.dev}".lower()
        return all(w in hay for w in self.text.split())


class PartitionsDialog(QDialog):
    def __init__(self, win, conn: B.Connection):
        super().__init__(win)
        from .ui import Task

        self.Task = Task
        self.win = win
        self.conn = conn
        self.setWindowTitle(f"Partitions – {conn.profile.name}")
        self.resize(1180, 760)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)
        top = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name, filesystem, mount point or device…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(lambda t: (self.proxy.set(text=t), self.update_info()))
        top.addWidget(self.filter, 1)
        self.kind = QComboBox()
        for label, k in (("All", "all"), ("Mounted", "mounted"), ("Active slot + unslotted", "active"),
                         ("Inactive slot", "inactive"), ("Physical", "physical"), ("Logical (in super)", "logical")):
            self.kind.addItem(label, k)
        self.kind.currentIndexChanged.connect(
            lambda _i: (self.proxy.set(kind=self.kind.currentData()), self.update_info()))
        top.addWidget(self.kind)
        self.btn_refresh = QPushButton(theme.line_icon("refresh"), " Refresh")
        self.btn_refresh.clicked.connect(self.load)
        self.btn_backup = QPushButton(theme.line_icon("save"), " Backup selected…")
        self.btn_backup.clicked.connect(lambda: self.backup(self.selected()))
        self.btn_folder = QPushButton(theme.line_icon("folder-plus"), " Backups folder")
        self.btn_folder.clicked.connect(self.open_backup_folder)
        for b in (self.btn_refresh, self.btn_backup, self.btn_folder):
            b.setAutoDefault(False)
            top.addWidget(b)
        lay.addLayout(top)

        self.model = PartModel()
        self.proxy = PartFilter()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        v = self.view = QTableView()
        v.setModel(self.proxy)
        v.setItemDelegateForColumn(5, UsageDelegate(v))
        v.setSortingEnabled(True)
        v.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        v.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        v.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        v.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.setAlternatingRowColors(True)
        v.setShowGrid(False)
        v.setWordWrap(False)
        v.verticalHeader().hide()
        v.verticalHeader().setDefaultSectionSize(30)
        h = v.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for c, w in enumerate((230, 100, 90, 90, 230, 170, 150)):
            v.setColumnWidth(c, w)
        h.setStretchLastSection(True)
        v.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        v.customContextMenuRequested.connect(self.context_menu)
        v.doubleClicked.connect(lambda idx: self.browse(self.part_at(idx)))
        lay.addWidget(v, 1)

        self.info = QLabel("Reading partitions…")
        self.info.setWordWrap(True)
        self.info.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(self.info)

        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.filter.setFocus)
        QShortcut(QKeySequence(Qt.Key.Key_F5), self, activated=self.load)
        QShortcut(QKeySequence.StandardKey.Copy, v, activated=self.copy_paths)
        self.load()

    # ---- data -----------------------------------------------------------
    def load(self):
        conn = self.conn
        self.btn_refresh.setEnabled(False)

        def done(r):
            parts, slot = r
            self.btn_refresh.setEnabled(True)
            self.model.set_parts(parts, slot)
            self.proxy.slot = slot
            self.proxy.set()
            self.update_info()

        def fail(m):
            self.btn_refresh.setEnabled(True)
            self.info.setText(f"Could not read partitions: {m}")
        self.Task.start(lambda: discover(conn), done, fail)

    def update_info(self):
        ps = self.model.parts
        phys = sum(1 for p in ps if p.kind == "physical")
        logi = sum(1 for p in ps if p.kind == "logical")
        mnt = sum(1 for p in ps if p.mounted)
        slot = self.model.slot.upper() or "none (not A/B)"
        self.info.setText(
            f"{self.proxy.rowCount()} shown · {phys} physical · {logi} logical · {mnt} mounted · active slot: {slot}"
            f"   —   right-click for backup / flash / mount · double-click a mounted partition to browse it · "
            f"backups go to {backup_dir(self.conn.profile.name)}")

    def part_at(self, idx) -> Partition | None:
        return self.proxy.data(self.proxy.index(idx.row(), 0), Qt.ItemDataRole.UserRole + 1) if idx.isValid() \
            else None

    def selected(self) -> list[Partition]:
        rows = self.view.selectionModel().selectedRows(0)
        return [p for p in (self.proxy.data(r, Qt.ItemDataRole.UserRole + 1) for r in rows) if p]

    # ---- menu -----------------------------------------------------------
    def context_menu(self, pos):
        idx = self.view.indexAt(pos)
        if idx.isValid() and not self.view.selectionModel().isSelected(idx):
            self.view.selectRow(idx.row())
        sel = self.selected()
        if not sel:
            return
        p = sel[0]
        one = len(sel) == 1
        L = theme.line_icon
        m = QMenu(self)
        if one:
            h = m.addAction(f"{p.name}  ·  {human(p.size)}" + (f"  ·  {p.fstype}" if p.fstype else ""))
            h.setEnabled(False)
            m.addSeparator()
        if one:
            m.addAction(L("list"), "Open in hex editor (read-only)",
                        lambda: self.win.open_hex(p.link, p.size, readonly=True, title=p.name))
            if p.kind != "disk":
                ow = m.addMenu(L("forward"), "Open with")
                self.win.fill_open_with(ow, lambda app: self.open_with(p, app))
            m.addSeparator()
        m.addAction(L("save"), "Backup to PC…" if one else f"Backup {len(sel)} partitions to PC…",
                    lambda: self.backup(sel))
        if one:
            tier, why = flash_tier(p)
            fa = m.addAction(L("download"), "Flash image…" if tier != "forbidden" else "Flash image (not allowed)",
                             lambda: self.flash(p))
            fa.setEnabled(tier != "forbidden")
            if tier == "forbidden":
                fa.setToolTip(why)
            m.setToolTipsVisible(True)
            m.addSeparator()
            if p.mounted:
                m.addAction(L("forward"), f"Browse {p.mountpoint}", lambda: self.browse(p))
                prot = self.protected(p.mountpoint)
                for label, opt in (("Remount read-write", "rw"), ("Remount read-only", "ro")):
                    a = m.addAction(L("lock") if opt == "ro" else L("rename"), label,
                                    lambda o=opt: self.remount(p, o))
                    a.setEnabled(not prot and (p.rw != (opt == "rw")))
                ua = m.addAction(L("disconnect"), "Unmount", lambda: self.unmount(p))
                ua.setEnabled(not prot)
                if prot:
                    note = m.addAction(f"({p.mountpoint} is a core system mount – locked)")
                    note.setEnabled(False)
            elif p.kind != "disk" and p.fstype:
                m.addAction(L("forward"), "Mount read-only", lambda: self.mount(p, "ro"))
                m.addAction(L("rename"), "Mount read-write", lambda: self.mount(p, "rw"))
            m.addSeparator()
        m.addAction(L("copy"), "Copy device path(s)", self.copy_paths)
        m.exec(self.view.viewport().mapToGlobal(pos))

    @staticmethod
    def protected(mp: str) -> bool:
        return any(mp == x or (x.endswith("_") and mp.startswith(x)) for x in PROTECTED_MOUNTS) or \
            mp.startswith("/apex/")

    def copy_paths(self):
        sel = self.selected()
        if sel:
            QApplication.clipboard().setText("\n".join(p.link for p in sel))

    def browse(self, p: Partition | None):
        if p and p.mounted:
            self.win.navigate(p.mountpoint)
            self.win.raise_()
            self.win.activateWindow()

    def open_backup_folder(self):
        os.startfile(backup_dir(self.conn.profile.name))  # type: ignore[attr-defined]

    # ---- backup ---------------------------------------------------------
    def backup(self, parts: list[Partition]):
        if not parts:
            return
        big = [p for p in parts if p.size > 8 << 30]
        if big and not self.ask(f"{', '.join(p.name for p in big)} {'is' if len(big) == 1 else 'are'} very large "
                                f"({human(sum(p.size for p in big))}). This can take a long time and needs that "
                                "much free space on the PC. Continue?"):
            return
        default = backup_dir(self.conn.profile.name)
        dest = QFileDialog.getExistingDirectory(self, "Back up partitions to", default)
        if not dest:
            return
        conn = self.conn
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        total = [sum(p.size for p in parts) or 1]

        def job(fs, progress):
            done = []
            for p in parts:
                progress(0, f"Reading {p.name} ({human(p.size)})")
                path = os.path.join(dest, f"{p.name}-{stamp}.img")
                digest = backup_partition(conn, p, path, progress, verify=True)
                done.append((p.name, path, digest))
            return done

        def finished(done):
            if done:
                QMessageBox.information(self, "Backup complete",
                                        f"{len(done)} partition image(s) saved and verified (SHA-256 matches the "
                                        f"device):\n\n" + "\n".join(f"{n}  →  {os.path.basename(pth)}"
                                                                    for n, pth, _d in done[:25])
                                        + f"\n\nFolder: {dest}")
        self.win._transfer("Backing up partitions", total, job, finished)

    def open_with(self, p: Partition, app: str | None):
        """Dump the partition to a temporary .img and open it in an external program (read-only:
        changes are never written back – use Flash for that)."""
        if p.size > 4 << 30 and not self.ask(f"{p.name} is {human(p.size)}. Dump it to a temporary file "
                                             "to open it?"):
            return
        import tempfile
        d = os.path.join(tempfile.gettempdir(), "AndroUnderTheHood", "partitions",
                         re.sub(r'[<>:"/\\|?*]', "_", self.conn.profile.name))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{p.name}.img")
        conn = self.conn

        def job(fs, progress):
            progress(0, f"Reading {p.name}")
            backup_partition(conn, p, path, progress, verify=False)
            return path

        def done(res):
            if res:
                self.win.launch_local(res, app)
        self.win._transfer(f"Dumping {p.name}", [p.size or 1], job, done)

    # ---- flash ----------------------------------------------------------
    def flash(self, p: Partition):
        tier, why = flash_tier(p)
        if tier == "forbidden":
            QMessageBox.warning(self, "Flash not allowed", why)
            return
        img, _ = QFileDialog.getOpenFileName(self, f"Image to flash to {p.name}",
                                             self.win.cfg.data.get("last_flash_dir", backup_dir(self.conn.profile.name)),
                                             "Images (*.img *.bin *.mbn *.elf);;All files (*)")
        if not img:
            return
        self.win.cfg.data["last_flash_dir"] = os.path.dirname(img)
        self.win.cfg.save()
        kind = image_kind(img)
        size = os.path.getsize(img)
        dlg = FlashConfirm(self, p, img, kind, size, tier, why, self.model.slot)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        conn, name = self.conn, self.conn.profile.name
        total = [p.size * 2 + size]

        def job(fs, progress):
            return flash_partition(conn, fs, p, img, name, progress)

        def finished(report):
            if report:
                QMessageBox.information(self, "Flash complete", report)
                self.load()
        self.win._transfer(f"Flashing {p.name}", total, job, finished)

    # ---- mount ----------------------------------------------------------
    def ask(self, text: str) -> bool:
        return QMessageBox.question(self, "Partitions", text) == QMessageBox.StandardButton.Yes

    def run(self, title: str, cmd: str):
        conn = self.conn

        def done(r):
            rc, out, err = r
            msg = (out + err).decode(errors="replace").strip()
            if rc != 0:
                QMessageBox.warning(self, title, msg or f"exit code {rc}")
            else:
                self.win.statusBar().showMessage(f"{title}: done", 5000)
            self.load()
        self.Task.start(lambda: conn.exec(cmd, timeout=120), done, lambda m: QMessageBox.warning(self, title, m))

    def mount(self, p: Partition, mode: str):
        mp = f"/mnt/androunderthehood/{p.name}"
        if mode == "rw" and not self.ask(f"Mount {p.name} READ-WRITE at {mp}?\n\nChanges you make to files there "
                                         "are written straight to the partition."):
            return
        self.run("Mount", f"mkdir -p {q(mp)} && mount -t {q(p.fstype)} -o {mode} {q(p.link)} {q(mp)} 2>&1")

    def unmount(self, p: Partition):
        if self.protected(p.mountpoint):
            return
        if not self.ask(f"Unmount {p.mountpoint} ({p.name})?\n\nApps or services using it may misbehave."):
            return
        self.run("Unmount", f"umount {q(p.mountpoint)} 2>&1 && "
                            f"case {q(p.mountpoint)} in /mnt/androunderthehood/*) rmdir {q(p.mountpoint)};; esac")

    def remount(self, p: Partition, mode: str):
        if self.protected(p.mountpoint):
            return
        if mode == "rw" and not self.ask(f"Remount {p.mountpoint} ({p.name}) READ-WRITE?"):
            return
        self.run(f"Remount {mode}", f"mount -o remount,{mode} {q(p.mountpoint)} 2>&1")


class FlashConfirm(QDialog):
    """Explicit confirmation: shows everything and requires typing the partition name."""

    def __init__(self, parent, p: Partition, img: str, kind: str, size: int, tier: str, why: str, slot: str):
        super().__init__(parent)
        self.p = p
        self.setWindowTitle(f"Flash {p.name}?")
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        warn = QLabel(("⚠  CRITICAL PARTITION\n" + why) if tier == "critical" else
                      "Writing a partition replaces its contents. A backup of the current contents is made "
                      "automatically first.")
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #ff8f8f; font-weight: 600;" if tier == "critical" else f"color: {theme.TEXT_DIM};")
        lay.addWidget(warn)
        form = QFormLayout()
        active = p.slot and p.slot == slot
        rows = [("Partition", f"{p.name}   ({p.link} → {p.dev})"),
                ("Slot", (p.slot.upper() + ("  — ACTIVE (in use right now)" if active else "  — inactive"))
                 if p.slot else "not slotted"),
                ("Partition size", f"{human(p.size)} ({p.size:,} bytes)"),
                ("Image", img),
                ("Image type", kind),
                ("Image size", f"{human(size)} ({size:,} bytes)"
                 + ("" if kind == "Android sparse image" else
                    ("  ✔ exact match" if size == p.size else "  (smaller – the rest is left as is)"
                     if size < p.size else "  ✘ TOO LARGE")))]
        for k, v in rows:
            lab = QLabel(v)
            lab.setWordWrap(True)
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(k, lab)
        lay.addLayout(form)
        steps = QLabel("Steps: 1) back up current contents to the PC (verified) · 2) copy the image to the phone "
                       "and verify its checksum · 3) write with dd + fsync · 4) read back and verify.")
        steps.setWordWrap(True)
        steps.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(steps)
        self.typed = QLineEdit()
        self.typed.setPlaceholderText(f"Type {p.name} to confirm")
        self.typed.textChanged.connect(self.check)
        lay.addWidget(self.typed)
        self.ack = None
        if tier == "critical":
            self.ack = QCheckBox("I understand this can brick the phone or destroy IMEI/calibration data, "
                                 "and I have a way to recover it.")
            self.ack.toggled.connect(self.check)
            lay.addWidget(self.ack)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText(f"Flash {p.name}")
        self.ok.setEnabled(False)
        bb.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.too_large = kind != "Android sparse image" and size > p.size

    def check(self, *_):
        ok = self.typed.text().strip() == self.p.name and (self.ack is None or self.ack.isChecked()) \
            and not self.too_large
        self.ok.setEnabled(ok)
