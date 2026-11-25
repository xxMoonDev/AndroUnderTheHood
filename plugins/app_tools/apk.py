"""APK inspector - reads an .apk (or several split APKs) on the PC, no Android SDK needed.

- AndroidManifest.xml (binary AXML): package, versions, SDKs, permissions, security flags, components
  and which of them are exported
- signing certificates: v1 (META-INF/*.RSA|DSA|EC) and v2/v3 (APK Signing Block)
- native ABIs (lib/<abi>/), dex files, known tracker / ad SDKs (type descriptors in classes*.dex)
"""
from __future__ import annotations

import hashlib
import re
import struct
import zipfile

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ATTR_IDS = {0x01010003: "name", 0x01010010: "exported", 0x01010006: "permission", 0x0101000f: "debuggable",
            0x01010280: "allowBackup", 0x010104ec: "usesCleartextTraffic", 0x0101020c: "minSdkVersion",
            0x01010270: "targetSdkVersion", 0x0101021b: "versionCode", 0x0101021c: "versionName",
            0x01010009: "protectionLevel", 0x01010527: "networkSecurityConfig", 0x01010018: "authorities",
            0x01010001: "label", 0x0101001b: "enabled", 0x01010000: "theme", 0x01010019: "grantUriPermissions",
            0x01010020: "readPermission", 0x0101001f: "writePermission", 0x01010571: "requestLegacyExternalStorage",
            0x01010000 + 0x572: "extractNativeLibs"}
DANGEROUS = {"READ_CONTACTS", "WRITE_CONTACTS", "GET_ACCOUNTS", "READ_CALENDAR", "WRITE_CALENDAR", "CAMERA",
             "RECORD_AUDIO", "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION", "ACCESS_BACKGROUND_LOCATION",
             "READ_PHONE_STATE", "READ_PHONE_NUMBERS", "CALL_PHONE", "READ_CALL_LOG", "WRITE_CALL_LOG",
             "PROCESS_OUTGOING_CALLS", "BODY_SENSORS", "SEND_SMS", "RECEIVE_SMS", "READ_SMS", "RECEIVE_MMS",
             "READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE", "READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO",
             "READ_MEDIA_AUDIO", "ACTIVITY_RECOGNITION", "POST_NOTIFICATIONS", "NEARBY_WIFI_DEVICES",
             "BLUETOOTH_SCAN", "BLUETOOTH_CONNECT", "UWB_RANGING"}
SPECIAL = {"SYSTEM_ALERT_WINDOW", "WRITE_SETTINGS", "MANAGE_EXTERNAL_STORAGE", "REQUEST_INSTALL_PACKAGES",
           "BIND_ACCESSIBILITY_SERVICE", "BIND_DEVICE_ADMIN", "BIND_NOTIFICATION_LISTENER_SERVICE",
           "PACKAGE_USAGE_STATS", "QUERY_ALL_PACKAGES", "SCHEDULE_EXACT_ALARM", "BIND_VPN_SERVICE",
           "READ_LOGS", "INSTALL_PACKAGES", "CHANGE_WIFI_STATE", "USE_FULL_SCREEN_INTENT"}
TRACKERS = {
    "Google Firebase Analytics": "Lcom/google/firebase/analytics", "Google AdMob / Ads": "Lcom/google/android/gms/ads",
    "Firebase Crashlytics": "Lcom/google/firebase/crashlytics", "Google Analytics": "Lcom/google/android/gms/analytics",
    "Facebook SDK": "Lcom/facebook/appevents", "Facebook Audience Network": "Lcom/facebook/ads",
    "AppsFlyer": "Lcom/appsflyer", "Adjust": "Lcom/adjust/sdk", "Branch": "Lio/branch/referral",
    "Mixpanel": "Lcom/mixpanel", "Amplitude": "Lcom/amplitude", "Segment": "Lcom/segment/analytics",
    "OneSignal": "Lcom/onesignal", "AppLovin": "Lcom/applovin", "Unity Ads": "Lcom/unity3d/ads",
    "ironSource": "Lcom/ironsource", "Flurry": "Lcom/flurry", "Sentry": "Lio/sentry", "Bugsnag": "Lcom/bugsnag",
    "Microsoft App Center": "Lcom/microsoft/appcenter", "Yandex AppMetrica": "Lcom/yandex/metrica",
    "Huawei HMS Analytics": "Lcom/huawei/hms/analytics", "Braze": "Lcom/braze", "CleverTap": "Lcom/clevertap",
    "Chartboost": "Lcom/chartboost", "Vungle / Liftoff": "Lcom/vungle", "InMobi": "Lcom/inmobi",
    "Pangle (ByteDance)": "Lcom/bytedance/sdk/openadsdk", "Mintegral": "Lcom/mbridge", "Tapjoy": "Lcom/tapjoy",
    "New Relic": "Lcom/newrelic", "Datadog": "Lcom/datadog", "Instabug": "Lcom/instabug",
}
COMPONENTS = ("activity", "activity-alias", "service", "receiver", "provider")


# ---------------------------------------------------------------- binary XML
def _strings(data: bytes, off: int) -> list[str]:
    _t, hsz, size, count, _styles, flags, s_start, _st = struct.unpack_from("<HHIIIIII", data, off)
    utf8 = bool(flags & 0x100)
    offs = struct.unpack_from(f"<{count}I", data, off + hsz)
    base = off + s_start
    out = []
    for o in offs:
        p = base + o
        try:
            if utf8:
                n = data[p]
                p += 2 if n & 0x80 else 1
                blen = data[p]
                if blen & 0x80:
                    blen = ((blen & 0x7F) << 8) | data[p + 1]
                    p += 2
                else:
                    p += 1
                out.append(data[p:p + blen].decode("utf-8", "replace"))
            else:
                n = struct.unpack_from("<H", data, p)[0]
                p += 2
                if n & 0x8000:
                    n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, p)[0]
                    p += 2
                out.append(data[p:p + n * 2].decode("utf-16-le", "replace"))
        except (IndexError, struct.error):
            out.append("")
    return out


def parse_axml(data: bytes) -> list[dict]:
    """-> list of elements {tag, attrs{name: value}, depth} in document order."""
    t, hsz, size = struct.unpack_from("<HHI", data, 0)
    if t != 0x0003:
        raise ValueError("not a binary XML file")
    strings, resmap, elements = [], [], []
    depth = 0
    off = hsz
    while off + 8 <= len(data):
        ctype, chsz, csize = struct.unpack_from("<HHI", data, off)
        if csize < 8:
            break
        if ctype == 0x0001:
            strings = _strings(data, off)
        elif ctype == 0x0180:
            resmap = list(struct.unpack_from(f"<{(csize - chsz) // 4}I", data, off + chsz))
        elif ctype == 0x0102:
            ns, name, a_start, a_size, a_count = struct.unpack_from("<IIHHH", data, off + chsz)
            attrs = {}
            for i in range(a_count):
                ao = off + chsz + a_start + i * a_size
                a_ns, a_name, a_raw, _vs, _r, dtype, dval = struct.unpack_from("<IIIHBBI", data, ao)
                key = strings[a_name] if a_name < len(strings) else ""
                if (not key or not key.isidentifier()) and a_name < len(resmap):
                    key = ATTR_IDS.get(resmap[a_name], f"0x{resmap[a_name]:08x}")
                if dtype == 0x03:
                    val = strings[dval] if dval < len(strings) else ""
                elif dtype == 0x12:
                    val = dval != 0
                elif dtype in (0x10, 0x11):
                    val = dval if dval < 0x80000000 else dval - (1 << 32)
                elif dtype == 0x01:
                    val = f"@0x{dval:08x}"
                elif a_raw != 0xFFFFFFFF and a_raw < len(strings):
                    val = strings[a_raw]
                else:
                    val = dval
                attrs[key] = val
            elements.append({"tag": strings[name] if name < len(strings) else "?", "attrs": attrs, "depth": depth})
            depth += 1
        elif ctype == 0x0103:
            depth -= 1
        off += csize
    return elements


# ---------------------------------------------------------------- signing
def _lp(buf: bytes, off: int) -> tuple[bytes, int]:
    n = struct.unpack_from("<I", buf, off)[0]
    return buf[off + 4:off + 4 + n], off + 4 + n


def _seq(buf: bytes) -> list[bytes]:
    out, off = [], 0
    while off + 4 <= len(buf):
        item, off = _lp(buf, off)
        out.append(item)
    return out


def signing_block(path: str) -> dict[int, bytes]:
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 65557))
        tail = f.read()
        eocd = tail.rfind(b"PK\x05\x06")
        if eocd < 0:
            return {}
        cd_off = struct.unpack_from("<I", tail, eocd + 16)[0]
        if cd_off < 32:
            return {}
        f.seek(cd_off - 24)
        footer = f.read(24)
        if footer[8:] != b"APK Sig Block 42":
            return {}
        bsize = struct.unpack_from("<Q", footer, 0)[0]
        f.seek(cd_off - bsize - 8)
        block = f.read(bsize - 16)
    pairs, off = {}, 8
    while off + 12 <= len(block):
        ln = struct.unpack_from("<Q", block, off)[0]
        pid = struct.unpack_from("<I", block, off + 8)[0]
        pairs[pid] = block[off + 12:off + 8 + ln]
        off += 8 + ln
    return pairs


def _cert_info(der: bytes) -> dict:
    info = {"sha256": hashlib.sha256(der).hexdigest(), "sha1": hashlib.sha1(der).hexdigest()}
    try:
        from cryptography import x509
        c = x509.load_der_x509_certificate(der)
        info.update(subject=c.subject.rfc4514_string(), issuer=c.issuer.rfc4514_string(),
                    valid_from=str(c.not_valid_before_utc.date()), valid_to=str(c.not_valid_after_utc.date()),
                    algorithm=c.signature_hash_algorithm.name if c.signature_hash_algorithm else "?")
    except Exception as e:  # noqa: BLE001
        info["error"] = str(e)
    return info


def certificates(path: str, z: zipfile.ZipFile) -> dict:
    res = {"schemes": [], "certs": []}
    seen = set()
    blocks = signing_block(path)
    for scheme, pid in (("v2", 0x7109871A), ("v3", 0xF05368C0), ("v3.1", 0x1B93AD61)):
        if pid not in blocks:
            continue
        res["schemes"].append(scheme)
        try:
            signers, _ = _lp(blocks[pid], 0)
            for signer in _seq(signers):
                signed, _ = _lp(signer, 0)
                _digests, o = _lp(signed, 0)
                certs, _ = _lp(signed, o)
                for der in _seq(certs):
                    h = hashlib.sha256(der).hexdigest()
                    if h not in seen:
                        seen.add(h)
                        res["certs"].append(_cert_info(der))
        except (struct.error, IndexError):
            pass
    v1 = [n for n in z.namelist() if re.fullmatch(r"META-INF/[^/]+\.(RSA|DSA|EC)", n, re.I)]
    if v1:
        res["schemes"].insert(0, "v1 (JAR)")
        try:
            from cryptography.hazmat.primitives.serialization import pkcs7
            from cryptography.hazmat.primitives.serialization import Encoding
            for c in pkcs7.load_der_pkcs7_certificates(z.read(v1[0])):
                der = c.public_bytes(Encoding.DER)
                h = hashlib.sha256(der).hexdigest()
                if h not in seen:
                    seen.add(h)
                    res["certs"].append(_cert_info(der))
        except Exception:  # noqa: BLE001
            pass
    for c in res["certs"]:
        s = c.get("subject", "")
        c["debug"] = "CN=Android Debug" in s
    return res


# ---------------------------------------------------------------- inspect
def inspect(paths: list[str]) -> dict:
    """paths[0] = base.apk, the rest = splits (optional)."""
    base = paths[0]
    with zipfile.ZipFile(base) as z:
        man = parse_axml(z.read("AndroidManifest.xml"))
        sig = certificates(base, z)
        names = z.namelist()
        dex = [n for n in names if re.fullmatch(r"classes\d*\.dex", n)]
        found = {}
        for n in dex:
            data = z.read(n)
            for label, pat in TRACKERS.items():
                if label not in found and pat.encode() in data:
                    found[label] = n
    abis = set()
    for p in paths:
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                m = re.match(r"lib/([^/]+)/[^/]+\.so$", n)
                if m:
                    abis.add(m.group(1))
    r = {"files": paths, "signing": sig, "abis": sorted(abis), "dex": dex, "trackers": sorted(found),
         "permissions": [], "components": [], "app": {}, "manifest": {}, "uses_sdk": {}, "defined_permissions": []}
    for e in man:
        a, tag = e["attrs"], e["tag"]
        if tag == "manifest":
            r["manifest"] = a
        elif tag == "uses-sdk":
            r["uses_sdk"] = a
        elif tag in ("uses-permission", "uses-permission-sdk-23"):
            r["permissions"].append(a.get("name", "?"))
        elif tag == "permission":
            r["defined_permissions"].append((a.get("name", "?"), a.get("protectionLevel")))
        elif tag == "application":
            r["app"] = a
    target = int(r["uses_sdk"].get("targetSdkVersion", 0) or 0)
    # components with their intent filters
    cur = None
    for e in man:
        if e["tag"] in COMPONENTS:
            a = e["attrs"]
            cur = {"type": e["tag"], "name": a.get("name", "?"), "exported_attr": a.get("exported"),
                   "permission": a.get("permission") or a.get("readPermission"), "filters": [], "depth": e["depth"],
                   "enabled": a.get("enabled", True), "authorities": a.get("authorities")}
            r["components"].append(cur)
        elif cur and e["tag"] == "action" and e["depth"] > cur["depth"]:
            cur["filters"].append(e["attrs"].get("name", "?"))
        elif e["tag"] not in ("intent-filter", "action", "category", "data", "meta-data", "path-permission",
                              "grant-uri-permission"):
            cur = None if e["tag"] in COMPONENTS or e["depth"] <= (cur or {}).get("depth", -1) else cur
    for c in r["components"]:
        if c["exported_attr"] is not None:
            c["exported"] = bool(c["exported_attr"])
        else:   # implicit: exported if it has intent filters (before Android 12 this was the default)
            c["exported"] = bool(c["filters"]) and (c["type"] != "provider" or target < 17)
        c["open"] = c["exported"] and not c["permission"]
    return r


def risk_notes(r: dict) -> list[tuple[str, str]]:
    """[(level, text)] level: bad | warn | info"""
    notes = []
    app, sdk = r["app"], r["uses_sdk"]
    if app.get("debuggable") is True:
        notes.append(("bad", "android:debuggable=true – anyone with ADB can attach a debugger and read its data"))
    if app.get("allowBackup", True) is not False:
        notes.append(("warn", "allowBackup is on – its data can be copied with adb backup / device transfer"))
    if app.get("usesCleartextTraffic") is True:
        notes.append(("warn", "usesCleartextTraffic=true – it may send data over unencrypted HTTP"))
    t = int(sdk.get("targetSdkVersion", 0) or 0)
    if t and t < 30:
        notes.append(("warn", f"targets an old Android (API {t}) – runs with older, looser rules"))
    open_comps = [c for c in r["components"] if c["open"] and c["type"] != "activity"]
    if open_comps:
        notes.append(("warn", f"{len(open_comps)} exported service/receiver/provider without a permission – other "
                              "apps can call them"))
    if any(c.get("debug") for c in r["signing"]["certs"]):
        notes.append(("warn", "signed with an Android Debug certificate"))
    if not r["signing"]["certs"]:
        notes.append(("bad", "no signing certificate found"))
    if r["trackers"]:
        notes.append(("info", f"{len(r['trackers'])} tracker / analytics SDK(s): {', '.join(r['trackers'])}"))
    return notes


def perm_kind(p: str) -> str:
    short = p.rsplit(".", 1)[-1]
    if p.startswith("android.permission.") and short in DANGEROUS:
        return "dangerous"
    if p.startswith("android.permission.") and short in SPECIAL:
        return "special"
    return "normal" if p.startswith("android.") else "custom"
