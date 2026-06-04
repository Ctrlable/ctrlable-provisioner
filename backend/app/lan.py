"""
LAN discovery and NAT for the Debian orchestrator.

Flow after enrollment:
  1. detect_lan()         → find LAN iface + subnet
  2. register_lan()       → POST /api/v1/devices/{id}/lan → portal allocates proxy_subnet
  3. setup_nat()          → IP forwarding + nftables MASQUERADE (iptables fallback)
  4. setup_netmap()       → nftables PREROUTING DNAT: proxy_subnet → real LAN
  5. scan_and_report()    → ARP sweep + socket port scan → POST /api/discovery/report

The heartbeat loop calls get_lan_candidates() every beat and scan_and_report()
every 5th beat (~5 min).  setup_netmap() is called whenever the heartbeat
response delivers a new proxy_subnet + lan_subnet pair.
"""
import ipaddress
import json
import logging
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import StateDB

log = logging.getLogger(__name__)

PORTAL_BASE     = "https://portal.ctrlable.com"
SCAN_PORTS_FILE = Path("/tmp/ctrlable_scan_ports")
LAST_SCAN_FILE  = Path("/tmp/ctrlable_last_scan")
NFT_TABLE       = "ctrlnat"

DEFAULT_PORTS = [22, 80, 443, 554, 1883, 3000, 4200, 5000, 7080, 8080, 8123, 8443, 8888, 9090, 9443]


# ---------------------------------------------------------------------------
# LAN detection
# ---------------------------------------------------------------------------

def detect_lan_iface() -> str | None:
    """Return the interface on the default route, excluding WG/tun/lo."""
    try:
        r = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            parts = line.split()
            if "dev" in parts:
                iface = parts[parts.index("dev") + 1]
                if not any(iface.startswith(p) for p in ("wg", "tun", "lo")):
                    return iface
    except Exception:
        pass
    return None


def detect_lan_subnet(iface: str) -> str | None:
    """Return the network CIDR of the interface (e.g. 192.168.1.0/24)."""
    try:
        r = subprocess.run(["ip", "-4", "addr", "show", iface], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if line.strip().startswith("inet "):
                cidr = line.strip().split()[1]
                return str(ipaddress.ip_interface(cidr).network)
    except Exception:
        pass
    return None


def detect_lan() -> tuple[str, str] | None:
    """Return (iface, subnet) or None."""
    iface = detect_lan_iface()
    if not iface:
        return None
    subnet = detect_lan_subnet(iface)
    if not subnet:
        return None
    return iface, subnet


def get_lan_candidates() -> list[str]:
    """Return all non-WG subnets as LAN candidates for the heartbeat payload."""
    candidates: list[str] = []
    try:
        r = subprocess.run(["ip", "-4", "addr", "show"], capture_output=True, text=True)
        current_iface = ""
        for line in r.stdout.splitlines():
            if not line.startswith(" "):
                current_iface = line.split(":")[1].strip().split("@")[0] if ":" in line else ""
            elif line.strip().startswith("inet "):
                if any(current_iface.startswith(p) for p in ("wg", "tun", "lo")):
                    continue
                parts = line.strip().split()
                try:
                    net = str(ipaddress.ip_interface(parts[1]).network)
                    if net not in candidates:
                        candidates.append(net)
                except ValueError:
                    pass
    except Exception:
        pass
    return candidates


# ---------------------------------------------------------------------------
# Portal LAN registration
# ---------------------------------------------------------------------------

def register_lan(device_id: str, device_token: str, lan_subnet: str) -> dict:
    """POST /api/v1/devices/{id}/lan — returns updated device dict with proxy_subnet."""
    payload = json.dumps({"lan_subnet": lan_subnet, "lan_access_enabled": True}).encode()
    req = urllib.request.Request(
        f"{PORTAL_BASE}/api/v1/devices/{device_id}/lan",
        data=payload,
        headers={"Content-Type": "application/json", "X-Device-Token": device_token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise ValueError(f"LAN registration failed {e.code}: {body}")


# ---------------------------------------------------------------------------
# IP forwarding
# ---------------------------------------------------------------------------

def enable_ip_forwarding() -> None:
    subprocess.run(["sysctl", "-w", "net.ipv4.ip_forward=1"], capture_output=True)
    Path("/etc/sysctl.d/99-ctrlable.conf").write_text("net.ipv4.ip_forward=1\n")


# ---------------------------------------------------------------------------
# NAT / firewall — nftables primary, iptables fallback
# ---------------------------------------------------------------------------

def _nft_ok() -> bool:
    return subprocess.run(["nft", "--version"], capture_output=True).returncode == 0


def setup_nat(lan_iface: str, wg_iface: str) -> None:
    """MASQUERADE: WireGuard peers can reach the LAN through the orchestrator."""
    enable_ip_forwarding()

    if _nft_ok():
        # Tear down any previous ctrlnat table first so re-running is idempotent
        subprocess.run(["nft", "delete", "table", "ip", NFT_TABLE], capture_output=True)
        rules = f"""
table ip {NFT_TABLE} {{
    chain forward {{
        type filter hook forward priority filter;
        iifname "{wg_iface}" oifname "{lan_iface}" accept
        iifname "{lan_iface}" oifname "{wg_iface}" ct state related,established accept
    }}
    chain postrouting {{
        type nat hook postrouting priority srcnat;
        ip saddr 10.10.0.0/16 oifname "{lan_iface}" masquerade
    }}
    chain prerouting {{
        type nat hook prerouting priority dstnat;
    }}
}}
"""
        r = subprocess.run(["nft", "-f", "-"], input=rules, capture_output=True, text=True)
        if r.returncode == 0:
            log.info("nftables NAT ready (LAN: %s, WG: %s)", lan_iface, wg_iface)
            return
        log.warning("nft NAT failed: %s — falling back to iptables", r.stderr.strip())

    # iptables fallback
    for cmd in [
        ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "10.10.0.0/16", "-o", lan_iface, "-j", "MASQUERADE"],
        ["iptables", "-A", "FORWARD", "-i", wg_iface, "-o", lan_iface, "-j", "ACCEPT"],
        ["iptables", "-A", "FORWARD", "-i", lan_iface, "-o", wg_iface,
         "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
    ]:
        subprocess.run(cmd, capture_output=True)
    log.info("iptables NAT ready")


def setup_netmap(proxy_subnet: str, lan_subnet: str, wg_iface: str) -> None:
    """
    PREROUTING DNAT: translate proxy_subnet → real LAN.
    Enables remote users to reach 192.168.x.y by hitting 10.20.x.y.
    """
    if _nft_ok():
        # Modern syntax (nft 0.9.3+ / kernel 5.2+)
        rule = (f'add rule ip {NFT_TABLE} prerouting '
                f'iifname "{wg_iface}" ip daddr {proxy_subnet} '
                f'dnat ip prefix to {lan_subnet}')
        r = subprocess.run(["nft"] + rule.split(), capture_output=True, text=True)
        if r.returncode == 0:
            log.info("NETMAP (nft prefix): %s → %s", proxy_subnet, lan_subnet)
            return

        # Older nft: hex bitwise NETMAP
        try:
            proxy_net = ipaddress.ip_network(proxy_subnet, strict=False)
            lan_net   = ipaddress.ip_network(lan_subnet,   strict=False)
            mask_hex  = f"0x{int(proxy_net.netmask):08x}"
            lan_hex   = f"0x{int(lan_net.network_address):08x}"
            rule_compat = (
                f'add rule ip {NFT_TABLE} prerouting '
                f'iifname "{wg_iface}" ip daddr {proxy_subnet} '
                f'dnat to ip daddr and {mask_hex} xor {lan_hex}'
            )
            r2 = subprocess.run(["nft"] + rule_compat.split(), capture_output=True, text=True)
            if r2.returncode == 0:
                log.info("NETMAP (nft compat): %s → %s", proxy_subnet, lan_subnet)
                return
        except Exception as e:
            log.warning("nft compat NETMAP error: %s", e)

    # iptables NETMAP fallback
    subprocess.run([
        "iptables", "-t", "nat", "-A", "PREROUTING",
        "-i", wg_iface, "-d", proxy_subnet, "-j", "NETMAP", "--to", lan_subnet,
    ], capture_output=True)
    log.info("NETMAP (iptables): %s → %s", proxy_subnet, lan_subnet)


def netmap_active(proxy_subnet: str) -> bool:
    """Return True if a NETMAP/DNAT rule for proxy_subnet is already in place."""
    if _nft_ok():
        r = subprocess.run(["nft", "list", "table", "ip", NFT_TABLE], capture_output=True, text=True)
        return proxy_subnet in r.stdout
    r = subprocess.run(["iptables", "-t", "nat", "-L", "PREROUTING", "-n"],
                       capture_output=True, text=True)
    return proxy_subnet in r.stdout


# ---------------------------------------------------------------------------
# LAN scanning
# ---------------------------------------------------------------------------

def _ping_sweep(subnet: str) -> None:
    """Populate ARP cache by pinging all hosts in subnet."""
    try:
        network = ipaddress.ip_network(subnet, strict=False)
        if network.prefixlen < 20:
            return  # too large
        # fping is fast if available
        r = subprocess.run(
            ["fping", "-a", "-g", subnet, "-t", "300", "-q"],
            capture_output=True, timeout=20
        )
        if r.returncode in (0, 1):
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # Parallel ping fallback
    try:
        hosts = list(ipaddress.ip_network(subnet, strict=False).hosts())[:254]
        procs = [
            subprocess.Popen(["ping", "-c1", "-W1", str(h)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for h in hosts
        ]
        for p in procs:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
    except Exception:
        pass


def _read_arp() -> dict[str, str]:
    """Return {ip: MAC} from ip-neigh, falling back to /proc/net/arp."""
    entries: dict[str, str] = {}
    try:
        r = subprocess.run(["ip", "neigh", "show"], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            parts = line.split()
            # format: IP dev IFACE lladdr MAC state
            if len(parts) >= 5 and "lladdr" in parts:
                ip  = parts[0]
                mac = parts[parts.index("lladdr") + 1].upper()
                if mac != "00:00:00:00:00:00" and not ip.startswith("10.10."):
                    entries[ip] = mac
        if entries:
            return entries
    except Exception:
        pass
    try:
        with open("/proc/net/arp") as f:
            for line in list(f)[1:]:
                parts = line.split()
                if len(parts) < 4:
                    continue
                ip, mac = parts[0], parts[3].upper()
                if mac != "00:00:00:00:00:00" and not ip.startswith("10.10."):
                    entries[ip] = mac
    except Exception:
        pass
    return entries


def _socket_scan(ip: str, ports: list[int], timeout: float = 0.8) -> list[int]:
    """TCP connect scan using Python sockets — no external tools needed."""
    open_ports = []
    for port in ports:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            if s.connect_ex((ip, port)) == 0:
                open_ports.append(port)
            s.close()
        except Exception:
            pass
    return open_ports


def scan_lan(lan_subnet: str, ports: list[int] | None = None) -> list[dict]:
    """
    Ping sweep + ARP read + socket port scan.
    Returns [{ip_address, mac_address, hostname, open_ports}].
    """
    ports = ports or DEFAULT_PORTS

    _ping_sweep(lan_subnet)
    arp = _read_arp()

    # Filter to IPs in our subnet
    try:
        net = ipaddress.ip_network(lan_subnet, strict=False)
        arp = {ip: mac for ip, mac in arp.items()
               if ipaddress.ip_address(ip) in net}
    except ValueError:
        pass

    devices = []
    for ip, mac in arp.items():
        devices.append({
            "ip_address": ip,
            "mac_address": mac,
            "hostname":    _resolve_hostname(ip),
            "open_ports":  _socket_scan(ip, ports),
        })

    # Include the orchestrator itself
    try:
        own_ip = socket.gethostbyname(socket.gethostname())
        if own_ip and own_ip not in arp:
            devices.append({
                "ip_address": own_ip,
                "mac_address": "00:00:00:00:00:00",
                "hostname":    socket.gethostname(),
                "open_ports":  _socket_scan("127.0.0.1", ports),
            })
    except Exception:
        pass

    return devices


def _resolve_hostname(ip: str) -> str:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Port list persistence
# ---------------------------------------------------------------------------

def load_scan_ports() -> list[int]:
    if SCAN_PORTS_FILE.exists():
        try:
            return [int(p.strip()) for p in SCAN_PORTS_FILE.read_text().split(",") if p.strip()]
        except Exception:
            pass
    return list(DEFAULT_PORTS)


def save_scan_ports(ports: list[int]) -> None:
    SCAN_PORTS_FILE.write_text(",".join(str(p) for p in ports))


def should_scan() -> bool:
    if not LAST_SCAN_FILE.exists():
        return True
    try:
        return (time.time() - float(LAST_SCAN_FILE.read_text().strip())) >= 300
    except Exception:
        return True


def mark_scanned() -> None:
    LAST_SCAN_FILE.write_text(str(time.time()))


# ---------------------------------------------------------------------------
# Discovery report
# ---------------------------------------------------------------------------

def report_scan(device_id: str, device_token: str, devices: list[dict]) -> None:
    """POST /api/discovery/report with ARP + port scan results."""
    payload = json.dumps({"scan_type": "arp_nc", "devices": devices}).encode()
    req = urllib.request.Request(
        f"{PORTAL_BASE}/api/v1/discovery/report",
        data=payload,
        headers={"Content-Type": "application/json", "X-Device-Token": device_token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        log.info("Discovery: reported %d devices to portal", len(devices))
    except Exception as e:
        log.warning("Discovery report failed: %s", e)


# ---------------------------------------------------------------------------
# Full LAN setup (called once after enrollment, then on every startup)
# ---------------------------------------------------------------------------

def setup_lan_access(device_id: str, device_token: str, db: "StateDB") -> None:
    """
    Detect LAN, register with portal (get proxy_subnet), configure NAT.
    Blocking — run via asyncio.to_thread().
    """
    detected = detect_lan()
    if not detected:
        log.warning("LAN setup: could not detect LAN interface/subnet")
        return

    lan_iface, lan_subnet = detected
    log.info("LAN: %s on %s", lan_subnet, lan_iface)

    # Register with portal to get proxy_subnet allocated
    try:
        resp = register_lan(device_id, device_token, lan_subnet)
        proxy_subnet = resp.get("proxy_subnet")
        db.update_lan_state(lan_iface, lan_subnet, proxy_subnet)
        log.info("LAN registered — proxy_subnet: %s", proxy_subnet)
    except Exception as e:
        log.warning("LAN registration failed: %s — will retry via heartbeat", e)
        proxy_subnet = None
        db.update_lan_state(lan_iface, lan_subnet, None)

    # Get WG iface from platform state
    pstate = db.get_platform_state()
    wg_iface = pstate["wg_iface"] if pstate else "wg1"

    # Set up NAT (MASQUERADE)
    setup_nat(lan_iface, wg_iface)

    # Set up NETMAP if we have proxy_subnet
    if proxy_subnet:
        setup_netmap(proxy_subnet, lan_subnet, wg_iface)
