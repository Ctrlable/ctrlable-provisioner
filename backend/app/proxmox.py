"""Proxmox VE implementation of the Provider interface.

Everything Proxmox-specific lives here: the proxmoxer client, the VMID/name
mapping, the lxc/qemu endpoint routing, and the async UPID/task polling. The
rest of the orchestrator sees only the neutral Provider surface (provider.py),
so a second backend (libvirt/Incus on a Debian base) is a sibling of this class
rather than a rewrite. See provider.py for why that seam exists (AGPL / product
licensing) and for the per-method porting notes.
"""
from __future__ import annotations

import random
import time
from typing import Optional

from proxmoxer import ProxmoxAPI

from .provider import (
    GuestKind,
    GuestRef,
    GuestSummary,
    NodeHealth,
    Provider,
)


class ProxmoxProvider(Provider):
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

    # -- internal helpers -----------------------------------------------------

    def _endpoint(self, ref: GuestRef):
        """The proxmoxer node-endpoint for a guest: .lxc(vmid) or .qemu(vmid)."""
        return getattr(self._px.nodes(self.node), ref.kind.proxmox)(int(ref.id))

    def _wait_task(self, upid: str, timeout: int = 120) -> None:
        """Block until a Proxmox task finishes. This is what makes every mutating
        method synchronous to the caller -- the async UPID model never leaves
        this file."""
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

    @staticmethod
    def _random_mac() -> str:
        # Locally administered, unicast.
        octets = [0x02, 0x00] + [random.randint(0x00, 0xFF) for _ in range(4)]
        return ":".join(f"{b:02x}" for b in octets)

    # -- inventory / health ---------------------------------------------------

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

    def list_guests(self, exclude: Optional[set[str]] = None) -> list[GuestSummary]:
        skip = exclude or set()
        guests: list[GuestSummary] = []
        for kind, proxmox_kind in ((GuestKind.CONTAINER, "lxc"),
                                   (GuestKind.VM, "qemu")):
            for g in getattr(self._px.nodes(self.node), proxmox_kind).get():
                if g.get("template") == 1:
                    continue
                gid = str(g["vmid"])
                if gid in skip:
                    continue
                guests.append(GuestSummary(
                    ref=GuestRef(id=gid, kind=kind),
                    name=g.get("name", ""),
                    status=g.get("status", "unknown"),
                    cpu=g.get("cpu", 0.0),
                    mem=g.get("mem", 0),
                    maxmem=g.get("maxmem", 0),
                ))
        return sorted(guests, key=lambda g: g.name)

    # -- lifecycle ------------------------------------------------------------

    def start(self, ref: GuestRef) -> None:
        self._endpoint(ref).status.start.post()

    def stop(self, ref: GuestRef) -> None:
        self._endpoint(ref).status.stop.post()

    def reboot(self, ref: GuestRef) -> None:
        self._endpoint(ref).status.reboot.post()

    def destroy(self, ref: GuestRef) -> None:
        ep = self._endpoint(ref)
        try:
            ep.status.stop.post()
        except Exception:
            pass
        ep.delete(purge=1, **{"destroy-unreferenced-disks": 1})

    # -- provisioning ---------------------------------------------------------

    def allocate_id(self) -> str:
        return str(int(self._px.cluster.nextid.get()))

    def clone(self, template: GuestRef, new_id: str, name: str) -> GuestRef:
        ep = self._endpoint(template)
        if template.kind is GuestKind.CONTAINER:
            upid = ep.clone.post(newid=int(new_id), hostname=name, full=1)
        else:
            upid = ep.clone.post(newid=int(new_id), name=name, full=1)
        self._wait_task(upid)
        return GuestRef(id=str(new_id), kind=template.kind)

    def set_fresh_mac(self, ref: GuestRef) -> str:
        mac = self._random_mac()
        ep = self._endpoint(ref)
        config = ep.config.get()
        if ref.kind is GuestKind.CONTAINER:
            net0 = config.get("net0", "name=eth0,bridge=vmbr0,ip=dhcp")
            parts = [p for p in net0.split(",")
                     if not p.lower().startswith("hwaddr=")]
            parts.append(f"hwaddr={mac}")
            ep.config.put(net0=",".join(parts))
        else:
            for key in [f"net{i}" for i in range(4)]:
                val = config.get(key)
                if val:
                    parts = [p for p in val.split(",")
                             if not p.lower().startswith("macaddr=")]
                    parts.append(f"macaddr={mac}")
                    ep.config.put(**{key: ",".join(parts)})
                    break
        return mac

    # -- config / hardware ----------------------------------------------------

    def get_config(self, ref: GuestRef) -> dict:
        return self._endpoint(ref).config.get()

    def update_config(self, ref: GuestRef,
                      changes: Optional[dict] = None,
                      deletes: Optional[list[str]] = None) -> None:
        kwargs = dict(changes or {})
        if deletes:
            kwargs["delete"] = ",".join(deletes)
        self._endpoint(ref).config.put(**kwargs)

    def resize_disk(self, ref: GuestRef, disk: str, size: str) -> None:
        self._endpoint(ref).resize.put(disk=disk, size=size)

    # -- host hardware discovery ---------------------------------------------

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

    # -- backup ---------------------------------------------------------------

    def backup(self, ref: GuestRef, *, mode: str = "snapshot",
               exclude_paths: Optional[list[str]] = None,
               stream_to: Optional[str] = None) -> str:
        """Back up via the Proxmox vzdump API.

        stream_to is accepted for interface parity but not supported through the
        API path (the CLI's --stdout streaming has no API equivalent); passing it
        raises so a caller relying on streaming fails loudly rather than silently
        writing to local storage. The migrations done by hand used the CLI form;
        wiring that here is a follow-up if the orchestrator needs to drive it."""
        if stream_to:
            raise NotImplementedError(
                "streaming backup is CLI-only (vzdump --stdout); not exposed via "
                "the Proxmox API. Drive it out-of-band for now.")
        params: dict = {"vmid": int(ref.id), "mode": mode, "remove": 0}
        if exclude_paths:
            params["exclude-path"] = exclude_paths
        upid = self._px.nodes(self.node).vzdump.post(**params)
        self._wait_task(upid, timeout=3600)
        return upid


# Back-compat alias: the class was ProxmoxClient before the Provider seam landed.
# Kept so any lingering import does not break; new code depends on Provider.
ProxmoxClient = ProxmoxProvider
