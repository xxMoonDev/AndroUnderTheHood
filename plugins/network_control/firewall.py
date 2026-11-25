"""Per-app firewall.

Android 13+ (`cmd connectivity set-package-networking-enabled`): Android's own eBPF firewall chain
FIREWALL_CHAIN_OEM_DENY_3 - blocks all of an app's traffic (every network, VPN included), resets at
reboot. Older Android: iptables/ip6tables owner rules in a private chain `auth_fw` hooked into OUTPUT.

Blocking works per uid: apps that share a uid (android:sharedUserId) are blocked together.
"""
from __future__ import annotations

import re

CHAIN = "auth_fw"
# never blockable: this app's own lifelines
PROTECTED = {"dev.rootssh": "AndroOpenHeart (SSH access to this device)",
             "com.topjohnwu.magisk": "Magisk app",
             "net.openvpn.openvpn": "VPN app", "com.wireguard.android": "VPN app",
             "net.openvpn.openvpn.connect": "VPN app"}


def run(conn, cmd, timeout=60, stdin=None):
    rc, out, err = conn.exec(cmd, timeout=timeout, stdin=stdin, as_root=True)
    return rc, (out + err).decode("utf-8", "replace")


def backend(conn) -> str:
    rc, text = run(conn, "cmd connectivity help 2>/dev/null | grep -q set-package-networking-enabled && echo yes")
    return "chain3" if "yes" in text else "iptables"


def vpn_package(conn) -> str:
    """The app that runs the active (or always-on) VPN, so it can be protected."""
    rc, text = run(conn, "settings get secure always_on_vpn_app; "
                         "dumpsys connectivity 2>/dev/null | grep -o 'VPN:[^ ]*' | head -n1; "
                         "dumpsys vpn_management 2>/dev/null | grep -o 'package=[A-Za-z0-9._]*' | head -n1")
    for ln in text.split():
        ln = ln.replace("package=", "").replace("VPN:", "").strip()
        if re.fullmatch(r"[A-Za-z][\w.]+\.[\w.]+", ln):
            return ln
    return ""


def blocked_uids(conn, kind: str) -> tuple[set[int], bool]:
    """(uids currently blocked, chain enabled?)"""
    if kind == "chain3":
        rc, text = run(conn, "cmd connectivity get-chain3-enabled; dumpsys connectivity trafficcontroller 2>/dev/null")
        enabled = "chain:enabled" in text
        uids = {int(m.group(1)) for m in re.finditer(r"^\s+(\d+)\s+.*OEM_DENY_3_MATCH", text, re.M)}
        return uids, enabled
    rc, text = run(conn, f"iptables -S {CHAIN} 2>/dev/null")
    return {int(u) for u in re.findall(r"--uid-owner (\d+)", text)}, rc == 0


def ensure_iptables(conn):
    run(conn, f"for t in iptables ip6tables; do $t -N {CHAIN} 2>/dev/null; "
              f"$t -C OUTPUT -j {CHAIN} 2>/dev/null || $t -I OUTPUT 1 -j {CHAIN}; done")


def set_blocked(conn, kind: str, pkg: str, uid: int, block: bool) -> str:
    if kind == "chain3":
        cmds = []
        if block:
            cmds.append("cmd connectivity set-chain3-enabled true")
        cmds.append(f"cmd connectivity set-package-networking-enabled {'false' if block else 'true'} {pkg}")
        rc, text = run(conn, " && ".join(cmds))
    else:
        ensure_iptables(conn)
        op = "-A" if block else "-D"
        check = "! " if block else ""
        rc, text = run(conn, "; ".join(
            f"{check}$t -C {CHAIN} -m owner --uid-owner {uid} -j REJECT 2>/dev/null && "
            f"$t {op} {CHAIN} -m owner --uid-owner {uid} -j REJECT"
            .replace("$t", t) for t in ("iptables", "ip6tables")) + "; true")
    if rc != 0:
        raise RuntimeError(text.strip() or f"exit code {rc}")
    if kind == "chain3" and not block:
        left, _ = blocked_uids(conn, kind)
        if not left:                     # nothing blocked any more: chain off again, as Android had it
            run(conn, "cmd connectivity set-chain3-enabled false")
    return text.strip()


def probe(conn, uid: int, host="1.1.1.1", port=443) -> bool:
    """Can a process running as this uid open a TCP connection? (proves the block works)"""
    rc, text = run(conn, f"su {uid} -c 'nc -w 4 -z {host} {port}' >/dev/null 2>&1; echo RC=$?", timeout=30)
    return "RC=0" in text


# ---- keep blocks after reboot (Magisk service.d) ------------------------------------------------
BOOT_HOOK = "/data/adb/service.d/auth-firewall.sh"


def boot_script(kind: str, pkgs: list[str], uids: list[int]) -> str:
    lines = ["#!/system/bin/sh", "# AndroUnderTheHood network_control: re-apply app blocks at boot",
             "until [ \"$(getprop sys.boot_completed)\" = 1 ]; do sleep 3; done"]
    if kind == "chain3":
        lines.append("cmd connectivity set-chain3-enabled true")
        lines += [f"cmd connectivity set-package-networking-enabled false {p}" for p in pkgs]
    else:
        for t in ("iptables", "ip6tables"):
            lines.append(f"{t} -N {CHAIN} 2>/dev/null; {t} -C OUTPUT -j {CHAIN} 2>/dev/null || {t} -I OUTPUT 1 -j {CHAIN}")
            lines += [f"{t} -A {CHAIN} -m owner --uid-owner {u} -j REJECT" for u in uids]
    return "\n".join(lines) + "\n"


def set_boot_hook(conn, kind, pkgs, uids, on: bool):
    if on and pkgs:
        run(conn, f"mkdir -p /data/adb/service.d && cat > {BOOT_HOOK} && chmod 755 {BOOT_HOOK}",
            stdin=boot_script(kind, pkgs, uids).encode())
    else:
        run(conn, f"rm -f {BOOT_HOOK}")


def boot_hook_present(conn) -> bool:
    rc, text = run(conn, f"test -f {BOOT_HOOK} && echo yes")
    return "yes" in text
