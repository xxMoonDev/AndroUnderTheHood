# AndroUnderTheHood

A Windows toolbox for Android phones and tablets. It connects over **SSH** (Wi-Fi, VPN or a USB port forward) or **ADB** (USB or adb over Wi-Fi) and gives you a file browser with thumbnails plus tools for apps, partitions, root, logs, the modem, hardware tests and more. It works with or without root. Features that need root say so and stay greyed out otherwise.

The device side is usually [AndroOpenHeart](https://github.com/xxMoonDev/AndroOpenHeart), an SSH server app that AndroUnderTheHood can install and set up over ADB in one click. Any SSH server works (Termux sshd, SimpleSSHD, Magisk SSH modules).

## Download

Get the Windows build from the [Releases](https://github.com/xxMoonDev/AndroUnderTheHood/releases) page, unzip it, and run `AndroUnderTheHood.exe`. Keep the whole folder together; the plugins are in `plugins\`.

## Connecting

- **SSH:** host, port, user, and a password or private key (the Windows ssh-agent works too). Passwords are never saved. On the first connection you confirm the device's host key fingerprint, and you're warned if it changes later (`%APPDATA%\AndroUnderTheHood\known_hosts`).
  - **Access mode:** *SFTP* when the server has an SFTP subsystem, or *Shell commands* for servers without one (AndroOpenHeart, most rooted setups). Shell mode uses toybox `stat`/`cat`/`tar` over the session.
  - **Root** toggle: runs everything through `su` (or asks AndroOpenHeart for a root session).
- **ADB:** pick a device in the ADB dialog (USB, or `192.168.x.x:5555` for adb over Wi-Fi). Most features work the same over ADB.
- **Set up AndroOpenHeart:** from the ADB dialog. It installs AndroOpenHeart and Shizuku if needed, authorizes this PC's SSH key, grants permissions, starts the server and saves an SSH profile. It works without root (through Shizuku), and can prepare Shizuku to start on boot.

## What's inside

**File browser**
- Thumbnail grid and details view (Ctrl+1 / Ctrl+2). Thumbnails for photos (JPEG, PNG, WebP, GIF, HEIC…), videos and album art (ffmpeg reads only the parts it needs), and APK icons. Cached in `%LOCALAPPDATA%\AndroUnderTheHood\thumbs`.
- Properties panel: permissions, owner and group, symlink target, EXIF and GPS, video/audio details, APK info, text preview, folder size.
- Download, open, upload (also drag and drop from Explorer), new folder, rename, delete, copy path, bookmarks and shortcuts.

**Device tools** (toolbar and the More menu)
- **Apps:** list, back up, install, uninstall, permissions and App Ops, activity launcher.
- **Device:** hardware info, boot and security state, reboot modes, ADB and USB controls.
- **Partitions:** view, back up, flash and mount partitions.
- **Root manager:** Magisk, Kitsune Mask, KernelSU (and forks) and APatch: modules, superuser list, deny list.
- **Logcat** viewer, **Run command** console, **system properties** editor (getprop / setprop / resetprop), **kernel tunables** explorer (/sys and /proc/sys), **hex editor** with a binwalk-style scanner.
- **Modem:** Qualcomm DIAG logging over the device's own `diag_socket_log`, without a USB diag port.
- **Screen mirror** through [scrcpy-ssh](https://github.com/xxMoonDev/scrcpy-ssh) (scrcpy over SSH, no ADB needed).
- **USB drivers:** Google, Qualcomm, MediaTek and Samsung drivers from their official sources, checked (SHA-1 and Authenticode) before installing.
- **Flash tools:** downloads official and open-source flashing tools (platform-tools, Thor, spd_dump, UsbDk, edl, mtkclient) and launches tools you add yourself.

**Plugins** (in `plugins\`, each one can be turned off)

| Plugin | What it does |
|---|---|
| Accelerometer test | Every acceleration sensor: live graph, tilt level, noise, six-side calibration test |
| Activity log | Which app and screen is used and every tap on it. Typed text is never logged. |
| App tools | APK inspector, app data snapshots, notification log |
| Battery & checksums | Live battery window, battery-drain analyser, SHA-256 checksum in the file menu |
| Camera test | Every camera, including hidden and physical sensors, streamed live |
| Cell, Wi-Fi & Bluetooth | Modem, SIMs, cells and signal, Wi-Fi and Bluetooth details and controls |
| Display test | Colours, dead-pixel check, multi-touch coverage, refresh rate (installs a small test app) |
| GPS & location test | GNSS hardware, fixes on a map, sky plot, GPX/CSV export |
| Microphone test | Every microphone: level meters, spectrum, listen on the PC, record to WAV |
| Network control | Per-app firewall, port forwards and tunnels, packet capture to .pcap |
| Phone desktop | Ubuntu + KDE Plasma on the phone, reached with RustDesk (see [PhoneDesktop](https://github.com/xxMoonDev/PhoneDesktop)) |
| Phone desktop (no root) | The older separate no-root version; Phone desktop 1.5+ includes a no-root mode |
| Play Integrity | Installs and configures Tricky Store, Play Integrity Fork / Fix and a Zygisk provider |
| Reset & Restore | Full backup, factory reset, guided setup, restore of apps, data and settings |
| Root workbench | Boot image tool, Magisk module workbench, SELinux denial viewer |
| Speaker test | Every speaker and amplifier, sine sweep per channel |
| System diff | Snapshots of properties, settings and package versions, and what changed |
| Task Manager | Processes, performance graphs, resource monitor, end task / kill / priority |

Writing your own plugin: see [PLUGINS.md](PLUGINS.md). Plugins are shipped as `.7z` packages (optionally password protected) and loaded from memory without unpacking.

## Be careful

Many tools change the device: flashing partitions, factory reset, firewall rules, root modules, system properties. Back up first and make sure you know what a button does. Everything is provided as is, without warranty (see the license).

## Running from source

Needs Python 3.12 on Windows.

```
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python run.py
```

`build.bat` produces `dist\AndroUnderTheHood\AndroUnderTheHood.exe`, packs every plugin folder into `dist\AndroUnderTheHood\plugins\*.7z` and collects the license files of everything it bundles into `dist\AndroUnderTheHood\licenses`.

Layout:

- `androunderthehood\`: the app. `backend.py` (SSH, SFTP and shell file systems), `adbconn.py` (ADB transport), `ui.py` (main window), one module per tool.
- `androunderthehood\assets\`: `AndroOpenHeart.apk` and `Shizuku.apk` (installed by the setup) and `androunderthehood-helper.jar` (built from `helper\AndroUnderTheHoodHelper.java`).
- `plugins\<id>\`: plugin sources. Plugins with an Android helper keep its Java source in `helper\` with a `build_helper.sh` (javac + d8 from the Android SDK). The display test app's source is `helper\displaytest-app`.

## Credits

AndroUnderTheHood uses these projects. The license texts of everything inside the Windows build are in its `licenses\` folder.

**Bundled in the Windows build**
- [Qt for Python (PySide6)](https://wiki.qt.io/Qt_for_Python) (LGPL-3.0): the user interface.
- [Paramiko](https://github.com/paramiko/paramiko) (LGPL-2.1): SSH and SFTP.
- [Pillow](https://github.com/python-pillow/Pillow) (MIT-CMU) and [pi-heif](https://github.com/bigcat88/pillow_heif) (BSD-3-Clause, libheif LGPL-3.0): images and HEIC thumbnails.
- [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) (BSD-2-Clause), with an [FFmpeg](https://ffmpeg.org/) build by [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) (GPL-3.0, run as a separate program): video thumbnails and details.
- [py7zr](https://github.com/miurahr/py7zr) (LGPL-2.1) with pyppmd, pybcj, inflate64, multivolumefile, PyCryptodome and backports.zstd: plugin packages.
- [cryptography](https://github.com/pyca/cryptography) (Apache-2.0 or BSD-3-Clause), bcrypt, PyNaCl, cffi, psutil, texttable.
- [PyInstaller](https://github.com/pyinstaller/pyinstaller) (GPL-2.0 with the bootloader exception): builds the exe.
- [Shizuku](https://github.com/RikkaApps/Shizuku) by RikkaApps (Apache-2.0): `Shizuku.apk`, installed on devices without root.

**Used, downloaded or started on request**
- [scrcpy](https://github.com/Genymobile/scrcpy) by Romain Vimont / Genymobile (Apache-2.0), through the [scrcpy-ssh](https://github.com/xxMoonDev/scrcpy-ssh) fork. The fake-context trick the plugin helpers use to call Android services from `app_process` comes from scrcpy's server.
- [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools) and the Google USB Driver (Google).
- Flash tools: [Thor](https://github.com/Samsung-Loki/Thor), [spreadtrum_flash / spd_dump](https://github.com/ilyakurdyukov/spreadtrum_flash) by Ilya Kurdyukov, [UsbDk](https://github.com/daynix/UsbDk) by Daynix, [edl](https://github.com/bkerler/edl) and [mtkclient](https://github.com/bkerler/mtkclient) by Bjoern Kerler, and the [Python](https://www.python.org/) embeddable package that runs the last two.
- Root managers it talks to: [Magisk](https://github.com/topjohnwu/Magisk) by topjohnwu, [KernelSU](https://github.com/tiann/KernelSU), [APatch](https://github.com/bmax121/APatch) and Kitsune Mask.
- Play Integrity plugin: [TrickyStoreOSS](https://github.com/beakthoven/TrickyStoreOSS), [TrickyStore](https://github.com/5ec1cff/TrickyStore), [TEESimulator](https://github.com/JingMatrix/TEESimulator), [PlayIntegrityFork](https://github.com/osm0sis/PlayIntegrityFork) by osm0sis, [PlayIntegrityFix](https://github.com/KOWX712/PlayIntegrityFix), [ZygiskNext](https://github.com/Dr-TSNG/ZygiskNext), [NeoZygisk](https://github.com/JingMatrix/NeoZygisk), [ReZygisk](https://github.com/PerformanC/ReZygisk), [Zygisk-Assistant](https://github.com/snake-4/Zygisk-Assistant), and [SPIC](https://github.com/herzhenr/spic-android) for verdict checks. They're downloaded from their own GitHub releases.
- Phone desktop plugins: see [PhoneDesktop](https://github.com/xxMoonDev/PhoneDesktop#credits) (Ubuntu, KDE, RustDesk, Termux's PRoot and more). The no-root plugin ships a static [PRoot](https://github.com/termux/proot) build (GPL-2.0) and talloc (LGPL-3.0); see `plugins\phone_desktop_noroot\licenses`.
- GPS plugin maps: © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (tiles from tile.openstreetmap.org, shown with attribution).
- USB driver packages come from the [Microsoft Update Catalog](https://www.catalog.update.microsoft.com/) (Qualcomm, MediaTek, Samsung).

## License

Copyright 2026 xxMoonDev. Licensed under the [Apache License 2.0](LICENSE). Bundled and downloaded third-party software keeps its own license.
