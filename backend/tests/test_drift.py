"""Drift comparison and the Z-Wave JS capability probe (SPEC § 16).

Run:  python3 backend/tests/test_drift.py

Standalone, like host/tests. The comparison is pure, and the probe is
exercised against a real socket serving a recorded handshake — so the frame
parsing is executed rather than asserted about.

The cases are the real ones: Pinecrest before the fix (schema 50 against a
Core pinning 51 -> OUTAGE) and after (51 -> OK).
"""
import pathlib
import socket
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.drift import (  # noqa: E402
    Observed, Severity, evaluate, probe_zwave_js, severity_of,
)

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  -- ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


class Req:
    def __init__(self, schema=None, server=None, gated=None):
        self.zwave_js_api_schema = schema
        self.zwave_js_server = server
        self.gated_across = gated


class Spec:
    def __init__(self, app_version=None, requires=None):
        self.app_version = app_version
        self.requires = requires


# --------------------------------------------------------------- comparison

def test_compare() -> None:
    print("\n1. the Pinecrest outage must read as OUTAGE, not drift")
    spec = Spec("11.25.2", Req(schema=51, server=">=3.11.0"))
    before = Observed(app_version="11.9.1", zwave_js_api_schema=50,
                      zwave_js_server="3.10.0")
    f = evaluate("zwavejs", spec, before)
    check("severity is OUTAGE", severity_of(f) is Severity.OUTAGE,
          f"got {severity_of(f).name}")
    check("names the schema field", any(x.field == "zwave_js_api_schema" for x in f))
    check("message says it will not load",
          any("will not load" in x.detail for x in f), str(f))

    print("\n2. after the update it is clean")
    after = Observed(app_version="11.25.2", zwave_js_api_schema=51,
                     zwave_js_server="3.11.0")
    check("no findings", evaluate("zwavejs", spec, after) == [])

    print("\n3. right version, wrong capability -> still OUTAGE")
    # The case a version-only monitor cannot see: the pin says 11.25.2 and the
    # box says 11.25.2, but the service never came back on the new binary.
    half = Observed(app_version="11.25.2", zwave_js_api_schema=50)
    f = evaluate("zwavejs", spec, half)
    check("OUTAGE despite matching app_version",
          severity_of(f) is Severity.OUTAGE, f"got {severity_of(f).name}")
    check("does NOT report version drift",
          not any(x.field == "app_version" for x in f), str(f))

    print("\n4. wrong version, right capability -> DRIFT, not OUTAGE")
    # Working today, wrong tomorrow. Must not page anyone.
    f = evaluate("zwavejs", spec, Observed(app_version="11.24.0",
                                           zwave_js_api_schema=51))
    check("severity is DRIFT", severity_of(f) is Severity.DRIFT,
          f"got {severity_of(f).name}")

    print("\n5. a newer schema is just as broken (MIN == MAX)")
    f = evaluate("zwavejs", spec, Observed(app_version="11.25.2",
                                           zwave_js_api_schema=52))
    check("schema 52 against required 51 is OUTAGE",
          severity_of(f) is Severity.OUTAGE, f"got {severity_of(f).name}")

    print("\n6. an unreachable service is UNKNOWN, never OK")
    f = evaluate("zwavejs", spec, Observed(probe_error="timed out"))
    check("severity is UNKNOWN", severity_of(f) is Severity.UNKNOWN)
    check("not reported as healthy", severity_of(f) is not Severity.OK)

    print("\n7. a template with no requires only ever drifts")
    f = evaluate("zigbee2mqtt", Spec("2.14.3", Req(gated="2.0.0")),
                 Observed(app_version="1.42.0"))
    check("severity is DRIFT", severity_of(f) is Severity.DRIFT,
          f"got {severity_of(f).name}")

    print("\n8. no pin and no requires -> nothing to say")
    check("clean", evaluate("x", Spec(), Observed(app_version="9")) == [])


# -------------------------------------------------------------------- probe

_HANDSHAKE = (b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
              b"Connection: Upgrade\r\n\r\n")


def _frame(payload: bytes) -> bytes:
    """Unmasked server text frame, the three length encodings."""
    head = bytes([0x81])
    n = len(payload)
    if n < 126:
        return head + bytes([n]) + payload
    if n < 65536:
        return head + bytes([126]) + n.to_bytes(2, "big") + payload
    return head + bytes([127]) + n.to_bytes(8, "big") + payload


def _serve_once(body: bytes, send_handshake: bytes = _HANDSHAKE) -> int:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run() -> None:
        try:
            conn, _ = srv.accept()
            buf = b""
            while b"\r\n\r\n" not in buf:
                c = conn.recv(4096)
                if not c:
                    break
                buf += c
            conn.sendall(send_handshake)
            if body:
                conn.sendall(body)
            conn.close()
        finally:
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return port


def test_probe() -> None:
    print("\n9. the probe parses a real version frame")
    body = (b'{"type":"version","driverVersion":"15.31.2",'
            b'"serverVersion":"3.11.0","minSchemaVersion":0,'
            b'"maxSchemaVersion":51}')
    port = _serve_once(_frame(body))
    o = probe_zwave_js("127.0.0.1", port, timeout=5)
    check("no probe error", o.probe_error is None, str(o.probe_error))
    check("maxSchemaVersion 51", o.zwave_js_api_schema == 51,
          str(o.zwave_js_api_schema))
    check("serverVersion 3.11.0", o.zwave_js_server == "3.11.0")
    check("driverVersion carried", o.extra.get("driverVersion") == "15.31.2")

    print("\n10. the 126-length encoding is handled")
    big = (b'{"serverVersion":"3.11.0","maxSchemaVersion":51,"pad":"'
           + b"x" * 400 + b'"}')
    port = _serve_once(_frame(big))
    o = probe_zwave_js("127.0.0.1", port, timeout=5)
    check("parsed a >125 byte frame", o.zwave_js_api_schema == 51,
          f"err={o.probe_error}")

    print("\n11. failures are errors, never a false reading")
    port = _serve_once(b"", send_handshake=b"HTTP/1.1 400 Bad Request\r\n\r\n")
    o = probe_zwave_js("127.0.0.1", port, timeout=5)
    check("refused handshake -> probe_error", o.probe_error is not None)
    check("no schema invented", o.zwave_js_api_schema is None)

    # nothing listening
    s = socket.socket(); s.bind(("127.0.0.1", 0)); dead = s.getsockname()[1]; s.close()
    o = probe_zwave_js("127.0.0.1", dead, timeout=2)
    check("closed port -> probe_error", o.probe_error is not None)
    check("and evaluates as UNKNOWN",
          severity_of(evaluate("zwavejs", Spec("1", Req(schema=51)), o))
          is Severity.UNKNOWN)


if __name__ == "__main__":
    test_compare()
    test_probe()
    print("\n" + ("ALL PASS" if not failures
                  else f"{len(failures)} FAILURE(S): {failures}"))
    sys.exit(1 if failures else 0)
