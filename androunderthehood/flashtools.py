"""Flash tools: download / add and launch the PC tools used to flash phones.

Two kinds of tools:
  * Official / open source – downloaded from where their makers publish them and verified:
      platform-tools (Google, checksum from Google's SDK manifest), Thor (Samsung Odin protocol),
      spd_dump (Unisoc), UsbDk (GitHub releases: SHA-256 digest from GitHub and/or Authenticode),
      mtkclient and edl (GitHub source + packages from PyPI, run by a private python.org Python).
  * Your own files – QPST, Odin, SP Flash Tool, MSM, Mi Flash … are never published by their makers,
    so the user adds the zip / exe / folder they got. It is unpacked into the tools folder, scanned with
    Microsoft Defender, its signature is shown, and it gets a Launch button.
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass, field

log = logging.getLogger("androunderthehood")
_NOWIN = 0x08000000 if sys.platform == "win32" else 0
UA = {"User-Agent": "AndroUnderTheHood"}


def tools_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "AndroUnderTheHood", "tools")
    os.makedirs(d, exist_ok=True)
    return d


def _state_path() -> str:
    return os.path.join(tools_dir(), "tools.json")


def load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st: dict):
    tmp = _state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, _state_path())


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------
@dataclass
class Tool:
    key: str
    name: str
    brands: str                 # chips / brands it is for
    what: str                   # one-line description
    kind: str                   # "official" or "own"
    source: str = ""            # where it comes from (official) / where people get it (own)
    exe_patterns: tuple = ()    # own tools: main program names to look for after unpacking
    console: bool = False       # command-line tool: launched in a terminal
    search: str = ""            # Google query for "where do I get it" (own tools)


OFFICIAL = [
    Tool("platform-tools", "Android platform-tools (fastboot, adb)", "All brands – Pixel, OnePlus, Xiaomi, Motorola, Nothing…",
         "Flash fastboot ROMs and images, unlock bootloaders, sideload OTAs", "official",
         "Google (Android SDK repository)", console=True),
    Tool("edl", "edl (Qualcomm EDL / Firehose)", "Qualcomm – OnePlus, OPPO, Realme, Xiaomi, Motorola, ZTE, LG…",
         "Read / write / erase partitions in Emergency Download mode (9008), open-source QFIL alternative",
         "official", "GitHub bkerler/edl + PyPI", console=True),
    Tool("mtkclient", "mtkclient (MediaTek BROM / Preloader)", "MediaTek – Xiaomi, Realme, OPPO, Vivo, Tecno, BLU, onn…",
         "Read / write partitions, unlock bootloader, bypass SLA/DAA – open-source SP Flash Tool alternative",
         "official", "GitHub bkerler/mtkclient + PyPI"),
    Tool("thor", "Thor (Samsung Odin protocol)", "Samsung",
         "Flash Samsung firmware (AP/BL/CP/CSC .tar.md5) in Download mode – open-source Odin alternative",
         "official", "GitHub Samsung-Loki/Thor", console=True),
    Tool("spd_dump", "spd_dump (Unisoc / Spreadtrum)", "Unisoc – Nokia, TCL/Alcatel, BLU, Tecno, Itel, ZTE budget phones",
         "Read / write flash through the Unisoc BootROM / FDL – open-source ResearchDownload alternative",
         "official", "GitHub ilyakurdyukov/spreadtrum_flash", console=True),
    Tool("usbdk", "UsbDk (USB access for edl / mtkclient)", "Needed by edl and mtkclient",
         "Lets edl and mtkclient talk to phones in EDL / BROM mode without replacing their drivers",
         "official", "GitHub daynix/UsbDk (signed by Red Hat)"),
]

OWN = [
    Tool("qpst", "QPST / QFIL", "Qualcomm (all brands)", "Qualcomm's own flash, NV and EFS tools", "own",
         "Qualcomm (not public)", ("QFIL.exe", "QPST Configuration.exe", "QPSTConfig.exe"), search="QPST QFIL download"),
    Tool("odin", "Odin3", "Samsung", "Samsung's firmware flash tool", "own", "Samsung (not public)",
         ("Odin3*.exe", "Odin*.exe"), search="Odin3 Samsung download"),
    Tool("spft", "SP Flash Tool", "MediaTek (all brands)", "MediaTek's scatter-file flash tool", "own",
         "MediaTek (not public)", ("flash_tool.exe", "SP_Flash_Tool*.exe"), search="SP Flash Tool download"),
    Tool("msm", "MSM Download Tool", "OPPO, Realme, OnePlus (Qualcomm)", "Factory unbrick tool for .ops / .ofp packages",
         "own", "OPPO / OnePlus (not public)", ("MsmDownloadTool*.exe", "MSMDownloadTool*.exe"),
         search="MSM Download Tool OnePlus"),
    Tool("miflash", "Mi Flash Tool", "Xiaomi, Redmi, POCO", "Xiaomi's fastboot / EDL ROM flasher", "own", "Xiaomi",
         ("XiaoMiFlash.exe", "MiFlash*.exe"), search="Xiaomi Mi Flash Tool official"),
    Tool("unisoc", "ResearchDownload / UpgradeDownload", "Unisoc / Spreadtrum – BLU, Nokia, TCL, Tecno, Itel",
         "Unisoc's .pac firmware flasher", "own", "Unisoc (not public)",
         ("ResearchDownload.exe", "UpgradeDownload.exe", "FactoryDownload.exe"), search="Unisoc ResearchDownload tool"),
    Tool("phoenix", "PhoenixSuit / LiveSuit", "Allwinner tablets and TV boxes", "Allwinner's .img firmware flasher",
         "own", "Allwinner (not public)", ("PhoenixSuit.exe", "LiveSuit.exe", "PhoenixCard.exe"),
         search="Allwinner PhoenixSuit download"),
    Tool("rkdev", "RKDevTool / FactoryTool", "Rockchip tablets and TV boxes", "Rockchip's update.img / loader flasher",
         "own", "Rockchip (not public)", ("RKDevTool.exe", "AndroidTool.exe", "FactoryTool.exe"),
         search="Rockchip RKDevTool download"),
    Tool("huawei", "Huawei / Honor tools", "Huawei, Honor", "HiSuite, Multi-Download Tool or similar", "own",
         "Huawei", ("HiSuite.exe", "MultiDownload*.exe", "*Download*.exe"), search="Huawei HiSuite download"),
    Tool("zte", "ZTE upgrade tools", "ZTE, Nubia", "ZTE's firmware upgrade / download tools", "own", "ZTE",
         ("*Upgrade*.exe", "*Download*.exe"), search="ZTE firmware upgrade tool"),
    Tool("tcl", "TCL / Alcatel Mobile Upgrade", "TCL, Alcatel", "TCL's official Mobile Upgrade tool", "own", "TCL",
         ("MobileUpgrade*.exe", "*Upgrade*.exe"), search="TCL Alcatel Mobile Upgrade S"),
    Tool("lgup", "LGUP", "LG", "LG's KDZ firmware flasher", "own", "LG (not public)", ("LGUP*.exe",),
         search="LGUP LG firmware tool"),
    Tool("moto", "Motorola Rescue (LMSA / RSD Lite)", "Motorola, Lenovo", "Motorola's rescue / flash tools", "own",
         "Lenovo / Motorola", ("Software Fix.exe", "RSD*.exe", "*Rescue*.exe"),
         search="Motorola Rescue and Smart Assistant download"),
    Tool("other", "Other tool", "Any brand", "Any other flasher you have (e.g. BLU, Infinix, Sony, Nokia tools)",
         "own", "", ("*.exe",)),
]
ALL = OFFICIAL + OWN


def by_key(key: str) -> Tool:
    return next(t for t in ALL if t.key == key)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _get(url: str, timeout=60) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def _download(url: str, dest: str, say, label: str, size: int = 0, progress=None):
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        total = size or int(r.headers.get("Content-Length") or 0)
        say(f"Downloading {label}" + (f" ({total / 1048576:.1f} MB)…" if total else "…"))
        done = 0
        while True:
            b = r.read(1 << 16)
            if not b:
                break
            f.write(b)
            done += len(b)
            if progress:
                progress(done, total)
    os.replace(tmp, dest)


def _hash(path: str, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def signature(path: str) -> tuple[str, str]:
    """(status, signer) from Windows' Authenticode check."""
    ps = (f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; "
          "'{0}|{1}' -f $s.Status, ($s.SignerCertificate.Subject -replace '^CN=\"?([^\",]+).*','$1')")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=60, creationflags=_NOWIN).stdout.strip()
    except Exception:  # noqa: BLE001
        return "Unknown", ""
    st, _, signer = out.partition("|")
    return st or "Unknown", signer


def _gh_latest(repo: str) -> dict:
    return json.loads(_get(f"https://api.github.com/repos/{repo}/releases/latest"))


def _gh_asset(repo: str, name_re: str) -> tuple[str, str, str, int]:
    """(tag, url, sha256 or '', size) of the newest release asset matching name_re."""
    rel = _gh_latest(repo)
    for a in rel.get("assets", []):
        if re.fullmatch(name_re, a["name"]):
            dig = a.get("digest") or ""
            return rel["tag_name"], a["browser_download_url"], dig[7:] if dig.startswith("sha256:") else "", a["size"]
    raise RuntimeError(f"{repo}: no release file matching {name_re}")


def _extract(archive: str, dest: str):
    os.makedirs(dest, exist_ok=True)
    if archive.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
        return
    # 7z / rar / tar: Windows' tar (libarchive) reads them
    r = subprocess.run(["tar", "-xf", archive, "-C", dest], capture_output=True, text=True, creationflags=_NOWIN)
    if r.returncode != 0:
        raise RuntimeError(f"Could not unpack {os.path.basename(archive)}: {(r.stderr or r.stdout)[-300:]}")


def _flatten(dest: str):
    """If an archive unpacked into a single folder, move its contents up one level."""
    items = [i for i in os.listdir(dest) if not i.startswith(".")]
    if len(items) == 1 and os.path.isdir(os.path.join(dest, items[0])):
        inner = os.path.join(dest, items[0])
        for i in os.listdir(inner):
            shutil.move(os.path.join(inner, i), os.path.join(dest, i))
        os.rmdir(inner)


# ---------------------------------------------------------------------------
# Python runtime (for edl / mtkclient)
# ---------------------------------------------------------------------------
PY_VER = "3.12.10"


def python_dir() -> str:
    return os.path.join(tools_dir(), "python")


def python_exe() -> str:
    return os.path.join(python_dir(), "python.exe")


def _ensure_build_tools(say):
    """setuptools + wheel in the private Python: some packages (e.g. docopt) only ship as source and must be
    built. Pip's isolated build environment doesn't work in an embeddable Python (its ._pth file fixes
    sys.path), so builds run with --no-build-isolation against these."""
    ok = os.path.join(python_dir(), ".build-ok")
    if os.path.isfile(ok):
        return
    say("Installing Python build tools (setuptools, wheel)…")
    _run([python_exe(), "-m", "pip", "install", "--no-warn-script-location", "--upgrade", "setuptools", "wheel"],
         "build tools")
    open(ok, "w").close()


def ensure_python(say, progress=None):
    if os.path.isfile(python_exe()) and os.path.isfile(os.path.join(python_dir(), ".pip-ok")):
        _ensure_build_tools(say)
        return
    d = python_dir()
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    z = os.path.join(tools_dir(), f"python-{PY_VER}-embed-amd64.zip")
    _download(f"https://www.python.org/ftp/python/{PY_VER}/python-{PY_VER}-embed-amd64.zip", z, say,
              f"Python {PY_VER} (python.org)", progress=progress)
    _extract(z, d)
    os.remove(z)
    st, signer = signature(python_exe())
    if st != "Valid" or signer != "Python Software Foundation":
        shutil.rmtree(d, ignore_errors=True)
        raise RuntimeError(f"python.exe signature {st} ({signer or 'unsigned'}) – refused")
    for pth in glob.glob(os.path.join(d, "python3*._pth")):   # enable site-packages + pip
        with open(pth, "w", encoding="utf-8") as f:
            f.write(f"{os.path.basename(pth)[:-5]}.zip\n.\nLib\\site-packages\nimport site\n")
    gp = os.path.join(d, "get-pip.py")
    _download("https://bootstrap.pypa.io/get-pip.py", gp, say, "pip (pypa.io)")
    say("Setting up pip…")
    _run([python_exe(), gp, "--no-warn-script-location", "--disable-pip-version-check"], "pip setup")
    os.remove(gp)
    open(os.path.join(d, ".pip-ok"), "w").close()
    _ensure_build_tools(say)


def _run(cmd: list[str], what: str, cwd: str | None = None, timeout=1800) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, creationflags=_NOWIN,
                       env={**os.environ, "PYTHONNOUSERSITE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    if r.returncode != 0:
        raise RuntimeError(f"{what} failed: {(r.stderr or r.stdout)[-600:]}")
    return r.stdout


def _python_tool(key: str, repo: str, ref_api: str, say, progress, skip=("flake8", "wheel", "setuptools")):
    ensure_python(say, progress)
    meta = json.loads(_get(f"https://api.github.com/repos/{repo}/{ref_api}"))
    if isinstance(meta, list):
        meta = meta[0]
    ref = meta.get("name") or meta.get("sha")            # tag name, or commit sha
    sha = meta.get("commit", {}).get("sha") or meta.get("sha")
    d = os.path.join(tools_dir(), key)
    shutil.rmtree(d, ignore_errors=True)
    z = os.path.join(tools_dir(), f"{key}.zip")
    _download(f"https://github.com/{repo}/archive/{sha}.zip", z, say, f"{key} {ref} (GitHub)", progress=progress)
    _extract(z, d)
    os.remove(z)
    _flatten(d)
    req = os.path.join(d, "requirements.txt")
    pkgs = []
    if os.path.isfile(req):
        for ln in open(req, encoding="utf-8"):
            ln = ln.split("#")[0].strip()
            if ln and re.split(r"[<>=!~ \[]", ln)[0].lower() not in skip:
                pkgs.append(ln)
    say(f"Installing {key}'s Python packages from PyPI (this can take a few minutes)…")
    _run([python_exe(), "-m", "pip", "install", "--no-warn-script-location", "--prefer-binary",
          "--no-build-isolation", "--target", os.path.join(d, "_lib"), *pkgs], f"{key} packages")
    return ref


# ---------------------------------------------------------------------------
# Install official tools
# ---------------------------------------------------------------------------
def install_official(key: str, say, progress=None) -> dict:
    """Download, verify and unpack one official tool. Returns its state entry."""
    root = os.path.join(tools_dir(), key)
    if key == "platform-tools":
        xml = _get("https://dl.google.com/android/repository/repository2-3.xml").decode("utf-8", "replace")
        i = xml.find('path="platform-tools"')
        blk = xml[i:xml.find("</remotePackage>", i)]
        ver = ".".join(re.findall(r"<(?:major|minor|micro)>(\d+)<", blk)[:3])
        m = re.search(r"<size>(\d+)</size>\s*<checksum[^>]*>(\w+)</checksum>\s*<url>(platform-tools_[^<]*-win\.zip)</url>",
                      blk)
        if not m:
            raise RuntimeError("platform-tools for Windows not found in Google's repository")
        size, sha1, fn = int(m.group(1)), m.group(2), m.group(3)
        z = os.path.join(tools_dir(), fn)
        _download("https://dl.google.com/android/repository/" + fn, z, say, f"platform-tools {ver}", size, progress)
        if _hash(z, "sha1") != sha1:
            os.remove(z)
            raise RuntimeError("platform-tools: checksum mismatch – refused")
        shutil.rmtree(root, ignore_errors=True)
        _extract(z, root)
        os.remove(z)
        _flatten(root)
        return {"version": ver, "exe": os.path.join(root, "fastboot.exe"), "verified": f"SHA-1 from Google ({sha1[:12]}…)"}
    if key in ("thor", "spd_dump", "usbdk"):
        repo, pat = {"thor": ("Samsung-Loki/Thor", r"Thor-Windows\.exe"),
                     "spd_dump": ("ilyakurdyukov/spreadtrum_flash", r"spd_dump64\.zip"),
                     "usbdk": ("daynix/UsbDk", r"UsbDk_[\d.]+_x64\.msi")}[key]
        tag, url, sha256, size = _gh_asset(repo, pat)
        shutil.rmtree(root, ignore_errors=True)
        os.makedirs(root)
        dest = os.path.join(root, url.rsplit("/", 1)[-1])
        _download(url, dest, say, f"{key} {tag}", size, progress)
        how = []
        if sha256:
            if _hash(dest, "sha256") != sha256:
                shutil.rmtree(root, ignore_errors=True)
                raise RuntimeError(f"{key}: SHA-256 does not match GitHub's – refused")
            how.append(f"SHA-256 from GitHub ({sha256[:12]}…)")
        if dest.lower().endswith((".exe", ".msi")):
            st, signer = signature(dest)
            if key == "usbdk" and (st != "Valid" or "Red Hat" not in signer):
                shutil.rmtree(root, ignore_errors=True)
                raise RuntimeError(f"UsbDk installer signature {st} ({signer or 'unsigned'}) – refused")
            how.append(f"signed by {signer}" if st == "Valid" else "unsigned (open source)")
        if not how:
            raise RuntimeError(f"{key}: nothing to verify the download against – refused")
        exe = dest
        if dest.lower().endswith(".zip"):
            _extract(dest, root)
            os.remove(dest)
            _flatten(root)
            exe = os.path.join(root, "spd_dump.exe")
        return {"version": tag, "exe": exe, "verified": ", ".join(how)}
    if key == "mtkclient":
        ref = _python_tool("mtkclient", "bkerler/mtkclient", "tags?per_page=1", say, progress)
        return {"version": ref, "exe": os.path.join(root, "mtk_gui.py"), "verified": "GitHub source (HTTPS) + PyPI"}
    if key == "edl":
        ref = _python_tool("edl", "bkerler/edl", "commits?per_page=1", say, progress)
        return {"version": ref[:10], "exe": os.path.join(root, "edl.py"), "verified": "GitHub source (HTTPS) + PyPI"}
    raise KeyError(key)


def usbdk_installed() -> bool:
    return os.path.isfile(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "UsbDk Runtime Library",
                                       "UsbDkController.exe"))


# ---------------------------------------------------------------------------
# Your own tools
# ---------------------------------------------------------------------------
def find_exes(root: str, patterns: tuple) -> list[str]:
    hits = []
    for pat in patterns:
        for p in glob.glob(os.path.join(root, "**", pat), recursive=True):
            if p not in hits and not re.search(r"(uninst|setup|vcredist|dotnet|driver)", os.path.basename(p), re.I):
                hits.append(p)
    return sorted(hits, key=lambda p: (p.count(os.sep), len(p)))


def add_own(key: str, src: str, say) -> dict:
    """Add a tool the user downloaded: archive → unpacked into tools/<key>; exe or folder → used where it is."""
    t = by_key(key)
    if os.path.isdir(src):
        root, copied = src, False
    elif src.lower().endswith((".zip", ".7z", ".rar", ".tar", ".tgz", ".gz")):
        root = os.path.join(tools_dir(), key)
        shutil.rmtree(root, ignore_errors=True)
        say(f"Unpacking {os.path.basename(src)}…")
        _extract(src, root)
        _flatten(root)
        copied = True
    elif src.lower().endswith((".exe", ".bat", ".cmd")):
        return {"version": "", "exe": src, "root": os.path.dirname(src), "copied": False}
    else:
        raise RuntimeError("Choose a .zip / .7z / .rar archive, a program (.exe) or a folder")
    exes = find_exes(root, t.exe_patterns) or find_exes(root, ("*.exe",))
    return {"version": "", "exe": exes[0] if exes else "", "root": root, "copied": copied, "candidates": exes[:30]}


def defender_scan(path: str) -> tuple[bool | None, str]:
    """Scan a file / folder with Microsoft Defender. (clean?, message); None = Defender not available."""
    plat = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Windows Defender", "Platform")
    cands = [os.path.join(plat, "CurrentVersion", "MpCmdRun.exe")]
    cands += sorted(glob.glob(os.path.join(plat, "*", "MpCmdRun.exe")), reverse=True)
    cands.append(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Windows Defender", "MpCmdRun.exe"))
    exe = next((c for c in cands if os.path.isfile(c)), None)
    if not exe:
        return None, "Microsoft Defender is not available"
    r = subprocess.run([exe, "-Scan", "-ScanType", "3", "-File", path, "-DisableRemediation"],
                       capture_output=True, text=True, timeout=1800, creationflags=_NOWIN)
    out = (r.stdout or "") + (r.stderr or "")
    if re.search(r"found no threats", out, re.I):
        return True, "Microsoft Defender: no threats found"
    if r.returncode == 2 and re.search(r"threat", out, re.I):
        found = [ln.strip() for ln in out.splitlines() if re.search(r"threat|file:", ln, re.I)]
        return False, "Microsoft Defender found threats:\n" + "\n".join(found[:10])
    if "0x80508023" in out:
        return None, "Defender did not scan it: that location is excluded from (or not available to) Defender scans"
    last = out.strip().splitlines()[-1] if out.strip() else f"code {r.returncode}"
    return None, f"Defender scan did not complete: {last[:200]}"


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------
def cli_path_dirs(state: dict) -> list[str]:
    """Folders to put on PATH in the flash terminal."""
    dirs = []
    for key in ("platform-tools", "spd_dump", "thor"):
        e = state.get(key, {}).get("exe")
        if e and os.path.isfile(e):
            dirs.append(os.path.dirname(e))
    shims = os.path.join(tools_dir(), "_shims")
    if os.path.isdir(shims):
        dirs.append(shims)
    return dirs


def write_shims(state: dict):
    """edl.cmd / mtk.cmd / thor.cmd wrappers so the terminal can just type the tool's name."""
    d = os.path.join(tools_dir(), "_shims")
    os.makedirs(d, exist_ok=True)
    py = python_exe()
    for key, script, name in (("edl", "edl.py", "edl"), ("mtkclient", "mtk.py", "mtk"),
                              ("mtkclient", "mtk_gui.py", "mtk_gui")):
        root = os.path.join(tools_dir(), key)
        path = os.path.join(d, f"{name}.cmd")
        if key in state and os.path.isfile(os.path.join(root, script)):
            with open(path, "w", encoding="utf-8") as f:
                f.write(f'@echo off\r\nset "PYTHONPATH={root}\\_lib;{root}"\r\nset PYTHONNOUSERSITE=1\r\n'
                        f'"{py}" "{root}\\{script}" %*\r\n')
        elif os.path.exists(path):
            os.remove(path)
    thor = state.get("thor", {}).get("exe")
    if thor and os.path.isfile(thor):
        with open(os.path.join(d, "thor.cmd"), "w", encoding="utf-8") as f:
            f.write(f'@"{thor}" %*\r\n')


def open_terminal(state: dict, cwd: str | None = None, banner: str = ""):
    write_shims(state)
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(cli_path_dirs(state) + [env.get("PATH", "")])
    names = []
    if "platform-tools" in state:
        names += ["fastboot", "adb"]
    if "edl" in state:
        names.append("edl")
    if "mtkclient" in state:
        names += ["mtk", "mtk_gui"]
    if "thor" in state:
        names.append("thor")
    if "spd_dump" in state:
        names.append("spd_dump")
    msg = banner or ("AndroUnderTheHood flash terminal – available: " + (", ".join(names) or "(no tools installed)"))
    subprocess.Popen(["cmd.exe", "/k", f"title AndroUnderTheHood flash terminal & echo {msg}"],
                     cwd=cwd or os.path.expanduser("~"), env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)


def launch(key: str, state: dict):
    t = by_key(key)
    ent = state.get(key, {})
    exe = ent.get("exe", "")
    if key == "usbdk":
        raise RuntimeError("UsbDk is installed with its own Install button")
    if key == "mtkclient":
        write_shims(state)
        env = {**os.environ, "PYTHONPATH": f"{os.path.join(tools_dir(), key, '_lib')};{os.path.join(tools_dir(), key)}",
               "PYTHONNOUSERSITE": "1"}
        pyw = os.path.join(python_dir(), "pythonw.exe")
        subprocess.Popen([pyw, exe], cwd=os.path.dirname(exe), env=env)
        return
    if t.console:
        open_terminal(state, cwd=os.path.dirname(exe) if key == "platform-tools" else None,
                      banner=f"{t.name}: type {os.path.splitext(os.path.basename(exe))[0] if key != 'edl' else 'edl'}"
                             f" --help to start. All installed flash tools are on PATH here.")
        return
    if not exe or not os.path.isfile(exe):
        raise RuntimeError("The program was not found – add it again")
    # ShellExecute (not CreateProcess): honours a tool's own "run as administrator" manifest; start it in its
    # folder because many flashers load their configs / DA files relative to the working directory
    import ctypes
    rc = ctypes.windll.shell32.ShellExecuteW(None, "open", exe, None, os.path.dirname(exe), 1)
    if rc <= 32:
        raise RuntimeError(f"Windows could not start {os.path.basename(exe)} (code {rc})")


def install_usbdk(state: dict) -> tuple[int, str]:
    """Install the downloaded, signature-checked UsbDk MSI (one UAC prompt)."""
    from .drivers import run_elevated
    msi = state.get("usbdk", {}).get("exe", "")
    if not msi or not os.path.isfile(msi):
        raise RuntimeError("Download UsbDk first")
    logf = os.path.join(tools_dir(), "usbdk-install.log")
    rc = run_elevated(f"$p = Start-Process msiexec.exe -ArgumentList '/i \"{msi}\" /qn /norestart /l*v \"{logf}\"' "
                      "-Wait -PassThru; exit $p.ExitCode\r\n")
    return rc, logf
