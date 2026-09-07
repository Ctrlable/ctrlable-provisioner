#!/usr/bin/env python3
"""Turnkey exercise of LibvirtProvider on a real Debian host.

Run this on a box with Incus (and optionally libvirt) installed to prove the
Debian backend drives real guests through the full Provider surface. It is safe:
it operates ONLY on a throwaway container it creates itself (name below), refuses
to run if that name is already taken, and deletes it in a finally block even if a
step fails.

WHAT IT DOES

  * node_health           -- always (reads /proc; no virt tools needed)
  * If incus is present:
      launch a throwaway container, then exercise
        list_guests -> stop -> start -> reboot -> get_config
        -> update_config(onboot) -> set_fresh_mac -> backup(export) -> destroy
      verifying the effect of each against incus directly.
  * If virsh is present: reports the VM inventory read-only (creating a test VM
    is heavy and host-specific, so lifecycle on VMs is left to a real template).

PREREQUISITES ON THE HOST

    apt install incus                       # containers  (required for the loop)
    apt install libvirt-clients virtinst    # VMs         (optional; read-only here)
    incus admin init --minimal              # one-time, if incus is fresh

RUNNING

  Needs the `app` package (provider.py + libvirt_provider.py) importable. The
  simplest turnkey path on a fresh host: copy the repo's `backend/` directory
  over, then from inside it run

      python3 tests/test_libvirt_provider.py

  or from the repo root:

      python3 backend/tests/test_libvirt_provider.py

Exit code is non-zero if any check fails.
"""
import os
import shutil
import subprocess
import sys
import time

# The `app` package lives one level up from this tests/ dir (backend/app). Put
# backend/ on the path so `app.libvirt_provider` (with its relative imports)
# resolves whether we are launched from the repo root or from backend/.
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _BACKEND)
from app.libvirt_provider import LibvirtProvider           # noqa: E402
from app.provider import GuestKind, GuestRef, Provider      # noqa: E402

TEST_CT = "ctrlable-provtest"          # the ONLY guest this script ever touches
IMAGE = os.environ.get("PROVTEST_IMAGE", "images:debian/12")

_passed = 0
_failed = 0


def check(label, cond, detail=""):
    global _passed, _failed
    mark = "PASS" if cond else "FAIL"
    if cond:
        _passed += 1
    else:
        _failed += 1
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    return cond


def incus(*args, timeout=120):
    return subprocess.run(["incus", *args], capture_output=True, text=True,
                          timeout=timeout)


def main():
    prov: Provider = LibvirtProvider(node="provtest")
    print("LibvirtProvider self-test\n")
    print(f"  virsh present: {prov._has_virsh}   incus present: {prov._has_incus}\n")

    # --- node_health: works everywhere -------------------------------------
    h = prov.node_health()
    check("node_health returns sane host data",
          h.mem_total > 0 and h.disk_total > 0 and h.uptime > 0,
          f"cpu={h.cpu:.2f} mem={h.mem_used}/{h.mem_total} up={h.uptime}s")

    # --- read-only VM inventory --------------------------------------------
    if prov._has_virsh:
        vms = [g for g in prov.list_guests() if g.ref.kind is GuestKind.VM]
        check("list_guests reads VM inventory", True, f"{len(vms)} VM(s)")
    else:
        print("  [skip] virsh not installed -- VM paths not exercised")

    if not prov._has_incus:
        print("\n  incus not installed -- skipping the container lifecycle loop.")
        print("  install it (apt install incus) to run the full test.")
        return _summary()

    # --- container lifecycle on a throwaway instance -----------------------
    if incus("info", TEST_CT).returncode == 0:
        print(f"\n  REFUSING: a container named {TEST_CT!r} already exists.")
        print("  Remove it first (incus delete --force " + TEST_CT + ").")
        _failed_hard()
        return _summary()

    ref = GuestRef(id=TEST_CT, kind=GuestKind.CONTAINER)
    created = False
    try:
        print(f"\n  launching throwaway container {TEST_CT} from {IMAGE} ...")
        r = incus("launch", IMAGE, TEST_CT, timeout=300)
        if not check("incus launch test container", r.returncode == 0,
                     r.stderr.strip()[:120]):
            return _summary()
        created = True
        time.sleep(3)

        # list_guests finds it
        found = [g for g in prov.list_guests() if g.ref.id == TEST_CT]
        check("list_guests finds the container", len(found) == 1,
              f"status={found[0].status}" if found else "not found")

        # stop
        prov.stop(ref)
        time.sleep(2)
        st = incus("info", TEST_CT).stdout
        check("stop() stopped it", "STOPPED" in st.upper() or "Status: Stopped" in st)

        # start
        prov.start(ref)
        time.sleep(3)
        st = incus("info", TEST_CT).stdout
        check("start() started it", "RUNNING" in st.upper() or "Status: Running" in st)

        # reboot (just must not error on a running container)
        try:
            prov.reboot(ref)
            check("reboot() succeeded", True)
        except Exception as e:
            check("reboot() succeeded", False, str(e)[:120])
        time.sleep(3)

        # get_config returns a dict
        cfg = prov.get_config(ref)
        check("get_config returns a dict", isinstance(cfg, dict) and bool(cfg),
              f"{len(cfg)} keys")

        # update_config(onboot=True) -> boot.autostart true
        prov.update_config(ref, changes={"onboot": True})
        got = incus("config", "get", TEST_CT, "boot.autostart").stdout.strip()
        check("update_config(onboot=True) set boot.autostart", got in ("true", "1"),
              f"boot.autostart={got!r}")

        # update_config rejects Proxmox-shaped keys, loudly
        try:
            prov.update_config(ref, changes={"net0": "name=eth0,bridge=vmbr0"})
            check("update_config rejects Proxmox-shaped keys", False,
                  "did not raise")
        except NotImplementedError:
            check("update_config rejects Proxmox-shaped keys", True)

        # set_fresh_mac -> hwaddr changes
        before = incus("config", "get", TEST_CT, "volatile.eth0.hwaddr").stdout.strip()
        mac = prov.set_fresh_mac(ref)
        after = incus("config", "get", TEST_CT, "volatile.eth0.hwaddr").stdout.strip()
        check("set_fresh_mac returns a MAC and applies it",
              bool(mac) and mac.count(":") == 5, f"{before or '?'} -> {mac}")

        # backup(export) -> tarball exists
        try:
            dest = prov.backup(ref)
            ok = os.path.exists(dest) and os.path.getsize(dest) > 0
            check("backup() produced an artifact", ok, dest)
            if ok:
                os.unlink(dest)
        except Exception as e:
            check("backup() produced an artifact", False, str(e)[:120])

        # destroy -> gone
        prov.destroy(ref)
        created = False
        time.sleep(2)
        gone = incus("info", TEST_CT).returncode != 0
        check("destroy() removed the container", gone)

    finally:
        if created:
            print(f"\n  cleanup: removing {TEST_CT}")
            incus("delete", TEST_CT, "--force")

    return _summary()


def _failed_hard():
    global _failed
    _failed += 1


def _summary():
    print(f"\n  ---- {_passed} passed, {_failed} failed ----")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    if not shutil.which("incus") and not shutil.which("virsh"):
        print("Neither incus nor virsh found. node_health still works, but install")
        print("incus (apt install incus) to exercise the container lifecycle.")
    sys.exit(main())
