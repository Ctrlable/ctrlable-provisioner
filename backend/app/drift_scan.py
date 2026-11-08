"""Per-instance drift scan — the orchestration M9 adds on top of the pure parts.

`drift.py` compares one observation against one spec; `addressing.py` resolves a
guest's address. This module is the loop that joins them to the live fleet: for
every instance in a project it finds the template's manifest spec, resolves the
guest's address, gathers what it can OBSERVE, compares, and returns one row per
instance carrying a headline severity and the three columns a dealer reads
(§ 17.5): is it working / is it what we pinned / where did we reach it.

Transport is INJECTED, never imported, for the same reason `drift.py` owns none:
the scan is then exercised against fakes with no Proxmox host and no live socket,
and — the point of the whole exercise — a dimension we could not reach reads as
UNKNOWN rather than a false OK.

Two observations, gathered differently:

    schema   The Z-Wave JS server's `maxSchemaVersion`, read straight off its
             websocket handshake over TCP. This is the capability that unloaded
             `zwave_js` at Pinecrest, and it needs no exec, no host trigger and
             no in-guest agent — just a connection the orchestrator already has.
             Fully determinable today.

    version  The installed app_version, which for a community `ct/` guest can
             only be read by running a command INSIDE it (§ 16.3 `version_probe`,
             kind: exec). That transport is the open § 18 question and is not
             wired to the restricted host key yet, so `exec_version` is injected
             and, when absent, the version dimension is reported UNKNOWN — the
             spec's contract: unprobed is never healthy.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .addressing import Address, resolve
from .drift import Finding, Observed, Severity, evaluate, probe_zwave_js, severity_of

# The port the Z-Wave JS server listens on inside the guest. Fixed across our
# builds; carried here rather than in the manifest because it is a property of
# the component, not of a release.
ZWAVE_JS_PORT = 3000

# Signatures of the injected transports. Defaults wire the real ones (or None).
SchemaProbe = Callable[[str, int], Observed]
#   exec_version(vmid, kind, command) -> (returncode, stdout), or None when no
#   exec transport is available for this guest. None is NOT an error — it means
#   "cannot read the installed version here", which surfaces as UNKNOWN.
ExecVersion = Callable[[int, str, str], Optional[tuple[int, str]]]
#   get_config(vmid, kind) -> provider config dict for the guest (net0 etc.).
GetConfig = Callable[[int, str], dict]


@dataclass
class InstanceDrift:
    """One row of the drift table. Flat and JSON-ready: this is what the API
    returns and the panel renders, so it carries both the headline and the
    evidence behind it."""

    instance_id: int
    hostname: str
    template: str
    vmid: Optional[int]
    status: str
    severity: str                      # OK | UNKNOWN | DRIFT | OUTAGE
    address: Optional[str]
    address_source: str
    pinned_version: Optional[str]
    live_version: Optional[str]
    schema_required: Optional[int]
    schema_live: Optional[int]
    findings: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _version_from_probe(spec, exec_version: Optional[ExecVersion],
                        vmid: Optional[int], kind: str) -> tuple[Optional[str], Optional[str]]:
    """Read the installed version via the declared `version_probe`, if we can.

    Returns (version, error). A missing probe declaration, a missing transport
    or a non-matching output all yield (None, reason) — never a guess. `evaluate`
    turns a None version into an UNKNOWN finding, not an OK one.
    """
    probe = getattr(spec, "version_probe", None)
    if probe is None:
        return None, "no version_probe declared"
    if probe.kind != "exec":
        return None, f"version_probe kind {probe.kind!r} not yet supported"
    if exec_version is None or vmid is None:
        return None, "no exec transport to the guest"
    try:
        rc, out = exec_version(vmid, kind, probe.command)
    except Exception as err:  # transport failure is UNKNOWN, not a false reading
        return None, f"exec failed: {type(err).__name__}: {err}"
    if rc != 0:
        return None, f"version command exited {rc}"
    m = re.search(probe.parse, out or "")
    if not m:
        return None, "version command output did not match the parse pattern"
    return (m.group(1) if m.groups() else m.group(0)), None


def gather(spec, address: Address, *, vmid: Optional[int], kind: str,
           status: str,
           schema_probe: SchemaProbe = probe_zwave_js,
           exec_version: Optional[ExecVersion] = None) -> Observed:
    """Observe one guest as far as the available transports allow.

    A guest that is not `active` is not probed at all — a half-built or stopped
    guest that fails a probe is not drift, it is just not up — and surfaces as
    UNKNOWN with a reason rather than a spurious OUTAGE.
    """
    if status != "active":
        return Observed(probe_error=f"instance is {status}, not active")

    req = getattr(spec, "requires", None)
    wants_schema = req is not None and getattr(req, "zwave_js_api_schema", None) is not None

    # The schema capability needs an address; if we have none, say why and stop,
    # because a schema we cannot read must read as UNKNOWN, not as matching.
    if wants_schema and not address:
        return Observed(probe_error=f"no address: {address.detail}")

    observed = Observed()
    if wants_schema:
        probed = schema_probe(address.ip, ZWAVE_JS_PORT)
        if probed.probe_error:
            # The schema is the capability that actually breaks the site, so a
            # failure to read it is the whole finding — report it and stop.
            return probed
        observed = probed

    # Version is a softer signal (DRIFT, not OUTAGE); read it if we can, and
    # fold a failure into `extra` rather than masking the schema result.
    version, verr = _version_from_probe(spec, exec_version, vmid, kind)
    observed.app_version = version
    if verr:
        observed.extra["version_probe"] = verr
    return observed


def scan_instance(instance: dict, spec, *,
                  get_config: Optional[GetConfig] = None,
                  schema_probe: SchemaProbe = probe_zwave_js,
                  exec_version: Optional[ExecVersion] = None) -> InstanceDrift:
    """Resolve, observe and compare a single instance.

    `instance` is a mapping with at least id, hostname, type, vmid, kind,
    status, reported_ip (an sqlite3.Row works — it is dict-accessed here).
    `spec` is the template's manifest spec, or None when the manifest no longer
    carries this template (itself a kind of drift: an instance of something we
    no longer ship).
    """
    vmid = instance["vmid"]
    kind = instance["kind"] if instance["kind"] else "lxc"
    status = instance["status"]
    hostname = instance["hostname"]

    if spec is None:
        return InstanceDrift(
            instance_id=instance["id"], hostname=hostname,
            template=instance["type"], vmid=vmid, status=status,
            severity=Severity.UNKNOWN.name, address=None,
            address_source="n/a", pinned_version=None, live_version=None,
            schema_required=None, schema_live=None,
            findings=[{"severity": "UNKNOWN", "field": "template",
                       "expected": "a manifest entry", "observed": "none",
                       "detail": f"{instance['type']} is not in this release's "
                                 "manifest; cannot say what it should be"}],
        )

    # Address: config first (static, authoritative), then first-boot report.
    config: dict = {}
    if get_config is not None and vmid is not None:
        try:
            config = get_config(vmid, kind)
        except Exception:
            config = {}  # unreachable host -> fall back to the reported address
    reported_ip = instance["reported_ip"] if "reported_ip" in instance.keys() else None
    address = resolve(config, reported_ip)

    observed = gather(spec, address, vmid=vmid, kind=kind, status=status,
                      schema_probe=schema_probe, exec_version=exec_version)
    findings = evaluate(instance["type"], spec, observed)

    req = getattr(spec, "requires", None)
    return InstanceDrift(
        instance_id=instance["id"], hostname=hostname,
        template=instance["type"], vmid=vmid, status=status,
        severity=severity_of(findings).name,
        address=address.ip, address_source=address.source.value,
        pinned_version=getattr(spec, "app_version", None),
        live_version=observed.app_version,
        schema_required=getattr(req, "zwave_js_api_schema", None) if req else None,
        schema_live=observed.zwave_js_api_schema,
        findings=[{"severity": f.severity.name, "field": f.field,
                   "expected": f.expected, "observed": f.observed,
                   "detail": f.detail} for f in findings],
    )


def scan_project(instances: list, specs: dict, *,
                 get_config: Optional[GetConfig] = None,
                 schema_probe: SchemaProbe = probe_zwave_js,
                 exec_version: Optional[ExecVersion] = None) -> list[InstanceDrift]:
    """Scan every instance of a project.

    `instances` are the project's instance rows; `specs` maps template name ->
    manifest spec (release.templates). Rows come back worst-severity first so a
    live outage sorts to the top of the table.
    """
    rows = [
        scan_instance(inst, specs.get(inst["type"]),
                      get_config=get_config, schema_probe=schema_probe,
                      exec_version=exec_version)
        for inst in instances
    ]
    rows.sort(key=lambda r: Severity[r.severity], reverse=True)
    return rows
