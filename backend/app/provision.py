"""
Provision callbacks — called by ctrlable-firstboot.sh running inside a cloned guest.

GET  /api/provision/assignment  — returns wire_to config; transitions instance pending→provisioning
POST /api/provision/complete    — marks instance active
"""
import json

from .state import StateDB


def get_assignment(db: StateDB, hostname: str, secret: str) -> dict:
    inst = db.get_instance_by_hostname(hostname)
    if not inst:
        raise LookupError(f"unknown hostname: {hostname!r}")

    project = db.get_project(inst["project_id"])
    release = db.get_release(project["release"])

    if not release or release["firstboot_secret"] != secret:
        raise PermissionError("invalid firstboot secret")

    if inst["status"] == "error":
        raise ValueError(f"instance {hostname!r} is in error state")

    # Re-running firstboot on an active instance re-applies its wiring (it is
    # idempotent); only a not-yet-active one moves to "provisioning".
    if inst["status"] != "active":
        db.set_instance_status(hostname, "provisioning")

    wire_to = json.loads(inst["wire_to"]) if inst["wire_to"] else {}
    return {
        "hostname": hostname,
        "type": inst["type"],
        "release": project["release"],
        "wire_to": wire_to,
    }


def complete_provisioning(db: StateDB, hostname: str, secret: str,
                          reported_ip: str | None = None) -> None:
    inst = db.get_instance_by_hostname(hostname)
    if not inst:
        raise LookupError(f"unknown hostname: {hostname!r}")

    project = db.get_project(inst["project_id"])
    release = db.get_release(project["release"])

    if not release or release["firstboot_secret"] != secret:
        raise PermissionError("invalid firstboot secret")

    # Optional, and deliberately not fatal: an older firstboot script sends
    # no address, and refusing its completion over a field § 16 wants would
    # break provisioning to improve monitoring. Validated rather than
    # trusted — a guest that resolved its own hostname to loopback would
    # otherwise have us probe the orchestrator and report its services as
    # the guest's.
    if reported_ip:
        from .addressing import from_reported
        addr = from_reported(reported_ip)
        if addr:
            db.set_reported_ip(hostname, addr.ip)

    db.set_instance_status(hostname, "active")
