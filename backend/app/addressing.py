"""Resolve a guest's address, so § 16 probes can reach it.

SPEC § 16 / § 18 "probe transport". Two sources, layered, because each is
blind exactly where the other sees:

    PROXMOX CONFIG   `net0` on the guest config, read over the API the
                     orchestrator already uses. Authoritative for a STATIC
                     address, needs no cooperation from the guest, and still
                     works when the guest is wedged or stopped. Says nothing
                     at all when the guest is on DHCP — `ip=dhcp` is a
                     declaration of intent, not an address.

    FIRST-BOOT       the address the guest reported through the assignment
                     callback it already makes (§ 7). The only source that
                     knows what the guest ACTUALLY bound, so it is the only
                     one that works under DHCP. Goes stale if the lease
                     moves, and does not exist until the guest has booted
                     once.

Neither is sufficient alone, so `resolve()` tries config first and falls back
to the recorded address, and ALWAYS reports which one answered. A drift row
that cannot say how it reached the guest is a row you cannot trust: "probed
OK" against a stale lease is a wrong answer that looks like a right one.

Failure is `UNKNOWN`, never a guess. drift.evaluate() already treats a missing
reading as not-healthy rather than OK.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import Enum


class Source(str, Enum):
    CONFIG = "proxmox-config"     # static, from net0
    REPORTED = "first-boot"       # what the guest said it bound
    NONE = "unresolved"


@dataclass(frozen=True)
class Address:
    ip: str | None
    source: Source
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ip is not None


# `net0: name=eth0,bridge=vmbr0,hwaddr=AA:..,ip=10.1.8.14/24,gw=10.1.8.1`
# `ip` may also be the literal `dhcp`, or `manual`, or absent entirely.
_IP_IN_NET = re.compile(r"(?:^|,)ip=([^,]+)")


def from_proxmox_config(config: dict) -> Address:
    """Pull a static address out of a guest config, if there is one.

    Scans every `netN` rather than assuming `net0`: a guest with a separate
    management interface may carry the reachable address on `net1`, and
    picking the first interface by name would quietly probe the wrong one.
    """
    if not config:
        return Address(None, Source.NONE, "no config returned")

    seen_dhcp = False
    for key in sorted(k for k in config if re.fullmatch(r"net\d+", str(k))):
        value = str(config[key] or "")
        m = _IP_IN_NET.search(value)
        if not m:
            continue
        raw = m.group(1).strip()
        if raw.lower() in {"dhcp", "manual", "auto", ""}:
            seen_dhcp = True
            continue
        try:
            # `ip=` carries a CIDR; the probe wants the host part only.
            addr = ipaddress.ip_interface(raw).ip
        except ValueError:
            continue
        if addr.is_loopback or addr.is_unspecified:
            continue
        return Address(str(addr), Source.CONFIG, f"{key} {raw}")

    if seen_dhcp:
        return Address(None, Source.NONE,
                       "guest is on DHCP; config declares intent, not an address")
    return Address(None, Source.NONE, "no ip= on any netN")


def from_reported(reported_ip: str | None, *, stale: bool = False) -> Address:
    """The address the guest reported at first boot.

    `stale` lets a caller mark a reading it already distrusts (an instance
    that has been rebooted since, say) without this module having to own a
    freshness policy it cannot see.
    """
    if not reported_ip:
        return Address(None, Source.NONE, "guest has not reported an address")
    try:
        addr = ipaddress.ip_address(str(reported_ip).strip())
    except ValueError:
        return Address(None, Source.NONE, f"not an address: {reported_ip!r}")
    if addr.is_loopback or addr.is_unspecified:
        # A guest that reported 127.0.0.1 resolved its own hostname badly.
        # Probing it would read the ORCHESTRATOR's own services and report
        # them as the guest's — a wrong answer that looks right.
        return Address(None, Source.NONE, f"guest reported {addr}, which is not routable")
    return Address(str(addr), Source.REPORTED,
                   "reported at first boot" + (" (possibly stale)" if stale else ""))


def resolve(config: dict | None = None,
            reported_ip: str | None = None,
            *, reported_stale: bool = False) -> Address:
    """Config first, then what the guest reported.

    Config wins when present because it is declarative and cannot go stale:
    if someone pinned a static address, that is the address, and a cached
    first-boot reading that disagrees is the one to distrust.
    """
    static = from_proxmox_config(config or {})
    if static:
        return static
    dynamic = from_reported(reported_ip, stale=reported_stale)
    if dynamic:
        return dynamic
    # Report the more informative of the two reasons.
    detail = static.detail if "DHCP" in static.detail else (
        dynamic.detail or static.detail)
    return Address(None, Source.NONE, detail or "no address from either source")
