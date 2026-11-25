r"""Build dist\AndroUnderTheHood with PyInstaller.

Besides the app itself it bundles the whole Python standard library and a few extra Qt modules, so
plugins can import them, packs every folder in plugins\ into a .7z package next to the exe (plugins are
read into memory from those packages, never unpacked), and copies PLUGINS.md.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "AndroUnderTheHood"
ASSETS = ["androunderthehood-helper.jar", "AndroOpenHeart.apk", "Shizuku.apk"]

# stdlib modules plugins can't use from a GUI app on Windows, or that are huge and useless here
SKIP = {"tkinter", "turtle", "turtledemo", "idlelib", "test", "lib2to3", "ensurepip", "venv", "pydoc_data",
        "distutils", "curses", "readline", "tty", "pty", "termios", "fcntl", "grp", "pwd", "resource", "syslog",
        "posix", "nis", "spwd", "crypt", "ossaudiodev", "_curses", "_curses_panel", "_tkinter", "msvcrt_",
        "__phello__", "_testcapi", "_testinternalcapi", "_testbuffer", "_testimportmultiple",
        "_testmultiphase", "_testsinglephase", "_xxtestfuzz", "_xxsubinterpreters", "xxsubtype", "antigravity",
        "this", "_pyrepl", "_aix_support", "_osx_support", "_android_support", "_ios_support", "pydoc"}
PACKAGES = {"asyncio", "collections", "concurrent", "ctypes", "dbm", "email", "encodings", "html", "http",
            "importlib", "json", "logging", "multiprocessing", "sqlite3", "tomllib", "unittest", "urllib", "wsgiref",
            "xml", "xmlrpc", "zipfile", "zoneinfo", "re", "sysconfig", "pathlib"}
QT_EXTRA = ["PySide6.QtNetwork", "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtCharts",
            "PySide6.QtPrintSupport", "PySide6.QtConcurrent"]


def check_dist_plugins():
    """PyInstaller recreates dist\\AndroUnderTheHood – refuse to build while it holds files we didn't create."""
    d = os.path.join(HERE, "dist", NAME, "plugins")
    if not os.path.isdir(d):
        return
    # Windows file names are case-insensitive: Activity_Log.7z is the package built from pluginsctivity_log
    ours = {f"{x}.7z".lower() for x in os.listdir(os.path.join(HERE, "plugins"))
            if os.path.isfile(os.path.join(HERE, "plugins", x, "plugin.json"))}
    extra = [f for f in os.listdir(d) if f.lower() not in ours]
    if extra:
        print("BUILD STOPPED: these files in", d, "would be deleted by the build:")
        for f in extra:
            print("   ", f)
        print("Move them somewhere else first (installed plugins belong in %APPDATA%\\AndroUnderTheHood\\plugins).")
        sys.exit(2)


# Python packages that end up inside the exe: their license files are copied into dist/licenses
BUNDLED = ["PySide6_Essentials", "PySide6_Addons", "shiboken6", "paramiko", "pillow", "pi-heif", "imageio-ffmpeg",
           "py7zr", "pycryptodomex", "pyppmd", "pybcj", "inflate64", "multivolumefile", "backports.zstd", "brotli",
           "texttable", "psutil", "cryptography", "bcrypt", "PyNaCl", "cffi", "pycparser", "invoke", "pyinstaller"]


def collect_licenses(dist):
    """Copy the license / notice files of everything bundled into dist/licenses."""
    import importlib.metadata as md
    import re
    out = os.path.join(dist, "licenses")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    for name in BUNDLED:
        try:
            files = md.files(name) or []
        except md.PackageNotFoundError:
            continue
        for f in files:
            if ".dist-info" in str(f) and re.search(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", f.name, re.I):
                d = os.path.join(out, name)
                os.makedirs(d, exist_ok=True)
                shutil.copy2(f.locate(), os.path.join(d, f.name))
    py = os.path.join(sys.base_prefix, "LICENSE.txt")
    if os.path.isfile(py):
        shutil.copy2(py, os.path.join(out, "Python-LICENSE.txt"))
    src = os.path.join(HERE, "licenses")
    if os.path.isdir(src):
        for f in os.listdir(src):
            shutil.copy2(os.path.join(src, f), os.path.join(out, f))
    with open(os.path.join(out, "FFmpeg-NOTICE.txt"), "w", encoding="utf-8") as f:
        f.write("imageio_ffmpeg\\binaries\\ffmpeg-*.exe is an FFmpeg build by gyan.dev (\"essentials\" build),\n"
                "configured with --enable-gpl --enable-version3. It is licensed under the GNU General Public\n"
                "License version 3 (see GPL-3.0-only.txt). AndroUnderTheHood runs it as a separate program.\n\n"
                "Source code: https://ffmpeg.org/download.html  and the build scripts / exact sources at\n"
                "https://www.gyan.dev/ffmpeg/builds/ (see the build's version for the matching source release).\n")
    for f in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        if os.path.isfile(os.path.join(HERE, f)):
            shutil.copy2(os.path.join(HERE, f), os.path.join(dist, f))


def main():
    check_dist_plugins()
    args = [os.path.join(HERE, ".venv", "Scripts", "pyinstaller.exe"), "--noconfirm", "--windowed", "--name", NAME,
            "--collect-binaries", "imageio_ffmpeg", "--collect-all", "pi_heif",
            # 7z plugin packages (AES, PPMd, BCJ, zstd, brotli, deflate64 codecs)
            "--collect-all", "py7zr", "--collect-all", "pyppmd", "--collect-all", "bcj",
            "--collect-all", "inflate64", "--collect-all", "multivolumefile", "--collect-all", "Cryptodome",
            "--collect-all", "backports.zstd", "--hidden-import", "brotli", "--hidden-import", "_brotli",
            "--hidden-import", "texttable", "--hidden-import", "psutil",
            "--exclude-module", "tkinter", "--exclude-module", "PySide6.QtWebEngineCore",
            "--exclude-module", "PySide6.QtQml", "--exclude-module", "PySide6.Qt3DCore"]
    for a in ASSETS:
        args += ["--add-data", f"androunderthehood\\assets\\{a};androunderthehood\\assets"]
    for m in sorted(sys.stdlib_module_names):
        if m in SKIP or m.startswith("_test"):
            continue
        args += ["--hidden-import", m]
        if m in PACKAGES:
            args += ["--collect-submodules", m]
    for m in QT_EXTRA:
        args += ["--hidden-import", m]
    args.append("run.py")
    r = subprocess.run(args, cwd=HERE)
    if r.returncode:
        sys.exit(r.returncode)
    dist = os.path.join(HERE, "dist", NAME)
    src = os.path.join(HERE, "plugins")
    if os.path.isdir(src):
        sys.path.insert(0, HERE)
        from androunderthehood.pluginpack import pack
        dst = os.path.join(dist, "plugins")
        shutil.rmtree(dst, ignore_errors=True)
        os.makedirs(dst)
        for d in sorted(os.listdir(src)):
            if os.path.isfile(os.path.join(src, d, "plugin.json")):
                pack(os.path.join(src, d), os.path.join(dst, f"{d}.7z"))
                print("packed plugin", d)
    collect_licenses(dist)
    for f in ("PLUGINS.md",):
        if os.path.isfile(os.path.join(HERE, f)):
            shutil.copy2(os.path.join(HERE, f), os.path.join(dist, f))
    print("Build complete:", dist)


if __name__ == "__main__":
    main()
