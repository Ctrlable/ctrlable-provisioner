"""
Orchestrator-side build trigger.

Spawns an async task that SSHes to the PVE host and runs ctrlable-build <release>.
Output is streamed line-by-line into the build log in the DB.
Result lines emitted by the host script (prefixed [RESULT]) are parsed to record
template VMIDs.
"""
import asyncio
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings
    from .state import StateDB

_RESULT_RE = re.compile(r"\[RESULT\]\s+name=(\S+)\s+vmid=(\d+)(?:\s+kind=(\S+))?")
_running: set[str] = set()   # releases currently building; prevents concurrent builds


async def trigger_build(release: str, db: "StateDB", settings: "Settings") -> int:
    if release in _running:
        raise RuntimeError(f"Build for {release} is already running")
    build_id = db.create_build(release)
    asyncio.create_task(_run_build(build_id, release, db, settings))
    return build_id


async def _run_build(build_id: int, release: str, db: "StateDB", settings: "Settings") -> None:
    _running.add(release)
    try:
        await _execute(build_id, release, db, settings)
    finally:
        _running.discard(release)


async def _execute(build_id: int, release: str, db: "StateDB", settings: "Settings") -> None:
    cmd = [
        "ssh",
        "-i", settings.build_key_path,
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",   # keepalive every 30s
        "-o", "ServerAliveCountMax=60",   # tolerate 30 min of silence
        f"root@{settings.pve_host}",
        f"ctrlable-build {release}",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async for raw in proc.stdout:
        line = raw.decode(errors="replace")
        db.append_build_log(build_id, line)

        m = _RESULT_RE.search(line)
        if m:
            name, vmid = m.group(1), int(m.group(2))
            kind_hint = m.group(3)  # emitted by ctrlable-build as kind=lxc or kind=qemu
            tmpl = db.get_template(release, name)
            kind = tmpl["kind"] if tmpl else (kind_hint or "lxc")
            db.record_template(
                release=release,
                name=name,
                kind=kind,
                template_vmid=vmid,
                app_version=None,
                builder_ref=settings.community_scripts_ref_for(release),
            )

    await proc.wait()
    db.finish_build(build_id, "success" if proc.returncode == 0 else "failed")
