"""Qualcomm DIAG protocol over AndroUnderTheHood's connection (no Qt).

Transport: the phone's own /vendor/bin/diag_socket_log (Qualcomm "diag over socket") is
pointed at a local listener whose bytes are relayed to the PC:
  * SSH: `nc -l` on the phone, attached to an SSH exec channel;
  * ADB: `adb reverse` to a socket AndroUnderTheHood listens on.
The stream is standard HDLC-framed DIAG (CRC-16/X.25, 0x7E terminators), the same as a
QXDM USB/COM connection.
"""
from __future__ import annotations

import datetime as dt
import errno as _errno
import logging
import random
import re
import socket
import struct
import subprocess
import threading
import time

from . import backend as B

log = logging.getLogger("androunderthehood")
q = B.shq
DSL = "/vendor/bin/diag_socket_log"

# --- command codes -----------------------------------------------------------
VERNO = 0x00
LOG = 0x10
BAD_CMD, BAD_PARM, BAD_LEN, BAD_MODE, BAD_SPC = 0x13, 0x14, 0x15, 0x18, 0x47
NV_READ, NV_WRITE = 0x26, 0x27
SUBSYS = 0x4B
EVENT_REPORT = 0x60
LOG_CONFIG = 0x73
EXT_MSG = 0x79
EXT_BUILD_ID = 0x7C
EXT_MSG_CONFIG = 0x7D
EVENT_MASK_GET, EVENT_MASK_SET = 0x81, 0x82
QSR_EXT_MSG_TERSE = 0x92
QSR4_EXT_MSG_TERSE = 0x99
MULTI_SIM = 0x98
ERRORS = {BAD_CMD: "command not supported", BAD_PARM: "bad parameters", BAD_LEN: "bad length",
          BAD_MODE: "not allowed in this mode", BAD_SPC: "locked (SPC/security)"}
STREAM = {LOG, EXT_MSG, EVENT_REPORT, QSR_EXT_MSG_TERSE, QSR4_EXT_MSG_TERSE, 0x9C}

EFS_SUBSYS = 0x13
EFS = dict(HELLO=0, QUERY=1, OPEN=2, CLOSE=3, READ=4, WRITE=5, UNLINK=8, MKDIR=9, RMDIR=10, OPENDIR=11,
           READDIR=12, CLOSEDIR=13, RENAME=14, STAT=15, LSTAT=16, STATFS=19)
O_RDONLY, O_WRONLY, O_CREAT, O_TRUNC = 0, 1, 0o100, 0o1000

NV_STATUS = {0: "ok", 1: "busy", 2: "bad command", 3: "memory full", 4: "failed", 5: "not set (inactive)",
             6: "bad parameter / no such item", 7: "read-only", 8: "bad target", 9: "no memory",
             10: "not allocated"}

# NV items / EFS paths that hold device identity: never written by this tool
PROTECTED_NV = {0: "ESN", 550: "IMEI", 1943: "MEID"}
PROTECTED_EFS = re.compile(r"(?i)imei|meid|/esn|esn_|sec_|/sfs/|secure")

NV_NAMES = {0: "ESN", 10: "PREF_MODE", 441: "BAND_PREF", 550: "UE_IMEI", 946: "BAND_PREF_16_31",
            1877: "RTRE_CONFIG", 1943: "MEID", 6828: "LTE_BC_CONFIG"}

LOG_NAMES = {
    0xB0C0: "LTE RRC OTA Packet", 0xB0C1: "LTE RRC MIB Message", 0xB0C2: "LTE RRC Serving Cell Info",
    0xB0E2: "LTE NAS ESM Plain OTA Incoming", 0xB0E3: "LTE NAS ESM Plain OTA Outgoing",
    0xB0EC: "LTE NAS EMM Plain OTA Incoming", 0xB0ED: "LTE NAS EMM Plain OTA Outgoing",
    0xB193: "LTE ML1 Serving Cell Meas Response", 0xB821: "NR5G RRC OTA Packet",
    0x412F: "WCDMA RRC Signaling Message", 0x713A: "UMTS NAS OTA Message", 0x512F: "GSM RR Signaling Message",
}
# "Signalling only" preset for the live log
SIGNALLING_CODES = sorted(LOG_NAMES)
EQUIPMENT = {1: "CDMA 1X", 4: "WCDMA", 5: "GSM", 7: "UMTS NAS", 0xB: "LTE / NR5G"}
F3_LEVELS = [("Low and up", 0x1F), ("Medium and up", 0x1E), ("High and up", 0x1C), ("Error and fatal", 0x18),
             ("Fatal only", 0x10)]


class DiagError(Exception):
    pass


# --- HDLC ------------------------------------------------------------------------
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return (~crc) & 0xFFFF


def hdlc_encode(payload: bytes) -> bytes:
    c = crc16(payload)
    raw = payload + bytes((c & 0xFF, c >> 8))
    return raw.replace(b"\x7d", b"\x7d\x5d").replace(b"\x7e", b"\x7d\x5e") + b"\x7e"


def hdlc_decode(frame: bytes) -> bytes | None:
    raw = frame.replace(b"\x7d\x5e", b"\x7e").replace(b"\x7d\x5d", b"\x7d")
    if len(raw) < 3 or crc16(raw[:-2]) != (raw[-2] | raw[-1] << 8):
        return None
    return raw[:-2]


# --- time ------------------------------------------------------------------------
GPS_EPOCH = dt.datetime(1980, 1, 6)


def qc_time(ts: int) -> dt.datetime:
    """Qualcomm timestamp: upper 48 bits in 1.25 ms units since 1980-01-06."""
    try:
        return GPS_EPOCH + dt.timedelta(milliseconds=(ts >> 16) * 1.25 + (ts & 0xFFFF) / 49152 * 1.25)
    except OverflowError:
        return GPS_EPOCH


# --- printf for F3 messages ---------------------------------------------------
_FMT = re.compile(r"%([-+ #0]*)(\d+|\*)?(?:\.(\d+))?(hh|h|ll|l|z|j|t|L)?([diouxXcspfFeEgG%])")


def c_format(fmt: str, args: list[int]) -> str:
    it = iter(args)

    def nxt():
        return next(it, 0)

    def rep(m):
        flags, width, prec, length, conv = m.groups()
        if conv == "%":
            return "%"
        if width == "*":
            width = str(nxt())
        v = nxt()
        if length == "ll":
            v |= nxt() << 32
        spec = "{:" + ("<" if "-" in flags else "") + ("0" if "0" in flags and "-" not in flags else "") + \
               (width or "")
        try:
            if conv in "di":
                bits = 64 if length == "ll" else 32
                if v >= 1 << (bits - 1):
                    v -= 1 << bits
                return (spec + "d}").format(v)
            if conv == "u":
                return (spec + "d}").format(v)
            if conv in "xX":
                return ("0x" if "#" in flags else "") + (spec + conv + "}").format(v)
            if conv == "o":
                return (spec + "o}").format(v)
            if conv == "p":
                return f"0x{v:08x}"
            if conv == "c":
                return chr(v & 0xFF) if 32 <= (v & 0xFF) < 127 else "?"
            if conv == "s":
                return f"‹str@0x{v:x}›"
            return f"‹{v}›"
        except (ValueError, IndexError):
            return m.group(0)
    try:
        return _FMT.sub(rep, fmt)
    except Exception:  # noqa: BLE001
        return fmt + "  " + " ".join(hex(a) for a in args)


def cstr(b: bytes, start: int) -> tuple[str, int]:
    end = b.find(b"\0", start)
    if end < 0:
        end = len(b)
    return b[start:end].decode("latin-1"), end + 1


# --- parsed records -------------------------------------------------------------
def parse_stream(p: bytes, sub: int | None = None) -> list[dict]:
    """Turn one streaming frame into display records (sub = SIM subscription, if wrapped)."""
    recs = _parse_stream(p)
    if sub is not None:
        for r in recs:
            r["name"] = (f"SIM{sub} " + r["name"]).strip()
    return recs


def _parse_stream(p: bytes) -> list[dict]:
    c = p[0]
    try:
        if c == EXT_MSG and len(p) >= 20:
            num_args = p[2]
            ts, line, ssid, mask = struct.unpack_from("<QHHI", p, 4)
            args = list(struct.unpack_from(f"<{num_args}I", p, 20))
            fmt, i = cstr(p, 20 + 4 * num_args)
            fname, _ = cstr(p, i)
            return [dict(kind="F3", time=now(), id=ssid, level=level_name(mask),
                         name=f"{fname.rsplit('/', 1)[-1]}:{line}", text=c_format(fmt, args))]
        if c == QSR4_EXT_MSG_TERSE and len(p) >= 18:
            # cmd, ts_type, (arg size << 4 | arg count), drop, ts u64, hash u32, u16, args
            size = (p[2] >> 4) or 4
            n = min(p[2] & 0xF, (len(p) - 18) // size)
            hsh = struct.unpack_from("<I", p, 12)[0] | struct.unpack_from("<H", p, 16)[0] << 32
            args = struct.unpack_from("<" + ("H" if size == 2 else "I") * n, p, 18)
            return [dict(kind="F3", time=now(), id="", level="", name="",
                         text=f"[hashed message {hsh:012x}] " + " ".join(f"0x{a:x}" for a in args))]
        if c == QSR_EXT_MSG_TERSE and len(p) >= 24:
            num_args = p[2]
            ts, line, ssid, mask, hsh = struct.unpack_from("<QHHII", p, 4)
            nargs = min(num_args, (len(p) - 24) // 4)
            args = struct.unpack_from(f"<{nargs}I", p, 24)
            return [dict(kind="F3", time=now(), id=ssid, level=level_name(mask), name=f"line {line}",
                         text=f"[hashed message 0x{hsh:08x}] " + " ".join(f"0x{a:x}" for a in args))]
        if c == LOG and len(p) >= 16:
            ln, code, ts = struct.unpack_from("<HHQ", p, 4)
            body = p[16:]
            return [dict(kind="LOG", time=now(), id=code, level="", name=LOG_NAMES.get(code, ""),
                         text=body[:64].hex(" ") + (" …" if len(body) > 64 else ""), raw=body)]
        if c == EVENT_REPORT and len(p) >= 3:
            return parse_events(p)
    except (struct.error, IndexError):
        pass
    return [dict(kind=f"0x{c:02X}", time=dt.datetime.now(), id=c, level="", name="", text=p[:64].hex(" "))]


def now() -> dt.datetime:
    """Records are stamped with the PC's arrival time (modem clocks vary by firmware)."""
    return dt.datetime.now()


def level_name(mask: int) -> str:
    for bit, name in ((0x10, "FATAL"), (0x8, "ERROR"), (0x4, "HIGH"), (0x2, "MED"), (0x1, "LOW")):
        if mask & bit:
            return name
    return ""


def parse_events(p: bytes) -> list[dict]:
    out = []
    length = struct.unpack_from("<H", p, 1)[0]
    i, end = 3, min(len(p), 3 + length)
    last_ts = 0
    while i + 2 <= end:
        idf = struct.unpack_from("<H", p, i)[0]
        i += 2
        eid = idf & 0xFFF
        plen_code = (idf >> 13) & 3
        if idf & 0x8000:
            tlo = struct.unpack_from("<H", p, i)[0]
            i += 2
            ts = (last_ts & ~(0xFFFF << 16)) | (tlo << 16)
        else:
            ts = struct.unpack_from("<Q", p, i)[0]
            i += 8
            last_ts = ts
        if plen_code == 0:
            data = b""
        elif plen_code == 1:
            data = p[i:i + 1]
            i += 1
        elif plen_code == 2:
            data = p[i:i + 2]
            i += 2
        else:
            n = p[i]
            data = p[i + 1:i + 1 + n]
            i += 1 + n
        out.append(dict(kind="EVENT", time=now(), id=eid, level="", name="", text=data.hex(" ")))
    return out


# --- the link -----------------------------------------------------------------------
class DiagLink:
    def __init__(self, conn: B.Connection):
        self.conn = conn
        self.buf = b""
        self.lock = threading.Lock()
        self.wlock = threading.Lock()
        self.req_lock = threading.RLock()   # one request/answer exchange at a time
        self.collectors: list[list] = []          # [predicate, list, event, max]
        self.listeners: list = []                 # fn(payload) for streaming frames
        self.alive = False
        self._chan = None
        self._proc = None
        self._sock = None
        self._srv = None
        self._rport = None
        self.version: dict = {}
        self.unsolicited = 0

    # ---- start / stop ------------------------------------------------------------
    def start(self, timeout: float = 15):
        port = random.randint(25000, 26999)
        kill = f"{DSL} -k >/dev/null 2>&1"
        if getattr(self.conn, "is_adb", False):
            self._start_adb(port, kill, timeout)
        else:
            cmd = (f"{kill}; (sleep 1; {DSL} -a 127.0.0.1 -p {port} -r 15 >/dev/null 2>&1 &); "
                   f"exec nc -l -p {port}")
            ch = self.conn.open_channel()
            ch.exec_command(self.conn.wrap_root(cmd))
            self._chan = ch
            threading.Thread(target=self._read_ssh, daemon=True, name="diag-rx").start()
        self.alive = True
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                r = self.request(bytes([VERNO]), timeout=1.5)
                self.version = parse_version(r)
                return self.version
            except DiagError:
                if not self.alive:
                    break
        self.close()
        raise DiagError("The modem did not answer. Is root on, and does this phone's diag_socket_log work?")

    def _start_adb(self, port, kill, timeout):
        from . import adbconn
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        srv.settimeout(timeout)
        self._srv = srv
        lport = srv.getsockname()[1]
        serial = self.conn.serial
        r = adbconn.adb(["-s", serial, "reverse", f"tcp:{port}", f"tcp:{lport}"], timeout=20)
        if r.returncode != 0:
            raise DiagError("adb reverse failed: " + (r.stdout + r.stderr).decode(errors="replace"))
        self._rport = port
        cmd = f"{kill}; exec {DSL} -a 127.0.0.1 -p {port} -r 15"
        self._proc = subprocess.Popen([self.conn.exe, "-s", serial, "shell", "-T", self.conn.wrap_root(cmd)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                      creationflags=0x08000000)
        try:
            s, _ = srv.accept()
        except socket.timeout as e:
            raise DiagError("diag_socket_log did not connect back over adb reverse") from e
        s.settimeout(None)
        self._sock = s
        threading.Thread(target=self._read_sock, daemon=True, name="diag-rx").start()

    def close(self):
        if not self.alive and not self._chan and not self._sock:
            return
        self.alive = False
        try:
            self.quiet()
        except Exception:  # noqa: BLE001
            pass
        for x in (self._chan, self._sock, self._srv):
            try:
                x and x.close()
            except Exception:  # noqa: BLE001
                pass
        if self._proc:
            self._proc.kill()
        try:
            self.conn.exec(f"{DSL} -k >/dev/null 2>&1", timeout=20)
        except Exception:  # noqa: BLE001
            pass
        if self._rport:
            from . import adbconn
            adbconn.adb(["-s", self.conn.serial, "reverse", "--remove", f"tcp:{self._rport}"], timeout=10)
        self._chan = self._sock = self._srv = self._proc = None
        for c in self.collectors:
            c[2].set()

    # ---- I/O -------------------------------------------------------------------------
    def _read_ssh(self):
        try:
            while True:
                d = self._chan.recv(65536)
                if not d:
                    break
                self._feed(d)
        except Exception as e:  # noqa: BLE001
            log.info("diag link closed: %r", e)
        self.alive = False
        for c in self.collectors:
            c[2].set()

    def _read_sock(self):
        try:
            while True:
                d = self._sock.recv(65536)
                if not d:
                    break
                self._feed(d)
        except Exception as e:  # noqa: BLE001
            log.info("diag link closed: %r", e)
        self.alive = False
        for c in self.collectors:
            c[2].set()

    def _feed(self, data: bytes):
        self.buf += data
        while True:
            i = self.buf.find(b"\x7e")
            if i < 0:
                break
            frame, self.buf = self.buf[:i], self.buf[i + 1:]
            if not frame:
                continue
            p = hdlc_decode(frame)
            if p:
                self._dispatch(p)

    def _dispatch(self, p: bytes):
        if p[0] == MULTI_SIM and len(p) > 8:
            # multi-SIM wrapper: cmd, version, 2 reserved, u32 subscription id, inner packet
            inner = p[8:]
            if inner and inner[0] in STREAM:
                sub = struct.unpack_from("<I", p, 4)[0]
                for fn in list(self.listeners):
                    try:
                        fn(inner, sub)
                    except Exception:  # noqa: BLE001
                        log.exception("diag listener")
                return
        taken = False
        with self.lock:
            for c in self.collectors:
                if c[0](p):
                    c[1].append(p)
                    if len(c[1]) >= c[3]:
                        c[2].set()
                    taken = True
                    break
        if not taken and p[0] in STREAM:
            for fn in list(self.listeners):
                try:
                    fn(p, None)
                except Exception:  # noqa: BLE001
                    log.exception("diag listener")
        elif not taken:
            self.unsolicited += 1

    def send(self, payload: bytes):
        data = hdlc_encode(payload)
        with self.wlock:
            if self._chan:
                self._chan.sendall(data)
            elif self._sock:
                self._sock.sendall(data)
            else:
                raise DiagError("not connected")

    @staticmethod
    def default_match(req: bytes):
        def m(r: bytes) -> bool:
            if r[0] in ERRORS and len(r) > 1 and r[1] == req[0]:
                return True
            if r[0] != req[0]:
                return False
            if req[0] == SUBSYS:
                return r[1:4] == req[1:4]
            return True
        return m

    def request_many(self, payloads: list[bytes], match, timeout: float = 5) -> list[bytes]:
        if not self.alive:
            raise DiagError("not connected")
        ev = threading.Event()
        col = [match, [], ev, len(payloads)]
        with self.req_lock:
            with self.lock:
                self.collectors.append(col)
            try:
                self.send_raw(b"".join(hdlc_encode(p) for p in payloads))
                ev.wait(timeout)
            finally:
                with self.lock:
                    self.collectors.remove(col)
        return col[1]

    def send_raw(self, data: bytes):
        with self.wlock:
            if self._chan:
                self._chan.sendall(data)
            elif self._sock:
                self._sock.sendall(data)
            else:
                raise DiagError("not connected")

    def request(self, payload: bytes, timeout: float = 5, match=None) -> bytes:
        r = self.request_many([payload], match or self.default_match(payload), timeout)
        if not r:
            raise DiagError("no answer from the modem" if self.alive else "the DIAG connection closed")
        if r[0][0] in ERRORS and payload[0] not in ERRORS:
            raise DiagError(f"modem: {ERRORS[r[0][0]]}")
        return r[0]

    # ---- logging masks ------------------------------------------------------------
    def set_f3(self, mask: int):
        self.request(struct.pack("<BBHI", EXT_MSG_CONFIG, 5, 0, mask), timeout=3)

    def set_events(self, on: bool):
        if on:
            try:
                r = self.request(bytes([EVENT_MASK_GET]), timeout=3)
                nbits = struct.unpack_from("<H", r, 4)[0]
                self.request(struct.pack("<BBHH", EVENT_MASK_SET, 0, 0, nbits) + b"\xff" * ((nbits + 7) // 8),
                             timeout=3)
            except (DiagError, struct.error) as e:
                log.info("event mask: %s", e)
        self.request(bytes([EVENT_REPORT, 1 if on else 0]), timeout=3)

    def log_ranges(self) -> list[int]:
        r = self.request(struct.pack("<B3xI", LOG_CONFIG, 1), timeout=3)
        return list(struct.unpack_from("<16I", r, 12))

    def set_log_mask(self, equip: int, codes: list[int] | None, last_item: int):
        """codes None = every code of this equipment id; [] = none."""
        n = last_item + 1
        mask = bytearray((n + 7) // 8)
        for i in range(n):
            if codes is None or ((equip << 12) | i) in codes:
                mask[i // 8] |= 1 << (i % 8)
        self.request(struct.pack("<B3xIII", LOG_CONFIG, 3, equip, n) + bytes(mask), timeout=3)

    def clear_log_masks(self):
        """Explicit empty mask for every technology (the generic "disable" op is ignored by some modems)."""
        ranges = self.log_ranges()
        for eq, last in enumerate(ranges):
            if last:
                self.set_log_mask(eq, [], last)

    def quiet(self):
        """Switch all streaming off again. Every command waits for its reply: diag_socket_log
        takes everything it reads from its socket in one go as a single command."""
        if not self.alive:
            return
        for req in (struct.pack("<BBHI", EXT_MSG_CONFIG, 5, 0, 0), bytes([EVENT_REPORT, 0])):
            try:
                self.request(req, timeout=3)
            except DiagError as e:
                log.info("diag quiet %s: %s", req.hex(), e)
        try:
            self.clear_log_masks()
        except DiagError as e:
            log.info("diag clear log masks: %s", e)

    # ---- NV ---------------------------------------------------------------------------
    def nv_read_many(self, items: list[int], timeout: float = 3, progress=None,
                     cancel=lambda: False) -> dict[int, tuple[int, bytes]]:
        """One request at a time: diag_socket_log treats each read from its socket as one command."""
        out = {}
        for n, i in enumerate(items):
            if cancel() or not self.alive:
                break
            r = self._nv_read_batch([i], timeout) or self._nv_read_batch([i], timeout)   # one retry
            out.update(r)
            if progress and n % 10 == 0:
                progress(n, len(items))
        return out

    def _nv_read_batch(self, items: list[int], timeout: float) -> dict[int, tuple[int, bytes]]:
        want = set(items)

        def match(r):
            return (r[0] == NV_READ and len(r) >= 133 and struct.unpack_from("<H", r, 1)[0] in want) or \
                   (r[0] in ERRORS and len(r) > 3 and r[1] == NV_READ and struct.unpack_from("<H", r, 2)[0] in want)
        pay = [struct.pack("<BH", NV_READ, i) + b"\0" * 130 for i in items]
        out = {}
        for r in self.request_many(pay, match, timeout):
            if r[0] == NV_READ:
                item = struct.unpack_from("<H", r, 1)[0]
                status = struct.unpack_from("<H", r, 131)[0]
                out[item] = (status, r[3:131])
            else:
                out[struct.unpack_from("<H", r, 2)[0]] = (-r[0], b"")
        return out

    def nv_read(self, item: int) -> tuple[int, bytes]:
        r = self._nv_read_batch([item], 4)
        if item not in r:
            raise DiagError("no answer from the modem")
        return r[item]

    def nv_write(self, item: int, data: bytes) -> int:
        if item in PROTECTED_NV:
            raise DiagError(f"NV {item} ({PROTECTED_NV[item]}) is a device identity item – not writable here")
        data = data[:128].ljust(128, b"\0")
        r = self.request(struct.pack("<BH", NV_WRITE, item) + data + b"\0\0")
        return struct.unpack_from("<H", r, 131)[0]

    # ---- EFS ---------------------------------------------------------------------------
    def _efs(self, cmd: str, body: bytes = b"", timeout: float = 6) -> bytes:
        req = struct.pack("<BBH", SUBSYS, EFS_SUBSYS, EFS[cmd]) + body
        return self.request(req, timeout=timeout)[4:]

    @staticmethod
    def _err(e: int) -> str:
        return f"{_errno.errorcode.get(e, 'E?')} ({e})"

    def efs_list(self, path: str) -> list[dict]:
        r = self._efs("OPENDIR", path.encode() + b"\0")
        dirp, err = struct.unpack_from("<Ii", r)
        if err:
            raise DiagError(f"{path}: {self._err(err)}")
        out = []
        try:
            seq = 1
            while seq < 4000:
                r = self._efs("READDIR", struct.pack("<Ii", dirp, seq))
                _d, _s, err, etype, mode, size, atime, mtime, ctime = struct.unpack_from("<IiiiiiIII", r)
                name, _ = cstr(r, 36)
                if err or not name:
                    break
                if name not in (".", ".."):
                    out.append(dict(name=name, type={0: "file", 1: "dir", 2: "link", 3: "item", 15: "item"}.get(etype, str(etype)),
                                    mode=mode, size=size, mtime=mtime))
                seq += 1
        finally:
            self._efs("CLOSEDIR", struct.pack("<I", dirp))
        return out

    def efs_stat(self, path: str) -> dict:
        r = self._efs("STAT", path.encode() + b"\0")
        err, mode, size, nlink, atime, mtime, ctime = struct.unpack_from("<iiiiIII", r)
        if err:
            raise DiagError(f"{path}: {self._err(err)}")
        return dict(mode=mode, size=size, mtime=mtime)

    def efs_read(self, path: str, limit: int = 4 << 20) -> bytes:
        r = self._efs("OPEN", struct.pack("<ii", O_RDONLY, 0) + path.encode() + b"\0")
        fd, err = struct.unpack_from("<ii", r)
        if err or fd < 0:
            raise DiagError(f"{path}: {self._err(err)}")
        data = bytearray()
        try:
            while len(data) < limit:
                r = self._efs("READ", struct.pack("<iII", fd, 512, len(data)))
                _fd, _off, n, err = struct.unpack_from("<iIii", r)
                if err:
                    raise DiagError(f"{path}: read {self._err(err)}")
                if n <= 0:
                    break
                data += r[16:16 + n]
        finally:
            self._efs("CLOSE", struct.pack("<i", fd))
        return bytes(data)

    def efs_write(self, path: str, data: bytes):
        if PROTECTED_EFS.search(path):
            raise DiagError(f"{path} looks like device identity / security data – not writable here")
        r = self._efs("OPEN", struct.pack("<ii", O_WRONLY | O_CREAT | O_TRUNC, 0o777) + path.encode() + b"\0")
        fd, err = struct.unpack_from("<ii", r)
        if err or fd < 0:
            raise DiagError(f"{path}: {self._err(err)}")
        try:
            off = 0
            while off < len(data):
                chunk = data[off:off + 512]
                r = self._efs("WRITE", struct.pack("<iI", fd, off) + chunk)
                _fd, _off, n, err = struct.unpack_from("<iIii", r)
                if err or n <= 0:
                    raise DiagError(f"{path}: write {self._err(err)}")
                off += n
        finally:
            self._efs("CLOSE", struct.pack("<i", fd))

    def efs_unlink(self, path: str):
        if PROTECTED_EFS.search(path):
            raise DiagError(f"{path} looks like device identity / security data – not deletable here")
        err = struct.unpack_from("<i", self._efs("UNLINK", path.encode() + b"\0"))[0]
        if err:
            raise DiagError(f"{path}: {self._err(err)}")


def parse_version(r: bytes) -> dict:
    try:
        return dict(comp=f"{r[1:12].decode('latin-1')} {r[12:20].decode('latin-1')}",
                    rel=f"{r[20:31].decode('latin-1')} {r[31:39].decode('latin-1')}",
                    ver_dir=r[39:47].decode("latin-1").strip("\0 "), scm=r[47], cai_rev=r[48], model=r[49],
                    firm_rev=struct.unpack_from("<H", r, 50)[0])
    except (IndexError, struct.error):
        return {"raw": r.hex()}


def hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        lines.append(f"{i:06x}  {chunk.hex(' '):<{width * 3}}  " +
                     "".join(chr(c) if 32 <= c < 127 else "." for c in chunk))
    return "\n".join(lines)
