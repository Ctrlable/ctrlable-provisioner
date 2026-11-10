"""ctrlable-build attaches each guest to the internal management LAN (SPEC § 20).

Run:  python3 host/tests/test_internal_net_nic.py

ctrlable-build is a standalone script on the PVE host, so it carries its own
copy of the internal-net resolution (the authoritative one is
backend/app/internal_net.py). This pins the behaviour the deploy path depends on:
an LXC gets a STATIC net1 at its mapped address, the HAOS VM gets a net1 + a
dnsmasq MAC reservation, and — crucially — BOTH no-op cleanly when the bridge is
not on the host yet, so a deploy on a host without the internal LAN still works.

No PVE host: subprocess is replaced with a recorder, so the exact `pct`/`qm`
commands are asserted without running them.
"""
import importlib.machinery
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "..", "ctrlable-build")
_loader = importlib.machinery.SourceFileLoader("ctrlable_build", PATH)
_spec = importlib.util.spec_from_loader("ctrlable_build", _loader)
cb = importlib.util.module_from_spec(_spec)
_loader.exec_module(cb)

# Originals captured before any test stubs them, so later blocks can restore.
_ORIG_is_ours = cb._bridge_is_ours
_ORIG_phys = cb._bridge_has_phys_member

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{('  -- ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


class Recorder:
    """Stand-in for subprocess.run that records argv and returns canned output."""
    def __init__(self):
        self.calls = []
        self.outputs = {}      # first-two-argv-words -> stdout text

    def __call__(self, argv, *a, **kw):
        self.calls.append(list(argv))
        class R:
            returncode = 0
            stdout = ""
        key = " ".join(argv[:2])
        R.stdout = self.outputs.get(key, "")
        return R

    def argvs(self):
        return [" ".join(c) for c in self.calls]


MANIFEST = {"network": {"internal": {
    "enabled": True, "bridge": "vmbr1", "subnet": "198.18.0.0/24",
    "assignments": {"ctrlable-pro": 10, "zigbee2mqtt": 21},
}}}


# ---- resolution + address math ---------------------------------------------
cfg = cb._internal_net_cfg(MANIFEST)
check("cfg parsed", cfg and cfg["bridge"] == "vmbr1" and cfg["subnet"] == "198.18.0.0/24")
check("ip for zigbee2mqtt is .21/24", cb._internal_ip(cfg, "zigbee2mqtt") == "198.18.0.21/24",
      cb._internal_ip(cfg, "zigbee2mqtt"))
check("ip for ctrlable-pro is .10/24", cb._internal_ip(cfg, "ctrlable-pro") == "198.18.0.10/24")
check("unknown template -> None", cb._internal_ip(cfg, "nope") is None)
check("disabled -> no cfg", cb._internal_net_cfg({"network": {"internal": {"enabled": False}}}) is None)
check("absent -> no cfg", cb._internal_net_cfg({}) is None)
try:
    cb._internal_ip({"subnet": "198.18.0.0/24", "assignments": {"x": 999}}, "x")
    check("out-of-range octet exits", False, "no exit")
except SystemExit:
    check("out-of-range octet exits", True)


# ---- LXC net1: static, and skipped when the bridge is absent ----------------
rec = Recorder(); cb.subprocess.run = rec
cb._bridge_exists = lambda b: True
cb._add_internal_lxc_nic(105, "zigbee2mqtt", MANIFEST)
want = "pct set 105 -net1 name=eth1,bridge=vmbr1,ip=198.18.0.21/24"
check("LXC gets the right static net1", want in rec.argvs(), str(rec.argvs()))

rec2 = Recorder(); cb.subprocess.run = rec2
cb._bridge_exists = lambda b: False
cb._add_internal_lxc_nic(105, "zigbee2mqtt", MANIFEST)
check("LXC net1 SKIPPED when bridge absent (no pct call)",
      not any(c[:2] == ["pct", "set"] for c in rec2.calls), str(rec2.argvs()))


# ---- VM net1 + dnsmasq reservation ------------------------------------------
rec3 = Recorder()
rec3.outputs["qm config"] = "net1: virtio=BC:24:11:AA:BB:CC,bridge=vmbr1\n"
cb.subprocess.run = rec3
cb._bridge_exists = lambda b: True
# make the dnsmasq write a no-op/visible: point Path at a temp dir
import tempfile, pathlib
tmp = tempfile.mkdtemp()
_real_reserve = cb._dnsmasq_reserve
reserved = {}
cb._dnsmasq_reserve = lambda vmid, mac, ip: reserved.update(vmid=vmid, mac=mac, ip=ip)
cb._add_internal_vm_nic(100, "ctrlable-pro", MANIFEST)
check("VM gets net1 on vmbr1", "qm set 100 -net1 virtio,bridge=vmbr1" in rec3.argvs(), str(rec3.argvs()))
check("VM dnsmasq reservation: .10 for its net1 MAC",
      reserved.get("ip") == "198.18.0.10" and reserved.get("mac") == "BC:24:11:AA:BB:CC",
      str(reserved))
cb._dnsmasq_reserve = _real_reserve


# ---- _dnsmasq_reserve: dhcp-hostsfile upsert by IP (the real fn) ------------
import pathlib, tempfile
rf = pathlib.Path(tempfile.mkdtemp()) / "reservations"
rec4 = Recorder(); cb.subprocess.run = rec4          # stub systemctl reload
cb._dnsmasq_reserve(100, "BC:24:11:AA:BB:CC", "198.18.0.10", resv=rf)
cb._dnsmasq_reserve(101, "BC:24:11:DD:EE:FF", "198.18.0.20", resv=rf)
cb._dnsmasq_reserve(100, "BC:24:11:99:88:77", "198.18.0.10", resv=rf)   # redeploy: new MAC, same slot
body = rf.read_text()
check("other slot (.20) kept", "BC:24:11:DD:EE:FF,198.18.0.20" in body, body)
check("redeploy replaced the .10 owner",
      "BC:24:11:AA:BB:CC" not in body and "BC:24:11:99:88:77,198.18.0.10" in body, body)
check("exactly one reservation for .10",
      sum(1 for l in body.splitlines() if l.strip().endswith(",198.18.0.10")) == 1, body)
check("reload dnsmasq was called", any(c[:2] == ["systemctl", "reload"] for c in rec4.calls))
# .10 and .100 must not be confused by the IP match
cb._dnsmasq_reserve(102, "BC:24:11:00:00:01", "198.18.0.100", resv=rf)
body2 = rf.read_text()
check(".10 survives adding .100 (no suffix confusion)",
      "BC:24:11:99:88:77,198.18.0.10" in body2 and "BC:24:11:00:00:01,198.18.0.100" in body2, body2)

# ---- parse_args accepts the enable-internal-net command ---------------------
def _parse(cmdline):
    os.environ["SSH_ORIGINAL_COMMAND"] = cmdline
    return cb.parse_args()

check("parse enable w/ vmid", _parse("ctrlable-build enable-internal-net 2026.11 100")
      == ("enable-internal-net", "2026.11", "100", None))
check("parse enable w/o vmid", _parse("ctrlable-build enable-internal-net 2026.11")
      == ("enable-internal-net", "2026.11", None, None))
check("deploy still parses", _parse("ctrlable-build deploy 2026.11 zigbee2mqtt z2m-1")
      == ("deploy", "2026.11", "zigbee2mqtt", "z2m-1"))


# ---- enable_internal_net: fabric + attach the HA VM, idempotent, no reboot --
BROKER_MANIFEST = {"network": {"internal": {
    "enabled": True, "bridge": "vmbr1", "subnet": "198.18.0.0/24",
    "assignments": {"ctrlable-pro": 10, "zigbee2mqtt": 21},
}}}

# (a) fresh host: runs the setup script with the manifest subnet, then attaches.
rec5 = Recorder()
rec5.outputs["qm config"] = "net1: virtio=BC:24:11:AA:BB:CC,bridge=vmbr1\n"
cb.subprocess.run = rec5
cb._setup_script = lambda: "/usr/local/lib/ctrlable/setup-internal-net.sh"
cb._bridge_exists = lambda b: True
cb._bridge_is_ours = lambda b: True          # fabric check: the bridge is ours
cb._wait_reachable = lambda ip, secs: True
resv2 = {}
_real = cb._dnsmasq_reserve
cb._dnsmasq_reserve = lambda vmid, mac, ip, **k: resv2.update(vmid=vmid, mac=mac, ip=ip)
# fresh: no pre-existing net1 on the bridge
cb._vm_net1_mac_on = lambda vmid, bridge: None
cb.enable_internal_net(BROKER_MANIFEST, {}, 100)
check("enable runs the setup script",
      any(c[:1] == ["bash"] and c[1].endswith("setup-internal-net.sh") for c in rec5.calls),
      str(rec5.argvs()))
check("enable reserves the broker at .10", resv2.get("ip") == "198.18.0.10", str(resv2))

# (b) idempotent: VM already has net1 on vmbr1 -> no `qm set`, reservation only.
rec6 = Recorder(); cb.subprocess.run = rec6
cb._vm_net1_mac_on = lambda vmid, bridge: "BC:24:11:99:88:77"
resv3 = {}
cb._dnsmasq_reserve = lambda vmid, mac, ip, **k: resv3.update(mac=mac, ip=ip)
cb.enable_internal_net(BROKER_MANIFEST, {}, 100)
check("idempotent: no new net1 when one already exists",
      not any(c[:2] == ["qm", "set"] for c in rec6.calls), str(rec6.argvs()))
check("idempotent: reservation refreshed for the existing MAC",
      resv3 == {"mac": "BC:24:11:99:88:77", "ip": "198.18.0.10"}, str(resv3))
check("enable NEVER reboots the HA VM",
      not any("reboot" in " ".join(c) or (c[:2] == ["qm", "reboot"]) for c in rec6.calls),
      str(rec6.argvs()))
cb._dnsmasq_reserve = _real

# (c) env really carries the subnet (checked by capturing the real call kwargs).
captured = {}
def _cap(argv, *a, **kw):
    captured.setdefault("env", kw.get("env"))
    class R: returncode = 0; stdout = ""
    return R
cb.subprocess.run = _cap
cb._vm_net1_mac_on = lambda vmid, bridge: "AA:BB:CC:DD:EE:FF"
cb._dnsmasq_reserve = lambda *a, **k: None
cb.enable_internal_net(BROKER_MANIFEST, {}, 100)
check("INTERNAL_SUBNET env set for the setup script",
      (captured.get("env") or {}).get("INTERNAL_SUBNET") == "198.18.0.0/24",
      str((captured.get("env") or {}).get("INTERNAL_SUBNET")))


# ---- bridge relocation: the host conf overrides the manifest bridge ---------
# setup-internal-net.sh relocates the internal LAN off a vmbr1 that belongs to
# another network (e.g. a Wi-Fi uplink) and records the chosen bridge here; the
# deploy path must then attach guests to THAT bridge, not the manifest default.
import tempfile as _tf
_confdir = pathlib.Path(_tf.mkdtemp())
_conf = _confdir / "internal-net.conf"
_conf.write_text("subnet=198.18.0.0/24\nbridge=vmbr2\ngateway=198.18.0.1\n")
cfg_ov = cb._internal_net_cfg(MANIFEST, conf_path=_conf)
check("conf overrides manifest bridge (vmbr1 -> vmbr2)", cfg_ov["bridge"] == "vmbr2", str(cfg_ov))
check("default (no conf) keeps manifest bridge",
      cb._internal_net_cfg(MANIFEST, conf_path=_confdir / "nope")["bridge"] == "vmbr1")

# a deploy with the relocated cfg attaches the LXC net1 to vmbr2
_real_cfg = cb._internal_net_cfg
cb._internal_net_cfg = lambda m: {"bridge": "vmbr2", "subnet": "198.18.0.0/24",
                                  "assignments": {"zigbee2mqtt": 21}}
cb._bridge_exists = lambda b: True
recR = Recorder(); cb.subprocess.run = recR
cb._add_internal_lxc_nic(105, "zigbee2mqtt", MANIFEST)
check("relocated: LXC net1 lands on vmbr2",
      "pct set 105 -net1 name=eth1,bridge=vmbr2,ip=198.18.0.21/24" in recR.argvs(), str(recR.argvs()))
cb._internal_net_cfg = _real_cfg


# ---- _bridge_is_ours: foreign bridges (Wi-Fi uplink) are rejected -----------
cb._bridge_is_ours = _ORIG_is_ours          # undo the stub the enable tests set
_real_exists = cb._bridge_exists
cb._bridge_exists = lambda b: b != "absent"

def _ours_with(phys: bool, addr_out: str) -> bool:
    cb._bridge_has_phys_member = lambda b: phys
    r = Recorder(); r.outputs["ip -o"] = addr_out; cb.subprocess.run = r
    return cb._bridge_is_ours("vmbr1")

check("absent bridge is not 'ours' (nothing to attach to)",
      cb._bridge_is_ours("absent") is False)
check("portless + internal addr -> ours",
      _ours_with(False, "13: vmbr1    inet 198.18.0.1/24 scope global vmbr1") is True)
check("portless + NO addr -> ours (freshly created)", _ours_with(False, "") is True)
check("has a physical port -> NOT ours (e.g. Wi-Fi bridge)",
      _ours_with(True, "13: vmbr1    inet 198.18.0.1/24 scope global vmbr1") is False)
check("foreign IPv4 -> NOT ours",
      _ours_with(False, "13: vmbr1    inet 192.168.1.1/24 scope global vmbr1") is False)
cb._bridge_has_phys_member, cb._bridge_exists = _ORIG_phys, _real_exists


# ---- enable REFUSES to attach guests to a foreign bridge --------------------
recF = Recorder()
recF.outputs["qm config"] = "net1: virtio=BC:24:11:AA:BB:CC,bridge=vmbr1\n"
cb.subprocess.run = recF
cb._setup_script = lambda: "/usr/local/lib/ctrlable/setup-internal-net.sh"
cb._bridge_exists = lambda b: True          # a bridge is there ...
cb._bridge_is_ours = lambda b: False        # ... but it is NOT ours (foreign)
cb._wait_reachable = lambda ip, secs: True
try:
    cb.enable_internal_net(BROKER_MANIFEST, {}, 100)
    check("enable aborts on a foreign bridge", False, "no SystemExit")
except SystemExit:
    check("enable aborts on a foreign bridge", True)
check("enable attached NO guest to the foreign bridge",
      not any(c[:2] == ["qm", "set"] for c in recF.calls), str(recF.argvs()))


# ---- migrate-internal: move a running service onto the internal LAN ---------
check("parse migrate-internal", _parse("ctrlable-build migrate-internal 2026.11 zigbee2mqtt 105")
      == ("migrate-internal", "2026.11", "zigbee2mqtt", "105"))

import io, contextlib
MIG_MANIFEST = {"network": {"internal": {
        "enabled": True, "bridge": "vmbr1", "subnet": "198.18.0.0/24",
        "assignments": {"ctrlable-pro": 10, "zwavejs": 20, "zigbee2mqtt": 21, "frigate": 30}}},
    "templates": {"zigbee2mqtt": {"kind": "lxc"}, "zwavejs": {"kind": "lxc"}}}

class MigRun:
    """subprocess.run stub that answers each pct call migrate-internal makes."""
    def __init__(self, expected_ip, net1_present=False):
        self.calls = []; self.expected_ip = expected_ip; self.net1_present = net1_present
    def __call__(self, argv, *a, **kw):
        self.calls.append(list(argv))
        class R: returncode = 0; stdout = ""; stderr = ""
        two = argv[:2]
        if two == ["pct", "config"]:
            R.stdout = ("net1: name=eth1,bridge=vmbr1,ip=%s/24,type=veth\n" % self.expected_ip
                        if self.net1_present else "net0: name=eth0,bridge=vmbr0\n")
        elif two == ["pct", "exec"]:
            script = argv[6] if len(argv) > 6 else ""
            if "addr show eth1" in script:   R.stdout = "eth1@if18 UP %s/24" % self.expected_ip
            elif "/dev/tcp/" in script:      R.stdout = "OPEN"
            elif "configuration.yaml" in script or "systemctl restart" in script: R.stdout = "REWROTE"
            elif "journalctl" in script:     R.stdout = "info: z2m: Connected to MQTT server"
        return R
    def argvs(self): return [" ".join(str(x) for x in c) for c in self.calls]

def run_migrate(template, vmid, net1_present=False):
    octet = MIG_MANIFEST["network"]["internal"]["assignments"][template]
    rec = MigRun("198.18.0.%d" % octet, net1_present=net1_present)
    cb.subprocess.run = rec
    cb._bridge_is_ours = lambda b: True
    cb.time.sleep = lambda *a, **k: None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cb.migrate_internal(MIG_MANIFEST, {}, template, vmid)
    return rec, buf.getvalue()

# (a) broker client (z2m): attach net1, rewrite MQTT config, confirm reconnect
rec, out = run_migrate("zigbee2mqtt", 105)
check("migrate z2m: attaches net1 at .21",
      "pct set 105 -net1 name=eth1,bridge=vmbr1,ip=198.18.0.21/24" in rec.argvs(), str(rec.argvs()))
check("migrate z2m: rewrites its config (configuration.yaml touched)",
      any("configuration.yaml" in (c[6] if len(c) > 6 else "") for c in rec.calls if c[:2] == ["pct","exec"]))
check("migrate z2m: reports reconnect to the broker", "reconnected to the broker" in out, out[-200:])

# (b) HA-served (zwavejs): NIC only, NO config rewrite, reports the HA-side step
rec2, out2 = run_migrate("zwavejs", 104)
check("migrate zwavejs: attaches net1 at .20",
      "pct set 104 -net1 name=eth1,bridge=vmbr1,ip=198.18.0.20/24" in rec2.argvs(), str(rec2.argvs()))
check("migrate zwavejs: does NOT rewrite a service config",
      not any("configuration.yaml" in (c[6] if len(c) > 6 else "") for c in rec2.calls if c[:2] == ["pct","exec"]))
check("migrate zwavejs: reports the HA-side URL change is pending",
      "HA-side URL change pending" in out2, out2[-200:])

# (c) idempotent: net1 already present -> no `pct set`, still re-homes
rec3, out3 = run_migrate("zigbee2mqtt", 105, net1_present=True)
check("migrate idempotent: no pct set when net1 already on the bridge",
      not any(c[:2] == ["pct", "set"] for c in rec3.calls), str(rec3.argvs()))

# (d) the broker VM itself is not a migratable service
cb.subprocess.run = MigRun("198.18.0.10"); cb._bridge_is_ours = lambda b: True; cb.time.sleep = lambda *a, **k: None
try:
    cb.migrate_internal(MIG_MANIFEST, {}, "ctrlable-pro", 100)
    check("migrate refuses the broker template", False, "no SystemExit")
except SystemExit:
    check("migrate refuses the broker template", True)


print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures)); sys.exit(1)
print("all internal-net NIC checks passed")
