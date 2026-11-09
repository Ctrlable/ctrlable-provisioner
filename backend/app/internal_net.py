"""The internal management LAN (SPEC § 20).

A private per-appliance bridge (default `vmbr1`, `198.18.0.0/24`) that every
guest also joins on a FIXED address, so the inter-service wiring the appliance
depends on — zigbee2mqtt → the MQTT broker, Home Assistant → zwave-js and
frigate — keeps working when the site LAN changes its DHCP range, or the whole
box is moved to a different site. (That is not hypothetical: the 174.48.139.185
appliance was provisioned on a 172.16.x LAN and shipped to a 10.0.0.x one; z2m
was pointed at the HA broker by its then-current DHCP address.)

Why 198.18.0.0/24 and not an RFC 1918 range: the bridge is isolated L2, so the
only collision that matters is with another network already present ON THE SAME
HOST. 198.18.0.0/15 is RFC 2544 benchmarking space — it is not a site LAN
(10/172.16/192.168), not Tailscale (100.64/10, RFC 6598), and not the Ctrlable
WireGuard fabric (10.10.16.x tunnels, 10.20.x proxy). `choose_subnet()` still
checks, so a surprise overlay can never make the bridge collide silently.

This module is PURE: it parses the manifest block, resolves a template's
internal address, and picks a non-colliding subnet. The host side (creating
`vmbr1` + dnsmasq, and the per-guest `net1`) is Phase 2, in install.sh and
ctrlable-build; the orchestrator wiring that uses `mqtt_host()` is in deploy.py.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

# The broker runs on the Home Assistant VM, so everything that needs MQTT is
# wired to this template's internal address.
BROKER_TEMPLATE = "ctrlable-pro"

# Tried in order when the manifest subnet collides. All RFC 2544 /24s or the
# far end of RFC 6598 that Tailscale does not hand out first — each still goes
# through the same overlap check, this list only sets the preference order.
DEFAULT_FALLBACKS = ["198.19.0.0/24", "198.18.1.0/24", "198.18.2.0/24"]


@dataclass(frozen=True)
class InternalNet:
    """Resolved internal-LAN config for one release.

    `assignments` maps a template name to its HOST OCTET within `subnet`
    (ctrlable-pro -> 10 means <subnet base> + 10). Storing the octet rather than
    a full address is deliberate: if `choose_subnet()` has to fall back to a
    different /24, every guest's address re-maps for free.
    """

    subnet: str
    bridge: str = "vmbr1"
    assignments: dict[str, int] = field(default_factory=dict)
    enabled: bool = True

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.ip_network(self.subnet, strict=False)

    @property
    def gateway(self) -> str:
        """The host's address on the bridge — always the first host (.1)."""
        return str(self.network.network_address + 1)

    def ip_for(self, template: str) -> str | None:
        """The fixed internal address assigned to a template, or None."""
        octet = self.assignments.get(template)
        if octet is None:
            return None
        addr = self.network.network_address + int(octet)
        if addr not in self.network or addr == self.network.network_address:
            raise ValueError(
                f"assignment {template}={octet} falls outside {self.subnet}"
            )
        return str(addr)

    def mqtt_host(self) -> str | None:
        """Where the MQTT broker lives on the internal LAN (the HA VM)."""
        return self.ip_for(BROKER_TEMPLATE)


def from_manifest(manifest) -> InternalNet | None:
    """Build an InternalNet from a ReleaseManifest, or None when the release
    declares no internal network (or declares it disabled).

    Accepts either a pydantic ReleaseManifest (with `.network.internal`) or a
    plain dict, so callers that have only parsed YAML can use it too.
    """
    block = _internal_block(manifest)
    if block is None:
        return None
    enabled = _get(block, "enabled", True)
    if not enabled:
        return None
    subnet = _get(block, "subnet", "198.18.0.0/24")
    bridge = _get(block, "bridge", "vmbr1")
    assignments = dict(_get(block, "assignments", {}) or {})
    # normalise octets to ints (YAML may give strings)
    assignments = {k: int(v) for k, v in assignments.items()}
    return InternalNet(subnet=subnet, bridge=bridge, assignments=assignments, enabled=True)


def overlaps(subnet: str, in_use: list[str]) -> list[str]:
    """Return the subset of `in_use` CIDRs that overlap `subnet`.

    `in_use` is whatever is already present on the host — every `ip -o addr`
    prefix, every route, and any tailscale0/zt* address — passed as CIDR
    strings. Entries that do not parse as a network are ignored rather than
    raising: a collision check that dies on a stray line is one nobody runs.
    """
    net = ipaddress.ip_network(subnet, strict=False)
    hits: list[str] = []
    for cidr in in_use:
        other = _as_network(cidr)
        if other is not None and net.overlaps(other):
            hits.append(cidr)
    return hits


def choose_subnet(preferred: str, in_use: list[str],
                  fallbacks: list[str] | None = None) -> tuple[str, bool]:
    """Pick the first candidate /24 that overlaps nothing already on the host.

    Returns (subnet, changed) where `changed` is True when the preferred subnet
    was NOT free and a fallback was used. Raises RuntimeError when every
    candidate collides, because silently sharing a subnet with Tailscale or the
    site LAN is exactly the failure this is here to prevent.
    """
    candidates = [preferred] + list(fallbacks if fallbacks is not None else DEFAULT_FALLBACKS)
    for cand in candidates:
        if not overlaps(cand, in_use):
            return cand, cand != preferred
    raise RuntimeError(
        f"no free internal subnet: every candidate {candidates} overlaps "
        f"a network already on the host ({in_use})"
    )


# --- internals -------------------------------------------------------------

def _internal_block(manifest):
    net = getattr(manifest, "network", None)
    if net is None and isinstance(manifest, dict):
        net = manifest.get("network")
    if net is None:
        return None
    internal = getattr(net, "internal", None)
    if internal is None and isinstance(net, dict):
        internal = net.get("internal")
    return internal


def _get(obj, key, default):
    if isinstance(obj, dict):
        return obj.get(key, default)
    val = getattr(obj, key, default)
    return default if val is None else val


def _as_network(cidr: str):
    try:
        return ipaddress.ip_network(str(cidr).strip(), strict=False)
    except ValueError:
        # Maybe a bare address with no prefix; treat as a /32 or /128.
        try:
            return ipaddress.ip_network(str(cidr).strip() + "/32", strict=False)
        except ValueError:
            return None
