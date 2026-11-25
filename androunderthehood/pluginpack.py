"""Plugin packages: .7z archives (optionally AES-256 encrypted, with encrypted file names) that are read
straight into memory and imported from there – their contents are never written to disk.

Also: an import hook that serves a plugin's modules from memory, packing a folder into a .7z, and
Windows DPAPI helpers to keep plugin passwords encrypted for the current Windows user.
"""
from __future__ import annotations

import base64
import ctypes
import importlib.abc
import importlib.util
import io
import linecache
import os
import sys
from ctypes import wintypes

MAX_TOTAL = 256 * 1024 * 1024        # refuse archives that unpack to more than this (zip-bomb guard)


class PasswordNeeded(Exception):
    def __str__(self):
        return "this plugin package is password protected"


class TooLarge(ValueError):
    pass


def _mb(n):
    return f"{n / 1048576:,.0f} MB"


class WrongPassword(Exception):
    def __str__(self):
        return "wrong password (or the package is damaged)"


# ---------------------------------------------------------------------------
# Reading into memory
# ---------------------------------------------------------------------------
def _factory():
    import py7zr.io as pio

    class _Mem(pio.Py7zIO):
        total = [0]

        def __init__(self, name):
            self.name = name
            self.buf = io.BytesIO()

        def write(self, s):
            _Mem.total[0] += len(s)
            if _Mem.total[0] > MAX_TOTAL:
                raise ValueError("plugin package is too large when unpacked")
            return self.buf.write(s)

        def read(self, size=None):
            return self.buf.read(size)

        def seek(self, offset, whence=0):
            return self.buf.seek(offset, whence)

        def flush(self):
            pass

        def size(self):
            return self.buf.getbuffer().nbytes

    class _Factory(pio.WriterFactory):
        def __init__(self):
            self.products = {}
            _Mem.total[0] = 0

        def create(self, filename):
            p = _Mem(filename)
            self.products[filename] = p
            return p

    return _Factory()


def needs_password(path: str) -> bool:
    import py7zr
    try:
        with py7zr.SevenZipFile(path, "r") as z:
            return bool(z.needs_password())
    except py7zr.exceptions.PasswordRequired:
        return True


def read_archive(path: str, password: str | None = None) -> dict[str, bytes]:
    """All files of the archive as {relative/path: bytes}, in memory. A single top folder is removed so
    plugin.json ends up at the root."""
    import py7zr
    size = os.path.getsize(path)
    if size > MAX_TOTAL:
        raise TooLarge(f"{os.path.basename(path)} is {_mb(size)} – plugin packages are limited to {_mb(MAX_TOTAL)} "
                       "(is this really a plugin?)")
    try:
        with py7zr.SevenZipFile(path, "r", password=password or None) as z:
            if z.needs_password() and not password:
                raise PasswordNeeded()
            total = z.archiveinfo().uncompressed or 0
            if total > MAX_TOTAL:
                raise TooLarge(f"{os.path.basename(path)} unpacks to {_mb(total)} – plugin packages are limited to "
                               f"{_mb(MAX_TOTAL)}")
            fac = _factory()
            z.extractall(factory=fac)
    except py7zr.exceptions.PasswordRequired:
        raise PasswordNeeded() from None
    except (PasswordNeeded, ValueError):
        raise
    except Exception:  # noqa: BLE001
        if password:      # a wrong key decrypts to garbage: py7zr then fails in many different ways
            raise WrongPassword() from None
        raise
    files = {}
    for name, prod in fac.products.items():
        files[name.replace("\\", "/").lstrip("/")] = prod.buf.getvalue()
    if "plugin.json" not in files:
        tops = {n.split("/", 1)[0] for n in files}
        if len(tops) == 1:
            top = tops.pop() + "/"
            files = {n[len(top):]: b for n, b in files.items() if n.startswith(top) and n != top}
    if "plugin.json" not in files:
        if password:
            raise WrongPassword()
        raise ValueError("no plugin.json in the package")
    return files


def pack(folder: str, out: str, password: str | None = None, encrypt_names: bool = True):
    """Pack a plugin folder into a .7z (AES-256 when a password is given)."""
    import py7zr
    if not os.path.isfile(os.path.join(folder, "plugin.json")):
        raise ValueError("the folder has no plugin.json")
    tmp = out + ".part"
    with py7zr.SevenZipFile(tmp, "w", password=password or None,
                            header_encryption=bool(password and encrypt_names)) as z:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "_lib", ".git")]
            for f in files:
                if f.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, folder).replace("\\", "/"))
    os.replace(tmp, out)


# ---------------------------------------------------------------------------
# Import hook: modules served from memory
# ---------------------------------------------------------------------------
class MemoryImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Serves package `prefix` (= the plugin's main file) and its submodules from a {path: bytes} dict."""

    def __init__(self, prefix: str, files: dict[str, bytes], main: str, origin: str):
        self.prefix = prefix
        self.files = files
        self.main = main
        self.origin = origin            # shown in tracebacks, e.g. "7z:reset_restore.7z"

    def _locate(self, fullname):
        if fullname == self.prefix:
            return self.main, True
        rel = fullname[len(self.prefix) + 1:].replace(".", "/")
        if rel + "/__init__.py" in self.files:
            return rel + "/__init__.py", True
        if rel + ".py" in self.files:
            return rel + ".py", False
        return None, False

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.prefix and not fullname.startswith(self.prefix + "."):
            return None
        rel, is_pkg = self._locate(fullname)
        if rel is None:
            return None
        spec = importlib.util.spec_from_loader(fullname, self, origin=f"<{self.origin}>/{rel}", is_package=is_pkg)
        spec.has_location = False
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        rel, is_pkg = self._locate(module.__name__)
        src = self.files[rel].decode("utf-8-sig")
        filename = f"<{self.origin}>/{rel}"
        linecache.cache[filename] = (len(src), None, src.splitlines(True), filename)   # readable tracebacks
        module.__file__ = filename
        if is_pkg:
            base = rel.rsplit("/", 1)[0] if "/" in rel and rel.endswith("__init__.py") else ""
            module.__path__ = [f"<{self.origin}>/{base}"]
        exec(compile(src, filename, "exec", dont_inherit=True), module.__dict__)   # noqa: S102

    def install(self):
        self.uninstall()
        sys.meta_path.insert(0, self)

    def uninstall(self):
        sys.meta_path[:] = [f for f in sys.meta_path
                            if not (isinstance(f, MemoryImporter) and f.prefix == self.prefix)]
        for k in [k for k in linecache.cache if k.startswith(f"<{self.origin}>/")]:
            linecache.cache.pop(k, None)


# ---------------------------------------------------------------------------
# Passwords: Windows DPAPI (encrypted for the current Windows user)
# ---------------------------------------------------------------------------
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    b = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    b._keep = buf
    return b


_ENTROPY = b"AndroUnderTheHood plugin password"


def protect(text: str) -> str:
    out = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(_blob(text.encode("utf-8"))), "aoh-plugin", ctypes.byref(
            _blob(_ENTROPY)), None, None, 0x1, ctypes.byref(out)):          # CRYPTPROTECT_UI_FORBIDDEN
        raise OSError("CryptProtectData failed")
    try:
        return base64.b64encode(ctypes.string_at(out.pbData, out.cbData)).decode()
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def unprotect(token: str) -> str | None:
    try:
        data = base64.b64decode(token)
    except Exception:  # noqa: BLE001
        return None
    out = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(_blob(data)), None, ctypes.byref(_blob(_ENTROPY)),
                                                    None, None, 0x1, ctypes.byref(out)):
        return None
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)
