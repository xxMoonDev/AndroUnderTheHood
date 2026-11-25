"""Boot image tool: list boot-chain partitions per slot, inspect (magiskboot), save to the PC,
patch with Magisk's own boot_patch.sh, compare slots. Flashing reuses the app's partitions
module (automatic backup, upload checksum, dd + fsync, read-back verify).

All on-device work happens in a private temp folder; partitions are only ever READ here.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

from androunderthehood import partitions as P

BOOT_BASES = ["boot", "init_boot", "vendor_boot", "vendor_kernel_boot", "recovery", "dtbo", "vbmeta",
              "vbmeta_system", "vbmeta_vendor"]
MAGISK = "/data/adb/magisk"
WORK = "/data/local/tmp/.rwb"


def q(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def run(conn, cmd: str, timeout: float = 300, stdin: bytes | None = None) -> tuple[int, str]:
    rc, out, err = conn.exec(cmd, timeout=timeout, stdin=stdin, as_root=True)
    return rc, (out + err).decode("utf-8", "replace")


def boot_parts(conn) -> tuple[list[P.Partition], str]:
    parts, slot = P.discover(conn)
    keep = [p for p in parts if p.base in BOOT_BASES and p.kind == "physical"]
    keep.sort(key=lambda p: (BOOT_BASES.index(p.base), p.slot))
    return keep, slot


def magisk_target(conn, parts: list[P.Partition], slot: str) -> str:
    """Magisk patches init_boot when it holds a real boot image with a ramdisk (devices launched
    with Android 13+), otherwise boot. Some devices have an EMPTY init_boot (the onn tablet), so
    the partition's mere existence is not enough."""
    ib = next((p for p in parts if p.base == "init_boot" and p.slot in (slot, "")), None)
    if ib:
        try:
            if "ramdisk" in inspect(conn, ib).get("kind", ""):
                return "init_boot"
        except Exception:  # noqa: BLE001
            pass
    return "boot"


INSPECT = r"""
W={work}; rm -rf $W; mkdir -p $W && cd $W || exit 1
dd if={link} of=img bs=1048576 2>/dev/null || {{ echo "@ERR read failed"; exit 1; }}
echo "@SHA $(sha256sum img | cut -d' ' -f1)"
echo "@MAGIC $(head -c 8 img | od -An -tx1 | tr -d ' \n')"
if [ "$(head -c 4 img)" = "AVB0" ]; then
  # vbmeta flags: u32 big-endian at offset 120 (1 = verity off, 2 = verification off)
  echo "@AVBFLAGS $(dd if=img bs=1 skip=120 count=4 2>/dev/null | od -An -tu1 | tr -s ' ' | sed 's/^ //')"
  exit 0
fi
{magisk}/magiskboot unpack -h img > unpack.txt 2>&1; echo "@UNPACK $?"
sed 's/^/@H /' unpack.txt
if [ -f ramdisk.cpio ]; then
  {magisk}/magiskboot cpio ramdisk.cpio test >/dev/null 2>&1; echo "@CPIOTEST $?"
  {magisk}/magiskboot cpio ramdisk.cpio "exists .backup/.magisk" >/dev/null 2>&1 && echo "@MAGISKCFG"
  echo "@RAMDISK $({magisk}/magiskboot cpio ramdisk.cpio ls 2>/dev/null | wc -l) entries"
fi
if [ -f kernel ]; then
  {magisk}/magiskboot decompress kernel kernel.raw >/dev/null 2>&1 || cp kernel kernel.raw
  echo "@KVER $(grep -a -m1 -o 'Linux version [^ ]* ([^)]*)' kernel.raw | head -n1)"
fi
cd /; rm -rf $W
"""


def inspect(conn, p: P.Partition) -> dict:
    rc, text = run(conn, INSPECT.format(work=WORK, link=q(p.link), magisk=MAGISK), timeout=300)
    info = {"partition": p.name, "size": p.size, "header": {}, "lines": []}
    for ln in text.splitlines():
        if ln.startswith("@SHA "):
            info["sha256"] = ln[5:].strip()
        elif ln.startswith("@MAGIC "):
            info["magic"] = ln[7:].strip()
        elif ln.startswith("@AVBFLAGS "):
            b = [int(x) for x in ln.split()[1:] if x.isdigit()]
            flags = int.from_bytes(bytes(b), "big") if len(b) == 4 else None
            info["avb_flags"] = flags
        elif ln.startswith("@UNPACK "):
            info["unpack_rc"] = int(ln.split()[1])
        elif ln.startswith("@H "):
            h = ln[3:]
            info["lines"].append(h)
            m = re.match(r"\s*([A-Z_]+)\s*\[(.*)\]", h)
            if m:
                info["header"][m.group(1)] = m.group(2)
        elif ln.startswith("@CPIOTEST "):
            info["cpio_test"] = int(ln.split()[1])
        elif ln.startswith("@MAGISKCFG"):
            info["magisk_backup"] = True
        elif ln.startswith("@RAMDISK "):
            info["ramdisk"] = ln[9:].strip()
        elif ln.startswith("@KVER "):
            info["kernel"] = ln[6:].strip()
        elif ln.startswith("@ERR "):
            raise RuntimeError(ln[5:])
    info["kind"] = describe(info)
    return info


def describe(info: dict) -> str:
    magic = info.get("magic", "")
    if magic.startswith("41564230"):          # AVB0
        f = info.get("avb_flags")
        if f is None:
            return "vbmeta"
        state = ("verity + verification ON" if f == 0 else
                 " + ".join(s for s, bit in (("verity OFF", 1), ("verification OFF", 2)) if f & bit))
        return f"vbmeta ({state})"
    if magic.startswith("d7b7ab1e"):
        return "dtbo (device-tree overlays)"
    if magic.startswith("564e4452424f4f54"):        # VNDRBOOT
        return "vendor_boot image (vendor ramdisk + DTB)"
    if magic and set(magic) == {"0"}:
        return "empty (all zeros) – not used on this device"
    t = info.get("cpio_test")
    if t is None:
        return "no ramdisk" if info.get("unpack_rc") == 0 else "not a boot image"
    return {0: "stock ramdisk", 1: "Magisk-patched ramdisk", 2: "ramdisk patched by another tool"}.get(t, f"cpio test {t}")


def pc_dir(device_name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', "_", device_name or "device")
    d = os.path.join(os.path.expanduser("~"), "Documents", "AndroUnderTheHood", "Boot images", safe)
    os.makedirs(d, exist_ok=True)
    return d


def save_to_pc(conn, p: P.Partition, device_name: str, progress) -> str:
    """Partition -> PC (verified with sha256 on both ends) + an info .json next to it."""
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(pc_dir(device_name), f"{p.name}-{stamp}.img")
    P.backup_partition(conn, p, path, progress, verify=True)
    try:
        info = inspect(conn, p)
        with open(path + ".json", "w", encoding="utf-8") as f:
            json.dump(info, f, indent=1)
    except Exception:  # noqa: BLE001 - the image itself is saved; the info is a bonus
        pass
    return path


PATCH = r"""
W={work}; rm -rf $W; mkdir -p $W && cd $W || exit 1
cp -a {magisk}/. $W/ || exit 1
dd if={link} of=boot.img bs=1048576 2>/dev/null || {{ echo "read failed"; exit 1; }}
. ./util_functions.sh >/dev/null 2>&1
get_flags >/dev/null 2>&1
export KEEPVERITY KEEPFORCEENCRYPT PATCHVBMETAFLAG RECOVERYMODE LEGACYSAR
echo "flags: KEEPVERITY=$KEEPVERITY KEEPFORCEENCRYPT=$KEEPFORCEENCRYPT PATCHVBMETAFLAG=$PATCHVBMETAFLAG RECOVERYMODE=$RECOVERYMODE"
sh ./boot_patch.sh boot.img 2>&1
[ -f new-boot.img ] || {{ echo "@FAIL no new-boot.img"; exit 1; }}
mv new-boot.img {out}; chmod 644 {out}
echo "@OUT $(stat -c %s {out}) $(sha256sum {out} | cut -d' ' -f1)"
cd /; rm -rf $W
"""


def patch(conn, fs, p: P.Partition, device_name: str, log, progress) -> str:
    """Reads the partition into a temp folder on the device, runs Magisk's own boot_patch.sh there
    (nothing is written to any partition), and downloads the patched image to the PC."""
    out = f"/data/local/tmp/.rwb-{p.name}-patched.img"
    rc, text = run(conn, PATCH.format(work=WORK, magisk=MAGISK, link=q(p.link), out=out), timeout=600)
    log(text if text.endswith("\n") else text + "\n")
    m = re.search(r"@OUT (\d+) ([0-9a-f]{64})", text)
    if rc != 0 or not m:
        run(conn, f"rm -rf {WORK} {out}")
        raise RuntimeError("Magisk's boot_patch.sh failed – see the log")
    size, dev_sha = int(m.group(1)), m.group(2)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    local = os.path.join(pc_dir(device_name), f"{p.name}-magisk_patched-{stamp}.img")
    try:
        fs.download(out, local, progress)
    finally:
        run(conn, f"rm -f {out}")
    sha = P.sha256_file(local)
    if os.path.getsize(local) != size or sha != dev_sha:
        os.remove(local)
        raise RuntimeError("Download verification failed for the patched image")
    with open(local + ".sha256", "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{sha}  {os.path.basename(local)}\n")
    return local


def compare_slots(conn, parts: list[P.Partition]) -> list[tuple[str, str, str, bool]]:
    """[(base, sha_a, sha_b, same)] for every slotted boot-chain partition."""
    by = {}
    for p in parts:
        if p.slot:
            by.setdefault(p.base, {})[p.slot] = p
    cmd = "; ".join(f"echo {p.name} $(sha256sum {q(p.link)} | cut -d' ' -f1)"
                    for d in by.values() for p in d.values())
    rc, text = run(conn, cmd, timeout=600)
    sums = dict(ln.split()[:2] for ln in text.splitlines() if len(ln.split()) >= 2)
    rows = []
    for base, d in by.items():
        a = sums.get(d["a"].name, "") if "a" in d else ""
        b = sums.get(d["b"].name, "") if "b" in d else ""
        rows.append((base, a, b, bool(a) and a == b))
    return rows
