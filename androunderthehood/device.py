"""Device window: hardware info, boot/security state, reboot modes, ADB and USB controls."""
from __future__ import annotations

import logging
import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QGridLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from . import backend as B
from . import theme
from .props import parse_getprop

log = logging.getLogger("androunderthehood")
q = B.shq

# ---------------------------------------------------------------------------
# lookup tables
# ---------------------------------------------------------------------------
SOC_NAMES = {
    "SM8750": "Snapdragon 8 Elite", "SM8650": "Snapdragon 8 Gen 3", "SM8635": "Snapdragon 8s Gen 3",
    "SM8550": "Snapdragon 8 Gen 2", "SM8475": "Snapdragon 8+ Gen 1", "SM8450": "Snapdragon 8 Gen 1",
    "SM8350": "Snapdragon 888", "SM8250": "Snapdragon 865", "SM8150": "Snapdragon 855", "SDM845": "Snapdragon 845",
    "SM7675": "Snapdragon 7+ Gen 3", "SM7550": "Snapdragon 7 Gen 3", "SM7475": "Snapdragon 7+ Gen 2",
    "SM7450": "Snapdragon 7 Gen 1", "SM7325": "Snapdragon 778G", "SM7250": "Snapdragon 765G",
    "SM6450": "Snapdragon 6 Gen 1", "SM6375": "Snapdragon 695", "SM4375": "Snapdragon 4 Gen 1",
}
CPU_PARTS = {  # ARM implementer 0x41
    "0xd03": "Cortex-A53", "0xd04": "Cortex-A35", "0xd05": "Cortex-A55", "0xd07": "Cortex-A57", "0xd08": "Cortex-A72",
    "0xd09": "Cortex-A73", "0xd0a": "Cortex-A75", "0xd0b": "Cortex-A76", "0xd0d": "Cortex-A77", "0xd41": "Cortex-A78",
    "0xd44": "Cortex-X1", "0xd46": "Cortex-A510", "0xd47": "Cortex-A710", "0xd48": "Cortex-X2", "0xd4d": "Cortex-A715",
    "0xd4e": "Cortex-X3", "0xd80": "Cortex-A520", "0xd81": "Cortex-A720", "0xd82": "Cortex-X4", "0xd85": "Cortex-X925",
    "0xd87": "Cortex-A725",
}
HDR_TYPES = {1: "Dolby Vision", 2: "HDR10", 3: "HLG", 4: "HDR10+"}
BATT_HEALTH = {1: "unknown", 2: "good", 3: "overheat", 4: "dead", 5: "over voltage", 6: "failure", 7: "cold"}
BATT_STATUS = {1: "unknown", 2: "charging", 3: "discharging", 4: "not charging", 5: "full"}
USB_FUNCS = {
    "none": "charging only", "mtp": "file transfer (MTP)", "ptp": "photo transfer (PTP)", "adb": "ADB debugging",
    "rndis": "USB tethering (RNDIS)", "ncm": "USB Ethernet (NCM)", "midi": "MIDI", "uvc": "webcam (UVC)",
    "uac2": "USB audio (UAC2)", "audio_source": "audio source", "accessory": "Android accessory (AOA)",
    "mass_storage": "mass storage (UMS)", "ccid": "smart card (CCID)", "diag": "Qualcomm DIAG",
    "diag_mdm": "DIAG (modem)", "diag_mdm2": "DIAG (modem 2)", "diag_cnss": "DIAG (Wi-Fi/BT)",
    "serial_cdev": "modem serial / AT", "serial_cdev_mdm": "modem serial (MDM)", "serial_cdev_nmea": "GPS NMEA serial",
    "rmnet": "RMNET (modem data)", "dpl": "data-path logging", "qdss": "QDSS trace", "qdss_mdm": "QDSS trace (modem)",
    "sec": "security", "*": "(any)",
}

REBOOT_MODES = [
    # key, label, command, description, warning level (0 normal, 1 caution, 2 danger), vendor note
    ("system", "Reboot", "reboot", "Normal restart.", 0, ""),
    ("recovery", "Recovery", "reboot recovery", "Recovery (stock recovery, TWRP/OrangeFox if installed).", 0, ""),
    ("bootloader", "Bootloader", "reboot bootloader",
     "Bootloader fastboot mode – for `fastboot flash`, unlock/lock, `fastboot boot`.", 0, ""),
    ("fastbootd", "Fastbootd", "reboot fastboot",
     "Userspace fastboot (fastbootd) – needed to flash logical partitions inside super.", 0, ""),
    ("sideload", "Sideload", "reboot sideload", "Recovery directly in ADB sideload mode.", 0, ""),
    ("safemode", "Safe mode", "setprop persist.sys.safemode 1 && reboot",
     "Restart with all third-party apps disabled (until the next normal reboot).", 1, ""),
    ("soft", "Soft reboot", "setprop ctl.restart zygote",
     "Restart Android's framework only (faster; the kernel keeps running).", 1, ""),
    ("systemui", "Restart System UI", "pkill -f com.android.systemui",
     "Restart the status bar / navigation / lock screen process.", 0, ""),
    ("poweroff", "Power off", "reboot -p", "Shut the phone down.", 1, ""),
    ("edl", "EDL (9008)", "reboot edl",
     "Qualcomm Emergency Download mode (USB 05c6:9008) for firehose/QFIL/MSM tools. The screen stays black; hold "
     "Power + Volume Up for ~10–15 s to leave it.", 2, "Qualcomm"),
    ("ftm", "FTM / factory", "reboot ftm", "OnePlus/Oppo factory test mode.", 2, "OnePlus/Oppo"),
    ("download", "Download mode", "reboot download", "Samsung Odin download mode.", 1, "Samsung"),
    ("meta", "META mode", "reboot meta", "MediaTek META (engineering) mode.", 2, "MediaTek"),
]


def human(n: float, unit="B") -> str:
    f = float(n)
    for u in ("", "K", "M", "G", "T"):
        if f < 1024 or u == "T":
            return f"{f:.0f} {u}{unit}" if not u else f"{f:.1f} {u}{unit}"
        f /= 1024
    return str(n)


# ---------------------------------------------------------------------------
# data gathering
# ---------------------------------------------------------------------------
GATHER = r"""
echo @PROPS; getprop
echo @CMDLINE; cat /proc/cmdline 2>/dev/null; echo; cat /proc/bootconfig 2>/dev/null
echo @SET; echo "adb_enabled=$(settings get global adb_enabled)"; echo "adb_wifi_enabled=$(settings get global adb_wifi_enabled)"
echo "dev_settings=$(settings get global development_settings_enabled)"; echo "selinux=$(getenforce)"
echo "otg=$(settings get global persist.sys.oplus.otg_support 2>/dev/null)"
echo "magisk=$(magisk -v 2>/dev/null) $(magisk -V 2>/dev/null)"; echo "ksu=$(ksud -V 2>/dev/null)"; echo "apatch=$(apd -V 2>/dev/null)"
echo "kernel=$(uname -r)"; echo "kernel_build=$(uname -v)"; echo "usbfn=$(svc usb getFunctions 2>&1 | tail -n1)"
echo "uptime=$(cut -d' ' -f1 /proc/uptime)"
echo @DEVINFO; for f in /proc/devinfo/*; do [ -f "$f" ] && { echo "## ${f##*/}"; cat "$f"; }; done
echo @MEM; head -3 /proc/meminfo
echo @CPU; grep -E "^CPU (part|implementer)" /proc/cpuinfo; for c in /sys/devices/system/cpu/cpu[0-9]*; do read f < "$c/cpufreq/cpuinfo_max_freq" 2>/dev/null; echo "freq ${c##*/} $f"; done
echo @UFS; cat /sys/block/sda/device/vendor /sys/block/sda/device/model 2>/dev/null
for d in /sys/devices/platform/soc/*ufshc*/device_descriptor/specification_version; do [ -f "$d" ] && { cat "$d"; break; }; done
for b in /sys/block/sd[a-z]; do read s < "$b/size"; echo "disk ${b##*/} $s"; done
echo @DF; df -k /data 2>/dev/null | tail -1
echo @DISPLAY; dumpsys display 2>/dev/null > /data/local/tmp/.db_disp; grep -m1 "mBaseDisplayInfo" /data/local/tmp/.db_disp; rm -f /data/local/tmp/.db_disp
wm size; wm density
echo @GPU; dumpsys SurfaceFlinger 2>/dev/null > /data/local/tmp/.db_sf; grep -m1 GLES /data/local/tmp/.db_sf; rm -f /data/local/tmp/.db_sf
echo @BATT; for f in charge_full_design charge_full cycle_count; do [ -f /sys/class/power_supply/battery/$f ] && echo "$f=$(cat /sys/class/power_supply/battery/$f)"; done
dumpsys battery 2>/dev/null
echo @TYPEC; for p in /sys/class/typec/port[0-9]; do [ -d "$p" ] && echo "${p##*/} power=$(cat $p/power_role 2>/dev/null) data=$(cat $p/data_role 2>/dev/null)"; done
dumpsys usb 2>/dev/null > /data/local/tmp/.db_usb; sed -n '/usb_ports={/,/connected_at_millis/p' /data/local/tmp/.db_usb | head -60; rm -f /data/local/tmp/.db_usb
echo @USBCFG; grep -h "on property:sys.usb.config=" /vendor/etc/init/hw/*.rc /system/etc/init/hw/*.rc /vendor/etc/init/*.rc /odm/etc/init/*.rc 2>/dev/null | sed "s/.*sys.usb.config=//; s/ .*//" | sort -u
echo @END
"""


def gather(conn: B.Connection) -> dict:
    rc, out, err = conn.exec(GATHER, timeout=120)
    text = out.decode("utf-8", errors="replace")
    sec: dict[str, str] = {}
    cur = None
    buf: list[str] = []
    for line in text.split("\n"):
        if re.fullmatch(r"@[A-Z]+", line.strip()):
            if cur:
                sec[cur] = "\n".join(buf)
            cur, buf = line.strip()[1:], []
        else:
            buf.append(line)
    if cur:
        sec[cur] = "\n".join(buf)
    props = dict(parse_getprop(sec.get("PROPS", "")))
    sets = dict(l.split("=", 1) for l in sec.get("SET", "").splitlines() if "=" in l)
    return {"props": props, "cmdline": sec.get("CMDLINE", ""), "set": sets, "raw": sec}


def boot_params(cmdline: str) -> dict:
    out = {}
    for m in re.finditer(r'(androidboot\.[\w.]+|oplusboot\.[\w.]+)\s*=\s*"?([^"\s]*)"?', cmdline):
        out.setdefault(m.group(1), m.group(2))
    return out


def hardware_rows(d: dict) -> list[tuple[str, str, str]]:
    """[(section, key, value)]"""
    p, raw, s = d["props"], d["raw"], d["set"]
    rows = []
    add = lambda sec, k, v: rows.append((sec, k, v)) if v not in (None, "") else None  # noqa: E731
    # device
    name = p.get("ro.vendor.oplus.market.name") or p.get("ro.product.marketname") or p.get("ro.product.model", "")
    add("Device", "Name", name)
    add("Device", "Brand / maker", f"{p.get('ro.product.brand', '')} / {p.get('ro.product.manufacturer', '')}")
    add("Device", "Model / codename", f"{p.get('ro.product.model', '')} / {p.get('ro.product.device', '')}")
    add("Device", "Android", f"{p.get('ro.build.version.release', '')} (SDK {p.get('ro.build.version.sdk', '')})")
    add("Device", "Build", p.get("ro.build.display.id", ""))
    add("Device", "Kernel", s.get("kernel", ""))
    up = s.get("uptime", "")
    if up:
        sec_up = int(float(up))
        add("Device", "Uptime", f"{sec_up // 86400} d {sec_up % 86400 // 3600} h {sec_up % 3600 // 60} min")
    # SoC / CPU
    model = p.get("ro.soc.model", "")
    soc = SOC_NAMES.get(model.upper(), "")
    add("SoC", "Chip", f"{soc} ({p.get('ro.soc.manufacturer', '')} {model})" if soc else
        f"{p.get('ro.soc.manufacturer', '')} {model}".strip())
    add("SoC", "Platform", f"{p.get('ro.board.platform', '')} / {p.get('ro.vendor.qti.soc_name', '')}".strip(" /"))
    parts = re.findall(r"CPU part\s*:\s*(0x[0-9a-f]+)", raw.get("CPU", ""))
    freqs = re.findall(r"freq cpu(\d+) (\d+)", raw.get("CPU", ""))
    if parts:
        clusters: dict[tuple, int] = {}
        fmap = {int(c): int(f) for c, f in freqs if f}
        for i, part in enumerate(parts):
            key = (CPU_PARTS.get(part, part), fmap.get(i, 0))
            clusters[key] = clusters.get(key, 0) + 1
        add("SoC", "CPU", " + ".join(f"{n}× {core} @ {f / 1e6:.2f} GHz" if f else f"{n}× {core}"
                                     for (core, f), n in clusters.items()))
    gpu = re.search(r"GLES: ([^,]+), ([^,]+), (OpenGL ES [\d.]+)", raw.get("GPU", ""))
    if gpu:
        add("SoC", "GPU", f"{gpu.group(2)} ({gpu.group(3)})")
    add("SoC", "ABIs", p.get("ro.product.cpu.abilist", ""))
    # memory / storage
    devinfo = raw.get("DEVINFO", "")

    def dev(section):
        m = re.search(rf"## {section}\n(.*?)(?=\n## |\Z)", devinfo, re.S)
        if not m:
            return ""
        vals = re.findall(r"Device (?:version|manufacture):\s*(.+)", m.group(1))
        return " · ".join(v.strip() for v in vals if v.strip())
    mt = re.search(r"MemTotal:\s+(\d+)", raw.get("MEM", ""))
    if mt:
        kb = int(mt.group(1))
        marketing = next((g for g in (2, 3, 4, 6, 8, 12, 16, 18, 24, 32) if kb / 1048576 <= g), round(kb / 1048576))
        add("Memory", "RAM", f"{marketing} GB" + (f" {dev('ddr_type')}" if dev("ddr_type") else "")
            + f"  (usable {kb / 1048576:.1f} GB)")
    ufs = raw.get("UFS", "").split("\n")
    disks = [int(x.split()[2]) * 512 for x in ufs if x.startswith("disk ") and len(x.split()) > 2]
    spec = next((x.strip() for x in ufs if x.strip().startswith("0x")), "")
    ufs_ver = ""
    if spec:
        v = int(spec, 16)
        ufs_ver = f"UFS {v >> 8}.{(v >> 4) & 0xF}"
    vendor_model = " ".join(x.strip() for x in ufs[:2] if x.strip() and not x.startswith(("0x", "disk")))
    total = sum(disks)
    marketing = next((g for g in (16, 32, 64, 128, 256, 512, 1024, 2048) if total / 1e9 <= g * 1.08), 0)
    add("Memory", "Storage", f"{marketing if marketing < 1024 else marketing // 1024} "
        f"{'GB' if marketing < 1024 else 'TB'} {ufs_ver}  ({vendor_model})".strip() if total else "")
    dfl = raw.get("DF", "").split()
    if len(dfl) >= 4 and dfl[1].isdigit():
        add("Memory", "/data", f"{human(int(dfl[2]) * 1024)} used of {human(int(dfl[1]) * 1024)}")
    # display
    disp = raw.get("DISPLAY", "")
    m = re.search(r"real (\d+) x (\d+)", disp)
    lcd = dev("lcd")
    if m:
        add("Display", "Resolution", f"{m.group(1)} × {m.group(2)}")
    dm = re.search(r"density (\d+), ([\d.]+) x ([\d.]+) dpi", disp)
    if dm:
        add("Display", "Density", f"{dm.group(1)} dpi (physical {float(dm.group(2)):.0f} ppi)")
    fps = sorted({float(x) for x in re.findall(r"fps=([\d.]+)", disp)})
    if fps:
        add("Display", "Refresh rates", ", ".join(f"{f:g} Hz" for f in fps))
    hdr = re.search(r"mSupportedHdrTypes=\[([^\]]*)\]", disp)
    if hdr and hdr.group(1).strip():
        add("Display", "HDR", ", ".join(HDR_TYPES.get(int(x), x) for x in hdr.group(1).split(",") if x.strip()))
    lum = re.search(r"mMaxLuminance=([\d.]+)", disp)
    if lum:
        add("Display", "Peak luminance (reported)", f"{float(lum.group(1)):.0f} nits")
    if lcd:
        add("Display", "Panel", lcd)
    # radio
    add("Radio", "Baseband", p.get("gsm.version.baseband", ""))
    add("Radio", "RIL", p.get("gsm.version.ril-impl", ""))
    add("Radio", "Operator", p.get("gsm.operator.alpha", "").strip(","))
    add("Radio", "Network type", p.get("gsm.network.type", "").strip(","))
    add("Radio", "SIM state", p.get("gsm.sim.state", "").strip(","))
    # battery
    b = raw.get("BATT", "")
    kv = dict(re.findall(r"(\w+)=(\d+)", b))
    db = dict(re.findall(r"^\s+([\w ]+): (.+)$", b, re.M))
    design = int(kv.get("charge_full_design", 0) or 0)
    full = int(kv.get("charge_full", 0) or 0)
    if design:
        add("Battery", "Design capacity", f"{design / 1000:.0f} mAh")
    if full:
        add("Battery", "Full-charge capacity", f"{full / 1000:.0f} mAh" + (f"  ({full * 100 / design:.0f}% health)"
                                                                          if design else ""))
    add("Battery", "Charge cycles", kv.get("cycle_count", ""))
    if "level" in db:
        st = BATT_STATUS.get(int(db.get("status", 1)), db.get("status", ""))
        add("Battery", "Level", f"{db['level']}%  ({st})")
    if "health" in db:
        add("Battery", "Health (reported)", BATT_HEALTH.get(int(db["health"]), db["health"]))
    if "temperature" in db:
        add("Battery", "Temperature", f"{int(db['temperature']) / 10:.1f} °C")
    if "voltage" in db:
        add("Battery", "Voltage", f"{int(db['voltage']) / 1000:.2f} V")
    for k in ("tp", "audio_mainboard"):
        v = dev(k)
        if v:
            add("Components", {"tp": "Touch panel", "audio_mainboard": "Audio"}[k], v)
    return rows


def security_rows(d: dict) -> list[tuple[str, str, str, str]]:
    """[(key, value, status, note)] status: ok / warn / bad / info"""
    p, s = d["props"], d["set"]
    bp = boot_params(d["cmdline"])
    rows = []
    real_state = bp.get("androidboot.vbmeta.device_state", "")
    prop_state = p.get("ro.boot.vbmeta.device_state", "")
    real_vb = bp.get("androidboot.verifiedbootstate", "") or bp.get("oplusboot.verifiedbootstate", "")
    prop_vb = p.get("ro.boot.verifiedbootstate", "")
    unlocked = (real_state or prop_state) == "unlocked" or p.get("ro.boot.flash.locked") == "0"
    spoof = bool(real_state and prop_state and real_state != prop_state) or \
        bool(real_vb and prop_vb and real_vb != prop_vb)
    rows.append(("Bootloader", "UNLOCKED" if unlocked else "locked", "warn" if unlocked else "ok",
                 f"kernel says '{real_state or '?'}', property says '{prop_state or '?'}'"))
    vb_note = {"green": "locked, verified OS", "yellow": "locked, custom key", "orange": "unlocked – not verified",
               "red": "verification failed"}.get(real_vb or prop_vb, "")
    rows.append(("Verified boot", (real_vb or prop_vb or "?").upper(),
                 {"green": "ok", "yellow": "warn", "orange": "warn", "red": "bad"}.get(real_vb or prop_vb, "info"),
                 vb_note))
    if spoof:
        rows.append(("Property spoofing", "DETECTED", "warn",
                     f"ro.boot.* properties ({prop_state}/{prop_vb}) differ from the kernel's real values "
                     f"({real_state}/{real_vb}) – typically a root-hiding module."))
    rows.append(("OEM unlocking (setting)", {"1": "allowed", "0": "not allowed"}.get(
        p.get("sys.oem_unlock_allowed", ""), p.get("sys.oem_unlock_allowed", "?")), "info",
        "Developer options → OEM unlocking" + ("; device supports unlocking" if p.get("ro.oem_unlock_supported") == "1"
                                              else "")))
    rows.append(("dm-verity", p.get("ro.boot.veritymode", "?"), "ok" if p.get("ro.boot.veritymode") == "enforcing"
                 else "warn", ""))
    rows.append(("AVB version", p.get("ro.boot.vbmeta.avb_version", "?"), "info", ""))
    se = s.get("selinux", "?")
    rows.append(("SELinux", se, "ok" if se == "Enforcing" else "warn", ""))
    root = []
    if s.get("magisk", "").strip():
        root.append("Magisk " + s["magisk"].split()[0].split(":")[0])
    if s.get("ksu", "").strip():
        root.append("KernelSU " + s["ksu"].strip())
    if s.get("apatch", "").strip():
        root.append("APatch " + s["apatch"].strip())
    rows.append(("Root", ", ".join(root) or "not detected", "warn" if root else "ok", ""))
    rows.append(("Encryption", f"{p.get('ro.crypto.state', '?')} ({p.get('ro.crypto.type', '?')}-based)",
                 "ok" if p.get("ro.crypto.state") == "encrypted" else "bad", ""))
    rows.append(("Build type", f"{p.get('ro.build.type', '?')} / {p.get('ro.build.tags', '?')}", "info", ""))
    rows.append(("ro.debuggable / ro.secure", f"{p.get('ro.debuggable', '?')} / {p.get('ro.secure', '?')}", "info", ""))
    rows.append(("Security patch", p.get("ro.build.version.security_patch", "?"), "info",
                 f"vendor {p.get('ro.vendor.build.security_patch', '?')}"))
    rows.append(("Developer options", "on" if s.get("dev_settings") == "1" else "off", "info", ""))
    rows.append(("USB debugging (ADB)", "on" if s.get("adb_enabled") == "1" else "off", "info", ""))
    rows.append(("Active slot", p.get("ro.boot.slot_suffix", "–").lstrip("_").upper() or "–", "info", ""))
    rows.append(("Fingerprint", p.get("ro.build.fingerprint", ""), "info", ""))
    return rows


ROLE_LABELS = {
    ("sink", "device"): ("Normal (phone charges, PC is host)", "the phone draws power and is a USB device"),
    ("source", "host"): ("OTG host (phone powers the device)", "flash drives, keyboards, mice, other phones"),
    ("source", "device"): ("Reverse charge (phone powers, stays a device)", "phone supplies power to the partner"),
    ("sink", "host"): ("Host while being charged", "powered hub/dock: phone is host but receives power"),
}


def parse_ports(d: dict) -> dict:
    """Type-C port info from `dumpsys usb`: id, current roles, supported combinations, can-change flags."""
    t = d["raw"].get("TYPEC", "")
    out = {"id": None, "power": None, "data": None, "combos": [], "can_power": False, "can_data": False,
           "connected": None}
    m = re.search(r"\bid=(\S+)", t)
    if m:
        out["id"] = m.group(1)
    for k, key in (("power_role", "power"), ("data_role", "data")):
        m = re.search(rf"^\s+{k}=(\w+)", t, re.M)
        if m:
            out[key] = m.group(1)
    combo_txt = t.split("role_combinations=[", 1)[1].split("]", 1)[0] if "role_combinations=[" in t else ""
    out["combos"] = re.findall(r"power_role=(\w+)\s+data_role=(\w+)", combo_txt)
    out["can_power"] = "can_change_power_role=true" in t
    out["can_data"] = "can_change_data_role=true" in t
    m = re.search(r"connected=(\w+)", t)
    out["connected"] = m.group(1) == "true" if m else None
    if not out["power"]:   # fall back to sysfs typec
        m = re.search(r"(port\d) power=.*?\[(\w+)\].*?data=.*?\[(\w+)\]", t)
        if m:
            out["id"], out["power"], out["data"] = out["id"] or m.group(1), m.group(2), m.group(3)
    return out


def usb_configs(d: dict) -> list[str]:
    cfgs = [c.strip() for c in d["raw"].get("USBCFG", "").splitlines() if c.strip() and c.strip() != "*"]
    return sorted(set(cfgs), key=lambda c: (c.count(","), c))


def describe_usb(cfg: str) -> str:
    return " + ".join(USB_FUNCS.get(f, f) for f in cfg.split(","))


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
class DeviceDialog(QDialog):
    def __init__(self, win, conn: B.Connection):
        super().__init__(win)
        from .ui import Task

        self.Task = Task
        self.win = win
        self.conn = conn
        self.data: dict | None = None
        self.setWindowTitle(f"Device – {conn.profile.name}")
        self.resize(1100, 800)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 12)
        top = QHBoxLayout()
        self.title = QLabel("Reading device…")
        self.title.setObjectName("propsTitle")
        top.addWidget(self.title, 1)
        self.btn_refresh = QPushButton(theme.line_icon("refresh"), " Refresh")
        self.btn_refresh.setAutoDefault(False)
        self.btn_refresh.clicked.connect(self.load)
        self.btn_copy = QPushButton(theme.line_icon("copy"), " Copy report")
        self.btn_copy.setAutoDefault(False)
        self.btn_copy.clicked.connect(self.copy_report)
        top.addWidget(self.btn_copy)
        top.addWidget(self.btn_refresh)
        lay.addLayout(top)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        lay.addWidget(self.tabs, 1)
        self.hw = self._table(3, ["", "", ""])
        self.tabs.addTab(self.hw, "Hardware")
        self.sec = self._table(3, ["", "", ""])
        self.tabs.addTab(self.sec, "Security && boot")
        self.power_tab = self._power_tab()
        self.tabs.addTab(self.power_tab, "Reboot / power")
        self.adb_tab = QWidget()
        self.tabs.addTab(self.adb_tab, "ADB")
        self.usb_tab = QWidget()
        self.tabs.addTab(self.usb_tab, "USB")
        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(self.status)
        self._build_adb_tab()
        self._build_usb_tab()
        self.load()

    def _table(self, cols, headers):
        t = QTableWidget(0, cols)
        t.horizontalHeader().hide()
        t.verticalHeader().hide()
        t.setShowGrid(False)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        t.setWordWrap(True)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        t.horizontalHeader().setStretchLastSection(True)
        t.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        return t

    # ---- loading -----------------------------------------------------------
    def load(self):
        conn = self.conn
        self.btn_refresh.setEnabled(False)

        def done(d):
            self.btn_refresh.setEnabled(True)
            self.data = d
            self.fill()

        def fail(m):
            self.btn_refresh.setEnabled(True)
            self.title.setText("Could not read the device")
            self.status.setText(m)
        self.Task.start(lambda: gather(conn), done, fail)

    def fill(self):
        d = self.data
        p = d["props"]
        name = p.get("ro.vendor.oplus.market.name") or p.get("ro.product.marketname") or p.get("ro.product.model")
        self.title.setText(f"{name}   ·   {p.get('ro.product.model', '')}   ·   Android {p.get('ro.build.version.release')}")
        # hardware
        rows = hardware_rows(d)
        t = self.hw
        t.setRowCount(len(rows))
        last = None
        bold = QFont()
        bold.setBold(True)
        for i, (sec, k, v) in enumerate(rows):
            a = QTableWidgetItem(sec if sec != last else "")
            a.setFont(bold)
            a.setForeground(QColor(theme.ACCENT))
            last = sec
            b = QTableWidgetItem(k)
            b.setForeground(QColor(theme.TEXT_DIM))
            c = QTableWidgetItem(v)
            c.setToolTip(v)
            for j, it in enumerate((a, b, c)):
                t.setItem(i, j, it)
        # security
        srows = security_rows(d)
        t = self.sec
        t.setRowCount(len(srows))
        colors = {"ok": theme.ACCENT, "warn": "#e0a84f", "bad": "#ff6b6b", "info": theme.TEXT}
        for i, (k, v, st, note) in enumerate(srows):
            a = QTableWidgetItem(k)
            a.setForeground(QColor(theme.TEXT_DIM))
            b = QTableWidgetItem(("● " if st != "info" else "") + v)
            b.setForeground(QColor(colors.get(st, theme.TEXT)))
            f = b.font()
            f.setBold(st != "info")
            b.setFont(f)
            c = QTableWidgetItem(note)
            c.setForeground(QColor(theme.TEXT_DIM))
            c.setToolTip(note)
            for j, it in enumerate((a, b, c)):
                t.setItem(i, j, it)
        self.update_adb_state()
        self.update_usb_state()
        self.status.setText("Read-only information. Actions on the Reboot, ADB and USB tabs ask for confirmation.")

    def copy_report(self):
        if not self.data:
            return
        lines = [self.title.text(), ""]
        for sec, k, v in hardware_rows(self.data):
            lines.append(f"{sec:10} {k:24} {v}")
        lines.append("")
        for k, v, _st, note in security_rows(self.data):
            lines.append(f"{k:26} {v}" + (f"  ({note})" if note else ""))
        QApplication.clipboard().setText("\n".join(lines))
        self.status.setText("Report copied to the clipboard.")

    # ---- helpers ------------------------------------------------------------
    def confirm(self, title: str, text: str, danger=False) -> bool:
        box = QMessageBox(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question, title, text, parent=self)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def run(self, title: str, cmd: str, after=None, quiet=False):
        conn = self.conn

        def done(r):
            rc, out, err = r
            msg = (out + err).decode(errors="replace").strip()
            if rc != 0:
                QMessageBox.warning(self, title, f"{cmd}\n\n{msg or f'exit code {rc}'}")
            elif not quiet:
                self.status.setText(f"{title}: done" + (f" – {msg[:200]}" if msg else ""))
            if after:
                after()
        self.Task.start(lambda: conn.exec(cmd + " 2>&1", timeout=60), done,
                        lambda m: QMessageBox.warning(self, title, m))

    # ---- power ----------------------------------------------------------------
    def _power_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        note = QLabel("Rebooting ends this connection. Commands run as root; proprietary modes only work on the "
                      "matching hardware (vendor shown in brackets).")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        outer.addWidget(note)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        for i, (key, label, cmd, desc, level, vendor) in enumerate(REBOOT_MODES):
            b = QPushButton(theme.line_icon("refresh" if key not in ("poweroff",) else "disconnect"),
                            f" {label}" + (f"  [{vendor}]" if vendor else ""))
            b.setAutoDefault(False)
            b.setMinimumWidth(220)
            if level == 2:
                b.setStyleSheet("QPushButton{color:#ff8f8f;}")
            b.clicked.connect(lambda _c=False, k=key: self.reboot(k))
            dl = QLabel(desc)
            dl.setWordWrap(True)
            dl.setStyleSheet(f"color: {theme.TEXT_DIM};")
            grid.addWidget(b, i, 0)
            grid.addWidget(dl, i, 1)
        grid.setColumnStretch(1, 1)
        outer.addLayout(grid)
        outer.addStretch(1)
        return w

    def reboot(self, key: str):
        mode = next(m for m in REBOOT_MODES if m[0] == key)
        _k, label, cmd, desc, level, vendor = mode
        extra = f"\n\nThis is a {vendor}-specific mode." if vendor else ""
        if level == 2:
            extra += "\n\n⚠ Advanced mode – only continue if you know how to get out of it."
        if not self.confirm(f"{label}?", f"{desc}{extra}\n\nCommand: {cmd}", danger=level >= 1):
            return
        ends = key not in ("systemui",)
        conn = self.conn
        # detach so the command survives the SSH session ending
        full = f"nohup sh -c {q('sleep 1; ' + cmd)} >/dev/null 2>&1 &" if ends else cmd

        def done(_r):
            if ends:
                self.status.setText(f"{label}: command sent. Disconnecting…")
                QTimer.singleShot(1500, self.win.disconnect)
                QTimer.singleShot(1600, self.close)
            else:
                self.status.setText(f"{label}: done.")
        self.Task.start(lambda: conn.exec(full, timeout=30), done, lambda m: QMessageBox.warning(self, label, m))

    # ---- ADB ------------------------------------------------------------------
    def _build_adb_tab(self):
        lay = QVBoxLayout(self.adb_tab)
        self.adb_state = QLabel("…")
        self.adb_state.setWordWrap(True)
        f = self.adb_state.font()
        f.setPointSizeF(f.pointSizeF() + 1)
        self.adb_state.setFont(f)
        lay.addWidget(self.adb_state)
        g1 = QGroupBox("USB debugging")
        l1 = QHBoxLayout(g1)
        self.btn_adb_on = QPushButton(theme.line_icon("forward"), " Turn on")
        self.btn_adb_off = QPushButton(theme.line_icon("disconnect"), " Turn off")
        self.btn_adb_on.clicked.connect(lambda: self.set_adb(True))
        self.btn_adb_off.clicked.connect(lambda: self.set_adb(False))
        l1.addWidget(self.btn_adb_on)
        l1.addWidget(self.btn_adb_off)
        l1.addStretch(1)
        lay.addWidget(g1)
        g2 = QGroupBox("Wireless ADB (adb over TCP)")
        l2 = QHBoxLayout(g2)
        self.tcp_port = QLineEdit("5555")
        self.tcp_port.setMaximumWidth(90)
        self.btn_tcp_on = QPushButton(theme.line_icon("forward"), " Enable on port")
        self.btn_tcp_off = QPushButton(theme.line_icon("disconnect"), " Disable")
        self.btn_tcp_on.clicked.connect(lambda: self.set_tcp(True))
        self.btn_tcp_off.clicked.connect(lambda: self.set_tcp(False))
        l2.addWidget(self.btn_tcp_on)
        l2.addWidget(self.tcp_port)
        l2.addWidget(self.btn_tcp_off)
        l2.addStretch(1)
        lay.addWidget(g2)
        g3 = QGroupBox("Use ADB from this PC")
        l3 = QVBoxLayout(g3)
        hint = QLabel("Enables wireless ADB, then connects AndroUnderTheHood to the phone over ADB (adb connect). "
                      "The phone asks once to allow this PC's ADB key.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        l3.addWidget(hint)
        row = QHBoxLayout()
        self.btn_adb_connect = QPushButton(theme.line_icon("connect"), " Connect AndroUnderTheHood via ADB")
        self.btn_adb_connect.clicked.connect(self.connect_via_adb)
        self.btn_adb_mirror = QPushButton(theme.line_icon("phone"), " Screen mirror over ADB")
        self.btn_adb_mirror.clicked.connect(self.mirror_via_adb)
        row.addWidget(self.btn_adb_connect)
        row.addWidget(self.btn_adb_mirror)
        self.btn_openheart = QPushButton(theme.line_icon("download"), " Install AndroOpenHeart + Shizuku && connect via SSH")
        self.btn_openheart.setToolTip("Over this ADB connection: install/update the bundled AndroOpenHeart app, authorize "
                                    "this PC's key, grant its permissions, set up Shizuku, start it and switch AndroUnderTheHood to SSH")
        self.btn_openheart.clicked.connect(lambda: self.win.setup_openheart(self.conn.serial, self))
        self.btn_openheart.setVisible(bool(getattr(self.conn, "is_adb", False)))
        self.btn_openheart.setAutoDefault(False)
        row.addWidget(self.btn_openheart)
        row.addStretch(1)
        l3.addLayout(row)
        lay.addWidget(g3)
        lay.addStretch(1)
        for b in (self.btn_adb_on, self.btn_adb_off, self.btn_tcp_on, self.btn_tcp_off, self.btn_adb_connect,
                  self.btn_adb_mirror):
            b.setAutoDefault(False)

    def update_adb_state(self):
        if not self.data:
            return
        s, p = self.data["set"], self.data["props"]
        port = p.get("service.adb.tcp.port", "") or p.get("persist.adb.tcp.port", "")
        on = s.get("adb_enabled") == "1"
        tcp = port not in ("", "-1", "0")
        self.adb_state.setText(
            f"USB debugging: <b style='color:{theme.ACCENT if on else '#e0a84f'}'>{'ON' if on else 'OFF'}</b> · "
            f"adbd: {p.get('init.svc.adbd', '?')} · wireless ADB (TCP): "
            f"<b>{('port ' + port) if tcp else 'off'}</b> · Android wireless debugging: "
            f"{'on' if s.get('adb_wifi_enabled') == '1' else 'off'}")
        self.btn_adb_on.setEnabled(not on)
        self.btn_adb_off.setEnabled(on)
        self.btn_tcp_off.setEnabled(tcp)

    def set_adb(self, on: bool):
        if not on and getattr(self.conn, "is_adb", False):
            if not self.confirm("Turn off USB debugging", "AndroUnderTheHood is connected through ADB – turning it off "
                                "ends this connection. Continue?", danger=True):
                return
        cmd = "settings put global development_settings_enabled 1; " if on else ""
        cmd += f"settings put global adb_enabled {1 if on else 0}"
        self.run("USB debugging", cmd, after=self.load)

    def set_tcp(self, on: bool):
        port = self.tcp_port.text().strip() or "5555"
        if on and not port.isdigit():
            QMessageBox.warning(self, "Wireless ADB", "Enter a port number.")
            return
        if on and not self.confirm("Wireless ADB", f"Allow ADB connections over the network on port {port}?\n\n"
                                   "Anyone on the same network can request access (the phone still asks you to "
                                   "authorize new computers)."):
            return
        val = port if on else "-1"
        self.run("Wireless ADB", f"settings put global adb_enabled 1; setprop service.adb.tcp.port {val}; "
                 "stop adbd; start adbd", after=lambda: QTimer.singleShot(1500, self.load))

    def connect_via_adb(self):
        from . import adbconn
        port = self.tcp_port.text().strip() or "5555"
        host = self.conn.profile.host if not getattr(self.conn, "is_adb", False) else None
        if not host:
            QMessageBox.information(self, "ADB", "Already connected through ADB.")
            return
        conn = self.conn

        def work():
            conn.exec("settings put global adb_enabled 1; "
                      f"[ \"$(getprop service.adb.tcp.port)\" = {port} ] || {{ setprop service.adb.tcp.port {port}; "
                      "stop adbd; start adbd; sleep 2; }", timeout=30)
            return adbconn.adb_connect(f"{host}:{port}")

        def done(r):
            ok, msg = r
            if not ok:
                QMessageBox.warning(self, "ADB connect", msg + "\n\nIf the phone shows “Allow USB debugging?”, accept "
                                    "it and try again.")
                return
            self.win.start_adb_connection(f"{host}:{port}", self.conn.profile.name + " (ADB)")
            self.close()
        self.status.setText("Enabling wireless ADB and connecting… accept the prompt on the phone if it appears.")
        self.Task.start(work, done, lambda m: QMessageBox.warning(self, "ADB connect", m))

    def mirror_via_adb(self):
        from . import adbconn
        port = self.tcp_port.text().strip() or "5555"
        if getattr(self.conn, "is_adb", False):
            self.win.start_mirror()
            return
        host = self.conn.profile.host
        conn = self.conn

        def work():
            conn.exec("settings put global adb_enabled 1; "
                      f"[ \"$(getprop service.adb.tcp.port)\" = {port} ] || {{ setprop service.adb.tcp.port {port}; "
                      "stop adbd; start adbd; sleep 2; }", timeout=30)
            return adbconn.adb_connect(f"{host}:{port}")

        def done(r):
            ok, msg = r
            if not ok:
                QMessageBox.warning(self, "ADB", msg)
                return
            self.win.start_mirror(adb_serial=f"{host}:{port}")
        self.Task.start(work, done, lambda m: QMessageBox.warning(self, "ADB", m))

    # ---- USB ------------------------------------------------------------------
    def _build_usb_tab(self):
        lay = QVBoxLayout(self.usb_tab)
        self.usb_state = QLabel("…")
        self.usb_state.setWordWrap(True)
        f = self.usb_state.font()
        f.setPointSizeF(f.pointSizeF() + 1)
        self.usb_state.setFont(f)
        lay.addWidget(self.usb_state)
        warn = QLabel("If you are connected over USB/ADB, a mode without “adb” ends that connection. Modes last until "
                      "the cable is re-plugged or the phone reboots (Android may switch back on its own).")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(warn)
        g1 = QGroupBox("Standard modes")
        l1 = QGridLayout(g1)
        std = [("Charging only", "none"), ("File transfer (MTP)", "mtp"), ("Photo transfer (PTP)", "ptp"),
               ("USB tethering (RNDIS)", "rndis"), ("MIDI", "midi"), ("USB Ethernet (NCM)", "ncm")]
        for i, (label, fn) in enumerate(std):
            b = QPushButton(label)
            b.setAutoDefault(False)
            if fn == "none":
                b.setToolTip("Requests “no data transfer”. Some vendors substitute their own default (OnePlus "
                             "uses MIDI): files and ADB are still blocked. The result is checked after switching.")
            b.clicked.connect(lambda _c=False, fn=fn, label=label: self.set_usb_std(fn, label))
            l1.addWidget(b, i // 3, i % 3)
        lay.addWidget(g1)
        g2 = QGroupBox("All modes supported by this phone (sys.usb.config)")
        l2 = QVBoxLayout(g2)
        self.usb_filter = QLineEdit()
        self.usb_filter.setPlaceholderText("Filter… e.g. diag, rmnet, uvc")
        self.usb_filter.textChanged.connect(self.fill_usb_list)
        l2.addWidget(self.usb_filter)
        self.usb_list = QListWidget()
        self.usb_list.itemDoubleClicked.connect(lambda it: self.set_usb_cfg(it.data(Qt.ItemDataRole.UserRole)))
        l2.addWidget(self.usb_list, 1)
        row = QHBoxLayout()
        self.usb_custom = QLineEdit()
        self.usb_custom.setPlaceholderText("custom, e.g. diag,serial_cdev,rmnet,adb")
        b_apply = QPushButton(theme.line_icon("forward"), " Apply selected / custom")
        b_apply.setAutoDefault(False)
        b_apply.clicked.connect(self.apply_usb_selected)
        row.addWidget(self.usb_custom, 1)
        row.addWidget(b_apply)
        l2.addLayout(row)
        lay.addWidget(g2, 1)
        g3 = QGroupBox("Port role (Type-C)")
        l3 = QHBoxLayout(g3)
        self.role_state = QLabel("")
        self.role_state.setWordWrap(True)
        l3.addWidget(self.role_state, 1)
        self.role_box = QHBoxLayout()   # buttons built from the roles the phone reports
        l3.addLayout(self.role_box)
        self.otg_box = QCheckBox("OnePlus OTG switch")
        self.otg_box.setToolTip("Settings → OTG connection (turns itself off after 10 minutes unused)")
        self.otg_box.clicked.connect(lambda on: self.run("OTG", f"settings put global persist.sys.oplus.otg_support "
                                                          f"{1 if on else 0}", after=self.load))
        l3.addWidget(self.otg_box)
        lay.addWidget(g3)

    def update_usb_state(self):
        if not self.data:
            return
        p, s = self.data["props"], self.data["set"]
        cfg = p.get("sys.usb.config", "?")
        state = p.get("sys.usb.state", "?")
        self.usb_state.setText(f"Current: <b>{cfg}</b>  ({describe_usb(cfg)}) · state: {state} · framework: "
                               f"{s.get('usbfn', '?')}")
        self.fill_usb_list()
        port = parse_ports(self.data)
        self.port_id = port["id"]
        while self.role_box.count():
            w = self.role_box.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not port["id"]:
            self.role_state.setText("Type-C role info not available")
        else:
            can = port["can_power"] or port["can_data"]
            why = "" if can else ("  –  the connected partner does not allow role changes (e.g. a PC is always the "
                                  "host); unplug it or use an OTG adapter/accessory")
            self.role_state.setText(f"{port['id']}: power {port['power']}, data {port['data']}{why}")
            for pr, dr in port["combos"]:
                label, tip = ROLE_LABELS.get((pr, dr), (f"{pr} / {dr}", ""))
                b = QPushButton(label)
                b.setAutoDefault(False)
                current = (pr, dr) == (port["power"], port["data"])
                b.setEnabled(can and not current)
                b.setToolTip(f"power {pr}, data {dr}: {tip}" + (" (current)" if current else "")
                             + ("" if can else " – not changeable with this partner"))
                b.clicked.connect(lambda _c=False, pr=pr, dr=dr: self.set_role(pr, dr))
                self.role_box.addWidget(b)
        otg = s.get("otg", "")
        self.otg_box.setVisible(otg not in ("", "null"))
        self.otg_box.setChecked(otg == "1")

    def fill_usb_list(self, *_):
        if not self.data:
            return
        f = self.usb_filter.text().lower().split()
        cur = self.data["props"].get("sys.usb.config", "")
        self.usb_list.clear()
        for cfg in usb_configs(self.data):
            text = f"{cfg}    —    {describe_usb(cfg)}"
            if f and not all(w in text.lower() for w in f):
                continue
            sdk = int(self.data["props"].get("ro.build.version.sdk", "0") or 0)
            needs14 = "uvc" in cfg.split(",") and sdk < 34
            if needs14:
                text += "    (webcam needs Android 14+ – the phone falls back)"
            it = QListWidgetItem(("● " if cfg == cur else "   ") + text)
            it.setData(Qt.ItemDataRole.UserRole, cfg)
            if needs14:
                it.setForeground(QColor(theme.TEXT_FAINT))
            elif cfg == cur:
                it.setForeground(QColor(theme.ACCENT))
            elif "diag" in cfg or "rmnet" in cfg or "qdss" in cfg:
                it.setForeground(QColor("#e0a84f"))
            self.usb_list.addItem(it)

    def _usb_warn(self, funcs: str) -> bool:
        if "adb" not in funcs.split(",") and getattr(self.conn, "is_adb", False):
            return self.confirm("USB mode", f"'{funcs}' has no ADB – this ends AndroUnderTheHood's ADB connection. "
                                "Continue?", danger=True)
        return True

    def set_usb_std(self, fn: str, label: str):
        if not self._usb_warn(fn + ",adb"):
            return
        if not self.confirm("USB mode", f"Switch USB to “{label}”?"):
            return
        self.run("USB mode", f"svc usb setFunctions {'' if fn == 'none' else fn}".strip(),
                 after=lambda: QTimer.singleShot(3000, lambda: self.verify_usb(label, {fn})), quiet=True)

    def apply_usb_selected(self):
        it = self.usb_list.currentItem()
        cfg = self.usb_custom.text().strip() or (it.data(Qt.ItemDataRole.UserRole) if it else "")
        if cfg:
            self.set_usb_cfg(cfg)

    def set_usb_cfg(self, cfg: str):
        if not re.fullmatch(r"[a-z0-9_,]+", cfg):
            QMessageBox.warning(self, "USB mode", "A USB configuration is a comma-separated list like mtp,adb.")
            return
        if not self._usb_warn(cfg):
            return
        diag = any(x in cfg for x in ("diag", "rmnet", "qdss", "serial_cdev", "dpl"))
        txt = (f"Set USB configuration to\n\n{cfg}\n({describe_usb(cfg)})?" +
               ("\n\nDiagnostic/modem ports expose low-level baseband interfaces (QPST/QXDM). Use them only on your "
                "own device." if diag else ""))
        if not self.confirm("USB mode", txt, danger=diag):
            return
        self.run("USB mode", f"setprop sys.usb.config {q(cfg)}",
                 after=lambda: QTimer.singleShot(3500, lambda: self.verify_usb(cfg, set(cfg.split(",")))), quiet=True)

    def verify_usb(self, label: str, wanted: set):
        """Re-read the phone and report what it actually switched to (vendors may substitute modes)."""
        conn = self.conn

        def done(d):
            self.data = d
            self.fill()
            state = d["props"].get("sys.usb.state", "")
            got = set(state.split(",")) - {""}
            ok = (wanted - {"adb"}) <= got and (got - {"adb"}) <= (wanted | {"none"})
            if ok:
                self.status.setText(f"✔ USB is now {state}  ({describe_usb(state)})")
            else:
                self.status.setText(f"⚠ Requested “{label}” but the phone switched to {state or '?'} "
                                    f"({describe_usb(state)}) – vendor policy or unsupported on this phone.")
        self.Task.start(lambda: gather(conn), done, lambda m: self.status.setText(f"Could not verify: {m}"))

    def set_role(self, power: str, data: str):
        port = getattr(self, "port_id", None) or "port0"
        if not self.confirm("USB port role", f"Set {port} to power role “{power}” and data role “{data}”?\n\n"
                            "Only works while a cable/partner is attached, and only if the hardware allows it."):
            return
        self.run("Port role", f"dumpsys usb set-port-roles {q(port)} {power} {data}",
                 after=lambda: QTimer.singleShot(2500, self.load))
