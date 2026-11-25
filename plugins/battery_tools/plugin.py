"""Example AndroUnderTheHood plugin.

Shows the main parts of the plugin API:
  * api.add_tool        – a toolbar button that opens a window
  * api.add_file_action – an item in the file browser's right-click menu
  * api.run / api.task  – run shell commands on the phone in the background
  * api.on              – react to the app connecting / disconnecting
  * api.get_setting / api.set_setting – remember options between runs
"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QVBoxLayout)

FIELDS = [("level", "Level", "%"), ("status", "Status", ""), ("plugged", "Charger", ""),
          ("temperature", "Temperature", "°C"), ("voltage", "Voltage", "V"), ("current", "Current", "mA"),
          ("health", "Health", ""), ("technology", "Technology", ""), ("cycle", "Charge cycles", "")]
STATUS = {"1": "Unknown", "2": "Charging", "3": "Discharging", "4": "Not charging", "5": "Full"}
HEALTH = {"1": "Unknown", "2": "Good", "3": "Overheat", "4": "Dead", "5": "Over voltage", "6": "Failure", "7": "Cold"}

READ = r"""
dumpsys battery 2>/dev/null
for d in /sys/class/power_supply/battery /sys/class/power_supply/bms; do
  [ -r $d/current_now ] && echo "  current_now: $(cat $d/current_now)" && break
done
for d in /sys/class/power_supply/battery /sys/class/power_supply/bms; do
  [ -r $d/cycle_count ] && echo "  cycle_count: $(cat $d/cycle_count)" && break
done
"""


def register(api):
    api.add_tool("Battery", lambda: BatteryDialog(api).show(), icon="battery",
                 tooltip="Live battery level, temperature, voltage and current (plugin)", needs="connection")
    api.add_tool("Battery drain analysis", lambda: open_drain(api), icon="down", toolbar=False,
                 tooltip="Which apps and wake locks use the battery, since the last full charge (plugin)",
                 needs="root")


def open_drain(api):
    from .drain_ui import DrainDialog
    DrainDialog(api).show()
    api.add_file_action("SHA-256 checksum", lambda entries: checksum(api, entries), when="file")
    api.on("connected", lambda conn: api.log.info("Battery plugin: connected to %s", api.device_name))


def parse(text: str) -> dict:
    raw = {}
    for line in text.splitlines():
        k, sep, v = line.strip().partition(":")
        if sep:
            raw[k.strip().lower()] = v.strip()
    out = {}
    if "level" in raw:
        out["level"] = raw["level"]
    out["status"] = STATUS.get(raw.get("status", ""), raw.get("status", ""))
    out["plugged"] = ", ".join(n for n in ("ac", "usb", "wireless", "dock")
                               if raw.get(f"{n} powered") == "true").upper() or "None"
    if raw.get("temperature", "").lstrip("-").isdigit():
        out["temperature"] = f"{int(raw['temperature']) / 10:.1f}"
    if raw.get("voltage", "").isdigit():
        v = int(raw["voltage"])
        out["voltage"] = f"{v / 1000:.3f}" if v > 100 else str(v)
    cur = raw.get("current_now", "")
    if cur.lstrip("-").isdigit():
        c = int(cur)
        out["current"] = f"{c / 1000:.0f}" if abs(c) > 10000 else str(c)   # µA on most kernels
    out["health"] = HEALTH.get(raw.get("health", ""), raw.get("health", ""))
    out["technology"] = raw.get("technology", "")
    out["cycle"] = raw.get("cycle_count", "")
    return out


class BatteryDialog(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"Battery – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(380, 360)
        lay = QVBoxLayout(self)
        grid = QGridLayout()
        self.vals = {}
        for r, (key, label, unit) in enumerate(FIELDS):
            a = QLabel(label)
            a.setStyleSheet(f"color: {api.theme.TEXT_DIM};")
            b = QLabel("…")
            b.setStyleSheet("font-weight: 600; font-size: 12pt;")
            grid.addWidget(a, r, 0)
            grid.addWidget(b, r, 1)
            self.vals[key] = (b, unit)
        lay.addLayout(grid)
        row = QHBoxLayout()
        self.live = QCheckBox("Live (every 3 s)")
        self.live.setChecked(bool(api.get_setting("live", True)))
        self.live.toggled.connect(self.set_live)
        row.addWidget(self.live)
        row.addStretch(1)
        b = QPushButton(api.icon("refresh"), " Refresh")
        b.clicked.connect(self.refresh)
        row.addWidget(b)
        d = QPushButton("Drain analysis…")
        d.setToolTip("Which apps and wake locks use the battery (needs Root)")
        d.clicked.connect(lambda: open_drain(api))
        row.addWidget(d)
        lay.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.busy = False
        self.set_live(self.live.isChecked())
        self.refresh()

    def set_live(self, on):
        self.api.set_setting("live", on)
        self.timer.start(3000) if on else self.timer.stop()

    def refresh(self):
        if self.busy or not self.api.connected():
            return
        self.busy = True

        def work():
            rc, out, err = self.api.run(READ, root=False, timeout=20)
            return parse(out)

        def done(d):
            self.busy = False
            try:
                for key, (lbl, unit) in self.vals.items():
                    v = d.get(key, "")
                    lbl.setText(f"{v} {unit}".strip() if v else "—")
            except RuntimeError:   # window closed meanwhile
                pass

        def fail(m):
            self.busy = False
            self.api.message(f"Battery: {m}", error=True)

        self.api.task(work, done, fail)

    def closeEvent(self, e):
        self.timer.stop()
        super().closeEvent(e)


def checksum(api, entries):
    paths = [e.path for e in entries]

    def work():
        q = " ".join("'" + p.replace("'", "'\\''") + "'" for p in paths)
        rc, out, err = api.run(f"sha256sum {q}", timeout=600)
        if rc != 0:
            raise RuntimeError(err.strip() or out.strip() or f"exit code {rc}")
        return out.strip()

    def done(text):
        lines = [ln.split(None, 1) for ln in text.splitlines() if ln.strip()]
        body = "\n\n".join(f"{p.rsplit('/', 1)[-1]}\n{h}" for h, p in lines)
        box = QMessageBox(api.window)
        box.setWindowTitle("SHA-256")
        box.setText(body)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.show()

    api.message(f"Computing SHA-256 of {len(paths)} file(s) on the device…")
    api.task(work, done)
