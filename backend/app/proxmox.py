import random
import time
from dataclasses import dataclass

from proxmoxer import ProxmoxAPI


@dataclass
class NodeHealth:
    node: str
    cpu: float          # fraction 0.0–1.0
    mem_used: int       # bytes
    mem_total: int
    disk_used: int
    disk_total: int
    uptime: int         # seconds


@dataclass
class GuestSummary:
    vmid: int
    name: str
    kind: str           # "lxc" | "qemu"
    status: str
    cpu: float
    mem: int
    maxmem: int


class ProxmoxClient:
    def __init__(
        self,
        host: str,
        token_id: str,
        token_secret: str,
        node: str,
        verify_ssl: bool = False,
    ):
        user, _, token_name = token_id.partition("!")
        self._px = ProxmoxAPI(
            host,
            user=user,
            token_name=token_name,
            token_value=token_secret,
            verify_ssl=verify_ssl,
        )
        self.node = node

    def node_health(self) -> NodeHealth:
        s = self._px.nodes(self.node).status.get()
        mem = s.get("memory", {})
        rootfs = s.get("rootfs", {})
        return NodeHealth(
            node=self.node,
            cpu=s.get("cpu", 0.0),
            mem_used=mem.get("used", 0),
            mem_total=mem.get("total", 0),
            disk_used=rootfs.get("used", 0),
            disk_total=rootfs.get("total", 0),
            uptime=s.get("uptime", 0),
        )

    def list_guests(self) -> list[GuestSummary]:
        guests = []
        for g in self._px.nodes(self.node).lxc.get():
            guests.append(GuestSummary(
                vmid=int(g["vmid"]),
                name=g.get("name", ""),
                kind="lxc",
                status=g.get("status", "unknown"),
                cpu=g.get("cpu", 0.0),
                mem=g.get("mem", 0),
                maxmem=g.get("maxmem", 0),
            ))
        for g in self._px.nodes(self.node).qemu.get():
            guests.append(GuestSummary(
                vmid=int(g["vmid"]),
                name=g.get("name", ""),
                kind="qemu",
                status=g.get("status", "unknown"),
                cpu=g.get("cpu", 0.0),
                mem=g.get("mem", 0),
                maxmem=g.get("maxmem", 0),
            ))
        return sorted(guests, key=lambda g: g.name)

    # ---------------------------------------------------------------------------
    # Deploy plane — clone / configure / lifecycle
    # ---------------------------------------------------------------------------

    def _wait_task(self, upid: str, timeout: int = 120) -> None:
        node = upid.split(":")[1]
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self._px.nodes(node).tasks(upid).status.get()
            if result.get("status") == "stopped":
                if result.get("exitstatus") != "OK":
                    raise RuntimeError(f"PVE task failed: {result.get('exitstatus')}")
                return
            time.sleep(2)
        raise TimeoutError(f"PVE task {upid} did not complete within {timeout}s")

    def next_vmid(self) -> int:
        return int(self._px.cluster.nextid.get())

    def clone_lxc(self, tmpl_vmid: int, newid: int, hostname: str) -> None:
        upid = self._px.nodes(self.node).lxc(tmpl_vmid).clone.post(
            newid=newid,
            hostname=hostname,
            full=1,
        )
        self._wait_task(upid)

    @staticmethod
    def _random_mac() -> str:
        # Locally administered, unicast
        octets = [0x02, 0x00] + [random.randint(0x00, 0xFF) for _ in range(4)]
        return ":".join(f"{b:02x}" for b in octets)

    def set_lxc_fresh_mac(self, vmid: int) -> str:
        mac = self._random_mac()
        config = self._px.nodes(self.node).lxc(vmid).config.get()
        net0 = config.get("net0", "name=eth0,bridge=vmbr0,ip=dhcp")
        parts = [p for p in net0.split(",") if not p.lower().startswith("hwaddr=")]
        parts.append(f"hwaddr={mac}")
        self._px.nodes(self.node).lxc(vmid).config.put(net0=",".join(parts))
        return mac

    def start_guest(self, kind: str, vmid: int) -> None:
        getattr(self._px.nodes(self.node), kind)(vmid).status.start.post()

    def stop_guest(self, kind: str, vmid: int) -> None:
        getattr(self._px.nodes(self.node), kind)(vmid).status.stop.post()

    def reboot_guest(self, kind: str, vmid: int) -> None:
        getattr(self._px.nodes(self.node), kind)(vmid).status.reboot.post()

    # ---------------------------------------------------------------------------
    # Config / hardware
    # ---------------------------------------------------------------------------

    def get_guest_config(self, kind: str, vmid: int) -> dict:
        return getattr(self._px.nodes(self.node), kind)(vmid).config.get()

    def update_guest_config(self, kind: str, vmid: int,
                            changes: dict | None = None,
                            deletes: list[str] | None = None) -> None:
        kwargs = dict(changes or {})
        if deletes:
            kwargs["delete"] = ",".join(deletes)
        getattr(self._px.nodes(self.node), kind)(vmid).config.put(**kwargs)

    def resize_disk(self, kind: str, vmid: int, disk: str, size: str) -> None:
        getattr(self._px.nodes(self.node), kind)(vmid).resize.put(
            disk=disk, size=size
        )

    def list_usb_devices(self) -> list[dict]:
        try:
            return self._px.nodes(self.node).scan.usb.get()
        except Exception:
            return []

    def list_pci_devices(self) -> list[dict]:
        try:
            return self._px.nodes(self.node).hardware.pci.get()
        except Exception:
            return []
