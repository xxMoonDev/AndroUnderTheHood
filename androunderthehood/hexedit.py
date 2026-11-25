"""Hex editor / binary analyzer for files and block devices on the device."""
from __future__ import annotations

import base64
import collections
import logging
import re
import struct
import threading

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QAbstractScrollArea, QApplication, QCheckBox, QComboBox, QDialog,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
                               QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from . import backend as B
from . import binscan
from . import theme

log = logging.getLogger("androunderthehood")
q = B.shq
PAGE = 65536


def human(n: int) -> str:
    f = float(n)
    for u in ("bytes", "KB", "MB", "GB", "TB"):
        if f < 1024 or u == "TB":
            return f"{int(f)} bytes" if u == "bytes" else f"{f:.1f} {u}"
        f /= 1024
    return str(n)


# --------------------------------------------------------------------------
# data source: 64 KB pages read lazily with dd, LRU cached
# --------------------------------------------------------------------------
class ByteSource(QObject):
    page_ready = Signal(int)

    MAX_PAGES = 512   # 32 MB cache

    def __init__(self, conn: B.Connection, path: str, size: int):
        super().__init__()
        self.conn, self.path, self.size = conn, path, size
        self.pages: collections.OrderedDict[int, bytes] = collections.OrderedDict()
        self.pending: set[int] = set()
        self.queue: collections.deque[int] = collections.deque()
        self.cond = threading.Condition()
        self.closed = False
        self.error: str | None = None
        threading.Thread(target=self._worker, daemon=True).start()

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def _fetch(self, page: int) -> bytes:
        rc, out, err = self.conn.exec(f"dd if={q(self.path)} bs={PAGE} skip={page} count=1 2>/dev/null",
                                      timeout=120)
        if rc != 0 and not out:
            raise B.BackendError((err or b"").decode(errors="replace").strip() or f"read failed ({rc})")
        return out

    def _worker(self):
        while True:
            with self.cond:
                while not self.queue and not self.closed:
                    self.cond.wait()
                if self.closed:
                    return
                page = self.queue.popleft()
            try:
                data = self._fetch(page)
                self.error = None
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
                data = None
            with self.cond:
                self.pending.discard(page)
                if data is not None:
                    self.pages[page] = data
                    while len(self.pages) > self.MAX_PAGES:
                        self.pages.popitem(last=False)
            try:
                self.page_ready.emit(page)
            except RuntimeError:
                return

    def get(self, off: int) -> int | None:
        page, i = divmod(off, PAGE)
        with self.cond:
            data = self.pages.get(page)
            if data is not None:
                self.pages.move_to_end(page)
                return data[i] if i < len(data) else None
            if page not in self.pending:
                self.pending.add(page)
                self.queue.appendleft(page)   # most recent request first
                self.cond.notify()
        return None

    def _fetch_run(self, page: int, count: int) -> dict[int, bytes]:
        """Read `count` consecutive pages with one dd (bulk reads: search, copy, verify)."""
        rc, out, err = self.conn.exec(f"dd if={q(self.path)} bs={PAGE} skip={page} count={count} 2>/dev/null",
                                      timeout=300)
        if rc != 0 and not out:
            raise B.BackendError((err or b"").decode(errors="replace").strip() or f"read failed ({rc})")
        return {page + k: out[k * PAGE:(k + 1) * PAGE] for k in range((len(out) + PAGE - 1) // PAGE)}

    def read_sync(self, off: int, n: int) -> bytes:
        """Blocking read (call from a worker thread)."""
        out = bytearray()
        end = min(self.size, off + n) if self.size else off + n
        while off < end:
            page, i = divmod(off, PAGE)
            with self.cond:
                data = self.pages.get(page)
            if data is None:
                # fetch the whole missing stretch (max 4 MB) in one command
                last = (end - 1) // PAGE
                count = 1
                with self.cond:
                    while count < 64 and page + count <= last and (page + count) not in self.pages:
                        count += 1
                got = self._fetch_run(page, count)
                with self.cond:
                    for pg, d in got.items():
                        self.pages[pg] = d
                    while len(self.pages) > self.MAX_PAGES:
                        self.pages.popitem(last=False)
                data = got.get(page, b"")
            if i >= len(data):
                break
            take = min(len(data) - i, end - off)
            out += data[i:i + take]
            off += take
        return bytes(out)

    def invalidate(self, pages=None):
        with self.cond:
            if pages is None:
                self.pages.clear()
            else:
                for p in pages:
                    self.pages.pop(p, None)


# --------------------------------------------------------------------------
# hex view widget
# --------------------------------------------------------------------------
_MISSING = object()


class HexView(QAbstractScrollArea):
    cursor_moved = Signal()
    mods_changed = Signal()

    def __init__(self, src: ByteSource, editable_allowed: bool):
        super().__init__()
        self.src = src
        self.editable_allowed = editable_allowed
        self.editable = False
        self.mods: dict[int, int] = {}
        self.undo: list[tuple[int, object]] = []
        self.cur = 0
        self.anchor: int | None = None
        self.area = "hex"
        self.nibble = 0
        self.flash: tuple[int, int] | None = None
        f = QFont("Consolas", 10)
        f.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(f)
        fm = QFontMetricsF(f)
        self.cw = fm.horizontalAdvance("0")
        self.lh = fm.height() + 3
        self.asc = fm.ascent()
        self.off_digits = max(8, len(f"{max(0, src.size - 1):X}"))
        self.x_off = 10
        self.x_hex = self.x_off + (self.off_digits + 3) * self.cw
        self.x_asc = self.x_hex + (16 * 3 + 2) * self.cw
        self.setMinimumWidth(int(self.x_asc + 17 * self.cw + 30))
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self.viewport().update())
        src.page_ready.connect(lambda _p: self.viewport().update())
        self.col_text = QColor(theme.TEXT)
        self.col_dim = QColor(theme.TEXT_FAINT)
        self.col_off = QColor(theme.TEXT_DIM)
        self.col_mod = QColor("#ffb454")
        self.col_sel = QColor(61, 220, 132, 70)
        self.col_flash = QColor(255, 180, 84, 70)
        self.col_cursor = QColor(theme.ACCENT)
        self.update_scroll()

    # ---- geometry ------------------------------------------------------------
    @property
    def rows(self) -> int:
        return (self.src.size + 15) // 16

    def vis_rows(self) -> int:
        return max(1, int(self.viewport().height() // self.lh))

    def update_scroll(self):
        sb = self.verticalScrollBar()
        sb.setRange(0, max(0, self.rows - self.vis_rows()))
        sb.setPageStep(self.vis_rows())
        sb.setSingleStep(1)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.update_scroll()

    def hex_x(self, i: int) -> float:
        return self.x_hex + i * 3 * self.cw + (self.cw if i >= 8 else 0)

    def hit(self, x: float, y: float) -> tuple[int, str, int]:
        row = self.verticalScrollBar().value() + int(y // self.lh)
        if x >= self.x_asc - self.cw:
            i = int((x - self.x_asc) // self.cw)
            area, nib = "ascii", 0
        else:
            rel = x - self.x_hex
            if rel >= 8 * 3 * self.cw:
                rel -= self.cw
            i = int(rel // (3 * self.cw))
            nib = 1 if (rel - i * 3 * self.cw) >= self.cw else 0
            area = "hex"
        i = max(0, min(15, i))
        off = max(0, min(max(0, self.src.size - 1), row * 16 + i))
        return off, area, nib

    # ---- data ----------------------------------------------------------------
    def byte(self, off: int) -> int | None:
        v = self.mods.get(off)
        return v if v is not None else self.src.get(off)

    def selection(self) -> tuple[int, int]:
        """(start, length); length 0 = no selection."""
        if self.anchor is None or self.anchor == self.cur:
            return self.cur, 0
        a, b = sorted((self.anchor, self.cur))
        return a, b - a + 1

    # ---- painting --------------------------------------------------------------
    def paintEvent(self, ev):
        p = QPainter(self.viewport())
        p.fillRect(self.viewport().rect(), QColor(theme.CARD))
        p.setFont(self.font())
        top = self.verticalScrollBar().value()
        sel_s, sel_n = self.selection()
        fl = self.flash
        size = self.src.size
        for r in range(self.vis_rows() + 1):
            row = top + r
            base = row * 16
            if base >= size:
                break
            y = r * self.lh
            ty = y + self.asc + 1
            p.setPen(self.col_off)
            p.drawText(QPointF(self.x_off, ty), f"{base:0{self.off_digits}X}")
            for i in range(16):
                o = base + i
                if o >= size:
                    break
                b = self.byte(o)
                hx = self.hex_x(i)
                ax = self.x_asc + i * self.cw
                in_sel = sel_n and sel_s <= o < sel_s + sel_n
                in_flash = fl and fl[0] <= o < fl[0] + fl[1]
                if in_sel or in_flash:
                    col = self.col_sel if in_sel else self.col_flash
                    w = 3 * self.cw if (i != 7 and i != 15) else 2 * self.cw
                    if i == 7:
                        w = 3 * self.cw
                    p.fillRect(QRectF(hx - self.cw * 0.5, y, w, self.lh), col)
                    p.fillRect(QRectF(ax, y, self.cw, self.lh), col)
                if o == self.cur:
                    pen_col = self.col_cursor
                    p.setPen(pen_col)
                    if self.area == "hex":
                        cx = hx + self.nibble * self.cw
                        p.drawRect(QRectF(cx - 0.5, y + 1, self.cw, self.lh - 2))
                        p.drawLine(QPointF(ax, y + self.lh - 1), QPointF(ax + self.cw, y + self.lh - 1))
                    else:
                        p.drawRect(QRectF(ax - 0.5, y + 1, self.cw, self.lh - 2))
                        p.drawLine(QPointF(hx, y + self.lh - 1), QPointF(hx + 2 * self.cw, y + self.lh - 1))
                if b is None:
                    p.setPen(self.col_dim)
                    p.drawText(QPointF(hx, ty), "··")
                    p.drawText(QPointF(ax, ty), "·")
                    continue
                modified = o in self.mods
                p.setPen(self.col_mod if modified else (self.col_dim if b == 0 else self.col_text))
                p.drawText(QPointF(hx, ty), f"{b:02X}")
                p.setPen(self.col_mod if modified else (self.col_text if 0x20 <= b < 0x7F else self.col_dim))
                p.drawText(QPointF(ax, ty), chr(b) if 0x20 <= b < 0x7F else ".")
        # column separators
        p.setPen(QColor(theme.BORDER))
        x1 = self.x_hex - 1.5 * self.cw
        x2 = self.x_asc - 1.0 * self.cw
        h = self.viewport().height()
        p.drawLine(QPointF(x1, 0), QPointF(x1, h))
        p.drawLine(QPointF(x2, 0), QPointF(x2, h))
        p.end()

    # ---- navigation ------------------------------------------------------------
    def ensure_visible(self, off: int):
        row = off // 16
        sb = self.verticalScrollBar()
        if row < sb.value():
            sb.setValue(row)
        elif row >= sb.value() + self.vis_rows():
            sb.setValue(row - self.vis_rows() + 1)

    def goto(self, off: int, length: int = 0, center=True):
        off = max(0, min(max(0, self.src.size - 1), off))
        self.cur = off
        self.anchor = None
        self.nibble = 0
        self.flash = (off, length) if length else None
        if center:
            self.verticalScrollBar().setValue(max(0, off // 16 - self.vis_rows() // 3))
        else:
            self.ensure_visible(off)
        self.viewport().update()
        self.cursor_moved.emit()

    def move_to(self, off: int, extend: bool):
        off = max(0, min(max(0, self.src.size - 1), off))
        if extend:
            if self.anchor is None:
                self.anchor = self.cur
        else:
            self.anchor = None
        self.cur = off
        self.nibble = 0
        self.flash = None
        self.ensure_visible(off)
        self.viewport().update()
        self.cursor_moved.emit()

    def mousePressEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(ev)
        off, area, nib = self.hit(ev.position().x(), ev.position().y())
        extend = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.area = area
        self.move_to(off, extend)
        if not extend:
            self.anchor = off
        self.nibble = nib if area == "hex" else 0
        self.viewport().update()

    def mouseMoveEvent(self, ev):
        if ev.buttons() & Qt.MouseButton.LeftButton:
            off, _a, _n = self.hit(ev.position().x(), max(0.0, ev.position().y()))
            if self.anchor is None:
                self.anchor = self.cur
            self.cur = off
            self.ensure_visible(off)
            self.viewport().update()
            self.cursor_moved.emit()

    def wheelEvent(self, ev):
        steps = -ev.angleDelta().y() // 40
        sb = self.verticalScrollBar()
        sb.setValue(sb.value() + int(steps))

    def keyPressEvent(self, ev):
        k = ev.key()
        mod = ev.modifiers()
        shift = bool(mod & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mod & Qt.KeyboardModifier.ControlModifier)
        c = self.cur
        page = 16 * self.vis_rows()
        moves = {
            Qt.Key.Key_Left: c - 1, Qt.Key.Key_Right: c + 1, Qt.Key.Key_Up: c - 16, Qt.Key.Key_Down: c + 16,
            Qt.Key.Key_PageUp: c - page, Qt.Key.Key_PageDown: c + page,
            Qt.Key.Key_Home: 0 if ctrl else c - c % 16,
            Qt.Key.Key_End: self.src.size - 1 if ctrl else min(self.src.size - 1, c - c % 16 + 15),
        }
        if k in moves:
            if k in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self.area == "hex" and not shift and not ctrl:
                # move by nibble in the hex area
                if k == Qt.Key.Key_Right and self.nibble == 0:
                    self.nibble = 1
                    self.viewport().update()
                    return
                if k == Qt.Key.Key_Left and self.nibble == 1:
                    self.nibble = 0
                    self.viewport().update()
                    return
            self.move_to(moves[k], shift)
            if k == Qt.Key.Key_Left and self.area == "hex" and not shift:
                self.nibble = 1
            return
        if k == Qt.Key.Key_Tab:
            self.area = "ascii" if self.area == "hex" else "hex"
            self.nibble = 0
            self.viewport().update()
            return
        if ctrl and k == Qt.Key.Key_Z:
            self.undo_last()
            return
        txt = ev.text()
        if self.editable and txt and not ctrl:
            if self.area == "hex" and txt.lower() in "0123456789abcdef" and len(txt) == 1:
                self.set_nibble(int(txt, 16))
                return
            if self.area == "ascii" and len(txt) == 1 and 0x20 <= ord(txt) < 0x7F:
                self.set_byte(self.cur, ord(txt))
                self.move_to(self.cur + 1, False)
                return
        super().keyPressEvent(ev)

    # ---- editing ---------------------------------------------------------------
    def set_byte(self, off: int, val: int):
        orig = self.src.get(off)
        prev = self.mods.get(off, _MISSING)
        self.undo.append((off, prev))
        if orig is not None and val == orig:
            self.mods.pop(off, None)
        else:
            self.mods[off] = val
        self.viewport().update()
        self.mods_changed.emit()

    def set_nibble(self, d: int):
        b = self.byte(self.cur)
        if b is None:
            return   # page not loaded yet
        if self.nibble == 0:
            nb = (b & 0x0F) | (d << 4)
            self.set_byte(self.cur, nb)
            self.nibble = 1
            self.viewport().update()
        else:
            nb = (b & 0xF0) | d
            self.set_byte(self.cur, nb)
            if self.cur < self.src.size - 1:
                self.move_to(self.cur + 1, False)

    def undo_last(self):
        if not self.undo:
            return
        off, prev = self.undo.pop()
        if prev is _MISSING:
            self.mods.pop(off, None)
        else:
            self.mods[off] = prev
        self.goto(off, center=False)
        self.mods_changed.emit()

    def contextMenuEvent(self, ev):
        self.parent_dialog.view_menu(ev.globalPos())


# --------------------------------------------------------------------------
# entropy strip
# --------------------------------------------------------------------------
class EntropyStrip(QWidget):
    clicked = Signal(int)

    def __init__(self):
        super().__init__()
        self.values: list[float] = []
        self.block = 65536
        self.size = 0
        self.setMinimumHeight(70)
        self.setToolTip("Entropy (compressibility) per 64 KB block – high = compressed/encrypted, "
                        "low = text/empty. Click to jump.")

    def set_values(self, values, block, size):
        self.values, self.block, self.size = values, block, size
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QColor(theme.BORDER))
        p.setBrush(QColor(theme.BG))
        p.drawRoundedRect(r, 6, 6)
        if not self.values:
            p.setPen(QColor(theme.TEXT_FAINT))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "Entropy graph appears after Analyze")
            return
        n = len(self.values)
        inner = r.adjusted(4, 4, -4, -4)
        w = inner.width() / n
        for i, v in enumerate(self.values):
            h = inner.height() * v
            col = QColor("#ff6b6b") if v > 0.95 else (QColor("#e0a84f") if v > 0.75 else QColor(theme.ACCENT))
            p.fillRect(QRectF(inner.x() + i * w, inner.bottom() - h, max(1.0, w), h), col)

    def mousePressEvent(self, ev):
        if self.values:
            frac = (ev.position().x() - 5) / max(1, self.width() - 10)
            self.clicked.emit(max(0, min(self.size - 1, int(frac * len(self.values)) * self.block)))


# --------------------------------------------------------------------------
# dialog
# --------------------------------------------------------------------------
class Cancelled(Exception):
    pass


class HexDialog(QDialog):
    def __init__(self, win, conn: B.Connection, path: str, size: int, readonly: bool, title: str | None = None,
                 kind: str = "file"):
        super().__init__(win)
        from .ui import Task

        self.Task = Task
        self.win = win
        self.conn = conn
        self.path = path
        self.readonly = readonly
        self.kind = kind
        self.setWindowTitle(f"{title or path} – Hex" + (" (read-only)" if readonly else ""))
        self.resize(1360, 860)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.src = ByteSource(conn, path, size)
        self.cancel_flag = False
        self.busy = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(8)

        # ---- toolbar
        top = QHBoxLayout()
        self.goto_edit = QLineEdit()
        self.goto_edit.setPlaceholderText("Go to offset (0x… or decimal, +/- relative)")
        self.goto_edit.setMaximumWidth(260)
        self.goto_edit.returnPressed.connect(self.do_goto)
        top.addWidget(self.goto_edit)
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Find…")
        self.find_edit.returnPressed.connect(lambda: self.find(forward=True))
        top.addWidget(self.find_edit, 1)
        self.find_mode = QComboBox()
        for m in ("Text", "Hex", "UTF-16"):
            self.find_mode.addItem(m)
        top.addWidget(self.find_mode)
        self.find_case = QCheckBox("Aa")
        self.find_case.setToolTip("Match case (text search)")
        top.addWidget(self.find_case)
        self.btn_prev = QPushButton(theme.line_icon("up"), "")
        self.btn_prev.setToolTip("Find previous (Shift+F3)")
        self.btn_prev.clicked.connect(lambda: self.find(forward=False))
        self.btn_next = QPushButton(theme.line_icon("down"), "")
        self.btn_next.setToolTip("Find next (F3)")
        self.btn_next.clicked.connect(lambda: self.find(forward=True))
        top.addWidget(self.btn_prev)
        top.addWidget(self.btn_next)
        self.btn_edit = QPushButton(theme.line_icon("rename"), " Edit")
        self.btn_edit.setCheckable(True)
        self.btn_edit.setEnabled(not readonly)
        self.btn_edit.setToolTip("Read-only: block devices are written with the partition manager's Flash"
                                 if readonly else "Enable overwriting bytes (type in the hex or text column)")
        self.btn_edit.toggled.connect(self.set_editable)
        self.btn_save = QPushButton(theme.line_icon("save"), " Save")
        self.btn_save.clicked.connect(self.save)
        self.btn_revert = QPushButton(theme.line_icon("refresh"), " Revert")
        self.btn_revert.clicked.connect(self.revert)
        for b in (self.btn_edit, self.btn_save, self.btn_revert):
            top.addWidget(b)
        for b in (self.btn_prev, self.btn_next, self.btn_edit, self.btn_save, self.btn_revert):
            b.setAutoDefault(False)
        lay.addLayout(top)

        # ---- center
        split = QSplitter()
        self.view = HexView(self.src, not readonly)
        self.view.parent_dialog = self
        self.view.cursor_moved.connect(self.on_cursor)
        self.view.mods_changed.connect(self.on_mods)
        split.addWidget(self.view)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        # inspector
        self.insp = QTableWidget(0, 2)
        self.insp.horizontalHeader().hide()
        self.insp.verticalHeader().hide()
        self.insp.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.insp.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.insp.horizontalHeader().setStretchLastSection(True)
        self.insp.setShowGrid(False)
        self.insp.setFont(QFont("Consolas", 9))
        self.tabs.addTab(self.insp, "Inspector")
        # signatures
        sigw = QWidget()
        sl = QVBoxLayout(sigw)
        sl.setContentsMargins(0, 6, 0, 0)
        arow = QHBoxLayout()
        self.btn_analyze = QPushButton(theme.line_icon("star"), " Analyze")
        self.btn_analyze.setToolTip("Scan the whole file once: signatures (binwalk-style), strings, entropy")
        self.btn_analyze.clicked.connect(self.analyze)
        self.btn_analyze.setAutoDefault(False)
        self.min_len = QSpinBox()
        self.min_len.setRange(3, 64)
        self.min_len.setValue(5)
        self.min_len.setPrefix("strings ≥ ")
        self.analyze_status = QLabel("")
        self.analyze_status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        arow.addWidget(self.btn_analyze)
        arow.addWidget(self.min_len)
        arow.addWidget(self.analyze_status, 1)
        sl.addLayout(arow)
        self.entropy = EntropyStrip()
        self.entropy.clicked.connect(lambda off: self.view.goto(off))
        sl.addWidget(self.entropy)
        self.sig_table = self._table(["Offset", "Type", "Description"])
        self.sig_table.cellDoubleClicked.connect(lambda r, _c: self.jump_table(self.sig_table, r))
        self.sig_table.cellClicked.connect(lambda r, _c: self.jump_table(self.sig_table, r))
        sl.addWidget(self.sig_table, 1)
        self.tabs.addTab(sigw, "Signatures")
        # strings
        strw = QWidget()
        stl = QVBoxLayout(strw)
        stl.setContentsMargins(0, 6, 0, 0)
        self.str_filter = QLineEdit()
        self.str_filter.setPlaceholderText("Filter strings… (run Analyze first)")
        self.str_filter.setClearButtonEnabled(True)
        self.str_filter.textChanged.connect(self.fill_strings)
        stl.addWidget(self.str_filter)
        self.str_table = self._table(["Offset", "Enc", "String"])
        self.str_table.cellClicked.connect(lambda r, _c: self.jump_table(self.str_table, r))
        stl.addWidget(self.str_table, 1)
        self.tabs.addTab(strw, "Strings")
        split.addWidget(self.tabs)
        split.setSizes([820, 520])
        split.setStretchFactor(0, 1)
        lay.addWidget(split, 1)

        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(self.status)

        QShortcut(QKeySequence("Ctrl+G"), self, activated=lambda: (self.goto_edit.setFocus(),
                                                                  self.goto_edit.selectAll()))
        QShortcut(QKeySequence.StandardKey.Find, self, activated=lambda: (self.find_edit.setFocus(),
                                                                         self.find_edit.selectAll()))
        QShortcut(QKeySequence(Qt.Key.Key_F3), self, activated=lambda: self.find(True))
        QShortcut(QKeySequence("Shift+F3"), self, activated=lambda: self.find(False))
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self.save)
        QShortcut(QKeySequence.StandardKey.Copy, self.view, activated=lambda: self.copy("hex"))
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.cancel)
        self.strings: list[tuple[int, str, str]] = []
        self.on_mods()
        self.on_cursor()
        self.view.setFocus()
        self.poll = QTimer(self)
        self.poll.timeout.connect(self.on_cursor)   # refresh inspector as pages arrive
        self.poll.start(400)

    def _table(self, headers):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().hide()
        t.verticalHeader().setDefaultSectionSize(24)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setShowGrid(False)
        t.setWordWrap(False)
        t.setAlternatingRowColors(True)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        t.horizontalHeader().setStretchLastSection(True)
        t.setFont(QFont("Consolas", 9))
        return t

    # ---- status / inspector -----------------------------------------------------
    def on_cursor(self):
        v = self.view
        off = v.cur
        start, n = v.selection()
        sel = f"   selection {n:,} bytes ({start:#x}–{start + n - 1:#x})" if n else ""
        err = f"   ⚠ {self.src.error}" if self.src.error else ""
        self.status.setText(f"{self.path}   ·   {human(self.src.size)} ({self.src.size:,} bytes)   ·   "
                            f"offset {off:#x} ({off:,}){sel}   ·   {len(v.mods)} modified byte(s)"
                            + ("   ·   EDIT MODE" if v.editable else "") + err)
        raw = bytes(b for b in (v.byte(off + i) for i in range(8)) if b is not None)
        rows = [("Offset", f"{off:#x}  /  {off:,}")]
        if raw:
            b0 = raw[0]
            rows += [("Binary", f"{b0:08b}"), ("UInt8 / Int8", f"{b0}  /  {struct.unpack('b', raw[:1])[0]}"),
                     ("Char", repr(chr(b0)) if 0x20 <= b0 < 0x7F else "·")]

            def num(fmt, n):
                if len(raw) < n:
                    return "–"
                return str(struct.unpack(fmt, raw[:n])[0])
            rows += [("UInt16 LE / BE", f"{num('<H', 2)}  /  {num('>H', 2)}"),
                     ("Int16 LE / BE", f"{num('<h', 2)}  /  {num('>h', 2)}"),
                     ("UInt32 LE / BE", f"{num('<I', 4)}  /  {num('>I', 4)}"),
                     ("Int32 LE / BE", f"{num('<i', 4)}  /  {num('>i', 4)}"),
                     ("UInt64 LE", num("<Q", 8)), ("Int64 LE", num("<q", 8)),
                     ("Float32 LE", f"{struct.unpack('<f', raw[:4])[0]:.7g}" if len(raw) >= 4 else "–"),
                     ("Float64 LE", f"{struct.unpack('<d', raw[:8])[0]:.15g}" if len(raw) >= 8 else "–"),
                     ("Hex (8)", raw.hex(" "))]
            if len(raw) >= 4:
                rows.append(("UTF-8", raw.decode("utf-8", errors="replace")))
        else:
            rows.append(("", "loading…"))
        if self.insp.rowCount() != len(rows):
            self.insp.setRowCount(len(rows))
        for i, (k, val) in enumerate(rows):
            for c, t in ((0, k), (1, val)):
                it = self.insp.item(i, c)
                if it is None:
                    it = QTableWidgetItem()
                    self.insp.setItem(i, c, it)
                if it.text() != t:
                    it.setText(t)
            self.insp.item(i, 0).setForeground(QColor(theme.TEXT_DIM))

    def on_mods(self):
        n = len(self.view.mods)
        self.btn_save.setEnabled(n > 0)
        self.btn_revert.setEnabled(n > 0)
        self.on_cursor()

    def set_editable(self, on: bool):
        self.view.editable = on
        self.view.viewport().update()
        self.on_cursor()
        self.view.setFocus()

    # ---- goto / find --------------------------------------------------------------
    def do_goto(self):
        t = self.goto_edit.text().strip().replace("_", "")
        if not t:
            return
        rel = t[0] in "+-"
        try:
            val = int(t[1:] if rel else t, 0)
        except ValueError:
            try:
                val = int(t[1:] if rel else t, 16)
            except ValueError:
                QMessageBox.warning(self, "Go to", f"'{t}' is not an offset (use 0x1A2B or 6699).")
                return
        off = (self.view.cur + (val if t[0] == "+" else -val)) if rel else val
        self.view.goto(off)
        self.view.setFocus()

    def pattern(self) -> tuple[bytes, bool] | None:
        t = self.find_edit.text()
        if not t:
            return None
        mode = self.find_mode.currentText()
        if mode == "Hex":
            h = re.sub(r"[^0-9a-fA-F]", "", t)
            if len(h) % 2 or not h:
                QMessageBox.warning(self, "Find", "Hex search needs an even number of hex digits, e.g. 7F 45 4C 46.")
                return None
            return bytes.fromhex(h), True
        data = t.encode("utf-16-le") if mode == "UTF-16" else t.encode("latin-1", errors="replace")
        return data, self.find_case.isChecked() or mode == "UTF-16"

    def find(self, forward=True):
        if self.busy:
            return
        pat = self.pattern()
        if not pat:
            return
        needle, case = pat
        src, view = self.src, self.view
        size = src.size
        start = view.cur + (1 if forward else -1)
        mods = dict(view.mods)
        self.cancel_flag = False
        self.busy = True
        self.analyze_status.setText("")
        self.status.setText(f"Searching {'forward' if forward else 'backward'} for {needle!r}… (Esc cancels)")
        win = 4 << 20
        n = len(needle)
        rx = re.compile(re.escape(needle), 0 if case else re.IGNORECASE)

        def apply_mods(off, data):
            if not mods:
                return data
            b = bytearray(data)
            for o, v in mods.items():
                if off <= o < off + len(b):
                    b[o - off] = v
            return bytes(b)

        def work():
            if forward:
                pos = max(0, start)
                while pos < size:
                    if self.cancel_flag:
                        raise Cancelled()
                    data = apply_mods(pos, src.read_sync(pos, win + n - 1))
                    m = rx.search(data)
                    if m:
                        return pos + m.start()
                    pos += win
            else:
                end = min(size, start + n)
                while end > 0:
                    if self.cancel_flag:
                        raise Cancelled()
                    pos = max(0, end - win - n + 1)
                    data = apply_mods(pos, src.read_sync(pos, end - pos))
                    last = None
                    for m in rx.finditer(data):
                        if pos + m.start() <= start:
                            last = m
                    if last:
                        return pos + last.start()
                    end = pos + n - 1 if pos > 0 else 0
            return None

        def done(off):
            self.busy = False
            if off is None:
                self.status.setText("Not found.")
                QApplication.beep()
            else:
                view.goto(off, n)
                self.on_cursor()

        def fail(m):
            self.busy = False
            self.status.setText("Search cancelled." if "Cancelled" in m or not m else f"Search failed: {m}")
        self.Task.start(work, done, fail)

    def cancel(self):
        if self.busy:
            self.cancel_flag = True
        else:
            self.close()

    # ---- copy / menu -------------------------------------------------------------
    def view_menu(self, pos):
        m = QMenu(self)
        m.addAction(theme.line_icon("copy"), "Copy as hex", lambda: self.copy("hex"))
        m.addAction(theme.line_icon("copy"), "Copy as text", lambda: self.copy("text"))
        m.addAction(theme.line_icon("copy"), "Copy as C array", lambda: self.copy("c"))
        m.addAction(theme.line_icon("copy"), "Copy offset", lambda: QApplication.clipboard().setText(
            f"{self.view.cur:#x}"))
        m.addSeparator()
        m.addAction(theme.line_icon("forward"), "Go to…", lambda: (self.goto_edit.setFocus(),
                                                                   self.goto_edit.selectAll()))
        if self.view.editable:
            m.addAction(theme.line_icon("refresh"), "Undo (Ctrl+Z)", self.view.undo_last)
        m.exec(pos)

    def copy(self, fmt: str):
        start, n = self.view.selection()
        n = n or 1
        if n > 16 << 20:
            QMessageBox.warning(self, "Copy", "Selection is larger than 16 MB.")
            return
        src, mods = self.src, dict(self.view.mods)

        def work():
            b = bytearray(src.read_sync(start, n))
            for o, v in mods.items():
                if start <= o < start + len(b):
                    b[o - start] = v
            return bytes(b)

        def done(b):
            if fmt == "hex":
                txt = b.hex(" ").upper()
            elif fmt == "c":
                txt = ", ".join(f"0x{x:02x}" for x in b)
            else:
                txt = b.decode("latin-1").translate({i: "." for i in range(32) if i not in (9, 10, 13)})
            QApplication.clipboard().setText(txt)
            self.status.setText(f"Copied {len(b):,} bytes as {fmt}.")
        self.Task.start(work, done, lambda m: QMessageBox.warning(self, "Copy", m))

    # ---- analyze --------------------------------------------------------------------
    def analyze(self):
        if self.busy:
            return
        size = self.src.size
        if size > 2 << 30 and QMessageBox.question(
                self, "Analyze", f"This is {human(size)}. Reading all of it over SSH can take a long time. "
                "Continue? (Esc cancels while running)") != QMessageBox.StandardButton.Yes:
            return
        self.busy = True
        self.cancel_flag = False
        self.btn_analyze.setEnabled(False)
        conn, path = self.conn, self.path
        scanner = binscan.Scanner(min_len=self.min_len.value())
        got = [0]
        lock = threading.Lock()

        def sink(b):
            if self.cancel_flag:
                raise Cancelled()
            with lock:
                scanner.feed(b)
                got[0] += len(b)

        def work():
            conn.exec_stream_out(f"cat {q(path)}", sink, timeout=24 * 3600)
            scanner.finish()
            return scanner

        timer = QTimer(self)

        def tick():
            pct = f"{got[0] * 100 // size}%" if size else ""
            self.analyze_status.setText(f"Reading… {human(got[0])} {pct}  (Esc cancels)")
        timer.timeout.connect(tick)
        timer.start(300)

        def done(sc):
            timer.stop()
            self.busy = False
            self.btn_analyze.setEnabled(True)
            self.show_results(sc)

        def fail(m):
            timer.stop()
            self.busy = False
            self.btn_analyze.setEnabled(True)
            self.analyze_status.setText("Cancelled." if self.cancel_flag else f"Failed: {m}")
        self.Task.start(work, done, fail)

    def show_results(self, sc: binscan.Scanner):
        self.entropy.set_values(sc.entropy, sc.block, self.src.size)
        t = self.sig_table
        t.setRowCount(len(sc.hits))
        for i, h in enumerate(sc.hits):
            for c, txt in enumerate((f"{h.offset:#010x}", h.kind, h.desc)):
                it = QTableWidgetItem(txt)
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, h.offset)
                it.setToolTip(h.desc)
                t.setItem(i, c, it)
        self.strings = sc.strings
        self.fill_strings()
        trunc = "  (result limit reached)" if sc.truncated else ""
        self.analyze_status.setText(f"{len(sc.hits)} signatures · {len(sc.strings):,} strings{trunc}")
        self.tabs.setCurrentIndex(1)

    def fill_strings(self, *_):
        f = self.str_filter.text().lower()
        rows = [s for s in self.strings if f in s[2].lower()] if f else self.strings
        shown = rows[:20000]
        t = self.str_table
        t.setUpdatesEnabled(False)
        t.setRowCount(len(shown))
        for i, (off, enc, s) in enumerate(shown):
            a = QTableWidgetItem(f"{off:#010x}")
            a.setData(Qt.ItemDataRole.UserRole, off)
            t.setItem(i, 0, a)
            t.setItem(i, 1, QTableWidgetItem(enc))
            t.setItem(i, 2, QTableWidgetItem(s))
        t.setUpdatesEnabled(True)
        if self.strings:
            self.tabs.setTabText(2, f"Strings ({len(rows):,}" + (", first 20,000 shown" if len(rows) > 20000 else "")
                                 + ")")

    def jump_table(self, table: QTableWidget, row: int):
        it = table.item(row, 0)
        if it is None:
            return
        off = it.data(Qt.ItemDataRole.UserRole)
        length = 0
        if table is self.str_table:
            s = table.item(row, 2).text()
            length = len(s) * (2 if table.item(row, 1).text() == "UTF-16" else 1)
        self.view.goto(int(off), length or 16)

    # ---- save -------------------------------------------------------------------------
    def runs(self) -> list[tuple[int, bytes]]:
        out = []
        for off in sorted(self.view.mods):
            v = self.view.mods[off]
            if out and out[-1][0] + len(out[-1][1]) == off and len(out[-1][1]) < 16384:   # keeps each command short (Windows limit for adb shell)
                out[-1] = (out[-1][0], out[-1][1] + bytes([v]))
            else:
                out.append((off, bytes([v])))
        return out

    def save(self):
        if self.readonly or not self.view.mods or self.busy:
            return
        runs = self.runs()
        total = sum(len(r[1]) for r in runs)
        box = QMessageBox(QMessageBox.Icon.Warning, "Save changes",
                          f"Write {total:,} changed byte(s) in {len(runs)} place(s) to\n{self.path}?\n\n"
                          "Only the changed bytes are patched in place on the device.", parent=self)
        backup = QCheckBox(f"Keep a backup copy on the device ({self.path.rsplit('/', 1)[-1]}.bak)")
        backup.setChecked(self.src.size < (256 << 20))
        box.setCheckBox(backup)
        box.setStandardButtons(QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Save:
            return
        conn, path, src = self.conn, self.path, self.src
        do_backup = backup.isChecked()
        self.busy = True

        def work():
            if do_backup:
                rc, out, err = conn.exec(f"cp -p {q(path)} {q(path + '.bak')} 2>&1", timeout=3600)
                if rc != 0:
                    raise B.BackendError("Backup copy failed: " + (out + err).decode(errors="replace"))
            for off, data in runs:
                b64 = base64.b64encode(data).decode()
                rc, out, err = conn.exec(
                    f"printf '%s' {q(b64)} | base64 -d | dd of={q(path)} bs=1 seek={off} conv=notrunc 2>&1",
                    timeout=600)
                if rc != 0:
                    raise B.BackendError(f"Writing at {off:#x} failed: " + (out + err).decode(errors="replace"))
            # verify
            pages = {o // PAGE for o, d in runs for o in range(o, o + len(d))}
            src.invalidate(pages)
            for off, data in runs:
                if src.read_sync(off, len(data)) != data:
                    raise B.BackendError(f"Verification failed at {off:#x}")
            return total

        def done(n):
            self.busy = False
            self.view.mods.clear()
            self.view.undo.clear()
            self.view.viewport().update()
            self.on_mods()
            self.status.setText(f"Saved and verified {n:,} byte(s)." + (" Backup: .bak" if do_backup else ""))

        def fail(m):
            self.busy = False
            QMessageBox.warning(self, "Save failed", m)
        self.Task.start(work, done, fail)

    def revert(self):
        if self.view.mods and QMessageBox.question(self, "Revert", f"Discard {len(self.view.mods)} unsaved "
                                                   "change(s)?") == QMessageBox.StandardButton.Yes:
            self.view.mods.clear()
            self.view.undo.clear()
            self.view.viewport().update()
            self.on_mods()

    def closeEvent(self, ev):
        if self.view.mods and QMessageBox.question(
                self, "Unsaved changes", f"Discard {len(self.view.mods)} unsaved change(s)?") \
                != QMessageBox.StandardButton.Yes:
            ev.ignore()
            return
        self.cancel_flag = True
        self.poll.stop()
        self.src.close()
        super().closeEvent(ev)
