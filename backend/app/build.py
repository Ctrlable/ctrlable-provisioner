"""
Orchestrator-side release preflight.

Spawns an async task that SSHes to the PVE host and runs
`ctrlable-build preflight <release>`, streaming output line-by-line into the
build log in the DB.

This used to send `ctrlable-build <release>` and expect the host to build
Proxmox templates. The host tool was rewritten to deploy instances directly —
no templates, no clone — and its argument parser only accepts the `deploy` and
`preflight` forms, so the old command died on every run with "Unexpected
SSH_ORIGINAL_COMMAND" and the button could never succeed.

Deploys being direct means nothing validates a release until a guest is half
created, so the useful job here is readiness: is the branded HAOS image staged
and intact, does it match the manifest's pin, are the pinned community-script
builders reachable, is the PVE tooling present. Exit status decides
success/failed on the build record.

[RESULT] parsing below is retained for the template model; preflight emits
[CHECK] lines instead and simply never matches it.
"""
import asyncio
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings
    from .state import StateDB

_RESULT_RE = re.compile(r"\[RESULT\]\s+name=(\S+)\s+vmid=(\d+)(?:\s+kind=(\S+))?")
_running: set[str] = set()   # releases currently building; prevents concurrent builds

_LOG_FLUSH_LINES = 20        # flush accumulated log lines to DB every N lines
_LOG_FLUSH_SECONDS = 2.0     # also flush if this many seconds pass without a flush


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


async def _flush_log(build_id: int, db: "StateDB", buf: list[str]) -> None:
    """Write accumulated log lines to the DB in a thread so the event loop stays free."""
    if not buf:
        return
    chunk = "".join(buf)
    buf.clear()
    await asyncio.to_thread(db.append_build_log, build_id, chunk)


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
        f"ctrlable-build preflight {release}",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,   # closed immediately so remote shell exits cleanly
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    log_buf: list[str] = []
    line_count = 0
    last_flush = asyncio.get_event_loop().time()

    async for raw in proc.stdout:
        line = raw.decode(errors="replace")
        log_buf.append(line)
        line_count += 1

        # Parse [RESULT] lines immediately — don't wait for the flush batch
        m = _RESULT_RE.search(line)
        if m:
            name, vmid = m.group(1), int(m.group(2))
            kind_hint = m.group(3)
            tmpl = await asyncio.to_thread(db.get_template, release, name)
            kind = tmpl["kind"] if tmpl else (kind_hint or "lxc")
            await asyncio.to_thread(
                db.record_template,
                release=release,
                name=name,
                kind=kind,
                template_vmid=vmid,
                app_version=None,
                builder_ref=settings.community_scripts_ref_for(release),
            )
            # Always flush immediately on a result line so the UI updates
            await _flush_log(build_id, db, log_buf)
            line_count = 0
            last_flush = asyncio.get_event_loop().time()
            continue

        # Batch ordinary log lines to avoid per-line DB writes for spinner output
        now = asyncio.get_event_loop().time()
        if line_count >= _LOG_FLUSH_LINES or (now - last_flush) >= _LOG_FLUSH_SECONDS:
            await _flush_log(build_id, db, log_buf)
            line_count = 0
            last_flush = now

    # Flush any remaining buffered lines
    await _flush_log(build_id, db, log_buf)

    await proc.wait()
    await asyncio.to_thread(
        db.finish_build, build_id, "success" if proc.returncode == 0 else "failed"
    )
