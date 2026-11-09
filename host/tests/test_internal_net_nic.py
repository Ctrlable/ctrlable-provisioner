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

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures)); sys.exit(1)
print("all internal-net NIC checks passed")
