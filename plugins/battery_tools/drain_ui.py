"""Battery drain window (Battery plugin): insights, per-app use, app & kernel wake locks, wake locks held now."""
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from . import drain as D


class _Sig(QObject):
    done = Signal(object)
    fail = Signal(str)


def table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.verticalHeader().setVisible(False)
    t.setSortingEnabled(True)
    return t


class NumItem(QTableWidgetItem):
    def __init__(self, text, key):
        super().__init__(text)
        self.key = key

    def __lt__(self, other):
        return self.key < getattr(other, "key", 0)


class DrainDialog(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"Battery drain – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(980, 720)
        self.s = _Sig()
        self.s.done.connect(self.show_result)
        self.s.fail.connect(lambda m: QMessageBox.warning(self, "Battery drain", m))
        lay = QVBoxLayout(self)
        self.head = QLabel("Reading Android's battery accounting…")
        self.head.setWordWrap(True)
        self.head.setStyleSheet("font-weight: 600;")
        lay.addWidget(self.head)
        self.ins = QLabel("")
        self.ins.setWordWrap(True)
        self.ins.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.ins)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.apps = QTreeWidget()
        self.apps.setHeaderLabels(["App / wake lock", "Estimated use", "Wake-lock time", "Times"])
        self.apps.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.apps, "Apps")
        self.kern = table(["Kernel wake lock", "Time", "Times"])
        self.kern.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.kern, "Kernel wake locks")
        self.held = table(["Held now", "Type", "By", "For"])
        self.held.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.held, "Held right now")
        row = QHBoxLayout()
        rb = QPushButton(api.icon("refresh"), " Refresh")
        rb.clicked.connect(self.refresh)
        row.addWidget(rb)
        xb = QPushButton("Reset statistics…")
        xb.setToolTip("Start counting from zero (dumpsys batterystats --reset)")
        xb.clicked.connect(self.reset)
        row.addWidget(xb)
        row.addStretch(1)
        note = QLabel("Since the last full charge · source: dumpsys batterystats")
        note.setStyleSheet(f"color: {api.theme.TEXT_DIM};")
        row.addWidget(note)
        lay.addLayout(row)
        self.refresh()

    def refresh(self):
        def work():
            return D.collect(self.api.conn)
        self.api.task(work, self.s.done.emit, self.s.fail.emit)

    def reset(self):
        if QMessageBox.question(self, "Reset statistics",
                                "Clear Android's battery statistics on the device and start counting from now?\n\n"
                                "Android's own battery screen loses its history too.") != QMessageBox.StandardButton.Yes:
            return
        self.api.task(lambda: (D.reset(self.api.conn), D.collect(self.api.conn))[1], self.s.done.emit, self.s.fail.emit)

    def show_result(self, r):
        try:
            t = r["totals"]
            cap = t.get("capacity_mah")
            self.head.setText(
                f"On battery {D.ms(t.get('on_battery_ms', 0))} since the last full charge · screen off "
                f"{D.ms(t.get('screen_off_ms', 0))} · battery {t.get('level', '?')} %"
                + (" (charging)" if t.get("plugged") else "") + (f" · capacity {cap} mAh" if cap else ""))
            self.ins.setText("\n".join("•  " + x for x in D.insights(r)))
            self.apps.clear()
            for a in sorted(r["apps"].values(), key=lambda a: (-a.mah, -a.wake_ms)):
                if a.mah < 0.005 and a.wake_ms <= 0 and not a.wakelocks:
                    continue
                it = QTreeWidgetItem([a.name + (f"  ({len(a.pkgs)} packages share this uid)" if len(a.pkgs) > 1 else ""),
                                      f"{a.mah:.2f} mAh", D.ms(a.wake_ms) if a.wake_ms else "", ""])
                it.setToolTip(0, f"uid {a.uid}\n" + "\n".join(a.pkgs))
                if a.wake_ms > 30 * 60 * 1000:
                    it.setForeground(2, QColor(self.api.theme.DANGER))
                for tag, w_ms, cnt in a.wakelocks[:30]:
                    QTreeWidgetItem(it, ["    " + tag, "", D.ms(w_ms), str(cnt)])
                self.apps.addTopLevelItem(it)
            self.fill(self.kern, [(k, NumItem(D.ms(m), m), NumItem(str(c), c)) for k, m, c in r["kernel"][:200]])
            self.fill(self.held, [(h["tag"], h["type"], h["who"], h["for"] + ("  LONG" if h["long"] else ""))
                                  for h in r["held"]])
        except RuntimeError:
            pass

    @staticmethod
    def fill(t, rows):
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for c, v in enumerate(row):
                t.setItem(i, c, v if isinstance(v, QTableWidgetItem) else QTableWidgetItem(str(v)))
        t.resizeColumnsToContents()
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        t.setSortingEnabled(True)
