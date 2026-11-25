"""App data snapshots: save one app's private data to the PC and put it back later.

Saved:    /data/data/<pkg>, /data/user_de/0/<pkg> and (optional) Android/data/<pkg> on internal storage,
          as one .tar.gz + .json (version, uid, sizes, sha256) in Documents\\AndroUnderTheHood\\App snapshots.
Restore:  the app is stopped, its folders are emptied and the snapshot unpacked; then owners are set for
          the uid the app has NOW (it may differ after a reinstall) and the SELinux labels are copied from the
          folders as they were (chcon - never `restorecon -RF`, which strips the per-app MLS categories).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re

TMP = "/data/local/tmp/.auth-snap.tgz"
EXT = "/data/media/0/Android/data"


def q(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def run(conn, cmd, timeout=600):
    rc, out, err = conn.exec(cmd, timeout=timeout, as_root=True)
    return rc, (out + err).decode("utf-8", "replace")


def folder(device_name: str, pkg: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', "_", device_name or "device")
    d = os.path.join(os.path.expanduser("~"), "Documents", "AndroUnderTheHood", "App snapshots", safe, pkg)
    os.makedirs(d, exist_ok=True)
    return d


def list_snapshots(device_name: str, pkg: str) -> list[dict]:
    d = folder(device_name, pkg)
    out = []
    for n in sorted(os.listdir(d), reverse=True):
        if n.endswith(".json"):
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    m = json.load(f)
                m["path"] = os.path.join(d, n[:-5] + ".tar.gz")
                if os.path.isfile(m["path"]):
                    out.append(m)
            except (OSError, ValueError):
                pass
    return out


def app_info(conn, pkg: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.]+", pkg):
        raise ValueError("bad package name")
    rc, text = run(conn, f"stat -c '%u %C' /data/data/{pkg} 2>/dev/null; "
                         f"dumpsys package {pkg} | grep -m1 -oE 'versionName=[^ ]+'; "
                         f"dumpsys package {pkg} | grep -m1 -oE 'versionCode=[0-9]+'; "
                         f"for d in /data/data/{pkg} /data/user_de/0/{pkg} {EXT}/{pkg}; do "
                         f"[ -d $d ] && echo \"DIR $d $(du -sk $d | cut -f1)\"; done", timeout=60)
    lines = text.splitlines()
    if not lines or not lines[0][:1].isdigit():
        raise RuntimeError(f"{pkg} has no data folder (not installed?)")
    uid, ctx = lines[0].split(" ", 1)
    info = {"pkg": pkg, "uid": int(uid), "context": ctx.strip(), "dirs": {}}
    for ln in lines[1:]:
        if ln.startswith("versionName="):
            info["versionName"] = ln.split("=", 1)[1]
        elif ln.startswith("versionCode="):
            info["versionCode"] = ln.split("=", 1)[1]
        elif ln.startswith("DIR "):
            _, d, kb = ln.split()
            info["dirs"][d] = int(kb) * 1024
    return info


def take(conn, fs, pkg: str, device_name: str, external: bool, log, progress) -> dict:
    info = app_info(conn, pkg)
    dirs = [d for d in info["dirs"] if external or not d.startswith(EXT)]
    rel = " ".join(q(d.lstrip("/")) for d in dirs)
    log(f"stopping {pkg} and packing {', '.join(dirs)}\n")
    rc, text = run(conn, f"am force-stop {pkg}; rm -f {TMP}; tar -czf {TMP} -C / {rel} 2>&1; r=$?; "
                         f"chmod 600 {TMP}; echo \"@SIZE $(stat -c %s {TMP})\"; "
                         f"echo \"@SHA $(sha256sum {TMP} | cut -d' ' -f1)\"; exit $r", timeout=1800)
    m_size, m_sha = re.search(r"@SIZE (\d+)", text), re.search(r"@SHA ([0-9a-f]{64})", text)
    if rc != 0 or not (m_size and m_sha):
        run(conn, f"rm -f {TMP}")
        raise RuntimeError("packing failed: " + text.strip()[-300:])
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(folder(device_name, pkg), f"{info.get('versionName', 'v')}-{stamp}")
    try:
        fs.download(TMP, base + ".tar.gz", progress)
    finally:
        run(conn, f"rm -f {TMP}")
    h = hashlib.sha256()
    with open(base + ".tar.gz", "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    if h.hexdigest() != m_sha.group(1):
        os.remove(base + ".tar.gz")
        raise RuntimeError("download checksum mismatch")
    meta = dict(info, dirs=dirs, sizes={d: info["dirs"][d] for d in dirs}, sha256=m_sha.group(1),
                archive_size=int(m_size.group(1)), created=stamp, device=device_name)
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    meta["path"] = base + ".tar.gz"
    return meta


def restore(conn, fs, meta: dict, log, progress) -> str:
    pkg = meta["pkg"]
    now = app_info(conn, pkg)                 # owner + labels of the app as installed NOW
    uid = now["uid"]
    app_id = uid % 100000
    cache_gid = 20000 + app_id                # Android: cache/code_cache group = AID_CACHE_GID_START + appId
    log(f"uploading {os.path.basename(meta['path'])}\n")
    fs.upload(meta["path"], TMP, progress)
    rc, text = run(conn, f"sha256sum {TMP} | cut -d' ' -f1")
    if text.strip() != meta["sha256"]:
        run(conn, f"rm -f {TMP}")
        raise RuntimeError("upload checksum mismatch – nothing was changed")
    script = [f"am force-stop {pkg}"]
    for d in meta["dirs"]:
        ext = d.startswith(EXT)
        # remember the folder's current label (per-app MLS categories), then empty it
        script += [f"mkdir -p {q(d)}",
                   f"ctx=$(stat -c %C {q(d)})",
                   f"find {q(d)} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +",
                   f"tar -xzf {TMP} -C / {q(d.lstrip('/'))}",
                   f"chown -R {uid}:{1078 if ext else uid} {q(d)}"]
        if not ext:
            script += [f"for c in {q(d)}/cache {q(d)}/code_cache; do [ -d $c ] && chown -R {uid}:{cache_gid} $c; done"]
        script += [f"chcon -R \"$ctx\" {q(d)}", f"echo \"restored {d} ($ctx)\""]
    script += [f"rm -f {TMP}"]
    rc, text = run(conn, " && ".join(script[:1]) + "; " + "; ".join(script[1:]) + " 2>&1", timeout=1800)
    log(text if text.endswith("\n") else text + "\n")
    if "restored" not in text:
        raise RuntimeError("restore failed – see the log")
    return f"{pkg} restored from {meta['created']} (uid {uid})"


def verify(conn, pkg: str) -> list[str]:
    """Owner / label summary of the app's folders (to compare before vs after a restore)."""
    rc, text = run(conn, f"for d in /data/data/{pkg} /data/user_de/0/{pkg}; do [ -d $d ] && "
                         f"echo \"$d $(stat -c '%u:%g %C' $d) files=$(find $d | wc -l) "
                         f"wrong_owner=$(find $d ! -user $(stat -c %u $d) | wc -l) "
                         f"wrong_label=$(find $d ! -context \"$(stat -c %C $d)\" 2>/dev/null | wc -l)\"; done")
    return text.strip().splitlines()
