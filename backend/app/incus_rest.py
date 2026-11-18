"""Direct Incus REST client over the proxied unix socket — the P6.1 keystone (SPEC §16.1).

WHY THIS EXISTS (distinct from IncusProvider):
  IncusProvider shells the `incus` CLI: request/response only, one process per call, cannot
  stream. The management UI (§16.1) needs LIVE data — fleet state that updates without the
  browser polling, operation progress, console/exec, metrics. Those live on the Incus REST API's
  streaming endpoints (`/1.0/events`, console/exec websockets, `/1.0/metrics`). This module adds
  the REST path: async HTTP/1.1 spoken straight over `/var/lib/incus/unix.socket` (the host daemon
  socket, proxied into the orchestrator container at that default path — SPEC §4). It is purely
  additive; the CLI provider stays the owner of lifecycle/deploy.

DEPENDENCY-FREE ON PURPOSE: stdlib `asyncio` only (no httpx/aiohttp), so it runs unchanged in the
orchestrator container and under the repo's dep-free test harness. HTTP/1.1 + `Connection: close`
(read to EOF) is all the Incus sync API needs; no third-party HTTP client.

FASTAPI-FREE ON PURPOSE: this module imports nothing from fastapi, so it is unit-testable in a bare
environment. The HTTP endpoints that expose it live in main.py as thin wrappers.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

DEFAULT_SOCKET = "/var/lib/incus/unix.socket"


class IncusError(RuntimeError):
    pass


class IncusREST:
    """Minimal async Incus REST client over a unix socket.

    Only the P6.1 surface is implemented: list instances (recursion=2 → state inline),
    per-instance state, a normalized fleet snapshot, and a live `events()` async generator.
    """

    def __init__(self, socket_path: str = DEFAULT_SOCKET, poll_interval: float = 3.0,
                 connect_timeout: float = 10.0):
        self.socket_path = socket_path
        self.poll_interval = poll_interval
        self.connect_timeout = connect_timeout

    # -- raw HTTP/1.1 over the unix socket -----------------------------------

    async def _request(self, method: str, path: str, body: "dict | None" = None) -> dict:
        """One request/response over a fresh UDS connection (Connection: close → read to EOF).
        Returns the parsed Incus envelope dict ({"type": "sync"|"error", "metadata": ...})."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path), self.connect_timeout)
        except (OSError, asyncio.TimeoutError) as e:
            raise IncusError(f"cannot connect to Incus socket {self.socket_path}: {e}") from e
        try:
            payload = b"" if body is None else json.dumps(body).encode()
            lines = [
                f"{method} {path} HTTP/1.1",
                "Host: incus",
                "Accept: application/json",
                "User-Agent: ctrlable-orchestrator",
                "Connection: close",
            ]
            if body is not None:
                lines += ["Content-Type: application/json", f"Content-Length: {len(payload)}"]
            writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + payload)
            await writer.drain()
            raw = await reader.read()  # Connection: close → server EOFs after the body
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 — closing a half-open socket is best-effort
                pass

        body = _http_body(raw)
        if not body:
            raise IncusError(f"empty response from Incus for {path}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise IncusError(f"non-JSON response from Incus for {path}: {body[:160]!r}") from e

    async def _sync(self, path: str) -> Any:
        """GET a sync endpoint and return its metadata, raising on an Incus error envelope."""
        env = await self._request("GET", path)
        if env.get("type") == "error":
            raise IncusError(f"Incus error for {path}: {env.get('error')} ({env.get('error_code')})")
        return env.get("metadata")

    # -- the P6.1 surface -----------------------------------------------------

    async def list_instances(self) -> list[dict]:
        """GET /1.0/instances?recursion=2 → full instance records (config + inline state)."""
        md = await self._sync("/1.0/instances?recursion=2")
        return md or []

    async def instance_state(self, name: str) -> dict:
        md = await self._sync(f"/1.0/instances/{name}/state")
        return md or {}

    async def instance(self, name: str) -> dict:
        """Full instance record (config + devices + expanded_devices + inline state) for the
        detail drawer — GET /1.0/instances/<name>?recursion=1. recursion=1 inlines `state`."""
        md = await self._sync(f"/1.0/instances/{name}?recursion=1")
        return md or {}

    async def resources(self) -> dict:
        """Host hardware over the socket — GET /1.0/resources (cpu, memory, storage, usb, pci,
        gpu). This is the REAL host's hardware (what to pass through), not the container's."""
        md = await self._sync("/1.0/resources")
        return md or {}

    async def exec_setup(self, name: str, command: "list | None" = None,
                         width: int = 80, height: int = 24) -> dict:
        """Start an interactive exec (POST /1.0/instances/<name>/exec, wait-for-websocket). Returns
        the async operation envelope; metadata.metadata.fds carry the per-stream WS secrets, and
        `operation` is the op path. The WS attach (over the UDS) runs the command — see the console
        endpoint in main.py."""
        body = {
            "command": command or ["/bin/sh"],
            "wait-for-websocket": True,
            "interactive": True,
            "environment": {"TERM": "xterm-256color"},
            "width": int(width), "height": int(height),
        }
        env = await self._request("POST", f"/1.0/instances/{name}/exec", body)
        if env.get("type") == "error":
            raise IncusError(f"exec {name}: {env.get('error')} ({env.get('error_code')})")
        return env

    async def operations(self) -> dict:
        """GET /1.0/operations?recursion=1 → running/queued operations (for progress + events)."""
        try:
            return await self._sync("/1.0/operations?recursion=1") or {}
        except IncusError:
            return {}

    async def fleet_snapshot(self) -> list[dict]:
        """Normalized instance list for the live fleet view — the /api/fleet/live payload."""
        return [summarize_instance(i) for i in await self.list_instances()]

    async def events(self) -> "AsyncIterator[dict]":
        """Live change stream for the SSE relay.

        POLL FALLBACK (deliberate for the P6.1 spike): Incus's real `/1.0/events` is a WEBSOCKET
        upgrade over the socket; a hand-rolled WS-over-UDS client (handshake + frame masking) is
        more than a spike warrants. Instead we poll `fleet_snapshot()` (+ operations) on an
        interval, diff against the last snapshot, and emit synthesized change events. This proves
        the end-to-end live path (SSE → browser) with zero fragility.

        TODO(P6.2): replace with a real `/1.0/events?type=lifecycle,operation,logging` websocket
        subscription (use `websockets`/`aiohttp` ws over the UDS, or a small stdlib WS client) so
        events are pushed by the daemon instead of discovered by polling.
        """
        prev: dict[str, dict] = {}
        first = True
        while True:
            try:
                snap = {s["name"]: s for s in await self.fleet_snapshot()}
            except IncusError as e:
                yield {"type": "error", "error": str(e)}
                await asyncio.sleep(self.poll_interval)
                continue

            if first:
                yield {"type": "snapshot", "instances": list(snap.values())}
                first = False
            else:
                for name, cur in snap.items():
                    old = prev.get(name)
                    if old is None:
                        yield {"type": "instance-added", "name": name, "instance": cur}
                    elif _changed(old, cur):
                        yield {"type": "instance-changed", "name": name, "instance": cur,
                               "from": {k: old.get(k) for k in _WATCH}}
                for name in prev.keys() - snap.keys():
                    yield {"type": "instance-removed", "name": name}
            prev = snap
            await asyncio.sleep(self.poll_interval)


# -- normalization helpers (pure functions — unit-tested) --------------------

# Fields whose change is worth emitting an event for (the poll-diff watch set).
_WATCH = ("status", "cpu_usage", "memory_usage", "network_rx", "network_tx", "disk_usage")


def _changed(old: dict, cur: dict) -> bool:
    return any(old.get(k) != cur.get(k) for k in _WATCH)


def summarize_instance(inst: dict) -> dict:
    """Flatten a recursion=2 instance record into the compact shape the fleet view renders.

    recursion=2 inlines `state`; be defensive — a stopped/erroring instance may omit sections.
    """
    state = inst.get("state") or {}
    cpu = (state.get("cpu") or {})
    mem = (state.get("memory") or {})
    net = state.get("network") or {}
    rx = tx = 0
    for _iface, nd in net.items():
        counters = (nd or {}).get("counters") or {}
        rx += counters.get("bytes_received") or 0
        tx += counters.get("bytes_sent") or 0
    disk = state.get("disk") or {}
    root_disk = (disk.get("root") or {}) if isinstance(disk, dict) else {}

    return {
        "name": inst.get("name"),
        "type": inst.get("type"),                       # "container" | "virtual-machine"
        "status": inst.get("status"),                   # Running | Stopped | ...
        "status_code": inst.get("status_code"),
        "cpu_usage": cpu.get("usage"),                  # nanoseconds (cumulative)
        "memory_usage": mem.get("usage"),               # bytes
        "memory_total": mem.get("total"),               # bytes (may be None)
        "network_rx": rx,                               # bytes (summed over NICs)
        "network_tx": tx,
        "disk_usage": root_disk.get("usage"),           # bytes (may be None)
        "pid": state.get("pid"),
        "project": inst.get("project"),
    }


def _http_body(raw: bytes) -> str:
    """Extract the body from an HTTP/1.1 response, handling chunked transfer-encoding.

    The Incus sync API usually sends Content-Length, but with `Connection: close` the body is
    simply everything after the header terminator; handle `Transfer-Encoding: chunked` too in case
    the daemon streams it.
    """
    sep = raw.find(b"\r\n\r\n")
    if sep == -1:
        return raw.decode("utf-8", "replace")
    head = raw[:sep].decode("iso-8859-1")
    body = raw[sep + 4:]
    if "transfer-encoding: chunked" in head.lower():
        body = _dechunk(body)
    return body.decode("utf-8", "replace")


def _dechunk(body: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(body):
        nl = body.find(b"\r\n", i)
        if nl == -1:
            break
        try:
            size = int(body[i:nl].split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        start = nl + 2
        out += body[start:start + size]
        i = start + size + 2
    return bytes(out)
