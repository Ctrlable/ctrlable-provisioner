"""
Deploy pipeline — direct deploy (no template+clone).

For each template in the manifest the orchestrator SSHes to the PVE host and runs:
  ctrlable-build deploy <release> <template_name> <hostname>

The host script creates the instance, injects firstboot, and starts it.
A [RESULT] line is emitted on success with the assigned VMID and kind.
"""
import asyncio
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings
    from .state import StateDB

_RESULT_RE = re.compile(r"\[RESULT\]\s+name=(\S+)\s+vmid=(\d+)(?:\s+kind=(\S+))?")

# Only one deploy runs at a time — prevents VMID collision when parallel add_instance
# calls are made. deploy_stack_async already runs sequentially, but this guards
# individual add_instance API calls too.
_deploy_lock: asyncio.Lock | None = None

def _get_lock() -> asyncio.Lock:
    global _deploy_lock
    if _deploy_lock is None:
        _deploy_lock = asyncio.Lock()
    return _deploy_lock



def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _next_hostname(site: str, template: str, existing: set[str]) -> str:
    base = f"ctrlable-{_slug(site)}-{template}"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _wire_to_for(template_name: str, shared: dict) -> dict:
    base = {
        k: shared[k]
        for k in ("mqtt_host", "mqtt_port", "mqtt_user", "mqtt_pass")
        if shared.get(k) is not None
    }
    if template_name == "zigbee2mqtt" and shared.get("zigbee_coordinator"):
        base["coordinator_url"] = shared["zigbee_coordinator"]
    if template_name == "zwavejs" and shared.get("zwave_coordinator"):
        base["coordinator_url"] = shared["zwave_coordinator"]
    return base


async def _ssh_deploy(
    release: str,
    name: str,
    hostname: str,
    settings: "Settings",
) -> tuple[int, str]:
    """SSH to PVE host, run 'ctrlable-build deploy', return (vmid, kind)."""
    cmd = [
        "ssh",
        "-i", settings.build_key_path,
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=120",  # tolerate up to 1 hour of silence (community scripts are slow)
        f"root@{settings.pve_host}",
        f"ctrlable-build deploy {release} {name} {hostname}",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    vmid: int | None = None
    kind: str = "lxc"

    async for raw in proc.stdout:
        line = raw.decode(errors="replace")
        print(f"[deploy/{name}] {line}", end="", flush=True)
        m = _RESULT_RE.search(line)
        if m:
            vmid = int(m.group(2))
            if m.group(3):
                kind = m.group(3)

    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"deploy failed for {name!r} (exit {proc.returncode})")
    if vmid is None:
        raise RuntimeError(f"deploy for {name!r} produced no [RESULT] line")
    return vmid, kind


async def deploy_instance(
    project_id: int,
    template_name: str,
    release: str,
    wire_to: dict,
    db: "StateDB",
    settings: "Settings",
) -> dict:
    project = await asyncio.to_thread(db.get_project, project_id)
    if not project:
        raise ValueError(f"project {project_id} not found")

    existing = await asyncio.to_thread(db.list_all_instances)
    existing_hostnames = {r["hostname"] for r in existing}
    hostname = _next_hostname(project["site_name"], template_name, existing_hostnames)

    async with _get_lock():
        vmid, kind = await _ssh_deploy(release, template_name, hostname, settings)

    await asyncio.to_thread(
        db.create_instance, project_id, template_name, vmid, hostname, wire_to, kind
    )
    return dict(await asyncio.to_thread(db.get_instance_by_hostname, hostname))


async def deploy_stack_async(
    project_id: int,
    release: str,
    shared_wire_to: dict,
    db: "StateDB",
    settings: "Settings",
) -> None:
    from .manifest import load_all_manifests
    manifests = load_all_manifests()
    manifest = manifests[release]

    for name in manifest.templates:
        wire_to = _wire_to_for(name, shared_wire_to)
        try:
            await deploy_instance(project_id, name, release, wire_to, db, settings)
        except Exception as exc:
            print(f"[deploy] {name} failed: {exc}", flush=True)
