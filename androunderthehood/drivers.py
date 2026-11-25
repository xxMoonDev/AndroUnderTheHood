"""USB drivers for every Android USB mode, installed with one click.

Packages come only from official sources and are checked before they are installed:
  * Google USB Driver (ADB / fastboot)        – dl.google.com (Android SDK repository)
  * Qualcomm (EDL 9008, Diag, modem, RmNet)   – Microsoft Update Catalog (WHQL-signed)
  * MediaTek (PreLoader / DA / VCOM ports)    – Microsoft Update Catalog
  * Samsung (ADB, modem, Download mode …)     – Microsoft Update Catalog
Every download must match its published SHA-1 and every .cat must carry a valid Authenticode
signature from Google or Microsoft's hardware publisher. Installing runs `pnputil /add-driver
… /install` in one elevated PowerShell (a single Windows UAC prompt).

MTP, PTP, RNDIS / NCM tethering, mass storage and MIDI use drivers built into Windows.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from ctypes import wintypes
from dataclasses import dataclass, field

log = logging.getLogger("androunderthehood")
_NOWIN = 0x08000000 if sys.platform == "win32" else 0
SIGNERS = ("Google LLC", "Microsoft Windows Hardware Compatibility Publisher")
CATALOG = "https://catalog.s.download.windowsupdate.com/"


@dataclass
class Download:
    url: str
    sha1: str
    size: int

    @property
    def name(self) -> str:
        return self.url.rsplit("/", 1)[-1]


@dataclass
class Package:
    key: str
    title: str
    modes: str                    # what it covers, shown to the user
    source: str
    files: list[Download] = field(default_factory=list)
    checks: list[tuple[str, str]] = field(default_factory=list)   # (inf name, DriverVer version) in the store
    vids: tuple[str, ...] = ()    # USB vendor IDs it serves (to match connected devices)
    builtin: bool = False         # part of Windows: status only


PACKAGES = [
    Package("google", "Google USB Driver r13", "ADB and fastboot (Pixel / Google IDs, generic bootloaders)",
            "Google (Android SDK repository)",
            [Download("https://dl.google.com/android/repository/usb_driver_r13-windows.zip",
                      "08a48c39084e9443f6146c239cbd3be6f91e681b", 8682039)],
            [("android_winusb.inf", "11.0.0000.00000")], ("18D1",)),
    Package("qualcomm", "Qualcomm USB drivers",
            "EDL / QDLoader 9008, Diagnostics (QXDM), NMEA, modem and RmNet network ports, fastboot 18D1:D00D",
            "Microsoft Update Catalog (Qualcomm, WHQL)",
            [Download(CATALOG + "c/msdownload/update/driver/drvs/2016/04/"
                      "20855130_dae427ffe0a2d6268aa209d782d05ea874aad0dc.cab",
                      "dae427ffe0a2d6268aa209d782d05ea874aad0dc", 10042674),
             Download(CATALOG + "d/msdownload/update/driver/drvs/2022/01/"
                      "qcusbser_658af40f6f7a19d91820ba7dd8c62f5f81356c11.cab",
                      "658af40f6f7a19d91820ba7dd8c62f5f81356c11", 139641)],
            [("qcser.inf", "2.1.1.0"), ("qcser.inf", "1.1.0.2"), ("qcnet.inf", "2.1.1.0")], ("05C6", "1D9C")),
    Package("mediatek", "MediaTek USB VCOM drivers",
            "PreLoader / BootROM / DA ports (SP Flash Tool, mtkclient), MediaTek modem and AT ports",
            "Microsoft Update Catalog (MediaTek, WHQL)",
            [Download(CATALOG + "d/msdownload/update/driver/drvs/2016/07/"
                      "20896845_fdc6bb5aa9a9bac99adf85d931d6c21d1130a96e.cab",
                      "fdc6bb5aa9a9bac99adf85d931d6c21d1130a96e", 129336)],
            [("cdc-acm.inf", "3.0.1504.0")], ("0E8D",)),
    Package("samsung", "Samsung USB drivers 2.21.4",
            "Samsung ADB, Download (Odin) mode, modem, serial, MTP and network ports",
            "Microsoft Update Catalog (Samsung, WHQL)",
            [Download(CATALOG + "d/msdownload/update/driver/drvs/2026/05/"
                      "4e856706-1b86-40c9-a423-f91d7d705817_8f6e9f3ca9b1f1b44a86cb70216ee23a63008659.cab",
                      "8f6e9f3ca9b1f1b44a86cb70216ee23a63008659", 4950537)],
            [("ssudadb.inf", "2.21.4.0")], ("04E8",)),
    Package("mtp", "MTP / PTP (file transfer, photos)", "File transfer and camera modes", "Built into Windows",
            checks=[("wpdmtp.inf", "")], builtin=True),
    Package("tether", "USB tethering (RNDIS / NCM)", "Phone shares its internet over USB", "Built into Windows",
            checks=[("rndiscmp.inf", "")], builtin=True),
    Package("storage", "Mass storage, MIDI, USB audio", "Recovery sideload storage, MIDI mode, USB audio",
            "Built into Windows", checks=[("usbstor.inf", "")], builtin=True),
]


def work_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "AndroUnderTheHood", "drivers")
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# What is installed: the driver store is readable without admin rights
# ---------------------------------------------------------------------------
def _store_dir() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "DriverStore", "FileRepository")


def _inf_version(path: str) -> str:
    try:
        b = open(path, "rb").read(65536)
    except OSError:
        return ""
    t = b.decode("utf-16", errors="replace") if b[:2] in (b"\xff\xfe", b"\xfe\xff") else b.decode("latin-1")
    m = re.search(r"^\s*DriverVer\s*=\s*[^,\r\n]*,\s*([0-9.]+)", t, re.M | re.I)
    return m.group(1) if m else ""


def store_versions() -> dict[str, set[str]]:
    """inf name → set of DriverVer versions present in the Windows driver store."""
    res: dict[str, set[str]] = {}
    try:
        names = os.listdir(_store_dir())
    except OSError:
        return res
    for d in names:
        m = re.match(r"(.+?\.inf)_[a-z0-9]+_[0-9a-f]+$", d, re.I)
        if not m:
            continue
        inf = m.group(1).lower()
        res.setdefault(inf, set()).add(_inf_version(os.path.join(_store_dir(), d, m.group(1))))
    return res


def installed(pkg: Package, store: dict[str, set[str]]) -> bool:
    for inf, ver in pkg.checks:
        vers = store.get(inf.lower())
        if not vers or (ver and ver not in vers):
            return False
    return True


@dataclass
class UsbDevice:
    name: str
    instance: str
    status: str
    problem: bool
    vid: str
    pid: str


def connected_devices() -> list[UsbDevice]:
    """USB devices of the vendors these packages cover, plus any USB device without a driver."""
    vids = "|".join(v for p in PACKAGES for v in p.vids) + "|22D9|2A70|0BB4|12D1|2717|1004|0FCE|22B8|2A45"
    ps = ("Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match '^USB\\\\VID_(" + vids + ")' -or "
          "($_.InstanceId -like 'USB\\*' -and $_.Status -ne 'OK') } | ForEach-Object { "
          "'{0}|{1}|{2}' -f $_.Status, $_.InstanceId, $_.FriendlyName }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=60, creationflags=_NOWIN).stdout
    except Exception as e:  # noqa: BLE001
        log.info("device scan failed: %r", e)
        return []
    devs = []
    for line in out.splitlines():
        st, _, rest = line.partition("|")
        inst, _, name = rest.partition("|")
        m = re.match(r"USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", inst, re.I)
        if not m:
            continue
        devs.append(UsbDevice(name.strip() or "(unknown device)", inst.strip(), st.strip(), st.strip() != "OK",
                              m.group(1).upper(), m.group(2).upper()))
    return devs


def package_for(vid: str) -> Package | None:
    return next((p for p in PACKAGES if vid in p.vids), None)


# ---------------------------------------------------------------------------
# Download, verify, extract
# ---------------------------------------------------------------------------
def _sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(pkg: Package, say, progress=None) -> str:
    """Download + verify + extract one package. Returns its folder of INF files."""
    root = os.path.join(work_dir(), pkg.key)
    ok_mark = os.path.join(root, ".verified")
    if os.path.isfile(ok_mark):
        return root
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    for i, dl in enumerate(pkg.files):
        dest = os.path.join(work_dir(), dl.name)
        if not (os.path.isfile(dest) and _sha1(dest) == dl.sha1):
            say(f"Downloading {pkg.title} ({dl.size / 1048576:.1f} MB)…")
            tmp = dest + ".part"
            req = urllib.request.Request(dl.url, headers={"User-Agent": "AndroUnderTheHood"})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                done = 0
                while True:
                    b = r.read(1 << 16)
                    if not b:
                        break
                    f.write(b)
                    done += len(b)
                    if progress:
                        progress(pkg.key, done, dl.size)
            got = _sha1(tmp)
            if got != dl.sha1:
                os.remove(tmp)
                raise RuntimeError(f"{dl.name}: checksum mismatch (got {got}) – download refused")
            os.replace(tmp, dest)
        out = os.path.join(root, str(i))
        os.makedirs(out)
        if dest.lower().endswith(".zip"):
            with zipfile.ZipFile(dest) as z:
                z.extractall(out)
        else:
            r = subprocess.run(["expand.exe", dest, "-F:*", out], capture_output=True, text=True,
                               creationflags=_NOWIN, timeout=120)
            if r.returncode != 0:
                raise RuntimeError(f"Could not unpack {dl.name}: {r.stdout[-300:]}")
    verify_signatures(root, pkg)
    open(ok_mark, "w").close()
    return root


def verify_signatures(root: str, pkg: Package):
    cats = [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs if f.lower().endswith(".cat")]
    if not cats:
        raise RuntimeError(f"{pkg.title}: no signed catalog files found")
    lst = os.path.join(root, ".cats.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.write("\n".join(cats))
    ps = (f"foreach ($p in (Get-Content -LiteralPath '{lst}' -Encoding UTF8)) {{ "
          "$s = Get-AuthenticodeSignature -LiteralPath $p; '{0}|{1}' -f $s.Status, $s.SignerCertificate.Subject }")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                       text=True, timeout=120, creationflags=_NOWIN)
    lines = [ln for ln in r.stdout.splitlines() if "|" in ln]
    if len(lines) != len(cats):
        raise RuntimeError(f"{pkg.title}: could not check signatures: {r.stderr[-300:]}")
    for cat, ln in zip(cats, lines):
        st, _, subj = ln.partition("|")
        if st != "Valid" or not any(f"CN={s}," in subj + "," for s in SIGNERS):
            raise RuntimeError(f"{os.path.basename(cat)}: signature {st} ({subj or 'unsigned'}) – not installed")


# ---------------------------------------------------------------------------
# Install (elevated)
# ---------------------------------------------------------------------------
class _SEI(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def run_elevated(script: str) -> int:
    """Run a PowerShell script as administrator (one UAC prompt) and wait. -1 = the user said no."""
    path = os.path.join(tempfile.gettempdir(), "androunderthehood-drivers.ps1")
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(script)
    sei = _SEI()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x00000040 | 0x00000400   # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI
    sei.lpVerb = "runas"
    sei.lpFile = "powershell.exe"
    sei.lpParameters = f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{path}"'
    sei.nShow = 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        return -1                         # cancelled at the UAC prompt (ERROR_CANCELLED)
    ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return code.value


def install(folders: dict[str, str]) -> tuple[int, str]:
    """pnputil every INF of the given packages in one elevated run. Returns (exit code, log text)."""
    logf = os.path.join(work_dir(), "install.log")
    try:
        os.remove(logf)
    except OSError:
        pass
    lines = ["$ErrorActionPreference = 'Continue'", f"$log = '{logf}'",
             "function W($t) { Add-Content -LiteralPath $log -Value $t -Encoding UTF8 }"]
    for key, folder in folders.items():
        lines += [f"W '=== {key}'",
                  f"$o = & pnputil.exe /add-driver '{folder}\\*.inf' /subdirs /install 2>&1 | Out-String",
                  "W $o", "W ('@@RC ' + $LASTEXITCODE)"]
    lines += ["& pnputil.exe /scan-devices 2>&1 | Out-Null", "W '@@DONE'", "exit 0"]
    rc = run_elevated("\r\n".join(lines) + "\r\n")
    try:
        text = open(logf, encoding="utf-8-sig", errors="replace").read()
    except OSError:
        text = ""
    return rc, text
