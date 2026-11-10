"""ctrlable-build update-host-tools — the host half of a provisioner self-update.

Run:  python3 host/tests/test_update_host_tools.py

It pulls the refreshed host tools out of the orchestrator CT and moves them into
place ATOMICALLY (temp + os.replace), never writing over a live file. No PVE
host: `pct` is faked (and makes the pulled temp files real so the atomic move
and the empty-file guard are actually exercised).
"""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("ctrlable_build", os.path.join(HERE, "..", "ctrlable-build"))
cb = importlib.util.module_from_spec(importlib.util.spec_from_loader("ctrlable_build", _loader))
_loader.exec_module(cb)

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{('  -- ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


def _parse(cmdline):
    os.environ["SSH_ORIGINAL_COMMAND"] = cmdline
    return cb.parse_args()


check("parse update-host-tools",
      _parse("ctrlable-build update-host-tools") == ("update-host-tools", None, None, None))


# ---- orchestrator ctid: from conf, else discovered -------------------------
check("ctid from build.conf", cb._orchestrator_ctid({"orchestrator_ctid": 900}) == 900)


# ---- the atomic pull: temp + replace, empty-file guard, mode ----------------
class PctFake:
    """Fakes subprocess.run for `pct pull/exec/list`. `pull` writes the dest arg."""
    def __init__(self, content=b"#!/bin/sh\nnew\n", builders="dali-bridge.sh\nhardware-manager.sh\n"):
        self.calls = []
        self.content = content
        self.builders = builders

    def __call__(self, argv, *a, **kw):
        self.calls.append(list(argv))
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        if argv[:2] == ["pct", "pull"]:
            # pct pull <ctid> <src> <dst>
            with open(argv[4], "wb") as fh:
                fh.write(self.content)
        elif argv[:2] == ["pct", "exec"] and "ls" in argv:
            R.stdout = self.builders
        return R


tmp = tempfile.mkdtemp()
dst = os.path.join(tmp, "bin", "ctrlable-build")
fake = PctFake()
cb.subprocess.run = fake
cb._pct_pull_atomic(900, "/opt/ctrlable-provisioner/host/ctrlable-build", dst, 0o755)
check("atomic pull created the dest", os.path.exists(dst))
check("dest has the pulled content", open(dst).read().strip().endswith("new"))
check("dest mode is 0755", (os.stat(dst).st_mode & 0o777) == 0o755, oct(os.stat(dst).st_mode & 0o777))
check("no leftover .new temp files", not any(f.endswith(tuple(f".new-{os.getpid()}" for _ in [0]))
                                             for f in os.listdir(os.path.dirname(dst))))

# empty pull (the known pct-pull-exits-0-on-missing trap) must fail, not install junk
empty = os.path.join(tmp, "empty-target")
fake_empty = PctFake(content=b"")
cb.subprocess.run = fake_empty
try:
    cb._pct_pull_atomic(900, "/missing", empty, 0o755)
    check("empty pull rejected", False, "no exit")
except SystemExit:
    check("empty pull rejected", True)
check("empty pull left no dest", not os.path.exists(empty))


# ---- update_host_tools pulls the whole set incl. builders -------------------
out = tempfile.mkdtemp()
cb._HOST_TOOL_FILES = [
    ("/opt/ctrlable-provisioner/host/ctrlable-build", os.path.join(out, "bin", "ctrlable-build"), 0o755),
    ("/opt/ctrlable-provisioner/host/setup-internal-net.sh", os.path.join(out, "lib", "setup-internal-net.sh"), 0o755),
]
cb._HOST_BUILDERS_DST = os.path.join(out, "builders")
fake2 = PctFake()
cb.subprocess.run = fake2
cb.update_host_tools({"orchestrator_ctid": 900})
# argv[4] is the temp path (<dst>.new-<pid>); compare the final destinations.
pulled = sorted(c[4].rsplit(".new-", 1)[0] for c in fake2.calls if c[:2] == ["pct", "pull"])
check("pulled ctrlable-build + setup script + both builders",
      pulled == sorted([
          os.path.join(out, "bin", "ctrlable-build"),
          os.path.join(out, "lib", "setup-internal-net.sh"),
          os.path.join(out, "builders", "dali-bridge.sh"),
          os.path.join(out, "builders", "hardware-manager.sh"),
      ]), str(pulled))
check("every tool landed on disk (final paths, not temps)",
      all(os.path.exists(p) for p in [
          os.path.join(out, "bin", "ctrlable-build"),
          os.path.join(out, "lib", "setup-internal-net.sh"),
          os.path.join(out, "builders", "dali-bridge.sh"),
          os.path.join(out, "builders", "hardware-manager.sh"),
      ]))


print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all update-host-tools checks passed")
