"""Address resolution for § 16 probes.

Run:  python3 backend/tests/test_addressing.py

The net0 strings are the real shapes Proxmox emits for an LXC, including the
DHCP case that is the whole reason a second source exists.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.addressing import (  # noqa: E402
    Source, from_proxmox_config, from_reported, resolve,
)
from app.drift import Observed, Severity, evaluate, severity_of  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  -- ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


STATIC = {"net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AA:BB:CC,"
                  "ip=10.1.8.14/24,gw=10.1.8.1,type=veth"}
DHCP = {"net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AA:BB:CC,ip=dhcp,type=veth"}


def main() -> int:
    print("\n1. a static address comes straight off the config")
    a = from_proxmox_config(STATIC)
    check("ip is the host part, not the CIDR", a.ip == "10.1.8.14", str(a.ip))
    check("source is the config", a.source is Source.CONFIG)

    print("\n2. DHCP yields NO address — and says why")
    a = from_proxmox_config(DHCP)
    check("no ip", a.ip is None, str(a.ip))
    check("explains it is intent, not an address", "DHCP" in a.detail, a.detail)

    print("\n3. a management address on net1 is still found")
    # Picking net0 by name would probe the wrong interface here.
    cfg = {"net0": "name=eth0,bridge=vmbr0,ip=dhcp",
           "net1": "name=eth1,bridge=vmbr1,ip=172.16.1.214/24"}
    a = from_proxmox_config(cfg)
    check("found the static one", a.ip == "172.16.1.214", str(a.ip))
    check("names which interface", "net1" in a.detail, a.detail)

    print("\n4. the guest's own report covers DHCP")
    a = from_reported("10.1.8.45")
    check("ip", a.ip == "10.1.8.45")
    check("source is first-boot", a.source is Source.REPORTED)

    print("\n5. a guest that reported loopback is REFUSED, not trusted")
    # It resolved its own hostname badly. Probing 127.0.0.1 from the
    # orchestrator reads the ORCHESTRATOR's services and reports them as the
    # guest's — a wrong answer that looks like a right one.
    a = from_reported("127.0.0.1")
    check("no ip", a.ip is None, str(a.ip))
    check("says it is not routable", "not routable" in a.detail, a.detail)
    check("garbage is refused too", from_reported("not-an-ip").ip is None)

    print("\n6. layering: config wins when both exist")
    a = resolve(STATIC, "10.1.8.99")
    check("static beats reported", a.ip == "10.1.8.14", str(a.ip))
    check("source recorded", a.source is Source.CONFIG)

    print("\n7. layering: reported covers what config cannot")
    a = resolve(DHCP, "10.1.8.45")
    check("falls back", a.ip == "10.1.8.45", str(a.ip))
    check("source recorded as first-boot", a.source is Source.REPORTED)
    check("a drift row can say HOW it reached the guest",
          a.source.value == "first-boot", a.source.value)

    print("\n8. neither source -> unresolved, never a guess")
    a = resolve(DHCP, None)
    check("no ip", a.ip is None)
    check("source NONE", a.source is Source.NONE)
    check("falsy, so callers cannot use it by accident", not a)

    print("\n9. unresolved must read as UNKNOWN downstream, not OK")
    # The join between this module and drift: no address means no probe,
    # and a component that was never probed is not healthy.
    class Req:
        zwave_js_api_schema = 51
        zwave_js_server = None
        gated_across = None

    class Spec:
        app_version = "11.25.2"
        requires = Req()

    a = resolve({}, None)
    observed = Observed(probe_error=f"no address: {a.detail}")
    f = evaluate("zwavejs", Spec(), observed)
    check("severity is UNKNOWN", severity_of(f) is Severity.UNKNOWN,
          severity_of(f).name)
    check("never OK", severity_of(f) is not Severity.OK)

    print("\n10. a stale reading is usable but flagged")
    a = from_reported("10.1.8.45", stale=True)
    check("still returns the ip", a.ip == "10.1.8.45")
    check("marked stale for the operator", "stale" in a.detail, a.detail)

    print("\n" + ("ALL PASS" if not failures
                  else f"{len(failures)} FAILURE(S): {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
