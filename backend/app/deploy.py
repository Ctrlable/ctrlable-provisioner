"""
Deploy pipeline — M5.

deploy_instance(): clone template → fresh MAC → pending DB record → start guest
deploy_stack():    create project → deploy all LXC templates for a release
"""
import asyncio
import re

from .manifest import load_all_manifests
from .proxmox import ProxmoxClient
from .state import StateDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def _next_hostname(site: str, template: str, existing: set[str]) -> str:
    base = f"ctrlable-{_slug(site)}-{template}"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _wire_to_for(template_name: str, shared: dict) -> dict:
    """Map the shared deploy config to the per-template wire_to blob."""
    base = {
        k: shared[k]
        for k in ("mqtt_host", "mqtt_port", "mqtt_user", "mqtt_pass")
        if shared.get(k) is not None
    }
    if template_name == "zigbee2mqtt" and shared.get("zigbee_coordinator"):
        base["coordinator_url"] = shared["zigbee_coordinator"]
    if template_name == "zwavejs" and shared.get("zwave_coordinator"):
        base["coordinator_url"] = shared["zwave_coordinator"]
    return base


# ---------------------------------------------------------------------------
# Single-instance deploy (synchronous — called from thread executor)
# ---------------------------------------------------------------------------

def deploy_instance(
    project_id: int,
    template_name: str,
    release: str,
    wire_to: dict,
    db: StateDB,
    px: ProxmoxClient,
) -> dict:
    """Clone a built template, assign a fresh MAC, record as pending, start."""
    tmpl = db.get_template(release, template_name)
    if not tmpl:
        raise ValueError(
            f"template '{template_name}' not built for release '{release}' — "
            "run Build Release first"
        )

    kind = tmpl["kind"]  # "lxc" or "qemu"
    project = db.get_project(project_id)
    existing = {r["hostname"] for r in db.list_all_instances()}
    hostname = _next_hostname(project["site_name"], template_name, existing)

    newid = px.next_vmid()
    if kind == "qemu":
        px.clone_vm(int(tmpl["template_vmid"]), newid, hostname)
    else:
        px.clone_lxc(int(tmpl["template_vmid"]), newid, hostname)
        px.set_lxc_fresh_mac(newid)

    db.create_instance(project_id, template_name, newid, hostname, wire_to)
    px.start_guest(kind, newid)

    return dict(db.get_instance_by_hostname(hostname))


# ---------------------------------------------------------------------------
# Full-stack deploy (runs as background asyncio task)
# ---------------------------------------------------------------------------

async def deploy_stack_async(
    project_id: int,
    release: str,
    shared_wire_to: dict,
    db: StateDB,
    px: ProxmoxClient,
) -> None:
    """Deploy all LXC templates for a release into an existing project."""
    manifests = load_all_manifests()
    manifest = manifests[release]

    for name, tmpl in manifest.templates.items():
        wire_to = _wire_to_for(name, shared_wire_to)
        try:
            await asyncio.to_thread(
                deploy_instance, project_id, name, release, wire_to, db, px
            )
        except Exception as exc:
            print(f"[deploy] {name} failed: {exc}")
