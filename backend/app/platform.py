"""
M8 — Portal enrollment and heartbeat.

Enrollment: POST /v1/enroll with a one-time token from portal.ctrlable.com.
Writes WireGuard config and brings up the tunnel so the orchestrator is
reachable from the Ctrlable portal.
Heartbeat: POST /api/v1/devices/{id}/heartbeat every 60 s.
"""
import asyncio
import base64
import json
import logging
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import StateDB

log = logging.getLogger(__name__)

PORTAL_BASE = "https://portal.ctrlable.com"
WG_DIR = Path("/etc/wireguard")
AGENT_CONF_DIR = Path("/etc/ctrlable")

_heartbeat_task: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_mac() -> str:
    for p in sorted(Path("/sys/class/net").iterdir()):
        if p.name == "lo":
            continue
        addr = (p / "address").read_text().strip() if (p / "address").exists() else ""
        if addr and addr != "00:00:00:00:00:00":
            return addr.upper()
    return "00:00:00:00:00:00"


def _get_hostname() -> str:
    return socket.gethostname()


def _portal_post(path: str, payload: dict, token: str | None = None) -> dict:
    data = json.dumps(payload).encode()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["X-Device-Token"] = token
    req = urllib.request.Request(
        f"{PORTAL_BASE}{path}", data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            detail = json.loads(body).get("detail", body)
        except Exception:
            detail = body
        raise ValueError(f"Portal returned {e.code}: {detail}")
    except Exception as e:
        raise ValueError(f"Portal request failed: {e}")


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------

def enroll(token: str, db: "StateDB") -> dict:
    """Call the portal enrollment API, configure WireGuard, store state."""
    resp = _portal_post("/v1/enroll", {
        "token": token,
        "mac_address": _get_mac(),
        "platform": "debian",
        "hostname": _get_hostname(),
    })

    device_id = resp.get("device_id")
    tunnel_ip = resp.get("tunnel_ip")
    wg_iface = resp.get("interface")
    device_token = resp.get("device_token")
    wg_b64 = resp.get("wg_config", "")

    if not device_id:
        raise ValueError(f"Unexpected portal response: {resp}")

    wg_conf = base64.b64decode(wg_b64).decode()

    # Write WireGuard config
    WG_DIR.mkdir(parents=True, exist_ok=True)
    WG_DIR.chmod(0o700)
    wg_conf_file = WG_DIR / f"{wg_iface}.conf"
    wg_conf_file.write_text(wg_conf)
    wg_conf_file.chmod(0o600)

    # Write agent credentials (compatible with the ctrlable-agent shell script)
    AGENT_CONF_DIR.mkdir(parents=True, exist_ok=True)
    (AGENT_CONF_DIR / "ctrlable.conf").write_text(
        f"DEVICE_ID={device_id}\n"
        f"DEVICE_TOKEN={device_token}\n"
        f"TUNNEL_IP={tunnel_ip}\n"
        f"WG_IFACE={wg_iface}\n"
        f"API_BASE={PORTAL_BASE}/api/v1\n"
    )
    (AGENT_CONF_DIR / "ctrlable.conf").chmod(0o600)

    # Bring up WireGuard (tear down first if re-enrolling)
    subprocess.run(["wg-quick", "down", wg_iface], capture_output=True)
    result = subprocess.run(["wg-quick", "up", wg_iface], capture_output=True, text=True)
    if result.returncode != 0:
        log.warning("wg-quick up failed: %s", result.stderr.strip())

    # Persist across reboots
    subprocess.run(["systemctl", "enable", f"wg-quick@{wg_iface}"], capture_output=True)

    db.set_platform_state(device_id, device_token, tunnel_ip, wg_iface)

    start_heartbeat(device_id, device_token)

    return {"device_id": device_id, "tunnel_ip": tunnel_ip, "wg_iface": wg_iface}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def get_status(db: "StateDB") -> dict:
    state = db.get_platform_state()
    if not state:
        return {"enrolled": False}
    return {
        "enrolled": True,
        "device_id": state["device_id"],
        "tunnel_ip": state["tunnel_ip"],
        "wg_iface": state["wg_iface"],
        "enrolled_at": state["enrolled_at"],
        "portal_url": f"{PORTAL_BASE}/devices/{state['device_id']}",
    }


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def start_heartbeat(device_id: str, device_token: str) -> None:
    global _heartbeat_task
    if _heartbeat_task and not _heartbeat_task.done():
        _heartbeat_task.cancel()
    _heartbeat_task = asyncio.create_task(_heartbeat_loop(device_id, device_token))


async def _heartbeat_loop(device_id: str, device_token: str) -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(_send_heartbeat, device_id, device_token)
        except Exception as e:
            log.debug("Heartbeat failed: %s", e)


def _send_heartbeat(device_id: str, device_token: str) -> None:
    rx, tx = _wg_stats()
    _portal_post(
        f"/api/v1/devices/{device_id}/heartbeat",
        {"rx_bytes": rx, "tx_bytes": tx, "agent_version": "ctrlable-provisioner/1.0"},
        token=device_token,
    )


def _wg_stats() -> tuple[int, int]:
    try:
        result = subprocess.run(["wg", "show", "all", "dump"], capture_output=True, text=True)
        rx = tx = 0
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 7:
                try:
                    rx += int(parts[5])
                    tx += int(parts[6])
                except ValueError:
                    pass
        return rx, tx
    except Exception:
        return 0, 0
