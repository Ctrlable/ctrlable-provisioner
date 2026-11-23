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
import os
import shutil
import socket
import subprocess
import sys
import time
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
ENROLL_TOKEN_FILE = AGENT_CONF_DIR / "enroll.token"

_heartbeat_task: asyncio.Task | None = None
_auto_enroll_task: asyncio.Task | None = None
_last_self_update_mono: float = 0.0  # monotonic clock of the last immediate self-update attempt


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
# Token file helpers
# ---------------------------------------------------------------------------

def save_enroll_token(token: str) -> None:
    AGENT_CONF_DIR.mkdir(parents=True, exist_ok=True)
    ENROLL_TOKEN_FILE.write_text(token.strip())
    ENROLL_TOKEN_FILE.chmod(0o600)


def _load_pending_token() -> str | None:
    if ENROLL_TOKEN_FILE.exists():
        t = ENROLL_TOKEN_FILE.read_text().strip()
        return t or None
    return None


def _clear_pending_token() -> None:
    ENROLL_TOKEN_FILE.unlink(missing_ok=True)


def _has_internet() -> bool:
    try:
        urllib.request.urlopen(f"{PORTAL_BASE}/health", timeout=5)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Auto-enrollment
# ---------------------------------------------------------------------------

def start_auto_enroll(db: "StateDB") -> None:
    """Launch background task that enrolls as soon as internet is reachable."""
    global _auto_enroll_task
    if _auto_enroll_task and not _auto_enroll_task.done():
        return
    _auto_enroll_task = asyncio.create_task(_auto_enroll_loop(db))


async def _auto_enroll_loop(db: "StateDB") -> None:
    """Retry enrollment every 30 s until the token is consumed or already enrolled."""
    while True:
        token = _load_pending_token()
        if not token:
            return

        if db.get_platform_state():
            _clear_pending_token()
            return

        if await asyncio.to_thread(_has_internet):
            try:
                _did, device_token, tunnel_ip, _iface = await asyncio.to_thread(enroll, token, db)
                start_heartbeat(_did, device_token, db)
                from .lan import setup_lan_access
                asyncio.create_task(asyncio.to_thread(setup_lan_access, _did, device_token, db))
                log.info("Auto-enrollment successful — tunnel IP %s", tunnel_ip)
                _clear_pending_token()
                return
            except Exception as e:
                log.warning("Auto-enrollment failed: %s — will retry in 30 s", e)

        await asyncio.sleep(30)


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

    # Strip DNS= lines — LXC uses its own resolver; resolvconf may not be installed
    wg_conf = "\n".join(
        line for line in wg_conf.splitlines()
        if not line.strip().startswith("DNS")
    ) + "\n"

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

    # Install wireguard-tools if missing
    if not shutil.which("wg-quick"):
        log.info("wireguard-tools not found — installing")
        r = subprocess.run(
            ["apt-get", "install", "-y", "-qq", "wireguard-tools"],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            log.warning("wireguard-tools install failed: %s", r.stderr.strip())

    # Bring up WireGuard (tear down first if re-enrolling)
    subprocess.run(["wg-quick", "down", wg_iface], capture_output=True)
    result = subprocess.run(["wg-quick", "up", wg_iface], capture_output=True, text=True)
    if result.returncode != 0:
        log.warning("wg-quick up failed: %s", result.stderr.strip())

    # Persist across reboots
    subprocess.run(["systemctl", "enable", f"wg-quick@{wg_iface}"], capture_output=True)

    db.set_platform_state(device_id, device_token, tunnel_ip, wg_iface)

    return device_id, device_token, tunnel_ip, wg_iface


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def ensure_tunnel_up(wg_iface: str) -> None:
    """Bring the WireGuard tunnel up if it's not already running."""
    result = subprocess.run(["ip", "link", "show", wg_iface], capture_output=True)
    if result.returncode == 0:
        return  # interface already exists

    # Strip DNS= lines from existing conf (resolvconf may not be installed)
    conf_file = WG_DIR / f"{wg_iface}.conf"
    if conf_file.exists():
        content = conf_file.read_text()
        stripped = "\n".join(
            l for l in content.splitlines() if not l.strip().startswith("DNS")
        ) + "\n"
        if stripped != content:
            conf_file.write_text(stripped)

    if not shutil.which("wg-quick"):
        subprocess.run(["apt-get", "install", "-y", "-qq", "wireguard-tools"],
                       capture_output=True)
    r = subprocess.run(["wg-quick", "up", wg_iface], capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("wg-quick up %s failed: %s", wg_iface, r.stderr.strip())
    else:
        log.info("WireGuard tunnel %s is up", wg_iface)


def get_status(db: "StateDB") -> dict:
    state = db.get_platform_state()
    if not state:
        return {"enrolled": False, "pending_token": _load_pending_token() is not None}
    return {
        "enrolled": True,
        "device_id": state["device_id"],
        "tunnel_ip": state["tunnel_ip"],
        "wg_iface": state["wg_iface"],
        "enrolled_at": state["enrolled_at"],
        "portal_url": f"{PORTAL_BASE}/devices/{state['device_id']}",
        "pending_token": False,
    }


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def start_heartbeat(device_id: str, device_token: str, db: "StateDB | None" = None) -> None:
    global _heartbeat_task
    if _heartbeat_task and not _heartbeat_task.done():
        _heartbeat_task.cancel()
    _heartbeat_task = asyncio.create_task(_heartbeat_loop(device_id, device_token, db))


async def _heartbeat_loop(device_id: str, device_token: str, db: "StateDB") -> None:
    from .lan import (
        get_lan_candidates, load_scan_ports, save_scan_ports,
        scan_lan, report_scan, should_scan, mark_scanned,
        setup_netmap, netmap_active, collect_diag,
    )
    beat = 0
    while True:
        await asyncio.sleep(60)
        beat += 1
        try:
            # Report NAT/tunnel diagnostics every ~5 min (keeps most beats small).
            diag = collect_diag() if beat % 5 == 0 else None
            resp = await asyncio.to_thread(
                _send_heartbeat, device_id, device_token, get_lan_candidates(), diag
            )
            # Update scan port list if portal sent a new one
            if resp.get("scan_ports"):
                save_scan_ports(resp["scan_ports"])

            # Portal-driven self-update (support / rollout). Checked every ~5 min
            # (it does a git fetch); only updates when actually behind, then restarts.
            # An operator-queued update carries update_ref: act immediately, to
            # that ref. Otherwise auto-track the installed ref, throttled to ~5 min.
            _cmd = resp.get("command")
            _uref = resp.get("update_ref")
            if _cmd == "self_update" and _uref:
                await _maybe_self_update(ref=_uref, immediate=True)
            elif _cmd in ("update_agent", "self_update") and beat % 5 == 0:
                await _maybe_self_update()

            # Remote shell: the portal opens a session; we dial the wss relay
            # OUTBOUND and bridge a PTY. The token is embedded in ws_url; de-dup
            # so repeated heartbeats don't fork a second bridge.
            _shell = resp.get("open_shell")
            if _shell and _shell.get("ws_url"):
                _maybe_open_shell(_shell["ws_url"])

            # Set up / refresh NETMAP when portal provides proxy_subnet
            proxy_subnet = resp.get("proxy_subnet")
            lan_subnet   = resp.get("lan_subnet")
            if proxy_subnet and lan_subnet and not netmap_active(proxy_subnet):
                pstate = db.get_platform_state()
                wg_iface = pstate["wg_iface"] if pstate else "wg1"
                await asyncio.to_thread(setup_netmap, proxy_subnet, lan_subnet, wg_iface)
                db.update_lan_state(
                    pstate["lan_iface"] if pstate else "",
                    lan_subnet, proxy_subnet
                )

        except Exception as e:
            log.warning("Heartbeat failed: %s", e)

        # Scan LAN every 5 beats (~5 min)
        if beat % 5 == 0:
            try:
                pstate = db.get_platform_state()
                if pstate and pstate["lan_subnet"]:
                    ports   = load_scan_ports()
                    devices = await asyncio.to_thread(scan_lan, pstate["lan_subnet"], ports)
                    await asyncio.to_thread(report_scan, device_id, device_token, devices)
                    mark_scanned()
            except Exception as e:
                log.warning("LAN scan failed: %s", e)


def _provisioner_version() -> str:
    """The running provisioner version, so the portal can spot a host that has
    stopped updating (§ 16.6). Cheap and failure-tolerant — the heartbeat must
    never break over this."""
    try:
        from .selfupdate import installed_version
        return installed_version()
    except Exception:                              # noqa: BLE001
        return "unknown"


# Remote-shell bridge child + de-dup key (the ha-addon tracks the same state
# in shell variables). At most one live bridge per orchestrator.
_shell_proc = None
_shell_last_ws: str = ""


_SHELL_LOG = "/tmp/ctrlable-shell-bridge.log"


def _maybe_open_shell(ws_url: str) -> None:
    """Spawn the PTY<->websocket bridge for a portal-opened shell session.
    De-dups on ws_url + a live child so repeated heartbeats are no-ops. Records
    the outcome in _last_cmd_result (which the heartbeat surfaces to the portal)
    and captures the bridge's own stderr, so a spawn/connect failure is visible
    without shell access to the orchestrator."""
    global _shell_proc, _shell_last_ws, _last_cmd_result
    try:
        if _shell_proc is not None:
            rc = _shell_proc.poll()
            if rc is None and ws_url == _shell_last_ws:
                return  # already bridging this session
            if rc is not None and rc != 0:
                tail = ""
                try:
                    with open(_SHELL_LOG, "r", errors="replace") as f:
                        tail = f.read()[-180:].replace("\n", " ").strip()
                except Exception:  # noqa: BLE001
                    pass
                _last_cmd_result = f"shell: bridge exited rc={rc} {tail}".strip()[:250]
        bridge = os.path.join(os.path.dirname(__file__), "shell_bridge.py")
        if not os.path.exists(bridge):
            _last_cmd_result = f"shell: bridge file missing at {bridge}"
            log.warning(_last_cmd_result)
            return
        logf = open(_SHELL_LOG, "wb")  # truncate per attempt
        _shell_proc = subprocess.Popen(
            [sys.executable, bridge],
            env={**os.environ, "CTRL_SHELL_WS": ws_url},
            stdout=logf, stderr=logf,
        )
        logf.close()  # child keeps its own dup of the fd
        _shell_last_ws = ws_url
        if not (_last_cmd_result or "").startswith("shell: bridge exited"):
            _last_cmd_result = (f"shell: bridge spawned pid={_shell_proc.pid} "
                                f"py={os.path.basename(sys.executable)}")
    except Exception as e:  # noqa: BLE001
        _last_cmd_result = f"shell: spawn error {e}"[:250]
        log.warning(_last_cmd_result)


# Result of the last portal-requested command, reported back on the next
# heartbeat so support can see what happened (mirrors the ha-addon agent).
_last_cmd_result: str = ""


async def _maybe_self_update(ref: str | None = None, immediate: bool = False) -> None:
    """Portal-driven self-update. Background mode (immediate=False) auto-tracks
    the installed ref, ~5-min throttled, only when behind. Immediate mode is an
    operator-queued update to a specific ref (the portal sends it every beat
    while queued, so dedupe to avoid re-checking each beat). Either way, only
    updates when actually behind and no build/deploy is in flight, then restarts.
    Mirrors POST /api/system/self-update; the orchestrator equivalent of the
    ha-addon self_update command."""
    global _last_cmd_result, _last_self_update_mono
    try:
        from . import selfupdate, build as _build
        from .config import get_settings
        if immediate and (time.monotonic() - _last_self_update_mono) < 90:
            return                                  # deduped: the portal re-sends every beat while queued
        if immediate:
            _last_self_update_mono = time.monotonic()
        if getattr(_build, "_running", None):
            _last_cmd_result = "self_update: skipped (build in progress)"
            return
        target = ref
        if not target:
            try:
                from .main import _install_record
                target = _install_record().get("ref") or "main"
            except Exception:                       # noqa: BLE001
                target = "main"
        chk = await asyncio.to_thread(selfupdate.check_update, target)
        if not chk.get("update_available"):
            _last_cmd_result = f"self_update: up-to-date (ref={target})"
            return
        log.info("Self-update (%s): %s behind on %s — updating",
                 "operator" if immediate else "auto", chk.get("behind"), target)
        result = await asyncio.to_thread(selfupdate.run_self_update, target, get_settings())
        _last_cmd_result = (f"self_update: ok, restarting (ref={target})" if result.get("ok")
                            else f"self_update: failed at {result.get('stage')}")
    except Exception as e:                          # noqa: BLE001
        _last_cmd_result = f"self_update: error {e}"[:200]
        log.warning("self-update failed: %s", e)


def _send_heartbeat(device_id: str, device_token: str, lan_candidates: list[str],
                    nat_diag: str | None = None) -> dict:
    rx, tx = _wg_stats()
    ver = _provisioner_version()
    payload = {
        "rx_bytes": rx,
        "tx_bytes": tx,
        "agent_version": "prov/1.0",
        "provisioner_version": ver,
        "addon_version": ver,          # surface the version in the portal's version field
        "lan_candidates": lan_candidates,
        "web_uis": _probe_web_uis(),
    }
    if nat_diag:
        payload["nat_diag"] = nat_diag
    if _last_cmd_result:
        payload["command_result"] = _last_cmd_result
    return _portal_post(
        f"/api/v1/devices/{device_id}/heartbeat",
        payload,
        token=device_token,
    )


def _probe_web_uis() -> list[dict]:
    """Report which web UIs this orchestrator exposes, for the portal shortcut.

    The manager (this app) is always on :8000. Proxmox VE serves its console on
    :8006 (TLS); Cockpit, when installed, on :9090. We only list a probed port
    if the socket accepts, so the list doubles as a reachability check.
    """
    uis = [{"port": 8000, "scheme": "http"}]
    for port, scheme in ((8006, "https"), (9090, "https")):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.5):
                uis.append({"port": port, "scheme": scheme})
        except OSError:
            pass
    return uis


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
