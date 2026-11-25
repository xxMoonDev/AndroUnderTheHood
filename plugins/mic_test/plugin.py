"""Microphone test plugin.

Detection merges three sources so no microphone is missed:
  * physical microphones (AudioManager.getMicrophones(): every capsule with its location, position, group…)
  * input devices (AudioManager.getDevices(INPUTS): built-in mic addresses, wired/USB/Bluetooth headsets…)
  * inputs the audio HAL declares (audio_policy_configuration*.xml and `dumpsys media.audio_policy`),
    including ones that are not attached right now
Streaming sends 16-bit PCM from a small dex helper (mictest.jar, source in helper/MicTest.java) that uses
AudioRecord as the shell user (or root). The window shows level meters per channel, a waveform, a spectrum, which
physical capsules Android is really using, and can play the sound on the PC or record it to WAV.
"""
from __future__ import annotations

import array
import cmath
import collections
import ctypes
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
import threading
import time
import wave

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QSlider, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

JAR_REMOTE = "/data/local/tmp/androunderthehood-mictest.jar"
_NOWIN = 0x08000000 if sys.platform == "win32" else 0
MODES = [("auto", "Auto (shell user, then root)"), ("shell", "Shell user"), ("root", "Root")]
SOURCES = [(1, "MIC – normal"), (5, "CAMCORDER – often the back/video mic"),
           (6, "VOICE_RECOGNITION – light processing"), (9, "UNPROCESSED – raw"),
           (7, "VOICE_COMMUNICATION – call processing, noise cancel"), (10, "VOICE_PERFORMANCE – low latency"),
           (0, "DEFAULT")]
# AudioDeviceInfo type -> AUDIO_DEVICE_IN_* names used by the HAL / audio policy
TYPE_ENUMS = {15: ("BUILTIN_MIC", "BACK_MIC"), 3: ("WIRED_HEADSET",), 7: ("BLUETOOTH_SCO_HEADSET",),
              11: ("USB_DEVICE",), 22: ("USB_HEADSET",), 12: ("USB_ACCESSORY",), 18: ("TELEPHONY_RX", "VOICE_CALL"),
              25: ("REMOTE_SUBMIX",), 16: ("FM_TUNER",), 17: ("TV_TUNER",), 28: ("ECHO_REFERENCE",),
              9: ("HDMI", "AUX_DIGITAL"), 10: ("HDMI_ARC",), 29: ("HDMI_EARC",), 5: ("LINE",), 6: ("SPDIF",),
              26: ("BLE_HEADSET",), 13: ("ANLG_DOCK_HEADSET", "DGTL_DOCK_HEADSET"), 21: ("BUS",), 20: ("IP",),
              8: ("BLUETOOTH_A2DP",), 30: ("BLE_BROADCAST",)}
ENUM_TYPE = {e: t for t, es in TYPE_ENUMS.items() for e in es}
MIC_ENUMS = {"BUILTIN_MIC", "BACK_MIC", "WIRED_HEADSET", "BLUETOOTH_SCO_HEADSET", "USB_DEVICE", "USB_HEADSET",
             "USB_ACCESSORY", "BLE_HEADSET", "LINE", "ANLG_DOCK_HEADSET", "DGTL_DOCK_HEADSET", "BLUETOOTH_BLE"}
NON_MIC_TYPES = {18, 25, 16, 17, 28, 9, 10, 29, 21, 20, 8, 30}

HAL_SCRIPT = r"""
for f in /odm/etc/*audio_policy_configuration*.xml /vendor/etc/*audio_policy_configuration*.xml \
         /vendor/etc/audio/*audio_policy_configuration*.xml /vendor/etc/audio/*/*audio_policy_configuration*.xml \
         /system/etc/*audio_policy_configuration*.xml; do
  [ -r "$f" ] && { echo "//FILE// $f"; grep -o '<devicePort[^>]*>' "$f"; }
done
echo "//DUMPSYS//"
dumpsys media.audio_policy 2>/dev/null | grep -E 'AUDIO_DEVICE_IN_|Available input|Available output|HW Modules|Hardware modules|- address: ' | head -600
"""


def register(api):
    api.add_tool("Microphones", lambda: MicDialog(api).show(), icon="mic",
                 tooltip="Find every microphone and listen to it live (plugin)", needs="connection")


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------------------------- device side
class Device:
    def __init__(self, api):
        self.api = api
        self.conn = api.conn
        self.jar = api.read_file("mictest.jar")
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
            tmp = os.path.join(self.api.data_dir, "mictest.jar")
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
        base = f"CLASSPATH={JAR_REMOTE} exec app_process / MicTest {args}"
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
            raise RuntimeError("This SSH server only allows interactive shells, which can't carry an audio stream.")
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
        pat = shq("MicTest stream .* " + token + "$")
        for root in (False, True):
            try:
                rc, out, _ = self.conn.exec(f"pkill -f {pat} 2>/dev/null; sleep 0.3; pgrep -f {pat} >/dev/null && echo alive",
                                            timeout=15, as_root=root)
                if b"alive" not in out:
                    return
            except Exception:  # noqa: BLE001
                pass


def parse_hal(text: str) -> list[dict]:
    """Input ports declared by the audio HAL (policy XML) or listed by the audio policy service."""
    ports: dict[tuple, dict] = {}

    def add(enum, addr, name, origin):
        enum = enum.replace("AUDIO_DEVICE_IN_", "")
        enum = {"VOICE_CALL": "TELEPHONY_RX", "AUX_DIGITAL": "HDMI"}.get(enum, enum)     # same value, two names
        if enum in ("DEFAULT", "STUB", "AMBIENT", "COMMUNICATION"):
            return
        key = (enum, addr or "")
        p = ports.setdefault(key, {"enum": enum, "address": addr or "", "names": [], "origins": set()})
        if name and name not in p["names"]:
            p["names"].append(name)
        p["origins"].add(origin)

    attached: set[str] = set()          # enums the audio policy reports as connected right now
    xml, _, dump = text.partition("//DUMPSYS//")
    for tag in re.findall(r"<devicePort[^>]*>", xml):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', tag))
        if attrs.get("role") == "source" and attrs.get("type", "").startswith("AUDIO_DEVICE_IN_"):
            add(attrs["type"], attrs.get("address", ""), attrs.get("tagName", ""), "policy XML")
    lines = dump.splitlines()
    in_available = False
    for i, ln in enumerate(lines):
        if "Available input" in ln:
            in_available = True
            continue
        if re.search(r"Available output|HW Modules|Hardware modules|^\S", ln) and "AUDIO_DEVICE_IN_" not in ln:
            in_available = False
        # new format:  1. Port ID: 12; "Built-In Mic"; {AUDIO_DEVICE_IN_BUILTIN_MIC, @:bottom}
        for name, enum, addr in re.findall(r'(?:"([^"]*)";\s*)?\{?(AUDIO_DEVICE_IN_[A-Z0-9_]+)(?:,\s*@:([^}\s]*))?', ln):
            add(enum, addr, name, "audio policy")
            if "Port ID" in ln or (in_available and "- type:" in ln):
                attached.add(enum.replace("AUDIO_DEVICE_IN_", ""))
        # old format:  - type: AUDIO_DEVICE_IN_BUILTIN_MIC / - address: bottom
        m = re.search(r"- type: (AUDIO_DEVICE_IN_[A-Z0-9_]+)", ln)
        if m and i + 1 < len(lines):
            a = re.search(r"- address: (\S+)", lines[i + 1])
            if a:
                add(m.group(1), a.group(1), "", "audio policy")
    # an address-less declaration is the same port as an addressed one of the same type
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
    last = ""
    data, used = None, False
    for root in tries:
        rc, out, err = dev.run("list", root)
        start = out.find("{")
        if rc == 0 and start >= 0:
            data, used = json.loads(out[start:]), root
            break
        last = (err.strip() or out.strip() or f"exit code {rc}")[-1500:]
    if data is None:
        raise RuntimeError(last)
    try:
        rc, out, err = dev.conn.exec(HAL_SCRIPT, timeout=40, as_root=dev.via_root)
        data["hal"] = parse_hal(out.decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        data["hal"] = []
        data["hal_error"] = str(e)
    return data, used


# ---------------------------------------------------------------------------------------------- PC audio out
class WaveOut:
    """Minimal Windows waveOut player (stdlib only)."""

    def __init__(self, rate: int, channels: int):
        from ctypes import wintypes as W

        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [("wFormatTag", W.WORD), ("nChannels", W.WORD), ("nSamplesPerSec", W.DWORD),
                        ("nAvgBytesPerSec", W.DWORD), ("nBlockAlign", W.WORD), ("wBitsPerSample", W.WORD),
                        ("cbSize", W.WORD)]

        class WAVEHDR(ctypes.Structure):
            _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", W.DWORD), ("dwBytesRecorded", W.DWORD),
                        ("dwUser", ctypes.c_size_t), ("dwFlags", W.DWORD), ("dwLoops", W.DWORD),
                        ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]
        self.HDR = WAVEHDR
        self.mm = ctypes.WinDLL("winmm")
        self.mm.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.POINTER(WAVEFORMATEX),
                                        ctypes.c_size_t, ctypes.c_size_t, W.DWORD]
        for fn in ("waveOutPrepareHeader", "waveOutUnprepareHeader", "waveOutWrite"):
            getattr(self.mm, fn).argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint]
        self.mm.waveOutReset.argtypes = [ctypes.c_void_p]
        self.mm.waveOutClose.argtypes = [ctypes.c_void_p]
        fmt = WAVEFORMATEX(1, channels, rate, rate * channels * 2, channels * 2, 16, 0)
        self.h = ctypes.c_void_p()
        r = self.mm.waveOutOpen(ctypes.byref(self.h), 0xFFFFFFFF, ctypes.byref(fmt), 0, 0, 0)
        if r != 0:
            raise RuntimeError(f"waveOutOpen failed ({r})")
        self.pending: list = []
        self.lock = threading.Lock()
        self.bytes_per_ms = rate * channels * 2 / 1000
        self.write(b"\0" * int(self.bytes_per_ms * 80))      # small pre-roll against stutter

    def _reap(self):
        keep = []
        for hdr, buf in self.pending:
            if hdr.dwFlags & 1:                                 # WHDR_DONE
                self.mm.waveOutUnprepareHeader(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
            else:
                keep.append((hdr, buf))
        self.pending = keep

    def write(self, pcm: bytes):
        with self.lock:
            if not self.h:
                return
            self._reap()
            queued = sum(h.dwBufferLength for h, _ in self.pending)
            if queued > self.bytes_per_ms * 300:                # >300 ms behind: drop to keep latency low
                return
            buf = ctypes.create_string_buffer(pcm, len(pcm))
            hdr = self.HDR(ctypes.cast(buf, ctypes.c_void_p), len(pcm), 0, 0, 0, 0, None, 0)
            self.mm.waveOutPrepareHeader(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
            self.mm.waveOutWrite(self.h, ctypes.byref(hdr), ctypes.sizeof(hdr))
            self.pending.append((hdr, buf))

    def close(self):
        with self.lock:
            if self.h:
                self.mm.waveOutReset(self.h)
                self._reap()
                self.mm.waveOutClose(self.h)
                self.h = ctypes.c_void_p()


# ---------------------------------------------------------------------------------------------- streaming
class Streamer(QObject):
    info = Signal(dict)
    status = Signal(dict)
    failed = Signal(str)
    ended = Signal()

    def __init__(self, dev: Device, args: str, root: bool, prev_token: str = ""):
        super().__init__()
        self.dev, self.args, self.root = dev, args, root
        self.prev_token = prev_token          # helper of the previous stream, killed before this one opens the mic
        self._stop = threading.Event()
        self._stopfn = None
        self.rate, self.channels = 48000, 1
        self.lock = threading.Lock()
        self.peak = [0.0] * 8
        self.rms_acc = [0.0] * 8
        self.rms_n = 0
        self.clip = [0.0] * 8                    # time of the last clipped sample per channel
        self.history = collections.deque(maxlen=600)   # (per-channel (min, max)) every 10 ms -> 6 s
        self.tail = collections.deque(maxlen=8)        # recent PCM for the spectrum
        self.total = 0
        self.player: WaveOut | None = None
        self.listen_channel = -1                 # -1 = all (mono/stereo), else one channel of many
        self.gain = 1.0
        self.wav: wave.Wave_write | None = None
        self.got_audio = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._stopfn:
            try:
                self._stopfn()
            except Exception:  # noqa: BLE001
                pass

    # -- listening / recording (called from the GUI thread)
    def set_listen(self, on: bool):
        with self.lock:
            if self.player:
                self.player.close()
                self.player = None
            if on and sys.platform == "win32":
                ch = self.channels if self.listen_channel < 0 and self.channels <= 2 else 1
                self.player = WaveOut(self.rate, ch)

    def start_wav(self, path: str):
        with self.lock:
            w = wave.open(path, "wb")
            w.setnchannels(self.channels)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            self.wav = w

    def stop_wav(self):
        with self.lock:
            if self.wav:
                self.wav.close()
                self.wav = None

    def take_levels(self):
        """Peak and RMS (0..1) per channel since the last call."""
        with self.lock:
            n = max(self.rms_n, 1)
            res = [(self.peak[c], math.sqrt(self.rms_acc[c] / n)) for c in range(self.channels)]
            self.peak = [0.0] * 8
            self.rms_acc = [0.0] * 8
            self.rms_n = 0
            return res

    def _audio(self, pcm: bytes):
        ch = self.channels
        a = array.array("h")
        a.frombytes(pcm[:len(pcm) - len(pcm) % (2 * ch)])
        if sys.byteorder == "big":
            a.byteswap()
        frames = len(a) // ch
        if not frames:
            return
        now = time.monotonic()
        step = max(1, self.rate // 100)         # 10 ms columns for the waveform
        cols = []
        for s in range(0, frames, step):
            seg = a[s * ch:min(frames, s + step) * ch]
            cols.append(tuple((min(seg[c::ch]) / 32768, max(seg[c::ch]) / 32768) for c in range(ch)))
        with self.lock:
            for c in range(ch):
                chan = a[c::ch]
                lo, hi = min(chan), max(chan)
                pk = max(-lo, hi) / 32768
                self.peak[c] = max(self.peak[c], pk)
                if pk >= 0.999:
                    self.clip[c] = now
                self.rms_acc[c] += sum(x * x for x in chan) / (frames * 1073741824.0)
            self.rms_n += 1
            self.history.extend(cols)
            self.tail.append(a)
            self.total += len(pcm)
            if self.wav:
                self.wav.writeframes(pcm)
            player = self.player
        if player:
            if self.listen_channel >= 0 and ch > 1:
                out = a[self.listen_channel::ch]
            elif ch > 2:
                out = a[0::ch]
            else:
                out = a
            if self.gain != 1.0:
                g = self.gain
                out = array.array("h", (max(-32768, min(32767, int(x * g))) for x in out))
            try:
                player.write(out.tobytes())
            except Exception:  # noqa: BLE001
                pass

    def _run(self):
        try:
            if self.prev_token:
                self.dev.kill_leftovers(self.prev_token)
            read, self._stopfn, stderr = self.dev.open_stream(self.args, self.root)
            if self._stop.is_set():
                self._stopfn()
                return
            buf = bytearray()
            while not self._stop.is_set():
                chunk = read(1 << 16)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 5:
                    t = buf[0]
                    n = struct.unpack(">I", bytes(buf[1:5]))[0]
                    if n > 16 << 20 or t not in b"IASEK":
                        raise RuntimeError("Corrupt stream: " + bytes(buf[:80]).decode(errors="replace"))
                    if len(buf) < 5 + n:
                        break
                    payload = bytes(buf[5:5 + n])
                    del buf[:5 + n]
                    if t == ord("A"):
                        self.got_audio = True
                        self._audio(payload)
                    elif t == ord("I"):
                        d = json.loads(payload)
                        self.rate, self.channels = int(d["rate"]), max(1, min(8, int(d["channels"])))
                        self.info.emit(d)
                    elif t == ord("S"):
                        self.status.emit(json.loads(payload))
                    elif t == ord("E"):
                        raise RuntimeError(payload.decode(errors="replace"))
            if not self._stop.is_set() and not self.got_audio:
                raise RuntimeError(stderr().strip()[-1500:] or "The helper ended without sending audio.")
        except Exception as e:  # noqa: BLE001
            if not self._stop.is_set():
                self.failed.emit(str(e) or repr(e))
        finally:
            if self._stopfn:
                try:
                    self._stopfn()
                except Exception:  # noqa: BLE001
                    pass
            self.set_listen(False)
            self.stop_wav()
            self.ended.emit()


# ---------------------------------------------------------------------------------------------- drawing
def db(x: float) -> float:
    return 20 * math.log10(x) if x > 1e-6 else -120.0


def fft(x: list[complex]) -> list[complex]:
    n = len(x)
    if n <= 1:
        return x
    even, odd = fft(x[0::2]), fft(x[1::2])
    t = [cmath.exp(-2j * math.pi * k / n) * odd[k] for k in range(n // 2)]
    return [even[k] + t[k] for k in range(n // 2)] + [even[k] - t[k] for k in range(n // 2)]


class Scope(QWidget):
    """Level meters (left), waveform (middle) and spectrum (bottom)."""

    def __init__(self, theme):
        super().__init__()
        self.theme = theme
        self.text = "Pick a microphone and press Start."
        self.levels: list[tuple[float, float]] = []
        self.hold: list[tuple[float, float]] = []     # (peak dB, time)
        self.clip: list[bool] = []
        self.history: list = []
        self.spectrum: list[float] = []
        self.rate = 48000
        self.setMinimumSize(420, 300)

    def sizeHint(self):
        return QSize(760, 520)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#101418"))
        if not self.levels:
            p.setPen(QColor(self.theme.TEXT_DIM))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.text)
            return
        f = QFont(self.font())
        f.setPointSizeF(8)
        p.setFont(f)
        W, H = self.width(), self.height()
        ch = len(self.levels)
        meter_w = min(46, max(18, 160 // ch))
        mx = 34 + ch * (meter_w + 6)
        top, bottom = 12, int(H * 0.62)
        # dB scale
        p.setPen(QColor("#5b6670"))
        for d in (0, -6, -12, -20, -30, -40, -50, -60):
            y = top + (bottom - top) * (-d / 60)
            p.drawText(QRectF(0, y - 7, 28, 14), Qt.AlignmentFlag.AlignRight, str(d))
        for c, (pk, rms) in enumerate(self.levels):
            x = 34 + c * (meter_w + 6)
            p.fillRect(QRectF(x, top, meter_w, bottom - top), QColor("#1d242b"))
            for val, col in ((db(rms), "#2fa36b"), (db(pk), "#7fd3a5")):
                frac = max(0.0, min(1.0, (val + 60) / 60))
                h = (bottom - top) * frac
                if col == "#7fd3a5":
                    p.fillRect(QRectF(x + meter_w * 0.7, bottom - h, meter_w * 0.3, h), QColor(col))
                else:
                    color = "#e0b030" if val > -6 else col
                    p.fillRect(QRectF(x, bottom - h, meter_w * 0.7, h), QColor(color))
            hd = self.hold[c][0] if c < len(self.hold) else -120
            y = top + (bottom - top) * min(1.0, -hd / 60) if hd > -60 else bottom
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.drawLine(int(x), int(y), int(x + meter_w), int(y))
            if c < len(self.clip) and self.clip[c]:
                p.fillRect(QRectF(x, top - 10, meter_w, 7), QColor("#e5484d"))
            p.setPen(QColor("#c9d1d9"))
            p.drawText(QRectF(x, bottom + 2, meter_w, 14), Qt.AlignmentFlag.AlignCenter, f"ch{c + 1}" if ch > 1 else "")
            p.drawText(QRectF(x - 4, bottom + 14, meter_w + 8, 14), Qt.AlignmentFlag.AlignCenter,
                       f"{max(-99, round(hd))}" if hd > -120 else "–")
        # waveform: one lane per channel
        wx, ww = mx + 10, W - mx - 20
        lane = (bottom - top) / ch
        hist = self.history[-int(ww):] if ww > 0 else []
        for c in range(ch):
            y0 = top + lane * c + lane / 2
            p.fillRect(QRectF(wx, top + lane * c + 1, ww, lane - 2), QColor("#161c22"))
            p.setPen(QColor("#2a333c"))
            p.drawLine(int(wx), int(y0), int(wx + ww), int(y0))
            p.setPen(QColor("#4aa3df"))
            off = ww - len(hist)
            for i, col in enumerate(hist):
                if c < len(col):
                    lo, hi = col[c]
                    xx = int(wx + off + i)
                    p.drawLine(xx, int(y0 - hi * lane / 2), xx, int(y0 - lo * lane / 2))
        # spectrum
        st, sb = bottom + 36, H - 18
        p.fillRect(QRectF(34, st, W - 44, sb - st), QColor("#161c22"))
        if self.spectrum:
            n = len(self.spectrum)
            bw = (W - 44) / n
            for i, v in enumerate(self.spectrum):
                frac = max(0.0, min(1.0, (v + 90) / 90))
                h = (sb - st) * frac
                p.fillRect(QRectF(34 + i * bw + 1, sb - h, max(1.0, bw - 2), h), QColor("#8a6fd1"))
        p.setPen(QColor("#5b6670"))
        for fr in (100, 300, 1000, 3000, 10000):
            if fr < self.rate / 2:
                x = 34 + (W - 44) * math.log(fr / 50) / math.log((self.rate / 2) / 50)
                p.drawText(QRectF(x - 20, sb + 1, 40, 14), Qt.AlignmentFlag.AlignCenter,
                           f"{fr // 1000}k" if fr >= 1000 else str(fr))
        p.drawText(QRectF(34, st - 16, 200, 14), Qt.AlignmentFlag.AlignLeft, "Spectrum")


# ---------------------------------------------------------------------------------------------- window
class MicDialog(QDialog):
    def __init__(self, api):
        super().__init__(api.window)
        self.api = api
        self.setWindowTitle(f"Microphones – {api.device_name}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1200, 720)
        self.dev = Device(api)
        self.data: dict = {}
        self.used_root = False
        self.streamer: Streamer | None = None
        self.token = ""
        self.stream_info: dict = {}
        self.mic_items: list[tuple[QTreeWidgetItem, dict]] = []
        self.rec_path = ""

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Run as"))
        self.mode = QComboBox()
        for k, label in MODES:
            self.mode.addItem(label, k)
        self.mode.setCurrentIndex([m[0] for m in MODES].index(api.get_setting("mode", "auto")))
        self.mode.currentIndexChanged.connect(lambda _i: api.set_setting("mode", self.mode.currentData()))
        top.addWidget(self.mode)
        self.b_scan = QPushButton(api.icon("refresh"), " Detect microphones")
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
        self.tree.setHeaderLabels(["Microphone", "Where", "Details"])
        self.tree.setColumnWidth(0, 200)
        self.tree.setColumnWidth(1, 110)
        self.tree.currentItemChanged.connect(lambda *_: self.on_select())
        self.tree.itemDoubleClicked.connect(lambda it, _c: self.start() if it.data(0, Qt.ItemDataRole.UserRole) else None)
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
        self.scope = Scope(api.theme)
        rl.addWidget(self.scope, 1)
        self.live = QLabel("")
        self.live.setWordWrap(True)
        self.live.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        rl.addWidget(self.live)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Source"))
        self.source = QComboBox()
        for v, label in SOURCES:
            self.source.addItem(label, v)
        row1.addWidget(self.source, 1)
        row1.addWidget(QLabel("Rate"))
        self.rate = QComboBox()
        row1.addWidget(self.rate)
        row1.addWidget(QLabel("Channels"))
        self.chans = QComboBox()
        row1.addWidget(self.chans)
        rl.addLayout(row1)
        row2 = QHBoxLayout()
        self.b_start = QPushButton("Start")
        self.b_start.clicked.connect(self.toggle)
        row2.addWidget(self.b_start)
        self.listen = QCheckBox("Listen on PC")
        self.listen.setEnabled(sys.platform == "win32")
        self.listen.toggled.connect(self.on_listen)
        row2.addWidget(self.listen)
        self.listen_ch = QComboBox()
        self.listen_ch.currentIndexChanged.connect(self.on_listen_ch)
        row2.addWidget(self.listen_ch)
        row2.addWidget(QLabel("Boost"))
        self.gain = QSlider(Qt.Orientation.Horizontal)
        self.gain.setRange(0, 24)
        self.gain.setValue(int(api.get_setting("boost_db", 0)))
        self.gain.setFixedWidth(90)
        self.gain.valueChanged.connect(self.on_gain)
        row2.addWidget(self.gain)
        self.gain_lbl = QLabel("")
        row2.addWidget(self.gain_lbl)
        self.b_rec = QPushButton(api.icon("download"), " Record WAV")
        self.b_rec.setCheckable(True)
        self.b_rec.setEnabled(False)
        self.b_rec.toggled.connect(self.on_record)
        row2.addWidget(self.b_rec)
        row2.addStretch(1)
        self.stats = QLabel("")
        self.stats.setStyleSheet(f"color: {api.theme.TEXT_DIM};")
        row2.addWidget(self.stats)
        rl.addLayout(row2)
        split.addWidget(right)
        split.setSizes([430, 770])
        lay.addWidget(split, 1)

        self.on_gain(self.gain.value())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(50)
        self.t_stats = time.monotonic()
        self.bytes_mark = 0
        self.spec_t = 0.0
        self.scan()

    # ---- detection
    def scan(self):
        self.stop()
        self.b_scan.setEnabled(False)
        self.summary.setText("Detecting microphones…")
        self.tree.clear()
        mode = self.mode.currentData()
        root_ok = self.api.is_root() or bool(getattr(self.api.conn, "native_root", False))

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
                QMessageBox.warning(self, "Microphones", f"Could not list the microphones:\n\n{msg}")
            except RuntimeError:
                pass

        self.api.task(lambda: detect(self.dev, mode, root_ok), done, fail)

    def fill(self, data: dict, used_root: bool):
        self.data, self.used_root = data, used_root
        devices = data.get("devices", [])
        mics = [m for m in data.get("microphones", []) if m.get("type") not in NON_MIC_TYPES]
        dev_by_key = {(d["type"], d.get("address", "")): d for d in devices}
        self.mic_items = []
        dim = QColor(self.api.theme.TEXT_FAINT)

        def group(title):
            g = QTreeWidgetItem([title])
            f = g.font(0)
            f.setBold(True)
            g.setFont(0, f)
            g.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(g)
            g.setExpanded(True)
            return g

        # 1) physical capsules
        if mics:
            g = group(f"Physical microphones ({len(mics)})")
            for m in mics:
                d = dev_by_key.get((m["type"], m.get("address", "")))
                bits = [m.get("directionality", "")]
                if m.get("group", -1) >= 0:
                    bits.append(f"group {m['group']}#{m.get('index_in_group')}")
                if "sensitivity_dbfs" in m:
                    bits.append(f"{m['sensitivity_dbfs']:.0f} dBFS/Pa")
                where = m.get("address") or m.get("location", "")
                it = QTreeWidgetItem(g, [m.get("description") or m.get("type_name"), where, ", ".join(b for b in bits if b)])
                target = {"kind": "mic", "mic": m, "device": d or self.fallback_device(devices, m)}
                it.setData(0, Qt.ItemDataRole.UserRole, target)
                self.mic_items.append((it, m))
        # 2) input devices
        g = group(f"Input devices ({len(devices)})")
        for d in devices:
            tag = d.get("type_name", "")
            extra = [f"id {d['id']}", "ch " + "/".join(map(str, d.get("channel_counts", []))) if d.get("channel_counts") else ""]
            if d.get("index_channels"):
                extra.append("index " + "/".join(map(str, d["index_channels"])))
            it = QTreeWidgetItem(g, [tag, d.get("address", "") or "—", ", ".join(e for e in extra if e)])
            it.setData(0, Qt.ItemDataRole.UserRole, {"kind": "device", "device": d})
            if not d.get("is_mic"):
                for c in range(3):
                    it.setForeground(c, dim)
                it.setToolTip(0, "Not a microphone (system/virtual input); it can still be streamed.")
        # 3) HAL-declared inputs that are not attached
        hal = data.get("hal", [])
        attached = {(d["type"], d.get("address", "")) for d in devices}
        attached_types = {d["type"] for d in devices}
        policy_knows = any(p.get("attached") for p in hal)
        missing = []
        for p in hal:
            if policy_knows:                    # the audio policy says exactly which ports are connected
                if p.get("attached"):
                    continue
            else:                               # older dumpsys: compare with the framework's devices
                t = ENUM_TYPE.get(p["enum"])
                if p["enum"] == "BACK_MIC":     # reported as a built-in mic with a "back" address
                    if any(d["type"] == 15 and "back" in d.get("address", "") for d in devices):
                        continue
                elif t is not None and ((t, p["address"]) in attached or (not p["address"] and t in attached_types)):
                    continue
            missing.append(p)
        if missing:
            g = group(f"Declared by the audio HAL, not attached ({len(missing)})")
            for p in sorted(missing, key=lambda p: (p["enum"] not in MIC_ENUMS, p["enum"])):
                builtin = p["enum"] in ("BUILTIN_MIC", "BACK_MIC")
                note = "built-in but not exposed – may be hidden or unused" if builtin else \
                    ("appears when plugged in / paired" if p["enum"] in MIC_ENUMS else "system input")
                it = QTreeWidgetItem(g, [", ".join(p["names"]) or p["enum"].replace("_", " ").title(),
                                         p["address"] or "—", note])
                it.setToolTip(0, f"AUDIO_DEVICE_IN_{p['enum']} · from {', '.join(p['origins'])}")
                if not builtin:
                    for c in range(3):
                        it.setForeground(c, dim)
        n_mics = len(mics) or sum(1 for d in devices if d.get("is_mic"))
        txt = f"{data.get('model', '')} · Android API {data.get('sdk')} · {n_mics} microphone(s)"
        txt += " · as root" if used_root else " · as shell user"
        if data.get("devices_error"):
            txt += f" · {data['devices_error']}"
        self.summary.setText(txt)
        first = self.mic_items[0][0] if self.mic_items else None
        if first is None:
            for i in range(self.tree.topLevelItemCount()):
                g = self.tree.topLevelItem(i)
                if g.childCount():
                    first = g.child(0)
                    break
        if first:
            self.tree.setCurrentItem(first)

    @staticmethod
    def fallback_device(devices, mic):
        """A capsule without its own input device is reached through the built-in mic (other sources/channels)."""
        for d in devices:
            if d["type"] == mic.get("type"):
                return d
        return None

    def target(self) -> dict | None:
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def on_select(self):
        t = self.target()
        self.rate.clear()
        self.chans.clear()
        if not t:
            self.details.setText("")
            return
        d = t.get("device") or {}
        rates = sorted(set(d.get("sample_rates") or [48000, 44100, 16000]), reverse=True)
        for r in rates:
            self.rate.addItem(f"{r} Hz", r)
        self.rate.setCurrentIndex(rates.index(48000) if 48000 in rates else 0)
        counts = sorted(set(d.get("channel_counts") or [1, 2]))
        idx = sorted(set(d.get("index_channels") or []))
        n_builtin = sum(1 for m in self.data.get("microphones", []) if m.get("type") == 15)
        opts = [(c, {1: "Mono", 2: "Stereo"}.get(c, f"{c} ch")) for c in counts if c <= 2]
        for c in idx + ([n_builtin] if n_builtin > 2 and d.get("type") == 15 else []):
            if c > 2 and all(c != o[0] for o in opts):
                opts.append((c, f"{c} ch (one per capsule)"))
        if not any(o[0] == 2 for o in opts) and d.get("type") == 15:
            opts.append((2, "Stereo"))
        for c, label in opts:
            self.chans.addItem(label, c)
        # stereo on a built-in mic usually records two different capsules – best for testing
        self.chans.setCurrentIndex(next((i for i, o in enumerate(opts) if o[0] == 2), 0))
        lines = []
        if t["kind"] == "mic":
            m = t["mic"]
            lines.append(f"<b>{m.get('description')}</b> – {m.get('type_name')}"
                         f"{' @' + m['address'] if m.get('address') else ''}, {m.get('location')}")
            for k, label in (("position_m", "position (m)"), ("orientation", "facing"),
                             ("directionality", "pattern"), ("sensitivity_dbfs", "sensitivity dBFS/Pa"),
                             ("max_spl", "max SPL dB"), ("channel_mapping", "channel map")):
                if k in m:
                    v = m[k]
                    lines.append(f"{label}: {', '.join(v) if isinstance(v, list) else v}")
            if not t.get("device"):
                lines.append("<b>No input device for this capsule – it can't be selected directly.</b>")
            elif (d.get("type"), d.get("address", "")) != (m.get("type"), m.get("address", "")):
                lines.append("Reached through the built-in mic input: try the sources (CAMCORDER often uses the back "
                             "capsule) and stereo/multi-channel; the live line under the graph shows which capsules "
                             "Android really opened.")
        if d:
            lines.append(f"Input device {d.get('id')}: {d.get('type_name')} '{d.get('name')}'"
                         f"{' @' + d['address'] if d.get('address') else ''} · rates "
                         f"{', '.join(map(str, d.get('sample_rates', [])))}")
        self.details.setText("<br>".join(lines))

    # ---- streaming
    def toggle(self):
        self.stop() if self.streamer else self.start()

    def start(self, root: bool | None = None):
        t = self.target()
        if not t or not t.get("device") or self.rate.currentData() is None:
            return
        prev = self.token if self.streamer else ""
        self.stop()
        if root is None:
            root = self.used_root or self.mode.currentData() == "root"
        d = t["device"]
        self.token = f"t{os.getpid()}x{int(time.time() * 1000) % 10**9}"
        args = f"stream {d['id']} {self.source.currentData()} {self.rate.currentData()} {self.chans.currentData()} {self.token}"
        s = Streamer(self.dev, args, root, prev)
        s.gain = 10 ** (self.gain.value() / 20)
        s.info.connect(self.on_info)
        s.status.connect(self.on_status)
        s.failed.connect(lambda msg, s=s, root=root: self.on_failed(s, msg, root))
        s.ended.connect(lambda s=s: self.on_ended(s))
        self.streamer = s
        self.scope.levels = []
        self.scope.text = "Opening the microphone…"
        self.scope.update()
        self.live.setText("")
        self.b_start.setText("Stop")
        s.start()

    def stop(self):
        s, self.streamer = self.streamer, None
        if s:
            s.stop()
            dev, token = self.dev, self.token
            threading.Thread(target=lambda: (time.sleep(1.5), dev.kill_leftovers(token)), daemon=True).start()
        self.b_start.setText("Start")
        if self.b_rec.isChecked():
            self.b_rec.setChecked(False)
        self.b_rec.setEnabled(False)
        self.stats.setText("")
        for it, _m in self.mic_items:
            it.setIcon(0, QIcon())

    def on_info(self, info: dict):
        s = self.streamer
        self.stream_info = info
        self.scope.rate = int(info.get("rate", 48000))
        self.scope.hold = [(-120.0, 0.0)] * int(info.get("channels", 1))
        self.listen_ch.blockSignals(True)
        self.listen_ch.clear()
        n = int(info.get("channels", 1))
        if n <= 2:
            self.listen_ch.addItem("Mono" if n == 1 else "Both channels", -1)
        for c in range(n if n > 1 else 0):
            self.listen_ch.addItem(f"Channel {c + 1} only", c)
        self.listen_ch.blockSignals(False)
        self.on_listen_ch()
        self.b_rec.setEnabled(True)
        if s and self.listen.isChecked():
            self.on_listen(True)

    def on_status(self, st: dict):
        parts = []
        if st.get("routed"):
            parts.append(f"Routed to <b>{st['routed']}</b>")
        act = st.get("active")
        if act is not None:
            names = [f"{m.get('description')} ({m.get('address') or m.get('location')})" for m in act]
            parts.append("Capsules in use: <b>" + (", ".join(names) if names else "not reported") + "</b>")
            keys = {(m.get("type"), m.get("address"), m.get("mic_id")) for m in act}
            for it, m in self.mic_items:
                on = (m.get("type"), m.get("address"), m.get("mic_id")) in keys
                it.setIcon(0, self.api.icon("antenna") if on else QIcon())
                f = it.font(0)
                f.setBold(on)
                it.setFont(0, f)
        if st.get("silenced"):
            parts.append("<span style='color:#e5484d'><b>Android is silencing this recording</b> (privacy toggle, "
                         "another app has priority, or background restriction)</span>")
        self.live.setText(" · ".join(parts))

    def on_failed(self, s, msg: str, root: bool):
        if self.streamer is not s:
            return
        can_root = self.api.is_root() or bool(getattr(self.api.conn, "native_root", False))
        if not root and self.mode.currentData() == "auto" and can_root:
            self.streamer = None
            self.scope.text = "Shell user was refused – retrying as root…"
            self.scope.update()
            QTimer.singleShot(300, lambda: self.start(root=True))
            return
        try:
            self.scope.levels = []
            hint = "" if root else "\n\nIf the shell user is refused, try “Run as: Root”."
            self.scope.text = f"Microphone stream failed:\n{msg}{hint}"
            self.scope.update()
        except RuntimeError:
            pass

    def on_ended(self, s):
        if self.streamer is s:
            self.streamer = None
            try:
                self.b_start.setText("Start")
                self.b_rec.setChecked(False)
                self.b_rec.setEnabled(False)
            except RuntimeError:
                pass

    def on_listen(self, on: bool):
        s = self.streamer
        if s and s.rate:
            try:
                s.set_listen(on)
            except Exception as e:  # noqa: BLE001
                self.api.message(f"Listen: {e}", error=True)

    def on_listen_ch(self, *_):
        s = self.streamer
        if s and self.listen_ch.currentData() is not None:
            s.listen_channel = int(self.listen_ch.currentData())
            if self.listen.isChecked():
                self.on_listen(True)

    def on_gain(self, v: int):
        self.api.set_setting("boost_db", v)
        self.gain_lbl.setText(f"+{v} dB")
        if self.streamer:
            self.streamer.gain = 10 ** (v / 20)

    def on_record(self, on: bool):
        s = self.streamer
        if not s:
            return
        if on:
            folder = os.path.join(os.path.expanduser("~"), "Music", "AndroUnderTheHood")
            os.makedirs(folder, exist_ok=True)
            self.rec_path = os.path.join(folder, f"mic_{time.strftime('%Y%m%d_%H%M%S')}.wav")
            s.start_wav(self.rec_path)
            self.b_rec.setText(" Stop recording")
        else:
            s.stop_wav()
            self.b_rec.setText(" Record WAV")
            if self.rec_path:
                self.api.message(f"Saved {self.rec_path}")

    def tick(self):
        s = self.streamer
        if not s or not s.got_audio:
            return
        now = time.monotonic()
        lv = s.take_levels()
        if not lv:
            return
        hold = list(self.scope.hold) + [(-120.0, 0.0)] * (len(lv) - len(self.scope.hold))
        for c, (pk, _r) in enumerate(lv):
            d = db(pk)
            if d >= hold[c][0] or now - hold[c][1] > 1.5:
                hold[c] = (d, now)
        self.scope.hold = hold[:len(lv)]
        self.scope.levels = lv
        self.scope.clip = [now - s.clip[c] < 1.0 for c in range(len(lv))]
        with s.lock:
            self.scope.history = list(s.history)
            tail = list(s.tail)
            total = s.total
        if now - self.spec_t > 0.12 and tail:
            self.spec_t = now
            self.scope.spectrum = self.spectrum(tail, s.channels, s.rate)
        self.scope.update()
        if now - self.t_stats >= 1:
            rate = (total - self.bytes_mark) / (now - self.t_stats)
            self.bytes_mark, self.t_stats = total, now
            ch = s.channels
            self.stats.setText(f"{s.rate} Hz · {ch} ch · {rate / 1000:.0f} kB/s"
                               f"{' · ● REC' if self.b_rec.isChecked() else ''}")

    def spectrum(self, tail, ch: int, rate: int, n: int = 1024, bands: int = 48) -> list[float]:
        c = max(0, self.listen_ch.currentData() or 0) if ch > 1 else 0
        samples = []
        for a in reversed(tail):
            samples = list(a[c::ch]) + samples
            if len(samples) >= n:
                break
        if len(samples) < n:
            return []
        x = samples[-n:]
        win = [0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1)) for i in range(n)]
        spec = fft([complex(x[i] * win[i] / 32768) for i in range(n)])
        mags = [abs(v) / (n / 4) for v in spec[:n // 2]]
        out = []
        lo_f, hi_f = 50, rate / 2
        for b in range(bands):
            f0 = lo_f * (hi_f / lo_f) ** (b / bands)
            f1 = lo_f * (hi_f / lo_f) ** ((b + 1) / bands)
            i0 = max(1, int(f0 * n / rate))
            i1 = max(i0 + 1, int(f1 * n / rate))
            out.append(db(max(mags[i0:i1])))
        return out

    def closeEvent(self, e):
        self.timer.stop()
        self.stop()
        super().closeEvent(e)
