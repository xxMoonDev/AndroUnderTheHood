# Phone desktop (no root)

Ubuntu 24.04 with KDE Plasma on an Android phone **without root**. It runs on a virtual display and you reach it with RustDesk. It is the no-root twin of the **Phone desktop** plugin: same window, same install steps, same offline bundle.

## How it works without root

- **proot instead of a chroot.** [proot](https://github.com/termux/proot) runs Ubuntu's programs under `ptrace` and fakes root, user ids and the file layout in user space. Nothing is mounted and nothing needs `su`. It is the technique Termux's `proot-distro` uses.
- **Runs as Android's shell user (uid 2000).** That user can run programs from `/data/local/tmp`, is allowed to use the network, and isn't stopped by Android's limits on background app processes. Normal apps can't run programs from their own data folder on current Android, so an app uid can't host this.
- **Everything lives in `/data/local/tmp/pdnr`** (Ubuntu in `rootfs/`, scripts and proot in `bin/`, install logs in `jobs/`). Files are staged in `/data/local/tmp/pdnr-stage`.
- **Socket files are moved to abstract sockets.** Android's SELinux policy doesn't let the shell user create Unix socket *files* (`bind` fails with "Permission denied"), but it allows abstract sockets. D-Bus, PulseAudio, RustDesk's IPC and Electron's single-instance lock all need socket files. `pdn-unixsock.so` is loaded into every program through Ubuntu's `/etc/ld.so.preload` and turns `bind`/`connect` on a socket path into an abstract socket with the same name. It also leaves an empty placeholder file at the path. Setup enables it only after a test program has run with it. D-Bus and PulseAudio still check every client with cookie files that only the shell user can read.
- **The system D-Bus runs as `messagebus` from the start.** Started as proot's fake root, it tries to drop Linux capabilities, gets "Operation not permitted" and exits.
- **The phone's storage** shows up inside Ubuntu at `/mnt/sdcard` through a proot bind. It isn't a real mount, so uninstalling can't touch it.

## Connecting

Use one of:

- **ADB:** USB debugging, or wireless debugging (Android 11+).
- **SSH with Shizuku:** the SSH server runs commands through Shizuku, which means as the shell user.

If an SSH connection runs commands as an app's own uid, the window says so and won't install. A root connection works too: every command then drops to uid 2000 with `su 2000`, so all files stay owned by the shell user.

## What's included

The same as the root version: KDE Plasma (minimal, standard or full), Xvfb, PulseAudio, RustDesk (direct IP, public servers, both, or your own server), Firefox from Mozilla's APT repository, and ElixirOffice when its arm64 `.deb` is in the files folder's `cache\`. The desktop user is `kde` with the password `kde`, which is kept if you change it.

The **files folder** defaults to `Desktop\PhoneDesktop`, the same one the root plugin and `deploy.bat` use. The Ubuntu tarball, the RustDesk and ElixirOffice `.deb`s and the offline bundle are shared. A bundle built by either plugin works for both.

## Differences from the root version

| | Root (Phone desktop) | No root (this plugin) |
|---|---|---|
| Needs | Magisk root | ADB or SSH with Shizuku |
| Speed | native | slower: every system call goes through proot. The install takes a few hours. |
| After a phone reboot | can start at boot (Magisk) | stopped; press **Start** again |
| Widget app, phone bridge | yes | no: both need root |
| Electron apps (ElixirOffice) | normal sandbox | started with `--no-sandbox` (set in its menu entry), because proot can't provide Chromium's sandbox |
| X display / RustDesk port | :1 / 21118 | :2 / 21118 by default (the port can be changed in the window) |

If both desktops are installed on one phone, they can run at the same time when they use different RustDesk ports.

## Process safety

Shell-user processes share Android's process list with your adb sessions and with Shizuku's server, which also run as uid 2000. The scripts never kill by name or by user. `pdn-stop.sh` stops only processes whose executable is this install's proot (`bin/proot`) or proot's loader copy (`tmp/prooted-*`). It refuses to run while an install or bundle job is still going.

## Files

- `device/proot`: static arm64 build of the Termux proot fork. It is rebuilt with `tools/build-proot.sh`; licences are in `licenses/`.
- `device/pd.conf`: paths and defaults. The plugin writes the chosen options to `pd.local.conf`.
- `device/pdn-proot.sh [cmd]`: runs a command in Ubuntu as Ubuntu's root, or opens an interactive shell without arguments.
- `device/pdn-install.sh`: the installer. It runs as a detached job (`pdn-job.sh`).
- `device/pdn-setup.sh`: the setup inside Ubuntu (packages, Firefox, RustDesk, ElixirOffice, user, Plasma settings).
- `device/pdn-desktop.sh`: starts the desktop inside Ubuntu and logs it out cleanly. It never kills anything.
- `device/pdn-start.sh`, `pdn-stop.sh [--now]`, `pdn-status.sh`, `pdn-rustdesk.sh`, `pdn-uninstall.sh`: the Android-side controls.
- `device/pd-packages.sh`, `pd-bundle.sh`, `pd-loginctl.sh`, `pd-shmshim.c`, `mozilla-apt.asc`: copies of the root version's files. Keep them in sync with `Desktop\PhoneDesktop\device`.

From a PC with adb, a root shell inside Ubuntu (faked by proot) is:

```
adb shell sh /data/local/tmp/pdnr/bin/pdn-proot.sh
```
