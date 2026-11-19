"""Incus implementation of the Provider interface — the P0 seam for the migration.

WHY A DEDICATED INCUS PROVIDER (distinct from LibvirtProvider)

LibvirtProvider splits the world: VMs -> libvirt/virsh, containers -> Incus. This
provider instead drives EVERYTHING through Incus, because that is the whole reason
the migration picked Incus (SPEC-incus-migration.md §2): one REST/CLI surface for
both system containers AND KVM/QEMU VMs. For a fleet whose default guest is a
Supervised-in-LXC Ctrlable Pro (no VM at all, §6), this is the natural backend —
uniform commands, one tool to learn, one thing to debug in the field.

So the CONTAINER/VM split that complicates LibvirtProvider mostly vanishes here:
`incus start/stop/restart/delete/copy/export` and `incus config ...` are identical
whether the instance is a container or a VM. The only place kind matters is reading
it back from `incus list` (type: "container" | "virtual-machine").

CLI, NOT BINDINGS — same rationale as LibvirtProvider: subprocess against `incus`
keeps the venv dependency-free and mirrors how the orchestrator already shells out.
Where Incus offers JSON (`incus list --format json`) we use it.

IDENTITY IS A NAME. Incus names instances; GuestRef.id is the instance name, so
GuestSummary.vmid (int(ref.id)) raises here exactly as it does for LibvirtProvider
— the VMID-native data model is the documented follow-up, not smuggled in here.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from typing import Optional

from .provider import (
    GuestKind,
    GuestRef,
    GuestSummary,
    NodeHealth,
    Provider,
)


class IncusProvider(Provider):
    def __init__(
        self,
        node: Optional[str] = None,
        storage_root: str = "/var/lib/incus",
        remote: Optional[str] = None,
        backup_dir: str = "/var/backups/ctrlable",
    ):
        self.node = node or os.uname().nodename
        self.storage_root = storage_root if os.path.isdir(storage_root) else "/"
        # remote: None = the local daemon; a name targets `incus --project`/remote
        # (e.g. "prod:") for a networked Incus. Prefixed onto instance names below.
        self.remote = remote or ""
        self.backup_dir = backup_dir
        self._has_incus = shutil.which("incus") is not None

    # -- shell helper ---------------------------------------------------------

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

    def _name(self, ref_or_id) -> str:
        """Instance name, remote-prefixed if this provider targets a remote."""
        rid = ref_or_id.id if isinstance(ref_or_id, GuestRef) else ref_or_id
        return f"{self.remote}{rid}" if self.remote else rid

    @staticmethod
    def _kind_from_type(t: str) -> GuestKind:
        return GuestKind.VM if t == "virtual-machine" else GuestKind.CONTAINER

    @staticmethod
    def _random_mac() -> str:
        # Locally administered, unicast -- same scheme as the other providers so a
        # clone never inherits its template's MAC. Incus wants no colons.
        octets = [0x02, 0x00] + [random.randint(0x00, 0xFF) for _ in range(4)]
        return "".join(f"{b:02x}" for b in octets)

    # -- inventory / health ---------------------------------------------------

    def _incus_server_name(self) -> "str | None":
        """Host name from the Incus API: environment.server_name defaults to the host's hostname.
        Blocking GET /1.0 over the unix socket (node_health is sync, so no async REST client here).
        Returns None on any error — the caller falls back to self.node."""
        import http.client
        import socket as _socket
        try:
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect("/var/lib/incus/unix.socket")
            conn = http.client.HTTPConnection("incus")
            conn.sock = s
            conn.request("GET", "/1.0")
            data = json.loads(conn.getresponse().read())
            conn.close()
            env = (data.get("metadata") or {}).get("environment") or {}
            return env.get("server_name") or None
        except Exception:  # noqa: BLE001 — host name is cosmetic; never fail node_health over it
            return None

    def node_health(self) -> NodeHealth:
        # The orchestrator runs in a container, so /proc here is the CONTAINER's. When the host's
        # /proc is bind-mounted in (firstboot adds `hostproc` -> /host/proc, read-only), read that
        # so the dashboard host card reflects the PHYSICAL host, not the orchestrator container.
        proc = "/host/proc" if os.path.isdir("/host/proc") else "/proc"
        host_root = "/host/root" if os.path.isdir("/host/root") else self.storage_root

        # Host-wide CPU via a short /proc/stat delta; per-guest CPU is skipped
        # (see list_guests) because sampling it makes listing slow.
        def _cpu_snapshot() -> tuple[int, int]:
            with open(f"{proc}/stat") as fh:
                vals = [int(x) for x in fh.readline().split()[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            return idle, sum(vals)

        i0, t0 = _cpu_snapshot()
        time.sleep(0.2)
        i1, t1 = _cpu_snapshot()
        dt = t1 - t0
        cpu = 0.0 if dt <= 0 else max(0.0, min(1.0, 1.0 - (i1 - i0) / dt))

        mem: dict[str, int] = {}
        with open(f"{proc}/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                try:
                    mem[key] = int(rest.strip().split()[0]) * 1024  # kB -> bytes
                except (ValueError, IndexError):
                    pass
        mem_total = mem.get("MemTotal", 0)
        mem_avail = mem.get("MemAvailable", mem.get("MemFree", 0))

        st = os.statvfs(host_root)
        disk_total = st.f_blocks * st.f_frsize
        disk_free = st.f_bavail * st.f_frsize

        # Hostname is special: /proc/sys/kernel/hostname is UTS-namespace-virtualized, so a
        # bind-mounted host /proc still reports the CONTAINER's name (unlike meminfo/uptime, which
        # are not namespaced and read true). The host's real name is the Incus API's
        # environment.server_name (defaults to the host hostname), read over the socket.
        node = self._incus_server_name() or self.node

        with open(f"{proc}/uptime") as fh:
            uptime = int(float(fh.readline().split()[0]))

        return NodeHealth(
            node=node, cpu=cpu,
            mem_used=mem_total - mem_avail, mem_total=mem_total,
            disk_used=disk_total - disk_free, disk_total=disk_total,
            uptime=uptime,
        )

    def list_guests(self, exclude: Optional[set[str]] = None) -> list[GuestSummary]:
        skip = exclude or set()
        try:
            data = json.loads(self._incus("list", "--format", "json"))
        except Exception:
            return []
        out: list[GuestSummary] = []
        for inst in data:
            name = inst.get("name", "")
            if name in skip:
                continue
            state = inst.get("state") or {}
            mem = ((state.get("memory") or {}).get("usage")) or 0
            limits = (inst.get("config") or {}).get("limits.memory") or ""
            out.append(GuestSummary(
                ref=GuestRef(id=name, kind=self._kind_from_type(inst.get("type", ""))),
                name=name,
                status=(inst.get("status", "") or "").lower(),
                cpu=0.0,  # not sampled -- see node docstring
                mem=int(mem),
                maxmem=_parse_bytes(limits),
            ))
        return sorted(out, key=lambda g: g.name)

    # -- lifecycle (uniform across container & VM in Incus) -------------------

    def start(self, ref: GuestRef) -> None:
        self._incus("start", self._name(ref))

    def stop(self, ref: GuestRef) -> None:
        # --force = immediate; matches Proxmox status/stop (not a clean shutdown).
        try:
            self._incus("stop", self._name(ref), "--force")
        except RuntimeError as e:
            if "not running" not in str(e).lower() and "already stopped" not in str(e).lower():
                raise

    def reboot(self, ref: GuestRef) -> None:
        self._incus("restart", self._name(ref))

    def destroy(self, ref: GuestRef) -> None:
        # --force stops it first if running. Incus delete removes the instance AND
        # its storage volume -- no orphaned-disk failure mode to guard (unlike the
        # libvirt provider's --remove-all-storage).
        self._incus("delete", self._name(ref), "--force")

    # -- provisioning ---------------------------------------------------------

    def allocate_id(self) -> str:
        # Name-based, like the libvirt backend: reserve a non-colliding name.
        return "ctrlable-%06x" % random.randint(0, 0xFFFFFF)

    def clone(self, template: GuestRef, new_id: str, name: str) -> GuestRef:
        # `incus copy` works for both containers and VMs; kind is preserved.
        self._incus("copy", self._name(template), self._name(name))
        return GuestRef(id=name, kind=template.kind)

    def deploy_template(
        self,
        *,
        template: str,
        hostname: str,
        image_alias: Optional[str] = None,
        wire_to: Optional[dict] = None,
        profile: str = "default",
        vm: bool = False,
        on_phase=None,
    ) -> GuestRef:
        """Create a fleet instance from a PREBUILT image alias, optionally seed
        firstboot wiring, and start it — the Incus equivalent of the PVE
        `ctrlable-build deploy` step (see deploy.py). Images are built ahead of
        time by incus-bootstrap (build_service / build_zwavejs / partB / the
        proprietary-service builders) and aliased `ctrlable-<template>`, so a
        deploy is launch + wire + start, never a build.

        `wire_to` is written to /etc/ctrlable/firstboot.conf so a container can
        self-configure; the orchestrator-callback model (deploy.py keeps an LXC
        "pending" until its firstboot calls home) remains the primary wiring
        path, and this seed carries the same values — including the
        `provisioning_key` the dali-bridge/hardware-manager `updater.py` enrols
        with to self-install/update from the Store (no GitHub token on-box).

        init-then-start (not `launch`) so the wiring lands BEFORE first boot.
        """
        def phase(n: int, tot: int, msg: str) -> None:
            if on_phase:
                on_phase(n, tot, msg)

        name = self._name(hostname)
        alias = image_alias or ("ctrlable-%s" % template)
        kind = GuestKind.VM if vm else GuestKind.CONTAINER
        tot = 4 if wire_to else 3
        n = 0

        def step(msg: str) -> None:
            nonlocal n
            n += 1
            phase(n, tot, msg)

        step("create %s from %s" % (hostname, alias))
        init_args = ["init", alias, name, "--profile", profile]
        if vm:
            init_args.append("--vm")
        self._incus(*init_args)

        if wire_to:
            step("inject firstboot wiring")
            conf = _render_firstboot_conf(wire_to)
            tmp = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
            try:
                tmp.write(conf)
                tmp.flush()
                tmp.close()
                self._incus("file", "push", tmp.name,
                            "%s/etc/ctrlable/firstboot.conf" % name, "--create-dirs")
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass

        step("start")
        self._incus("start", name)
        step("deployed")
        return GuestRef(id=hostname, kind=kind)

    def set_fresh_mac(self, ref: GuestRef) -> str:
        mac = self._random_mac()
        # Instance-level override on the primary NIC (eth0 from the default
        # profile). Works for container and VM alike.
        self._incus("config", "device", "set", self._name(ref), "eth0",
                    f"hwaddr={mac}")
        return mac

    # -- config / hardware ----------------------------------------------------

    def get_config(self, ref: GuestRef) -> dict:
        # `incus config show` is YAML; return a shallow key/value scrape plus the
        # raw text, without pulling in a YAML dependency (mirrors LibvirtProvider).
        raw = self._incus("config", "show", self._name(ref))
        cfg = {"raw": raw}
        for line in raw.splitlines():
            m = re.match(r"^\s{2}([a-z0-9._-]+):\s*(.*)$", line)
            if m:
                cfg[m.group(1)] = m.group(2).strip()
        return cfg

    def update_config(self, ref: GuestRef,
                      changes: Optional[dict] = None,
                      deletes: Optional[list[str]] = None) -> None:
        changes = changes or {}
        deletes = deletes or []

        # Proxmox-shaped keys (net0=, usbN=, hostpciN=) have no faithful 1:1 and
        # MUST NOT be silently dropped -- they belong in typed methods. Raise so a
        # caller relying on them fails loudly (same contract as LibvirtProvider).
        unsupported = [k for k in list(changes) + deletes
                       if re.match(r"^(net|usb|hostpci|sata|scsi|virtio|ide)\d+$", k)]
        if unsupported:
            raise NotImplementedError(
                "IncusProvider.update_config does not translate Proxmox-shaped "
                f"keys {unsupported}; these need typed methods, not a string bag")

        name = self._name(ref)
        if "onboot" in changes:
            on = str(changes["onboot"]) in ("1", "True", "true", "yes")
            self._incus("config", "set", name, "boot.autostart",
                        "true" if on else "false")

        keymap = {"memory": "limits.memory", "cores": "limits.cpu", "vcpu": "limits.cpu"}
        for key, incus_key in keymap.items():
            if key in changes:
                self._incus("config", "set", name, incus_key, str(changes[key]))

        for key in deletes:
            self._incus("config", "unset", name, key)

    def resize_disk(self, ref: GuestRef, disk: str, size: str) -> None:
        # Incus device size is absolute; a '+NG' delta has no direct form.
        if size.startswith("+"):
            raise NotImplementedError(
                "Incus resize needs an absolute size, not a delta like "
                f"{size!r}; compute the target and pass it")
        self._incus("config", "device", "set", self._name(ref), disk, f"size={size}")

    def device_add(self, ref: GuestRef, dev_name: str, dtype: str,
                   props: Optional[dict] = None) -> None:
        """Attach a device to an instance: `incus config device add <inst> <name> <type> k=v…`
        (usb/gpu/pci/disk/unix-char/…). Hot-plugs on a running instance where Incus supports it."""
        args = ["config", "device", "add", self._name(ref), dev_name, dtype]
        args += [f"{k}={v}" for k, v in (props or {}).items()]
        self._incus(*args)

    def device_remove(self, ref: GuestRef, dev_name: str) -> None:
        self._incus("config", "device", "remove", self._name(ref), dev_name)

    def exec_cmd(self, ref: GuestRef, command: str, timeout: int = 30) -> dict:
        """Run a one-shot command in the instance; return stdout/stderr/exit without raising on a
        non-zero exit (the caller renders it). A lightweight console until the interactive terminal."""
        if not self._has_incus:
            raise RuntimeError("incus not installed")
        r = subprocess.run(["incus", "exec", self._name(ref), "--", "sh", "-lc", command],
                           capture_output=True, text=True, timeout=timeout)
        return {"stdout": r.stdout, "stderr": r.stderr, "exit": r.returncode}

    # -- host hardware discovery (host-level; identical to LibvirtProvider) ---

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
        os.makedirs(self.backup_dir, exist_ok=True)
        if stream_to:
            raise NotImplementedError(
                "streaming backup not implemented for the incus backend yet")
        # `incus export` is snapshot-consistent and works for both containers and
        # VMs, producing one portable tarball -- the single backup/move artifact
        # the migration's data-pool model is built around (§14a).
        dest = os.path.join(self.backup_dir, f"{ref.id}.tar.gz")
        self._incus("export", self._name(ref), dest, timeout=3600)
        return dest


def _render_firstboot_conf(wire_to: dict) -> str:
    """KEY=VALUE lines a container's firstboot sources (mqtt_host, coordinator_url,
    provisioning_key, …). None values are dropped; keys are sorted for a stable,
    diff-friendly file. Values are emitted verbatim — callers pass trusted wiring."""
    lines = [
        "%s=%s" % (k, v)
        for k, v in sorted(wire_to.items())
        if v is not None and v != ""
    ]
    return "\n".join(lines) + ("\n" if lines else "")


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
