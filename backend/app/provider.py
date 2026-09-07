"""Virtualization provider interface.

WHY THIS EXISTS

The orchestrator drives one hypervisor today (Proxmox), but the base OS is a
business decision that may change -- Proxmox VE is AGPLv3, and shipping it in a
commercial appliance carries redistribution obligations a Debian + libvirt +
Incus/Docker base would not. This interface is the seam that keeps that decision
CHEAP: every hypervisor-specific call lives behind it, so a second backend is
"implement a class" rather than "rewrite the orchestrator".

THE CONTRACT IS DELIBERATELY HYPERVISOR-NEUTRAL

  * Guests are CONTAINER or VM -- not "lxc"/"qemu". Proxmox's names are one
    provider's detail; libvirt has domains, Incus has instances. The interface
    must not leak any of them.
  * A guest is addressed by an opaque GuestRef, not a VMID. Proxmox numbers
    guests; libvirt/Incus name them. GuestRef.id is whatever the provider uses;
    callers pass it back verbatim and never parse it.
  * Every state-changing method is synchronous from the caller's view: it
    returns when the change is DONE, or raises. Proxmox's async UPID/task model
    is hidden inside the Proxmox provider; a naturally-synchronous libvirt
    provider just returns. Callers never see task ids.

BACK-COMPAT NOTE. The app around this is still VMID-native (its DB, REST paths
and dashboard JSON use integer vmids and the strings "lxc"/"qemu"). That is a
larger, separate migration. To keep THIS change non-breaking, GuestSummary
exposes `.vmid` and `.kind` (legacy) accessors so existing serialization is
untouched, while new provider operations take GuestRef. De-Proxmoxing the data
model and API contract is a deliberate follow-up, not smuggled in here.

Each method's docstring records what it maps to on the two backends that matter,
so this file doubles as the porting spec when the second provider is written.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class GuestKind(str, Enum):
    """What a guest fundamentally is, independent of hypervisor.

    Proxmox:  CONTAINER -> lxc,        VM -> qemu
    Debian:   CONTAINER -> incus/lxc,  VM -> libvirt/kvm domain
    """
    CONTAINER = "container"
    VM = "vm"

    @classmethod
    def from_proxmox(cls, kind: str) -> "GuestKind":
        """Map a Proxmox kind string ("lxc"/"qemu") to the neutral kind."""
        return cls.CONTAINER if kind == "lxc" else cls.VM

    @property
    def proxmox(self) -> str:
        """The Proxmox kind string for this kind ("lxc"/"qemu").

        Lives here rather than in the Proxmox provider ONLY because the app's
        existing API contract still emits these strings; a pure interface would
        not know them. Remove when the data model stops being VMID/lxc-native."""
        return "lxc" if self is GuestKind.CONTAINER else "qemu"


@dataclass(frozen=True)
class GuestRef:
    """An opaque handle to one guest.

    `id` is whatever the provider addresses a guest by -- a Proxmox VMID as a
    string, a libvirt domain name/UUID, an Incus instance name. Callers pass it
    back verbatim and never parse it. `kind` travels with it because some
    backends need it to route the call and it is always known where a ref is made.
    """
    id: str
    kind: GuestKind


@dataclass
class NodeHealth:
    """Host-level resource snapshot. Same shape regardless of backend --
    Proxmox reads it from the node status API; a Debian provider reads /proc,
    libvirt node info, and statvfs on the storage root."""
    node: str
    cpu: float          # fraction 0.0-1.0
    mem_used: int       # bytes
    mem_total: int
    disk_used: int
    disk_total: int
    uptime: int         # seconds


@dataclass
class GuestSummary:
    """One row of the guest list. `ref` addresses it; the rest is for display.
    cpu is 0.0-1.0; mem/maxmem are bytes."""
    ref: GuestRef
    name: str
    status: str         # provider-normalised: "running" | "stopped" | other
    cpu: float
    mem: int
    maxmem: int

    # -- legacy accessors: keep the VMID-native serialization working unchanged.
    @property
    def vmid(self) -> int:
        """Integer id, for the app's existing DB/JSON that assume numeric vmids.
        Raises if a provider uses non-numeric ids -- which is the signal that the
        data-model migration can no longer be deferred."""
        return int(self.ref.id)

    @property
    def kind(self) -> str:
        """Legacy "lxc"/"qemu" string for the existing API contract."""
        return self.ref.kind.proxmox


class Provider(abc.ABC):
    """The full surface the orchestrator needs from a virtualization backend.

    Implementations: ProxmoxProvider (today), LibvirtProvider / IncusProvider
    (the day licensing forces the move). The orchestrator depends ONLY on this
    class -- construct the concrete provider once at startup and pass it around,
    so no business logic ever imports a backend directly.
    """

    # -- inventory / health ---------------------------------------------------

    @abc.abstractmethod
    def node_health(self) -> NodeHealth:
        """Host CPU/mem/disk/uptime.
        Proxmox: nodes/<node>/status.  Debian: /proc + statvfs + libvirt nodeinfo."""

    @abc.abstractmethod
    def list_guests(self, exclude: Optional[set[str]] = None) -> list[GuestSummary]:
        """Every non-template guest, containers and VMs, sorted by name.
        `exclude` is a set of GuestRef.id to omit (e.g. the orchestrator itself).
        Proxmox: nodes/<node>/lxc + /qemu.  Debian: incus list + virsh list."""

    # -- lifecycle ------------------------------------------------------------

    @abc.abstractmethod
    def start(self, ref: GuestRef) -> None:
        """Boot a guest. Returns when started (or already running), else raises.
        Proxmox: status/start.  Debian: incus start / virsh start."""

    @abc.abstractmethod
    def stop(self, ref: GuestRef) -> None:
        """Stop a guest (hard stop). Idempotent if already stopped.
        Proxmox: status/stop.  Debian: incus stop / virsh destroy."""

    @abc.abstractmethod
    def reboot(self, ref: GuestRef) -> None:
        """Reboot a running guest.
        Proxmox: status/reboot.  Debian: incus restart / virsh reboot."""

    @abc.abstractmethod
    def destroy(self, ref: GuestRef) -> None:
        """Stop (if needed) and permanently delete the guest and its disks.
        MUST purge disks -- a half-deleted guest that leaves volumes behind is
        how storage silently fills. Proxmox: delete purge=1 + destroy-unreferenced-disks.
        Debian: incus delete --force / virsh undefine --remove-all-storage."""

    # -- provisioning ---------------------------------------------------------

    @abc.abstractmethod
    def allocate_id(self) -> str:
        """Reserve a fresh guest id for a clone/create.
        Proxmox: cluster/nextid (a VMID string). Debian: generate a name -- so
        this is where 'numbers vs names' is absorbed."""

    @abc.abstractmethod
    def clone(self, template: GuestRef, new_id: str, name: str) -> GuestRef:
        """Full-clone a template into a new guest; return a ref to it.
        Proxmox: lxc/qemu clone full=1.  Debian: incus copy / virt-clone."""

    @abc.abstractmethod
    def set_fresh_mac(self, ref: GuestRef) -> str:
        """Assign a new locally-administered unicast MAC to the guest's primary
        NIC and return it, so a clone does not inherit the template's MAC and
        collide. Proxmox: rewrite net0 hwaddr / netN macaddr.
        Debian: incus config device set / edit domain XML <mac>."""

    # -- config / hardware ----------------------------------------------------

    @abc.abstractmethod
    def get_config(self, ref: GuestRef) -> dict:
        """Raw provider config for the guest, as a dict. Callers that read it are
        provider-aware by definition; keep such reads rare. Proxmox: config GET.
        Debian: incus config show / virsh dumpxml (parsed)."""

    @abc.abstractmethod
    def update_config(self, ref: GuestRef,
                      changes: Optional[dict] = None,
                      deletes: Optional[list[str]] = None) -> None:
        """Apply config key changes and/or delete keys.
        Proxmox: config PUT (+ delete=).  Debian: incus config set/unset or XML edit.
        NOTE: `changes` keys are provider-shaped today (net0=..., usb0=...). A
        follow-up should lift the common ones (network, memory, cores, passthrough)
        into typed methods so callers stop passing Proxmox-flavoured strings."""

    @abc.abstractmethod
    def resize_disk(self, ref: GuestRef, disk: str, size: str) -> None:
        """Grow a disk. `size` accepts the '+80G' delta form.
        Proxmox: resize PUT.  Debian: lvextend + resize2fs / incus device set size."""

    # -- host hardware discovery ---------------------------------------------

    @abc.abstractmethod
    def list_usb_devices(self) -> list[dict]:
        """USB devices on the host (for passthrough pickers). Best-effort: return
        [] rather than raise. Proxmox: nodes/<node>/scan/usb.  Debian: lsusb parse."""

    @abc.abstractmethod
    def list_pci_devices(self) -> list[dict]:
        """PCI devices on the host. Best-effort, returns [].
        Proxmox: nodes/<node>/hardware/pci.  Debian: lspci parse."""

    # -- backup (the gap that is NOT wrapped today) --------------------------

    @abc.abstractmethod
    def backup(self, ref: GuestRef, *, mode: str = "snapshot",
               exclude_paths: Optional[list[str]] = None,
               stream_to: Optional[str] = None) -> str:
        """Back up a guest; return an identifier/path for the artifact.

        Called out explicitly because it is the biggest thing the orchestrator
        gets from Proxmox for free (vzdump) and does NOT yet wrap -- migrations
        so far shelled out to `vzdump` over SSH. A Debian provider owns this
        entirely (virsh + LVM snapshot, or borg/restic). Making it part of the
        interface now means the port cannot forget it.

        mode: 'snapshot' (live, consistent) | 'stop' (cold).
        stream_to: if set, stream the artifact there instead of local storage."""
