"""Screen mirroring through scrcpy-ssh (scrcpy fork that uses SSH instead of adb)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .backend import Profile, app_dir

_NOWIN = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW

# (config key, menu label, scrcpy arguments)
MIRROR_OPTIONS = [
    ("turn_screen_off", "Turn device screen off while mirroring", ["--turn-screen-off"]),
    ("stay_awake", "Keep device awake", ["--stay-awake"]),
    ("no_audio", "No audio", ["--no-audio"]),
    ("max_size", "Limit resolution (1600 px)", ["--max-size=1600"]),
    ("show_touches", "Show touches on device", ["--show-touches"]),
    ("always_on_top", "Window always on top", ["--always-on-top"]),
]


def log_path() -> str:
    return os.path.join(app_dir(), "scrcpy.log")


def find_scrcpy(configured: str | None) -> str | None:
    """Locate scrcpy.exe of the scrcpy-ssh build."""
    cands = []
    if configured:
        cands.append(configured)
    if os.environ.get("SCRCPY_SSH"):
        cands.append(os.environ["SCRCPY_SSH"])
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    home = os.path.expanduser("~")
    cands += [
        os.path.join(home, "Desktop", "scrcpy-ssh", "scrcpy.exe"),
        os.path.join(here, "scrcpy-ssh", "scrcpy.exe"),
        os.path.join(os.path.dirname(here), "scrcpy-ssh", "scrcpy.exe"),
    ]
    for c in cands:
        if c and os.path.isfile(c) and supports_ssh(c):
            return c
    return None


def supports_ssh(exe: str) -> bool:
    """True if this scrcpy build has the --ssh option (plain scrcpy does not)."""
    try:
        p = subprocess.run([exe, "--help"], capture_output=True, timeout=15, creationflags=_NOWIN)
        return b"--ssh=" in p.stdout or b"--ssh=" in p.stderr
    except Exception:  # noqa: BLE001
        return False


def ssh_exe() -> str:
    return shutil.which("ssh") or "ssh"


def ssh_options(profile: Profile, known_hosts: str) -> list[str]:
    """ssh options shared by the login check and scrcpy (via SCRCPY_SSH_ARGS).

    AndroUnderTheHood's own known_hosts (where the user already trusted the device)
    is used first, then the user's normal ~/.ssh/known_hosts.
    """
    files = [known_hosts]
    user_kh = os.path.join(os.path.expanduser("~"), ".ssh", "known_hosts")
    if " " not in user_kh and os.path.exists(user_kh):
        files.append(user_kh)
    if any(" " in f for f in files):
        files = [known_hosts]
    opts = ["-o", "UserKnownHostsFile=" + " ".join(files), "-o", "StrictHostKeyChecking=yes"]
    if profile.key_path:
        opts += ["-i", profile.key_path]
    return opts


def destination(profile: Profile) -> str:
    return f"{profile.username}@{profile.host}:{profile.port}"


def check_key_login(profile: Profile, known_hosts: str) -> tuple[bool, str]:
    """scrcpy-ssh needs non-interactive (key / agent) authentication."""
    cmd = [ssh_exe(), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
           *ssh_options(profile, known_hosts), "-p", str(profile.port),
           f"{profile.username}@{profile.host}", "id -u"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=_NOWIN)
    except FileNotFoundError:
        return False, "The OpenSSH client (ssh.exe) was not found. Install Windows' OpenSSH Client feature."
    except subprocess.TimeoutExpired:
        return False, "ssh timed out"
    out = (p.stdout + p.stderr).decode(errors="replace").strip()
    if p.returncode != 0:
        return False, out or f"ssh exited with {p.returncode}"
    return True, out


def public_key_text() -> str | None:
    d = os.path.join(os.path.expanduser("~"), ".ssh")
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                return f.read().strip()
    return None


def launch(scrcpy: str, profile: Profile, known_hosts: str, extra: list[str]) -> subprocess.Popen:
    env = dict(os.environ)
    env["SCRCPY_SSH_ARGS"] = "\n".join(ssh_options(profile, known_hosts))
    cmd = [scrcpy, "--ssh", destination(profile), "--window-title", f"{profile.name} – screen", *extra]
    log = open(log_path(), "w", encoding="utf-8", errors="replace")
    log.write("$ " + subprocess.list2cmdline(cmd) + "\n")
    log.flush()
    return subprocess.Popen(cmd, cwd=os.path.dirname(scrcpy), env=env, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, creationflags=_NOWIN)


def launch_adb(scrcpy: str, serial: str, name: str, extra: list[str]) -> subprocess.Popen:
    """Plain scrcpy over ADB (USB or `adb connect`): the scrcpy-ssh build is a full scrcpy."""
    from .adbconn import find_adb
    env = dict(os.environ)
    adb = find_adb()
    if adb:
        env["ADB"] = adb
    cmd = [scrcpy, "-s", serial, "--window-title", f"{name} – screen (ADB)", *extra]
    log = open(log_path(), "w", encoding="utf-8", errors="replace")
    log.write("$ " + subprocess.list2cmdline(cmd) + "\n")
    log.flush()
    return subprocess.Popen(cmd, cwd=os.path.dirname(scrcpy), env=env, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, creationflags=_NOWIN)


def log_tail(lines: int = 15) -> str:
    try:
        with open(log_path(), "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:]).strip()
    except OSError:
        return ""
