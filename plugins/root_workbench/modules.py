"""Magisk module workbench: module projects on the PC (Documents\\AndroUnderTheHood\\ModuleDev),
create from a template, pack to a flashable zip, test-run a script as root without installing,
install with the root manager's own command, and read Magisk's log.
"""
from __future__ import annotations

import os
import re
import zipfile

SCRIPTS = ["customize.sh", "post-fs-data.sh", "service.sh", "boot-completed.sh", "uninstall.sh",
           "action.sh", "sepolicy.rule", "system.prop", "module.prop"]
DEV_WORK = "/data/local/tmp/.mwb"
DEV_ZIP = "/data/local/tmp/.mwb-install.zip"

UPDATE_BINARY = """#!/sbin/sh
#################
# Initialization
#################
umask 022
ui_print() { echo "$1"; }
require_new_magisk() {
  ui_print "*******************************"
  ui_print " Please install Magisk v20.4+! "
  ui_print "*******************************"
  exit 1
}
OUTFD=$2
ZIPFILE=$3
mount /data 2>/dev/null
[ -f /data/adb/magisk/util_functions.sh ] || require_new_magisk
. /data/adb/magisk/util_functions.sh
[ $MAGISK_VER_CODE -lt 20400 ] && require_new_magisk
install_module
exit 0
"""

TEMPLATES = {
    "customize.sh": """# Runs once while the module is installed (Magisk sources this file).
# $MODPATH = the module's folder. ui_print shows text in the installer.
ui_print "- Installing {name}"
# set_perm_recursive $MODPATH/system/bin 0 0 0755 0755
""",
    "post-fs-data.sh": """#!/system/bin/sh
# Runs early at every boot (blocking, before zygote). Keep it short.
MODDIR=${0%/*}
""",
    "service.sh": """#!/system/bin/sh
# Runs late at every boot (non-blocking). Log with: log -t {id} "message"
MODDIR=${0%/*}
until [ "$(getprop sys.boot_completed)" = 1 ]; do sleep 2; done
log -t {id} "service.sh ran"
""",
    "uninstall.sh": """#!/system/bin/sh
# Runs when the module is removed.
MODDIR=${0%/*}
""",
}


def project_root() -> str:
    d = os.path.join(os.path.expanduser("~"), "Documents", "AndroUnderTheHood", "ModuleDev")
    os.makedirs(d, exist_ok=True)
    return d


def read_prop(folder: str) -> dict:
    out = {}
    try:
        with open(os.path.join(folder, "module.prop"), encoding="utf-8") as f:
            for ln in f:
                k, sep, v = ln.strip().partition("=")
                if sep and not k.startswith("#"):
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def list_projects() -> list[tuple[str, dict]]:
    root = project_root()
    res = []
    for n in sorted(os.listdir(root)):
        d = os.path.join(root, n)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, "module.prop")):
            res.append((d, read_prop(d)))
    return res


def create(mid: str, name: str, version: str, author: str, description: str) -> str:
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9._-]+", mid):
        raise ValueError("Module id: a letter first, then letters, digits, . _ -")
    d = os.path.join(project_root(), mid)
    if os.path.exists(d):
        raise ValueError(f"{d} already exists")
    os.makedirs(os.path.join(d, "META-INF", "com", "google", "android"))
    os.makedirs(os.path.join(d, "system"))
    code = re.sub(r"\D", "", version) or "1"

    def w(rel, text):
        with open(os.path.join(d, rel), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    w("module.prop", f"id={mid}\nname={name}\nversion={version}\nversionCode={code}\n"
                     f"author={author}\ndescription={description}\n")
    for fn, t in TEMPLATES.items():
        w(fn, t.replace("{name}", name).replace("{id}", mid))
    w("META-INF/com/google/android/update-binary", UPDATE_BINARY)
    w("META-INF/com/google/android/updater-script", "#MAGISK\n")
    w("system/.placeholder", "")
    return d


def pack(folder: str) -> str:
    """Flashable zip next to the project folder: <id>-<version>.zip (LF line endings kept)."""
    prop = read_prop(folder)
    if not prop.get("id"):
        raise ValueError("module.prop has no id")
    out = os.path.join(os.path.dirname(folder), f"{prop['id']}-{prop.get('version', 'v1')}.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for base, dirs, files in os.walk(folder):
            dirs[:] = [x for x in dirs if not x.startswith(".git")]
            for fn in files:
                if fn.endswith((".zip", "~")):
                    continue
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, folder).replace(os.sep, "/")
                if rel.endswith(".sh") or rel in ("module.prop", "sepolicy.rule", "system.prop") \
                        or rel.startswith("META-INF/"):
                    with open(full, "rb") as f:
                        data = f.read().replace(b"\r\n", b"\n")     # Windows editors -> LF for sh
                    z.writestr(rel, data)
                else:
                    z.write(full, rel)
    return out


def upload_tree(conn, fs, folder: str, remote: str):
    """Copy a project folder to the device (small files: over stdin, binary-safe via base64)."""
    import base64
    conn.exec(f"rm -rf {remote} && mkdir -p {remote}", timeout=60, as_root=True)
    for base, dirs, files in os.walk(folder):
        dirs[:] = [x for x in dirs if not x.startswith(".git")]
        rel_dir = os.path.relpath(base, folder).replace(os.sep, "/")
        rdir = remote if rel_dir == "." else f"{remote}/{rel_dir}"
        conn.exec(f"mkdir -p '{rdir}'", timeout=30, as_root=True)
        for fn in files:
            with open(os.path.join(base, fn), "rb") as f:
                data = f.read()
            if fn.endswith(".sh") or fn in ("module.prop", "sepolicy.rule", "system.prop"):
                data = data.replace(b"\r\n", b"\n")
            conn.exec(f"base64 -d > '{rdir}/{fn}'", stdin=base64.b64encode(data), timeout=120, as_root=True)


def test_run(conn, fs, folder: str, script: str, sink) -> int:
    """Run one of the module's scripts NOW as root (like Magisk would: MODDIR/MODPATH set),
    from a temporary copy - nothing is installed. Output is streamed to sink(bytes)."""
    prop = read_prop(folder)
    remote = f"{DEV_WORK}/{prop.get('id', 'module')}"
    upload_tree(conn, fs, folder, remote)
    # like Magisk: its busybox ash in standalone mode, so the script sees the same applets
    bb = "/data/adb/magisk/busybox"
    cmd = (f"cd {remote} && export MODDIR={remote} MODPATH={remote} ASH_STANDALONE=1 && "
           f"chmod 755 {remote}/*.sh 2>/dev/null; "
           f"if [ -x {bb} ]; then {bb} sh {remote}/{script}; else sh {remote}/{script}; fi 2>&1; "
           f"echo \"[exit code $?]\"")
    try:
        conn.exec_stream_out(cmd, sink, timeout=600)
        rc = 0
    except Exception as e:  # noqa: BLE001
        sink(f"[{e}]\n".encode())
        rc = 1
    conn.exec(f"rm -rf {DEV_WORK}", timeout=60, as_root=True)
    return rc


def install(conn, fs, zip_path: str, sink) -> int:
    """Install the packed zip with Magisk (takes effect after a reboot). Uses the app's root
    manager to build the command for the installed root solution."""
    from androunderthehood import rootmgr
    m = rootmgr.detect(conn)
    fs.upload(zip_path, DEV_ZIP, lambda n: None)
    cmd = rootmgr.install_command(m, DEV_ZIP) + f" 2>&1; r=$?; rm -f {DEV_ZIP}; exit $r"
    try:
        conn.exec_stream_out(cmd, sink, timeout=600)
        return 0
    except Exception as e:  # noqa: BLE001
        sink(f"[{e}]\n".encode())
        return 1


def magisk_log(conn, lines: int = 300) -> str:
    """Magisk logs to logcat (tag Magisk) since v26; module scripts should use `log -t <id>`."""
    rc, out, err = conn.exec(f"logcat -d -b main -v time -s Magisk:* 2>/dev/null | tail -n {lines}", timeout=60,
                             as_root=True)
    return out.decode("utf-8", "replace")


def installed(conn) -> list:
    from androunderthehood import rootmgr
    try:
        return rootmgr.list_modules(conn)
    except Exception:  # noqa: BLE001
        return []
