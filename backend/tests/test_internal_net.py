"""Internal management LAN resolution + collision check (SPEC § 19).

Run:  python3 backend/tests/test_internal_net.py

Standalone, like the sibling tests. Exercises the pure helpers in
app/internal_net.py against the REAL 2026.11 manifest and the real collision
scenarios: the default 198.18 subnet dodging Tailscale's 100.64/10 and the
site LAN, and falling back to another /24 when something already occupies it.

The point under test is the promise the internal LAN makes: a fixed address per
service that does not move with DHCP, and a subnet that can never silently
overlap something already on the host.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app import internal_net as inet  # noqa: E402
from app.manifest import load_manifest, ReleaseManifest, NetworkSpec, InternalNetSpec  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  -- ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


# ---- 1. the REAL 2026.11 manifest resolves to the expected plan -------------
mpath = pathlib.Path(__file__).resolve().parent.parent.parent / "releases" / "2026.11.yaml"
manifest = load_manifest(mpath)
net = inet.from_manifest(manifest)

check("2026.11 declares an internal net", net is not None)
check("bridge is vmbr1", net and net.bridge == "vmbr1", str(net and net.bridge))
check("gateway is .1", net and net.gateway == "198.18.0.1", str(net and net.gateway))
check("ctrlable-pro -> .10", net and net.ip_for("ctrlable-pro") == "198.18.0.10",
      str(net and net.ip_for("ctrlable-pro")))
check("zigbee2mqtt -> .21", net and net.ip_for("zigbee2mqtt") == "198.18.0.21",
      str(net and net.ip_for("zigbee2mqtt")))
check("frigate -> .30", net and net.ip_for("frigate") == "198.18.0.30")
check("mqtt_host is the HA VM", net and net.mqtt_host() == "198.18.0.10",
      str(net and net.mqtt_host()))
check("unknown template -> None", net and net.ip_for("nope") is None)


# ---- 2. octets re-map when the subnet changes (why we store octets) ---------
moved = inet.InternalNet(subnet="198.19.0.0/24", assignments=net.assignments)
check("subnet fallback re-maps ctrlable-pro -> 198.19.0.10",
      moved.ip_for("ctrlable-pro") == "198.19.0.10", moved.ip_for("ctrlable-pro"))
check("subnet fallback re-maps mqtt_host", moved.mqtt_host() == "198.19.0.10")


# ---- 3. out-of-range octet is rejected, not silently wrong ------------------
try:
    inet.InternalNet(subnet="198.18.0.0/24", assignments={"x": 300}).ip_for("x")
    check("octet 300 outside /24 raises", False, "no raise")
except ValueError:
    check("octet 300 outside /24 raises", True)


# ---- 4. overlap check: fires on a collision, silent when free (non-vacuous) --
# Default 198.18 does NOT collide with Tailscale or a 10.x / 192.168 site LAN.
clean_host = ["10.0.0.216/24", "100.64.0.5/32", "172.30.32.1/23", "10.10.16.107/32"]
check("198.18 is clear of Tailscale + site LAN + WG",
      inet.overlaps("198.18.0.0/24", clean_host) == [], str(inet.overlaps("198.18.0.0/24", clean_host)))
# But it DOES collide when something already sits in 198.18.
dirty_host = clean_host + ["198.18.0.1/24"]
check("198.18 overlap detected when host already uses it",
      inet.overlaps("198.18.0.0/24", dirty_host) == ["198.18.0.1/24"])
# A garbage line must not crash the check.
check("garbage CIDR is ignored but a real overlap after it is still caught",
      inet.overlaps("198.18.0.0/24", ["not-an-ip", "198.18.0.9/24"]) == ["198.18.0.9/24"])


# ---- 5. choose_subnet: prefer, fall back, or refuse -------------------------
sub, changed = inet.choose_subnet("198.18.0.0/24", clean_host)
check("choose_subnet keeps 198.18 when free", sub == "198.18.0.0/24" and not changed, f"{sub} changed={changed}")
sub2, changed2 = inet.choose_subnet("198.18.0.0/24", dirty_host)
check("choose_subnet falls back off a taken 198.18", sub2 == "198.19.0.0/24" and changed2, f"{sub2} changed={changed2}")
try:
    # every candidate blocked -> refuse rather than collide
    blocked = ["198.18.0.0/16", "198.19.0.0/16"]
    inet.choose_subnet("198.18.0.0/24", blocked)
    check("choose_subnet raises when nothing is free", False, "no raise")
except RuntimeError:
    check("choose_subnet raises when nothing is free", True)


# ---- 6. from_manifest honours enabled / absent ------------------------------
disabled = ReleaseManifest(release="t", community_scripts_ref="r", proxmox_min_version="8.2",
                           templates={}, network=NetworkSpec(internal=InternalNetSpec(enabled=False)))
check("disabled internal net -> None", inet.from_manifest(disabled) is None)
absent = ReleaseManifest(release="t", community_scripts_ref="r", proxmox_min_version="8.2", templates={})
check("no network block -> None", inet.from_manifest(absent) is None)
# plain-dict manifest works too (callers with only parsed YAML)
as_dict = {"network": {"internal": {"enabled": True, "subnet": "198.18.0.0/24",
                                    "assignments": {"ctrlable-pro": 10}}}}
check("dict manifest resolves", inet.from_manifest(as_dict).mqtt_host() == "198.18.0.10")


print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all internal_net checks passed")
