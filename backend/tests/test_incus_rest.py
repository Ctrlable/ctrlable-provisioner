"""Exercise app/incus_rest.py — the direct Incus REST client (P6.1, SPEC §16.1) — against a FAKE
Incus daemon on a real unix socket, plus the pure normalization/parsing helpers. Dependency-free
(stdlib asyncio only), so it runs anywhere — no Incus host, no httpx/fastapi.

Run:  python3 backend/tests/test_incus_rest.py

Fail-first is inherent: before app/incus_rest.py existed this file could not import it (ImportError),
so every check was red until the module landed.
"""
import asyncio
import json
import os
import sys
import tempfile

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _BACKEND)

from app.incus_rest import (  # noqa: E402
    IncusREST, summarize_instance, _http_body, _dechunk,
)

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{('  -- ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


# --- a canned recursion=2 instances payload (one running container) ----------
INSTANCES = [{
    "name": "ctrlable-pro-1",
    "type": "container",
    "status": "Running",
    "status_code": 103,
    "project": "default",
    "state": {
        "pid": 4242,
        "cpu": {"usage": 123456789},
        "memory": {"usage": 536870912, "total": 2147483648},
        "network": {
            "eth0": {"counters": {"bytes_received": 1000, "bytes_sent": 2000}},
            "eth1": {"counters": {"bytes_received": 50, "bytes_sent": 7}},
        },
        "disk": {"root": {"usage": 1073741824}},
    },
}, {
    "name": "frigate-1",
    "type": "container",
    "status": "Stopped",
    "status_code": 102,
    "state": {},  # a stopped instance may carry an empty state — must not crash
}]


def _envelope(metadata) -> bytes:
    body = json.dumps({"type": "sync", "status": "Success",
                       "status_code": 200, "metadata": metadata}).encode()
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body)


async def _fake_incus(reader, writer):
    """Minimal Incus-daemon stand-in: read the request line, reply with the matching envelope."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            break
        data += chunk
    line = data.split(b"\r\n", 1)[0].decode()
    path = line.split(" ")[1] if " " in line else ""
    if path.startswith("/1.0/instances?recursion"):
        writer.write(_envelope(INSTANCES))
    elif path.startswith("/1.0/instances/") and path.endswith("/state"):
        writer.write(_envelope(INSTANCES[0]["state"]))
    elif path.startswith("/1.0/operations"):
        writer.write(_envelope({"running": []}))
    else:
        err = json.dumps({"type": "error", "error": "not found", "error_code": 404}).encode()
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: " + str(len(err)).encode()
                     + b"\r\nConnection: close\r\n\r\n" + err)
    await writer.drain()
    writer.close()


async def run():
    # -- pure helpers (no socket) --------------------------------------------
    s = summarize_instance(INSTANCES[0])
    check("summarize: name/type/status", s["name"] == "ctrlable-pro-1"
          and s["type"] == "container" and s["status"] == "Running")
    check("summarize: cpu usage", s["cpu_usage"] == 123456789, str(s["cpu_usage"]))
    check("summarize: mem usage+total", s["memory_usage"] == 536870912 and s["memory_total"] == 2147483648)
    check("summarize: network summed over NICs", s["network_rx"] == 1050 and s["network_tx"] == 2007,
          f"rx={s['network_rx']} tx={s['network_tx']}")
    check("summarize: disk root usage", s["disk_usage"] == 1073741824, str(s["disk_usage"]))
    s2 = summarize_instance(INSTANCES[1])
    check("summarize: empty state doesn't crash", s2["status"] == "Stopped" and s2["cpu_usage"] is None)

    check("_http_body: content-length body", json.loads(_http_body(_envelope({"x": 1})))["metadata"] == {"x": 1})
    chunked = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
               b"4\r\n{\"a\"\r\n3\r\n:1}\r\n0\r\n\r\n")
    check("_dechunk via _http_body", _http_body(chunked) == '{"a":1}', _http_body(chunked))

    # -- live client against the fake daemon on a real UDS -------------------
    tmp = tempfile.mkdtemp()
    sock = os.path.join(tmp, "incus.sock")
    server = await asyncio.start_unix_server(_fake_incus, path=sock)
    try:
        rest = IncusREST(socket_path=sock, poll_interval=0)
        insts = await rest.list_instances()
        check("list_instances: parses recursion=2 list", isinstance(insts, list) and len(insts) == 2,
              str(type(insts)))
        check("list_instances: names", [i["name"] for i in insts] == ["ctrlable-pro-1", "frigate-1"])

        st = await rest.instance_state("ctrlable-pro-1")
        check("instance_state: pid", st.get("pid") == 4242, str(st.get("pid")))

        snap = await rest.fleet_snapshot()
        check("fleet_snapshot: summarized shape", snap[0]["network_rx"] == 1050 and snap[1]["status"] == "Stopped")

        # events(): first emission is the snapshot, then a diff yields a change event
        rest2 = IncusREST(socket_path=sock, poll_interval=0)
        snaps = [
            [{"name": "a", "status": "Running", "cpu_usage": 1}],
            [{"name": "a", "status": "Running", "cpu_usage": 2}],   # cpu changed → event
            [{"name": "a", "status": "Running", "cpu_usage": 2},
             {"name": "b", "status": "Running"}],                   # b added → event
        ]
        calls = {"n": 0}

        async def fake_snapshot():
            i = min(calls["n"], len(snaps) - 1)
            calls["n"] += 1
            return snaps[i]
        rest2.fleet_snapshot = fake_snapshot  # type: ignore[assignment]

        got = []
        async for ev in rest2.events():
            got.append(ev)
            if len(got) >= 3:
                break
        check("events: first is snapshot", got[0]["type"] == "snapshot", got[0]["type"])
        check("events: emits instance-changed on cpu change",
              any(e["type"] == "instance-changed" and e["name"] == "a" for e in got),
              str([e["type"] for e in got]))
        check("events: emits instance-added for new instance",
              any(e["type"] == "instance-added" and e["name"] == "b" for e in got),
              str([e["type"] for e in got]))
    finally:
        server.close()
        await server.wait_closed()


asyncio.run(run())

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all incus-rest checks passed")
