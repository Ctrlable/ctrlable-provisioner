"""Device-passthrough helpers (USB serial coordinators, PCI) — pure, no host I/O.

The orchestrator assigns radios (Zigbee / Z-Wave / Thread USB sticks, or PCI
accelerators such as a Coral TPU) to guests. There are two transports, chosen by
guest kind:

  * VM (qemu):  ``usbN=host=<vendor>:<product>``  /  ``hostpciN=<pciid>,...``
                — set through the PVE API (``update_config``), already wired.
  * LXC:        raw ``lxc.cgroup2.devices.allow`` + ``lxc.mount.entry`` lines
                written into ``/etc/pve/lxc/<id>.conf`` by the host wrapper.
                The PVE API does **not** accept raw ``lxc.*`` keys, so LXC
                passthrough must go through a ``ctrlable-build`` subcommand, not
                the API provider.

The golden rule is **bind by stable identity, never by the kernel's ephemeral
``/dev/ttyUSB*`` / ``ttyACM*`` name** (which renumbers on replug, reboot, or when
a sibling device appears — the same class of bug as binding a mic by card index):

  * ``/dev/serial/by-id/usb-<vendor>_<product>_<serial>-ifNN`` — stable per
    *stick*; survives a move to another port. **Preferred.**
  * ``/dev/serial/by-path/...`` — stable per *port*; the only disambiguator when
    two identical sticks share vendor:product and expose no unique serial.

These functions turn a chosen identity into the exact config lines, read back
what a guest already has, and flag the ambiguous (duplicate-model) and absent
(unplugged / renumbered) cases. They do no I/O so they are unit-testable without
a host; the host enumeration (resolving a by-id symlink to its major:minor) and
the conf write live in the host wrapper.
"""
from __future__ import annotations

import re

BYID_DIR = "/dev/serial/by-id"
# The char-device majors a USB serial coordinator presents as.
USB_SERIAL_MAJORS = {188: "ttyUSB (usb-serial)", 166: "ttyACM (CDC-ACM)"}

# A raw conf line: "<key><: or => <value>". The key is a dotted token, so the
# first separator is unambiguous even though values may contain ':' (e.g.
# "c 188:0 rwm") or '=' (e.g. "name=eth0,...").
_KV = re.compile(r"^\s*([\w.]+)\s*[:=]\s*(.*?)\s*$")


def _split_kv(line: str) -> tuple[str, str] | None:
    """(key, value) for one raw ``/etc/pve/lxc/<id>.conf`` line, or None if it is
    blank/uncparseable. Handles both the Proxmox ``key: value`` spelling and the
    native-LXC ``key = value`` spelling."""
    m = _KV.match(line)
    return (m.group(1), m.group(2)) if m else None


def lxc_passthrough_lines(by_id: str, major: int, minor: int) -> list[tuple[str, str]]:
    """Raw ``/etc/pve/lxc/<id>.conf`` (key, value) pairs that hand an LXC one USB
    serial device, bound by its stable ``/dev/serial/by-id`` handle.

    - the cgroup rule allows the char device ``major:minor`` (read/write/mknod);
    - the bind mount exposes the device inside the container at the **same**
      ``/dev/serial/by-id/<by_id>`` path it has on bare metal, so Home Assistant
      / zwave-js-ui / zigbee2mqtt find it unchanged. ``optional`` means a missing
      stick does not block container start (it just comes up without the radio,
      which absence-detection then surfaces); ``create=file`` makes the target.

    Keys are returned as a list (not a dict) because the conf legitimately repeats
    these keys once per device.
    """
    if major is None or minor is None:
        raise ValueError("major/minor are required — resolve them host-side from the by-id node")
    src = f"{BYID_DIR}/{by_id}"
    dst = f"dev/serial/by-id/{by_id}"  # relative; LXC roots it at the container's /
    return [
        ("lxc.cgroup2.devices.allow", f"c {major}:{minor} rwm"),
        ("lxc.mount.entry", f"{src} {dst} none bind,optional,create=file 0 0"),
    ]


def parse_lxc_passthrough(conf_lines: list[str]) -> list[str]:
    """The by-id handles an LXC is currently bound to, read from its conf lines.

    Looks at ``lxc.mount.entry`` lines whose source is under ``/dev/serial/by-id``.
    Returns the by-id basenames, in file order.
    """
    out: list[str] = []
    prefix = f"{BYID_DIR}/"
    for raw in conf_lines:
        kv = _split_kv(raw)
        if not kv or kv[0] != "lxc.mount.entry":
            continue
        parts = kv[1].split()
        src = parts[0] if parts else ""
        if src.startswith(prefix):
            out.append(src[len(prefix):])
    return out


def detach_plan(conf_lines: list[str], by_id: str, major: int, minor: int) -> list[str]:
    """The exact raw lines to delete to remove ``by_id`` from an LXC: its bind
    mount plus the cgroup-allow line for its ``major:minor``.

    We emit one allow line per device at its *specific* ``major:minor`` (each
    stick has a distinct minor), so the allow line is 1:1 with the device and is
    always removed with it — no need to keep it for other sticks, which carry
    their own allow lines. A hand-written broad rule (e.g. ``c 188:* rwm``) will
    not match the specific ``allow_val`` and is deliberately left untouched.
    Returns matched lines verbatim (as given in ``conf_lines``).
    """
    allow_val = f"c {major}:{minor} rwm"
    want_src = f"{BYID_DIR}/{by_id}"
    doomed: list[str] = []
    for raw in conf_lines:
        kv = _split_kv(raw)
        if not kv:
            continue
        key, value = kv
        if key == "lxc.mount.entry":
            parts = value.split()
            if parts and parts[0] == want_src:
                doomed.append(raw)
        elif key == "lxc.cgroup2.devices.allow" and value == allow_val:
            doomed.append(raw)
    return doomed


def vm_usb_host_arg(vendor_id: str, product_id: str, *,
                    busnum: int | None = None, devpath: str | None = None,
                    by_port: bool = False) -> str:
    """The ``host=`` value for a VM ``usbN`` line.

    Default is ``host=<vendor>:<product>`` (identity — survives a port move while
    that model is unique on the box). When two identical sticks force
    disambiguation, pass ``by_port=True`` with ``busnum``/``devpath`` to pin the
    physical port (``host=<bus>-<path>``) — stable per port, but it does **not**
    survive a move, which is the inherent limit of VM USB passthrough.
    """
    if by_port:
        if busnum is None or not devpath:
            raise ValueError("by_port requires busnum and devpath")
        return f"host={busnum}-{devpath}"
    return f"host={vendor_id}:{product_id}"


def model_is_ambiguous(vendor_id: str, product_id: str, scan: list[dict]) -> bool:
    """True if more than one connected USB device shares this vendor:product, so
    a vendor:product binding would be ambiguous and the caller must fall back to a
    per-port / per-serial identity. ``scan`` items use the PVE scan keys
    (``vendid``/``prodid``).
    """
    n = sum(1 for d in scan
            if str(d.get("vendid", "")).lower() == str(vendor_id).lower()
            and str(d.get("prodid", "")).lower() == str(product_id).lower())
    return n > 1


def assignment_absent(by_id: str, present_by_ids: list[str]) -> bool:
    """True if a guest is bound to ``by_id`` but that stick is not currently
    present on the host — the signal to flag a radio as missing rather than let a
    service silently open the wrong (or no) device.
    """
    return by_id not in set(present_by_ids)
