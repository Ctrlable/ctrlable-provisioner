"""In-process pub/sub: deploy progress -> the live SSE event stream (SPEC §16.1, P6.2).

deploy.py emits `[PHASE] n/tot msg` and `[RESULT]` markers as it provisions. Historically those
only reached the UI by writing the instances table and letting the browser poll. This bus lets
`/api/incus/events` MULTIPLEX them onto the same Server-Sent-Events stream that already carries the
Incus change feed, so the live fleet view shows deploy progress the instant it happens.

THREAD-SAFE ON PURPOSE: the Incus deploy path runs its `on_phase` callback inside a worker thread
(`asyncio.to_thread`), while the SSH path runs it on the event loop. `publish()` therefore hops
back onto the captured loop with `call_soon_threadsafe`, so either caller is safe. A full
subscriber queue DROPS the event for that subscriber (progress is advisory — never stall a deploy).
"""
from __future__ import annotations

import asyncio
from typing import Any

_subscribers: "set[asyncio.Queue]" = set()
_loop: "asyncio.AbstractEventLoop | None" = None


def _capture_loop() -> None:
    global _loop
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        pass


def subscribe() -> "asyncio.Queue":
    """Called from the SSE generator (on the loop) — also captures the loop for cross-thread publish."""
    _capture_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    _subscribers.add(q)
    return q


def unsubscribe(q: "asyncio.Queue") -> None:
    _subscribers.discard(q)


def publish(event: "dict[str, Any]") -> None:
    """Fan one event out to every subscriber, safe to call from any thread."""
    def _fan() -> None:
        for q in list(_subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # slow subscriber — drop, don't block the deployer
                pass
    loop = _loop
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(_fan)
    else:  # no live loop / no subscribers yet — best-effort inline
        _fan()
