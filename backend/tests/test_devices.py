"""Pure device-passthrough logic (app.devices) — SPEC: bind radios by stable
identity, for LXC and VM.

Run:  python3 backend/tests/test_devices.py

Fail-first is inherent: before app.devices existed this file could not import it
(ImportError), so every check was red until the module landed. Stdlib only —
app.devices does no I/O and pulls in no FastAPI, so this runs anywhere.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.devices import (  # noqa: E402
    assignment_absent,
    bound_by_ids,
    detach_plan,
    lxc_passthrough_lines,
    model_is_ambiguous,
    parse_lxc_passthrough,
    parse_pve_dev_passthrough,
    vm_usb_host_arg,
)

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{('  -- ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


ZWAVE = "usb-Silicon_Labs_CP2102N_USB_to_UART_abc123-if00-port0"
ZIGBEE = "usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_V2_20231114-if00"


# ---- lxc_passthrough_lines: by-id bind + cgroup allow -----------------------
lines = lxc_passthrough_lines(ZWAVE, 188, 0)
d = dict(lines)
check("lxc: emits exactly cgroup-allow + mount.entry", len(lines) == 2 and set(d) ==
      {"lxc.cgroup2.devices.allow", "lxc.mount.entry"}, str(lines))
check("lxc: allow rule is specific major:minor rwm",
      d["lxc.cgroup2.devices.allow"] == "c 188:0 rwm", d["lxc.cgroup2.devices.allow"])
check("lxc: binds the by-id node (not /dev/ttyUSB*)",
      d["lxc.mount.entry"].split()[0] == f"/dev/serial/by-id/{ZWAVE}", d["lxc.mount.entry"])
check("lxc: mount target is the SAME by-id path inside the CT (relative)",
      d["lxc.mount.entry"].split()[1] == f"dev/serial/by-id/{ZWAVE}", d["lxc.mount.entry"])
check("lxc: mount is optional + create=file (missing stick won't block boot)",
      "bind,optional,create=file" in d["lxc.mount.entry"], d["lxc.mount.entry"])

try:
    lxc_passthrough_lines(ZWAVE, None, 0)
    check("lxc: missing major raises (must resolve host-side)", False, "no raise")
except ValueError:
    check("lxc: missing major raises (must resolve host-side)", True)


# ---- parse_lxc_passthrough: read back bound by-ids, both conf spellings -----
conf_colon = [
    "arch: amd64",
    "hostname: zwavejs-1",
    "net0: name=eth0,bridge=vmbr0,ip=dhcp,type=veth",
    "rootfs: local-lvm:vm-104-disk-0,size=8G",
    f"lxc.cgroup2.devices.allow: c 188:0 rwm",
    f"lxc.mount.entry: /dev/serial/by-id/{ZWAVE} dev/serial/by-id/{ZWAVE} none bind,optional,create=file 0 0",
]
check("parse: finds the one bound by-id (colon spelling, skips net0's ':')",
      parse_lxc_passthrough(conf_colon) == [ZWAVE], str(parse_lxc_passthrough(conf_colon)))

conf_equals = [
    "lxc.cgroup2.devices.allow = c 188:0 rwm",
    f"lxc.mount.entry = /dev/serial/by-id/{ZWAVE} dev/serial/by-id/{ZWAVE} none bind,optional,create=file 0 0",
]
check("parse: handles native '=' spelling too",
      parse_lxc_passthrough(conf_equals) == [ZWAVE], str(parse_lxc_passthrough(conf_equals)))

check("parse: empty when nothing bound",
      parse_lxc_passthrough(["arch: amd64", "net0: name=eth0,ip=dhcp"]) == [], "")


# ---- detach_plan: pull one stick, leave a sibling on a different minor ------
two_sticks = [
    "arch: amd64",
    "lxc.cgroup2.devices.allow: c 188:0 rwm",
    f"lxc.mount.entry: /dev/serial/by-id/{ZWAVE} dev/serial/by-id/{ZWAVE} none bind,optional,create=file 0 0",
    "lxc.cgroup2.devices.allow: c 188:1 rwm",
    f"lxc.mount.entry: /dev/serial/by-id/{ZIGBEE} dev/serial/by-id/{ZIGBEE} none bind,optional,create=file 0 0",
]
doomed = detach_plan(two_sticks, ZWAVE, 188, 0)
check("detach: removes the target's mount + its 188:0 allow (2 lines)", len(doomed) == 2, str(doomed))
check("detach: targets the right mount line",
      any(ZWAVE in x and "mount.entry" in x for x in doomed), str(doomed))
check("detach: removes 188:0 allow, NOT 188:1",
      any("c 188:0 rwm" in x for x in doomed) and not any("c 188:1 rwm" in x for x in doomed), str(doomed))
check("detach: leaves the sibling zigbee stick's lines intact",
      not any(ZIGBEE in x for x in doomed), str(doomed))
remaining = [l for l in two_sticks if l not in doomed]
check("detach: sibling still fully present after removal",
      ZIGBEE in "".join(remaining) and "c 188:1 rwm" in "".join(remaining), "")


# ---- vm_usb_host_arg: identity by default, port only on demand --------------
check("vm: default binds vendor:product (survives a port move if model unique)",
      vm_usb_host_arg("10c4", "ea60") == "host=10c4:ea60", vm_usb_host_arg("10c4", "ea60"))
check("vm: by_port pins bus-path (the identical-stick fallback)",
      vm_usb_host_arg("10c4", "ea60", busnum=3, devpath="4.1", by_port=True) == "host=3-4.1", "")
try:
    vm_usb_host_arg("10c4", "ea60", by_port=True)
    check("vm: by_port without bus/path raises", False, "no raise")
except ValueError:
    check("vm: by_port without bus/path raises", True)


# ---- model_is_ambiguous: two identical sticks force disambiguation ----------
scan_dup = [{"vendid": "10c4", "prodid": "ea60"}, {"vendid": "10c4", "prodid": "ea60"},
            {"vendid": "1a86", "prodid": "55d4"}]
check("ambiguous: true when two sticks share vendor:product",
      model_is_ambiguous("10c4", "ea60", scan_dup) is True, "")
check("ambiguous: false for the unique model",
      model_is_ambiguous("1a86", "55d4", scan_dup) is False, "")
check("ambiguous: case-insensitive on hex ids",
      model_is_ambiguous("10C4", "EA60", scan_dup) is True, "")


# ---- assignment_absent: flag a bound-but-unplugged radio --------------------
check("absent: bound stick not currently present -> absent",
      assignment_absent(ZWAVE, [ZIGBEE]) is True, "")
check("absent: present stick -> not absent",
      assignment_absent(ZWAVE, [ZWAVE, ZIGBEE]) is False, "")



# ---- parse_pve_dev_passthrough: PVE-native devN binds (the false-free bug) ---
pve_conf = [
    "arch: amd64",
    "hostname: zigbee2mqtt-1",
    f"dev0: /dev/serial/by-id/{ZIGBEE},gid=20",
    "net0: name=eth0,bridge=vmbr0,ip=dhcp,type=veth",
]
check("pve-dev: finds a devN-bound by-id (stored as dev0:, never a mount.entry)",
      parse_pve_dev_passthrough(pve_conf) == [ZIGBEE], str(parse_pve_dev_passthrough(pve_conf)))
check("pve-dev: strips the ,uid=/,gid=/,mode= option suffix",
      parse_pve_dev_passthrough([f"dev1: /dev/serial/by-id/{ZWAVE},uid=0,gid=20,mode=0660"]) == [ZWAVE], "")
check("pve-dev: a raw mount.entry is NOT a devN (left to parse_lxc_passthrough)",
      parse_pve_dev_passthrough(conf_colon) == [], str(parse_pve_dev_passthrough(conf_colon)))
check("pve-dev: ignores a devN pointing outside /dev/serial/by-id",
      parse_pve_dev_passthrough(["dev0: /dev/dri/renderD128"]) == [], "")

# ---- bound_by_ids: union of both transports = the free/in-use decision -------
mixed = conf_colon + [f"dev0: /dev/serial/by-id/{ZIGBEE},gid=20"]
check("bound: unions raw-mount + devN binds",
      bound_by_ids(mixed) == [ZWAVE, ZIGBEE], str(bound_by_ids(mixed)))
check("bound: a devN-only coordinator reads as bound (the reported false-free)",
      assignment_absent(ZIGBEE, bound_by_ids(pve_conf)) is False, str(bound_by_ids(pve_conf)))
check("bound: de-dups a device bound by BOTH spellings",
      bound_by_ids([f"lxc.mount.entry: /dev/serial/by-id/{ZWAVE} dev/serial/by-id/{ZWAVE} none bind,optional,create=file 0 0",
                    f"dev0: /dev/serial/by-id/{ZWAVE}"]) == [ZWAVE], "")


print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all device-passthrough checks passed")
