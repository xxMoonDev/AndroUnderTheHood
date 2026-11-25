"""Storage health: eMMC / UFS wear indicators, identity, lifetime writes, and a speed test.

eMMC  (/sys/class/mmc_host/*/mmc*:*): life_time = "A B" (two NAND types), pre_eol_info.
UFS   (health_descriptor): life_time_estimation_a / _b, eol_info.
Both use the JEDEC scale: 0x01 = 0-10 % of the rated lifetime used ... 0x0A = 90-100 %, 0x0B = exceeded.
Pre-EOL (reserved blocks): 0x01 normal, 0x02 warning (80 % used), 0x03 urgent.
"""
from __future__ import annotations

import re

MAKERS = {0x15: "Samsung", 0xEC: "Samsung", 0xCE: "Samsung", 0x13: "Micron", 0x2C: "Micron", 0x45: "SanDisk / WD",
          0x90: "SK hynix", 0xAD: "SK hynix", 0x11: "Kioxia (Toshiba)", 0x98: "Kioxia (Toshiba)", 0x70: "Kingston",
          0xD6: "Foresee (Longsys)", 0x9B: "YMTC", 0x88: "Foresee", 0xFE: "Micron", 0x1B: "Samsung"}
EMMC_REV = {0: "4.0", 1: "4.1", 2: "4.2", 3: "4.3", 5: "4.41", 6: "4.5", 7: "5.0", 8: "5.1"}

READ = r"""
for d in /sys/class/mmc_host/mmc*/mmc*:*; do
  [ "$(cat $d/type 2>/dev/null)" = MMC ] || continue
  echo "@EMMC $d"
  for f in name manfid oemid date rev prv fwrev life_time pre_eol_info; do
    [ -r $d/$f ] && echo "$f=$(cat $d/$f)"
  done
  for b in $d/block/mmcblk?; do [ -d "$b" ] && echo "size=$(( $(cat $b/size) * 512 ))"; done   # not boot0/1/rpmb
done
for h in $(find /sys/devices /sys/bus/platform/devices -maxdepth 5 -type d -name health_descriptor 2>/dev/null | sort -u); do
  u=${h%/health_descriptor}
  echo "@UFS $u"
  for f in life_time_estimation_a life_time_estimation_b eol_info; do [ -r $h/$f ] && echo "$f=$(cat $h/$f)"; done
  for f in manufacturer_id specification_version; do [ -r $u/device_descriptor/$f ] && echo "$f=$(cat $u/device_descriptor/$f)"; done
  for f in product_name manufacturer_name product_revision; do [ -r $u/string_descriptors/$f ] && echo "$f=$(cat $u/string_descriptors/$f)"; done
done
for s in /sys/block/sd?; do [ -e $s/device/model ] && echo "@LUN ${s##*/} $(( $(cat $s/size) * 512 )) $(cat $s/device/vendor) $(cat $s/device/model) $(cat $s/device/rev 2>/dev/null)"; done
dev=$(awk '$2=="/data"{print $1}' /proc/mounts | tail -n1); dev=$(readlink -f $dev 2>/dev/null); dev=${dev##*/}
fs=$(awk '$2=="/data"{print $3}' /proc/mounts | tail -n1)
echo "@DATA $fs $dev $(cat /sys/fs/$fs/$dev/lifetime_write_kbytes 2>/dev/null)"
df -k /data | tail -n1 | awk '{print "@DF", $2, $3, $4}'
"""


def jedec_life(v: str) -> str:
    try:
        n = int(v, 16)
    except ValueError:
        return v
    if n == 0:
        return "not reported"
    if n == 0x0B:
        return "EXCEEDED its rated lifetime"
    if 1 <= n <= 10:
        return f"{(n - 1) * 10}–{n * 10} % of rated lifetime used"
    return v


def pre_eol(v: str) -> tuple[str, str]:
    try:
        n = int(v, 16)
    except ValueError:
        return v, "unknown"
    return {1: ("normal", "ok"), 2: ("WARNING – 80 % of reserve blocks used", "warn"),
            3: ("URGENT – reserve blocks almost gone", "bad")}.get(n, ("not reported", "unknown"))


def read(conn) -> dict:
    rc, out, err = conn.exec(READ, timeout=60, as_root=True)
    text = out.decode("utf-8", "replace")
    chips, cur, luns = [], None, []
    info = {"chips": chips, "luns": luns}
    for ln in text.splitlines():
        if ln.startswith(("@EMMC ", "@UFS ")):
            kind, path = ln[1:].split(" ", 1)
            cur = {"kind": "eMMC" if kind == "EMMC" else "UFS", "path": path}
            chips.append(cur)
        elif ln.startswith("@LUN "):
            p = ln.split()
            luns.append({"dev": p[1], "size": int(p[2]), "vendor": p[3], "model": " ".join(p[4:-1]) or p[4],
                         "rev": p[-1]})
        elif ln.startswith("@DATA "):
            p = ln.split()
            info["data_fs"] = p[1] if len(p) > 1 else ""
            info["lifetime_kb"] = int(p[3]) if len(p) > 3 and p[3].isdigit() else None
        elif ln.startswith("@DF "):
            p = ln.split()
            info["data_total"], info["data_used"], info["data_free"] = (int(x) * 1024 for x in p[1:4])
        elif cur is not None and "=" in ln:
            k, v = ln.split("=", 1)
            cur[k.strip()] = v.strip()
    for c in chips:
        c["report"] = describe(c, luns)
    return info


def describe(c: dict, luns: list[dict]) -> list[tuple[str, str, str]]:
    """[(label, value, level)] level: ok | warn | bad | info"""
    out = []
    if c["kind"] == "eMMC":
        mf = int(c.get("manfid", "0"), 16) if c.get("manfid") else 0
        out.append(("Chip", f"{MAKERS.get(mf, f'maker 0x{mf:02x}')} {c.get('name', '')}".strip(), "info"))
        try:
            rev = int(c.get("rev", "0"), 16)
            out.append(("Standard", f"eMMC {EMMC_REV.get(rev, f'rev {rev}')}", "info"))
        except ValueError:
            pass
        if c.get("size"):
            out.append(("Size", f"{int(c['size']) / 1e9:.1f} GB", "info"))
        if c.get("date"):
            out.append(("Made", c["date"], "info"))
        if c.get("fwrev"):
            out.append(("Firmware", c["fwrev"], "info"))
        lt = c.get("life_time", "").split()
        for name, v in zip(("Wear (type A NAND)", "Wear (type B NAND)"), lt):
            out.append((name, jedec_life(v), level_of(v)))
        if c.get("pre_eol_info"):
            txt, lvl = pre_eol(c["pre_eol_info"])
            out.append(("Reserve blocks", txt, lvl))
    else:
        name = " ".join(x for x in (c.get("manufacturer_name"), c.get("product_name")) if x)
        if not name and luns:
            name = f"{luns[0]['vendor']} {luns[0]['model']}"
        out.append(("Chip", name or "UFS", "info"))
        sv = c.get("specification_version", "")
        if sv:
            try:
                n = int(sv, 16)
                out.append(("Standard", f"UFS {n >> 8}.{(n >> 4) & 0xF}", "info"))
            except ValueError:
                pass
        if luns:
            out.append(("Size", f"{sum(l['size'] for l in luns) / 1e9:.1f} GB in {len(luns)} LUNs", "info"))
        for name, key in (("Wear (type A)", "life_time_estimation_a"), ("Wear (type B)", "life_time_estimation_b")):
            if c.get(key):
                out.append((name, jedec_life(c[key]), level_of(c[key])))
        if c.get("eol_info"):
            txt, lvl = pre_eol(c["eol_info"])
            out.append(("Reserve blocks", txt, lvl))
    return out


def level_of(v: str) -> str:
    try:
        n = int(v, 16)
    except ValueError:
        return "info"
    return "bad" if n >= 0x0A else "warn" if n >= 0x08 else "ok" if n else "info"


SPEED = r"""
F=/data/local/tmp/.auth-speedtest
need=$(( {mb} * 2 * 1024 ))
free=$(df -k /data | tail -n1 | awk '{{print $4}}')
[ "$free" -gt "$need" ] || {{ echo "@ERR not enough free space"; exit 1; }}
sync
t0=$(date +%s%N); dd if=/dev/zero of=$F bs=1048576 count={mb} conv=fsync 2>/dev/null; t1=$(date +%s%N)
sync; echo 3 > /proc/sys/vm/drop_caches
t2=$(date +%s%N); dd if=$F of=/dev/null bs=1048576 2>/dev/null; t3=$(date +%s%N)
rm -f $F
echo "@W $(( t1 - t0 ))"; echo "@R $(( t3 - t2 ))"
"""


def speed_test(conn, mb: int = 256) -> dict:
    """Sequential write (fsync'd) and read (after dropping the page cache) of a temp file in /data."""
    rc, out, err = conn.exec(SPEED.format(mb=int(mb)), timeout=600, as_root=True)
    text = (out + err).decode("utf-8", "replace")
    if "@ERR" in text:
        raise RuntimeError(text.split("@ERR", 1)[1].strip())
    w = re.search(r"@W (\d+)", text)
    r = re.search(r"@R (\d+)", text)
    if not (w and r):
        raise RuntimeError("speed test failed: " + text.strip()[-200:])
    ws, rs = int(w.group(1)) / 1e9, int(r.group(1)) / 1e9
    return {"mb": mb, "write_s": ws, "read_s": rs, "write_mbs": mb / ws if ws else 0, "read_mbs": mb / rs if rs else 0}
