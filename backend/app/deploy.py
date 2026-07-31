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
# [PHASE] 3/6 decompressing image — emitted by the host tool at each step.
_PHASE_RE = re.compile(r"\[PHASE\]\s+(\d+)/(\d+)\s+(.+)")

_LOG_FLUSH_LINES = 20        # flush accumulated log lines to DB every N lines
_LOG_FLUSH_SECONDS = 2.0     # also flush if this many seconds pass without a flush

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
    db: "StateDB | None" = None,
) -> tuple[int, str]:
    """SSH to PVE host, run 'ctrlable-build deploy', return (vmid, kind).

    When `db` is given, output is streamed into the instance row as it arrives
    and [PHASE] markers update the progress fields, so the UI can show what is
    happening. Output still goes to stdout for journald either way.
    """
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

    buf: list[str] = []
    last_flush = asyncio.get_event_loop().time()

    async def _flush() -> None:
        if db is None or not buf:
            return
        chunk = "".join(buf)
        buf.clear()
        await asyncio.to_thread(db.append_instance_log, hostname, chunk)

    async for raw in proc.stdout:
        line = raw.decode(errors="replace")
        print(f"[deploy/{name}] {line}", end="", flush=True)
        buf.append(line)

        m = _RESULT_RE.search(line)
        if m:
            vmid = int(m.group(2))
            if m.group(3):
                kind = m.group(3)

        p = _PHASE_RE.search(line)
        if p and db is not None:
            # Flush first so the log tail and the phase the UI renders agree.
            await _flush()
            await asyncio.to_thread(
                db.update_instance_phase, hostname,
                int(p.group(1)), int(p.group(2)), p.group(3).strip(),
            )
            last_flush = asyncio.get_event_loop().time()
            continue

        now = asyncio.get_event_loop().time()
        if len(buf) >= _LOG_FLUSH_LINES or (now - last_flush) >= _LOG_FLUSH_SECONDS:
            await _flush()
            last_flush = now

    await _flush()
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

    # Create the row BEFORE deploying. The deploy takes minutes, and previously
    # the row only appeared once it finished — so the UI had nothing to show
    # meanwhile, and a failed deploy left no trace at all. vmid is 0 until the
    # host reports the real one in its [RESULT] line.
    await asyncio.to_thread(
        db.create_instance, project_id, template_name, 0, hostname, wire_to, "lxc",
        "provisioning",
    )

    try:
        async with _get_lock():
            vmid, kind = await _ssh_deploy(release, template_name, hostname, settings, db)
    except Exception as exc:
        await asyncio.to_thread(db.append_instance_log, hostname, f"\n[ERROR] {exc}\n")
        await asyncio.to_thread(db.set_instance_status, hostname, "error")
        raise

    await asyncio.to_thread(db.finish_instance, hostname, vmid, kind, "active")
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
