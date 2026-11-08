"""Per-instance drift scan orchestration (SPEC § 16 / § 17.5, milestone 9).

Run:  python3 backend/tests/test_drift_scan.py

Standalone, like the sibling tests. The scan is executed against FAKE transports
— a schema probe, an exec reader and a config reader — so the join logic runs
with no Proxmox host and no live socket, and the cases are the real ones:
Pinecrest before the fix (schema 50 against a Core pinning 51) and after.

The point under test is the contract the outage violated: a dimension we could
not reach must read UNKNOWN, never a false OK, and a live schema mismatch must
sort to the top as OUTAGE.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.drift import Observed  # noqa: E402
from app.drift_scan import scan_instance, scan_project  # noqa: E402
from app.manifest import Requires, TemplateSpec, VersionProbe  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  -- ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


# ----- the real manifest specs, built directly (no YAML needed) -------------

ZWAVEJS = TemplateSpec(
    kind="lxc", builder="ct/zwave-js-ui.sh", app_version="11.25.2",
    version_probe=VersionProbe(kind="exec", command="zwave-js-ui --version"),
    requires=Requires(zwave_js_server=">=3.11.0", zwave_js_api_schema=51),
)
Z2M = TemplateSpec(
    kind="lxc", builder="ct/zigbee2mqtt.sh", app_version="2.14.3",
    version_probe=VersionProbe(kind="exec", command="cat package.json",
                               parse=r'"version"\s*:\s*"([0-9.]+)"'),
    requires=Requires(gated_across="2.0.0"),
)
SPECS = {"zwavejs": ZWAVEJS, "zigbee2mqtt": Z2M}

STATIC_CFG = {"net0": "name=eth0,bridge=vmbr0,ip=10.1.8.14/24,gw=10.1.8.1"}
DHCP_CFG = {"net0": "name=eth0,bridge=vmbr0,ip=dhcp"}


def inst(**over) -> dict:
    row = {"id": 1, "hostname": "ctrlable-pinecrest-zwavejs", "type": "zwavejs",
           "vmid": 104, "kind": "lxc", "status": "active", "reported_ip": None}
    row.update(over)
    return row


def schema(n):
    """A fake schema probe that answers n regardless of host/port."""
    return lambda host, port: Observed(zwave_js_api_schema=n,
                                        zwave_js_server="3.x")


def exec_returning(out, rc=0):
    return lambda vmid, kind, command: (rc, out)


def main() -> int:
    print("\n1. the Pinecrest outage surfaces as an OUTAGE row")
    # schema 50 live, pinned 11.25.2, box still on the stale 11.21.1.
    r = scan_instance(inst(), ZWAVEJS,
                      get_config=lambda v, k: STATIC_CFG,
                      schema_probe=schema(50),
                      exec_version=exec_returning("11.21.1"))
    check("severity OUTAGE", r.severity == "OUTAGE", r.severity)
    check("live schema recorded as 50", r.schema_live == 50, str(r.schema_live))
    check("required schema 51", r.schema_required == 51, str(r.schema_required))
    check("address resolved from config", r.address == "10.1.8.14", str(r.address))
    check("address source is the config", r.address_source == "proxmox-config",
          r.address_source)
    check("a finding says it will not load",
          any("will not load" in f["detail"] for f in r.findings), str(r.findings))

    print("\n2. after the fix it is clean (schema 51 + version matches)")
    r = scan_instance(inst(), ZWAVEJS,
                      get_config=lambda v, k: STATIC_CFG,
                      schema_probe=schema(51),
                      exec_version=exec_returning("11.25.2"))
    check("severity OK", r.severity == "OK", r.severity)
    check("no findings", r.findings == [], str(r.findings))
    check("live version read", r.live_version == "11.25.2", str(r.live_version))

    print("\n3. schema OK but NO exec transport -> UNKNOWN, never a false OK")
    # The capability that breaks the site (schema) is verified fine; we simply
    # cannot read the version string. That is UNKNOWN, and must NOT read OK.
    r = scan_instance(inst(), ZWAVEJS,
                      get_config=lambda v, k: STATIC_CFG,
                      schema_probe=schema(51),
                      exec_version=None)
    check("severity UNKNOWN", r.severity == "UNKNOWN", r.severity)
    check("not OUTAGE — schema was fine", r.severity != "OUTAGE", r.severity)
    check("schema still recorded as 51", r.schema_live == 51, str(r.schema_live))
    check("live version is None, not invented", r.live_version is None,
          str(r.live_version))

    print("\n4. no address -> UNKNOWN, and no schema is invented")
    # DHCP config (declares intent, not an address) and nothing reported.
    calls: list = []
    r = scan_instance(inst(), ZWAVEJS,
                      get_config=lambda v, k: DHCP_CFG,
                      schema_probe=lambda h, p: (calls.append((h, p))
                                                 or Observed(zwave_js_api_schema=51)),
                      exec_version=exec_returning("11.25.2"))
    check("severity UNKNOWN", r.severity == "UNKNOWN", r.severity)
    check("the schema probe was never called", calls == [], str(calls))
    check("schema_live stays None", r.schema_live is None, str(r.schema_live))

    print("\n5. a guest that is not active is not probed")
    calls = []
    r = scan_instance(inst(status="provisioning"), ZWAVEJS,
                      get_config=lambda v, k: STATIC_CFG,
                      schema_probe=lambda h, p: (calls.append(1)
                                                 or Observed(zwave_js_api_schema=51)),
                      exec_version=exec_returning("11.25.2"))
    check("severity UNKNOWN", r.severity == "UNKNOWN", r.severity)
    check("probe not called on a non-active guest", calls == [], str(calls))
    check("finding explains it is not active",
          any("not active" in f["detail"] for f in r.findings), str(r.findings))

    print("\n6. zigbee2mqtt behind its pin -> DRIFT, not OUTAGE")
    # No schema requirement; version 2.9.1 vs pinned 2.14.3.
    r = scan_instance(inst(id=2, hostname="pc-z2m", type="zigbee2mqtt", vmid=105),
                      Z2M, get_config=lambda v, k: STATIC_CFG,
                      schema_probe=schema(51),
                      exec_version=exec_returning('{"version":"2.9.1"}'))
    check("severity DRIFT", r.severity == "DRIFT", r.severity)
    check("live version parsed from package.json", r.live_version == "2.9.1",
          str(r.live_version))
    check("no schema columns for z2m", r.schema_required is None, str(r.schema_required))

    print("\n7. DHCP + a first-boot report resolves via first-boot")
    r = scan_instance(inst(reported_ip="10.1.8.45"), ZWAVEJS,
                      get_config=lambda v, k: DHCP_CFG,
                      schema_probe=schema(51),
                      exec_version=exec_returning("11.25.2"))
    check("address is the reported one", r.address == "10.1.8.45", str(r.address))
    check("source is first-boot", r.address_source == "first-boot", r.address_source)

    print("\n8. an instance of a template no longer in the manifest -> UNKNOWN")
    r = scan_instance(inst(type="frigate"), None)
    check("severity UNKNOWN", r.severity == "UNKNOWN", r.severity)
    check("finding names the missing manifest entry",
          any(f["field"] == "template" for f in r.findings), str(r.findings))

    print("\n9. scan_project sorts the outage to the top")
    rows = scan_project(
        [inst(id=1, type="zigbee2mqtt", hostname="z2m"),
         inst(id=2, type="zwavejs", hostname="zw")],
        SPECS, get_config=lambda v, k: STATIC_CFG,
        # z2m clean, zwavejs in outage
        schema_probe=schema(50),
        exec_version=lambda v, k, c: (0, '{"version":"2.14.3"}')
        if "package.json" in c or "cat" in c else (0, "11.25.2"))
    check("worst severity first", rows[0].severity == "OUTAGE", rows[0].severity)
    check("the outage row is the zwavejs one", rows[0].template == "zwavejs",
          rows[0].template)

    print("\n" + ("ALL PASS" if not failures
                  else f"{len(failures)} FAILURE(S): {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
