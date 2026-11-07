"""Component drift: what is installed vs what the manifest says must be.

SPEC § 16. This module is the COMPARISON and the PROBES. It deliberately owns
no transport and no schedule: callers supply what they observed, so the logic
is testable without a Proxmox host, and a probe that cannot run yields
`UNKNOWN` rather than a false `OK`.

Two findings that are not the same thing, per `Requires`:

    app_version mismatch  -> DRIFT    cosmetic until it isn't
    requires  mismatch    -> OUTAGE   the site is already broken

Pinecrest 2026-11-07 was the second kind. zwave-js-ui shipped
@zwave-js/server 3.10.0 (maxSchemaVersion 50) against a Core pinning schema
51, so `zwave_js` never loaded -- no degraded mode, every Z-Wave entity gone,
and the keymaster service-entrance lock with it. It was found by a dealer
noticing dead locks, roughly a day later.

Why capability over version
---------------------------
`app_version` says what we INTENDED to install. It cannot tell you the install
succeeded, the right binary is on disk, or the service came back up. For
anything that exposes its capability over the network, ask the service:

    requires.zwave_js_api_schema: 51   ->  read maxSchemaVersion off the
                                           Z-Wave JS websocket handshake

That is the exact number Core rejects on, so it is the number worth asserting.
A site can sit on the correct version and still be broken; the capability
probe catches that and a version string never will.

It also sidesteps SPEC § 18's open "probe transport" question for the
component that matters most: this needs no `pct exec`, no host trigger and no
in-guest agent -- just a TCP connection, which the orchestrator already has.
"""

from __future__ import annotations

import base64
import json
import os
import socket
from dataclasses import dataclass, field
from enum import IntEnum


class Severity(IntEnum):
    """Ordered so `max()` over findings gives the row's headline severity."""

    OK = 0
    UNKNOWN = 1      # the probe could not run — NOT the same as healthy
    DRIFT = 2        # off app_version: wrong version, still working
    OUTAGE = 3       # off requires: the site is broken right now


@dataclass(frozen=True)
class Finding:
    severity: Severity
    field: str
    expected: str
    observed: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"[{self.severity.name}] {self.field}: {self.detail}"


@dataclass
class Observed:
    """What a probe actually saw. Every field optional: absent means not probed,
    which must surface as UNKNOWN rather than silently passing."""

    app_version: str | None = None
    zwave_js_api_schema: int | None = None
    zwave_js_server: str | None = None
    probe_error: str | None = None
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def evaluate(name: str, spec, observed: Observed) -> list[Finding]:
    """Compare one template's manifest spec against what was observed.

    `spec` is a manifest.TemplateSpec (or anything with the same attributes),
    kept duck-typed so tests need no pydantic and no manifest file.
    """
    out: list[Finding] = []

    if observed.probe_error:
        return [Finding(Severity.UNKNOWN, "probe", "a reading", "none",
                        f"could not probe {name}: {observed.probe_error}")]

    want_version = getattr(spec, "app_version", None)
    if want_version:
        if observed.app_version is None:
            out.append(Finding(Severity.UNKNOWN, "app_version", want_version,
                               "unknown", "no version probe configured"))
        elif observed.app_version != want_version:
            out.append(Finding(
                Severity.DRIFT, "app_version", want_version,
                observed.app_version,
                f"installed {observed.app_version}, manifest pins {want_version}"))

    req = getattr(spec, "requires", None)
    if req is not None:
        # EQUALITY, not a floor. Core's zwave_js_server client sets MIN == MAX,
        # so a newer schema is exactly as broken as an older one — which is why
        # "just don't update" is not a safe posture for this component.
        want_schema = getattr(req, "zwave_js_api_schema", None)
        if want_schema is not None:
            got = observed.zwave_js_api_schema
            if got is None:
                out.append(Finding(
                    Severity.UNKNOWN, "zwave_js_api_schema", str(want_schema),
                    "unknown", "the Z-Wave JS server could not be reached"))
            elif got != want_schema:
                out.append(Finding(
                    Severity.OUTAGE, "zwave_js_api_schema", str(want_schema),
                    str(got),
                    f"server offers schema {got}, Core requires exactly "
                    f"{want_schema} — zwave_js will not load at all"))

    return out


def severity_of(findings: list[Finding]) -> Severity:
    return max((f.severity for f in findings), default=Severity.OK)


# --------------------------------------------------------------------------
# probes
# --------------------------------------------------------------------------

def probe_zwave_js(host: str, port: int = 3000, timeout: float = 8.0) -> Observed:
    """Read the Z-Wave JS server's capabilities off its websocket handshake.

    The server sends a `version` frame immediately on connect, before any auth
    or subscription, carrying driverVersion / serverVersion / minSchemaVersion
    / maxSchemaVersion. That is everything needed, so this speaks just enough
    websocket to read one frame rather than taking a dependency.

    Verified against Pinecrest 2026-11-07: reported server 3.11.0 /
    maxSchemaVersion 51 after the update, 3.10.0 / 50 before it.
    """
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(req.encode())

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return Observed(probe_error="connection closed during handshake")
            buf += chunk
        if b"101" not in buf.split(b"\r\n", 1)[0]:
            return Observed(probe_error=f"handshake refused: "
                                        f"{buf.split(chr(13).encode())[0][:80]!r}")

        # Whatever followed the header terminator is already the first frame.
        # TCP is a stream: the server's `version` frame arrives in the SAME
        # segment as the handshake often enough that dropping the remainder
        # and recv()ing again loses it and reads as "no version frame". The
        # live probe passed only because that server split the two packets.
        _, _, rest = buf.partition(b"\r\n\r\n")
        payload = _read_one_frame(sock, timeout, prefix=rest)
        if payload is None:
            return Observed(probe_error="no version frame from the server")
        msg = json.loads(payload)
        return Observed(
            zwave_js_api_schema=msg.get("maxSchemaVersion"),
            zwave_js_server=msg.get("serverVersion"),
            extra={"driverVersion": msg.get("driverVersion"),
                   "minSchemaVersion": msg.get("minSchemaVersion")},
        )
    except (OSError, ValueError, json.JSONDecodeError) as err:
        return Observed(probe_error=f"{type(err).__name__}: {err}")
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _read_one_frame(sock: socket.socket, timeout: float,
                    prefix: bytes = b"") -> str | None:
    """Minimal server->client text frame reader.

    Server frames are never masked (RFC 6455 §5.1), so there is no unmasking
    to do. Only the three payload-length encodings need handling.
    """
    data = prefix
    while len(data) < 2:
        chunk = sock.recv(65536)
        if not chunk:
            return None
        data += chunk
    length = data[1] & 0x7F
    offset = 2
    if length == 126:
        if len(data) < 4:
            return None
        length = int.from_bytes(data[2:4], "big")
        offset = 4
    elif length == 127:
        if len(data) < 10:
            return None
        length = int.from_bytes(data[2:10], "big")
        offset = 10
    while len(data) < offset + length:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    return data[offset:offset + length].decode("utf-8", "replace")
