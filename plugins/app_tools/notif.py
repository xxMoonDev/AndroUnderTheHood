"""Notification log.

A small root logger on the device polls `dumpsys notification --noredact` every few seconds and appends
every new notification (app, title, text, channel, post time) to a root-only file - it keeps logging
while the PC is away. The PC imports that file into a local SQLite database you can search.
Nothing leaves the device and the PC.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3

DIR = "/data/local/auth-notif"
LOG = f"{DIR}/log.tsv"
MARK = "authnotiflogge"            # + "r": the logger's marker (bracket trick for pkill/pgrep)

AWK = r"""
function val(s) { sub(/^[^=]*=[A-Za-z]+ \(/, "", s); sub(/\)[ \t]*$/, "", s); gsub(/\t/, " ", s); return s }
function out() { if (key != "" && pkg != "android") print key "\t" pkg "\t" when "\t" ch "\t" title "\t" text; key = "" }
/mArchive=/ { out(); done = 1 }
done { next }
/^    NotificationRecord\(/ {
  out(); pkg = ""; when = ""; title = ""; text = ""; ch = ""; key = "?"
  if (match($0, /pkg=[^ ]+/)) pkg = substr($0, RSTART + 4, RLENGTH - 4)
  if (match($0, /channel=[^ ]+/)) ch = substr($0, RSTART + 8, RLENGTH - 8)
  next
}
key == "?" && /^      key=/ { key = substr($0, index($0, "=") + 1) }
when == "" && /when=[0-9]/ { w = $0; sub(/.*when=/, "", w); sub(/[^0-9].*/, "", w); when = w }
title == "" && /android\.title=/ { title = val($0) }
text == "" && /android\.(text|bigText)=/ { text = val($0) }
END { out() }
"""

LOGGER = r"""
D={dir}; mkdir -p $D; chmod 700 $D
cat > $D/parse.awk
touch $D/seen $D/log.tsv; chmod 600 $D/*
setsid sh -c '
  while :; do
    dumpsys notification --noredact 2>/dev/null | awk -f {dir}/parse.awk > {dir}/cur.tsv
    grep -vxFf {dir}/seen {dir}/cur.tsv > {dir}/new.tsv
    if [ -s {dir}/new.tsv ]; then
      t=$(date +%s); awk -v t=$t "{{print t \"\t\" \$0}}" {dir}/new.tsv >> {dir}/log.tsv
      cat {dir}/new.tsv >> {dir}/seen; tail -n 3000 {dir}/seen > {dir}/seen.t && mv {dir}/seen.t {dir}/seen
    fi
    sleep {interval}
  done; : {mark}r' </dev/null >/dev/null 2>&1 &
echo started
"""


def run(conn, cmd, timeout=60, stdin=None):
    rc, out, err = conn.exec(cmd, timeout=timeout, stdin=stdin, as_root=True)
    return rc, (out + err).decode("utf-8", "replace")


def status(conn) -> dict:
    rc, text = run(conn, f"echo PID $(pgrep -f '{MARK}[r]' | head -n1); "
                         f"if [ -f {LOG} ]; then echo LINES $(wc -l < {LOG}); else echo LINES 0; fi")
    kv = dict((ln.split() + [""])[:2] for ln in text.splitlines() if ln.startswith(("PID", "LINES")))
    pid = kv.get("PID", "")
    lines = kv.get("LINES", "0")
    return {"running": pid.isdigit(), "pid": pid, "lines": int(lines) if lines.isdigit() else 0}


def start(conn, interval: int = 10) -> str:
    if status(conn)["running"]:
        return "already running"
    rc, text = run(conn, LOGGER.format(dir=DIR, interval=int(interval), mark=MARK), stdin=AWK.encode())
    return text.strip()


def stop(conn) -> str:
    # the loop's shell exits once its current `sleep` ends - wait for it, then force it
    run(conn, f"pkill -f '{MARK}[r]'; for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -f '{MARK}[r]' >/dev/null || break; "
              f"sleep 1; done; pkill -9 -f '{MARK}[r]'; true", timeout=30)
    return "stopped"


def clear_device(conn):
    run(conn, f"rm -f {LOG} {DIR}/seen; touch {LOG} {DIR}/seen; chmod 600 {DIR}/*")


def current(conn) -> list[list[str]]:
    rc, text = run(conn, f"cat > /data/local/tmp/.auth-notif.awk && dumpsys notification --noredact 2>/dev/null | "
                         f"awk -f /data/local/tmp/.auth-notif.awk; rm -f /data/local/tmp/.auth-notif.awk",
                   stdin=AWK.encode())
    t = int(dt.datetime.now().timestamp())
    return [[str(t)] + ln.split("\t") for ln in text.splitlines() if ln.count("\t") >= 5]


def fetch_log(conn) -> list[list[str]]:
    rc, text = run(conn, f"cat {LOG} 2>/dev/null", timeout=120)
    return [ln.split("\t") for ln in text.splitlines() if ln.count("\t") >= 6]


# ---- PC side database ------------------------------------------------------------------------------
class Store:
    def __init__(self, folder: str, device: str):
        os.makedirs(folder, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(folder, "notifications.db"), check_same_thread=False)
        self.device = device
        self.db.execute("""CREATE TABLE IF NOT EXISTS n (device TEXT, seen INTEGER, key TEXT, pkg TEXT,
                           posted INTEGER, channel TEXT, title TEXT, text TEXT,
                           UNIQUE(device, key, posted, title, text))""")
        self.db.commit()

    def add(self, rows: list[list[str]]) -> int:
        before = self.db.total_changes
        for r in rows:
            try:
                seen, key, pkg, when, ch, title, text = r[:7]
                posted = int(when) // 1000 if when.isdigit() and int(when) > 0 else int(seen)
                self.db.execute("INSERT OR IGNORE INTO n VALUES (?,?,?,?,?,?,?,?)",
                                (self.device, int(seen), key, pkg, posted, ch, title, text))
            except (ValueError, IndexError):
                continue
        self.db.commit()
        return self.db.total_changes - before

    def query(self, text: str = "", pkg: str = "", limit: int = 5000) -> list[tuple]:
        q = "SELECT posted, pkg, title, text, channel FROM n WHERE device=?"
        args = [self.device]
        if pkg:
            q += " AND pkg=?"
            args.append(pkg)
        if text:
            q += " AND (title LIKE ? OR text LIKE ? OR pkg LIKE ?)"
            args += [f"%{text}%"] * 3
        q += " ORDER BY posted DESC LIMIT ?"
        args.append(limit)
        return self.db.execute(q, args).fetchall()

    def apps(self) -> list[tuple[str, int]]:
        return self.db.execute("SELECT pkg, COUNT(*) FROM n WHERE device=? GROUP BY pkg ORDER BY 2 DESC",
                               (self.device,)).fetchall()

    def clear(self):
        self.db.execute("DELETE FROM n WHERE device=?", (self.device,))
        self.db.commit()
