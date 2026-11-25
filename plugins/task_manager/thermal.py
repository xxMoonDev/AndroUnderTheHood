"""Thermal & throttling recorder.

Records what the Task Manager sampler already reads every snapshot (CPU load, per-core frequency,
thermal zones, GPU) and detects throttling: a CPU cluster running well below its top frequency while
it is busy. An optional stress load (one busy loop per core, auto-stopping) makes runs repeatable.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import re

THROTTLE_PCT = 85.0      # cluster below this % of its max frequency ...
BUSY_PCT = 80.0          # ... while its cores are at least this busy  -> throttled
STRESS_MARK = "authstress"


def clusters(static: dict) -> dict[int, list[int]]:
    """{max_khz: [cores]} - cores that share a top frequency form a cluster (little/big/prime)."""
    out: dict[int, list[int]] = {}
    for core, mx in static.get("max_khz", {}).items():
        out.setdefault(int(mx), []).append(int(core))
    return dict(sorted(out.items()))


class Recorder:
    def __init__(self, static: dict):
        self.static = static
        self.clusters = clusters(static)
        self.types = static.get("therm_types", {})
        self.rows: list[dict] = []
        self.t0 = None
        self.events: list[dict] = []     # {cluster, start, end, min_pct, temp}
        self._open: dict[int, dict] = {}

    def add(self, model) -> dict | None:
        snap = model.cur
        if not snap or not model.prev:
            return None
        if self.t0 is None:
            self.t0 = snap["t"]
        t = snap["t"] - self.t0
        row = {"t": round(t, 1), "cpu": round(model.cpu_pct.get("all", 0.0), 1)}
        temps = model.cpu_temps() or list(snap["therm"].values())
        row["cpu_temp"] = round(max(temps), 1) if temps else None
        row["gpu"] = snap["gpu"].get("busy")
        for mx, cores in self.clusters.items():
            fr = [snap["freq"].get(c) for c in cores if snap["freq"].get(c)]
            load = [model.cpu_pct.get(c, 0.0) for c in cores]
            pct = 100.0 * (sum(fr) / len(fr)) / mx if fr and mx else None
            busy = sum(load) / len(load) if load else 0.0
            row[f"c{mx}_pct"] = round(pct, 1) if pct is not None else None
            row[f"c{mx}_load"] = round(busy, 1)
            throttled = pct is not None and pct < THROTTLE_PCT and busy >= BUSY_PCT
            ev = self._open.get(mx)
            if throttled and not ev:
                self._open[mx] = {"cluster": mx, "start": t, "end": t, "min_pct": pct, "temp": row["cpu_temp"]}
            elif throttled:
                ev["end"] = t
                ev["min_pct"] = min(ev["min_pct"], pct)
            elif ev:
                if ev["end"] - ev["start"] >= 2:       # ignore one-sample blips
                    self.events.append(ev)
                self._open.pop(mx)
        for z, c in snap["therm"].items():
            row[f"z{z}"] = c
        self.rows.append(row)
        return row

    def close(self):
        for mx, ev in list(self._open.items()):
            if ev["end"] - ev["start"] >= 2:
                self.events.append(ev)
        self._open.clear()

    def summary(self) -> list[str]:
        if not self.rows:
            return ["nothing recorded"]
        dur = self.rows[-1]["t"]
        temps = [r["cpu_temp"] for r in self.rows if r["cpu_temp"] is not None]
        lines = [f"Duration {dur:.0f} s, {len(self.rows)} samples",
                 f"CPU temperature: start {temps[0]:.1f} °C, peak {max(temps):.1f} °C, end {temps[-1]:.1f} °C"
                 if temps else "No CPU temperature sensor readable"]
        for mx, cores in self.clusters.items():
            vals = [r[f"c{mx}_pct"] for r in self.rows if r.get(f"c{mx}_pct") is not None and r[f"c{mx}_load"] >= BUSY_PCT]
            name = f"Cluster {cores[0]}-{cores[-1]} (max {mx / 1e6:.2f} GHz)"
            if vals:
                lines.append(f"{name}: while busy it ran at {sum(vals) / len(vals):.0f} % of max on average, "
                             f"lowest {min(vals):.0f} %")
            else:
                lines.append(f"{name}: never fully busy during the recording")
        ev = self.events
        if ev:
            first = min(e["start"] for e in ev)
            lines.append(f"Throttling: {len(ev)} episode(s); first after {first:.0f} s"
                         + (f" at {ev[0]['temp']:.1f} °C" if ev[0]["temp"] is not None else ""))
            for e in ev:
                lines.append(f"   {e['start']:.0f}–{e['end']:.0f} s: cluster max {e['cluster'] / 1e6:.2f} GHz down to "
                             f"{e['min_pct']:.0f} %")
        else:
            lines.append("No throttling detected (busy clusters stayed above "
                         f"{THROTTLE_PCT:.0f} % of their top frequency)")
        return lines

    def save_csv(self, device_name: str) -> str:
        safe = re.sub(r'[<>:"/\\|?*]', "_", device_name or "device")
        d = os.path.join(os.path.expanduser("~"), "Documents", "AndroUnderTheHood", "Thermal", safe)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"thermal-{dt.datetime.now():%Y%m%d-%H%M%S}.csv")
        cols = []
        for r in self.rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        names = {f"z{z}": f"zone{z}:{ty}" for z, ty in self.types.items()}
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([names.get(c, c) for c in cols])
            for r in self.rows:
                w.writerow([r.get(c, "") for c in cols])
        return path


def stress_cmd(cores: int, seconds: int) -> str:
    """One busy loop per core, detached, each stopping itself after `seconds` (also if the PC goes away)."""
    loop = f"timeout {int(seconds)} sh -c 'while :; do :; done; : {STRESS_MARK}'"
    return (f"for i in $(seq {int(cores)}); do setsid {loop} </dev/null >/dev/null 2>&1 & done; "
            f"echo started {int(cores)}")


def stop_stress_cmd() -> str:
    # bracket trick: the pattern must not match the shell that runs pkill itself
    return f"pkill -f '{STRESS_MARK[:-1]}[{STRESS_MARK[-1]}]'; true"
