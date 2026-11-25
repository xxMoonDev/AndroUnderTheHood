"""Wake-lock & battery-drain analyser, from Android's own accounting:

  dumpsys batterystats --charged --checkin   (since the last full charge, machine-readable)
    bt   time on battery / screen off          pwi  estimated power use per uid (mAh)
    m    screen-on time, total partial wake-lock time
    awl  wake-lock time per uid (actual)       wl   each app wake lock: time + count
    kwl  kernel wake locks                     uid  uid -> package names
  dumpsys power                               wake locks held right now
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

COLLECT = ("echo @@CHECKIN; dumpsys batterystats --charged --checkin 2>/dev/null; "
           "echo @@POWER; dumpsys power 2>/dev/null | sed -n '/Wake Locks: size/,/^$/p'; "
           "echo @@PKG; pm list packages -U 2>/dev/null; "
           "echo @@BAT; dumpsys battery 2>/dev/null")

CORE = {0: "root / kernel", 1000: "Android system", 1001: "Phone (radio)", 1002: "Bluetooth", 1010: "Wi-Fi",
        1013: "Media", 1016: "VPN", 1021: "GPS", 1027: "NFC", 1036: "logd", 1041: "Audio server", 1047: "Camera",
        1066: "statsd", 1068: "Secure element", 1073: "Network stack", 2000: "Shell (adb)"}


@dataclass
class AppUse:
    uid: int
    name: str = ""
    pkgs: list = field(default_factory=list)
    mah: float = 0.0
    wake_ms: int = 0
    wakelocks: list = field(default_factory=list)      # (tag, ms, count)


def ms(n) -> str:
    n = int(n)
    s = n // 1000
    if s < 60:
        return f"{n / 1000:.1f} s"
    h, m = divmod(s // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min {s % 60} s"


def collect(conn):
    rc, out, err = conn.exec(COLLECT, timeout=120, as_root=True)
    return parse(out.decode("utf-8", "replace"))


def parse(text: str) -> dict:
    parts = dict(re.findall(r"@@(\w+)\n(.*?)(?=\n@@\w+\n|\Z)", text, re.S))
    rows = list(csv.reader(io.StringIO(parts.get("CHECKIN", ""))))
    names: dict[int, list[str]] = {}
    for ln in parts.get("PKG", "").splitlines():
        m = re.search(r"package:(\S+)\s+uid:(\d+)", ln)
        if m:
            names.setdefault(int(m.group(2)), []).append(m.group(1))
    res = {"apps": {}, "kernel": [], "held": [], "totals": {}}
    apps: dict[int, AppUse] = res["apps"]

    def app(uid):
        a = apps.get(uid)
        if not a:
            a = apps[uid] = AppUse(uid)
        return a

    t = res["totals"]
    for r in rows:
        if len(r) < 4:
            continue
        try:
            uid = int(r[1])
        except ValueError:
            continue
        kind, typ = r[2], r[3]
        f = r[4:]
        try:
            if kind == "i" and typ == "uid" and len(f) >= 2:
                names.setdefault(int(f[0]), [])
                if f[1] not in names[int(f[0])]:
                    names[int(f[0])].append(f[1])
            elif typ == "bt" and len(f) >= 7:
                t["on_battery_ms"] = int(f[1])
                t["since_ms"] = int(f[3])
                t["screen_off_ms"] = int(f[6])
                if len(f) > 9 and f[9].isdigit():
                    t["capacity_mah"] = int(f[9])
            elif typ == "m" and len(f) >= 4:
                t["screen_on_ms"] = int(f[0])
                t["partial_wake_ms"] = int(f[3])
            elif typ == "pws" and f and f[0].isdigit():
                t.setdefault("capacity_mah", int(f[0]))
            elif typ == "pwi" and len(f) >= 2 and f[0] == "uid":
                app(uid).mah += float(f[1])
            elif typ == "awl" and f:
                app(uid).wake_ms = int(f[0])
            elif typ == "wl" and "p" in f:
                i = f.index("p")
                part_ms, count = int(f[i - 1]), int(f[i + 1])
                if part_ms or count:
                    app(uid).wakelocks.append((f[0], part_ms, count))
            elif typ == "kwl" and len(f) >= 3:
                k_ms, cnt = int(f[1]), int(f[2])
                if k_ms > 0:
                    res["kernel"].append((f[0].strip('"') or "(unnamed)", k_ms, cnt))
        except (ValueError, IndexError):
            continue
    for a in apps.values():
        a.pkgs = names.get(a.uid, [])
        a.name = CORE.get(a.uid) or (a.pkgs[0] if a.pkgs else f"uid {a.uid}")
        a.wakelocks.sort(key=lambda w: -w[1])
    res["kernel"].sort(key=lambda k: -k[1])
    for ln in parts.get("POWER", "").splitlines():
        m = re.match(r"\s+(\w+)\s+'([^']*)'.*?ACQ=(\S+).*?uid=(\d+)", ln)
        if m:
            uid = int(m.group(4))
            res["held"].append({"type": m.group(1), "tag": m.group(2), "for": m.group(3).lstrip("-"),
                                "uid": uid, "long": " LONG" in ln,
                                "who": CORE.get(uid) or (names.get(uid) or [f"uid {uid}"])[0]})
    if t.get("capacity_mah", 0) > 100_000:          # some versions report µAh here
        t["capacity_mah"] //= 1000
    bat = parts.get("BAT", "")
    lv = re.search(r"level: (\d+)", bat)
    t["level"] = int(lv.group(1)) if lv else None
    t["plugged"] = any(re.search(rf"{x} powered: true", bat) for x in ("AC", "USB", "Wireless", "Dock"))
    return res


def insights(res: dict) -> list[str]:
    t = res["totals"]
    out = []
    onb = t.get("on_battery_ms", 0)
    if t.get("plugged"):
        out.append("The device is on the charger now; the numbers cover the time on battery since the last full charge.")
    if onb < 10 * 60 * 1000:
        out.append(f"Only {ms(onb)} on battery since the last full charge – too little to judge drain. Unplug it "
                   "for a few hours (screen off) and look again.")
    off, pw = t.get("screen_off_ms", 0), t.get("partial_wake_ms", 0)
    if off > 60_000:
        pct = 100 * min(pw, off) / off
        level = "high – something keeps the phone from sleeping" if pct > 30 else \
            "moderate" if pct > 10 else "low – the phone sleeps well"
        out.append(f"With the screen off, wake locks kept the CPU awake {pct:.0f} % of the time ({ms(pw)} of "
                   f"{ms(off)}): {level}.")
    longest = sorted(res["held"], key=lambda h: (not h["long"],))
    for h in longest:
        if h["long"] and h["type"].startswith("PARTIAL"):
            out.append(f"Held right now for {h['for']}: '{h['tag']}' by {h['who']} – a long partial wake lock keeps "
                       "the CPU from sleeping the whole time.")
    apps = sorted(res["apps"].values(), key=lambda a: -a.mah)
    if apps and apps[0].mah > 0:
        top = ", ".join(f"{a.name} {a.mah:.1f} mAh" for a in apps[:3] if a.mah > 0)
        out.append(f"Biggest estimated users: {top}.")
    return out


def reset(conn) -> str:
    rc, out, err = conn.exec("dumpsys batterystats --reset 2>&1", timeout=60, as_root=True)
    return (out + err).decode("utf-8", "replace").strip()
