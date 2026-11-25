"""Port forwards and tunnels.

ADB:  native `adb forward` (PC port -> device port) and `adb reverse` (device port -> PC port).
SSH:  local forwards only. AndroOpenHeart refuses SSH port forwarding, so each incoming PC connection
      opens an exec channel running `nc HOST PORT` on the device and the bytes are relayed through it.
"""
from __future__ import annotations

import socket
import subprocess
import threading

_NOWIN = 0x08000000  # CREATE_NO_WINDOW


def is_adb(conn) -> bool:
    return bool(getattr(conn, "is_adb", False))


def adb(conn, *args, timeout=30) -> str:
    r = subprocess.run([conn.exe, "-s", conn.serial, *args], capture_output=True, timeout=timeout,
                       creationflags=_NOWIN)
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    if r.returncode != 0:
        raise RuntimeError(out or f"adb {' '.join(args)} failed")
    return out


def adb_list(conn) -> list[dict]:
    """Active adb forwards/reverses for this device."""
    rows = []
    for line in adb(conn, "forward", "--list").splitlines():
        p = line.split()
        if len(p) == 3 and p[0] == conn.serial:
            rows.append({"kind": "local", "pc": p[1], "dev": p[2]})
    try:
        for line in adb(conn, "reverse", "--list").splitlines():
            p = line.split()
            if len(p) >= 3:
                rows.append({"kind": "reverse", "dev": p[1], "pc": p[2]})
    except RuntimeError:
        pass
    return rows


class Relay:
    """PC listener on 127.0.0.1:pc_port; each connection -> `nc host port` on the device over SSH."""

    def __init__(self, conn, pc_port: int, host: str, port: int, log=lambda s: None):
        self.conn, self.pc_port, self.host, self.port, self.log = conn, pc_port, host, port, log
        self.active = 0
        self.total = 0
        self.stopped = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", pc_port))
        self.sock.listen(16)
        threading.Thread(target=self._accept, daemon=True, name=f"relay-{pc_port}").start()

    def _accept(self):
        while not self.stopped:
            try:
                c, _ = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        self.active += 1
        self.total += 1
        chan = None
        try:
            chan = self.conn.open_channel(as_root=False)
            chan.exec_command(f"nc -q 1 {self.host} {self.port}")

            def up():
                try:
                    while True:
                        b = c.recv(65536)
                        if not b:
                            break
                        chan.sendall(b)
                finally:
                    try:
                        chan.shutdown_write()
                    except Exception:  # noqa: BLE001
                        pass
            t = threading.Thread(target=up, daemon=True)
            t.start()
            while True:
                b = chan.recv(65536)
                if not b:
                    break
                c.sendall(b)
        except Exception as e:  # noqa: BLE001
            self.log(f"relay {self.pc_port}: {e}\n")
        finally:
            self.active -= 1
            for x in (c, chan):
                try:
                    x and x.close()
                except Exception:  # noqa: BLE001
                    pass

    def stop(self):
        self.stopped = True
        try:
            self.sock.close()
        except OSError:
            pass


# relays survive the window being closed (like adb forwards do); stopped on plugin unload
RELAYS: dict[int, Relay] = {}


def start(conn, kind: str, pc_port: int, dev_port: int, host: str = "127.0.0.1", log=lambda s: None) -> str:
    if is_adb(conn):
        if kind == "local":
            if host not in ("127.0.0.1", "localhost"):
                raise RuntimeError("adb forward reaches ports on the device itself only (host 127.0.0.1)")
            adb(conn, "forward", f"tcp:{pc_port}", f"tcp:{dev_port}")
            return f"PC 127.0.0.1:{pc_port} → device :{dev_port} (adb forward)"
        adb(conn, "reverse", f"tcp:{dev_port}", f"tcp:{pc_port}")
        return f"device 127.0.0.1:{dev_port} → PC :{pc_port} (adb reverse)"
    if kind != "local":
        raise RuntimeError("Reverse forwards need an ADB connection (the SSH server doesn't allow them)")
    if pc_port in RELAYS:
        raise RuntimeError(f"PC port {pc_port} is already forwarded")
    RELAYS[pc_port] = Relay(conn, pc_port, host, dev_port, log)
    return f"PC 127.0.0.1:{pc_port} → device {host}:{dev_port} (SSH relay via nc)"


def stop(conn, kind: str, pc_port: int, dev_port: int):
    if pc_port in RELAYS:
        RELAYS.pop(pc_port).stop()
        return
    if is_adb(conn):
        if kind == "local":
            adb(conn, "forward", "--remove", f"tcp:{pc_port}")
        else:
            adb(conn, "reverse", "--remove", f"tcp:{dev_port}")


def stop_all_relays():
    for p in list(RELAYS):
        RELAYS.pop(p).stop()
