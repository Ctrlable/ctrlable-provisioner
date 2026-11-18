"""Pure-logic exercise of IncusProvider — parsing, kind mapping, and the exact
`incus` argv each method issues — with a FAKE incus runner, so it runs anywhere
(CI, no Incus host). A live end-to-end exercise against a real daemon is the job
of the turnkey test_libvirt_provider.py pattern; this one pins the translation.

Run:  python3 backend/tests/test_incus_provider.py

Fail-first is inherent: before app/incus_provider.py existed this file could not
import it (ImportError), so every check was red until the module landed.
"""
import os
import re
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _BACKEND)

from app.incus_provider import IncusProvider, _parse_bytes   # noqa: E402
from app.provider import GuestKind, GuestRef                 # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{('  -- ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


class FakeIncus:
    """Stand-in for IncusProvider._incus: records argv, returns canned output."""
    def __init__(self, list_json="[]", show_yaml=""):
        self.calls: list[tuple] = []
        self.list_json = list_json
        self.show_yaml = show_yaml

    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[:3] == ("list", "--format", "json"):
            return self.list_json
        if args[:2] == ("config", "show"):
            return self.show_yaml
        return ""

    def issued(self, *prefix):
        return any(c[:len(prefix)] == prefix for c in self.calls)


def provider(fake):
    p = IncusProvider()
    p._has_incus = True
    p._incus = fake
    return p


# ---- list_guests: JSON parse + container/VM kind mapping + mem/limits -------
LIST = """[
 {"name":"ctrlable-pro-1","type":"container","status":"Running",
  "state":{"memory":{"usage":536870912}},"config":{"limits.memory":"2GiB"}},
 {"name":"win-vm","type":"virtual-machine","status":"Stopped",
  "state":{"memory":{"usage":0}},"config":{"limits.memory":"4GiB"}},
 {"name":"skipme","type":"container","status":"Running","state":{},"config":{}}
]"""
f = FakeIncus(list_json=LIST)
guests = provider(f).list_guests(exclude={"skipme"})
check("list: honours exclude", [g.name for g in guests] == ["ctrlable-pro-1", "win-vm"],
      str([g.name for g in guests]))
by = {g.name: g for g in guests}
check("list: container type -> CONTAINER kind",
      by["ctrlable-pro-1"].ref.kind is GuestKind.CONTAINER, "")
check("list: virtual-machine type -> VM kind",
      by["win-vm"].ref.kind is GuestKind.VM, "")
check("list: status normalised lower-case", by["ctrlable-pro-1"].status == "running", "")
check("list: mem usage parsed (bytes)", by["ctrlable-pro-1"].mem == 536870912, "")
check("list: maxmem from limits.memory (2GiB)", by["ctrlable-pro-1"].maxmem == 2 * 1024**3,
      str(by["ctrlable-pro-1"].maxmem))
check("list: ref.id is the instance NAME", by["win-vm"].ref.id == "win-vm", "")

# ---- lifecycle argv (uniform container/VM) ----------------------------------
ct = GuestRef(id="ctrlable-pro-1", kind=GuestKind.CONTAINER)
vm = GuestRef(id="win-vm", kind=GuestKind.VM)

f = FakeIncus(); provider(f).start(ct)
check("start: issues `incus start <name>`", f.issued("start", "ctrlable-pro-1"), str(f.calls))
f = FakeIncus(); provider(f).stop(vm)
check("stop: issues `incus stop <name> --force`", f.issued("stop", "win-vm", "--force"), str(f.calls))
f = FakeIncus(); provider(f).reboot(ct)
check("reboot: issues `incus restart <name>`", f.issued("restart", "ctrlable-pro-1"), str(f.calls))
f = FakeIncus(); provider(f).destroy(ct)
check("destroy: issues `incus delete <name> --force`", f.issued("delete", "ctrlable-pro-1", "--force"), str(f.calls))

# stop() swallows "not running" but re-raises anything else
class Raiser:
    def __init__(self, msg): self.msg = msg; self.calls = []
    def __call__(self, *a, timeout=60):
        self.calls.append(a); raise RuntimeError(self.msg)
p = IncusProvider(); p._has_incus = True; p._incus = Raiser("Error: The instance is not running")
try:
    p.stop(ct); check("stop: swallows 'not running'", True)
except RuntimeError:
    check("stop: swallows 'not running'", False, "raised")
p._incus = Raiser("Error: permission denied")
try:
    p.stop(ct); check("stop: re-raises real errors", False, "did not raise")
except RuntimeError:
    check("stop: re-raises real errors", True)

# ---- clone: preserves kind, returns a name ref ------------------------------
f = FakeIncus()
ref = provider(f).clone(GuestRef(id="tmpl", kind=GuestKind.VM), "newid", "new-vm")
check("clone: issues `incus copy <tmpl> <name>`", f.issued("copy", "tmpl", "new-vm"), str(f.calls))
check("clone: returns ref with the new name + preserved kind",
      ref == GuestRef(id="new-vm", kind=GuestKind.VM), str(ref))

# ---- set_fresh_mac: locally-administered, no colons, set on eth0 ------------
f = FakeIncus()
mac = provider(f).set_fresh_mac(ct)
check("mac: locally-administered unicast, no colons", bool(re.fullmatch(r"0200[0-9a-f]{8}", mac)), mac)
check("mac: set on eth0 via config device set",
      f.issued("config", "device", "set", "ctrlable-pro-1", "eth0", f"hwaddr={mac}"), str(f.calls))

# ---- update_config: portable keys mapped; Proxmox-shaped keys REJECTED ------
f = FakeIncus()
provider(f).update_config(ct, changes={"onboot": "1", "memory": "4GiB", "cores": "2"})
check("update: onboot -> boot.autostart true",
      f.issued("config", "set", "ctrlable-pro-1", "boot.autostart", "true"), str(f.calls))
check("update: memory -> limits.memory",
      f.issued("config", "set", "ctrlable-pro-1", "limits.memory", "4GiB"), str(f.calls))
check("update: cores -> limits.cpu",
      f.issued("config", "set", "ctrlable-pro-1", "limits.cpu", "2"), str(f.calls))
try:
    provider(FakeIncus()).update_config(ct, changes={"net0": "name=eth0,bridge=vmbr0"})
    check("update: Proxmox-shaped key raises", False, "no raise")
except NotImplementedError:
    check("update: Proxmox-shaped key raises", True)

# ---- resize_disk: absolute only; delta rejected -----------------------------
f = FakeIncus(); provider(f).resize_disk(ct, "root", "20GiB")
check("resize: absolute size -> config device set size=",
      f.issued("config", "device", "set", "ctrlable-pro-1", "root", "size=20GiB"), str(f.calls))
try:
    provider(FakeIncus()).resize_disk(ct, "root", "+5GB")
    check("resize: '+delta' rejected", False, "no raise")
except NotImplementedError:
    check("resize: '+delta' rejected", True)

# ---- get_config: YAML scrape ------------------------------------------------
SHOW = "architecture: x86_64\nconfig:\n  boot.autostart: \"true\"\n  limits.memory: 2GiB\n"
f = FakeIncus(show_yaml=SHOW)
cfg = provider(f).get_config(ct)
check("get_config: scrapes 2-space keys", cfg.get("limits.memory") == "2GiB" and cfg.get("boot.autostart") == '"true"',
      str({k: v for k, v in cfg.items() if k != "raw"}))
check("get_config: keeps raw", "raw" in cfg and "architecture" in cfg["raw"], "")

# ---- remote prefixing -------------------------------------------------------
f = FakeIncus()
pr = IncusProvider(remote="prod:"); pr._has_incus = True; pr._incus = f
pr.start(ct)
check("remote: prefixes the instance name", f.issued("start", "prod:ctrlable-pro-1"), str(f.calls))

# ---- helpers ----------------------------------------------------------------
check("_parse_bytes: 2GiB", _parse_bytes("2GiB") == 2 * 1024**3, "")
check("_parse_bytes: plain int", _parse_bytes("1073741824") == 1073741824, "")
check("_parse_bytes: junk -> 0", _parse_bytes("nonsense") == 0, "")
check("_kind_from_type: virtual-machine -> VM", IncusProvider._kind_from_type("virtual-machine") is GuestKind.VM, "")
check("_kind_from_type: container -> CONTAINER", IncusProvider._kind_from_type("container") is GuestKind.CONTAINER, "")


print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all incus-provider checks passed")
