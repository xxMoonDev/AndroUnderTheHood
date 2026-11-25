"""Play Integrity toolkit – device logic (no Qt here).

Works with every root manager AndroUnderTheHood knows (Magisk, Kitsune, Magisk Alpha, KernelSU and
its forks, APatch) through androunderthehood.rootmgr. Modules come only from their official GitHub
releases; the asset's SHA-256 published by GitHub is checked before anything goes to the phone.
Keyboxes are never downloaded – only a file the user picks is used.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from androunderthehood import backend as B
from androunderthehood import rootmgr as R

q = B.shq
UA = {"User-Agent": "AndroUnderTheHood-integrity-plugin", "Accept": "application/vnd.github+json"}
TS_DIR = "/data/adb/tricky_store"
TEESIM_DIR = "/data/adb/teesim"
PIF_ID = "playintegrityfix"
CHECKER_PKG = "com.henrikherzig.playintegritychecker"
CHECKER_APK = "https://api.github.com/repos/herzhenr/spic-android/releases/latest"
REVOCATION_URL = "https://android.googleapis.com/attestation/status"

# Google apps that always need the keystore spoof (Play services, Play Store, GSF, Wallet, Pay)
CORE_TARGETS = ["com.google.android.gms", "com.google.android.gsf", "com.android.vending",
                "com.google.android.apps.walletnfcrel", "com.google.android.apps.nbu.paisa.user"]


@dataclass
class Component:
    key: str
    name: str
    repo: str
    kind: str            # "tricky" | "pif" | "zygisk" | "hide"
    module_id: str
    note: str = ""
    asset_skip: str = r"(?i)debug"


COMPONENTS = {c.key: c for c in [
    Component("tsoss", "Tricky Store OSS", "beakthoven/TrickyStoreOSS", "tricky", "tricky_store",
              "Open source (GPL-3.0), actively maintained. Recommended."),
    Component("ts", "Tricky Store (5ec1cff)", "5ec1cff/TrickyStore", "tricky", "tricky_store",
              "The original. Closed source."),
    Component("teesim", "TEESimulator", "JingMatrix/TEESimulator", "tricky", "teesim",
              "Open source. Simulates KeyMint in-process; uses /data/adb/teesim/config.json."),
    Component("pif_fork", "Play Integrity Fork (osm0sis)", "osm0sis/PlayIntegrityFork", "pif", PIF_ID,
              "Recommended alongside Tricky Store. autopif4 fingerprints."),
    Component("pif_inject", "Play Integrity Fix [INJECT] (KOWX712)", "KOWX712/PlayIntegrityFix", "pif", PIF_ID,
              "Alternative PIF with a WebUI."),
    Component("zygisk_next", "Zygisk Next", "Dr-TSNG/ZygiskNext", "zygisk", "zygisksu",
              "Most widely used Zygisk for KernelSU/APatch. Closed source."),
    Component("neozygisk", "NeoZygisk", "JingMatrix/NeoZygisk", "zygisk", "zygisksu", "Open source (GPL-3.0)."),
    Component("rezygisk", "ReZygisk", "PerformanC/ReZygisk", "zygisk", "rezygisk", "Open source (GPL-3.0)."),
    Component("zygisk_assistant", "Zygisk Assistant", "snake-4/Zygisk-Assistant", "hide", "zygisk-assistant",
              "Open source root hiding (DenyList based). Optional."),
]}

# installed modules that would fight with the component kind we install
CONFLICTS = {
    "tricky": {"tricky_store", "teesim", "oh_my_keymint", "teesimulator_rs", "TEESimulator-RS"},
    "pif": {PIF_ID, "safetynet-fix", "playcurl", "pif", "PlayIntegrityNEXT", "playintegrityfix_next"},
    "zygisk": {"zygisksu", "rezygisk", "neozygisk"},
    "hide": {"zygisk-assistant", "zygisk_shamiko", "nohello", "treat_wheel"},
}


# ---------------------------------------------------------------------------
# device helpers
# ---------------------------------------------------------------------------
def sh(conn, cmd: str, timeout: float = 60, stdin: bytes | None = None) -> tuple[int, str]:
    rc, out, err = conn.exec(cmd, timeout=timeout, stdin=stdin, as_root=True)
    return rc, (out + err).decode("utf-8", "replace")


def write_file(conn, path: str, data: bytes, mode: str = "644"):
    """Write a small text file as root. Via base64 so it is safe over ADB (0x1A) and SSH alike."""
    b64 = base64.b64encode(data).decode()
    tmp = path + ".aunt-new"
    cmd = f"echo {q(b64)} | base64 -d > {q(tmp)} && chmod {mode} {q(tmp)} && mv -f {q(tmp)} {q(path)}"
    if len(cmd) > 100_000:
        raise B.BackendError("file too large for write_file")
    rc, out = sh(conn, cmd, timeout=30)
    if rc != 0:
        raise B.BackendError(f"Writing {path} failed: {out.strip() or rc}")


def backup_file(conn, path: str):
    sh(conn, f"[ -f {q(path)} ] && cp -p {q(path)} {q(path)}.bak", timeout=20)


def push(conn, local: str, remote: str):
    if hasattr(conn, "upload_stream"):       # ADB: binary-safe push
        conn.upload_stream(local, remote, lambda n: None)
        return
    with open(local, "rb") as f:
        data = f.read()
    rc, out, err = conn.exec(f"cat > {q(remote)}", stdin=data, timeout=300, as_root=False)
    if rc != 0:
        raise B.BackendError((err or out).decode(errors="replace") or "copy failed")


# ---------------------------------------------------------------------------
# GitHub releases
# ---------------------------------------------------------------------------
def http_get(url: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


@dataclass
class Release:
    comp: Component
    tag: str
    asset: str
    url: str
    size: int
    sha256: str          # "" when GitHub has no digest for the asset
    published: str


def latest_release(comp: Component, cache_dir: str) -> Release:
    os.makedirs(cache_dir, exist_ok=True)
    cpath = os.path.join(cache_dir, f"release-{comp.key}.json")
    d = None
    try:
        if time.time() - os.path.getmtime(cpath) < 3600:
            with open(cpath, encoding="utf-8") as f:
                d = json.load(f)
    except OSError:
        pass
    if d is None:
        d = json.loads(http_get(f"https://api.github.com/repos/{comp.repo}/releases/latest"))
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump(d, f)
    assets = [a for a in d.get("assets", []) if a["name"].lower().endswith(".zip")
              and not re.search(comp.asset_skip, a["name"])]
    if not assets:
        raise B.BackendError(f"{comp.name}: no module zip in release {d.get('tag_name')}")
    a = assets[0]
    dig = a.get("digest") or ""
    return Release(comp, d.get("tag_name", ""), a["name"], a["browser_download_url"], a["size"],
                   dig.split(":", 1)[1].lower() if dig.startswith("sha256:") else "", d.get("published_at", "")[:10])


def download(rel: Release, cache_dir: str, say=lambda s: None) -> str:
    path = os.path.join(cache_dir, rel.asset)
    if os.path.exists(path) and os.path.getsize(path) == rel.size and _sha_ok(path, rel.sha256):
        say(f"  using cached {rel.asset}\n")
        return path
    say(f"  downloading {rel.asset} ({rel.size / 1048576:.1f} MB) from github.com/{rel.comp.repo}\n")
    data = http_get(rel.url, timeout=300)
    if len(data) != rel.size:
        raise B.BackendError(f"{rel.asset}: size {len(data)} != {rel.size}")
    h = hashlib.sha256(data).hexdigest()
    if rel.sha256 and h != rel.sha256:
        raise B.BackendError(f"{rel.asset}: SHA-256 mismatch (got {h}, GitHub says {rel.sha256})")
    say(f"  SHA-256 {h} " + ("matches GitHub\n" if rel.sha256 else "(GitHub publishes no digest for this file)\n"))
    with open(path + ".part", "wb") as f:
        f.write(data)
    os.replace(path + ".part", path)
    return path


def _sha_ok(path, want):
    if not want:
        return True
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest() == want


# ---------------------------------------------------------------------------
# state detection
# ---------------------------------------------------------------------------
DETECT = r'''
for p in sdk:ro.build.version.sdk rel:ro.build.version.release abi:ro.product.cpu.abi \
  patch:ro.build.version.security_patch vpatch:ro.vendor.build.security_patch model:ro.product.model \
  fp:ro.build.fingerprint vbs:ro.boot.verifiedbootstate locked:ro.boot.flash.locked \
  vbmeta:ro.boot.vbmeta.device_state first_api:ro.product.first_api_level; do
  echo "${p%%:*}=$(getprop ${p#*:})"
done
[ -d /data/adb/tricky_store ] && echo "ts_dir=1"
for f in target.txt security_patch.txt tee_status; do
  [ -f /data/adb/tricky_store/$f ] && echo "ts_$f=$(base64 -w0 /data/adb/tricky_store/$f)"
done
[ -f /data/adb/tricky_store/keybox.xml ] && echo "ts_keybox=1"
[ -f /data/adb/teesim/config.json ] && echo "teesim_cfg=$(base64 -w0 /data/adb/teesim/config.json)"
[ -f /data/adb/teesim/keybox.xml ] && echo "teesim_keybox=1"
for d in /data/adb/modules/playintegrityfix /data/adb/modules_update/playintegrityfix; do
  for f in custom.pif.prop custom.pif.json pif.prop pif.json; do
    if [ -f $d/$f ]; then
      echo "pif_file=$d/$f"
      echo "pif_fp=$(grep -m1 -oE '(FINGERPRINT|"FINGERPRINT")[^,]*' $d/$f | sed -E 's/^"?FINGERPRINT"?[=: ]*"?//; s/"$//')"
      break 2
    fi
  done
done
echo "procs=$(ps -A -o NAME,ARGS 2>/dev/null | grep -iE 'tricky|teesim|zygiskd|zygisk' | grep -v grep | tr '\n' ';')"
echo "checker=$(pm path com.henrikherzig.playintegritychecker 2>/dev/null | head -1)"
'''


@dataclass
class State:
    mgr: R.Manager
    props: dict
    modules: list
    targets: list = field(default_factory=list)       # raw target.txt lines (TrickyStore)
    security_patch: str = ""
    tee_status: str = ""
    teesim_cfg: dict | None = None
    keybox_path: str = ""
    keybox: "KeyboxInfo | None" = None
    procs: str = ""

    def module(self, mid: str):
        return next((m for m in self.modules if m.id == mid), None)

    @property
    def sdk(self) -> int:
        try:
            return int(self.props.get("sdk", "0"))
        except ValueError:
            return 0

    @property
    def zygisk(self) -> str:
        """'' when nothing provides Zygisk, else a description."""
        if self.mgr.magisk_family:
            on = self.props.get("_zygisk_setting") == 1
            if on and self.mgr.zygisk_loaded:
                return "Magisk built-in (active)"
            if on:
                return "Magisk built-in (enabled, active after reboot)"
        for mid in R.ZYGISK_PROVIDERS:
            m = self.module(mid)
            if m and m.enabled and not m.removing:
                return f"{m.name} {m.props.get('version', '')}" + ("" if self.mgr.zygisk_loaded else " (after reboot)")
        if self.mgr.zygisk_loaded:
            return "loaded"
        return ""

    def installed(self, kind: str):
        """(component key or '?', Module) of the installed module of that kind, or (None, None)."""
        for m in self.modules:
            if m.removing:
                continue
            k = identify(m)
            if k and COMPONENTS.get(k, None) and COMPONENTS[k].kind == kind:
                return k, m
            if not k and m.id in CONFLICTS[kind]:
                return "?", m
        return None, None


def identify(mod) -> str:
    name = (mod.props.get("name", "") + " " + mod.props.get("author", "")).lower()
    if mod.id == "tricky_store":
        return "tsoss" if "oss" in name or "beakthoven" in name else "ts"
    if mod.id == "teesim":
        return "teesim"
    if mod.id == PIF_ID:
        if "fork" in name or "osm0sis" in name:
            return "pif_fork"
        if "inject" in name or "kowx712" in name:
            return "pif_inject"
        return ""
    if mod.id == "zygisksu":
        return "neozygisk" if "neozygisk" in name or "jingmatrix" in name else "zygisk_next"
    if mod.id == "rezygisk":
        return "rezygisk"
    if mod.id == "zygisk-assistant":
        return "zygisk_assistant"
    return ""


def _kv(text):
    d = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep and re.fullmatch(r"[a-z_.]+", k):
            d[k] = v.strip()
    return d


def _b64(s):
    try:
        return base64.b64decode(s).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def detect(conn) -> State:
    mgr = R.detect(conn)
    if not mgr.kind:
        raise B.BackendError("No root manager found (Magisk, KernelSU or APatch is required).")
    rc, out = sh(conn, DETECT, timeout=60)
    p = _kv(out)
    if mgr.magisk_family:
        try:
            p["_zygisk_setting"] = R.magisk_settings(conn, mgr).get("zygisk", 0)
        except B.BackendError:
            pass
    st = State(mgr, p, R.list_modules(conn))
    st.targets = _b64(p.get("ts_target.txt", "")).splitlines()
    st.security_patch = _b64(p.get("ts_security_patch.txt", "")).strip()
    st.tee_status = _b64(p.get("ts_tee_status", "")).strip()
    st.procs = p.get("procs", "")
    if p.get("teesim_cfg"):
        try:
            st.teesim_cfg = json.loads(_b64(p["teesim_cfg"]))
        except ValueError:
            st.teesim_cfg = None
    k, _ = st.installed("tricky")
    if k == "teesim" and p.get("teesim_keybox"):
        st.keybox_path = f"{TEESIM_DIR}/keybox.xml"
    elif p.get("ts_keybox"):
        st.keybox_path = f"{TS_DIR}/keybox.xml"
    if st.keybox_path:
        rc, kb = sh(conn, f"cat {q(st.keybox_path)}", timeout=20)
        if rc == 0:
            try:
                st.keybox = parse_keybox(kb.encode())
            except Exception as e:  # noqa: BLE001
                st.keybox = KeyboxInfo(error=str(e))
    return st


# ---------------------------------------------------------------------------
# keybox parsing (certificates only; private keys are never looked at or sent anywhere)
# ---------------------------------------------------------------------------
@dataclass
class Cert:
    serial: str
    subject: str
    issuer: str
    not_after: str


@dataclass
class KeyboxInfo:
    keys: list = field(default_factory=list)      # [(algorithm, [Cert …])]
    device_id: str = ""
    error: str = ""
    revoked: list = field(default_factory=list)   # serials found on Google's list
    checked: bool = False

    @property
    def kind(self) -> str:
        if self.error:
            return "invalid"
        roots = " ".join((c[-1].subject + " " + c[-1].issuer) for _, c in self.keys if c).lower()
        if "software" in roots or ("android" in roots and "google" not in roots and "f92009e853b6b045" not in roots):
            return "software"
        if "f92009e853b6b045" in roots or "google" in roots or "key attestation" in roots:
            return "hardware"
        return "unknown"

    def summary(self) -> str:
        if self.error:
            return f"Invalid keybox: {self.error}"
        algs = ", ".join(a for a, _ in self.keys) or "no keys"
        kind = {"software": "AOSP software keybox – BASIC/DEVICE only",
                "hardware": "Hardware keybox (Google root)", "unknown": "Keybox with unknown root"}[self.kind]
        s = f"{kind}; {algs}"
        if self.device_id:
            s += f"; DeviceID {self.device_id}"
        if self.checked:
            s += "; REVOKED by Google" if self.revoked else "; not on Google's revocation list"
        return s


def _der(b: bytes, i: int):
    tag = b[i]
    ln = b[i + 1]
    i += 2
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(b[i:i + n], "big")
        i += n
    return tag, i, i + ln


def _children(b, start, end):
    out = []
    i = start
    while i < end:
        t, s, e = _der(b, i)
        out.append((t, s, e))
        i = e
    return out


_OIDS = {b"\x55\x04\x03": "CN", b"\x55\x04\x0a": "O", b"\x55\x04\x0b": "OU", b"\x55\x04\x05": "serialNumber",
         b"\x55\x04\x0c": "title", b"\x55\x04\x06": "C"}


def _name(b, s, e) -> str:
    parts = []
    for _t, ss, se in _children(b, s, e):              # SET
        for _t2, s2, e2 in _children(b, ss, se):       # SEQ {oid, value}
            kids = _children(b, s2, e2)
            if len(kids) == 2:
                oid = b[kids[0][1]:kids[0][2]]
                val = b[kids[1][1]:kids[1][2]].decode("utf-8", "replace")
                parts.append(f"{_OIDS.get(oid, '?')}={val}")
    return ", ".join(parts)


def parse_cert(der: bytes) -> Cert:
    _t, s, e = _der(der, 0)                 # Certificate
    _t, ts, te = _der(der, s)               # tbsCertificate
    kids = _children(der, ts, te)
    if kids and kids[0][0] == 0xA0:         # [0] version
        kids = kids[1:]
    serial = int.from_bytes(der[kids[0][1]:kids[0][2]], "big")
    issuer = _name(der, kids[2][1], kids[2][2])
    validity = _children(der, kids[3][1], kids[3][2])
    not_after = der[validity[1][1]:validity[1][2]].decode(errors="replace") if len(validity) > 1 else ""
    subject = _name(der, kids[4][1], kids[4][2])
    return Cert(format(serial, "x"), subject, issuer, not_after)


def parse_keybox(data: bytes) -> KeyboxInfo:
    root = ET.fromstring(data.decode("utf-8", "replace").strip().lstrip("﻿"))
    if root.tag != "AndroidAttestation":
        raise ValueError("root element is not <AndroidAttestation>")
    info = KeyboxInfo()
    kb = root.find("Keybox")
    if kb is None:
        raise ValueError("no <Keybox>")
    info.device_id = kb.get("DeviceID", "")
    for key in kb.findall("Key"):
        alg = key.get("algorithm", "?")
        if key.find("PrivateKey") is None:
            raise ValueError(f"{alg} key has no <PrivateKey>")
        certs = []
        for c in key.findall("CertificateChain/Certificate"):
            pem = re.sub(r"-----[^-]+-----|\s", "", c.text or "")
            certs.append(parse_cert(base64.b64decode(pem)))
        if not certs:
            raise ValueError(f"{alg} key has no certificates")
        info.keys.append((alg, certs))
    if not info.keys:
        raise ValueError("no <Key> entries")
    return info


def check_revocation(info: KeyboxInfo, cache_dir: str) -> KeyboxInfo:
    """Compare the chain serials with Google's public attestation revocation list (downloaded, nothing sent)."""
    path = os.path.join(cache_dir, "attestation-status.json")
    data = None
    try:
        if time.time() - os.path.getmtime(path) < 6 * 3600:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
    except (OSError, ValueError):
        pass
    if data is None:
        data = json.loads(http_get(REVOCATION_URL))
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    entries = {k.lower().lstrip("0") for k in data.get("entries", {})}
    info.revoked = [c.serial for _, chain in info.keys for c in chain if c.serial.lstrip("0") in entries]
    info.checked = True
    return info


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
def default_patch_date(today: dt.date | None = None) -> str:
    """The newest monthly patch that is surely published: this month from the 8th, else last month."""
    d = today or dt.date.today()
    if d.day < 8:
        d = (d.replace(day=1) - dt.timedelta(days=1))
    return f"{d.year:04d}-{d.month:02d}-01"


def user_packages(conn) -> list[str]:
    rc, out = sh(conn, "pm list packages -3 2>/dev/null", timeout=60)
    return sorted(line[8:].strip() for line in out.splitlines() if line.startswith("package:"))


def merge_targets(existing: list[str], add: list[str]) -> list[str]:
    """Keep existing lines (and their ?/! suffixes), append missing packages."""
    have = {re.sub(r"[?!]$", "", ln.split("#")[0].strip()) for ln in existing}
    out = [ln for ln in existing if ln.strip()]
    out += [p for p in add if p not in have]
    return out


def write_tricky_config(conn, targets: list[str] | None, patch: str | None):
    sh(conn, f"mkdir -p {TS_DIR}", timeout=20)
    if targets is not None:
        backup_file(conn, f"{TS_DIR}/target.txt")
        write_file(conn, f"{TS_DIR}/target.txt", ("\n".join(targets) + "\n").encode())
    if patch is not None:
        backup_file(conn, f"{TS_DIR}/security_patch.txt")
        if patch == "":
            sh(conn, f"rm -f {TS_DIR}/security_patch.txt", timeout=20)
        else:
            # the simple YYYYMMDD form (all three patch levels) is understood by every Tricky Store version
            write_file(conn, f"{TS_DIR}/security_patch.txt", (patch.replace("-", "") + "\n").encode())


def write_teesim_config(conn, cfg: dict | None, targets: list[str] | None, patch: str | None):
    cfg = cfg or {"version": 1, "profiles": {"default": {"keybox": "keybox.xml", "mode": "patch", "apps": []}}}
    prof = cfg.setdefault("profiles", {}).setdefault("default", {})
    if targets is not None:
        prof["apps"] = [re.sub(r"[?!]$", "", t.split("#")[0].strip()) for t in targets if t.strip()]
    if patch is not None:
        prof["patchLevel"] = ({"system": "today", "vendor": "YYYY-MM-05", "boot": "YYYY-MM-05"} if patch == ""
                              else {"system": patch, "vendor": patch, "boot": patch})
    sh(conn, f"mkdir -p {TEESIM_DIR}", timeout=20)
    backup_file(conn, f"{TEESIM_DIR}/config.json")
    write_file(conn, f"{TEESIM_DIR}/config.json", json.dumps(cfg, indent=2).encode(), mode="600")


def install_keybox(conn, data: bytes, teesim: bool):
    parse_keybox(data)                                  # refuse anything that is not a keybox
    path = f"{TEESIM_DIR if teesim else TS_DIR}/keybox.xml"
    sh(conn, f"mkdir -p {q(os.path.dirname(path).replace(os.sep, '/'))}", timeout=20)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    sh(conn, f"[ -f {path} ] && cp -p {path} {path}.bak-{stamp}", timeout=20)
    write_file(conn, path, data, mode="600")
    return path


def fix_denylist(conn, st: State) -> str:
    """With Magisk's DenyList enforced, Zygisk modules are unloaded from denylisted processes – PIF must
    reach Play services, so com.google.android.gms is taken off the DenyList (as PIF's docs require)."""
    m = st.mgr
    if not (m.magisk_family and m.denylist_cli and m.denylist_on):
        return ""
    entries = [(p, pr) for p, pr in R.denylist(conn, m) if p == "com.google.android.gms"]
    if not entries:
        return ""
    sh(conn, f"{q(m.binary)} {m.denylist_cli} rm com.google.android.gms 2>&1", timeout=30)
    return f"Removed com.google.android.gms ({len(entries)} processes) from the DenyList."


# ---------------------------------------------------------------------------
# PIF helpers
# ---------------------------------------------------------------------------
def pif_dir(st: State) -> str:
    m = st.module(PIF_ID)
    return m.path if m else f"/data/adb/modules/{PIF_ID}"


def autopif_command(st: State) -> str:
    k, m = st.installed("pif")
    if not m:
        raise B.BackendError("No Play Integrity Fix module is installed.")
    d = q(m.path)
    if k == "pif_fork":
        strong = " --strong" if st.installed("tricky")[1] else ""
        return f"cd {d} && sh ./autopif4.sh{strong} 2>&1"
    if k == "pif_inject":
        if m.pending_install:
            raise B.BackendError("PIF [INJECT] only runs autopif from its installed folder – reboot first.")
        return f"cd {d} && sh ./autopif.sh 2>&1"
    raise B.BackendError(f"Don't know how to generate a fingerprint with {m.name}.")


KILL_PI = ("killall com.google.android.gms.unstable 2>/dev/null; killall com.android.vending 2>/dev/null; "
           "am force-stop com.android.vending 2>/dev/null; echo 'Play Integrity processes restarted.'")


# ---------------------------------------------------------------------------
# verdict check through Simple Play Integrity Checker (open source, github.com/herzhenr/spic-android)
# ---------------------------------------------------------------------------
VERDICTS = ["MEETS_BASIC_INTEGRITY", "MEETS_DEVICE_INTEGRITY", "MEETS_STRONG_INTEGRITY", "MEETS_VIRTUAL_INTEGRITY"]


def _ui_nodes(conn) -> list[dict]:
    rc, out = sh(conn, "uiautomator dump /data/local/tmp/.aunt-ui.xml >/dev/null 2>&1; "
                       "cat /data/local/tmp/.aunt-ui.xml; rm -f /data/local/tmp/.aunt-ui.xml", timeout=40)
    nodes = []
    for m in re.finditer(r"<node ([^>]*?)/?>", out):
        a = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(1)))
        nodes.append(a)
    return nodes


def _center(bounds: str):
    n = list(map(int, re.findall(r"\d+", bounds)))
    return (n[0] + n[2]) // 2, (n[1] + n[3]) // 2


def run_checker(conn, say=lambda s: None, timeout: float = 60) -> dict:
    """Launch SPIC, press the Play Integrity check button and read the verdict from the screen."""
    rc, out = sh(conn, f"pm path {CHECKER_PKG}", timeout=20)
    if "package:" not in out:
        raise B.BackendError("The checker app is not installed (use 'Install checker').")
    sh(conn, "input keyevent KEYCODE_WAKEUP", timeout=10)
    sh(conn, f"am force-stop {CHECKER_PKG}; am start -W -n {CHECKER_PKG}/.MainActivity >/dev/null 2>&1", timeout=30)
    time.sleep(2)
    nodes = _ui_nodes(conn)
    if not any(n.get("package") == CHECKER_PKG for n in nodes):
        raise B.BackendError("The checker is not on screen – unlock the phone and try again.")
    btn = None
    for pat in (r"make play integrity request", r"play integrity.*(request|check)|(request|check).*play integrity"):
        for n in nodes:
            label = (n.get("text", "") + " " + n.get("content-desc", "")).strip()
            if re.search(pat, label, re.I) and not re.search(r"checker|safety ?net", label, re.I) \
                    and n.get("bounds", "[0,0][0,0]") != "[0,0][0,0]":
                btn = n
                break
        if btn:
            break
    if btn is None:
        raise B.BackendError("Could not find the Play Integrity button in the checker.")
    x, y = _center(btn["bounds"])
    say("Requesting a Play Integrity verdict…\n")
    sh(conn, f"input tap {x} {y}", timeout=10)
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(3)
        nodes = _ui_nodes(conn)
        texts = [n.get("text", "") for n in nodes if n.get("text")]
        joined = "\n".join(texts)
        found = [v for v in VERDICTS if v in joined]
        err = next((t for t in texts if re.search(r"error|failed|exception|too many", t, re.I)), "")
        if found or "NO_INTEGRITY" in joined:
            return {"verdicts": found, "text": joined}
        if err:
            raise B.BackendError(f"Checker: {err}")
    raise B.BackendError("No verdict appeared within a minute.")


def install_checker(conn, cache_dir: str, say=lambda s: None):
    d = json.loads(http_get(CHECKER_APK))
    a = next(x for x in d["assets"] if x["name"].endswith(".apk"))
    path = os.path.join(cache_dir, a["name"])
    if not (os.path.exists(path) and os.path.getsize(path) == a["size"]):
        say(f"Downloading {a['name']} from github.com/herzhenr/spic-android…\n")
        data = http_get(a["browser_download_url"], timeout=300)
        if len(data) != a["size"]:
            raise B.BackendError("download size mismatch")
        with open(path, "wb") as f:
            f.write(data)
    dev = "/data/local/tmp/aunt-spic.apk"
    push(conn, path, dev)
    rc, out = sh(conn, f"pm install -r {dev}; rc=$?; rm -f {dev}; exit $rc", timeout=180)
    if rc != 0:
        raise B.BackendError(out.strip())
    say(out.strip() + "\n")


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------
def install(conn, st: State, keys: list[str], remove_conflicts: bool, cache_dir: str, say=lambda s: None) -> bool:
    """Download, verify and install the chosen components. Returns True when a reboot is needed."""
    comps = [COMPONENTS[k] for k in keys]
    if st.sdk and st.sdk < 29 and any(c.kind == "tricky" for c in comps):
        raise B.BackendError(f"Tricky Store needs Android 10 or newer (this is SDK {st.sdk}).")
    files = []
    for c in comps:
        say(f"\n== {c.name}\n")
        rel = latest_release(c, cache_dir)
        say(f"  latest release {rel.tag} ({rel.published})\n")
        files.append((c, rel, download(rel, cache_dir, say)))

    m = st.mgr
    if m.magisk_family and any(c.kind in ("pif", "hide") for c in comps) and st.props.get("_zygisk_setting") != 1:
        say("\nEnabling Magisk's built-in Zygisk (active after reboot)\n")
        R.set_magisk_setting(conn, m, "zygisk", 1)

    if remove_conflicts:
        for c in comps:
            for mod in st.modules:
                if mod.removing or mod.id not in CONFLICTS[c.kind]:
                    continue
                if mod.id == c.module_id:
                    continue                      # same id: the new install replaces it
                say(f"\nRemoving conflicting module {mod.name} ({mod.id}) at next reboot\n")
                R.uninstall_module(conn, m, mod)

    for i, (c, rel, path) in enumerate(files):
        dev = f"/data/local/tmp/aunt-module-{i}.zip"
        say(f"\n== Installing {c.name} {rel.tag} with {m.name}\n")
        push(conn, path, dev)
        sh(conn, f"chmod 644 {dev}", timeout=20)
        rc, out = sh(conn, f"{R.install_command(m, dev)}; rc=$?; rm -f {dev}; exit $rc", timeout=300)
        say(out.rstrip() + "\n")
        if rc != 0:
            raise B.BackendError(f"{c.name}: install failed (exit {rc})")

    msg = fix_denylist(conn, st)
    if msg:
        say("\n" + msg + "\n")
    return True


def start_daemon(conn, st: State) -> str:
    """Start the keystore-spoof module's service.sh the way the root manager does at boot (the daemon
    sometimes does not come up on the first boot after an install)."""
    k, mod = st.installed("tricky")
    if not mod or mod.pending_install:
        raise B.BackendError("The keystore spoof module is not active yet – reboot first.")
    bb = st.mgr.busybox
    d = q(mod.path)
    rc, out = sh(conn, f"cd {d} && if [ -x {q(bb)} ]; then ASH_STANDALONE=1 nohup {q(bb)} sh -o standalone ./service.sh; "
                       f"else nohup sh ./service.sh; fi >/dev/null 2>&1 & sleep 5; "
                       "ps -A -o NAME 2>/dev/null | grep -iE 'tricky|teesim' | head -3", timeout=40)
    if not out.strip():
        raise B.BackendError("The daemon did not start. Check logcat for TrickyStore / TEESimulator errors.")
    return out.strip()


def aosp_keybox(cache_dir: str, say=lambda s: None) -> bytes:
    """The AOSP software keybox that Tricky Store OSS ships (from its verified release zip)."""
    import zipfile
    rel = latest_release(COMPONENTS["tsoss"], cache_dir)
    path = download(rel, cache_dir, say)
    with zipfile.ZipFile(path) as z:
        data = z.read("keybox.xml")
    info = parse_keybox(data)
    if info.kind != "software":
        raise B.BackendError("The keybox in the Tricky Store OSS release is not the AOSP software one.")
    return data
