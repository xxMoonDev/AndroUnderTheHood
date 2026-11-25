"""Sampling and maths for the Task Manager plugin (no Qt widgets here).

One long-running shell loop on the device prints a snapshot every few seconds (/proc/stat, meminfo, diskstats,
net/dev, CPU frequencies, GPU load, temperatures, battery, every process's /proc/<pid>/stat, ps names, per-process
I/O when root, TCP/UDP sockets). The PC parses it and turns two consecutive snapshots into rates.
"""
from __future__ import annotations

import collections
import ipaddress
import re
import struct
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, Signal

_NOWIN = 0x08000000 if sys.platform == "win32" else 0
HIST = 120                      # points kept for every graph

STATIC_SCRIPT = r"""
echo "@@SOC"; getprop ro.soc.manufacturer; getprop ro.soc.model; getprop ro.board.platform; getprop ro.product.model
echo "@@CPUINFO"; cat /proc/cpuinfo 2>/dev/null
echo "@@MAXF"; grep -H '' /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_min_freq 2>/dev/null
echo "@@CONF"; getconf PAGESIZE 2>/dev/null || echo 4096; getconf CLK_TCK 2>/dev/null || echo 100
echo "@@UNAME"; uname -r; cat /proc/uptime; grep btime /proc/stat
echo "@@GPU"
[ -r /sys/class/kgsl/kgsl-3d0/gpu_model ] && { echo "$(cat /sys/class/kgsl/kgsl-3d0/gpu_model)"; echo "max_mhz $(cat /sys/class/kgsl/kgsl-3d0/max_clock_mhz 2>/dev/null)"; }
ls -d /sys/class/kgsl/kgsl-3d0 /sys/kernel/ged/hal /sys/class/misc/mali0 /sys/devices/platform/*mali* 2>/dev/null
echo "@@BLOCK"
for b in /sys/block/sd[a-z] /sys/block/mmcblk[0-9] /sys/block/nvme[0-9]n[0-9]; do [ -e "$b" ] || continue
  echo "${b##*/}|$(cat $b/size 2>/dev/null)|$(cat $b/device/vendor 2>/dev/null)|$(cat $b/device/model 2>/dev/null)|$(cat $b/device/name 2>/dev/null)|$(cat $b/device/type 2>/dev/null)"; done
echo "@@DF"; for p in / /system /vendor /product /data /storage/emulated/0 /cache /metadata; do echo "$p|$(df -k $p 2>/dev/null | tail -1)"; done
echo "@@THERM"; for t in /sys/class/thermal/thermal_zone*; do echo "${t##*zone}|$(cat $t/type 2>/dev/null)"; done
echo "@@PKG"; pm list packages -U 2>/dev/null
echo "@@ZRAM"; cat /sys/block/zram0/disksize 2>/dev/null; cat /sys/block/zram0/comp_algorithm 2>/dev/null
echo "@@END"
"""

SAMPLE_SCRIPT = r"""
T=@TOKEN@; I=@INTERVAL@; R=@ROOT@
if [ -r /sys/class/kgsl/kgsl-3d0/gpu_busy_percentage ]; then G="/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage /sys/class/kgsl/kgsl-3d0/clock_mhz"; GK=kgsl
elif [ -r /sys/kernel/ged/hal/gpu_utilization ]; then G="/sys/kernel/ged/hal/gpu_utilization /sys/kernel/ged/hal/current_freqency"; GK=ged
elif [ -r /sys/class/misc/mali0/device/utilization ]; then G="/sys/class/misc/mali0/device/utilization"; GK=mali
else G=""; GK=none; fi
while :; do
echo "@@S $(date +%s%N) $GK"
echo "@@STAT"; cat /proc/stat
echo "@@MEM"; cat /proc/meminfo
echo "@@DISK"; cat /proc/diskstats 2>/dev/null
echo "@@NET"; cat /proc/net/dev
echo "@@FREQ"; grep -H '' /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq 2>/dev/null
echo "@@ONLINE"; cat /sys/devices/system/cpu/online 2>/dev/null
echo "@@GPU"; for f in $G; do cat $f 2>/dev/null; done
echo "@@THERM"; grep -H '' /sys/class/thermal/thermal_zone*/temp 2>/dev/null
echo "@@BAT"; grep -H '' /sys/class/power_supply/battery/current_now /sys/class/power_supply/battery/voltage_now /sys/class/power_supply/battery/capacity /sys/class/power_supply/battery/status 2>/dev/null
echo "@@PSTAT"; cat /proc/[0-9]*/stat 2>/dev/null
echo "@@PS"; ps -A -o PID,UID,NAME 2>/dev/null
if [ "$R" = 1 ]; then echo "@@IO"; grep -H -E '^(read_bytes|write_bytes)' /proc/[0-9]*/io 2>/dev/null; fi
echo "@@TCP"; cat /proc/net/tcp /proc/net/tcp6 2>/dev/null
echo "@@UDP"; cat /proc/net/udp /proc/net/udp6 2>/dev/null
echo "@@END"
sleep $I || exit
done
"""

SYS_UIDS = {0: "root", 1000: "system", 1001: "radio", 1002: "bluetooth", 1003: "graphics", 1004: "input",
            1005: "audio", 1006: "camera", 1007: "log", 1010: "wifi", 1013: "media", 1016: "vpn", 1017: "keystore",
            1019: "drm", 1020: "mdnsr", 1021: "gps", 1023: "media_rw", 1024: "mtp", 1027: "nfc", 1036: "logd",
            1037: "shared_relro", 1040: "mediaex", 1041: "audioserver", 1046: "mediacodec", 1047: "cameraserver",
            1048: "firewall", 1049: "trunks", 1051: "nvram", 1052: "dns", 1053: "dns_tether", 1054: "webview_zygote",
            1055: "vehicle_network", 1056: "media_audio", 1058: "media_video", 1062: "secure_element",
            1066: "statsd", 1067: "incidentd", 1068: "secure_element", 1069: "lmkd", 1072: "llkd", 1073: "iorapd",
            1074: "gpu_service", 1075: "network_stack", 1076: "gsid", 1077: "fsverity_cert", 1078: "credstore",
            1081: "artd", 1082: "uwb", 1083: "thread_network", 2000: "shell", 9999: "nobody"}
TCP_STATES = {1: "ESTABLISHED", 2: "SYN_SENT", 3: "SYN_RECV", 4: "FIN_WAIT1", 5: "FIN_WAIT2", 6: "TIME_WAIT",
              7: "CLOSE", 8: "CLOSE_WAIT", 9: "LAST_ACK", 10: "LISTEN", 11: "CLOSING"}
STATE_NAMES = {"R": "Running", "S": "Sleeping", "D": "Waiting (disk)", "T": "Suspended", "t": "Traced",
               "Z": "Zombie", "X": "Dead", "I": "Idle", "W": "Paging", "K": "Wakekill", "P": "Parked"}


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def user_name(uid: int, packages: dict[int, list[str]]) -> str:
    if uid in SYS_UIDS:
        return SYS_UIDS[uid]
    if uid >= 10000:
        user, app = divmod(uid, 100000)
        if app >= 99000:
            return f"u{user}_i{app - 99000}"           # isolated process
        if app >= 10000:
            return f"u{user}_a{app - 10000}"
    return str(uid)


def human_bytes(n: float, rate: bool = False) -> str:
    unit = "/s" if rate else ""
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return (f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}") + unit
        n /= 1024
    return f"{n:.1f} TB{unit}"


def sections(text: str) -> dict[str, list[str]]:
    sec: dict[str, list[str]] = {}
    cur = None
    for ln in text.splitlines():
        if ln.startswith("@@"):
            cur = ln[2:].split()[0] if len(ln) > 2 else ""
            sec.setdefault(cur, [])
            if cur == "S":
                sec["S"] = [ln]
        elif cur:
            sec[cur].append(ln)
    return sec


# ---------------------------------------------------------------------------------------------- static info
def parse_static(text: str) -> dict:
    sec = sections(text)
    soc = (sec.get("SOC", []) + ["", "", "", ""])[:4]
    info = {"soc_vendor": soc[0].strip(), "soc_model": soc[1].strip(), "platform": soc[2].strip(),
            "model": soc[3].strip()}
    cpuinfo = "\n".join(sec.get("CPUINFO", []))
    m = re.search(r"^Hardware\s*:\s*(.+)$", cpuinfo, re.M)
    info["hardware"] = m.group(1).strip() if m else ""
    parts = re.findall(r"^CPU part\s*:\s*(0x[0-9a-f]+)", cpuinfo, re.M | re.I)
    info["cpu_parts"] = parts
    maxf, minf = {}, {}
    for ln in sec.get("MAXF", []):
        m = re.search(r"cpu(\d+)/cpufreq/cpuinfo_(max|min)_freq:(\d+)", ln)
        if m:
            (maxf if m.group(2) == "max" else minf)[int(m.group(1))] = int(m.group(3))
    info["max_khz"], info["min_khz"] = maxf, minf
    conf = sec.get("CONF", ["4096", "100"])
    try:
        info["page"], info["hz"] = int(conf[0]), int(conf[1])
    except (ValueError, IndexError):
        info["page"], info["hz"] = 4096, 100
    un = sec.get("UNAME", [])
    info["kernel"] = un[0].strip() if un else ""
    info["uptime"] = float(un[1].split()[0]) if len(un) > 1 and un[1].split() else 0.0
    info["btime"] = int(un[2].split()[1]) if len(un) > 2 and len(un[2].split()) > 1 else 0
    gl = [x.strip() for x in sec.get("GPU", []) if x.strip()]
    gpu_model, gpu_max = "", 0
    for x in gl:
        if x.startswith("max_mhz"):
            try:
                gpu_max = int(x.split()[1])
            except (IndexError, ValueError):
                pass
        elif not x.startswith("/"):
            gpu_model = gpu_model or x
    if not gpu_model:
        if any("mali" in x for x in gl):
            gpu_model = "Arm Mali"
        elif any("kgsl" in x for x in gl):
            gpu_model = "Qualcomm Adreno"
    info["gpu_model"], info["gpu_max_mhz"] = gpu_model, gpu_max
    disks = []
    for ln in sec.get("BLOCK", []):
        p = (ln.split("|") + [""] * 6)[:6]
        try:
            size = int(p[1]) * 512
        except ValueError:
            size = 0
        kind = "UFS" if p[0].startswith("sd") else "eMMC/SD" if p[0].startswith("mmc") else "NVMe"
        disks.append({"dev": p[0], "size": size, "vendor": p[2].strip(), "model": (p[3] or p[4]).strip(),
                      "type": p[5].strip(), "kind": kind})
    info["disks"] = disks
    df, seen_fs = [], set()
    for ln in sec.get("DF", []):
        path, _, rest = ln.partition("|")
        f = rest.split()
        if len(f) >= 4 and f[1].isdigit() and (f[0], f[1]) not in seen_fs:
            seen_fs.add((f[0], f[1]))                 # the same filesystem under two paths is listed once
            df.append({"fs": f[0], "size": int(f[1]) * 1024, "used": int(f[2]) * 1024, "free": int(f[3]) * 1024,
                       "mount": path})
    info["df"] = df
    therm = {}
    for ln in sec.get("THERM", []):
        z, _, t = ln.partition("|")
        if z.isdigit():
            therm[int(z)] = t.strip()
    info["therm_types"] = therm
    pk: dict[int, list[str]] = {}
    for ln in sec.get("PKG", []):
        m = re.match(r"package:(\S+) uid:(\d+)", ln.strip())
        if m:
            pk.setdefault(int(m.group(2)), []).append(m.group(1))
    info["packages"] = pk
    z = [x.strip() for x in sec.get("ZRAM", []) if x.strip()]
    info["zram_size"] = int(z[0]) if z and z[0].isdigit() else 0
    info["zram_algo"] = (re.search(r"\[(\w+)\]", z[1]).group(1) if len(z) > 1 and "[" in z[1] else
                         (z[1] if len(z) > 1 else ""))
    return info


# ---------------------------------------------------------------------------------------------- snapshot
def _hex_addr(h: str) -> str:
    try:
        if len(h) == 8:
            return str(ipaddress.IPv4Address(struct.pack("<I", int(h, 16))))
        if len(h) == 32:
            raw = b"".join(struct.pack("<I", int(h[i:i + 8], 16)) for i in range(0, 32, 8))
            a = ipaddress.IPv6Address(raw)
            return str(a.ipv4_mapped) if a.ipv4_mapped else str(a)
    except Exception:  # noqa: BLE001
        pass
    return h


def _sockets(lines: list[str], proto: str) -> list[dict]:
    out = []
    for ln in lines:
        f = ln.split()
        if len(f) < 10 or ":" not in f[1] or f[0] == "sl":
            continue
        try:
            la, lp = f[1].split(":")
            ra, rp = f[2].split(":")
            st = int(f[3], 16)
            out.append({"proto": proto + ("6" if len(la) == 32 else ""), "local": _hex_addr(la), "lport": int(lp, 16),
                        "remote": _hex_addr(ra), "rport": int(rp, 16),
                        "state": (TCP_STATES.get(st, str(st)) if proto == "TCP" else ("UNCONN" if st == 7 else "CONN")),
                        "uid": int(f[7]), "inode": f[9]})
        except (ValueError, IndexError):
            continue
    return out


def parse_snapshot(text: str) -> dict | None:
    sec = sections(text)
    head = (sec.get("S") or [""])[0].split()
    if len(head) < 2:
        return None
    try:
        t = int(head[1]) / 1e9
    except ValueError:
        t = time.time()
    snap = {"t": t, "gpu_kind": head[2] if len(head) > 2 else "none"}
    cpus = {}
    for ln in sec.get("STAT", []):
        f = ln.split()
        if not f:
            continue
        if f[0].startswith("cpu"):
            v = [int(x) for x in f[1:9]] + [0] * (8 - len(f[1:9]))
            idle = v[3] + v[4]
            cpus["all" if f[0] == "cpu" else int(f[0][3:])] = (sum(v), idle)
        elif f[0] == "ctxt":
            snap["ctxt"] = int(f[1])
        elif f[0] == "procs_running":
            snap["running"] = int(f[1])
    snap["cpu"] = cpus
    mem = {}
    for ln in sec.get("MEM", []):
        k, _, v = ln.partition(":")
        v = v.split()
        if v:
            mem[k.strip()] = int(v[0]) * 1024
    snap["mem"] = mem
    disks = {}
    for ln in sec.get("DISK", []):
        f = ln.split()
        if len(f) >= 14:
            disks[f[2]] = (int(f[3]), int(f[5]), int(f[7]), int(f[9]), int(f[12]), int(f[11]))
    snap["disk"] = disks           # reads, sectors read, writes, sectors written, io_ticks ms, in flight
    net = {}
    for ln in sec.get("NET", [])[2:]:
        name, _, rest = ln.partition(":")
        f = rest.split()
        if len(f) >= 10:
            net[name.strip()] = (int(f[0]), int(f[1]), int(f[8]), int(f[9]))
    snap["net"] = net
    freq = {}
    for ln in sec.get("FREQ", []):
        m = re.search(r"cpu(\d+)/cpufreq/scaling_cur_freq:(\d+)", ln)
        if m:
            freq[int(m.group(1))] = int(m.group(2))
    snap["freq"] = freq
    snap["online"] = (sec.get("ONLINE") or [""])[0].strip()
    g = [x.strip() for x in sec.get("GPU", []) if x.strip()]
    gpu = {}
    try:
        if snap["gpu_kind"] == "kgsl" and g:
            gpu["busy"] = float(g[0].split()[0])
            if len(g) > 1:
                gpu["mhz"] = float(g[1].split()[0])
        elif snap["gpu_kind"] == "ged" and g:
            gpu["busy"] = float(g[0].split()[0])
            if len(g) > 1 and len(g[1].split()) > 1:
                gpu["mhz"] = int(g[1].split()[1]) / 1000
        elif snap["gpu_kind"] == "mali" and g:
            gpu["busy"] = float(re.findall(r"[\d.]+", g[0])[0])
    except (ValueError, IndexError):
        pass
    snap["gpu"] = gpu
    therm = {}
    for ln in sec.get("THERM", []):
        m = re.search(r"thermal_zone(\d+)/temp:(-?\d+)", ln)
        if m:
            v = int(m.group(2))
            c = v / 1000 if abs(v) > 1000 else float(v)
            if -30 < c < 150:
                therm[int(m.group(1))] = c
    snap["therm"] = therm
    bat = {}
    for ln in sec.get("BAT", []):
        k, _, v = ln.rpartition(":")
        bat[k.rsplit("/", 1)[-1]] = v.strip()
    snap["bat"] = bat
    procs = {}
    for ln in sec.get("PSTAT", []):
        a, b = ln.find("("), ln.rfind(")")
        if a < 0 or b < 0:
            continue
        try:
            pid = int(ln[:a])
            r = ln[b + 2:].split()
            procs[pid] = {"comm": ln[a + 1:b], "state": r[0], "ppid": int(r[1]), "ticks": int(r[11]) + int(r[12]),
                          "majflt": int(r[9]), "prio": int(r[15]), "nice": int(r[16]), "threads": int(r[17]),
                          "start": int(r[19]), "vsize": int(r[20]), "rss": int(r[21])}
        except (ValueError, IndexError):
            continue
    names = {}
    for ln in sec.get("PS", [])[1:]:
        f = ln.split(None, 2)
        if len(f) >= 3 and f[0].isdigit():
            names[int(f[0])] = (int(f[1]) if f[1].isdigit() else -1, f[2].strip())
    for pid, p in procs.items():
        uid, nm = names.get(pid, (-1, ""))
        p["uid"] = uid
        p["name"] = nm or p["comm"]
    io = {}
    for ln in sec.get("IO", []):
        m = re.match(r"/proc/(\d+)/io:(read_bytes|write_bytes):\s*(\d+)", ln)
        if m:
            io.setdefault(int(m.group(1)), [0, 0])[0 if m.group(2) == "read_bytes" else 1] = int(m.group(3))
    for pid, (r, w) in io.items():
        if pid in procs:
            procs[pid]["io_r"], procs[pid]["io_w"] = r, w
    snap["has_io"] = "IO" in sec
    snap["procs"] = procs
    snap["sockets"] = _sockets(sec.get("TCP", []), "TCP") + _sockets(sec.get("UDP", []), "UDP")
    return snap


# ---------------------------------------------------------------------------------------------- model
class Model:
    """Keeps the latest snapshot, derived rates and graph history."""

    def __init__(self, static: dict):
        self.static = static
        self.hz = static.get("hz", 100) or 100
        self.page = static.get("page", 4096) or 4096
        self.prev: dict | None = None
        self.cur: dict | None = None
        self.dt = 0.0
        self.hist = collections.defaultdict(lambda: collections.deque(maxlen=HIST))
        self.proc_cpu_hist = collections.defaultdict(lambda: collections.deque(maxlen=30))
        self.ncpu = max(1, len(static.get("max_khz", {})) or 1)

    def update(self, snap: dict):
        self.prev, self.cur = self.cur, snap
        if not self.prev:
            return
        self.dt = max(0.05, snap["t"] - self.prev["t"])
        h = self.hist
        cpu_now, cpu_prev = snap["cpu"], self.prev["cpu"]
        self.cpu_pct = {}
        for k, (tot, idle) in cpu_now.items():
            if k in cpu_prev:
                dt_, di = tot - cpu_prev[k][0], idle - cpu_prev[k][1]
                self.cpu_pct[k] = max(0.0, min(100.0, 100.0 * (dt_ - di) / dt_)) if dt_ > 0 else 0.0
        cores = [k for k in self.cpu_pct if k != "all"]
        self.ncpu = max(self.ncpu, len(cores), max(cores, default=-1) + 1)
        h["cpu"].append(self.cpu_pct.get("all", 0.0))
        for k in cores:
            h[f"core{k}"].append(self.cpu_pct[k])
        m = snap["mem"]
        total = m.get("MemTotal", 1)
        avail = m.get("MemAvailable", m.get("MemFree", 0) + m.get("Cached", 0))
        self.mem_used = total - avail
        h["mem"].append(100.0 * self.mem_used / total if total else 0)
        # disks: whole devices only
        self.disk_rates = {}
        for name, v in snap["disk"].items():
            pv = self.prev["disk"].get(name)
            if not pv:
                continue
            rd = (v[1] - pv[1]) * 512 / self.dt
            wr = (v[3] - pv[3]) * 512 / self.dt
            busy = min(100.0, (v[4] - pv[4]) / (self.dt * 1000) * 100)
            self.disk_rates[name] = {"read": max(0.0, rd), "write": max(0.0, wr), "busy": max(0.0, busy),
                                     "rio": max(0, v[0] - pv[0]) / self.dt, "wio": max(0, v[2] - pv[2]) / self.dt,
                                     "queue": v[5]}
        for d in self.whole_disks():
            r = self.disk_rates.get(d)
            if r:
                h[f"disk:{d}"].append(r["busy"])
                h[f"diskr:{d}"].append(r["read"])
                h[f"diskw:{d}"].append(r["write"])
        stor = self.storage_rates()
        h["storage"].append(stor["busy"])
        h["storage_r"].append(stor["read"])
        h["storage_w"].append(stor["write"])
        self.net_rates = {}
        for name, v in snap["net"].items():
            pv = self.prev["net"].get(name)
            if pv:
                self.net_rates[name] = {"rx": max(0, v[0] - pv[0]) / self.dt, "tx": max(0, v[2] - pv[2]) / self.dt,
                                        "rx_total": v[0], "tx_total": v[2]}
                h[f"rx:{name}"].append(self.net_rates[name]["rx"])
                h[f"tx:{name}"].append(self.net_rates[name]["tx"])
        if "busy" in snap["gpu"]:
            h["gpu"].append(snap["gpu"]["busy"])
        if snap["therm"]:
            h["temp"].append(max(self.cpu_temps() or list(snap["therm"].values())))
        # processes
        ticks_per_s = self.hz * self.dt
        for pid, p in snap["procs"].items():
            pp = self.prev["procs"].get(pid)
            if pp and pp.get("start") == p.get("start"):
                p["cpu"] = max(0.0, (p["ticks"] - pp["ticks"]) / ticks_per_s / self.ncpu * 100)
                p["faults"] = max(0, p["majflt"] - pp["majflt"]) / self.dt
                if "io_r" in p and "io_r" in pp:
                    p["io_rs"] = max(0, p["io_r"] - pp["io_r"]) / self.dt
                    p["io_ws"] = max(0, p["io_w"] - pp["io_w"]) / self.dt
            else:
                p["cpu"] = 0.0
                p["faults"] = 0.0
            p["mem"] = p["rss"] * self.page
            hist = self.proc_cpu_hist[(pid, p.get("start"))]
            hist.append(p["cpu"])
            p["cpu_avg"] = sum(hist) / len(hist)
        alive = {(pid, p.get("start")) for pid, p in snap["procs"].items()}
        for k in [k for k in self.proc_cpu_hist if k not in alive]:
            del self.proc_cpu_hist[k]

    def whole_disks(self) -> list[str]:
        if not self.cur:
            return []
        return [n for n in self.cur["disk"] if re.fullmatch(r"sd[a-z]|mmcblk\d|nvme\d+n\d+", n)]

    def drives(self) -> list[dict]:
        """Physical storage: UFS exposes one chip as several LUNs (sda..sdf); group them by model."""
        groups: dict[str, dict] = {}
        for d in self.static.get("disks", []):
            key = f"{d['kind']}|{d['vendor']}|{d['model']}" if d["model"] else d["dev"]
            g = groups.setdefault(key, {"name": " ".join(x for x in (d["vendor"], d["model"]) if x) or d["dev"],
                                        "kind": d["kind"], "size": 0, "devs": []})
            g["size"] += d["size"]
            g["devs"].append(d["dev"])
        if not groups:
            for n in self.whole_disks():
                groups[n] = {"name": n, "kind": "", "size": 0, "devs": [n]}
        return list(groups.values())

    def drive_rates(self, devs: list[str]) -> dict:
        r = {"read": 0.0, "write": 0.0, "busy": 0.0, "rio": 0.0, "wio": 0.0, "queue": 0}
        for d in devs:
            x = getattr(self, "disk_rates", {}).get(d)
            if x:
                for k in ("read", "write", "rio", "wio", "queue"):
                    r[k] += x[k]
                r["busy"] = max(r["busy"], x["busy"])
        return r

    def storage_rates(self) -> dict:
        r = {"read": 0.0, "write": 0.0, "busy": 0.0}
        for d in self.whole_disks():
            x = getattr(self, "disk_rates", {}).get(d)
            if x:
                r["read"] += x["read"]
                r["write"] += x["write"]
                r["busy"] = max(r["busy"], x["busy"])
        return r

    def cpu_temps(self) -> list[float]:
        types = self.static.get("therm_types", {})
        out = []
        for z, c in (self.cur or {}).get("therm", {}).items():
            ty = types.get(z, "").lower()
            if re.search(r"cpu|tsens_tz_sensor|mtktscpu|soc|apc|cpuss", ty) and "gpu" not in ty:
                out.append(c)
        return out

    def interfaces(self) -> list[str]:
        """Interfaces worth showing: up with traffic, not loopback/dummy."""
        if not self.cur:
            return []
        out = []
        for n, v in self.cur["net"].items():
            if n in ("lo",) or n.startswith(("dummy", "ip6tnl", "sit", "ip_vti", "ip6_vti", "ifb", "gre", "erspan")):
                continue
            if v[0] + v[2] > 0:
                out.append(n)
        return sorted(out, key=lambda n: -(self.cur["net"][n][0] + self.cur["net"][n][2]))


# ---------------------------------------------------------------------------------------------- device side
class Device:
    def __init__(self, api):
        self.api = api
        self.conn = api.conn
        self.user_uid = None
        self.use_root = False

    @property
    def is_adb(self) -> bool:
        return getattr(self.conn, "transport", None) is None

    def root_ok(self) -> bool:
        try:
            return bool(self.api.is_root() or getattr(self.conn, "native_root", False))
        except Exception:  # noqa: BLE001
            return False

    def probe(self):
        rc, out, _ = self.conn.exec("id -u", timeout=20, as_root=False)
        try:
            self.user_uid = int(out.decode(errors="replace").split()[0])
        except (ValueError, IndexError):
            self.user_uid = -1
        # root shows every process's I/O, disk stats, temperatures and GPU load; an app-uid session sees nothing
        self.use_root = self.root_ok()
        if not self.use_root and self.user_uid not in (0, 2000):
            raise RuntimeError(f"The connection runs as an app (uid {self.user_uid}), which cannot see other "
                               "processes. Turn Root on, or start Shizuku on the device so the session runs as the "
                               "shell user, then try again.")

    def run(self, cmd: str, timeout: float = 60, root: bool | None = None) -> tuple[int, str, str]:
        r = self.use_root if root is None else root
        rc, out, err = self.conn.exec(cmd, timeout=timeout, as_root=r)
        return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def static(self) -> dict:
        self.probe()
        rc, out, err = self.run(STATIC_SCRIPT, timeout=90)
        info = parse_static(out)
        info["root"] = self.use_root
        info["labels"] = self.labels()
        return info

    def labels(self) -> dict[str, str]:
        """App names from AndroUnderTheHood's own helper (best effort)."""
        try:
            from androunderthehood import apps as A
            if self.api.fs is None:
                return {}
            A.ensure_helper(self.conn, self.api.fs)
            rc, out, _ = self.conn.exec(f"{A.HELPER_RUN} labels 2>/dev/null", timeout=90, as_root=False)
            res = {}
            for line in out.decode("utf-8", "replace").splitlines():
                p = line.split("\t")
                if len(p) == 3 and p[0] == "L":
                    res[p[1]] = p[2]
            return res
        except Exception:  # noqa: BLE001
            return {}

    def open_stream(self, script: str):
        if self.is_adb:
            cmd = self.conn.wrap_root(script) if self.use_root else script
            if self.use_root:
                # Magisk su may give the command a pty (seen with Magisk 28 on the onn tablet): newlines become
                # CR LF and output can be lost. A pipe as su's stdout keeps the stream byte-exact.
                cmd += " | cat"
            p = subprocess.Popen([self.conn.exe, "-s", self.conn.serial, "exec-out", cmd], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=_NOWIN)
            return (lambda n: p.stdout.read1(n)), p.kill
        if getattr(self.conn, "use_pty", False):
            raise RuntimeError("This SSH server only allows interactive shells, which can't carry a live stream.")
        chan = self.conn.open_channel(self.use_root)
        chan.exec_command(self.conn.wrap_root(script) if self.use_root else script)
        chan.shutdown_write()

        def read(n):
            while chan.recv_stderr_ready():
                chan.recv_stderr(65536)
            return chan.recv(n)
        return read, chan.close

    @staticmethod
    def token_pattern(token: str) -> str:
        """Regex that matches the sampler's "T=<token>;" but not the pkill/pgrep command line itself
        (otherwise pkill also kills the shell it runs in)."""
        return shq(f"T={token[0]}[{token[1]}]{token[2:]};")

    def kill_leftovers(self, token: str):
        pat = self.token_pattern(token)
        for root in ([self.use_root, True] if self.root_ok() else [False]):
            try:
                rc, out, _ = self.conn.exec(f"pkill -f {pat} 2>/dev/null; sleep 0.3; "
                                            f"pgrep -f {pat} >/dev/null && echo alive",
                                            timeout=15, as_root=root)
                if b"alive" not in out:
                    return
            except Exception:  # noqa: BLE001
                pass


class Sampler(QObject):
    snapshot = Signal(dict)
    failed = Signal(str)
    ended = Signal()

    def __init__(self, dev: Device, interval: float, token: str, prev_token: str = ""):
        super().__init__()
        self.dev, self.interval, self.token, self.prev_token = dev, interval, token, prev_token
        self._stop = threading.Event()
        self._stopfn = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._stopfn:
            try:
                self._stopfn()
            except Exception:  # noqa: BLE001
                pass

    def _run(self):
        try:
            if self.prev_token:
                self.dev.kill_leftovers(self.prev_token)
            script = (SAMPLE_SCRIPT.replace("@TOKEN@", self.token).replace("@INTERVAL@", f"{self.interval:g}")
                      .replace("@ROOT@", "1" if self.dev.use_root else "0"))
            read, self._stopfn = self.dev.open_stream(script)
            if self._stop.is_set():
                self._stopfn()
                return
            buf = b""
            while not self._stop.is_set():
                chunk = read(1 << 18)
                if not chunk:
                    break
                buf += chunk
                while b"@@END\n" in buf:
                    block, buf = buf.split(b"@@END\n", 1)
                    snap = parse_snapshot(block.decode("utf-8", "replace"))
                    if snap:
                        self.snapshot.emit(snap)
            if not self._stop.is_set():
                raise RuntimeError("The sampler on the device stopped.")
        except Exception as e:  # noqa: BLE001
            if not self._stop.is_set():
                self.failed.emit(str(e) or repr(e))
        finally:
            if self._stopfn:
                try:
                    self._stopfn()
                except Exception:  # noqa: BLE001
                    pass
            self.ended.emit()
