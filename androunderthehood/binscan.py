"""binwalk-style analysis: signatures, strings and entropy of a byte stream (Qt-free)."""
from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass


@dataclass
class Hit:
    offset: int
    kind: str
    desc: str


def _u16(b, o, e="<"):
    return struct.unpack_from(e + "H", b, o)[0]


def _u32(b, o, e="<"):
    return struct.unpack_from(e + "I", b, o)[0]


def _cstr(b: bytes, o: int, n: int) -> str:
    s = b[o:o + n].split(b"\0", 1)[0]
    return s.decode("latin-1", errors="replace").strip()


# ---- validators/describers: (buf, pos) -> description or None ----------------
def d_elf(b, p):
    if len(b) < p + 20:
        return None
    cls = {1: "32-bit", 2: "64-bit"}.get(b[p + 4])
    end = {1: "LSB", 2: "MSB"}.get(b[p + 5])
    if not cls or not end:
        return None
    e = "<" if end == "LSB" else ">"
    typ = {1: "relocatable", 2: "executable", 3: "shared object", 4: "core"}.get(_u16(b, p + 16, e), "?")
    mach = {3: "x86", 0x28: "ARM", 0xB7: "AArch64", 0x3E: "x86-64", 8: "MIPS", 0xF3: "RISC-V",
            0xA4: "Qualcomm Hexagon", 0xF7: "BPF"}.get(_u16(b, p + 18, e), f"machine {_u16(b, p + 18, e):#x}")
    return f"ELF {cls} {end} {typ}, {mach}"


def d_gzip(b, p):
    if len(b) < p + 10 or b[p + 3] & 0xE0:
        return None
    flags, mtime = b[p + 3], _u32(b, p + 4)
    desc = "gzip compressed data"
    if flags & 0x08:
        o = p + 10 + (2 + _u16(b, p + 10) if flags & 0x04 and len(b) > p + 12 else 0)
        name = _cstr(b, o, 128)
        if name:
            desc += f', original name "{name}"'
    if mtime:
        desc += f", mtime {mtime}"
    return desc


def d_zip(b, p):
    if len(b) < p + 30:
        return None
    nlen = _u16(b, p + 26)
    name = b[p + 30:p + 30 + min(nlen, 200)].decode("utf-8", errors="replace")
    method = {0: "stored", 8: "deflate", 12: "bzip2", 14: "lzma", 93: "zstd"}.get(_u16(b, p + 8), "?")
    return f'Zip archive entry "{name}" ({method}, {_u32(b, p + 18):,} bytes compressed)'


def d_lzma(b, p):
    if len(b) < p + 13:
        return None
    dic = _u32(b, p + 1)
    size = struct.unpack_from("<q", b, p + 5)[0]
    if dic & (dic - 1) or not (1 << 12) <= dic <= (1 << 28):
        return None
    if size != -1 and not (0 < size < (1 << 34)):
        return None
    return f"LZMA compressed data, dictionary {dic >> 10} KB" + (f", {size:,} bytes uncompressed" if size > 0 else "")


def d_png(b, p):
    if len(b) < p + 24 or b[p + 12:p + 16] != b"IHDR":
        return None
    return f"PNG image, {_u32(b, p + 16, '>')} x {_u32(b, p + 20, '>')}"


def d_jpeg(b, p):
    return "JPEG image" if len(b) > p + 3 and b[p + 3] in (0xE0, 0xE1, 0xDB, 0xEE, 0xE2) else None


def d_boot(b, p):
    if len(b) < p + 48:
        return None
    ver = _u32(b, p + 40)
    if ver > 8:
        return None
    if ver >= 3:
        kern, rd = _u32(b, p + 8), _u32(b, p + 12)
        cmd = _cstr(b, p + 44, 300)
    else:
        kern, rd = _u32(b, p + 8), _u32(b, p + 16)
        cmd = _cstr(b, p + 64, 300)
    d = f"Android boot image v{ver}, kernel {kern:,} bytes, ramdisk {rd:,} bytes"
    if cmd:
        d += f', cmdline "{cmd[:120]}"'
    return d


def d_vboot(b, p):
    if len(b) < p + 16:
        return None
    return f"Android vendor_boot image v{_u32(b, p + 8)}"


def d_avb(b, p):
    if len(b) < p + 16:
        return None
    return f"Android Verified Boot (vbmeta) header, version {_u32(b, p + 4, '>')}.{_u32(b, p + 8, '>')}"


def d_sparse(b, p):
    if len(b) < p + 28 or _u16(b, p + 4) != 1:
        return None
    return (f"Android sparse image, block size {_u32(b, p + 12)}, {_u32(b, p + 16):,} blocks "
            f"({(_u32(b, p + 12) * _u32(b, p + 16)) >> 20} MB raw), {_u32(b, p + 20)} chunks")


def d_dtb(b, p):
    if len(b) < p + 24:
        return None
    total, ver = _u32(b, p + 4, ">"), _u32(b, p + 20, ">")
    if not (40 < total < (64 << 20)) or ver > 32:
        return None
    return f"Flattened device tree (DTB), {total:,} bytes, version {ver}"


def d_dtbo(b, p):
    if len(b) < p + 20:
        return None
    return f"Android DTBO table, {_u32(b, p + 4, '>'):,} bytes, {_u32(b, p + 16, '>')} entries"


def d_squash(b, p):
    if len(b) < p + 48:
        return None
    major = _u16(b, p + 28)
    if major not in (3, 4):
        return None
    return f"Squashfs filesystem v{major}.{_u16(b, p + 30)}, {_u32(b, p + 40):,} bytes, {_u32(b, p + 4):,} inodes"


def d_cpio(b, p):
    if len(b) < p + 110:
        return None
    try:
        nlen = int(b[p + 94:p + 102], 16)
    except ValueError:
        return None
    name = _cstr(b, p + 110, min(nlen, 200))
    return f'cpio archive (newc), first entry "{name}"' if name != "TRAILER!!!" else None


def d_tar(b, p):
    s = p - 257
    if s < 0:
        return None
    return f'POSIX tar archive, first entry "{_cstr(b, s, 100)}"'


def d_erofs(b, p):
    return "EROFS filesystem" if p >= 1024 else None


def d_f2fs(b, p):
    return "F2FS filesystem" if p >= 1024 else None


def d_arm64(b, p):
    s = p - 56
    if s < 0 or len(b) < s + 64:
        return None
    return f"Linux ARM64 kernel Image, size {struct.unpack_from('<Q', b, s + 16)[0]:,} bytes"


def d_linux(b, p):
    s = b[p:p + 200].split(b"\0", 1)[0].split(b"\n", 1)[0]
    if len(s) < 20:
        return None
    return s.decode("latin-1", errors="replace")


def d_dex(b, p):
    if len(b) < p + 8 or not b[p + 4:p + 7].isdigit():
        return None
    return f"Android DEX file, version {b[p + 4:p + 7].decode()}"


def d_mz(b, p):
    if len(b) < p + 64:
        return None
    pe = _u32(b, p + 60)
    if 64 <= pe < 4096 and b[p + pe:p + pe + 4] == b"PE\0\0":
        return "Windows PE executable"
    return None


def d_der(b, p):
    if len(b) < p + 8 or b[p + 4:p + 6] != b"\x30\x82":
        return None
    return f"DER-encoded ASN.1 structure (certificate?), {_u16(b, p + 2, '>') + 4:,} bytes"


def d_pem(b, p):
    return "PEM " + _cstr(b, p + 11, 40).split("-")[0].strip().lower() + " block"


def d_ok(desc):
    return lambda b, p: desc


# (magic, kind, describer, offset adjustment = where the structure starts relative to the magic)
SIGNATURES: list[tuple[bytes, str, object, int]] = [
    (b"\x7fELF", "ELF", d_elf, 0),
    (b"\x1f\x8b\x08", "gzip", d_gzip, 0),
    (b"PK\x03\x04", "zip", d_zip, 0),
    (b"7z\xbc\xaf\x27\x1c", "7-zip", d_ok("7-zip archive"), 0),
    (b"\xfd7zXZ\x00", "xz", d_ok("xz compressed data"), 0),
    (b"BZh91AY&SY", "bzip2", d_ok("bzip2 compressed data (900k blocks)"), 0),
    (b"\x04\x22\x4d\x18", "LZ4", d_ok("LZ4 compressed data (frame)"), 0),
    (b"\x02\x21\x4c\x18", "LZ4", d_ok("LZ4 compressed data (legacy, used for Linux kernels/ramdisks)"), 0),
    (b"\x28\xb5\x2f\xfd", "zstd", d_ok("Zstandard compressed data"), 0),
    (b"\x5d\x00\x00", "LZMA", d_lzma, 0),
    (b"\x89PNG\r\n\x1a\n", "PNG", d_png, 0),
    (b"\xff\xd8\xff", "JPEG", d_jpeg, 0),
    (b"GIF89a", "GIF", d_ok("GIF image (89a)"), 0),
    (b"GIF87a", "GIF", d_ok("GIF image (87a)"), 0),
    (b"%PDF-", "PDF", d_ok("PDF document"), 0),
    (b"ANDROID!", "boot", d_boot, 0),
    (b"VNDRBOOT", "vendor_boot", d_vboot, 0),
    (b"AVB0", "vbmeta", d_avb, 0),
    (b"\x3a\xff\x26\xed", "sparse", d_sparse, 0),
    (b"\xd0\x0d\xfe\xed", "DTB", d_dtb, 0),
    (b"\xd7\xb7\xab\x1e", "DTBO", d_dtbo, 0),
    (b"hsqs", "squashfs", d_squash, 0),
    (b"070701", "cpio", d_cpio, 0),
    (b"070702", "cpio", d_cpio, 0),
    (b"ustar", "tar", d_tar, -257),
    (b"\xe2\xe1\xf5\xe0", "EROFS", d_erofs, -1024),
    (b"\x10\x20\xf5\xf2", "F2FS", d_f2fs, -1024),
    (b"ARM\x64", "kernel", d_arm64, -56),
    (b"Linux version ", "kernel", d_linux, 0),
    (b"\x27\x05\x19\x56", "uImage", d_ok("U-Boot legacy uImage header"), 0),
    (b"SQLite format 3\x00", "SQLite", d_ok("SQLite 3 database"), 0),
    (b"dex\n0", "DEX", d_dex, 0),
    (b"oat\n", "OAT", d_ok("Android OAT file"), 0),
    (b"vdex", "VDEX", d_ok("Android VDEX file"), 0),
    (b"\xca\xfe\xba\xbe", "class", d_ok("Java class file / Mach-O fat binary"), 0),
    (b"MZ", "PE", d_mz, 0),
    (b"\x30\x82", "DER", d_der, 0),
    (b"-----BEGIN ", "PEM", d_pem, 0),
    (b"UBI#", "UBI", d_ok("UBI erase count header"), 0),
    (b"MTK_BLOADER_INFO", "MTK", d_ok("MediaTek bootloader info"), 0),
]
_SIG_RE = re.compile(b"|".join(re.escape(m) for m, *_ in SIGNATURES))
_SIG_BY_MAGIC = {}
for _m, _k, _d, _a in SIGNATURES:
    _SIG_BY_MAGIC.setdefault(_m, (_k, _d, _a))
_MAX_MAGIC = max(len(m) for m, *_ in SIGNATURES)
LOOKAHEAD = 4096   # bytes after a magic that describers may inspect


def _printable(c: int) -> bool:
    return 0x20 <= c <= 0x7E or c == 0x09


def _trailing_run_start(buf: bytes, enc: str, cap: int = 1 << 16) -> int:
    """Start of the printable (ASCII) / printable-UTF-16LE stretch at the end of buf."""
    n = len(buf)
    i = n
    stop = max(0, n - cap)
    if enc == "ASCII":
        while i > stop and _printable(buf[i - 1]):
            i -= 1
        return i
    # UTF-16LE: pairs (printable, 0); the buffer may end mid-pair
    if i > stop and _printable(buf[i - 1]):
        i -= 1
    while i - 2 >= stop and buf[i - 1] == 0 and _printable(buf[i - 2]):
        i -= 2
    return i


class Scanner:
    """Feed chunks in order; collects signatures, strings and per-block entropy."""

    def __init__(self, min_len=5, utf16=True, max_hits=5000, max_strings=200_000, block=65536):
        self.min_len = min_len
        self.utf16 = utf16
        self.hits: list[Hit] = []
        self.strings: list[tuple[int, str, str]] = []
        self.entropy: list[float] = []
        self.max_hits = max_hits
        self.max_strings = max_strings
        self.block = block
        self._buf = b""
        self._base = 0          # absolute offset of _buf[0]
        self._sig_from = 0      # absolute offset from which magics are still unscanned
        self._str_from = {"ASCII": 0, "UTF-16": 0}   # resume points for string runs
        self._blockbuf = bytearray()
        self._ascii = re.compile(rb"[\x20-\x7e\t]{%d,}" % min_len)
        self._wide = re.compile(rb"(?:[\x20-\x7e]\x00){%d,}" % min_len)
        self._last_cpio = -1 << 40
        self._der_end = -1
        self.truncated = False
        # ext4: magic 0xEF53 at 0x438 of the stream start
        self._ext4_checked = False

    def _entropy_feed(self, data: bytes):
        self._blockbuf += data
        while len(self._blockbuf) >= self.block:
            blk = bytes(self._blockbuf[:self.block])
            del self._blockbuf[:self.block]
            self.entropy.append(min(1.0, len(zlib.compress(blk, 1)) / len(blk)))

    def feed(self, data: bytes, final=False):
        """Process the next chunk. Magics are reported once their LOOKAHEAD bytes are available;
        string runs once they are terminated (or at the end of the stream)."""
        self._entropy_feed(data)
        buf = self._buf + data
        base = self._base
        if not self._ext4_checked and (len(buf) >= 0x43A or final):
            self._ext4_checked = True
            if base == 0 and buf[0x438:0x43A] == b"\x53\xef":
                self.hits.append(Hit(0, "ext4", "ext2/3/4 filesystem superblock"))
        limit = len(buf) if final else len(buf) - LOOKAHEAD   # magics must start before this
        keep_from = len(buf)
        if limit > 0:
            # ---- signatures
            sfrom = max(0, self._sig_from - base)
            for m in _SIG_RE.finditer(buf, sfrom):
                pos = m.start()
                if pos >= limit:
                    break
                kind, desc_fn, adj = _SIG_BY_MAGIC[m.group(0)]
                abs_off = base + pos + adj
                if abs_off < 0 or len(self.hits) >= self.max_hits:
                    self.truncated = self.truncated or len(self.hits) >= self.max_hits
                    continue
                try:
                    desc = desc_fn(buf, pos)
                except (struct.error, IndexError, ValueError):
                    desc = None
                if not desc:
                    continue
                if kind == "cpio":     # one hit per archive, not per entry
                    near = abs_off - self._last_cpio < (1 << 20)
                    self._last_cpio = abs_off
                    if near:
                        continue
                if kind == "DER":      # nested ASN.1: only the outermost structure
                    if abs_off < self._der_end:
                        continue
                    self._der_end = abs_off + _u16(buf, pos + 2, ">") + 4
                self.hits.append(Hit(abs_off, kind, desc))
            self._sig_from = base + limit
            keep_from = limit
        else:
            keep_from = 0
        # ---- strings: a run touching the end of the buffer may continue in the next chunk
        for rx, enc in ((self._ascii, "ASCII"), (self._wide, "UTF-16")):
            if enc == "UTF-16" and not self.utf16:
                continue
            start = max(0, self._str_from[enc] - base)
            resume = None
            last_end = start
            # a UTF-16 run can end one byte early when the next char's high byte is in the next chunk
            slack = 1 if enc == "UTF-16" else 0
            for m in rx.finditer(buf, start):
                if m.end() >= len(buf) - slack and not final and m.end() - m.start() < (1 << 20):
                    resume = m.start()          # may continue in the next chunk
                    break
                if len(self.strings) < self.max_strings:
                    txt = m.group(0).decode("utf-16-le" if enc == "UTF-16" else "latin-1", errors="replace")
                    self.strings.append((base + m.start(), enc, txt[:400]))
                else:
                    self.truncated = True
                last_end = m.end()
            if resume is None:
                # carry a trailing printable stretch that is still too short to be a match
                resume = len(buf) if final else max(last_end, _trailing_run_start(buf, enc))
            self._str_from[enc] = base + resume
            keep_from = min(keep_from, resume)
        # keep the unprocessed tail (lookahead area / unfinished string)
        keep_from = max(0, min(keep_from, len(buf)))
        self._base = base + keep_from
        self._buf = b"" if final else buf[keep_from:]

    def finish(self):
        self.feed(b"", final=True)
        if self._blockbuf:
            blk = bytes(self._blockbuf)
            self.entropy.append(min(1.0, len(zlib.compress(blk, 1)) / max(1, len(blk))))
        self.hits.sort(key=lambda h: h.offset)
        self.strings.sort(key=lambda s: s[0])
