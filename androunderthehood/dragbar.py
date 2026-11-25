"""Toolbars whose buttons can be dragged into any order (also between the two toolbars).

Every action that should keep its place needs an objectName (its key). The order is saved in the app config
under "toolbar_order" as {toolbar key: [action keys]} and put back on start and whenever plugins add buttons.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QMimeData, QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QDrag
from PySide6.QtWidgets import QApplication, QFrame, QMenu, QToolBar, QToolButton, QWidget, QWidgetAction

from . import theme

MIME = "application/x-androunderthehood-button"
TEXT_BESIDE = "aoh_text_beside"     # action property: always show this button's text next to its icon


class DragToolBar(QToolBar):
    _dragging: QAction | None = None    # the action being dragged (same process only)
    _drop: tuple | None = None          # (target toolbar, insert-before action or None)

    def __init__(self, title: str, key: str, owner):
        super().__init__(title)
        self.key = key
        self.owner = owner                 # main window: has .toolbars, .save_toolbar_order(), .reset_toolbar_order()
        self.setObjectName(key)
        self.setMovable(False)
        self.setAcceptDrops(True)
        self._press: tuple[QWidget, QPoint] | None = None
        self._marker = QFrame(self)
        self._marker.setStyleSheet(f"background: {theme.ACCENT}; border-radius: 1px;")
        self._marker.hide()
        self.installEventFilter(self)       # presses on disabled buttons arrive here

    # ---- helpers ----------------------------------------------------------------
    def add_widget(self, w: QWidget, key: str) -> QWidgetAction:
        """Like addWidget, but the action owns the widget so the item can be moved safely."""
        a = QWidgetAction(self)
        a.setDefaultWidget(w)
        a.setObjectName(key)
        self.addAction(a)
        return a

    def add_separator(self, key: str) -> QAction:
        a = self.addSeparator()
        a.setObjectName(key)
        return a

    def _action_at(self, w: QWidget | None) -> QAction | None:
        while w is not None and w is not self:
            for a in self.actions():
                if self.widgetForAction(a) is w:
                    return a
            w = w.parentWidget()
        return None

    def actionEvent(self, ev):
        super().actionEvent(ev)
        if ev.type() == QEvent.Type.ActionAdded:
            w = self.widgetForAction(ev.action())
            if isinstance(w, QToolButton):
                w.installEventFilter(self)
                if ev.action().property(TEXT_BESIDE):
                    w.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

    # ---- starting a drag ----------------------------------------------------------
    def eventFilter(self, obj, ev):
        t = ev.type()
        if t not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease):
            return False
        if obj is self:      # a press that reached the toolbar itself, e.g. on a disabled button
            w = self.childAt(ev.position().toPoint())
            btn = w if isinstance(w, QToolButton) else None
            if btn is None or btn.isEnabled():
                return False
            pos = btn.mapFrom(self, ev.position().toPoint())
        else:
            btn = obj
            pos = ev.position().toPoint()
        if t == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            self._press = (btn, pos)
            # a button that opens its menu on press would never see the drag: open it on release instead
            return btn.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup
        if t == QEvent.Type.MouseMove and self._press and self._press[0] is btn \
                and ev.buttons() & Qt.MouseButton.LeftButton:
            if (pos - self._press[1]).manhattanLength() >= QApplication.startDragDistance():
                start = self._press[1]
                self._press = None
                self._start_drag(btn, start)
                return True
            return btn.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup
        if t == QEvent.Type.MouseButtonRelease and self._press and self._press[0] is btn:
            self._press = None
            if btn.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup:
                if btn.rect().contains(pos):
                    QTimer.singleShot(0, btn.showMenu)
                return True
        return False

    def _start_drag(self, btn: QToolButton, hot: QPoint):
        a = self._action_at(btn)
        if a is None or not a.objectName():
            return
        DragToolBar._dragging = a
        DragToolBar._drop = None
        drag = QDrag(btn)
        md = QMimeData()
        md.setData(MIME, a.objectName().encode())
        drag.setMimeData(md)
        drag.setPixmap(btn.grab())
        drag.setHotSpot(hot)
        btn.setDown(False)
        drag.exec(Qt.DropAction.MoveAction)
        drop, DragToolBar._drop, DragToolBar._dragging = DragToolBar._drop, None, None
        for tb in self.owner.toolbars:
            tb._marker.hide()
        if drop:
            # move after the drag has finished: moving deletes the dragged button
            QTimer.singleShot(0, lambda: self._move(a, *drop))

    def _move(self, a: QAction, target: "DragToolBar", before: QAction | None):
        if before is a:
            return
        for tb in self.owner.toolbars:
            if a in tb.actions():
                tb.removeAction(a)
        target.insertAction(before, a)
        self.owner.save_toolbar_order()
        self.owner.toolbar_changed()

    # ---- dropping -------------------------------------------------------------------
    def _drop_slot(self, x: int) -> tuple[QAction | None, int]:
        """The action to insert before for a drop at x (None = at the end) and where to draw the marker."""
        items = []
        for a in self.actions():
            w = self.widgetForAction(a)
            if w is not None and w.isVisible() and w.width() > 0:
                items.append((a, w.geometry()))
        for a, g in items:
            if x < g.center().x():
                return a, g.left() - 2
        return None, (items[-1][1].right() + 2 if items else 4)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasFormat(MIME) and DragToolBar._dragging is not None:
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dragMoveEvent(self, ev):
        if DragToolBar._dragging is None:
            ev.ignore()
            return
        _before, mx = self._drop_slot(ev.position().toPoint().x())
        self._marker.setGeometry(max(0, mx - 1), 3, 3, max(8, self.height() - 6))
        self._marker.show()
        self._marker.raise_()
        ev.acceptProposedAction()

    def dragLeaveEvent(self, ev):
        self._marker.hide()

    def dropEvent(self, ev):
        self._marker.hide()
        if DragToolBar._dragging is None:
            ev.ignore()
            return
        before, _mx = self._drop_slot(ev.position().toPoint().x())
        DragToolBar._drop = (self, before)
        ev.acceptProposedAction()

    def contextMenuEvent(self, ev):
        m = QMenu(self)
        hint = m.addAction("Drag any button to move it")
        hint.setEnabled(False)
        m.addSeparator()
        m.addAction(theme.line_icon("refresh"), "Reset button order", self.owner.reset_toolbar_order)
        m.exec(ev.globalPos())


# ---------------------------------------------------------------------------------
# saving / restoring
# ---------------------------------------------------------------------------------
def current_order(toolbars) -> dict[str, list[str]]:
    return {tb.key: [a.objectName() for a in tb.actions() if a.objectName()] for tb in toolbars}


def merge_saved(toolbars, saved: dict) -> dict[str, list[str]]:
    """The current order, keeping the places of buttons that are missing right now (unloaded plugins)."""
    cur = current_order(toolbars)
    present = {n for names in cur.values() for n in names}
    out = {}
    for key, names in cur.items():
        names = list(names)
        old = saved.get(key, [])
        for i, n in enumerate(old):
            if n in present or n in names:
                continue
            j = i - 1        # put it back after the nearest earlier button that is still here
            while j >= 0 and old[j] not in names:
                j -= 1
            names.insert(names.index(old[j]) + 1 if j >= 0 else 0, n)
        out[key] = names
    return out


def apply_order(toolbars, saved: dict):
    """Rearrange the toolbars to match saved; buttons not in saved keep their place next to their neighbour."""
    acts, home = {}, {}
    for tb in toolbars:
        for a in tb.actions():
            if a.objectName():
                acts[a.objectName()] = a
                home[a.objectName()] = tb
    claimed = {}
    for tb in toolbars:
        for n in saved.get(tb.key, []):
            if n in acts and n not in claimed:
                claimed[n] = tb
    want = {}
    for tb in toolbars:
        cur = tb.actions()
        w = [acts[n] for n in saved.get(tb.key, []) if claimed.get(n) is tb]
        for i, a in enumerate(cur):
            if a.objectName() in claimed:
                continue
            j = i - 1
            while j >= 0 and cur[j] not in w:
                j -= 1
            w.insert(w.index(cur[j]) + 1 if j >= 0 else 0, a)
        want[tb] = w
    if all(tb.actions() == w for tb, w in want.items()):
        return False
    # take everything that changes toolbar out first: a widget can only live in one toolbar at a time
    for tb, w in want.items():
        for a in tb.actions():
            if a not in w:
                tb.removeAction(a)
    for tb, w in want.items():
        for i, a in enumerate(w):
            cur = tb.actions()
            if i < len(cur) and cur[i] is a:
                continue
            if a in cur:
                tb.removeAction(a)
                cur = tb.actions()
            tb.insertAction(cur[i] if i < len(cur) else None, a)
    return True
