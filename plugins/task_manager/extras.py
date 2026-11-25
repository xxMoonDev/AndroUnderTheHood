"""Task Manager extra tabs: Thermal recorder and Disks (storage health + speed test).

Mixed into TaskManager; uses self.tabs, self.model, self.static, self.dev, self.api and the Graph widget.
"""
from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from . import storage as ST
from . import thermal as TH

CLUSTER_COLOURS = ["#46a758", "#3e8ed0", "#e08a3c", "#a070e0"]
LEVEL = {"ok": "#3fb950", "warn": "#f0b429", "bad": "#ff6b6b", "info": None, "unknown": None}


class _Sig(QObject):
    done = Signal(str, object)
    fail = Signal(str, str)


class ExtrasMixin:
    # ================================================================== Thermal recorder
    def build_thermal(self, Graph, temp_colour):
        self._xs = _Sig()
        self._xs.done.connect(self._x_done)
        self._xs.fail.connect(lambda op, m: QMessageBox.warning(self, "Task Manager", f"{op}: {m}"))
        self.rec = None
        w = QWidget()
        lay = QVBoxLayout(w)
        intro = QLabel("Records temperatures and CPU frequencies from the live sampler and finds throttling – a CPU "
                       "cluster running well below its top frequency while it is busy. Turn on the stress load for a "
                       "repeatable test (one busy loop per core, stops by itself).")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        row = QHBoxLayout()
        self.rec_btn = QPushButton("Start recording")
        self.rec_btn.clicked.connect(self.toggle_recording)
        row.addWidget(self.rec_btn)
        self.stress = QCheckBox("with stress load for")
        row.addWidget(self.stress)
        self.stress_s = QSpinBox()
        self.stress_s.setRange(10, 1800)
        self.stress_s.setValue(120)
        self.stress_s.setSuffix(" s")
        row.addWidget(self.stress_s)
        open_b = QPushButton("Open folder")
        open_b.clicked.connect(lambda: os.startfile(os.path.join(os.path.expanduser("~"), "Documents",
                                                                 "AndroUnderTheHood", "Thermal")))
        row.addWidget(open_b)
        self.rec_live = QLabel("")
        row.addWidget(self.rec_live, 1)
        lay.addLayout(row)
        self.g_temp = Graph(temp_colour, "CPU temperature", vmax=None)
        self.g_freq = Graph("#46a758", "Cluster frequency, % of max", vmax=100)
        lay.addWidget(self.g_temp, 1)
        lay.addWidget(self.g_freq, 1)
        self.rec_sum = QPlainTextEdit()
        self.rec_sum.setReadOnly(True)
        self.rec_sum.setMaximumHeight(150)
        lay.addWidget(self.rec_sum)
        self.tabs.addTab(w, "Thermal recorder")

    def toggle_recording(self):
        if self.rec is None:
            if not self.model or not self.sampler:
                QMessageBox.information(self, "Thermal recorder", "Set the update speed to High or Normal first – "
                                                                  "the recorder uses the live samples.")
                return
            self.rec = TH.Recorder(self.static)
            self.rec_btn.setText("Stop && save")
            self.rec_sum.setPlainText("Recording… " + ", ".join(
                f"cluster {c[0]}-{c[-1]} max {mx / 1e6:.2f} GHz" for mx, c in self.rec.clusters.items()))
            if self.stress.isChecked():
                cores = max(1, self.model.ncpu)
                cmd = TH.stress_cmd(cores, self.stress_s.value())
                threading.Thread(target=lambda: self.dev.run(cmd, timeout=30), daemon=True).start()
                self.rec_stress_until = time.time() + self.stress_s.value()
            else:
                self.rec_stress_until = 0
            return
        rec, self.rec = self.rec, None
        self.rec_btn.setText("Start recording")
        if self.rec_stress_until > time.time():
            threading.Thread(target=lambda: self.dev.run(TH.stop_stress_cmd(), timeout=30), daemon=True).start()
        rec.close()
        path = rec.save_csv(self.api.device_name) if rec.rows else ""
        self.rec_sum.setPlainText("\n".join(rec.summary()) + (f"\n\nSaved: {path}" if path else ""))

    def thermal_tick(self):
        """Called from on_snapshot."""
        if self.rec is None:
            return
        row = self.rec.add(self.model)
        if not row:
            return
        rows = self.rec.rows[-300:]
        temps = [r["cpu_temp"] or 0 for r in rows]
        self.g_temp.set((temps, "#e5484d", ""), unit=" °C", right=f"{temps[-1]:.1f} °C" if temps else "")
        series = []
        for i, mx in enumerate(self.rec.clusters):
            series.append(([r.get(f"c{mx}_pct") or 0 for r in rows], CLUSTER_COLOURS[i % 4], f"{mx / 1e6:.2f} GHz"))
        self.g_freq.set(*series)
        left = int(self.rec_stress_until - time.time())
        thr = sum(1 for _ in self.rec.events) + len(self.rec._open)
        self.rec_live.setText(f"{row['t']:.0f} s · CPU {row['cpu']:.0f} % · {row['cpu_temp'] or '?'} °C · "
                              f"throttling episodes: {thr}" + (f" · stress {left} s left" if left > 0 else ""))

    # ================================================================== Disks
    def build_disks(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        row = QHBoxLayout()
        rb = QPushButton("Read health")
        rb.clicked.connect(self.read_health)
        row.addWidget(rb)
        row.addWidget(QLabel("Speed test size"))
        self.sp_size = QComboBox()
        for mb in (128, 256, 512, 1024):
            self.sp_size.addItem(f"{mb} MB", mb)
        self.sp_size.setCurrentIndex(1)
        row.addWidget(self.sp_size)
        sb = QPushButton("Run speed test")
        sb.setToolTip("Writes a temporary file in /data (fsync), reads it back after dropping the page cache, deletes it")
        sb.clicked.connect(self.run_speed)
        row.addWidget(sb)
        row.addStretch(1)
        outer.addLayout(row)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        self.disk_box = QWidget()
        self.disk_lay = QVBoxLayout(self.disk_box)
        sa.setWidget(self.disk_box)
        outer.addWidget(sa, 1)
        self.speed_log = QPlainTextEdit()
        self.speed_log.setReadOnly(True)
        self.speed_log.setMaximumHeight(120)
        outer.addWidget(self.speed_log)
        self.tabs.addTab(w, "Disks")
        self._disks_read = False
        self.tabs.currentChanged.connect(lambda i: self.read_health()
                                         if self.tabs.tabText(i) == "Disks" and not self._disks_read else None)

    def _x_run(self, op, fn):
        def body():
            try:
                self._xs.done.emit(op, fn())
            except Exception as e:  # noqa: BLE001
                self._xs.fail.emit(op, str(e))
        threading.Thread(target=body, daemon=True).start()

    def read_health(self):
        self._disks_read = True
        self._x_run("health", lambda: ST.read(self.dev.conn))

    def run_speed(self):
        mb = self.sp_size.currentData()
        self.speed_log.appendPlainText(f"testing {mb} MB …")
        self._x_run("speed", lambda: ST.speed_test(self.dev.conn, mb))

    def _x_done(self, op, r):
        if op == "health":
            self.show_health(r)
        elif op == "speed":
            self.speed_log.appendPlainText(f"{r['mb']} MB: write {r['write_mbs']:.0f} MB/s ({r['write_s']:.1f} s, "
                                           f"fsync), read {r['read_mbs']:.0f} MB/s ({r['read_s']:.1f} s, uncached)")

    def show_health(self, info):
        while self.disk_lay.count():
            it = self.disk_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not info["chips"]:
            self.disk_lay.addWidget(QLabel("No eMMC/UFS health information is exposed by this kernel."))
        for c in info["chips"]:
            g = QGroupBox(f"{c['kind']} storage")
            fl = QFormLayout(g)
            for label, value, level in c["report"]:
                v = QLabel(value)
                col = LEVEL.get(level)
                if col:
                    v.setStyleSheet(f"color: {col}; font-weight: 600;")
                fl.addRow(label, v)
            self.disk_lay.addWidget(g)
        g = QGroupBox("/data")
        fl = QFormLayout(g)
        if info.get("data_total"):
            fl.addRow("Used", QLabel(f"{info['data_used'] / 1e9:.1f} of {info['data_total'] / 1e9:.1f} GB "
                                     f"({info['data_free'] / 1e9:.1f} GB free)"))
        if info.get("lifetime_kb"):
            fl.addRow("Written since format", QLabel(f"{info['lifetime_kb'] / 1024 / 1024:.1f} GB "
                                                     f"({info.get('data_fs', '')} lifetime counter)"))
        self.disk_lay.addWidget(g)
        note = QLabel("Wear uses the JEDEC scale the chip reports: 10 % steps of its rated write endurance. "
                      "'Reserve blocks' is the spare-block pool for worn-out blocks.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {self.api.theme.TEXT_DIM};")
        self.disk_lay.addWidget(note)
        self.disk_lay.addStretch(1)
