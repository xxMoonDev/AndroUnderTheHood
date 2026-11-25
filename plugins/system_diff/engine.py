"""System snapshots & diff: getprop, settings (system / secure / global) and installed packages + versions.

Snapshots are JSON files in Documents\\AndroUnderTheHood\\System snapshots\\<device>. A diff compares two
snapshots (or a snapshot and the device right now) and lists added / removed / changed keys.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

CAPTURE = ("echo @@prop; getprop; "
           "for ns in system secure global; do echo @@settings.$ns; settings list $ns 2>/dev/null; done; "
           "echo @@packages; pm list packages --show-versioncode 2>/dev/null; "
           "echo @@info; echo kernel=$(uname -r); echo selinux=$(getenforce 2>/dev/null); "
           "echo magisk=$(magisk -v 2>/dev/null); echo modules=$(ls /data/adb/modules 2>/dev/null | tr '\\n' ' ')")

SECTIONS = ["prop", "settings.system", "settings.secure", "settings.global", "packages", "info"]
TITLES = {"prop": "System property", "settings.system": "Settings: system", "settings.secure": "Settings: secure",
          "settings.global": "Settings: global", "packages": "Package version", "info": "Device"}

# values that change on their own (boot times, counters, service pids, clocks) - hidden by "hide noise"
NOISE = re.compile(r"^(ro\.boottime\.|init\.svc_debug_pid\.|sys\.boot\.|sys\.rescue|dev\.bootcomplete|"
                   r"persist\.sys\.boot\.reason|ro\.runtime\.firstboot|sys\.sysctl\.|vendor\.debug\.|"
                   r"debug\.tracing\.|sys\.usb\.|persist\.vendor\.radio\.|gsm\.|vendor\.ril\.|ril\.|"
                   r"sys\.oem_unlock|bootreceiver\.|next_alarm_formatted|boot_count|"
                   r"wifi_scan_throttle|bluetooth_address|.*_timestamp.*|.*last_.*time.*|"
                   r"zram\.|sys\.lmk\.|persist\.sys\.dalvik\.vm\.lib|net\.dns|net\.change)", re.I)


def parse(text: str) -> dict:
    snap = {s: {} for s in SECTIONS}
    cur = None
    for ln in text.splitlines():
        if ln.startswith("@@"):
            cur = ln[2:].strip()
            continue
        if cur == "prop":
            m = re.match(r"\[([^\]]*)\]: \[(.*)\]$", ln)
            if m:
                snap["prop"][m.group(1)] = m.group(2)
        elif cur and cur.startswith("settings."):
            k, sep, v = ln.partition("=")
            if sep:
                snap[cur][k] = v
        elif cur == "packages":
            m = re.match(r"package:(\S+) versionCode:(\d+)", ln)
            if m:
                snap["packages"][m.group(1)] = m.group(2)
        elif cur == "info":
            k, sep, v = ln.partition("=")
            if sep:
                snap["info"][k] = v.strip()
    return snap


def capture(conn) -> dict:
    rc, out, err = conn.exec(CAPTURE, timeout=120, as_root=True)
    return parse(out.decode("utf-8", "replace"))


def folder(device_name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', "_", device_name or "device")
    d = os.path.join(os.path.expanduser("~"), "Documents", "AndroUnderTheHood", "System snapshots", safe)
    os.makedirs(d, exist_ok=True)
    return d


def save(snap: dict, device_name: str, label: str) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^\w\- ]+", "", label).strip().replace(" ", "_")[:40]
    path = os.path.join(folder(device_name), f"{stamp}{'-' + safe if safe else ''}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"label": label, "created": stamp, "device": device_name, "data": snap}, f, indent=0)
    return path


def load_all(device_name: str) -> list[dict]:
    out = []
    d = folder(device_name)
    for n in sorted(os.listdir(d), reverse=True):
        if n.endswith(".json"):
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    s = json.load(f)
                s["path"] = os.path.join(d, n)
                out.append(s)
            except (OSError, ValueError):
                pass
    return out


def diff(a: dict, b: dict, hide_noise: bool = True) -> list[tuple[str, str, str, str, str]]:
    """[(section, key, old, new, kind)] kind: added | removed | changed"""
    rows = []
    for sec in SECTIONS:
        x, y = a.get(sec, {}), b.get(sec, {})
        for k in sorted(set(x) | set(y)):
            if hide_noise and NOISE.match(k):
                continue
            if k not in x:
                rows.append((sec, k, "", y[k], "added"))
            elif k not in y:
                rows.append((sec, k, x[k], "", "removed"))
            elif x[k] != y[k]:
                rows.append((sec, k, x[k], y[k], "changed"))
    return rows


def summary(rows) -> str:
    if not rows:
        return "No differences."
    parts = []
    for sec in SECTIONS:
        r = [x for x in rows if x[0] == sec]
        if r:
            c = {k: sum(1 for x in r if x[4] == k) for k in ("added", "removed", "changed")}
            parts.append(f"{TITLES[sec]}: " + ", ".join(f"{n} {k}" for k, n in c.items() if n))
    return " · ".join(parts)
