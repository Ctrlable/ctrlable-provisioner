"""Debian-native implementation of the Provider interface.

This is the second backend the seam in provider.py exists for: a Ctrlable
appliance on plain Debian instead of Proxmox VE (see provider.py for the AGPL /
licensing motivation). It composes the two tools that do on Debian what Proxmox
bundles:

    VMs        -> libvirt / KVM   (via the `virsh` CLI + `virt-clone`)
    CONTAINERS -> Incus           (via the `incus` CLI)

Named LibvirtProvider because libvirt is the VM engine that gives the file its
character; containers ride Incus, which is the LXD fork Debian ships and the
natural successor to Proxmox's LXC.

WHY THE CLI, NOT THE PYTHON BINDINGS

subprocess against `virsh`/`incus` keeps this provider dependency-free -- no
libvirt-python or pylxd added to the venv -- and mirrors how the rest of the
orchestrator already shells out (deploy.py). The cost is output parsing; where a
tool offers JSON (`incus list --format json`) we use it, and where it does not
(virsh) we parse narrowly and defensively.

IDENTITY IS A NAME, NOT A NUMBER

Proxmox numbers guests; libvirt and Incus name them. So GuestRef.id here is the
domain/instance NAME. This is the one place the app's still-VMID-native data
model shows through: GuestSummary.vmid does int(ref.id) and will raise on these
name ids. That is deliberate and documented in provider.py -- wiring this backend
into the current dashboard needs the data-model follow-up first. The provider
itself is complete and testable standalone regardless.

HONEST LIMITATIONS (called out, not hidden)

  * Per-guest CPU% is not sampled -- reported as 0.0. Proxmox hands it over free;
    here it needs a delta pass (read cpu.time twice) that would make listing slow.
    node_health CPU *is* real (a /proc/stat delta on the host only).
  * update_config translates the portable keys (onboot); Proxmox-shaped strings
    (net0=, usbN=, hostpciN=) raise, by design -- they belong in typed methods,
    per the note in provider.py, not in a string bag that pretends to be neutral.
  * backup does cold (stopped) VM backups and Incus export; live VM snapshotting
    is storage-specific and left explicit rather than faked.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Optional

from .provider import (
    GuestKind,
    GuestRef,
    GuestSummary,
    NodeHealth,
    Provider,
)


class LibvirtProvider(Provider):
    def __init__(
        self,
        node: Optional[str] = None,
        storage_root: str = "/var/lib/libvirt/images",
        libvirt_uri: str = "qemu:///system",
        backup_dir: str = "/var/backups/ctrlable",
    ):
        self.node = node or os.uname().nodename
        # Where to measure free space for node_health, and where VM disks live.
        self.storage_root = storage_root if os.path.isdir(storage_root) else "/"
        self.libvirt_uri = libvirt_uri
        self.backup_dir = backup_dir
        self._has_virsh = shutil.which("virsh") is not None
        self._has_incus = shutil.which("incus") is not None
        self._has_virtclone = shutil.which("virt-clone") is not None

    # -- shell helpers --------------------------------------------------------

    def _virsh(self, *args: str, timeout: int = 60) -> str:
        if not self._has_virsh:
            raise RuntimeError("virsh not installed (apt install libvirt-clients)")
        r = subprocess.run(
            ["virsh", "-c", self.libvirt_uri, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            raise RuntimeError(f"virsh {' '.join(args)}: {r.stderr.strip()[:200]}")
        return r.stdout

    def _incus(self, *args: str, timeout: int = 60) -> str:
        if not self._has_incus:
            raise RuntimeError("incus not installed (apt install incus)")
        r = subprocess.run(
            ["incus", *args],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            raise RuntimeError(f"incus {' '.join(args)}: {r.stderr.strip()[:200]}")
        return r.stdout

    @staticmethod
    def _random_mac() -> str:
        # Locally administered, unicast -- same scheme as the Proxmox provider so
        # a clone never inherits its template's MAC.
        octets = [0x02, 0x00] + [random.randint(0x00, 0xFF) for _ in range(4)]
        return ":".join(f"{b:02x}" for b in octets)

    def _endpoint_ok(self, ref: GuestRef) -> None:
        need = self._has_incus if ref.kind is GuestKind.CONTAINER else self._has_virsh
        if not need:
            tool = "incus" if ref.kind is GuestKind.CONTAINER else "virsh"
            raise RuntimeError(f"{tool} not installed; cannot act on {ref.id}")

    # -- inventory / health ---------------------------------------------------

    def node_health(self) -> NodeHealth:
        # CPU: a real /proc/stat delta over a short window. Host-wide only, which
        # is cheap; per-guest CPU is the expensive part we skip (see list_guests).
        def _cpu_snapshot() -> tuple[int, int]:
            with open("/proc/stat") as fh:
                vals = [int(x) for x in fh.readline().split()[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            return idle, sum(vals)

        i0, t0 = _cpu_snapshot()
        time.sleep(0.2)
        i1, t1 = _cpu_snapshot()
        dt = t1 - t0
        cpu = 0.0 if dt <= 0 else max(0.0, min(1.0, 1.0 - (i1 - i0) / dt))

        mem: dict[str, int] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                try:
                    mem[key] = int(rest.strip().split()[0]) * 1024  # kB -> bytes
                except (ValueError, IndexError):
                    pass
        mem_total = mem.get("MemTotal", 0)
        mem_avail = mem.get("MemAvailable", mem.get("MemFree", 0))

        st = os.statvfs(self.storage_root)
        disk_total = st.f_blocks * st.f_frsize
        disk_free = st.f_bavail * st.f_frsize

        with open("/proc/uptime") as fh:
            uptime = int(float(fh.readline().split()[0]))

        return NodeHealth(
            node=self.node,
            cpu=cpu,
            mem_used=mem_total - mem_avail,
            mem_total=mem_total,
            disk_used=disk_total - disk_free,
            disk_total=disk_total,
            uptime=uptime,
        )

    def list_guests(self, exclude: Optional[set[str]] = None) -> list[GuestSummary]:
        skip = exclude or set()
        out: list[GuestSummary] = []

        # Containers via Incus (JSON -- reliable).
        if self._has_incus:
            try:
                data = json.loads(self._incus("list", "--format", "json"))
            except Exception:
                data = []
            for inst in data:
                name = inst.get("name", "")
                if name in skip:
                    continue
                state = inst.get("state") or {}
                mem = ((state.get("memory") or {}).get("usage")) or 0
                limits = (inst.get("config") or {}).get("limits.memory") or ""
                out.append(GuestSummary(
                    ref=GuestRef(id=name, kind=GuestKind.CONTAINER),
                    name=name,
                    status=(inst.get("status", "") or "").lower(),
                    cpu=0.0,  # not sampled -- see module docstring
                    mem=int(mem),
                    maxmem=_parse_bytes(limits),
                ))

        # VMs via virsh. `virsh list --all --name` then dominfo per domain.
        if self._has_virsh:
            try:
                names = [n for n in self._virsh("list", "--all", "--name").split("\n")
                         if n.strip()]
            except Exception:
                names = []
            for name in names:
                if name in skip:
                    continue
                status, mem, maxmem = "unknown", 0, 0
                try:
                    info = self._virsh("dominfo", name)
                    for line in info.splitlines():
                        k, _, v = line.partition(":")
                        k, v = k.strip(), v.strip()
                        if k == "State":
                            status = "running" if v == "running" else \
                                     "stopped" if v in ("shut off", "shutoff") else v
                        elif k == "Used memory":
                            mem = _parse_bytes(v)
                        elif k == "Max memory":
                            maxmem = _parse_bytes(v)
                except Exception:
                    pass
                out.append(GuestSummary(
                    ref=GuestRef(id=name, kind=GuestKind.VM),
                    name=name, status=status, cpu=0.0, mem=mem, maxmem=maxmem,
                ))

        return sorted(out, key=lambda g: g.name)

    # -- lifecycle ------------------------------------------------------------

    def start(self, ref: GuestRef) -> None:
        self._endpoint_ok(ref)
        if ref.kind is GuestKind.CONTAINER:
            self._incus("start", ref.id)
        else:
            self._virsh("start", ref.id)

    def stop(self, ref: GuestRef) -> None:
        self._endpoint_ok(ref)
        if ref.kind is GuestKind.CONTAINER:
            self._incus("stop", ref.id, "--force")
        else:
            # destroy = hard stop in virsh terms; matches Proxmox status/stop.
            try:
                self._virsh("destroy", ref.id)
            except RuntimeError as e:
                if "not running" not in str(e) and "domain is not running" not in str(e):
                    raise

    def reboot(self, ref: GuestRef) -> None:
        self._endpoint_ok(ref)
        if ref.kind is GuestKind.CONTAINER:
            self._incus("restart", ref.id)
        else:
            self._virsh("reboot", ref.id)

    def destroy(self, ref: GuestRef) -> None:
        self._endpoint_ok(ref)
        if ref.kind is GuestKind.CONTAINER:
            self._incus("delete", ref.id, "--force")
        else:
            try:
                self._virsh("destroy", ref.id)
            except RuntimeError:
                pass  # already stopped
            # --remove-all-storage is the libvirt equivalent of purge=1: a guest
            # deleted without it leaves disk images behind, which is how storage
            # silently fills -- the exact failure the Proxmox provider guards.
            self._virsh("undefine", ref.id, "--remove-all-storage", "--nvram")

    # -- provisioning ---------------------------------------------------------

    def allocate_id(self) -> str:
        # Debian is name-based: the "id" a caller reserves is a unique name.
        # No cluster counter to query; generate one that will not collide.
        return "ctrlable-%06x" % random.randint(0, 0xFFFFFF)

    def clone(self, template: GuestRef, new_id: str, name: str) -> GuestRef:
        self._endpoint_ok(template)
        if template.kind is GuestKind.CONTAINER:
            self._incus("copy", template.id, name)
            return GuestRef(id=name, kind=GuestKind.CONTAINER)
        if not self._has_virtclone:
            raise RuntimeError("virt-clone not installed (apt install virtinst)")
        subprocess.run(
            ["virt-clone", "--connect", self.libvirt_uri,
             "--original", template.id, "--name", name, "--auto-clone"],
            capture_output=True, text=True, timeout=600, check=True,
        )
        return GuestRef(id=name, kind=GuestKind.VM)

    def set_fresh_mac(self, ref: GuestRef) -> str:
        self._endpoint_ok(ref)
        mac = self._random_mac()
        if ref.kind is GuestKind.CONTAINER:
            # Incus: set the volatile hwaddr on the primary NIC.
            self._incus("config", "device", "set", ref.id, "eth0",
                        f"hwaddr={mac}")
            return mac
        # libvirt: rewrite the first <mac> in the domain XML and redefine.
        # Requires the domain to be shut off; a running domain keeps its live MAC
        # until next boot, same caveat as editing any persistent config.
        xml = self._virsh("dumpxml", ref.id)
        new_xml, n = re.subn(r'(<mac address=")[0-9a-fA-F:]+(")',
                             rf'\g<1>{mac}\g<2>', xml, count=1)
        if n == 0:
            raise RuntimeError(f"{ref.id}: no <mac> element found to rewrite")
        self._define_from_xml(new_xml)
        return mac

    # -- config / hardware ----------------------------------------------------

    def get_config(self, ref: GuestRef) -> dict:
        self._endpoint_ok(ref)
        if ref.kind is GuestKind.CONTAINER:
            # incus config show is YAML; return the parsed-enough subset plus raw,
            # without pulling in a YAML dependency.
            raw = self._incus("config", "show", ref.id)
            cfg = {"raw": raw}
            for line in raw.splitlines():
                m = re.match(r"^\s{2}([a-z0-9._-]+):\s*(.*)$", line)
                if m:
                    cfg[m.group(1)] = m.group(2).strip()
            return cfg
        # VMs: return the XML plus a couple of normalised fields.
        xml = self._virsh("dumpxml", ref.id)
        cfg = {"xml": xml}
        try:
            root = ET.fromstring(xml)
            cfg["memory"] = int((root.findtext("memory") or "0"))
            cfg["vcpu"] = int((root.findtext("vcpu") or "0"))
        except ET.ParseError:
            pass
        return cfg

    def update_config(self, ref: GuestRef,
                      changes: Optional[dict] = None,
                      deletes: Optional[list[str]] = None) -> None:
        self._endpoint_ok(ref)
        changes = changes or {}
        deletes = deletes or []

        # Only the portable keys are honoured. Proxmox-shaped strings (net0=,
        # usbN=, hostpciN=) have no faithful 1:1 here and MUST NOT be silently
        # dropped -- they need typed methods (set_network/add_usb/...), per the
        # interface note. Raise so a caller relying on them fails loudly.
        unsupported = [k for k in list(changes) + deletes
                       if re.match(r"^(net|usb|hostpci|sata|scsi|virtio|ide)\d+$", k)]
        if unsupported:
            raise NotImplementedError(
                "LibvirtProvider.update_config does not translate Proxmox-shaped "
                f"keys {unsupported}; these need typed methods, not a string bag")

        if "onboot" in changes:
            on = str(changes["onboot"]) in ("1", "True", "true", "yes")
            if ref.kind is GuestKind.CONTAINER:
                self._incus("config", "set", ref.id,
                            "boot.autostart", "true" if on else "false")
            else:
                self._virsh("autostart", *([] if on else ["--disable"]), ref.id)

        for key in ("memory", "cores", "vcpu"):
            if key in changes and ref.kind is GuestKind.CONTAINER:
                incus_key = {"memory": "limits.memory",
                             "cores": "limits.cpu", "vcpu": "limits.cpu"}[key]
                self._incus("config", "set", ref.id, incus_key, str(changes[key]))
            # VM memory/vcpu live changes go through virsh setmem/setvcpus; left
            # to a typed method rather than guessed at here.

    def resize_disk(self, ref: GuestRef, disk: str, size: str) -> None:
        self._endpoint_ok(ref)
        if ref.kind is GuestKind.CONTAINER:
            # Incus wants an absolute size on the device; a '+NG' delta has no
            # direct form, so reject it clearly rather than misapply.
            if size.startswith("+"):
                raise NotImplementedError(
                    "Incus resize needs an absolute size, not a delta like "
                    f"{size!r}; compute the target and pass it")
            self._incus("config", "device", "set", ref.id, disk, f"size={size}")
        else:
            # virsh blockresize takes an absolute size; libvirt has no delta form.
            if size.startswith("+"):
                raise NotImplementedError(
                    "virsh blockresize needs an absolute size, not a delta")
            self._virsh("blockresize", ref.id, "--path", disk, "--size", size)

    # -- host hardware discovery ---------------------------------------------

    def list_usb_devices(self) -> list[dict]:
        if not shutil.which("lsusb"):
            return []
        try:
            out = subprocess.run(["lsusb"], capture_output=True, text=True,
                                 timeout=15).stdout
        except Exception:
            return []
        devs = []
        for line in out.splitlines():
            m = re.match(r"Bus (\d+) Device (\d+): ID ([0-9a-f]{4}):([0-9a-f]{4})\s*(.*)",
                         line)
            if m:
                devs.append({
                    "busnum": int(m.group(1)), "devnum": int(m.group(2)),
                    "id": f"{m.group(3)}:{m.group(4)}",
                    "vendor_id": m.group(3), "product_id": m.group(4),
                    "product": m.group(5).strip(),
                })
        return devs

    def list_pci_devices(self) -> list[dict]:
        if not shutil.which("lspci"):
            return []
        try:
            out = subprocess.run(["lspci", "-Dmm"], capture_output=True, text=True,
                                 timeout=15).stdout
        except Exception:
            return []
        devs = []
        for line in out.splitlines():
            # -Dmm: 0000:00:00.0 "Class" "Vendor" "Device" -rNN "SVendor" "SDevice"
            fields = re.findall(r'"([^"]*)"|(\S+)', line)
            flat = [a or b for a, b in fields]
            if len(flat) >= 4:
                devs.append({
                    "id": flat[0], "class": flat[1],
                    "vendor": flat[2], "device": flat[3],
                })
        return devs

    # -- backup ---------------------------------------------------------------

    def backup(self, ref: GuestRef, *, mode: str = "snapshot",
               exclude_paths: Optional[list[str]] = None,
               stream_to: Optional[str] = None) -> str:
        self._endpoint_ok(ref)
        os.makedirs(self.backup_dir, exist_ok=True)
        if stream_to:
            raise NotImplementedError(
                "streaming backup not implemented for the libvirt backend yet")

        if ref.kind is GuestKind.CONTAINER:
            # incus export is snapshot-consistent regardless of mode; it always
            # produces a portable tarball.
            dest = os.path.join(self.backup_dir, f"{ref.id}.tar.gz")
            self._incus("export", ref.id, dest, timeout=3600)
            return dest

        # VM: cold backup only. A live-consistent VM snapshot is storage-specific
        # (external snapshot + blockcommit, or LVM/ZFS snapshot) and is left
        # explicit rather than faked -- this is the gap the interface flags.
        if mode != "stop":
            raise NotImplementedError(
                "live VM snapshot backup is storage-specific; call with "
                "mode='stop' for a cold backup, or drive an external snapshot")
        was_running = "running" in self._virsh("dominfo", ref.id)
        if was_running:
            self._virsh("shutdown", ref.id)
            for _ in range(60):
                if "running" not in self._virsh("dominfo", ref.id):
                    break
                time.sleep(2)
        dest_dir = os.path.join(self.backup_dir, ref.id)
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "domain.xml"), "w") as fh:
            fh.write(self._virsh("dumpxml", ref.id))
        # Copy each disk image referenced by the domain.
        root = ET.fromstring(self._virsh("dumpxml", ref.id))
        for disk in root.findall(".//devices/disk/source"):
            path = disk.get("file") or disk.get("dev")
            if path and os.path.exists(path):
                shutil.copy2(path, dest_dir)
        if was_running:
            self._virsh("start", ref.id)
        return dest_dir

    # -- internal -------------------------------------------------------------

    def _define_from_xml(self, xml: str) -> None:
        """Redefine a domain from XML via a temp file (virsh define needs a path)."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as fh:
            fh.write(xml)
            path = fh.name
        try:
            self._virsh("define", path)
        finally:
            os.unlink(path)


def _parse_bytes(s: str) -> int:
    """Turn '2GiB', '2048 MiB', '1073741824', '2G' into bytes. Returns 0 on junk."""
    if not s:
        return 0
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    m = re.match(r"([\d.]+)\s*([KMGT]?)i?B?", s, re.IGNORECASE)
    if not m:
        return 0
    val = float(m.group(1))
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    return int(val * mult.get(m.group(2).upper(), 1))
