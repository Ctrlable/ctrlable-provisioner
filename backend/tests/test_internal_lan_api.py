"""The internal-LAN orchestrator endpoints (SPEC § 20).

Run:  ./.venv/bin/python backend/tests/test_internal_lan_api.py

Covers the glue that the pure resolver tests (test_internal_net.py) don't:
  - GET  /api/lan/internal  reports the PLAN for the broker's release
  - POST /api/lan/internal/enable  refuses when no broker VM is known (409),
    and otherwise SSHes exactly `ctrlable-build enable-internal-net <rel> <vmid>`
    and reports broker_reachable / reboot_suggested from the run's output.

Calls the endpoint functions directly (no TestClient/httpx dependency). The
host work itself (bridge, dnsmasq, net1) is tested in host/tests.
"""
import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("CTRLABLE_DB", "/tmp/ctrlable_test_lan_api.db")
if os.path.exists("/tmp/ctrlable_test_lan_api.db"):
    os.remove("/tmp/ctrlable_test_lan_api.db")

from fastapi import HTTPException  # noqa: E402
import app.main as M  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{('  -- ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


class _FakeProc:
    def __init__(self, out: bytes, rc: int):
        self._out, self.returncode = out, rc

    async def communicate(self):
        return self._out, b""


def _stub_ssh(output: bytes, rc: int) -> dict:
    captured: dict = {}

    async def fake_exec(*argv, **kw):
        captured["argv"] = list(argv)
        return _FakeProc(output, rc)

    M.asyncio.create_subprocess_exec = fake_exec  # type: ignore
    return captured


# ---- status: no broker -> not configured -----------------------------------
M._broker_instance = lambda: None
j = M.internal_lan_status("admin")
check("no broker -> configured False and broker_known False",
      j == {"configured": False, "broker_known": False}, str(j))


# ---- status: broker on 2026.11 -> the real plan -----------------------------
M._broker_instance = lambda: {"type": "ctrlable-pro", "vmid": 100, "release": "2026.11"}
j = M.internal_lan_status("admin")
check("configured True", j.get("configured") is True, str(j))
check("plan: bridge vmbr1", j.get("bridge") == "vmbr1", str(j.get("bridge")))
check("plan: broker_ip .10", j.get("broker_ip") == "198.18.0.10", str(j.get("broker_ip")))
check("plan: broker_vmid 100", j.get("broker_vmid") == 100)
check("plan: zigbee2mqtt -> .21",
      (j.get("assignments") or {}).get("zigbee2mqtt") == "198.18.0.21", str(j.get("assignments")))


# ---- enable: no broker -> 409 (cannot resolve release/vmid) -----------------
M._broker_instance = lambda: None
M.settings.pve_host = "pve.example"
M.settings.build_key_path = "/etc/ctrlable/build_key"
M.settings.build_token = "t"
try:
    asyncio.run(M.internal_lan_enable("admin"))
    check("enable without broker -> 409", False, "no raise")
except HTTPException as e:
    check("enable without broker -> 409", e.status_code == 409, str(e.status_code))


# ---- enable: success path -> right command, broker reachable ----------------
M._broker_instance = lambda: {"type": "ctrlable-pro", "vmid": 100, "release": "2026.11"}
cap = _stub_ssh(b"[enable-internal-net] fabric up\n[enable-internal-net] OK: broker reachable at 198.18.0.10 on vmbr1\n", 0)
j = asyncio.run(M.internal_lan_enable("admin"))
check("ran enable-internal-net with release+vmid",
      cap["argv"][-1] == "ctrlable-build enable-internal-net 2026.11 100", str(cap.get("argv", [])[-1:]))
check("enable ok True", j.get("ok") is True, str(j))
check("broker_reachable True", j.get("broker_reachable") is True)
check("reboot NOT suggested when reachable", j.get("reboot_suggested") is False)


# ---- enable: NIC attached but not up yet -> reboot suggested ----------------
_stub_ssh(b"[enable-internal-net] net1 attached + reserved for 198.18.0.10, reboot to pick it up\n", 0)
j = asyncio.run(M.internal_lan_enable("admin"))
check("ok True (attach succeeded)", j.get("ok") is True)
check("broker_reachable False", j.get("broker_reachable") is False)
check("reboot suggested when not reachable", j.get("reboot_suggested") is True)


print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all internal-lan API checks passed")
