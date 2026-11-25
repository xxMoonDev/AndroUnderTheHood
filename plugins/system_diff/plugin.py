"""System diff - snapshot props, settings and package versions, then see exactly what changed (plugin)."""
import csv
import os
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import engine as E

_win = {}
LIVE = "__live__"


def register(api):
    api.add_tool("System diff", lambda: open_window(api), icon="save",
                 tooltip="Snapshot system properties, settings and package versions; compare snapshots (plugin)",
                 needs="connection")


def unload(api):
    w = _win.pop("w", None)
    if w:
        try:
            w.close()
        except RuntimeError:
            pass


def open_window(api):
    w = _win.get("w")
    try:
        if w is not None and w.isVisible():
            w.raise_()
            return
    except RuntimeError:
        pass
    w = _win["w"] = DiffWindow(api)
    w.show()


class Bridge(QObject):
    done = Signal(str, object)
    fail = Signal(str)


class DiffWindow(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"System diff – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1150, 760)
        self.b = Bridge()
        self.b.done.connect(self.on_done)
        self.b.fail.connect(lambda m: (self.status.setText(m), QMessageBox.warning(self, "System diff", m)))
        self.rows = []
        split = QSplitter()
        lay = QVBoxLayout(self)
        lay.addWidget(split, 1)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        tb = QPushButton("Take snapshot…")
        tb.clicked.connect(self.take)
        lv.addWidget(tb)
        lv.addWidget(QLabel("Snapshots"))
        self.snaps = QListWidget()
        lv.addWidget(self.snaps, 1)
        r = QHBoxLayout()
        for text, fn in (("Delete", self.delete), ("Folder", lambda: os.startfile(E.folder(self.api.device_name)))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            r.addWidget(b)
        lv.addLayout(r)
        split.addWidget(left)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        cr = QHBoxLayout()
        cr.addWidget(QLabel("Compare"))
        self.a_box, self.b_box = QComboBox(), QComboBox()
        cr.addWidget(self.a_box, 1)
        cr.addWidget(QLabel("with"))
        cr.addWidget(self.b_box, 1)
        go = QPushButton("Compare")
        go.clicked.connect(self.compare)
        cr.addWidget(go)
        rv.addLayout(cr)
        fr = QHBoxLayout()
        self.filt = QLineEdit()
        self.filt.setPlaceholderText("Filter keys or values…")
        self.filt.textChanged.connect(self.fill)
        fr.addWidget(self.filt, 1)
        self.sec = QComboBox()
        self.sec.addItem("All sections", "")
        for s in E.SECTIONS:
            self.sec.addItem(E.TITLES[s], s)
        self.sec.currentIndexChanged.connect(self.fill)
        fr.addWidget(self.sec)
        self.noise = QCheckBox("Hide noisy keys")
        self.noise.setChecked(True)
        self.noise.setToolTip("Boot times, service PIDs, radio counters and similar values that change on their own")
        self.noise.toggled.connect(lambda *_: self.compare() if self.last else None)
        fr.addWidget(self.noise)
        xb = QPushButton("Export CSV…")
        xb.clicked.connect(self.export)
        fr.addWidget(xb)
        rv.addLayout(fr)
        self.sumlbl = QLabel("Take a snapshot, change something on the device, then compare the snapshot with "
                             "'the device now'.")
        self.sumlbl.setWordWrap(True)
        rv.addWidget(self.sumlbl)
        self.tab = QTableWidget(0, 5)
        self.tab.setHorizontalHeaderLabels(["Change", "Where", "Key", "Before", "After"])
        self.tab.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.tab.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.tab.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tab.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tab.verticalHeader().setVisible(False)
        self.tab.setSortingEnabled(True)
        rv.addWidget(self.tab, 1)
        split.addWidget(right)
        split.setSizes([280, 870])
        self.status = QLabel("")
        lay.addWidget(self.status)
        self.last = None
        self.reload()

    def run(self, op, fn, label):
        self.status.setText(label)

        def body():
            try:
                self.b.done.emit(op, fn())
            except Exception as e:  # noqa: BLE001
                self.b.fail.emit(str(e))
        threading.Thread(target=body, daemon=True).start()

    def on_done(self, op, r):
        getattr(self, "done_" + op)(r)

    def reload(self):
        self.all = E.load_all(self.api.device_name)
        self.snaps.clear()
        for s in self.all:
            n = sum(len(v) for v in s["data"].values())
            self.snaps.addItem(QListWidgetItem(f"{s['created']}  {s.get('label', '')}  ({n} entries)"))
        for box, default in ((self.a_box, 0), (self.b_box, -1)):
            box.clear()
            box.addItem("the device now", LIVE)
            for s in self.all:
                box.addItem(f"{s['created']} {s.get('label', '')}", s["path"])
        self.a_box.setCurrentIndex(1 if self.all else 0)
        self.b_box.setCurrentIndex(0)

    def take(self):
        label, ok = QInputDialog.getText(self, "Snapshot", "Label (optional), e.g. 'before update':")
        if not ok:
            return
        self.run("take", lambda: E.save(E.capture(self.api.conn), self.api.device_name, label), "Reading the device…")

    def done_take(self, path):
        self.status.setText(f"Saved {os.path.basename(path)}")
        self.reload()

    def delete(self):
        r = self.snaps.currentRow()
        if 0 <= r < len(self.all) and QMessageBox.question(self, "Delete", "Delete this snapshot?") \
                == QMessageBox.StandardButton.Yes:
            os.remove(self.all[r]["path"])
            self.reload()

    def side(self, box):
        p = box.currentData()
        if p == LIVE:
            return "the device now", None
        s = next(x for x in self.all if x["path"] == p)
        return f"{s['created']} {s.get('label', '')}", s["data"]

    def compare(self):
        (na, a), (nb, b) = self.side(self.a_box), self.side(self.b_box)
        if a is None and b is None:
            QMessageBox.information(self, "Compare", "Pick a snapshot on at least one side.")
            return
        live = a is None or b is None
        noise = self.noise.isChecked()

        def fn():
            cur = E.capture(self.api.conn) if live else None
            return na, nb, E.diff(a if a is not None else cur, b if b is not None else cur, noise)
        self.run("diff", fn, "Comparing…")

    def done_diff(self, r):
        na, nb, rows = r
        self.last = r
        self.rows = rows
        self.sumlbl.setText(f"<b>{na}</b> → <b>{nb}</b>: {E.summary(rows)}")
        self.fill()
        self.status.setText(f"{len(rows)} difference(s)")

    def fill(self):
        f = self.filt.text().strip().lower()
        sec = self.sec.currentData()
        rows = [r for r in self.rows if (not sec or r[0] == sec) and (not f or f in " ".join(r).lower())]
        colours = {"added": "#3fb950", "removed": self.api.theme.DANGER, "changed": "#f0b429"}
        self.tab.setSortingEnabled(False)
        self.tab.setRowCount(len(rows))
        for i, (s, k, old, new, kind) in enumerate(rows):
            for c, v in enumerate((kind, E.TITLES[s], k, old, new)):
                it = QTableWidgetItem(v)
                it.setToolTip(v)
                if c == 0:
                    it.setForeground(QColor(colours[kind]))
                self.tab.setItem(i, c, it)
        self.tab.resizeColumnToContents(0)
        self.tab.resizeColumnToContents(1)
        self.tab.setColumnWidth(2, 320)
        self.tab.setSortingEnabled(True)

    def export(self):
        if not self.rows:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export diff", "system-diff.csv", "CSV (*.csv)")
        if path:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["change", "section", "key", "before", "after"])
                for s, k, old, new, kind in self.rows:
                    w.writerow([kind, s, k, old, new])
            self.status.setText(f"Exported to {path}")

    def closeEvent(self, e):
        _win.pop("w", None)
        super().closeEvent(e)
