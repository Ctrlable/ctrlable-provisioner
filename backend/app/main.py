import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import auth_enabled, create_token, hash_password, require_auth, verify_password
from .build import trigger_build
from .config import get_settings
from .deploy import deploy_instance, deploy_stack_async
from .manifest import load_all_manifests
from .lan import setup_lan_access
from .platform import enroll as _platform_enroll, ensure_tunnel_up, get_status as _platform_get_status, start_auto_enroll, start_heartbeat
from .provision import complete_provisioning, get_assignment
from .proxmox import ProxmoxClient
from .state import StateDB

settings = get_settings()
db = StateDB(settings.db_file())


@asynccontextmanager
async def lifespan(app: FastAPI):
    for m in load_all_manifests().values():
        db.seed_release(m)
    if settings.must_change_password:
        import warnings
        warnings.warn(
            "ADMIN_PASSWORD_HASH not set — using default admin/admin. "
            "The UI will force a password change on first login.",
            stacklevel=1,
        )
    # Resume portal heartbeat if previously enrolled; otherwise start auto-enroll watcher
    pstate = db.get_platform_state()
    if pstate:
        ensure_tunnel_up(pstate["wg_iface"])
        start_heartbeat(pstate["device_id"], pstate["device_token"], db)
        asyncio.create_task(asyncio.to_thread(setup_lan_access, pstate["device_id"], pstate["device_token"], db))
    else:
        start_auto_enroll(db)
    yield


app = FastAPI(title="Ctrlable Provisioner", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _pve() -> ProxmoxClient:
    return ProxmoxClient(
        settings.pve_host,
        settings.pve_token_id,
        settings.pve_token_secret,
        settings.pve_node,
        settings.pve_verify_ssl,
    )


def _check_build_token(token: str) -> None:
    if not settings.build_token or token != settings.build_token:
        raise HTTPException(403, "invalid build token")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(req: LoginRequest) -> dict:
    s = get_settings()   # always fresh so runtime password changes take effect immediately
    if (
        req.username != s.admin_user
        or not verify_password(req.password, s.effective_password_hash)
    ):
        raise HTTPException(401, "Invalid credentials")
    return {
        "token": create_token(req.username, s),
        "must_change_password": s.must_change_password,
    }


@app.get("/api/auth/status")
def auth_status() -> dict:
    s = get_settings()   # always fresh — reflects .env changes without restart
    return {
        "auth_enabled": True,
        "must_change_password": s.must_change_password,
    }


class SetupPasswordRequest(BaseModel):
    new_password: str


@app.post("/api/auth/setup")
def setup_password(req: SetupPasswordRequest) -> dict:
    """First-time password setup — only works while must_change_password is True."""
    if not get_settings().must_change_password:
        raise HTTPException(403, "Password already configured — use change-password instead")
    if len(req.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    new_hash = hash_password(req.new_password)
    env_file = settings.env_file_path()
    if env_file.exists():
        text = env_file.read_text()
        if re.search(r"^ADMIN_PASSWORD_HASH=", text, re.MULTILINE):
            text = re.sub(r"^ADMIN_PASSWORD_HASH=.*$", f"ADMIN_PASSWORD_HASH={new_hash}", text, flags=re.MULTILINE)
        else:
            text += f"\nADMIN_PASSWORD_HASH={new_hash}\n"
        env_file.write_text(text)
    get_settings.cache_clear()
    fresh = get_settings()
    return {
        "token": create_token(fresh.admin_user, fresh),
        "must_change_password": False,
    }


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/auth/change-password")
def change_password(
    req: ChangePasswordRequest,
    _: str | None = Depends(require_auth),
) -> dict:
    s = get_settings()
    if not verify_password(req.current_password, s.admin_password_hash):
        raise HTTPException(401, "Current password is incorrect")
    if len(req.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    new_hash = hash_password(req.new_password)
    env_file = settings.env_file_path()
    if env_file.exists():
        text = env_file.read_text()
        if re.search(r"^ADMIN_PASSWORD_HASH=", text, re.MULTILINE):
            text = re.sub(r"^ADMIN_PASSWORD_HASH=.*$", f"ADMIN_PASSWORD_HASH={new_hash}", text, flags=re.MULTILINE)
        else:
            text += f"\nADMIN_PASSWORD_HASH={new_hash}\n"
        env_file.write_text(text)
    get_settings.cache_clear()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"ok": True}


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

@app.get("/api/releases")
def list_releases(_: str | None = Depends(require_auth)) -> list[dict]:
    return [dict(r) for r in db.list_releases()]


@app.get("/api/releases/{release}/manifest")
def get_manifest(release: str, token: str = Query(...)) -> dict:
    """Called by the host-side build script to fetch the manifest as JSON.
    Includes firstboot_secret and orchestrator_url so the build script can
    bake them into each template's /etc/ctrlable/firstboot.conf."""
    _check_build_token(token)
    manifests = load_all_manifests()
    if release not in manifests:
        raise HTTPException(404, f"release {release!r} not found")
    result = manifests[release].model_dump()
    row = db.get_release(release)
    if row:
        result["firstboot_secret"] = row["firstboot_secret"]
    result["orchestrator_url"] = settings.orchestrator_url
    return result


# ---------------------------------------------------------------------------
# Builds
# ---------------------------------------------------------------------------

class BuildRequest(BaseModel):
    release: str


@app.post("/api/builds", status_code=202)
async def start_build(req: BuildRequest, _: str | None = Depends(require_auth)) -> dict:
    if not settings.build_configured():
        raise HTTPException(400, "build plane not configured (BUILD_KEY_PATH / BUILD_TOKEN)")
    manifests = load_all_manifests()
    if req.release not in manifests:
        raise HTTPException(404, f"release {req.release!r} not found")
    try:
        build_id = await trigger_build(req.release, db, settings)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return {"build_id": build_id}


@app.get("/api/releases/{release}/templates")
def list_release_templates(release: str, token: str = Query(...)) -> list[dict]:
    _check_build_token(token)
    return [dict(r) for r in db.list_templates(release)]


@app.get("/api/builds")
def list_builds(_: str | None = Depends(require_auth)) -> list[dict]:
    return [dict(r) for r in db.list_builds()]


@app.get("/api/builds/{build_id}")
def get_build(build_id: int, _: str | None = Depends(require_auth)) -> dict:
    row = db.get_build(build_id)
    if not row:
        raise HTTPException(404)
    return dict(row)


# ---------------------------------------------------------------------------
# Provision callbacks
# ---------------------------------------------------------------------------

def _firstboot_path(filename: str) -> Path:
    return Path(__file__).parent.parent.parent / "host" / "firstboot" / filename


@app.get("/api/provision/firstboot-script")
def firstboot_script(token: str = Query(...)) -> Response:
    """Shell script baked into Debian LXC templates; fetched by ctrlable-build."""
    _check_build_token(token)
    p = _firstboot_path("ctrlable-firstboot.sh")
    if not p.exists():
        raise HTTPException(404, "firstboot script not found")
    return Response(content=p.read_text(), media_type="text/plain")


@app.get("/api/provision/firstboot-service-unit")
def firstboot_service_unit(token: str = Query(...)) -> Response:
    """Systemd unit baked into Debian LXC templates; fetched by ctrlable-build."""
    _check_build_token(token)
    p = _firstboot_path("ctrlable-firstboot.service")
    if not p.exists():
        raise HTTPException(404, "firstboot service unit not found")
    return Response(content=p.read_text(), media_type="text/plain")


@app.get("/api/provision/assignment")
def provision_assignment(hostname: str = Query(...), secret: str = Query(...)) -> dict:
    try:
        return get_assignment(db, hostname, secret)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


class CompleteRequest(BaseModel):
    hostname: str
    secret: str


@app.post("/api/provision/complete")
def provision_complete(req: CompleteRequest) -> dict:
    try:
        complete_provisioning(db, req.hostname, req.secret)
        return {"ok": True}
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


# ---------------------------------------------------------------------------
# Projects + Deploy (M5)
# ---------------------------------------------------------------------------

class DeployStackRequest(BaseModel):
    site_name: str
    release: str
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_pass: str = ""
    zigbee_coordinator: str = ""   # TCP URL for zigbee2mqtt coordinator
    zwave_coordinator: str = ""    # TCP URL for z-wave coordinator


@app.post("/api/projects", status_code=202)
async def create_project_deploy(req: DeployStackRequest, _: str | None = Depends(require_auth)) -> dict:
    if not settings.configured():
        raise HTTPException(400, "Proxmox not configured")
    manifests = load_all_manifests()
    if req.release not in manifests:
        raise HTTPException(404, f"release {req.release!r} not found")

    built = db.list_templates(req.release)
    if not built:
        raise HTTPException(400, f"Release {req.release!r} has no built templates — run a build first")

    project_id = db.create_project(req.site_name, req.release)
    asyncio.create_task(
        deploy_stack_async(project_id, req.release, req.model_dump(), db, _pve())
    )
    return {"project_id": project_id, "status": "deploying"}


class AddInstanceRequest(BaseModel):
    template_name: str
    wire_to: dict = {}


@app.post("/api/projects/{project_id}/instances", status_code=202)
async def add_instance(project_id: int, req: AddInstanceRequest, _: str | None = Depends(require_auth)) -> dict:
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404)
    try:
        inst = await asyncio.to_thread(
            deploy_instance, project_id, req.template_name,
            project["release"], req.wire_to, db, _pve()
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return inst


@app.get("/api/projects")
def list_projects_api(_: str | None = Depends(require_auth)) -> list[dict]:
    result = []
    for p in db.list_projects():
        instances = db.list_instances(p["id"])
        result.append({**dict(p), "instances": [dict(i) for i in instances]})
    return result


@app.get("/api/projects/{project_id}")
def get_project_api(project_id: int, _: str | None = Depends(require_auth)) -> dict:
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404)
    instances = db.list_instances(project["id"])
    return {**dict(project), "instances": [dict(i) for i in instances]}


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project_api(project_id: int, _: str | None = Depends(require_auth)) -> None:
    instances = db.list_instances(project_id)
    if instances:
        raise HTTPException(400, "Cannot delete a project that has running instances")
    if not db.delete_project(project_id):
        raise HTTPException(404)


# ---------------------------------------------------------------------------
# Host device inventory (USB / PCI)
# ---------------------------------------------------------------------------

@app.get("/api/host/devices/usb")
def list_usb(_: str | None = Depends(require_auth)) -> list[dict]:
    if not settings.configured():
        return []
    return _pve().list_usb_devices()


@app.get("/api/host/devices/pci")
def list_pci(_: str | None = Depends(require_auth)) -> list[dict]:
    if not settings.configured():
        return []
    return _pve().list_pci_devices()


# ---------------------------------------------------------------------------
# Guest config — on-boot, USB, PCI passthrough, disk resize
# ---------------------------------------------------------------------------

@app.get("/api/guests/{vmid}/config")
def get_guest_config(vmid: int, _: str | None = Depends(require_auth)) -> dict:
    inst = db.get_instance_by_vmid(vmid)
    kind = "lxc"
    if inst:
        tmpl = db.get_template(inst["release"], inst["type"])
        if tmpl:
            kind = tmpl["kind"]
    # Fallback: try both and return whichever works
    px = _pve()
    try:
        return {"kind": kind, "config": px.get_guest_config(kind, vmid)}
    except Exception:
        other = "qemu" if kind == "lxc" else "lxc"
        try:
            return {"kind": other, "config": px.get_guest_config(other, vmid)}
        except Exception as e:
            raise HTTPException(404, f"Could not get config: {e}")


class GuestConfigUpdate(BaseModel):
    onboot: bool | None = None
    usb_add: str | None = None      # e.g. "host=0558:1001"
    usb_del: str | None = None      # e.g. "usb0"
    pci_add: str | None = None      # e.g. "0000:01:00.0,pcie=1"
    pci_del: str | None = None      # e.g. "hostpci0"


@app.put("/api/guests/{vmid}/config")
def update_guest_config(
    vmid: int,
    req: GuestConfigUpdate,
    _: str | None = Depends(require_auth),
) -> dict:
    cfg_resp = get_guest_config(vmid)
    kind = cfg_resp["kind"]
    cfg = cfg_resp["config"]
    px = _pve()
    changes: dict = {}
    deletes: list[str] = []

    if req.onboot is not None:
        changes["onboot"] = 1 if req.onboot else 0

    if req.usb_add:
        # Find the next free usb slot
        used = {k for k in cfg if re.match(r"^usb\d+$", k)}
        slot = next(f"usb{i}" for i in range(10) if f"usb{i}" not in used)
        changes[slot] = req.usb_add

    if req.usb_del:
        deletes.append(req.usb_del)

    if req.pci_add and kind == "qemu":
        used = {k for k in cfg if re.match(r"^hostpci\d+$", k)}
        slot = next(f"hostpci{i}" for i in range(10) if f"hostpci{i}" not in used)
        changes[slot] = req.pci_add

    if req.pci_del and kind == "qemu":
        deletes.append(req.pci_del)

    if changes or deletes:
        px.update_guest_config(kind, vmid, changes or None, deletes or None)
    return {"ok": True}


class ResizeRequest(BaseModel):
    disk: str   # e.g. "scsi0" for VMs, "rootfs" for LXC
    size: str   # absolute size like "32G" or delta like "+10G"


@app.post("/api/guests/{vmid}/resize")
def resize_guest_disk(
    vmid: int,
    req: ResizeRequest,
    _: str | None = Depends(require_auth),
) -> dict:
    cfg_resp = get_guest_config(vmid)
    kind = cfg_resp["kind"]
    try:
        _pve().resize_disk(kind, vmid, req.disk, req.size)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Guest lifecycle controls
# ---------------------------------------------------------------------------

@app.post("/api/guests/{vmid}/{action}")
def guest_action(vmid: int, action: str, _: str | None = Depends(require_auth)) -> dict:
    if action not in ("start", "stop", "reboot"):
        raise HTTPException(400, f"invalid action: {action!r}")

    inst = db.get_instance_by_vmid(vmid)
    kind = "lxc"
    if inst:
        tmpl = db.get_template(inst["release"], inst["type"])
        if tmpl:
            kind = tmpl["kind"]

    px = _pve()
    if action == "start":
        px.start_guest(kind, vmid)
    elif action == "stop":
        px.stop_guest(kind, vmid)
    elif action == "reboot":
        px.reboot_guest(kind, vmid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Built templates (user-auth — for UI display)
# ---------------------------------------------------------------------------

@app.get("/api/releases/{release}/built-templates")
def list_built_templates(release: str, _: str | None = Depends(require_auth)) -> list[dict]:
    return [dict(r) for r in db.list_templates(release)]


# ---------------------------------------------------------------------------
# Platform enrollment (M8)
# ---------------------------------------------------------------------------

@app.get("/api/platform/status")
def platform_status(_: str | None = Depends(require_auth)) -> dict:
    return _platform_get_status(db)


class PlatformEnrollRequest(BaseModel):
    token: str


@app.post("/api/platform/enroll")
async def platform_enroll_endpoint(req: PlatformEnrollRequest, _: str | None = Depends(require_auth)) -> dict:
    try:
        device_id, device_token, tunnel_ip, wg_iface = await asyncio.to_thread(_platform_enroll, req.token, db)
        start_heartbeat(device_id, device_token, db)
        asyncio.create_task(asyncio.to_thread(setup_lan_access, device_id, device_token, db))
        return {"device_id": device_id, "tunnel_ip": tunnel_ip, "wg_iface": wg_iface}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ---------------------------------------------------------------------------
# LAN status
# ---------------------------------------------------------------------------

@app.get("/api/lan/status")
def lan_status(_: str | None = Depends(require_auth)) -> dict:
    pstate = db.get_platform_state()
    if not pstate or not pstate["lan_subnet"]:
        return {"configured": False}
    return {
        "configured":    True,
        "lan_iface":     pstate["lan_iface"],
        "lan_subnet":    pstate["lan_subnet"],
        "proxy_subnet":  pstate["proxy_subnet"],
        "nat_active":    True,
    }


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/api/dashboard")
def dashboard(_: str | None = Depends(require_auth)) -> dict:
    if not settings.configured():
        return {"configured": False, "host": None, "guests": []}

    try:
        px = _pve()
        host_health = px.node_health()
        raw_guests = px.list_guests()
    except Exception as exc:
        return {"configured": True, "error": str(exc), "host": None, "guests": []}

    instances = {row["hostname"]: row for row in db.list_all_instances()}

    guests = []
    for g in raw_guests:
        inst = instances.get(g.name)
        guests.append({
            "vmid": g.vmid,
            "hostname": g.name,
            "kind": g.kind,
            "status": g.status,
            "cpu": g.cpu,
            "mem": g.mem,
            "maxmem": g.maxmem,
            "project": inst["site_name"] if inst else None,
            "type": inst["type"] if inst else None,
            "release": inst["release"] if inst else None,
            "db_status": inst["status"] if inst else None,
        })

    return {
        "configured": True,
        "host": {
            "node": host_health.node,
            "cpu": host_health.cpu,
            "mem_used": host_health.mem_used,
            "mem_total": host_health.mem_total,
            "disk_used": host_health.disk_used,
            "disk_total": host_health.disk_total,
            "uptime": host_health.uptime,
        },
        "guests": guests,
    }


# ---------------------------------------------------------------------------
# Serve built frontend (production)
# ---------------------------------------------------------------------------

_dist = Path(__file__).parent.parent.parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="static")
