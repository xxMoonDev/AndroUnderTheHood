"""Packet capture: tcpdump on the device, streamed live (binary-clean) to a .pcap on the PC and, if
Wireshark is installed, into Wireshark at the same time.

tcpdump comes from the device (/system/bin on many custom ROMs) or from the Phone desktop Ubuntu
chroot (`apt install tcpdump` there). The chroot shares Android's network, so it sees every interface.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import struct
import subprocess
import threading

_NOWIN = 0x08000000
PD_SHELL = "/data/local/phonedesktop/bin/pd-shell.sh"
PD_ROOTFS = "/data/local/phonedesktop/rootfs"


def q(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def run(conn, cmd, timeout=120):
    rc, out, err = conn.exec(cmd, timeout=timeout, as_root=True)
    return rc, (out + err).decode("utf-8", "replace")


def find_tool(conn) -> tuple[str, str]:
    """('system', path) | ('chroot', '/usr/bin/tcpdump') | ('chroot-missing', '') | ('', '')"""
    rc, text = run(conn, f"command -v tcpdump; test -x {PD_ROOTFS}/usr/bin/tcpdump && echo CHROOT; "
                         f"test -f {PD_SHELL} && echo PD")
    lines = text.split()
    if lines and lines[0].startswith("/"):
        return "system", lines[0]
    if "CHROOT" in lines:
        return "chroot", "/usr/bin/tcpdump"
    if "PD" in lines:
        return "chroot-missing", ""
    return "", ""


def install_in_chroot(conn) -> str:
    rc, text = run(conn, f"nsenter -t 1 -m -- sh {PD_SHELL} /bin/bash -c "
                         f"'DEBIAN_FRONTEND=noninteractive apt-get install -y tcpdump' 2>&1 | tail -n 5", timeout=900)
    return text


def interfaces(conn) -> list[str]:
    rc, text = run(conn, "ip -o link show 2>/dev/null")
    names = []
    for ln in text.splitlines():
        m = re.match(r"\d+:\s+([^:@\s]+)", ln)
        if m:
            names.append(m.group(1))
    return ["any"] + [n for n in names if n != "lo"] + ["lo"]


def wireshark_path() -> str:
    for p in (r"C:\Program Files\Wireshark\Wireshark.exe", r"C:\Program Files (x86)\Wireshark\Wireshark.exe"):
        if os.path.isfile(p):
            return p
    return ""


def captures_dir(device_name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', "_", device_name or "device")
    d = os.path.join(os.path.expanduser("~"), "Documents", "AndroUnderTheHood", "Captures", safe)
    os.makedirs(d, exist_ok=True)
    return d


class Capture:
    """One running capture. stats: bytes / packets; stop() ends it on the PC and on the device."""

    def __init__(self, conn, tool: tuple[str, str], iface: str, bpf: str, snaplen: int, device_name: str,
                 wireshark: bool, on_end=lambda msg: None):
        if bpf and not re.fullmatch(r"[\w\s.:/\-()!&|<>=\[\]]*", bpf):
            raise ValueError("The filter contains characters that aren't allowed in a capture filter")
        self.conn, self.iface, self.on_end = conn, iface, on_end
        self.bytes = self.packets = 0
        self.stopped = False
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(captures_dir(device_name), f"{iface}-{stamp}.pcap")
        core = f"{tool[1] or 'tcpdump'} -i {q(iface)} -U -s {int(snaplen)} -w - {bpf}".strip()
        self.marker = f"-i {iface} -U -s {int(snaplen)} -w -"
        cmd = core if tool[0] == "system" else f"nsenter -t 1 -m -- sh {PD_SHELL} {core}"
        self.cmd = cmd + " 2>/dev/null"
        self.ws = None
        if wireshark and wireshark_path():
            self.ws = subprocess.Popen([wireshark_path(), "-k", "-i", "-"], stdin=subprocess.PIPE)
        self.file = open(self.path, "wb")
        self.proc = self.chan = None
        if getattr(conn, "is_adb", False):
            # exec-out is binary-clean (adb shell would turn \n into \r\n on Windows)
            self.proc = subprocess.Popen([conn.exe, "-s", conn.serial, "exec-out", f"su -c {q(self.cmd)} | cat"],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         stdin=subprocess.DEVNULL, creationflags=_NOWIN)
            self.read = lambda: self.proc.stdout.read1(65536)
        else:
            self.chan = conn.open_channel(as_root=True)
            self.chan.exec_command(conn.wrap_root(self.cmd))
            self.read = lambda: self.chan.recv(65536)
        threading.Thread(target=self._pump, daemon=True, name=f"capture-{iface}").start()

    def _pump(self):
        buf = b""
        header_done = False
        endian = "<"
        msg = "capture ended"
        try:
            while not self.stopped:
                b = self.read()
                if not b:
                    break
                self.file.write(b)
                self.bytes += len(b)
                if self.ws:
                    try:
                        self.ws.stdin.write(b)
                        self.ws.stdin.flush()
                    except OSError:
                        self.ws = None
                # count packets from the pcap record headers
                buf += b
                if not header_done and len(buf) >= 24:
                    # pcap magic a1b2c3d4 (µs) / a1b23c4d (ns), written in the capturer's byte order
                    endian = "<" if buf[:4] in (bytes.fromhex("d4c3b2a1"), bytes.fromhex("4d3cb2a1")) else ">"
                    buf = buf[24:]
                    header_done = True
                while header_done and len(buf) >= 16:
                    incl = struct.unpack(endian + "I", buf[8:12])[0]
                    if len(buf) < 16 + incl:
                        break
                    buf = buf[16 + incl:]
                    self.packets += 1
        except Exception as e:  # noqa: BLE001
            msg = f"capture error: {e}"
        finally:
            self.file.close()
            self.on_end(msg)

    def stop(self):
        self.stopped = True
        # the device side: kill our tcpdump (bracket trick so pkill doesn't match its own shell)
        pat = "tcpdum[p] " + self.marker
        try:
            run(self.conn, f"pkill -f {q(pat)}; true", timeout=20)
        except Exception:  # noqa: BLE001
            pass
        for x in (self.proc, self.chan):
            try:
                if x is self.proc and x:
                    x.kill()
                elif x:
                    x.close()
            except Exception:  # noqa: BLE001
                pass
        if self.ws:
            try:
                self.ws.stdin.close()
            except OSError:
                pass
