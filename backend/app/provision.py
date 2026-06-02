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

    if inst["status"] == "active":
        raise ValueError(f"instance {hostname!r} is already active")
    if inst["status"] == "error":
        raise ValueError(f"instance {hostname!r} is in error state")

    db.set_instance_status(hostname, "provisioning")

    wire_to = json.loads(inst["wire_to"]) if inst["wire_to"] else {}
    return {
        "hostname": hostname,
        "type": inst["type"],
        "release": project["release"],
        "wire_to": wire_to,
    }


def complete_provisioning(db: StateDB, hostname: str, secret: str) -> None:
    inst = db.get_instance_by_hostname(hostname)
    if not inst:
        raise LookupError(f"unknown hostname: {hostname!r}")

    project = db.get_project(inst["project_id"])
    release = db.get_release(project["release"])

    if not release or release["firstboot_secret"] != secret:
        raise PermissionError("invalid firstboot secret")

    db.set_instance_status(hostname, "active")
