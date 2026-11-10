"""Provisioner self-update staging + rollback (SPEC § 16.6).

Run:  ./.venv/bin/python backend/tests/test_selfupdate.py

No git/pip/npm/systemd here: selfupdate._run is replaced with a fake that
records the command sequence and can be told to fail a given step, so we assert
the real promises — fetch→checkout→pip→npm→verify in order, a failure at any
stage rolls the checkout back and does NOT restart, and the host-tools refresh
is attempted but never fatal.
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import app.selfupdate as S  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{('  -- ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


class FakeRun:
    """Stand-in for selfupdate._run. `fail` = substrings of commands to fail."""
    def __init__(self, fail=None):
        self.calls: list[str] = []
        self.fail = set(fail or [])
        self.checked_out = False

    def __call__(self, cmd, cwd=None, timeout=600):
        key = " ".join(cmd)
        self.calls.append(key)
        if "checkout" in key and "-B" in key:
            self.checked_out = True
        for f in self.fail:
            if f in key:
                return (1, f"forced-fail::{f}")
        if "rev-parse" in key:
            return (0, "NEWHEAD\n" if self.checked_out else "OLDHEAD\n")
        if key.startswith("git -C") and "describe" in key:
            return (0, "v9.9.9-0-gNEW\n")
        if key.startswith("cat ") and key.endswith("VERSION"):
            return (0, "9.9.9\n")
        if "ctrlable-build update-host-tools" in key:
            return (0, "[update-host-tools] done\n")
        return (0, "")


class FakeSettings:
    build_key_path = "/etc/ctrlable/build_key"
    pve_host = "pve.example"
    _bc = True

    def build_configured(self):
        return self._bc


def _install(fake, restart_flag):
    S._run = fake                      # type: ignore
    S._schedule_restart = lambda delay=3: restart_flag.append(True)  # type: ignore
    tmp = tempfile.mkdtemp()
    S.INSTALL_RECORD = pathlib.Path(tmp) / "install.json"


# ---- happy path -------------------------------------------------------------
restart: list = []
fake = FakeRun()
_install(fake, restart)
r = S.run_self_update("main", FakeSettings())
order = [c for c in fake.calls if any(k in c for k in
         ("fetch", "checkout", "/pip", "npm run build", "import app.main"))]
seq = " > ".join(
    ("fetch" if "fetch" in c else "checkout" if "checkout" in c else
     "pip" if "/pip" in c else "npm" if "npm run build" in c else "verify")
    for c in order)
check("ok on success", r.get("ok") is True, str(r.get("stage")))
check("steps run in order fetch>checkout>pip>npm>verify",
      seq == "fetch > checkout > pip > npm > verify", seq)
check("records from OLDHEAD to NEWHEAD", r.get("from") == "OLDHEAD" and r.get("to") == "NEWHEAD",
      f'{r.get("from")}->{r.get("to")}')
check("version recorded", r.get("version") == "9.9.9", str(r.get("version")))
check("host tools attempted + ok", r.get("host_tools", {}).get("ok") is True, str(r.get("host_tools")))
check("restart scheduled", restart == [True])
check("install.json written", S.INSTALL_RECORD.exists())


# ---- failure at npm rolls back and does NOT restart -------------------------
restart = []
fake = FakeRun(fail=["npm run build"])
_install(fake, restart)
r = S.run_self_update("main", FakeSettings())
check("npm failure -> ok False", r.get("ok") is False)
check("npm failure -> stage npm-build", r.get("stage") == "npm-build", str(r.get("stage")))
check("npm failure -> rolled back", r.get("rolled_back") is True)
check("rollback checks out the old head",
      any("checkout" in c and "OLDHEAD" in c for c in fake.calls), str(fake.calls[-3:]))
check("npm failure -> NO restart", restart == [])
check("npm failure -> verify never ran",
      not any("import app.main" in c for c in fake.calls))


# ---- failure at verify-import also rolls back -------------------------------
restart = []
fake = FakeRun(fail=["import app.main"])
_install(fake, restart)
r = S.run_self_update("main", FakeSettings())
check("verify failure -> stage verify-import", r.get("stage") == "verify-import", str(r.get("stage")))
check("verify failure -> rolled back + no restart", r.get("rolled_back") is True and restart == [])


# ---- fetch failure: nothing to roll back, no restart ------------------------
restart = []
fake = FakeRun(fail=["fetch"])
_install(fake, restart)
r = S.run_self_update("main", FakeSettings())
check("fetch failure -> stage fetch, no restart, no checkout",
      r.get("stage") == "fetch" and restart == [] and not any("-B" in c for c in fake.calls),
      str(r.get("stage")))


# ---- host-tools rejected (old host tool) is reported, NOT fatal -------------
restart = []
fake = FakeRun(fail=["update-host-tools"])
_install(fake, restart)
r = S.run_self_update("main", FakeSettings())
check("host-tools rejected still succeeds + restarts", r.get("ok") is True and restart == [True])
check("host-tools result marks not-ok", r.get("host_tools", {}).get("ok") is False, str(r.get("host_tools")))


# ---- host-tools skipped when the build key isn't configured -----------------
restart = []
fake = FakeRun()
_install(fake, restart)
s = FakeSettings(); s._bc = False
r = S.run_self_update("main", s)
check("no build key -> host tools not attempted",
      r.get("host_tools", {}).get("attempted") is False, str(r.get("host_tools")))
check("no build key -> no ssh call", not any("update-host-tools" in c for c in fake.calls))


print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all self-update checks passed")
