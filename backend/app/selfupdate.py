"""Provisioner self-update (SPEC § 16.6).

Triggered (not yet auto-polled): fast-forward the orchestrator's OWN git
checkout to a ref, rebuild, verify, refresh the host tools, record the version,
then restart — rolling the checkout back if anything fails before the restart.

Runs inside the orchestrator LXC, so git / pip / npm / systemctl act on itself.
The restart is detached (a child that outlives this process) because restarting
the service would otherwise kill the request mid-response; the client re-polls
`/api/version` to see the new build.

The host-tools refresh is a best effort: it SSHes the build key to run
`ctrlable-build update-host-tools`, which an OLD host tool (from before that
action existed) will reject — that is expected on a site's FIRST self-update and
is reported, not fatal. Subsequent updates keep the host in step automatically.
"""
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings

REPO_DIR = Path(os.environ.get("CTRLABLE_REPO_DIR", "/opt/ctrlable-provisioner"))
SERVICE = os.environ.get("CTRLABLE_SERVICE", "ctrlable-provisioner")
INSTALL_RECORD = Path(os.environ.get("CTRLABLE_INSTALL_RECORD", str(REPO_DIR / "install.json")))


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 600) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except Exception as exc:                       # noqa: BLE001
        return 1, str(exc)


def current_revision() -> str:
    rc, out = _run(["git", "-C", str(REPO_DIR), "describe", "--tags", "--always", "--dirty"])
    return out.strip() if rc == 0 else "unknown"


def _head() -> str | None:
    rc, out = _run(["git", "-C", str(REPO_DIR), "rev-parse", "HEAD"])
    return out.strip() if rc == 0 else None


def installed_version() -> str:
    """The version string from the install record, for the heartbeat."""
    try:
        return json.loads(INSTALL_RECORD.read_text()).get("version", "unknown")
    except Exception:                              # noqa: BLE001
        return "unknown"


def _write_install_record(ref: str) -> tuple[str, str]:
    rc, ver = _run(["cat", str(REPO_DIR / "VERSION")])
    version = ver.strip() if rc == 0 and ver.strip() else "untagged"
    rev = current_revision()
    INSTALL_RECORD.write_text(json.dumps({
        "version": version,
        "revision": rev,
        "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ref": ref,
        "source": "self-update",
    }, indent=1))
    return version, rev


def _schedule_restart(delay: int = 3) -> None:
    # Detached so it survives this process; the delay lets the response flush.
    subprocess.Popen(["bash", "-c", f"sleep {delay}; systemctl restart {SERVICE}"],
                     start_new_session=True)


def run_self_update(target_ref: str, settings: "Settings",
                    update_host_tools: bool = True) -> dict:
    """Perform the staged update. Returns a report; on success the service is
    restarting (the caller should tell the client to re-poll the version)."""
    steps: list[dict] = []

    def step(name: str, cmd: list[str], cwd: str | None = None, timeout: int = 600) -> int:
        rc, out = _run(cmd, cwd=cwd, timeout=timeout)
        steps.append({"step": name, "rc": rc, "output": out[-2000:]})
        return rc

    old = _head()

    if step("fetch", ["git", "-C", str(REPO_DIR), "fetch", "--quiet", "origin", target_ref]) != 0:
        return {"ok": False, "stage": "fetch", "from": old, "steps": steps}
    if step("checkout", ["git", "-C", str(REPO_DIR), "checkout", "--quiet",
                         "--force", "-B", target_ref, "FETCH_HEAD"]) != 0:
        return {"ok": False, "stage": "checkout", "from": old, "steps": steps}
    new = _head()

    def rollback(stage: str) -> dict:
        if old:
            _run(["git", "-C", str(REPO_DIR), "checkout", "--quiet", "--force", old])
        return {"ok": False, "stage": stage, "rolled_back": bool(old),
                "from": old, "to": new, "steps": steps}

    if step("pip", [str(REPO_DIR / ".venv/bin/pip"), "install", "--quiet",
                    "-r", str(REPO_DIR / "backend/requirements.txt")]) != 0:
        return rollback("pip")
    if step("npm-build", ["npm", "run", "build"], cwd=str(REPO_DIR / "frontend")) != 0:
        return rollback("npm-build")
    # Verify the NEW code at least imports before we restart onto it.
    if step("verify-import", [str(REPO_DIR / ".venv/bin/python"), "-c", "import app.main"],
            cwd=str(REPO_DIR / "backend")) != 0:
        return rollback("verify-import")

    version, rev = _write_install_record(target_ref)

    host: dict = {"attempted": False}
    if update_host_tools and settings.build_configured():
        host["attempted"] = True
        rc, out = _run([
            "ssh", "-i", settings.build_key_path,
            "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            f"root@{settings.pve_host}", "ctrlable-build update-host-tools",
        ], timeout=180)
        host["ok"] = rc == 0
        host["output"] = out[-1000:]

    _schedule_restart()
    return {"ok": True, "from": old, "to": new, "unchanged": old == new,
            "version": version, "revision": rev, "host_tools": host,
            "restarting": True, "steps": steps}
