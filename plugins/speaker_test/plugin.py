"""Speaker test plugin.

Detection merges:
  * output devices (AudioManager.getDevices(OUTPUTS)); a stereo speaker is split into its left and right channel,
    which on most phones are two different drivers (bottom speaker + earpiece used as the second speaker)
  * amplifier chips (I2C / SoundWire / platform devices whose name or device-tree id is a speaker amp; most phones
    have one smart amp per physical speaker) and the ALSA sound card
  * outputs the audio HAL declares (audio_policy_configuration*.xml and `dumpsys media.audio_policy`)
Playback: a small dex helper (spktest.jar, source in helper/SpkTest.java) generates a logarithmic sine sweep (or a
steady tone) with AudioTrack, routed to the chosen device, and reports the current frequency and the real routing.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QSlider, QSpinBox, QSplitter, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

JAR_REMOTE = "/data/local/tmp/androunderthehood-spktest.jar"
_NOWIN = 0x08000000 if sys.platform == "win32" else 0
MODES = [("auto", "Auto (shell user, then root)"), ("shell", "Shell user"), ("root", "Root")]
SPEAKER_TYPES = {1, 2, 24, 27}          # earpiece, built-in speaker, speaker (safe), BLE speaker
HIDDEN_TYPES = {18, 25, 20, 21}         # telephony, remote submix, IP, bus: not something you can hear
AMP_RE = re.compile(r"(?<![a-z])amp|spk|speaker|cs35l|cs40l|tfa98|aw88|aw87|aw8155|max98|tas25|tas27|tas57|tas58|"
                    r"rt13\d\d|rt1308|rt1316|rt1318|rt1019|rt1015|fs16|fs18|fs19|sia81|sia91|sma1|ssm4|nau83|wsa8",
                    re.I)
PRESETS = [("Full sweep 20 Hz – 20 kHz", 20, 20000, 10), ("Speech range 200 Hz – 8 kHz", 200, 8000, 6),
           ("Bass 20 – 300 Hz (rattle check)", 20, 300, 8), ("Treble 2 – 20 kHz", 2000, 20000, 6),
           ("Tone 1 kHz", 1000, 1000, 4), ("Tone 440 Hz", 440, 440, 4), ("Tone 100 Hz", 100, 100, 4)]
PROBE_SCRIPT = r"""
echo "//CARDS//"; cat /proc/asound/cards 2>/dev/null
echo "//AMPS//"
for d in /sys/bus/i2c/devices/*-* /sys/bus/soundwire/devices/* /sys/bus/spi/devices/*; do
  [ -e "$d" ] || continue
  n=$(cat $d/name 2>/dev/null); c=$(tr '\0' ' ' < $d/of_node/compatible 2>/dev/null)
  m=$(cat $d/modalias 2>/dev/null); drv=$(readlink $d/driver 2>/dev/null)
  echo "${d##*/}|$n|$c|$m|${drv##*/}"
done
for b in $(ls /sys/bus/platform/devices 2>/dev/null | grep -i -E 'wsa|spk|speaker|amp|cs35l|tfa98|aw88|max98|tas25'); do
  d=/sys/bus/platform/devices/$b; drv=$(readlink $d/driver 2>/dev/null)
  echo "plat:$b|$b|$(tr '\0' ' ' < $d/of_node/compatible 2>/dev/null)||${drv##*/}"
done
echo "//HAL//"
for f in /odm/etc/*audio_policy_configuration*.xml /vendor/etc/*audio_policy_configuration*.xml \
         /vendor/etc/audio/*audio_policy_configuration*.xml /vendor/etc/audio/*/*audio_policy_configuration*.xml \
         /system/etc/*audio_policy_configuration*.xml; do
  [ -r "$f" ] && grep -o '<devicePort[^>]*>' "$f"
done
echo "//DUMPSYS//"
dumpsys media.audio_policy 2>/dev/null | grep -E 'AUDIO_DEVICE_OUT_|Available output|Available input|HW Modules|Hardware modules' | head -600
"""


def register(api):
    api.add_tool("Speakers", lambda: SpeakerDialog(api).show(), icon="speaker",
                 tooltip="Find every speaker and play a test sweep on it (plugin)", needs="connection")


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------------------------- device side
class Device:
    def __init__(self, api):
        self.api = api
        self.conn = api.conn
        self.jar = api.read_file("spktest.jar")
        self.user_uid0 = None
        self.user_uid = None
        self.via_root = False

    @property
    def is_adb(self) -> bool:
        return getattr(self.conn, "transport", None) is None

    def _root_ok(self) -> bool:
        try:
            return bool(self.api.is_root() or getattr(self.conn, "native_root", False))
        except Exception:  # noqa: BLE001
            return False

    def ensure_jar(self):
        """Upload the helper if missing/outdated and work out how to reach the shell user (uid 2000)."""
        want = hashlib.md5(self.jar).hexdigest()
        rc, out, _ = self.conn.exec("id -u", timeout=20, as_root=False)
        try:
            self.user_uid = int(out.decode(errors="replace").split()[0])
        except (ValueError, IndexError):
            self.user_uid = -1
        self.user_uid0 = self.user_uid == 0
        # A session running as an ordinary app (e.g. AndroOpenHeart without Shizuku) can neither use
        # /data/local/tmp nor the hardware APIs: go through root and drop to the shell user from there.
        self.via_root = self.user_uid not in (0, 2000)
        if self.via_root and not self._root_ok():
            raise RuntimeError(f"The connection runs as an app (uid {self.user_uid}), which Android does not let use "
                               "this hardware. Turn Root on, or start Shizuku on the device so the session runs as "
                               "the shell user, then try again.")
        rc, out, _ = self.conn.exec(f"md5sum {JAR_REMOTE} 2>/dev/null", timeout=20, as_root=self.via_root)
        if want in out.decode(errors="replace").split():
            return
        # an outdated copy owned by another user can't be overwritten; delete it first
        for root in (False, True):
            try:
                self.conn.exec(f"rm -f {JAR_REMOTE}", timeout=20, as_root=root)
            except Exception:  # noqa: BLE001
                pass
        if self.is_adb:
            os.makedirs(self.api.data_dir, exist_ok=True)
            tmp = os.path.join(self.api.data_dir, "spktest.jar")
            with open(tmp, "wb") as f:
                f.write(self.jar)
            r = subprocess.run([self.conn.exe, "-s", self.conn.serial, "push", tmp, JAR_REMOTE],
                               capture_output=True, timeout=60, creationflags=_NOWIN)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).decode(errors="replace").strip())
        else:
            cmd = f"cat > {JAR_REMOTE} && chmod 644 {JAR_REMOTE}"
            if self.via_root:
                cmd += f" && chown 2000:2000 {JAR_REMOTE}"
            rc, out, err = self.conn.exec(cmd, timeout=60, stdin=self.jar, as_root=self.via_root)
            if rc != 0:
                raise RuntimeError(err.decode(errors="replace").strip() or f"upload failed ({rc})")

    def command(self, args: str, root: bool) -> tuple[str, bool]:
        """Shell command + whether it must go through a root channel."""
        base = f"CLASSPATH={JAR_REMOTE} exec app_process / SpkTest {args}"
        if root:
            return base, True
        if self.user_uid0 or self.via_root:     # drop to the shell user, the uid these APIs expect
            return f"su 2000 -c {shq(base)}", self.via_root
        return base, False

    def run(self, args: str, root: bool, timeout: float = 60) -> tuple[int, str, str]:
        cmd, as_root = self.command(args, root)
        rc, out, err = self.conn.exec(cmd, timeout=timeout, as_root=as_root)
        return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def open_stream(self, args: str, root: bool):
        cmd, as_root = self.command(args, root)
        if self.is_adb:
            full = self.conn.wrap_root(cmd) if as_root else cmd
            p = subprocess.Popen([self.conn.exe, "-s", self.conn.serial, "exec-out", full], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, creationflags=_NOWIN)
            err = bytearray()
            threading.Thread(target=lambda: [err.extend(b) for b in iter(lambda: p.stderr.read(4096), b"")],
                             daemon=True).start()
            return (lambda n: p.stdout.read1(n)), p.kill, (lambda: err.decode(errors="replace"))
        if getattr(self.conn, "use_pty", False):
            rc, out, err = self.run(args, root, timeout=600)       # no streaming: run it and report at the end
            data = [out.encode()]
            return (lambda n: data.pop() if data else b""), (lambda: None), (lambda: err)
        chan = self.conn.open_channel(as_root)
        chan.exec_command(self.conn.wrap_root(cmd) if as_root else cmd)
        chan.shutdown_write()
        err = bytearray()

        def read(n):
            while chan.recv_stderr_ready():
                err.extend(chan.recv_stderr(65536))
            return chan.recv(n)

        def stderr():
            try:
                while chan.recv_stderr_ready():
                    err.extend(chan.recv_stderr(65536))
            except Exception:  # noqa: BLE001
                pass
            return err.decode(errors="replace")
        return read, chan.close, stderr

    def kill_leftovers(self, token: str):
        """Kill one run's helper if it outlived its connection (each run carries a unique token).
        Root is only used when the shell user could not kill it, to avoid a superuser toast on every stop."""
        pat = shq("SpkTest play .* " + token + "$")
        for root in (False, True):
            try:
                rc, out, _ = self.conn.exec(f"pkill -f {pat} 2>/dev/null; sleep 0.3; pgrep -f {pat} >/dev/null && echo alive",
                                            timeout=15, as_root=root)
                if b"alive" not in out:
                    return
            except Exception:  # noqa: BLE001
                pass


def parse_amps(text: str) -> list[dict]:
    amps = []
    seen = set()
    for ln in text.splitlines():
        parts = (ln.split("|") + [""] * 5)[:5]
        dev, name, compat, modalias, drv = (p.strip() for p in parts)
        hay = " ".join((name, compat, modalias, drv))
        if not AMP_RE.search(hay) or re.search(r"camera|touch|charger|haptic|vib|sar|nfc|cam_", hay, re.I):
            continue
        key = compat or name or dev
        if (dev, key) in seen:
            continue
        seen.add((dev, key))
        platform = dev.startswith("plat:")
        dev = dev[5:] if platform else dev
        amps.append({"bus_id": dev, "name": name or dev, "compatible": compat, "driver": drv,
                     "kind": "platform" if platform else "chip"})
    return amps


def parse_hal_out(xml: str, dump: str) -> list[dict]:
    ports: dict[tuple, dict] = {}
    attached: set[str] = set()

    def add(enum, addr, name, origin):
        enum = enum.replace("AUDIO_DEVICE_OUT_", "")
        enum = {"AUX_DIGITAL": "HDMI"}.get(enum, enum)
        if enum in ("DEFAULT", "STUB", "ECHO_CANCELLER"):
            return
        p = ports.setdefault((enum, addr or ""), {"enum": enum, "address": addr or "", "names": [], "origins": set()})
        if name and name not in p["names"]:
            p["names"].append(name)
        p["origins"].add(origin)

    for tag in re.findall(r"<devicePort[^>]*>", xml):
        a = dict(re.findall(r'(\w+)="([^"]*)"', tag))
        if a.get("role") == "sink" and a.get("type", "").startswith("AUDIO_DEVICE_OUT_"):
            add(a["type"], a.get("address", ""), a.get("tagName", ""), "policy XML")
    for ln in dump.splitlines():
        for name, enum, addr in re.findall(r'(?:"([^"]*)";\s*)?\{?(AUDIO_DEVICE_OUT_[A-Z0-9_]+)(?:,\s*@:([^}\s]*))?', ln):
            add(enum, addr, name, "audio policy")
            if "Port ID" in ln:
                attached.add(enum.replace("AUDIO_DEVICE_OUT_", ""))
    for key in [k for k in ports if not k[1]]:
        same = [p for k, p in ports.items() if k[0] == key[0] and k[1]]
        if same:
            p = ports.pop(key)
            same[0]["names"] += [n for n in p["names"] if n not in same[0]["names"]]
            same[0]["origins"] |= p["origins"]
    out = []
    for p in ports.values():
        p["origins"] = sorted(p["origins"])
        p["attached"] = p["enum"] in attached
        out.append(p)
    return out


def detect(dev: Device, mode: str, root_ok: bool) -> tuple[dict, bool]:
    dev.ensure_jar()
    tries = {"auto": [False, True] if root_ok else [False], "shell": [False], "root": [True]}[mode]
    data, used, last = None, False, ""
    for root in tries:
        rc, out, err = dev.run("list", root)
        start = out.find("{")
        if rc == 0 and start >= 0:
            data, used = json.loads(out[start:]), root
            break
        last = (err.strip() or out.strip() or f"exit code {rc}")[-1500:]
    if data is None:
        raise RuntimeError(last)
    text = ""
    for root in ([True, False] if root_ok else [False]):      # amp details usually need root
        try:
            rc, out, _ = dev.conn.exec(PROBE_SCRIPT, timeout=60, as_root=root)
            text = out.decode("utf-8", "replace")
            if "//AMPS//" in text and text.split("//AMPS//")[1].split("//HAL//")[0].strip():
                break
        except Exception:  # noqa: BLE001
            pass
    cards, _, rest = text.partition("//CARDS//")[2].partition("//AMPS//")
    amps, _, rest = rest.partition("//HAL//")
    xml, _, dump = rest.partition("//DUMPSYS//")
    data["cards"] = [ln.strip() for ln in cards.splitlines() if re.match(r"\s*\d+\s*\[", ln)]
    data["amps"] = parse_amps(amps)
    data["hal"] = parse_hal_out(xml, dump)
    return data, used


# ---------------------------------------------------------------------------------------------- playback
class Player(QObject):
    info = Signal(dict)
    progress = Signal(float, float, int)
    routed = Signal(dict)
    warning = Signal(str)
    failed = Signal(str)
    ended = Signal(bool)          # True when the sweep finished by itself

    def __init__(self, dev: Device, args: str, root: bool, prev_token: str = ""):
        super().__init__()
        self.dev, self.args, self.root, self.prev_token = dev, args, root, prev_token
        self._stop = threading.Event()
        self._stopfn = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._stopfn:
            try:
                self._stopfn()
            except Exception:  # noqa: BLE001
                pass

    def _run(self):
        done = False
        try:
            if self.prev_token:
                self.dev.kill_leftovers(self.prev_token)
            read, self._stopfn, stderr = self.dev.open_stream(self.args, self.root)
            if self._stop.is_set():
                self._stopfn()
                return
            buf = b""
            while not self._stop.is_set():
                chunk = read(4096)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    ln = raw.decode("utf-8", "replace").strip()
                    if not ln:
                        continue
                    kind, _, rest = ln.partition(" ")
                    if kind == "P":
                        f, t, loop = rest.split()
                        self.progress.emit(float(f), float(t), int(loop))
                    elif kind == "I":
                        self.info.emit(json.loads(rest))
                    elif kind == "R":
                        self.routed.emit(json.loads(rest))
                    elif kind == "W":
                        self.warning.emit(rest)
                    elif kind == "E":
                        raise RuntimeError(rest)
                    elif kind == "D":
                        done = True
            if not self._stop.is_set() and not done:
                raise RuntimeError(stderr().strip()[-1500:] or "The helper stopped unexpectedly.")
        except Exception as e:  # noqa: BLE001
            if not self._stop.is_set():
                self.failed.emit(str(e) or repr(e))
        finally:
            if self._stopfn:
                try:
                    self._stopfn()
                except Exception:  # noqa: BLE001
                    pass
            self.ended.emit(done)


class SweepView(QWidget):
    """Log-frequency axis with the sweep range and a cursor at the frequency playing now."""

    def __init__(self, theme):
        super().__init__()
        self.theme = theme
        self.f0, self.f1 = 20.0, 20000.0
        self.now: float | None = None
        self.played: list[float] = []
        self.text = ""
        self.setMinimumHeight(150)

    def sizeHint(self):
        return QSize(700, 200)

    @staticmethod
    def x_of(f, x0, w):
        return x0 + w * math.log(max(f, 10) / 10) / math.log(24000 / 10)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#101418"))
        W, H = self.width(), self.height()
        x0, w = 30, W - 60
        top, bot = 30, H - 34
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)
        p.setPen(QColor("#2a333c"))
        for fr in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            x = self.x_of(fr, x0, w)
            p.drawLine(int(x), top, int(x), bot)
        p.setPen(QColor("#7d8790"))
        for fr in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            x = self.x_of(fr, x0, w)
            p.drawText(QRectF(x - 24, bot + 4, 48, 14), Qt.AlignmentFlag.AlignCenter,
                       f"{fr // 1000}k" if fr >= 1000 else str(fr))
        a, b = sorted((self.f0, self.f1))
        xa, xb = self.x_of(a, x0, w), self.x_of(b, x0, w)
        p.fillRect(QRectF(xa, top, max(3.0, xb - xa), bot - top), QColor(70, 140, 220, 50))
        if self.played:
            lo, hi = min(self.played), max(self.played)
            p.fillRect(QRectF(self.x_of(lo, x0, w), top, max(3.0, self.x_of(hi, x0, w) - self.x_of(lo, x0, w)),
                              bot - top), QColor(70, 160, 110, 90))
        if self.now is not None:
            x = self.x_of(self.now, x0, w)
            p.setPen(QPen(QColor("#f0c040"), 3))
            p.drawLine(int(x), top, int(x), bot)
            big = QFont(self.font())
            big.setPointSizeF(16)
            big.setBold(True)
            p.setFont(big)
            p.setPen(QColor("#ffffff"))
            label = f"{self.now / 1000:.2f} kHz" if self.now >= 1000 else f"{self.now:.0f} Hz"
            p.drawText(QRectF(0, 2, W, 26), Qt.AlignmentFlag.AlignCenter, label)
        elif self.text:
            p.setPen(QColor(self.theme.TEXT_DIM))
            p.drawText(QRectF(0, 2, W, 26), Qt.AlignmentFlag.AlignCenter, self.text)


# ---------------------------------------------------------------------------------------------- window
class SpeakerDialog(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"Speakers – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1150, 680)
        self.dev = Device(api)
        self.data: dict = {}
        self.used_root = False
        self.player: Player | None = None
        self.token = ""
        self.queue: list[dict] = []          # remaining steps of "play each channel"
        self.current_target: dict | None = None

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Run as"))
        self.mode = QComboBox()
        for k, label in MODES:
            self.mode.addItem(label, k)
        self.mode.setCurrentIndex([m[0] for m in MODES].index(api.get_setting("mode", "auto")))
        self.mode.currentIndexChanged.connect(lambda _i: api.set_setting("mode", self.mode.currentData()))
        top.addWidget(self.mode)
        self.b_scan = QPushButton(api.icon("refresh"), " Detect speakers")
        self.b_scan.clicked.connect(self.scan)
        top.addWidget(self.b_scan)
        top.addStretch(1)
        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color: {api.theme.TEXT_DIM};")
        top.addWidget(self.summary)
        lay.addLayout(top)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Speaker / output", "Channel", "Details"])
        self.tree.setColumnWidth(0, 210)
        self.tree.setColumnWidth(1, 70)
        self.tree.currentItemChanged.connect(lambda *_: self.on_select())
        self.tree.itemDoubleClicked.connect(lambda it, _c: self.play() if it.data(0, Qt.ItemDataRole.UserRole) else None)
        ll.addWidget(self.tree, 1)
        self.details = QLabel("")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.details.setStyleSheet(f"color: {api.theme.TEXT_DIM};")
        ll.addWidget(self.details)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.view = SweepView(api.theme)
        rl.addWidget(self.view, 1)
        self.live = QLabel("")
        self.live.setWordWrap(True)
        rl.addWidget(self.live)

        g = QGridLayout()
        g.addWidget(QLabel("Preset"), 0, 0)
        self.preset = QComboBox()
        self.preset.addItem("Custom", None)
        for p in PRESETS:
            self.preset.addItem(p[0], p)
        self.preset.currentIndexChanged.connect(self.on_preset)
        g.addWidget(self.preset, 0, 1, 1, 3)
        g.addWidget(QLabel("From (Hz)"), 1, 0)
        self.f0 = QSpinBox()
        self.f0.setRange(10, 24000)
        self.f0.setValue(int(api.get_setting("f0", 20)))
        g.addWidget(self.f0, 1, 1)
        g.addWidget(QLabel("To (Hz)"), 1, 2)
        self.f1 = QSpinBox()
        self.f1.setRange(10, 24000)
        self.f1.setValue(int(api.get_setting("f1", 20000)))
        g.addWidget(self.f1, 1, 3)
        g.addWidget(QLabel("Length (s)"), 2, 0)
        self.secs = QDoubleSpinBox()
        self.secs.setRange(0.5, 120)
        self.secs.setValue(float(api.get_setting("secs", 10)))
        g.addWidget(self.secs, 2, 1)
        g.addWidget(QLabel("Level"), 2, 2)
        lv = QHBoxLayout()
        self.level = QSlider(Qt.Orientation.Horizontal)
        self.level.setRange(-40, 0)
        self.level.setValue(int(api.get_setting("level", -12)))
        self.level_lbl = QLabel("")
        self.level.valueChanged.connect(lambda v: self.level_lbl.setText(f"{v} dBFS"))
        self.level_lbl.setText(f"{self.level.value()} dBFS")
        lv.addWidget(self.level, 1)
        lv.addWidget(self.level_lbl)
        g.addLayout(lv, 2, 3)
        g.addWidget(QLabel("Play as"), 3, 0)
        self.usage = QComboBox()
        for k, label in (("media", "Media"), ("alarm", "Alarm"), ("call", "Call audio")):
            self.usage.addItem(label, k)
        g.addWidget(self.usage, 3, 1)
        self.repeat = QCheckBox("Repeat until stopped")
        g.addWidget(self.repeat, 3, 2, 1, 2)
        for w in (self.f0, self.f1, self.secs):
            w.valueChanged.connect(self.on_range_edit)
        rl.addLayout(g)

        row = QHBoxLayout()
        self.b_play = QPushButton("Play")
        self.b_play.clicked.connect(self.toggle)
        row.addWidget(self.b_play)
        self.b_each = QPushButton("Play each speaker in turn")
        self.b_each.setToolTip("Plays the sweep on the left channel, then the right channel of the selected output")
        self.b_each.clicked.connect(self.play_each)
        row.addWidget(self.b_each)
        row.addStretch(1)
        self.vol_lbl = QLabel("")
        row.addWidget(self.vol_lbl)
        self.vol = QSpinBox()
        self.vol.setRange(0, 100)
        row.addWidget(self.vol)
        b_vol = QPushButton("Set volume")
        b_vol.setToolTip("Changes the device's volume for the selected 'Play as' stream")
        b_vol.clicked.connect(self.set_volume)
        row.addWidget(b_vol)
        rl.addLayout(row)
        split.addWidget(right)
        split.setSizes([430, 720])
        lay.addWidget(split, 1)
        self.usage.currentIndexChanged.connect(lambda _i: self.show_volume())
        self.on_range_edit()
        self.scan()

    # ---- detection
    def root_ok(self) -> bool:
        return self.api.is_root() or bool(getattr(self.api.conn, "native_root", False))

    def scan(self):
        self.stop()
        self.b_scan.setEnabled(False)
        self.summary.setText("Detecting speakers…")
        self.tree.clear()
        mode, root_ok = self.mode.currentData(), self.root_ok()

        def done(res):
            try:
                self.b_scan.setEnabled(True)
                self.fill(*res)
            except RuntimeError:
                pass

        def fail(msg):
            try:
                self.b_scan.setEnabled(True)
                self.summary.setText("Detection failed")
                QMessageBox.warning(self, "Speakers", f"Could not list the speakers:\n\n{msg}")
            except RuntimeError:
                pass

        self.api.task(lambda: detect(self.dev, mode, root_ok), done, fail)

    def fill(self, data: dict, used_root: bool):
        self.data, self.used_root = data, used_root
        dim = QColor(self.api.theme.TEXT_FAINT)
        devices = [d for d in data.get("devices", []) if d["type"] not in HIDDEN_TYPES]

        def group(title):
            g = QTreeWidgetItem([title])
            f = g.font(0)
            f.setBold(True)
            g.setFont(0, f)
            g.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(g)
            g.setExpanded(True)
            return g

        def add_device(parent, d):
            stereo = 2 in d.get("channel_counts", []) or any(m & 12 == 12 for m in d.get("channel_masks", []))
            label = d["type_name"] + (f" ({d['address']})" if d.get("address") else "")
            extra = f"id {d['id']}, " + ("stereo" if stereo else "mono") + \
                    (f", {d['name']}" if d.get("name") and d["type"] not in SPEAKER_TYPES else "")
            it = QTreeWidgetItem(parent, [label, "both" if stereo else "mono", extra])
            it.setData(0, Qt.ItemDataRole.UserRole, {"device": d, "channel": "both", "stereo": stereo})
            if stereo:
                for ch, nm in (("left", "Left channel"), ("right", "Right channel")):
                    c = QTreeWidgetItem(it, [nm, ch, "one driver on most stereo phones/tablets"])
                    c.setData(0, Qt.ItemDataRole.UserRole, {"device": d, "channel": ch, "stereo": True})
                it.setExpanded(True)
            return it

        spk = [d for d in devices if d["type"] in SPEAKER_TYPES]
        other = [d for d in devices if d["type"] not in SPEAKER_TYPES]
        first = None
        if spk:
            g = group(f"Speakers ({len(spk)} output{'s' if len(spk) != 1 else ''})")
            for d in sorted(spk, key=lambda d: (d["type"] != 2, d["type"])):
                it = add_device(g, d)
                first = first or it
        if other:
            g = group(f"Other outputs ({len(other)})")
            for d in other:
                it = add_device(g, d)
                first = first or it
        amps = [a for a in data.get("amps", []) if a.get("kind") == "chip"]
        dsps = [a for a in data.get("amps", []) if a.get("kind") != "chip"]
        for title, items, tip in ((f"Amplifier chips ({len(amps)})", amps,
                                   "Physical amplifier; most devices have one per speaker driver. Information only."),
                                  (f"Speaker drivers / DSP ({len(dsps)})", dsps,
                                   "Platform driver or DSP block for the speakers (not a separate chip). Information only.")):
            if not items:
                continue
            g = group(title)
            for a in items:
                it = QTreeWidgetItem(g, [a["name"], "", f"{a['compatible'] or 'no device-tree id'} · {a['bus_id']}"
                                                         f"{' · driver ' + a['driver'] if a['driver'] else ''}"])
                it.setToolTip(0, tip)
                for c in range(3):
                    it.setForeground(c, dim)
        hal = data.get("hal", [])
        policy_knows = any(p.get("attached") for p in hal)
        missing = [p for p in hal if policy_knows and not p.get("attached")]
        if missing:
            g = group(f"Declared by the audio HAL, not attached ({len(missing)})")
            for p in sorted(missing, key=lambda p: (p["enum"] not in ("SPEAKER", "EARPIECE", "SPEAKER_SAFE"), p["enum"])):
                builtin = p["enum"] in ("SPEAKER", "EARPIECE", "SPEAKER_SAFE")
                it = QTreeWidgetItem(g, [", ".join(p["names"]) or p["enum"].replace("_", " ").title(), "",
                                         "built-in but not exposed" if builtin else "appears when connected"])
                it.setToolTip(0, f"AUDIO_DEVICE_OUT_{p['enum']} · from {', '.join(p['origins'])}")
                if not builtin:
                    for c in range(3):
                        it.setForeground(c, dim)
        n_drv = sum(2 if (2 in d.get("channel_counts", [])) and d["type"] == 2 else 1 for d in spk)
        txt = f"{data.get('model', '')} · Android API {data.get('sdk')} · {len(spk)} speaker output(s)"
        if amps:
            txt += f" · {len(amps)} amp chip(s)"
        if data.get("cards"):
            txt += " · card " + data["cards"][0].split("]:")[-1].strip()[:40]
        txt += " · as root" if used_root else " · as shell user"
        self.summary.setText(txt)
        self.summary.setToolTip(f"Up to {n_drv} speaker channel(s) reported by Android")
        if first:
            self.tree.setCurrentItem(first)
        self.show_volume()

    def target(self) -> dict | None:
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def on_select(self):
        t = self.target()
        self.b_each.setEnabled(bool(t and t.get("stereo")))
        if not t:
            self.details.setText("")
            return
        d = t["device"]
        lines = [f"<b>{d['type_name']}</b> – output device {d['id']}"
                 f"{' @' + d['address'] if d.get('address') else ''}, '{d.get('name')}'",
                 f"Channels {', '.join(map(str, d.get('channel_counts', [])))} · rates "
                 f"{', '.join(map(str, d.get('sample_rates', [])))} Hz"]
        if d["type"] == 1:
            lines.append("The earpiece is only used for calls: the sweep is played as call audio and routed to it "
                         "(the device switches to call mode while playing).")
        if t["channel"] != "both":
            lines.append(f"Plays only the <b>{t['channel']}</b> channel. On stereo phones one channel is the bottom "
                         "speaker and the other the earpiece/top speaker; which is which depends on the rotation.")
        self.details.setText("<br>".join(lines))

    def show_volume(self):
        v = self.data.get("volumes", {}).get(self.usage.currentData())
        if v:
            self.vol_lbl.setText(f"Device volume {v['cur']}/{v['max']}{' (muted)' if v.get('muted') else ''}")
            self.vol.setRange(0, int(v["max"]))
            self.vol.setValue(int(v["cur"]))

    def set_volume(self):
        usage, n, root = self.usage.currentData(), self.vol.value(), self.used_root

        def work():
            rc, out, err = self.dev.run(f"setvol {usage} {n}", root, timeout=30)
            if rc != 0:
                raise RuntimeError(err.strip()[-500:] or out.strip() or f"exit code {rc}")
            return int(out.strip().split()[-1])

        def done(cur):
            try:
                self.data.setdefault("volumes", {}).setdefault(usage, {"max": self.vol.maximum()})["cur"] = cur
                self.show_volume()
                self.api.message(f"{usage.title()} volume is now {cur}")
            except RuntimeError:
                pass

        self.api.task(work, done)

    def on_preset(self, _i):
        p = self.preset.currentData()
        if not p:
            return
        for w in (self.f0, self.f1, self.secs):
            w.blockSignals(True)
        self.f0.setValue(p[1])
        self.f1.setValue(p[2])
        self.secs.setValue(p[3])
        for w in (self.f0, self.f1, self.secs):
            w.blockSignals(False)
        self.on_range_edit(keep_preset=True)

    def on_range_edit(self, *_a, keep_preset=False):
        if not keep_preset:
            self.preset.blockSignals(True)
            self.preset.setCurrentIndex(0)
            self.preset.blockSignals(False)
        self.view.f0, self.view.f1 = float(self.f0.value()), float(self.f1.value())
        self.view.update()

    # ---- playback
    def toggle(self):
        if self.player:
            self.queue = []
            self.stop()
        else:
            self.play()

    def play_each(self):
        t = self.target()
        if not t:
            return
        self.queue = [dict(t, channel="left"), dict(t, channel="right")]
        self.play(self.queue.pop(0))

    def play(self, t: dict | None = None, root: bool | None = None):
        t = t or self.target()
        if not t:
            return
        prev = self.token if self.player else ""
        self.stop(keep_queue=True)
        if root is None:
            root = self.used_root or self.mode.currentData() == "root"
        for k, w in (("f0", self.f0), ("f1", self.f1), ("secs", self.secs), ("level", self.level)):
            self.api.set_setting(k, w.value())
        d = t["device"]
        self.current_target = t
        self.token = f"t{os.getpid()}x{int(time.time() * 1000) % 10**9}"
        loops = 1000 if self.repeat.isChecked() else 1
        args = (f"play {d['id']} {self.usage.currentData()} {self.f0.value()} {self.f1.value()} "
                f"{self.secs.value():.2f} {t['channel']} {self.level.value()} {loops} {self.token}")
        p = Player(self.dev, args, root, prev)
        p.info.connect(self.on_info)
        p.progress.connect(self.on_progress)
        p.routed.connect(self.on_routed)
        p.warning.connect(lambda m: self.api.message(f"Speakers: {m}"))
        p.failed.connect(lambda m, p=p, root=root, t=t: self.on_failed(p, m, root, t))
        p.ended.connect(lambda done, p=p: self.on_ended(p, done))
        self.player = p
        self.view.played = []
        self.view.now = None
        self.view.text = "Starting…"
        self.view.update()
        where = d["type_name"] + ("" if t["channel"] == "both" else f" – {t['channel']} channel")
        self.live.setText(f"Playing on <b>{where}</b>")
        self.b_play.setText("Stop")
        p.start()

    def stop(self, keep_queue: bool = False):
        if not keep_queue:
            self.queue = []
        p, self.player = self.player, None
        if p:
            p.stop()
            dev, token = self.dev, self.token
            threading.Thread(target=lambda: (time.sleep(1.5), dev.kill_leftovers(token)), daemon=True).start()
        self.b_play.setText("Play")
        self.view.now = None
        self.view.update()

    def on_info(self, info: dict):
        self.vol_lbl.setText(f"Device volume {info.get('volume')}/{info.get('volume_max')}")
        if info.get("volume") == 0:
            self.live.setText(self.live.text() + " · <span style='color:#e5484d'><b>the device volume for this stream "
                                                 "is 0 – nothing will be heard</b></span>")

    def on_progress(self, f: float, t: float, loop: int):
        self.view.now = f
        self.view.played.append(f)
        self.view.update()

    def on_routed(self, r: dict):
        t = self.current_target or {}
        want = (t.get("device") or {}).get("id")
        txt = self.live.text().split(" · Android routed")[0]
        txt += f" · Android routed it to <b>{r.get('name', '?')}</b>"
        if want is not None and r.get("id") not in (None, want):
            txt += " <span style='color:#e5484d'>(not the selected output – Android overrode the choice)</span>"
        self.live.setText(txt)

    def on_failed(self, p, msg, root, t):
        if self.player is not p:
            return
        if not root and self.mode.currentData() == "auto" and self.root_ok():
            self.player = None
            QTimer.singleShot(300, lambda: self.play(t, root=True))
            return
        self.queue = []
        self.view.now = None
        self.view.text = "Playback failed"
        self.view.update()
        self.live.setText(f"<span style='color:#e5484d'>Playback failed: {msg}</span>")

    def on_ended(self, p, done: bool):
        if self.player is not p:
            return
        self.player = None
        try:
            self.b_play.setText("Play")
            self.view.now = None
            self.view.text = "Finished" if done else ""
            self.view.update()
            if done and self.queue:
                QTimer.singleShot(600, lambda: self.play(self.queue.pop(0)) if self.queue else None)
        except RuntimeError:
            pass

    def closeEvent(self, e):
        self.stop()
        super().closeEvent(e)
