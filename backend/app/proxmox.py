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
