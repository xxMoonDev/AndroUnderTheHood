"""SELinux denial viewer: collects `avc: denied` from the kernel log and logcat, groups them by
(source type, target type, class), and turns each group into a magiskpolicy allow rule.

Rules can be copied, saved as a module's sepolicy.rule (persistent, via the module workbench) or
applied live with `magiskpolicy --live` (until reboot) - the UI asks before either.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

COLLECT = ("{ dmesg 2>/dev/null; logcat -b all -d 2>/dev/null; } | grep -a 'avc: *denied' | tail -n 4000")

AVC = re.compile(r"avc:\s+denied\s+\{\s*(?P<perms>[^}]*)\}\s+for\s+(?P<rest>.*)")
KV = re.compile(r'(\w+)=("[^"]*"|\S+)')

# domains where an allow rule weakens Android's app sandbox noticeably
RISKY = re.compile(r"^(untrusted_app\w*|isolated_app\w*|platform_app|priv_app|shell|sdcardfs|kernel|init|zygote|"
                   r"system_server)$")


def ctx_type(ctx: str) -> str:
    """u:r:untrusted_app:s0:c123 -> untrusted_app"""
    parts = ctx.split(":")
    return parts[2] if len(parts) >= 3 else ctx


@dataclass
class Group:
    stype: str
    ttype: str
    tclass: str
    perms: set = field(default_factory=set)
    count: int = 0
    comms: set = field(default_factory=set)
    names: set = field(default_factory=set)
    permissive: bool = False
    example: str = ""

    @property
    def rule(self) -> str:
        p = sorted(self.perms)
        perms = p[0] if len(p) == 1 else "{ " + " ".join(p) + " }"
        return f"allow {self.stype} {self.ttype} {self.tclass} {perms}"

    @property
    def risky(self) -> bool:
        return bool(RISKY.match(self.stype))


def parse(text: str) -> list[Group]:
    groups: dict[tuple, Group] = {}
    for line in text.splitlines():
        m = AVC.search(line)
        if not m:
            continue
        kv = {k: v.strip('"') for k, v in KV.findall(m.group("rest"))}
        if "scontext" not in kv or "tcontext" not in kv or "tclass" not in kv:
            continue
        key = (ctx_type(kv["scontext"]), ctx_type(kv["tcontext"]), kv["tclass"])
        g = groups.get(key)
        if not g:
            g = groups[key] = Group(*key, example=line.strip()[-400:])
        g.perms.update(m.group("perms").split())
        g.count += 1
        if kv.get("comm"):
            g.comms.add(kv["comm"])
        if kv.get("name") or kv.get("path"):
            g.names.add(kv.get("path") or kv.get("name"))
        if kv.get("permissive") == "1":
            g.permissive = True
    return sorted(groups.values(), key=lambda g: -g.count)


def collect(conn) -> list[Group]:
    rc, out, err = conn.exec(COLLECT, timeout=120, as_root=True)
    return parse(out.decode("utf-8", "replace"))


def apply_live(conn, rules: list[str]) -> str:
    """magiskpolicy --live: changes the running policy until the next reboot."""
    q = " ".join("'" + r.replace("'", "") + "'" for r in rules)
    rc, out, err = conn.exec(f"magiskpolicy --live {q} 2>&1; echo \"[exit code $?]\"", timeout=120, as_root=True)
    return out.decode("utf-8", "replace")


def append_to_module(folder: str, rules: list[str]) -> str:
    """Add rules to <module>/sepolicy.rule (Magisk applies it at every boot)."""
    import os
    path = os.path.join(folder, "sepolicy.rule")
    old = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    new = [r for r in rules if r not in old.splitlines()]
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        if old and not old.endswith("\n"):
            f.write("\n")
        for r in new:
            f.write(r + "\n")
    return path
