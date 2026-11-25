"""One-click AndroOpenHeart setup over ADB: install, configure (PC key), grant root, start, link an SSH profile.

Works with or without root. Without root, AndroOpenHeart (1.4+) is configured through its AdbSetupReceiver,
which only the ADB shell user can call, and its sessions run as the shell user through Shizuku."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import paramiko

from . import adbconn
from . import backend as B

log = logging.getLogger("androunderthehood")
q = B.shq
PKG = "dev.rootssh"
SHIZUKU_PKG = "moe.shizuku.privileged.api"
SHIZUKU_CFG = "/data/user_de/0/com.android.shell/shizuku.json"
SHIZUKU_MAIN = f"{SHIZUKU_PKG}/moe.shizuku.manager.MainActivity"
SHIZUKU_BOOT_RECEIVER = f"{SHIZUKU_PKG}/moe.shizuku.manager.receiver.BootCompleteReceiver"
DEFAULT_PORT = 2222
# development/runtime permissions granted with `pm grant`, and app-ops set with `appops set`
GRANT_PERMS = ("POST_NOTIFICATIONS", "READ_LOGS", "DUMP", "WRITE_SECURE_SETTINGS")
APP_OPS = ("MANAGE_EXTERNAL_STORAGE", "GET_USAGE_STATS", "REQUEST_INSTALL_PACKAGES")
_NOWIN = 0x08000000 if sys.platform == "win32" else 0


def bundled_apk(name: str = "AndroOpenHeart.apk") -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(base, "androunderthehood", "assets", name),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name)):
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(f"{name} is not bundled with this AndroUnderTheHood build")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# PC key
# ---------------------------------------------------------------------------
def pc_key(log_fn) -> tuple[str, str]:
    """(private key path, public key line). Creates an ed25519 key if the PC has none."""
    d = os.path.join(os.path.expanduser("~"), ".ssh")
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        priv = os.path.join(d, name)
        if os.path.isfile(priv) and os.path.isfile(priv + ".pub"):
            with open(priv + ".pub", encoding="utf-8") as f:
                return priv, f.read().strip()
    os.makedirs(d, exist_ok=True)
    priv = os.path.join(d, "id_ed25519")
    comment = f"androunderthehood@{socket.gethostname()}"
    keygen = shutil.which("ssh-keygen")
    if keygen:
        log_fn("Creating an SSH key for this PC (~/.ssh/id_ed25519)…")
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", priv], check=True,
                       capture_output=True, creationflags=_NOWIN)
    else:
        log_fn("Creating an SSH key for this PC (~/.ssh/id_ecdsa)…")
        priv = os.path.join(d, "id_ecdsa")
        k = paramiko.ECDSAKey.generate(bits=256)
        k.write_private_key_file(priv)
        with open(priv + ".pub", "w", encoding="utf-8") as f:
            f.write(f"{k.get_name()} {k.get_base64()} {comment}\n")
    with open(priv + ".pub", encoding="utf-8") as f:
        return priv, f.read().strip()


# ---------------------------------------------------------------------------
# AndroOpenHeart config (SharedPreferences XML)
# ---------------------------------------------------------------------------
def merge_config(xml_text: str | None, pub: str) -> tuple[str, dict]:
    """Return (new xml, effective settings). Keeps existing values; adds the PC key."""
    vals = {"port": DEFAULT_PORT, "username": "root", "password": "", "authorized_keys": "", "use_root": True,
            "use_shizuku": True, "start_on_boot": True}
    if xml_text:
        try:
            root = ET.fromstring(xml_text)
            for el in root:
                n = el.get("name")
                if el.tag == "string":
                    vals[n] = el.text or ""
                elif el.tag == "int":
                    vals[n] = int(el.get("value", "0"))
                elif el.tag == "boolean":
                    vals[n] = el.get("value") == "true"
        except ET.ParseError:
            pass
    keys = [k for k in vals["authorized_keys"].splitlines() if k.strip()]
    blob = pub.split()[1] if len(pub.split()) > 1 else pub
    if not any(blob in k for k in keys):
        keys.append(pub)
    vals["authorized_keys"] = "\n".join(keys)
    vals["use_root"] = False   # AndroUnderTheHood asks for root itself (su) only when its Root toggle is on
    vals["use_shizuku"] = True  # non-root commands run as the shell user (ADB-level rights)
    if not vals.get("port"):
        vals["port"] = DEFAULT_PORT
    m = ET.Element("map")
    for n in ("authorized_keys", "password", "username"):
        e = ET.SubElement(m, "string", name=n)
        e.text = vals[n]
    ET.SubElement(m, "int", name="port", value=str(vals["port"]))
    for n in ("use_root", "use_shizuku", "start_on_boot"):
        ET.SubElement(m, "boolean", name=n, value="true" if vals[n] else "false")
    body = ET.tostring(m, encoding="unicode")
    return "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n" + body + "\n", vals


# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------
class SetupError(Exception):
    pass


def setup(serial: str, log_fn) -> dict:
    """Run the whole setup on ADB device `serial`. Returns the SSH profile fields + host key."""
    prof = B.Profile(name=serial, host=serial, port=0, username="adb", mode=B.MODE_SHELL, su_command="su -c")
    conn = adbconn.AdbConnection(prof)
    log_fn(f"Connecting to {serial} over ADB…")
    conn.open()
    root = bool(conn.check_root())
    if not root:
        log_fn("No root over ADB – setting up without root. AndroOpenHeart will run commands as the ADB shell "
               "user through Shizuku. (On a rooted phone, allow root for “Shell” in Magisk to set it up with root.)")

    def sh(cmd, timeout=60):
        rc, out, err = conn.exec(cmd, timeout=timeout, as_root=root)
        return rc, (out + err).decode(errors="replace").strip()

    def install(apk_name: str, pkg: str, label: str):
        apk = bundled_apk(apk_name)
        want = sha256_file(apk)
        rc, path = sh(f"pm path {pkg} 2>/dev/null | head -n1 | cut -d: -f2")
        have = sh(f"sha256sum {q(path)} | cut -d' ' -f1")[1] if path else ""
        if have == want:
            log_fn(f"{label} is already installed (same version).")
            return
        log_fn(f"Updating {label}…" if path else f"Installing {label}…")
        r = adbconn.adb(["-s", serial, "install", "-r", apk], timeout=300)
        out = (r.stdout + r.stderr).decode(errors="replace").strip()
        if "Success" not in out:
            if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in out:
                raise SetupError(f"A {label} signed with a different key is installed. Uninstall it on the phone "
                                 "first (its settings will be lost), then retry.")
            if "INSTALL_FAILED_VERSION_DOWNGRADE" in out:
                log_fn(f"A newer {label} is already installed – keeping it.")
                return
            raise SetupError(f"{label} install failed: " + out[-400:])
        log_fn("Installed.")

    def app_uid(pkg: str) -> str:
        for cmd in (f"pm list packages -U {pkg} 2>/dev/null | grep -m1 'package:{pkg} ' "
                    f"| sed 's/.*uid://; s/[ ,].*//'",
                    f"stat -c %u /data/data/{pkg} 2>/dev/null",
                    f"dumpsys package {pkg} | grep -m1 -oE '(userId|appId)=[0-9]+' | cut -d= -f2"):
            u = sh(cmd)[1].strip()
            if u.isdigit():
                return u
        return ""

    # 1. install / update AndroOpenHeart and Shizuku
    install("AndroOpenHeart.apk", PKG, "AndroOpenHeart")
    uid = app_uid(PKG)
    if not uid.isdigit():
        raise SetupError("Could not find AndroOpenHeart's user id after installing.")
    install("Shizuku.apk", SHIZUKU_PKG, "Shizuku")

    if not root:
        return _setup_noroot(conn, serial, sh, uid, log_fn)

    # 2. stop the app, then write its settings (merged with existing ones)
    fs = conn.new_fs()
    sh(f"am force-stop {PKG}")
    pdir = f"/data/data/{PKG}/shared_prefs"
    cfg_path = f"{pdir}/config.xml"
    rc, cur = sh(f"cat {q(cfg_path)} 2>/dev/null")
    priv, pub = pc_key(log_fn)
    new_xml, vals = merge_config(cur if cur.startswith("<?xml") else None, pub)
    log_fn(f"Configuring AndroOpenHeart: user “{vals['username']}”, port {vals['port']}, run-as-root off "
           f"(AndroUnderTheHood's Root toggle uses su instead), this PC's key "
           f"authorized{' (added)' if pub.split()[1] not in (cur or '') else ''}.")
    tmp = os.path.join(tempfile.gettempdir(), "androunderthehood-openheart-config.xml")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_xml)
    stage = "/data/local/tmp/.androunderthehood-openheart.xml"
    fs.upload(tmp, stage, lambda n: None)
    os.remove(tmp)
    rc, ctx = sh(f"stat -c %C /data/data/{PKG}")
    rc, out = sh(f"mkdir -p {q(pdir)}; cat {q(stage)} > {q(cfg_path)}; rm -f {q(stage)}; "
                 f"chown {uid}:{uid} {q(pdir)} {q(cfg_path)}; chmod 771 {q(pdir)}; chmod 660 {q(cfg_path)}; "
                 f"chcon {q(ctx)} {q(pdir)} {q(cfg_path)} 2>/dev/null; echo ok")
    if not out.endswith("ok"):
        raise SetupError("Could not write AndroOpenHeart's settings: " + out)

    # 3. permissions: root (Magisk), notifications, battery
    rc, mv = sh("magisk -V 2>/dev/null")
    if mv.strip().isdigit():
        sh(f"magisk --sqlite \"REPLACE INTO policies (uid,policy,until,logging,notification) "
           f"VALUES({uid},2,0,1,0)\"")
        log_fn("Root granted to AndroOpenHeart in Magisk.")
    else:
        log_fn("Magisk not found – if AndroOpenHeart asks for root on the phone, allow it.")
    _grant_perms(sh, log_fn)
    setup_shizuku(conn, uid, log_fn)

    # 4. start the server and wait for it to listen
    port = int(vals["port"])
    log_fn(f"Starting the SSH server on port {port}…")
    sh(f"am start-foreground-service -n {PKG}/.SshService")
    if not _wait_listen(sh, port):
        raise SetupError("AndroOpenHeart did not start listening – open the app on the phone and press Start.")
    log_fn("Server is running.")

    # 5. host key, read over the trusted ADB channel
    rc, keytxt = sh(f"cat /data/data/{PKG}/files/ssh_host_ec_key")
    host_key = None
    try:
        host_key = paramiko.PKey.from_private_key(io.StringIO(keytxt))
    except Exception:  # noqa: BLE001
        for cls in (paramiko.ECDSAKey, paramiko.Ed25519Key, paramiko.RSAKey):
            try:
                host_key = cls.from_private_key(io.StringIO(keytxt))
                break
            except Exception:  # noqa: BLE001
                continue
    if host_key is None:
        log_fn("Could not read the server's host key – you will be asked to confirm its fingerprint.")

    return _finish(serial, sh, port, vals["username"], host_key, priv, log_fn)


def _grant_perms(sh, log_fn):
    granted, failed = [], []
    for perm in GRANT_PERMS:
        rc, out = sh(f"pm grant {PKG} android.permission.{perm} 2>&1 && echo ok")
        (granted if out.endswith("ok") else failed).append(perm)
    for op in APP_OPS:
        rc, out = sh(f"appops set {PKG} {op} allow 2>&1 && echo ok")
        (granted if out.endswith("ok") else failed).append(op)
    log_fn("Granted AndroOpenHeart: " + ", ".join(p.lower() for p in granted) + " (it can see all apps by default)."
           + (f" Not granted: {', '.join(failed)}." if failed else ""))
    sh(f"dumpsys deviceidle whitelist +{PKG} >/dev/null 2>&1")


def _wait_listen(sh, port: int, tries: int = 30) -> bool:
    hexport = f"{port:04X}"
    for _ in range(tries):
        rc, out = sh(f"cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk '$2 ~ /:{hexport}$/ && $4 == \"0A\"' "
                     f"| wc -l")
        if out.strip().isdigit() and int(out.strip()) > 0:
            return True
        time.sleep(0.5)
    return False


def _setup_noroot(conn, serial, sh, uid, log_fn) -> dict:
    """No root: configure through AndroOpenHeart's AdbSetupReceiver (callable only by the ADB shell user)."""
    rc, out = sh(f"dumpsys package {PKG} | grep -m1 AdbSetupReceiver")
    if "AdbSetupReceiver" not in out:
        raise SetupError("The installed AndroOpenHeart is too old for setup without root (needs 1.4 or newer). "
                         "Uninstall it on the phone, then retry.")
    _grant_perms(sh, log_fn)
    shizuku_ok = setup_shizuku(conn, uid, log_fn)
    boot = prepare_shizuku_boot(conn, log_fn) if shizuku_ok else "no_shizuku"
    priv, pub = pc_key(log_fn)

    def setup_broadcast(**extra) -> dict:
        args = [f"--es add_key {q(pub)}", "--ez use_root false", "--ez use_shizuku true", "--ez start_on_boot true"]
        for k, v in extra.items():
            args.append(f"--ez {k} {'true' if v else 'false'}" if isinstance(v, bool) else f"--ei {k} {int(v)}")
        rc, out = sh(f"am broadcast -a dev.rootssh.action.SETUP -n {PKG}/.AdbSetupReceiver {' '.join(args)}",
                     timeout=60)
        m = re.search(r'data="(.*)"', out, re.S)
        if not m:
            raise SetupError("AndroOpenHeart did not answer the setup request: " + out[-300:])
        return dict(line.split("=", 1) for line in m.group(1).splitlines() if "=" in line)

    # first pass: settings only (the server may not be allowed to start while the app is in the background)
    res = setup_broadcast()
    if res.get("ok") != "true":
        raise SetupError("AndroOpenHeart refused the settings: " + res.get("error", "unknown error"))
    port = int(res.get("port") or DEFAULT_PORT)
    added = res.get("key") == "added"
    if port == 2022 and added:       # fresh install: use the usual port
        res = setup_broadcast(port=DEFAULT_PORT)
        port = int(res.get("port") or DEFAULT_PORT)
    log_fn(f"Configured AndroOpenHeart: user “{res.get('username')}”, port {port}, run-as-root off, this PC's key "
           f"{'added' if added else 'already authorized'}.")

    # Android 12+ only lets an app start its server from the foreground: open it, then start
    log_fn(f"Opening AndroOpenHeart on the phone and starting the SSH server on port {port}…")
    sh("input keyevent KEYCODE_WAKEUP >/dev/null 2>&1")
    sh(f"am start -n {PKG}/.MainActivity >/dev/null 2>&1")
    time.sleep(1.5)
    res = setup_broadcast(start=True)
    if not res.get("start", "").startswith(("started", "restarted")) or not _wait_listen(sh, port):
        raise SetupError("AndroOpenHeart did not start listening (" + res.get("start", "?") + "). Unlock the phone, "
                         "open AndroOpenHeart and press Start, then retry.")
    log_fn("Server is running.")
    sh("input keyevent KEYCODE_HOME >/dev/null 2>&1")
    if not shizuku_ok:
        log_fn("Without Shizuku, sessions run as AndroOpenHeart's own app user (limited access).")
    elif boot == "old_android":
        log_fn("Note: without root, Shizuku stops when the phone reboots, and before Android 11 it can't start "
               "itself again. Run this setup again after a reboot.")

    host_key = None
    try:
        kt, blob = res.get("hostkey", "").split()[:2]
        host_key = paramiko.PKey.from_type_string(kt, base64.b64decode(blob))
    except Exception:  # noqa: BLE001
        log_fn("Could not read the server's host key – you will be asked to confirm its fingerprint.")
    out = _finish(serial, sh, port, res.get("username") or "root", host_key, priv, log_fn)
    out["shizuku_boot"] = boot
    return out


def _finish(serial, sh, port, username, host_key, priv, log_fn) -> dict:
    # 6. find an address the PC can reach
    rc, addrs = sh("ip -o -4 addr 2>/dev/null")
    cands = []
    for line in addrs.splitlines():
        f = line.split()
        if len(f) >= 4 and f[2] == "inet":
            iface, ip = f[1], f[3].split("/")[0]
            if iface == "lo" or iface.startswith(("rmnet", "dummy", "r_rmnet", "ccmni")):
                continue
            prio = 0 if iface.startswith("wlan") else 1 if iface.startswith(("tun", "wg", "ppp")) else 2
            cands.append((prio, iface, ip))
    cands.sort()
    host, use_port, via = None, port, ""
    for _p, iface, ip in cands:
        try:
            with socket.create_connection((ip, port), timeout=1.5):
                host, via = ip, iface
                break
        except OSError:
            continue
    if not host:
        log_fn("The phone's network address is not reachable from this PC – using SSH through the USB cable "
               "(adb forward).")
        local = port if port > 1024 else 2222
        r = adbconn.adb(["-s", serial, "forward", f"tcp:{local}", f"tcp:{port}"], timeout=20)
        if r.returncode != 0:
            raise SetupError("adb forward failed: " + (r.stdout + r.stderr).decode(errors="replace"))
        host, use_port, via = "127.0.0.1", local, "USB (adb forward)"
    log_fn(f"Using {host}:{use_port} ({via}).")

    # 7. trust the host key for that address
    if host_key is not None:
        B.KnownHosts().add(host, use_port, host_key)
        log_fn(f"Trusted host key {B.fingerprint(host_key)}.")

    rc, model = sh("getprop ro.vendor.oplus.market.name; getprop ro.product.marketname; getprop ro.product.model")
    name = next((x for x in model.splitlines() if x.strip()), serial)
    return {"name": f"{name} (AndroOpenHeart)", "host": host, "port": use_port, "username": username,
            "key_path": priv, "via": via}


# ---------------------------------------------------------------------------
# Shizuku
# ---------------------------------------------------------------------------
def setup_shizuku(conn, uid: str, log_fn) -> bool:
    """Allow AndroOpenHeart in Shizuku and (re)start the Shizuku server as the ADB shell user (uid 2000).

    Runs as the plain ADB shell user on purpose: Shizuku's allowed-apps list lives in the shell
    user's folder, and a server started by root would give "no-root" sessions root rights."""
    def shell(cmd, timeout=60):
        rc, out, err = conn.exec(cmd, timeout=timeout, as_root=False)
        return rc, (out + err).decode(errors="replace").strip()

    rc, me = shell("id -u")
    if me != "2000":
        log_fn("Shizuku skipped: the ADB shell is not the shell user (adbd running as root?).")
        return False
    # 1. allow AndroOpenHeart. Stop a running server first: it keeps the list in memory and would
    #    write its own copy over ours.
    shell("for p in $(pidof shizuku_server); do kill $p; done; sleep 1")
    # Shizuku mirrors its allow-list from this runtime permission when the app starts
    shell(f"pm grant {PKG} moe.shizuku.manager.permission.API_V23")
    rc, cur = shell(f"cat {SHIZUKU_CFG} 2>/dev/null")
    try:
        cfg = json.loads(cur) if cur.startswith("{") else {}
    except ValueError:
        cfg = {}
    cfg.setdefault("version", 2)
    pkgs = [e for e in cfg.get("packages", [])
            if PKG not in (e.get("packages") or []) and str(e.get("uid")) != uid]
    pkgs.append({"uid": int(uid), "flags": 2, "packages": [PKG]})   # 2 = FLAG_ALLOWED
    cfg["packages"] = pkgs
    blob = base64.b64encode(json.dumps(cfg, separators=(",", ":")).encode()).decode()
    rc, out = shell(f"echo {blob} | base64 -d > {SHIZUKU_CFG}.new && mv {SHIZUKU_CFG}.new {SHIZUKU_CFG} "
                    f"&& chmod 600 {SHIZUKU_CFG} && echo ok")
    if not out.endswith("ok"):
        log_fn("Could not pre-authorize AndroOpenHeart in Shizuku – tap “Allow” if Shizuku asks on the phone. " + out)
    # 2. (re)start the server so it reads the list
    rc, starter = shell(f'p=$(pm path {SHIZUKU_PKG} | head -n1 | cut -d: -f2); '
                        f'ls "$(dirname "$p")"/lib/*/libshizuku.so 2>/dev/null | head -n1')
    if not starter.endswith("libshizuku.so"):
        log_fn("Shizuku's starter was not found – open Shizuku on the phone and start it there.")
        return False
    log_fn("Starting Shizuku as the shell user…")
    rc, out = shell(q(starter), timeout=60)
    if "exit with 0" not in out:
        log_fn("Shizuku did not start: " + out[-300:])
        return False
    for _ in range(20):
        rc, ps = shell("ps -A -o USER,NAME | grep shizuku_server")
        if "shizuku_server" in ps:
            log_fn("Shizuku is running as the shell user and AndroOpenHeart is allowed to use it.")
            return True
        time.sleep(0.5)
    log_fn("Shizuku server did not show up.")
    return False


# ---------------------------------------------------------------------------
# Shizuku start on boot (no root)
# ---------------------------------------------------------------------------
# Shizuku 13.6's boot receiver (enabled by default) starts the server without root when
#  - its last recorded start method is "adb" (pref "mode" = 1; its main screen records that whenever it
#    sees a server running as the shell user),
#  - it holds WRITE_SECURE_SETTINGS (to switch Wireless debugging on at boot), and
#  - it is paired with Wireless debugging (its own adb key - done once: the user types a 6-digit code).
# Needs Android 11+, and Wi-Fi connected at boot.

def _shell_user(conn):
    def shell(cmd, timeout=60):
        rc, out, err = conn.exec(cmd, timeout=timeout, as_root=False)
        return rc, (out + err).decode(errors="replace").strip()
    return shell


def _sdk(shell) -> int:
    v = shell("getprop ro.build.version.sdk")[1].strip()
    return int(v) if v.isdigit() else 0


def prepare_shizuku_boot(conn, log_fn) -> str:
    """Everything for Shizuku's start on boot that ADB can do -> "ready" | "old_android" | "failed".
    "ready" still needs the one-time pairing (pair_shizuku)."""
    shell = _shell_user(conn)
    sdk = _sdk(shell)
    if sdk < 30:
        return "old_android"
    shell(f"pm grant {SHIZUKU_PKG} android.permission.WRITE_SECURE_SETTINGS")
    if sdk >= 33:          # pairing talks to the user through a notification
        shell(f"pm grant {SHIZUKU_PKG} android.permission.POST_NOTIFICATIONS")
    shell(f"pm enable {SHIZUKU_BOOT_RECEIVER} >/dev/null 2>&1")
    # let Shizuku's main screen see its server running as the shell user: it records "started via adb"
    shell("input keyevent KEYCODE_WAKEUP >/dev/null 2>&1")
    shell(f"am start -n {SHIZUKU_MAIN} >/dev/null 2>&1")
    time.sleep(3)
    shell("input keyevent KEYCODE_HOME >/dev/null 2>&1")
    rc, perms = shell(f"dumpsys package {SHIZUKU_PKG} | grep -m1 'WRITE_SECURE_SETTINGS: granted'")
    if "granted=true" not in perms:
        log_fn("Shizuku could not get the permission it needs to start itself after a reboot.")
        return "failed"
    log_fn("Prepared Shizuku to start itself after a reboot (it switches Wireless debugging on; Wi-Fi must be "
           "connected). One step is still needed once: pairing Shizuku with Wireless debugging.")
    return "ready"


# The phone's own UI driven over ADB (English labels; in other languages the user follows the steps the
# log gives). Arguments: TEXT SCROLLS - scroll until a node with exactly that text shows, then tap it.
_UI_TAP = r"""
T="$1"; N=${2:-0}; i=0; X=/data/local/tmp/.auth-ui.xml
while :; do
  uiautomator dump $X >/dev/null 2>&1
  b=$(sed 's/></>\n</g' $X | grep -F "text=\"$T\"" | head -n1 \
      | sed -E 's/.*bounds="\[([0-9]+),([0-9]+)\]\[([0-9]+),([0-9]+)\]".*/\1 \2 \3 \4/')
  if [ -n "$b" ]; then set -- $b; input tap $(( ($1+$3)/2 )) $(( ($2+$4)/2 )); rm -f $X; exit 0; fi
  i=$((i+1)); [ $i -gt $N ] && break
  input swipe 540 1700 540 900 600; sleep 0.8
done
rm -f $X; exit 1
"""


def pair_shizuku(serial: str, log_fn, wait_s: int = 240) -> bool:
    """One-time pairing of Shizuku with Wireless debugging, so it can start itself after a reboot.
    Opens Shizuku's pairing and the "Pair device with pairing code" dialog on the phone. The user types the
    6-digit code into Shizuku's notification - it is never typed for them. Waits for the result."""
    prof = B.Profile(name=serial, host=serial, port=0, username="adb", mode=B.MODE_SHELL, su_command="")
    conn = adbconn.AdbConnection(prof)
    conn.open()
    shell = _shell_user(conn)
    if shell("id -u")[1] != "2000":
        raise SetupError("Pairing has to run as the ADB shell user.")
    if _sdk(shell) < 30:
        raise SetupError("Wireless debugging needs Android 11 or newer.")
    if "inet" not in shell("ip -o -4 addr show wlan0 2>/dev/null | head -n1")[1]:
        raise SetupError("The phone is not on Wi-Fi. Wireless debugging (and so pairing) only works on Wi-Fi.")

    def tap(text, scrolls=0) -> bool:
        return shell(f"sh -c {q(_UI_TAP)} tap {q(text)} {scrolls}", timeout=180)[0] == 0

    def title() -> str:
        out = shell("dumpsys notification --noredact 2>/dev/null | grep -A60 'pkg=moe.shizuku' "
                    "| grep -m1 'android.title='")[1]
        m = re.search(r"android\.title=\S+ \((.*)\)", out)
        return m.group(1) if m else ""

    log_fn("Opening Shizuku's pairing on the phone…")
    shell(f"pm grant {SHIZUKU_PKG} android.permission.POST_NOTIFICATIONS >/dev/null 2>&1")
    # a pairing session left over from an earlier try makes a new one hang (app not responding).
    # force-stop only ends Shizuku's app process; its server runs as the shell user and keeps running.
    shell(f"am force-stop {SHIZUKU_PKG}")
    shell("input keyevent KEYCODE_WAKEUP >/dev/null 2>&1")
    shell(f"am start -n {SHIZUKU_MAIN} >/dev/null 2>&1")
    time.sleep(2.5)
    if not tap("Pairing", 3):
        log_fn("Couldn't find Shizuku's “Pairing” button – on the phone tap it yourself (Shizuku → Start via "
               "Wireless debugging → Pairing).")
    time.sleep(2)
    log_fn("Opening Developer options → Wireless debugging → Pair device with pairing code…")
    shell("am start -a android.settings.APPLICATION_DEVELOPMENT_SETTINGS >/dev/null 2>&1")
    time.sleep(2.5)
    opened = tap("Wireless debugging", 40)
    time.sleep(2.5)
    if not (opened and tap("Pair device with pairing code", 2)):
        log_fn("Couldn't open the pairing dialog – on the phone open Settings → Developer options → Wireless "
               "debugging → Pair device with pairing code.")
    log_fn("ON THE PHONE: pull down the notifications, tap “Enter pairing code” on Shizuku's notification and "
           "type the 6-digit Wi-Fi pairing code from the dialog. Waiting for the pairing…")
    t0 = time.time()
    while time.time() - t0 < wait_s:
        t = title()
        if t == "Pairing successful":
            log_fn("Shizuku is paired with Wireless debugging. It now starts itself after a reboot "
                   "(Wi-Fi must be connected).")
            shell("input keyevent KEYCODE_HOME >/dev/null 2>&1")
            return True
        if t == "Pairing failed":
            raise SetupError("Pairing failed (wrong or expired code?). Run it again for a new code.")
        time.sleep(2)
    raise SetupError(f"No pairing within {wait_s // 60} minutes. Run it again when you are at the phone.")
