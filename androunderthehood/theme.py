"""Dark, rounded theme for AndroUnderTheHood (Fusion style + palette + stylesheet)."""
from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QWidget

# ---- colours ---------------------------------------------------------------
BG = "#17181b"          # window background (behind the cards)
CARD = "#1f2125"        # panels / views
CARD_ALT = "#24272b"    # alternate rows
RAISED = "#2a2d32"      # inputs, buttons
HOVER = "#33373d"
BORDER = "#30333a"
BORDER_STRONG = "#3d4148"
TEXT = "#e7e9ec"
TEXT_DIM = "#9aa0a8"
TEXT_FAINT = "#6c727b"
ACCENT = "#3ddc84"      # Android green
ACCENT_HOVER = "#5ee69b"
ACCENT_TEXT = "#0d1f14"
SEL = "rgba(61, 220, 132, 0.22)"
SEL_HOVER = "rgba(61, 220, 132, 0.12)"
DANGER = "#ff6b6b"

RADIUS = 10
RADIUS_S = 7

QSS = f"""
* {{
    outline: 0;
}}
QWidget {{
    color: {TEXT};
    font-size: 9.5pt;
}}
QMainWindow, QDialog {{
    background: {BG};
}}
QToolTip {{
    background: {RAISED};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_S}px;
    padding: 5px 8px;
}}

/* ---- toolbars ---------------------------------------------------------- */
QToolBar {{
    background: {BG};
    border: none;
    spacing: 4px;
    padding: 6px 8px 2px 8px;
}}
QToolBar::separator {{
    background: {BORDER};
    width: 1px;
    margin: 6px 6px;
}}
QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS_S}px;
    padding: 5px 8px;
    color: {TEXT};
}}
QToolButton:hover {{
    background: {HOVER};
}}
QToolButton:pressed, QToolButton:checked {{
    background: {SEL};
    border-color: rgba(61, 220, 132, 0.45);
}}
QToolButton:disabled {{
    color: {TEXT_FAINT};
}}
QToolButton[popupMode="1"] {{
    padding-right: 20px;
}}
QToolButton::menu-button {{
    border: none;
    border-left: 1px solid {BORDER};
    border-top-right-radius: {RADIUS_S}px;
    border-bottom-right-radius: {RADIUS_S}px;
    width: 16px;
}}
QToolButton::menu-button:hover {{
    background: {BORDER_STRONG};
}}

/* ---- inputs ------------------------------------------------------------ */
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {{
    background: {RAISED};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_S}px;
    padding: 5px 9px;
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_TEXT};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{
    border-color: {BORDER_STRONG};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{
    border-color: {ACCENT};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    color: {TEXT_FAINT};
    background: {CARD};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox::down-arrow {{
    image: url(@DOWN@);
    width: 12px;
    height: 12px;
}}
QComboBox QAbstractItemView {{
    background: {RAISED};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS_S}px;
    padding: 4px;
    selection-background-color: {SEL};
}}
QSpinBox::up-button, QSpinBox::down-button {{
    border: none;
    width: 18px;
}}
QSpinBox::up-arrow {{
    image: url(@UP@);
    width: 10px;
    height: 10px;
}}
QSpinBox::down-arrow {{
    image: url(@DOWN@);
    width: 10px;
    height: 10px;
}}
QPushButton#browse {{
    padding: 5px 0;
    min-width: 32px;
}}

/* ---- buttons ----------------------------------------------------------- */
QPushButton {{
    background: {RAISED};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_S}px;
    padding: 6px 16px;
    min-height: 18px;
}}
QPushButton:hover {{
    background: {HOVER};
    border-color: {BORDER_STRONG};
}}
QPushButton:pressed {{
    background: {BORDER_STRONG};
}}
QPushButton:default {{
    background: {ACCENT};
    color: {ACCENT_TEXT};
    border-color: {ACCENT};
    font-weight: 600;
}}
QPushButton:default:hover {{
    background: {ACCENT_HOVER};
}}
QPushButton:disabled {{
    color: {TEXT_FAINT};
    background: {CARD};
    border-color: {BORDER};
}}
QCheckBox {{
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 5px;
    border: 1px solid {BORDER_STRONG};
    background: {RAISED};
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
    image: url(@CHECK@);
}}

/* ---- views (cards) ----------------------------------------------------- */
QListView, QListWidget, QTableView, QTableWidget, QTreeView {{
    background: {CARD};
    alternate-background-color: {CARD_ALT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 4px;
    selection-background-color: {SEL};
    selection-color: {TEXT};
}}
QListView::item, QListWidget::item, QTreeView::item {{
    border-radius: {RADIUS_S}px;
    padding: 3px;
}}
QListView::item:hover, QListWidget::item:hover, QTreeView::item:hover {{
    background: {SEL_HOVER};
}}
QListView::item:selected, QListWidget::item:selected, QTreeView::item:selected {{
    background: {SEL};
    color: {TEXT};
}}
QListWidget#places {{
    padding: 6px;
}}
QListWidget#places::item {{
    padding: 7px 8px;
    margin: 1px 0;
}}
QTableView::item {{
    padding: 0 6px;
    border: none;
}}
QTableView::item:selected {{
    background: {SEL};
}}
QHeaderView {{
    background: transparent;
    border: none;
}}
QHeaderView::section {{
    background: {CARD};
    color: {TEXT_DIM};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
    font-weight: 600;
}}
QHeaderView::section:hover {{
    color: {TEXT};
}}
QTableCornerButton::section {{
    background: {CARD};
    border: none;
}}

/* ---- properties panel -------------------------------------------------- */
QWidget#propsPanel {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QLabel#preview {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 6px;
}}
QLabel#propsTitle {{
    font-size: 12pt;
    font-weight: 600;
    padding: 4px 2px 2px 2px;
}}
QTableWidget#propsTable {{
    background: transparent;
    border: none;
    padding: 0;
}}
QTableWidget#propsTable::item {{
    padding: 3px 4px;
}}

/* ---- scrollbars -------------------------------------------------------- */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 4px 2px 4px 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0 4px 2px 4px;
}}
QScrollBar::handle {{
    background: {BORDER_STRONG};
    border-radius: 4px;
    min-height: 30px;
    min-width: 30px;
}}
QScrollBar::handle:hover {{
    background: #50555d;
}}
QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
    border: none;
    width: 0;
    height: 0;
}}

/* ---- menus ------------------------------------------------------------- */
QMenu {{
    background: {RAISED};
    border: 1px solid {BORDER_STRONG};
    border-radius: {RADIUS}px;
    padding: 6px;
}}
QMenu::item {{
    padding: 6px 22px 6px 12px;
    border-radius: {RADIUS_S}px;
}}
QMenu::item:selected {{
    background: {SEL};
}}
QMenu::item:disabled {{
    color: {TEXT_FAINT};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 5px 8px;
}}
QMenu::indicator {{
    width: 14px;
    height: 14px;
    border-radius: 4px;
    margin-left: 4px;
}}
QMenu::indicator:checked {{
    background: {ACCENT};
}}

/* ---- misc -------------------------------------------------------------- */
QStatusBar {{
    background: {BG};
    color: {TEXT_DIM};
    border: none;
}}
QStatusBar::item {{
    border: none;
}}
QStatusBar QLabel {{
    color: {TEXT_DIM};
    padding: 0 6px;
}}
QSplitter::handle {{
    background: transparent;
}}
QProgressBar {{
    background: {RAISED};
    border: none;
    border-radius: 6px;
    height: 12px;
    text-align: center;
    color: {TEXT};
}}
QProgressBar::chunk {{
    background: {ACCENT};
    border-radius: 6px;
}}
QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER_STRONG};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {TEXT};
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{
    background: {ACCENT_HOVER};
}}
QMessageBox QLabel {{
    min-width: 280px;
}}
"""


def palette() -> QPalette:
    p = QPalette()
    c = QColor
    roles = {
        QPalette.ColorRole.Window: c(BG),
        QPalette.ColorRole.WindowText: c(TEXT),
        QPalette.ColorRole.Base: c(CARD),
        QPalette.ColorRole.AlternateBase: c(CARD_ALT),
        QPalette.ColorRole.ToolTipBase: c(RAISED),
        QPalette.ColorRole.ToolTipText: c(TEXT),
        QPalette.ColorRole.PlaceholderText: c(TEXT_FAINT),
        QPalette.ColorRole.Text: c(TEXT),
        QPalette.ColorRole.Button: c(RAISED),
        QPalette.ColorRole.ButtonText: c(TEXT),
        QPalette.ColorRole.BrightText: c(DANGER),
        QPalette.ColorRole.Highlight: c(ACCENT),
        QPalette.ColorRole.HighlightedText: c(ACCENT_TEXT),
        QPalette.ColorRole.Link: c(ACCENT),
        QPalette.ColorRole.Light: c(HOVER),
        QPalette.ColorRole.Midlight: c(BORDER_STRONG),
        QPalette.ColorRole.Mid: c(BORDER),
        QPalette.ColorRole.Dark: c("#101113"),
        QPalette.ColorRole.Shadow: c("#000000"),
    }
    for role, color in roles.items():
        p.setColor(role, color)
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, c(TEXT_FAINT))
    return p


def tinted_icon(sp: QStyle.StandardPixmap, color: str = TEXT, size: int = 32) -> QIcon:
    """A standard (often dark, low-contrast) icon recoloured for the dark theme."""
    src = QApplication.style().standardIcon(sp).pixmap(size, size)
    out = QPixmap(src.size())
    out.setDevicePixelRatio(src.devicePixelRatio())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.drawPixmap(0, 0, src)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(out.rect(), QColor(color))
    p.end()
    ic = QIcon(out)
    # dimmed variant for disabled actions
    dis = QPixmap(out)
    p = QPainter(dis)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(dis.rect(), QColor(TEXT_FAINT))
    p.end()
    ic.addPixmap(dis, QIcon.Mode.Disabled)
    return ic


def _draw_glyph(p: QPainter, name: str):
    """Draw a line icon on a 24x24 grid (pen already set)."""
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QPainterPath

    P = QPointF

    def poly(*pts):
        p.drawPolyline([P(x, y) for x, y in pts])

    def rrect(x, y, w, h, r=2.5):
        p.drawRoundedRect(QRectF(x, y, w, h), r, r)

    if name == "back":
        poly((15, 5), (8, 12), (15, 19))
    elif name == "forward":
        poly((9, 5), (16, 12), (9, 19))
    elif name == "up":
        poly((5, 15), (12, 8), (19, 15))
    elif name == "down":
        poly((5, 9), (12, 16), (19, 9))
    elif name == "refresh":
        path = QPainterPath()
        path.arcMoveTo(QRectF(5, 5, 14, 14), 60)
        path.arcTo(QRectF(5, 5, 14, 14), 60, 290)
        p.drawPath(path)
        poly((15.5, 3.5), (15.5, 7.5), (19.5, 7.5))
    elif name == "connect":  # plug
        poly((9, 3), (9, 8))
        poly((15, 3), (15, 8))
        rrect(6, 8, 12, 6, 2)
        poly((12, 14), (12, 17))
        path = QPainterPath(P(12, 17))
        path.cubicTo(P(12, 21), P(17, 21), P(18, 18))
        p.drawPath(path)
    elif name == "disconnect":
        poly((6, 6), (18, 18))
        poly((18, 6), (6, 18))
    elif name == "grid":
        for x, y in ((4, 4), (13, 4), (4, 13), (13, 13)):
            rrect(x, y, 7, 7, 2)
    elif name == "list":
        for y in (6, 12, 18):
            poly((9, y), (20, y))
            p.drawPoint(P(4.5, y))
    elif name == "download":
        poly((12, 4), (12, 15))
        poly((7, 10), (12, 15), (17, 10))
        poly((5, 19.5), (19, 19.5))
    elif name == "upload":
        poly((12, 15), (12, 4))
        poly((7, 9), (12, 4), (17, 9))
        poly((5, 19.5), (19, 19.5))
    elif name == "folder-plus":
        path = QPainterPath(P(3, 7))
        path.lineTo(3, 18)
        path.quadTo(3, 20, 5, 20)
        path.lineTo(19, 20)
        path.quadTo(21, 20, 21, 18)
        path.lineTo(21, 9.5)
        path.quadTo(21, 7.5, 19, 7.5)
        path.lineTo(12, 7.5)
        path.lineTo(10, 5)
        path.lineTo(5, 5)
        path.quadTo(3, 5, 3, 7)
        p.drawPath(path)
        poly((12, 11), (12, 17))
        poly((9, 14), (15, 14))
    elif name == "rename":  # pencil
        path = QPainterPath(P(5, 19))
        path.lineTo(6, 15)
        path.lineTo(15.5, 5.5)
        path.lineTo(18.5, 8.5)
        path.lineTo(9, 18)
        path.closeSubpath()
        p.drawPath(path)
        poly((13.5, 7.5), (16.5, 10.5))
    elif name == "trash":
        poly((4, 7), (20, 7))
        poly((9.5, 7), (10, 4.5), (14, 4.5), (14.5, 7))
        path = QPainterPath(P(6, 7))
        path.lineTo(7, 19)
        path.quadTo(7.2, 20.5, 9, 20.5)
        path.lineTo(15, 20.5)
        path.quadTo(16.8, 20.5, 17, 19)
        path.lineTo(18, 7)
        p.drawPath(path)
        poly((10, 11), (10, 17))
        poly((14, 11), (14, 17))
    elif name == "copy":
        rrect(8, 8, 12, 12, 2.5)
        poly((5, 15.5), (4, 15.5), (4, 6), (6, 4), (15.5, 4), (15.5, 5))
    elif name == "bookmark":
        path = QPainterPath(P(7, 4))
        path.lineTo(17, 4)
        path.lineTo(17, 20)
        path.lineTo(12, 16)
        path.lineTo(7, 20)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "terminal":
        rrect(3, 4.5, 18, 15, 3)
        poly((7, 9.5), (10, 12), (7, 14.5))
        poly((12, 15), (17, 15))
    elif name == "broom":  # clear cache
        poly((14, 4), (10.5, 12))
        path = QPainterPath(P(7, 11))
        path.lineTo(14.5, 13.5)
        path.lineTo(13, 20.5)
        path.lineTo(4, 17.5)
        path.closeSubpath()
        p.drawPath(path)
        poly((8, 15), (7, 18.5))
        poly((11, 16), (10.5, 19.5))
    elif name == "phone":  # screen mirror / internal storage
        rrect(7, 2.5, 10, 19, 2.5)
        poly((10.5, 18.5), (13.5, 18.5))
    elif name == "sdcard":
        path = QPainterPath(P(9, 3))
        path.lineTo(17, 3)
        path.quadTo(19, 3, 19, 5)
        path.lineTo(19, 19)
        path.quadTo(19, 21, 17, 21)
        path.lineTo(7, 21)
        path.quadTo(5, 21, 5, 19)
        path.lineTo(5, 7)
        path.closeSubpath()
        p.drawPath(path)
        for x in (10, 13, 16):
            poly((x, 6.5), (x, 9.5))
    elif name == "usb":
        rrect(8, 2.5, 8, 6, 1.5)
        rrect(5.5, 8.5, 13, 13, 3)
        poly((10.5, 5), (10.5, 6))
        poly((13.5, 5), (13.5, 6))
    elif name == "root":  # disk with slash
        rrect(3, 6, 18, 12, 3)
        poly((13, 9), (10.5, 15))
        p.drawPoint(P(17, 12))
    elif name == "sliders":  # property editor
        for y, x in ((6, 9), (12, 15), (18, 7)):
            poly((4, y), (20, y))
            p.save()
            p.setBrush(QColor(BG))
            p.drawEllipse(P(x, y), 2.4, 2.4)
            p.restore()
    elif name == "lock":
        rrect(5.5, 11, 13, 9.5, 2.5)
        path = QPainterPath(P(8.5, 11))
        path.lineTo(8.5, 8)
        path.arcTo(QRectF(8.5, 4.5, 7, 7), 180, -180)
        path.lineTo(15.5, 11)
        p.drawPath(path)
    elif name == "package":  # app manager (box)
        path = QPainterPath(P(12, 3))
        path.lineTo(20, 7.5)
        path.lineTo(20, 16.5)
        path.lineTo(12, 21)
        path.lineTo(4, 16.5)
        path.lineTo(4, 7.5)
        path.closeSubpath()
        p.drawPath(path)
        poly((4, 7.5), (12, 12), (20, 7.5))
        poly((12, 12), (12, 21))
        poly((8, 5.25), (16, 9.75))
    elif name == "more":  # horizontal ellipsis
        for x in (6, 12, 18):
            p.drawEllipse(P(x, 12), 0.9, 0.9)
    elif name == "antenna":  # modem (DIAG): mast with radio waves
        p.drawEllipse(P(12, 9), 1.6, 1.6)
        poly((12, 11), (8.5, 21))
        poly((12, 11), (15.5, 21))
        poly((9.6, 17.5), (14.4, 17.5))
        for r in (4.5, 8):
            p.drawArc(QRectF(12 - r, 9 - r, 2 * r, 2 * r), 135 * 16, 70 * 16)
            p.drawArc(QRectF(12 - r, 9 - r, 2 * r, 2 * r), -25 * 16, 70 * 16)
    elif name == "flash":  # flash tools: lightning bolt
        path = QPainterPath(P(13.5, 2.5))
        path.lineTo(5.5, 13.5)
        path.lineTo(11.5, 13.5)
        path.lineTo(10.5, 21.5)
        path.lineTo(18.5, 10.5)
        path.lineTo(12.5, 10.5)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "shield":  # root manager: shield with a check mark
        path = QPainterPath(P(12, 3))
        path.lineTo(19.5, 6)
        path.cubicTo(19.5, 13, 17, 18, 12, 21)
        path.cubicTo(7, 18, 4.5, 13, 4.5, 6)
        path.closeSubpath()
        p.drawPath(path)
        poly((8.5, 12), (11, 14.5), (15.5, 9.5))
    elif name == "power":  # reboot: power symbol
        p.drawArc(QRectF(5, 5, 14, 14), 120 * 16, 300 * 16)
        poly((12, 3.5), (12, 11.5))
    elif name == "tree":  # sysfs explorer: node hierarchy
        rrect(3.5, 3.5, 7, 5, 1.3)
        poly((7, 8.5), (7, 17.5))
        poly((7, 11.5), (12.5, 11.5))
        poly((7, 17.5), (12.5, 17.5))
        rrect(12.5, 9, 8, 5, 1.3)
        rrect(12.5, 15, 8, 5, 1.3)
    elif name == "chip":  # device info: SoC with pins
        rrect(7, 7, 10, 10, 2)
        rrect(10, 10, 4, 4, 0.8)
        for k in (9.5, 12, 14.5):
            poly((k, 3.5), (k, 7))
            poly((k, 17), (k, 20.5))
            poly((3.5, k), (7, k))
            poly((17, k), (20.5, k))
    elif name == "log":  # logcat: document with lines
        rrect(5, 3, 14, 18, 2.5)
        for y, x2 in ((8, 16), (12, 14.5), (16, 12)):
            poly((8, y), (x2, y))
    elif name == "plus":
        poly((12, 5), (12, 19))
        poly((5, 12), (19, 12))
    elif name == "save":
        poly((12, 4), (12, 14))
        poly((8, 10), (12, 14), (16, 10))
        rrect(4, 15, 16, 5, 2)
    elif name == "star":
        import math
        pts = []
        for k in range(10):
            r = 8.5 if k % 2 == 0 else 3.8
            a = math.radians(-90 + k * 36)
            pts.append(P(12 + r * math.cos(a), 12.6 + r * math.sin(a)))
        pts.append(pts[0])
        p.drawPolyline(pts)
    elif name == "hash":  # Root toggle: the root shell prompt
        poly((10, 4), (8, 20))
        poly((16, 4), (14, 20))
        poly((5, 9.5), (19.5, 9.5))
        poly((4.5, 14.5), (19, 14.5))
    elif name == "pie":  # partitions: a disk split into slices
        p.drawEllipse(P(12, 12), 8.5, 8.5)
        poly((12, 3.5), (12, 12), (20.5, 12))
        poly((12, 12), (6, 18))
    elif name == "puzzle":  # plugins
        path = QPainterPath(P(4, 8))
        path.lineTo(9, 8)
        path.cubicTo(P(7.5, 3.5), P(13.5, 3.5), P(12, 8))
        path.lineTo(17, 8)
        path.lineTo(17, 12)
        path.cubicTo(P(21.5, 10.5), P(21.5, 17.5), P(17, 16))
        path.lineTo(17, 20)
        path.lineTo(4, 20)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "gear":  # settings / manage
        import math
        p.drawEllipse(P(12, 12), 5.6, 5.6)
        p.drawEllipse(P(12, 12), 2.2, 2.2)
        for k in range(8):
            a = math.radians(k * 45)
            poly((12 + 5.6 * math.cos(a), 12 + 5.6 * math.sin(a)), (12 + 8.4 * math.cos(a), 12 + 8.4 * math.sin(a)))
    elif name == "accel":  # accelerometer: three axes
        poly((12, 13), (12, 3.5))
        poly((9.5, 6), (12, 3.5), (14.5, 6))
        poly((12, 13), (20.5, 17.5))
        poly((17.2, 18.2), (20.5, 17.5), (18.8, 14.6))
        poly((12, 13), (3.5, 17.5))
        poly((6.8, 18.2), (3.5, 17.5), (5.2, 14.6))
    elif name == "history":  # activity log: clock with a back-arrow
        path = QPainterPath()
        path.arcMoveTo(QRectF(4, 4, 16, 16), 160)
        path.arcTo(QRectF(4, 4, 16, 16), 160, -310)
        p.drawPath(path)
        poly((3.2, 5.6), (4.5, 9.3), (8.2, 8.4))
        poly((12, 7.5), (12, 12), (15, 14))
    elif name == "battery":
        rrect(2.5, 7, 16.5, 10, 2.5)
        rrect(19, 10, 2.5, 4, 0.8)
        for x in (6, 9.5, 13):
            poly((x, 10.5), (x, 13.5))
    elif name == "camera":
        rrect(3, 7, 18, 13, 3)
        poly((8, 7), (9.5, 4.5), (14.5, 4.5), (16, 7))
        p.drawEllipse(P(12, 13.5), 3.6, 3.6)
    elif name == "pin":  # GPS: map pin
        path = QPainterPath(P(12, 21.5))
        path.cubicTo(P(9, 18), P(5, 14), P(5, 9.5))
        path.arcTo(QRectF(5, 2.5, 14, 14), 180, -180)
        path.cubicTo(P(19, 14), P(15, 18), P(12, 21.5))
        p.drawPath(path)
        p.drawEllipse(P(12, 9.5), 2.6, 2.6)
    elif name == "badge":  # integrity: seal with a check and ribbons
        p.drawEllipse(P(12, 9.5), 6.5, 6.5)
        poly((9, 9.6), (11.2, 11.8), (15, 7.6))
        poly((8.6, 15), (7.5, 21), (10.2, 19.6), (11.5, 21.5))
        poly((15.4, 15), (16.5, 21), (13.8, 19.6), (12.5, 21.5))
    elif name == "mic":
        rrect(9, 3, 6, 11, 3)
        p.drawArc(QRectF(5.5, 5, 13, 12.5), 180 * 16, 180 * 16)
        poly((12, 17.5), (12, 21))
        poly((8.5, 21), (15.5, 21))
    elif name == "wifi":
        for r in (4, 8, 12):
            p.drawArc(QRectF(12 - r, 19 - r, 2 * r, 2 * r), 45 * 16, 90 * 16)
        p.drawEllipse(P(12, 19), 0.9, 0.9)
    elif name == "speaker":
        path = QPainterPath(P(3.5, 9.5))
        path.lineTo(7.5, 9.5)
        path.lineTo(12.5, 5)
        path.lineTo(12.5, 19)
        path.lineTo(7.5, 14.5)
        path.lineTo(3.5, 14.5)
        path.closeSubpath()
        p.drawPath(path)
        for r in (3.5, 7):
            p.drawArc(QRectF(12.5 - r, 12 - r, 2 * r, 2 * r), -45 * 16, 90 * 16)
    elif name == "restore":  # reset & restore: a phone inside a turn-back arrow
        rrect(9, 7.5, 6, 9, 1.3)
        path = QPainterPath()
        path.arcMoveTo(QRectF(3, 3, 18, 18), 100)
        path.arcTo(QRectF(3, 3, 18, 18), 100, 300)
        p.drawPath(path)
        poly((7.6, 1.8), (10.4, 3.1), (8.6, 5.9))
    elif name == "pulse":  # task manager: activity graph
        rrect(3, 4, 18, 16, 2.5)
        poly((5.5, 13), (9, 13), (11, 8), (13.5, 17), (15.5, 12), (18.5, 12))
    else:
        rrect(4, 4, 16, 16)


def line_icon(name: str, color: str = TEXT, size: int = 48) -> QIcon:
    """Crisp monochrome toolbar icon drawn at runtime (normal, active, disabled)."""
    from PySide6.QtGui import QPen

    ic = QIcon()
    for mode, col in ((QIcon.Mode.Normal, color), (QIcon.Mode.Active, color),
                      (QIcon.Mode.Selected, ACCENT), (QIcon.Mode.Disabled, TEXT_FAINT)):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(size / 24, size / 24)
        pen = QPen(QColor(col), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        _draw_glyph(p, name)
        p.end()
        ic.addPixmap(pm, mode)
    return ic


def monogram_icon(text: str, color: str = TEXT, size: int = 48) -> QIcon:
    """Fallback toolbar icon: up to two letters in a rounded square (used so no two buttons look the same)."""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QFont, QPen

    ic = QIcon()
    for mode, col in ((QIcon.Mode.Normal, color), (QIcon.Mode.Active, color),
                      (QIcon.Mode.Selected, ACCENT), (QIcon.Mode.Disabled, TEXT_FAINT)):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        k = size / 24
        pen = QPen(QColor(col), 1.8 * k)
        p.setPen(pen)
        p.drawRoundedRect(QRectF(3 * k, 3 * k, 18 * k, 18 * k), 4 * k, 4 * k)
        f = QFont()
        f.setBold(True)
        f.setPixelSize(int((10 if len(text) <= 1 else 8.5) * k))
        p.setFont(f)
        p.drawText(QRectF(3 * k, 3 * k, 18 * k, 18 * k), int(Qt.AlignmentFlag.AlignCenter), text)
        p.end()
        ic.addPixmap(pm, mode)
    return ic


def _dark_title_bar(w: QWidget):
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = int(w.winId())
        val = ctypes.c_int(1)
        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20 (19 on older Windows 10 builds)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val),
                                                         ctypes.sizeof(val)) == 0:
                break
        # Windows 11: tint the caption to match the window background
        caption = ctypes.c_int(0x001B1817)  # COLORREF 0x00BBGGRR for BG #17181b
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption),
                                                   ctypes.sizeof(caption))
    except Exception:  # noqa: BLE001
        pass


class _ThemeFilter(QObject):
    """Dark title bars for every window; truly rounded (translucent) menus."""

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Type.Polish and isinstance(obj, QMenu):
            obj.setWindowFlags(obj.windowFlags() | Qt.WindowType.FramelessWindowHint
                               | Qt.WindowType.NoDropShadowWindowHint)
            obj.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        elif t == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow() \
                and not isinstance(obj, QMenu) and not obj.property("_dark_tb"):
            obj.setProperty("_dark_tb", True)
            _dark_title_bar(obj)
        return False


_filter: _ThemeFilter | None = None


def _check_image() -> str:
    """Render a checkmark PNG for checkbox indicators; returns a QSS-safe path."""
    import os

    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPen

    from .backend import app_dir

    path = os.path.join(app_dir(), "theme-check.png")
    pm = QPixmap(32, 32)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(ACCENT_TEXT), 4.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.drawPolyline([QPointF(8, 16.5), QPointF(13.5, 22), QPointF(24, 10)])
    p.end()
    pm.save(path, "PNG")
    return path.replace("\\", "/")


def _glyph_png(name: str, color: str) -> str:
    """Render a line glyph to a PNG for use in the stylesheet."""
    import os

    from .backend import app_dir

    path = os.path.join(app_dir(), f"theme-{name}.png")
    line_icon(name, color, 32).pixmap(32, 32).save(path, "PNG")
    return path.replace("\\", "/")


def apply(app: QApplication):
    global _filter
    app.setStyle("Fusion")
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    except Exception:  # noqa: BLE001
        pass
    app.setPalette(palette())
    try:
        check = _check_image()
    except Exception:  # noqa: BLE001
        check = ""
    try:
        down, up = _glyph_png("down", TEXT_DIM), _glyph_png("up", TEXT_DIM)
    except Exception:  # noqa: BLE001
        down = up = ""
    app.setStyleSheet(QSS.replace("@CHECK@", check).replace("@DOWN@", down).replace("@UP@", up))
    _filter = _ThemeFilter()
    app.installEventFilter(_filter)
