"""Thumbnail + media metadata extraction for remote files.

Everything here is Qt-free: functions take a FileSystem + Entry and return
PNG bytes plus an ``info`` dict that the properties panel displays.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import posixpath
import re
import secrets
import shutil
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import ExifTags, Image, ImageOps

from .backend import Entry, FileSystem

try:  # HEIC / HEIF photos (Samsung, Pixel "high efficiency" mode)
    # pi-heif: the decode-only build of pillow-heif (LGPL libheif + libde265, no GPL x265 encoder)
    try:
        import pi_heif as _heif
    except ImportError:
        import pillow_heif as _heif

    _heif.register_heif_opener()
    HEIF = True
except Exception:  # noqa: BLE001
    HEIF = False

Image.MAX_IMAGE_PIXELS = 300_000_000

IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "ico", "heic", "heif", "avif"}
VIDEO_EXT = {"mp4", "m4v", "mkv", "webm", "3gp", "3g2", "mov", "avi", "ts", "mts", "m2ts", "wmv", "flv", "mpg", "mpeg"}
AUDIO_EXT = {"mp3", "m4a", "flac", "ogg", "opus", "aac", "wav", "wma", "amr", "mka"}
APK_EXT = {"apk"}
TEXT_EXT = {"txt", "log", "xml", "json", "conf", "cfg", "ini", "prop", "sh", "rc", "md", "csv", "html", "htm",
            "js", "css", "py", "java", "kt", "c", "h", "cpp", "yaml", "yml", "properties", "list", "smali"}

MAX_FULL_IMAGE = 60 * 1024 * 1024
THUMB_VERSION = 2


def ext_of(name: str) -> str:
    _, e = posixpath.splitext(name)
    return e[1:].lower()


def media_kind(entry: Entry) -> str | None:
    if entry.is_dir:
        return None
    e = ext_of(entry.name)
    if e in IMAGE_EXT:
        return "image"
    if e in VIDEO_EXT:
        return "video"
    if e in AUDIO_EXT:
        return "audio"
    if e in APK_EXT:
        return "apk"
    return None


def file_size(entry: Entry) -> int:
    return entry.extra.get("target_size", entry.size) if entry.is_link else entry.size


# --------------------------------------------------------------------------
# Disk cache
# --------------------------------------------------------------------------
class ThumbCache:
    def __init__(self):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        self.dir = os.path.join(base, "AndroUnderTheHood", "thumbs")
        os.makedirs(self.dir, exist_ok=True)

    def key(self, host: str, entry: Entry, size: int) -> str:
        raw = f"{THUMB_VERSION}|{host}|{entry.path}|{file_size(entry)}|{entry.mtime}|{size}"
        return hashlib.sha1(raw.encode("utf-8", "surrogateescape")).hexdigest()

    def get(self, key: str):
        p = os.path.join(self.dir, key[:2], key)
        try:
            with open(p + ".json", "r", encoding="utf-8") as f:
                info = json.load(f)
            png = None
            if os.path.exists(p + ".png"):
                with open(p + ".png", "rb") as f:
                    png = f.read()
            return png, info
        except Exception:  # noqa: BLE001
            return None

    def put(self, key: str, png: bytes | None, info: dict):
        d = os.path.join(self.dir, key[:2])
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, key)
        try:
            if png:
                with open(p + ".png", "wb") as f:
                    f.write(png)
            with open(p + ".json", "w", encoding="utf-8") as f:
                json.dump(info, f)
        except Exception:  # noqa: BLE001
            pass

    def clear(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)

    def usage(self) -> int:
        total = 0
        for root, _, files in os.walk(self.dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------
def _to_png(img: Image.Image, size: int) -> bytes:
    img = img.copy()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False)
    return buf.getvalue()


def _exif_thumbnail(head: bytes) -> bytes | None:
    """Extract the embedded JPEG thumbnail from the EXIF APP1 segment."""
    i = head.find(b"Exif\x00\x00")
    if i < 4 or head[i - 4:i - 2] != b"\xff\xe1":
        return None
    seg_len = int.from_bytes(head[i - 2:i], "big")
    seg_end = min(len(head), i - 2 + seg_len)
    soi = head.find(b"\xff\xd8\xff", i + 8, seg_end)
    if soi < 0:
        return None
    eoi = head.find(b"\xff\xd9", soi, seg_end)
    if eoi < 0:
        return None
    return head[soi:eoi + 2]


_EXIF_WANTED = {
    "Make": "Camera make", "Model": "Camera model", "DateTimeOriginal": "Date taken",
    "DateTime": "Date (EXIF)", "ExposureTime": "Exposure", "FNumber": "F-number",
    "ISOSpeedRatings": "ISO", "PhotographicSensitivity": "ISO", "FocalLength": "Focal length",
    "LensModel": "Lens", "Software": "Software", "Orientation": "Orientation",
}


def _fmt_exif_val(tag: str, v) -> str:
    try:
        if tag == "ExposureTime":
            f = float(v)
            return f"1/{round(1 / f)} s" if 0 < f < 1 else f"{f:g} s"
        if tag == "FNumber":
            return f"f/{float(v):.1f}"
        if tag == "FocalLength":
            return f"{float(v):.1f} mm"
    except Exception:  # noqa: BLE001
        pass
    if isinstance(v, bytes):
        v = v.decode(errors="replace")
    return str(v).strip("\x00 ")


def _gps_to_deg(vals, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in vals)
        deg = d + m / 60 + s / 3600
        if ref in ("S", "W"):
            deg = -deg
        return round(deg, 6)
    except Exception:  # noqa: BLE001
        return None


def image_info(img: Image.Image) -> dict:
    info = {"Dimensions": f"{img.width} × {img.height} px", "Image format": img.format or "?",
            "Color mode": img.mode}
    if getattr(img, "n_frames", 1) > 1:
        info["Frames"] = str(img.n_frames)
    try:
        exif = img.getexif()
        merged = dict(exif)
        try:
            merged.update(exif.get_ifd(ExifTags.IFD.Exif))
        except Exception:  # noqa: BLE001
            pass
        for tag_id, val in merged.items():
            tag = ExifTags.TAGS.get(tag_id)
            if tag in _EXIF_WANTED:
                label = _EXIF_WANTED[tag]
                if tag == "Orientation":
                    if val in (1, None):
                        continue
                    val = {3: "Rotated 180°", 6: "Rotated 90° CW", 8: "Rotated 90° CCW"}.get(val, val)
                info[label] = _fmt_exif_val(tag, val)
        try:
            gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
            if gps:
                lat = _gps_to_deg(gps.get(2), gps.get(1))
                lon = _gps_to_deg(gps.get(4), gps.get(3))
                if lat is not None and lon is not None:
                    info["GPS"] = f"{lat}, {lon}"
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass
    return info


def image_thumbnail(fs: FileSystem, entry: Entry, size: int) -> tuple[bytes | None, dict]:
    fsize = file_size(entry)
    ext = ext_of(entry.name)
    info: dict = {}
    if ext in ("jpg", "jpeg") and fsize > 400 * 1024:
        head = fs.read_range(entry.path, 0, min(fsize, 256 * 1024))
        try:
            hdr = Image.open(io.BytesIO(head))
            info = image_info(hdr)
            orientation = hdr.getexif().get(0x0112, 1)
        except Exception:  # noqa: BLE001
            orientation = 1
        th = _exif_thumbnail(head)
        if th:
            try:
                t = Image.open(io.BytesIO(th))
                t.load()
                if max(t.size) >= min(size, 160):
                    t = _apply_orientation(t, orientation)
                    return _to_png(t, size), info
            except Exception:  # noqa: BLE001
                pass
    if fsize > MAX_FULL_IMAGE:
        return None, info or {"Note": "Too large for thumbnail"}
    data = fs.read_all(entry.path, fsize)
    img = Image.open(io.BytesIO(data))
    info = image_info(img)
    img.draft("RGB", (size * 2, size * 2))  # fast JPEG downscale on decode
    img = ImageOps.exif_transpose(img)
    return _to_png(img, size), info


def _apply_orientation(img: Image.Image, o: int) -> Image.Image:
    ops = {2: [Image.Transpose.FLIP_LEFT_RIGHT], 3: [Image.Transpose.ROTATE_180],
           4: [Image.Transpose.FLIP_TOP_BOTTOM], 5: [Image.Transpose.TRANSPOSE],
           6: [Image.Transpose.ROTATE_270], 7: [Image.Transpose.TRANSVERSE], 8: [Image.Transpose.ROTATE_90]}
    for op in ops.get(o, []):
        img = img.transpose(op)
    return img


# --------------------------------------------------------------------------
# Video / audio via ffmpeg reading through a local HTTP range proxy
# --------------------------------------------------------------------------
def ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg

        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            return p
    except Exception:  # noqa: BLE001
        pass
    return shutil.which("ffmpeg")


class _RangeHandler(BaseHTTPRequestHandler):
    server: "_RangeHTTPServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence
        pass

    def _target(self):
        tok = self.path.strip("/").split("/", 1)[0]
        return self.server.owner.files.get(tok)

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _serve(self, head: bool):
        t = self._target()
        if not t:
            self.send_error(404)
            return
        fs, path, size = t
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        status = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                status = 206
        if start >= size and size > 0:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = max(0, end - start + 1)
        self.send_response(status)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head or length == 0:
            return
        try:
            f = fs.open_read(path, size)
            try:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(512 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            finally:
                f.close()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass


class _RangeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    owner: "RangeProxy"


class RangeProxy:
    """Exposes remote files at http://127.0.0.1:<port>/<token> so ffmpeg can seek in them."""

    _inst = None
    _inst_lock = threading.Lock()

    @classmethod
    def get(cls) -> "RangeProxy":
        with cls._inst_lock:
            if cls._inst is None:
                cls._inst = RangeProxy()
            return cls._inst

    def __init__(self):
        self.files: dict[str, tuple] = {}
        self.httpd = _RangeHTTPServer(("127.0.0.1", 0), _RangeHandler)
        self.httpd.owner = self
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def register(self, fs, path, size) -> tuple[str, str]:
        tok = secrets.token_hex(12)
        self.files[tok] = (fs, path, size)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", posixpath.basename(path))[-60:]
        return tok, f"http://127.0.0.1:{self.httpd.server_address[1]}/{tok}/{name}"

    def unregister(self, tok):
        self.files.pop(tok, None)


_NOWIN = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def _parse_ffmpeg_info(stderr: str) -> dict:
    info: dict = {}
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        info["Duration"] = f"{h}:{mi:02d}:{s:05.2f}" if h else f"{mi}:{s:05.2f}"
    m = re.search(r"bitrate: (\d+ kb/s)", stderr)
    if m:
        info["Bitrate"] = m.group(1)
    m = re.search(r"Stream #\S+.*?: Video: ([^,\s]+)[^\n]*?, (\d{2,5})x(\d{2,5})", stderr)
    if m and "attached pic" not in stderr[m.start():stderr.find("\n", m.start())]:
        info["Video"] = f"{m.group(1)}, {m.group(2)} × {m.group(3)}"
        fps = re.search(r"([\d.]+) fps", stderr[m.start():stderr.find("\n", m.start())])
        if fps:
            info["Video"] += f", {fps.group(1)} fps"
    m = re.search(r"Stream #\S+.*?: Audio: ([^,\s]+)[^\n]*?, (\d+) Hz, ([^,\n]+)", stderr)
    if m:
        info["Audio"] = f"{m.group(1)}, {int(m.group(2)) / 1000:g} kHz, {m.group(3).strip()}"
    for key in ("title", "artist", "album", "date", "genre", "creation_time", "location"):
        m = re.search(rf"^\s+{key}\s*:\s*(.+)$", stderr, re.M | re.I)
        if m:
            info[key.replace("_", " ").capitalize()] = m.group(1).strip()
    m = re.search(r"rotate\s*:\s*(-?\d+)|rotation of (-?[\d.]+)", stderr)
    if m:
        info["Rotation"] = (m.group(1) or m.group(2)) + "°"
    return info


def media_thumbnail(fs: FileSystem, entry: Entry, size: int, is_audio: bool) -> tuple[bytes | None, dict]:
    exe = ffmpeg_exe()
    if not exe:
        return None, {"Note": "ffmpeg not available – no video thumbnails"}
    proxy = RangeProxy.get()
    tok, url = proxy.register(fs, entry.path, file_size(entry))
    try:
        vf = f"scale='min({size},iw)':'min({size},ih)':force_original_aspect_ratio=decrease"
        attempts = [["-i", url]] if is_audio else [["-ss", "1.5", "-i", url], ["-i", url]]
        info: dict = {}
        for pre in attempts:
            cmd = [exe, "-hide_banner", "-nostdin", "-rw_timeout", "20000000", *pre,
                   "-map", "0:v:0?", "-frames:v", "1", "-vf", vf, "-f", "image2pipe", "-c:v", "png", "-"]
            try:
                p = subprocess.run(cmd, capture_output=True, timeout=45, creationflags=_NOWIN)
            except subprocess.TimeoutExpired:
                return None, info or {"Note": "ffmpeg timed out"}
            stderr = p.stderr.decode(errors="replace")
            info = _parse_ffmpeg_info(stderr) or info
            if p.stdout[:8] == b"\x89PNG\r\n\x1a\n":
                return p.stdout, info
        return None, info
    finally:
        proxy.unregister(tok)


# --------------------------------------------------------------------------
# APK icons
# --------------------------------------------------------------------------
_DENSITY = {"xxxhdpi": 6, "xxhdpi": 5, "xhdpi": 4, "hdpi": 3, "mdpi": 2, "ldpi": 1, "anydpi": 0}


def apk_thumbnail(fs: FileSystem, entry: Entry, size: int) -> tuple[bytes | None, dict]:
    f = fs.open_read(entry.path, file_size(entry))
    try:
        z = zipfile.ZipFile(f)
        names = z.infolist()
        info = {"APK entries": str(len(names))}
        dex = sum(1 for n in names if re.fullmatch(r"classes\d*\.dex", n.filename))
        if dex:
            info["DEX files"] = str(dex)
        abis = sorted({n.filename.split("/")[1] for n in names if n.filename.startswith("lib/") and n.filename.count("/") >= 2})
        if abis:
            info["Native ABIs"] = ", ".join(abis)
        best = None
        for n in names:
            fn = n.filename
            if not re.search(r"\.(png|webp)$", fn, re.I) or not fn.startswith("res/"):
                continue
            base = posixpath.basename(fn).lower()
            score = 0
            if base.startswith(("ic_launcher", "app_icon", "icon", "launcher")):
                score += 100
            else:
                continue
            if "foreground" in base or "background" in base or "monochrome" in base:
                score -= 60
            if "round" in base:
                score -= 5
            d = fn.split("/")[1]
            for k, v in _DENSITY.items():
                if k in d:
                    score += v * 3
                    break
            if best is None or score > best[0]:
                best = (score, n)
        if not best:
            return None, info
        img = Image.open(io.BytesIO(z.read(best[1])))
        return _to_png(img, size), info
    finally:
        f.close()


# --------------------------------------------------------------------------
def make_thumbnail(fs: FileSystem, entry: Entry, size: int = 256) -> tuple[bytes | None, dict]:
    kind = media_kind(entry)
    if kind == "image":
        return image_thumbnail(fs, entry, size)
    if kind in ("video", "audio"):
        return media_thumbnail(fs, entry, size, kind == "audio")
    if kind == "apk":
        return apk_thumbnail(fs, entry, size)
    return None, {}
