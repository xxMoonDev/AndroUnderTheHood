"""Root manager control: Magisk, Kitsune Mask, KernelSU (and forks such as KernelSU Next /
SukiSU Ultra) and APatch.

Everything is done with the manager's own command-line tools as root:
  * Magisk / Kitsune  `magisk --sqlite` (settings, superuser policies), `magisk --denylist`
                      (or the older `magisk --hide` CLI on Kitsune builds), `magisk --install-module`
  * KernelSU          `/data/adb/ksud module install|uninstall|enable|disable|action`
  * APatch            `/data/adb/apd module …` and /data/adb/ap/package_config (exclude list)
Modules of every manager live in /data/adb/modules (+ /data/adb/modules_update for installs that
are waiting for a reboot) and use the same flag files (disable, remove, update).
"""
from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field

from . import backend as B

log = logging.getLogger("androunderthehood")
q = B.shq

MAGISK, KITSUNE, KERNELSU, APATCH = "magisk", "kitsune", "kernelsu", "apatch"

# manager app packages → display name (the Magisk app can be hidden under a random name)
MANAGER_APPS = {
    "com.topjohnwu.magisk": "Magisk",
    "io.github.huskydg.magisk": "Kitsune Mask",
    "io.github.vvb2060.magisk": "Magisk Alpha",
    "me.weishu.kernelsu": "KernelSU",
    "com.rifsxd.ksunext": "KernelSU Next",
    "com.sukisu.ultra": "SukiSU Ultra",
    "com.resukisu.resukisu": "ReSukiSU",
    "me.bmax.apatch": "APatch",
    "me.garfieldhan.apatch.next": "APatch Next",
}

# modules that provide Zygisk on KernelSU / APatch (Magisk and Kitsune have it built in)
ZYGISK_PROVIDERS = ("zygisksu", "rezygisk", "neozygisk", "zygisknext", "zygisk_next")

# Magisk database settings shown on the overview: key → (label, default, {value: text}, help)
MAGISK_SETTINGS = [
    ("zygisk", "Zygisk", 0, {0: "Off", 1: "On"},
     "Run code inside every app process (needed by Zygisk modules such as LSPosed, Shamiko, PIF). "
     "Takes effect after a reboot."),
    ("denylist", "Enforce DenyList", 0, {0: "Off", 1: "On"},
     "Revert all Magisk changes for the processes on the DenyList (needs Zygisk). Takes effect right away "
     "for newly started processes."),
    ("root_access", "Superuser access", 3, {0: "Disabled", 1: "Apps only", 2: "ADB only", 3: "Apps and ADB"},
     "Who may request root. Careful: AndroOpenHeart is an app – \"Disabled\" or \"ADB only\" cuts AndroUnderTheHood "
     "off from root."),
    ("multiuser_mode", "Multiuser mode", 0, {0: "Device owner only", 1: "Device owner managed",
                                             2: "User independent"},
     "How root works for secondary users / work profiles."),
    ("mnt_ns", "Mount namespace mode", 1, {0: "Global namespace", 1: "Inherit namespace",
                                           2: "Isolated namespace"},
     "Which mount namespace root shells get. Global is what file managers expect."),
]
POLICY_TEXT = {0: "Ask", 1: "Deny", 2: "Allow"}


@dataclass
class Manager:
    kind: str = ""              # MAGISK / KITSUNE / KERNELSU / APATCH, "" if none found
    name: str = ""              # display name ("Magisk", "KernelSU Next" …)
    version: str = ""
    version_code: str = ""
    binary: str = ""            # magisk / /data/adb/ksud / /data/adb/apd
    app_pkg: str = ""           # manager app package if found
    app_hidden: bool = False    # Magisk app installed under a random package name
    denylist_cli: str = ""      # "--denylist" or "--hide" (Magisk family only)
    denylist_on: bool | None = None
    zygisk_loaded: bool = False  # zygote has a zygisk library mapped right now
    extra: dict = field(default_factory=dict)

    @property
    def magisk_family(self) -> bool:
        return self.kind in (MAGISK, KITSUNE)

    @property
    def busybox(self) -> str:
        return {MAGISK: "/data/adb/magisk/busybox", KITSUNE: "/data/adb/magisk/busybox",
                KERNELSU: "/data/adb/ksu/bin/busybox", APATCH: "/data/adb/ap/bin/busybox"}.get(self.kind, "")


@dataclass
class Module:
    id: str
    path: str
    props: dict = field(default_factory=dict)
    flags: set = field(default_factory=set)    # disable remove update skip_mount action.sh zygisk webroot …
    size_kb: int = 0
    pending_install: bool = False              # only in modules_update (new install, waiting for reboot)

    @property
    def name(self) -> str:
        return self.props.get("name") or self.id

    @property
    def enabled(self) -> bool:
        return "disable" not in self.flags

    @property
    def removing(self) -> bool:
        return "remove" in self.flags

    @property
    def status(self) -> str:
        if self.pending_install:
            return "Installed – reboot to activate"
        if self.removing:
            return "Will be removed at reboot"
        if "update" in self.flags:
            return "Updated – reboot to apply"
        return "Enabled" if self.enabled else "Disabled"

    @property
    def features(self) -> list[str]:
        out = []
        for f, t in (("zygisk", "Zygisk"), ("system", "system files"), ("service.sh", "service"),
                     ("post-fs-data.sh", "post-fs-data"), ("system.prop", "props"), ("sepolicy.rule", "sepolicy"),
                     ("action.sh", "action"), ("webroot", "WebUI")):
            if f in self.flags:
                out.append(t)
        return out

    def project_url(self) -> str:
        """Best guess at the module's home page from module.prop."""
        for k in ("support", "homepage", "website", "url", "donate"):
            v = self.props.get(k, "")
            if v.startswith("http"):
                return v
        for k in ("updateJson", "zipUrl", "changelog"):
            v = self.props.get(k, "")
            m = re.match(r"https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/", v) or \
                re.match(r"https?://github\.com/([^/]+)/([^/]+)", v)
            if m:
                return f"https://github.com/{m.group(1)}/{m.group(2)}"
            m = re.match(r"https?://gitlab\.com/([^/]+)/([^/]+)", v)
            if m:
                return f"https://gitlab.com/{m.group(1)}/{m.group(2)}"
        return ""


def _kv(text: str) -> dict:
    d = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() and not k.lstrip().startswith("#"):
            d[k.strip()] = v.strip()
    return d


DETECT = r'''
M=$(command -v magisk 2>/dev/null)
[ -z "$M" ] && for p in /debug_ramdisk/magisk /sbin/magisk /data/adb/magisk/magisk; do [ -x $p ] && M=$p && break; done
echo "M_BIN=$M"
if [ -n "$M" ]; then echo "M_V=$($M -v 2>/dev/null)"; echo "M_VC=$($M -V 2>/dev/null)"; echo "M_C=$($M -c 2>/dev/null)"; fi
[ -x /data/adb/ksud ] && { echo "K_BIN=/data/adb/ksud"; echo "K_V=$(/data/adb/ksud -V 2>/dev/null | head -1)"; echo "K_KV=$(/data/adb/ksud debug version 2>/dev/null | head -1)"; }
[ -x /data/adb/apd ] && { echo "A_BIN=/data/adb/apd"; echo "A_V=$(/data/adb/apd -V 2>/dev/null | head -1)"; }
[ -f /data/adb/ap/package_config ] && echo "A_PC=1"
echo "PKGS=$(pm list packages 2>/dev/null | sed 's/^package://' | grep -E '%(apps)s' | tr '\n' ' ')"
[ -n "$M" ] && [ -f /data/adb/magisk.db ] && echo "M_MGR=$($M --sqlite "SELECT value FROM strings WHERE key='requester'" 2>/dev/null | sed 's/^value=//')"
z=$(pidof zygote64 2>/dev/null || pidof zygote 2>/dev/null)
for p in $z; do grep -qi zygisk /proc/$p/maps 2>/dev/null && echo "Z_LOADED=1"; done
'''


def detect(conn: B.Connection) -> Manager:
    apps = "|".join("^" + re.escape(p) + "$" for p in MANAGER_APPS)
    rc, out, err = conn.exec(DETECT % {"apps": apps}, timeout=40)
    d = _kv(out.decode(errors="replace"))
    pkgs = d.get("PKGS", "").split()
    m = Manager(zygisk_loaded=d.get("Z_LOADED") == "1")
    if d.get("K_BIN"):
        m.kind, m.binary = KERNELSU, d["K_BIN"]
        m.version = d.get("K_V", "").replace("ksud ", "").strip()
        m.extra["kernel_version"] = d.get("K_KV", "")
        m.name = next((MANAGER_APPS[p] for p in pkgs if p in MANAGER_APPS and "su" in MANAGER_APPS[p].lower()
                       and "Magisk" not in MANAGER_APPS[p]), "KernelSU")
    elif d.get("A_BIN"):
        m.kind, m.binary = APATCH, d["A_BIN"]
        m.version = d.get("A_V", "").replace("apd ", "").strip()
        m.name = next((MANAGER_APPS[p] for p in pkgs if "APatch" in MANAGER_APPS.get(p, "")), "APatch")
        m.extra["package_config"] = d.get("A_PC") == "1"
    elif d.get("M_BIN"):
        m.binary = d["M_BIN"]
        v = d.get("M_V", "")
        m.version = v.split(":")[0]
        m.version_code = d.get("M_VC", "")
        kitsune = bool(re.search(r"kitsune|delta", v, re.I)) or "io.github.huskydg.magisk" in pkgs
        m.kind = KITSUNE if kitsune else MAGISK
        m.name = "Kitsune Mask" if kitsune else ("Magisk Alpha" if "io.github.vvb2060.magisk" in pkgs
                                                  or "alpha" in v.lower() else "Magisk")
        m.extra["flavor"] = v.split(":")[1] if ":" in v else ""
    else:
        return m
    for p in pkgs:
        if p in MANAGER_APPS:
            m.app_pkg = p
            break
    if m.magisk_family:
        req = d.get("M_MGR", "").strip()
        if req and req not in MANAGER_APPS:
            m.app_pkg, m.app_hidden = req, True
        elif req:
            m.app_pkg = req
        _detect_denylist(conn, m)
    return m


def _detect_denylist(conn: B.Connection, m: Manager):
    for cli in ("--denylist", "--hide"):
        rc, out, err = conn.exec(f"{q(m.binary)} {cli} status 2>&1", timeout=20)
        t = out.decode(errors="replace").strip()
        if re.search(r"enforc|enabl|disabl|is not|is ", t, re.I) and "Unrecognized" not in t \
                and "usage" not in t.lower()[:20]:
            m.denylist_cli = cli
            m.denylist_on = rc == 0 and not re.search(r"not|disabl", t, re.I)
            return


# ---------------------------------------------------------------------------
# Magisk database
# ---------------------------------------------------------------------------
def _sql(conn: B.Connection, m: Manager, sql: str) -> list[dict]:
    rc, out, err = conn.exec(f"{q(m.binary)} --sqlite {q(sql)}", timeout=30)
    if rc != 0:
        raise B.BackendError((err or out).decode(errors="replace").strip() or f"sqlite failed ({rc})")
    rows = []
    for line in out.decode(errors="replace").splitlines():
        if not line.strip():
            continue
        r = {}
        for part in line.split("|"):
            k, _, v = part.partition("=")
            r[k] = v
        rows.append(r)
    return rows


def magisk_settings(conn: B.Connection, m: Manager) -> dict[str, int]:
    vals = {k: dflt for k, _, dflt, _, _ in MAGISK_SETTINGS}
    for r in _sql(conn, m, "SELECT key, value FROM settings"):
        try:
            vals[r.get("key", "")] = int(r.get("value", "0"))
        except ValueError:
            pass
    return vals


def set_magisk_setting(conn: B.Connection, m: Manager, key: str, value: int):
    if not re.fullmatch(r"[a-z_]+", key):
        raise ValueError(key)
    _sql(conn, m, f"REPLACE INTO settings (key, value) VALUES ('{key}', {int(value)})")
    if key == "denylist" and m.denylist_cli:   # the daemon only picks this up through its CLI
        conn.exec(f"{q(m.binary)} {m.denylist_cli} {'enable' if value else 'disable'} 2>&1", timeout=30)


@dataclass
class Policy:
    uid: int
    policy: int
    until: int
    logging: bool
    notification: bool
    packages: list = field(default_factory=list)


def su_policies(conn: B.Connection, m: Manager) -> list[Policy]:
    rows = _sql(conn, m, "SELECT uid, policy, until, logging, notification FROM policies")
    rc, out, _ = conn.exec("pm list packages -U 2>/dev/null", timeout=40)
    by_uid: dict[int, list[str]] = {}
    for line in out.decode(errors="replace").splitlines():
        mm = re.match(r"package:(\S+) uid:(\d+)", line.strip())
        if mm:
            for u in mm.group(2).split(","):
                by_uid.setdefault(int(u), []).append(mm.group(1))
    res = []
    for r in rows:
        try:
            uid = int(r["uid"])
            p = Policy(uid, int(r.get("policy", 0)), int(r.get("until", 0)), r.get("logging") == "1",
                       r.get("notification") == "1")
        except (KeyError, ValueError):
            continue
        app_uid = uid % 100000
        p.packages = by_uid.get(app_uid, []) or ({0: ["root"], 1000: ["android (system)"], 2000: ["shell (ADB)"]}
                                                 .get(app_uid, []))
        res.append(p)
    return sorted(res, key=lambda p: (p.packages[0] if p.packages else "~", p.uid))


def set_policy(conn: B.Connection, m: Manager, uid: int, policy: int | None = None,
               logging: bool | None = None, notification: bool | None = None, delete: bool = False):
    uid = int(uid)
    if delete:
        _sql(conn, m, f"DELETE FROM policies WHERE uid={uid}")
        return
    sets = []
    if policy is not None:
        sets.append(f"policy={int(policy)}")
    if logging is not None:
        sets.append(f"logging={int(bool(logging))}")
    if notification is not None:
        sets.append(f"notification={int(bool(notification))}")
    if sets:
        _sql(conn, m, f"UPDATE policies SET {', '.join(sets)} WHERE uid={uid}")


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------
LIST_MODULES = r'''
for base in /data/adb/modules /data/adb/modules_update; do
  for d in $base/*; do
    [ -d "$d" ] || continue
    echo "@@MOD	$base	$(basename "$d")"
    for f in disable remove update skip_mount action.sh service.sh post-fs-data.sh uninstall.sh system.prop sepolicy.rule zygisk webroot system; do
      [ -e "$d/$f" ] && echo "@@F	$f"
    done
    echo "@@SIZE	$(du -sk "$d" 2>/dev/null | cut -f1)"
    echo "@@OTHER	$(ls -A "$d" 2>/dev/null | grep -v -x -E 'module.prop|update|disable|remove' | wc -l)"
    echo "@@PROP	$(base64 -w 0 "$d/module.prop" 2>/dev/null)"
  done
done
'''


def list_modules(conn: B.Connection) -> list[Module]:
    rc, out, err = conn.exec(LIST_MODULES, timeout=60)
    mods: dict[str, Module] = {}
    pend: dict[str, Module] = {}
    cur: Module | None = None
    for line in out.decode(errors="replace").splitlines():
        tag, _, rest = line.partition("\t")
        if tag == "@@MOD":
            base, _, mid = rest.partition("\t")
            cur = Module(mid, f"{base}/{mid}")
            (pend if base.endswith("_update") else mods)[mid] = cur
        elif cur is None:
            continue
        elif tag == "@@F":
            cur.flags.add(rest)
        elif tag == "@@OTHER":
            if rest.strip() == "0":
                cur.flags.add("_stub")
        elif tag == "@@SIZE":
            cur.size_kb = int(rest) if rest.isdigit() else 0
        elif tag == "@@PROP":
            try:
                cur.props = _kv(base64.b64decode(rest).decode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                pass
    for mid, p in pend.items():
        if mid in mods and "_stub" in mods[mid].flags and "update" in mods[mid].flags:
            # Magisk stages a brand-new install as modules/<id> holding only module.prop + update
            p.pending_install = True
            p.flags.add("_stub_at:" + mods[mid].path)
            mods[mid] = p
        elif mid in mods:
            mods[mid].flags.add("update")
            if p.props:
                mods[mid].props.setdefault("_new_version", p.props.get("version", ""))
        else:
            p.pending_install = True
            mods[mid] = p
    return sorted(mods.values(), key=lambda x: x.name.lower())


def _ok(rc, out, err, what):
    if rc != 0:
        raise B.BackendError(f"{what} failed: " + ((err + out).decode(errors="replace").strip() or f"exit {rc}"))
    return (out + err).decode(errors="replace")


def set_module_enabled(conn: B.Connection, m: Manager, mod: Module, on: bool):
    d = q(mod.path)
    if m.kind in (KERNELSU, APATCH):
        rc, out, err = conn.exec(f"{q(m.binary)} module {'enable' if on else 'disable'} {q(mod.id)} 2>&1", timeout=60)
        if rc == 0:
            return
        log.info("%s module enable/disable failed, using flag file: %s", m.kind, out[:300])
    _ok(*conn.exec(f"rm -f {d}/disable" if on else f"touch {d}/disable", timeout=20), "Changing the module")


def uninstall_module(conn: B.Connection, m: Manager, mod: Module):
    d = q(mod.path)
    if mod.pending_install:    # never activated: just drop the staged copy (and Magisk's placeholder)
        stub = next((f[len("_stub_at:"):] for f in mod.flags if f.startswith("_stub_at:")), "")
        _ok(*conn.exec(f"rm -rf {d}" + (f" {q(stub)}" if stub else ""), timeout=60), "Removing the staged module")
        return
    if m.kind in (KERNELSU, APATCH):
        rc, out, err = conn.exec(f"{q(m.binary)} module uninstall {q(mod.id)} 2>&1", timeout=60)
        if rc == 0:
            return
        log.info("%s module uninstall failed, using flag file: %s", m.kind, out[:300])
    _ok(*conn.exec(f"touch {d}/remove", timeout=20), "Marking the module for removal")


def restore_module(conn: B.Connection, m: Manager, mod: Module):
    if m.kind == KERNELSU:
        rc, out, err = conn.exec(f"{q(m.binary)} module undo-uninstall {q(mod.id)} 2>&1", timeout=30)
        if rc == 0:
            return
    _ok(*conn.exec(f"rm -f {q(mod.path)}/remove", timeout=20), "Restoring the module")


DEVICE_ZIP = "/data/local/tmp/androunderthehood-module.zip"


def install_command(m: Manager, path: str = DEVICE_ZIP) -> str:
    if m.kind in (KERNELSU, APATCH):
        return f"{q(m.binary)} module install {q(path)} 2>&1"
    return f"{q(m.binary)} --install-module {q(path)} 2>&1"


def action_command(m: Manager, mod: Module) -> str:
    if m.kind == KERNELSU:
        return f"{q(m.binary)} module action {q(mod.id)} 2>&1"
    bb = m.busybox
    return (f"cd {q(mod.path)} && export ASH_STANDALONE=1 MODPATH={q(mod.path)} && "
            f"if [ -x {bb} ]; then {bb} sh ./action.sh; else sh ./action.sh; fi 2>&1")


# ---------------------------------------------------------------------------
# DenyList (Magisk / Kitsune) and APatch exclude list
# ---------------------------------------------------------------------------
@dataclass
class AppProcs:
    pkg: str
    label: str = ""
    system: bool = False
    procs: dict = field(default_factory=dict)   # process name → isolated(bool)

    @property
    def name(self):
        return self.label or self.pkg


def load_app_procs(conn: B.Connection, fs) -> dict[str, AppProcs]:
    from .apps import HELPER_RUN, ensure_helper
    ensure_helper(conn, fs)
    rc, out, err = conn.exec(f"{HELPER_RUN} procs 2>/dev/null", timeout=180)
    apps: dict[str, AppProcs] = {}
    for line in out.decode("utf-8", errors="replace").splitlines():
        p = line.split("\t")
        if p[0] == "A" and len(p) >= 4:
            apps[p[1]] = AppProcs(p[1], p[3], p[2] == "1")
        elif p[0] == "P" and len(p) >= 4 and p[1] in apps:
            apps[p[1]].procs[p[2]] = p[3] == "1"
    if not apps:
        raise B.BackendError("Could not list the installed apps: " + (err or out[:200]).decode(errors="replace"))
    return apps


def denylist(conn: B.Connection, m: Manager) -> set[tuple[str, str]]:
    rc, out, err = conn.exec(f"{q(m.binary)} {m.denylist_cli} ls 2>&1", timeout=40)
    res = set()
    for line in out.decode(errors="replace").splitlines():
        pkg, sep, proc = line.strip().partition("|")
        if sep:
            res.add((pkg, proc))
    return res


def denylist_change(conn: B.Connection, m: Manager, add: list[tuple[str, str]], rm: list[tuple[str, str]]) -> str:
    """Apply many add/rm operations in one shell round trip. Returns any error output."""
    def arg(p, pr):
        return f"{q(p)} {q(pr)}" if pr else q(p)

    cmds = [f"{q(m.binary)} {m.denylist_cli} rm {arg(p, pr)} >/dev/null 2>&1 || echo 'rm {p} {pr} failed'"
            for p, pr in rm]
    cmds += [f"{q(m.binary)} {m.denylist_cli} add {arg(p, pr)} >/dev/null 2>&1 || echo 'add {p} {pr} failed'"
             for p, pr in add]
    msgs = []
    for i in range(0, len(cmds), 60):
        rc, out, err = conn.exec("; ".join(cmds[i:i + 60]), timeout=120)
        msgs.append(out.decode(errors="replace").strip())
    return "\n".join(x for x in msgs if x)


APATCH_PC = "/data/adb/ap/package_config"
APATCH_HEADER = "pkg,exclude,allow,uid,to_uid,sctx"


def apatch_config(conn: B.Connection) -> tuple[list[str], dict[str, list[str]]]:
    rc, out, err = conn.exec(f"cat {APATCH_PC} 2>/dev/null", timeout=20)
    lines = out.decode(errors="replace").splitlines()
    header = lines[0].split(",") if lines and lines[0].startswith("pkg") else APATCH_HEADER.split(",")
    rows = {}
    for line in lines[1:] if lines and lines[0].startswith("pkg") else lines:
        p = line.split(",")
        if len(p) >= 2 and p[0]:
            rows[p[0]] = p
    return header, rows


def apatch_set_excluded(conn: B.Connection, changes: dict[str, bool]):
    """Set the 'exclude' column (APatch's DenyList equivalent). Keeps a backup of the file."""
    header, rows = apatch_config(conn)
    ix = header.index("exclude") if "exclude" in header else 1
    need_uid = [p for p in changes if p not in rows]
    uids: dict[str, str] = {}
    if need_uid:
        rc, out, _ = conn.exec("pm list packages -U 2>/dev/null", timeout=40)
        for line in out.decode(errors="replace").splitlines():
            mm = re.match(r"package:(\S+) uid:(\d+)", line.strip())
            if mm:
                uids[mm.group(1)] = mm.group(2).split(",")[0]
    for pkg, on in changes.items():
        if pkg not in rows:
            if not on:
                continue
            r = [""] * len(header)
            r[0] = pkg
            for k, v in (("allow", "0"), ("uid", uids.get(pkg, "0")), ("to_uid", "0"), ("sctx", "u:r:untrusted_app:s0")):
                if k in header:
                    r[header.index(k)] = v
            rows[pkg] = r
        rows[pkg] += [""] * (len(header) - len(rows[pkg]))
        rows[pkg][ix] = "1" if on else "0"
    text = ",".join(header) + "\n" + "".join(",".join(r) + "\n" for r in rows.values())
    _ok(*conn.exec(f"cp -p {APATCH_PC} {APATCH_PC}.androunderthehood.bak 2>/dev/null; cat > {APATCH_PC}.new && "
                   f"chmod 600 {APATCH_PC}.new && mv -f {APATCH_PC}.new {APATCH_PC}",
                   stdin=text.encode(), timeout=30), "Writing package_config")
