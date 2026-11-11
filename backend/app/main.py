import asyncio
import json
import os
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
from .deploy import deploy_instance, deploy_stack_async, deploy_templates_async
from .drift import Severity
from .drift_scan import scan_project
from .manifest import load_all_manifests
from . import internal_net as inet
from . import selfupdate
from .lan import setup_lan_access, resolve_orchestrator_url
from .platform import enroll as _platform_enroll, ensure_tunnel_up, get_status as _platform_get_status, start_auto_enroll, start_heartbeat
from .provision import complete_provisioning, get_assignment
from .provider import GuestKind, GuestRef, Provider
from .proxmox import ProxmoxProvider
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


def _provider() -> Provider:
    """The virtualization backend. Returns the neutral Provider interface, not a
    concrete class -- swapping Proxmox for a Debian/libvirt backend later is a
    change here and in provider construction only. See provider.py."""
    return ProxmoxProvider(
        settings.pve_host,
        settings.pve_token_id,
        settings.pve_token_secret,
        settings.pve_node,
        settings.pve_verify_ssl,
    )


def _ref(vmid: int, kind: str) -> GuestRef:
    """Bridge the app's VMID-native world to a GuestRef. The DB and REST API
    still speak integer vmids and "lxc"/"qemu"; this converts at the boundary so
    the provider interface stays hypervisor-neutral."""
    return GuestRef(id=str(vmid), kind=GuestKind.from_proxmox(kind))


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

INSTALL_RECORD = os.environ.get(
    "CTRLABLE_INSTALL_RECORD", "/opt/ctrlable-provisioner/install.json")


def _install_record() -> dict:
    """What this orchestrator is running, as recorded at install time.

    "What is this orchestrator running?" had no answer. The CT's tree is NOT
    always a git checkout: install.sh clones when it is pointed at the repo,
    but with --local-src it tars the source in with `--exclude=.git`, which is
    how the live one was built -- so `git describe` inside the container
    returns nothing and the directory can drift from the repo with no record
    that it has.

    The same fault was just found and fixed one layer down, on the hardware
    manager: an appliance reported version 0.1.0 for six weeks while running
    code that revision never had, because the path the code actually arrived
    by did not record itself. So this reads the record install.sh now writes,
    rather than trying to derive identity from a tree that may have none.
    """
    try:
        with open(INSTALL_RECORD) as fh:
            return json.load(fh)
    except FileNotFoundError:
        # Installed before the record existed. Say that, rather than inventing
        # a version -- "unknown" is actionable (re-run install.sh) and a guess
        # is not.
        return {"version": "unknown", "revision": "unknown",
                "note": "installed before install.json was written; "
                        "re-run install.sh to record it"}
    except Exception as exc:                      # noqa: BLE001
        return {"version": "unknown", "revision": "unknown",
                "note": "unreadable install record: %s" % exc}


@app.get("/health")
def health() -> dict:
    # The install record rides on /health because that is the endpoint anything
    # already calls to ask "are you there?", and "which version is there" is
    # the next question every time.
    return {"ok": True, "install": _install_record()}


@app.get("/api/version")
def version() -> dict:
    """Unauthenticated on purpose: it reports a version, not a secret, and the
    whole point is that it can be asked of a fleet without credentials."""
    return _install_record()


@app.get("/api/whoami")
def whoami() -> dict:
    """Unauthenticated discovery marker. The Ctrlable Pro orchestrator add-on
    scans the site LAN for this exact signature to find the orchestrator without
    anyone typing an IP — a plain version endpoint isn't distinctive enough, so
    this names the service explicitly."""
    rec = _install_record()
    return {"service": "ctrlable-orchestrator",
            "version": rec.get("version", "unknown"),
            "revision": rec.get("revision", "unknown")}


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
    url = resolve_orchestrator_url(settings.orchestrator_url)
    if url:
        result["orchestrator_url"] = url
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
    if not settings.build_configured():
        raise HTTPException(400, "Build plane not configured (BUILD_KEY_PATH / BUILD_TOKEN)")
    manifests = load_all_manifests()
    if req.release not in manifests:
        raise HTTPException(404, f"release {req.release!r} not found")

    project_id = db.create_project(req.site_name, req.release)
    asyncio.create_task(
        deploy_stack_async(project_id, req.release, req.model_dump(), db, settings)
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
    asyncio.create_task(
        deploy_instance(project_id, req.template_name, project["release"], req.wire_to, db, settings)
    )
    return {"status": "deploying", "template_name": req.template_name}


@app.post("/api/projects/{project_id}/redeploy-missing", status_code=202)
async def redeploy_missing(project_id: int, req: DeployStackRequest,
                           _: str | None = Depends(require_auth)) -> dict:
    """Redeploy every template of the project's release that has no active or
    in-progress instance. Failed rows are removed first (and a half-created
    guest destroyed), so re-running a deploy converges instead of duplicating."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404)
    manifest = load_all_manifests().get(project["release"])
    if not manifest:
        raise HTTPException(404, f"release {project['release']!r} not found")
    instances = db.list_instances(project_id)
    healthy = {i["type"] for i in instances if i["status"] in ("active", "provisioning", "pending")}
    missing = [n for n in manifest.templates if n not in healthy]
    for inst in instances:
        if inst["type"] in missing and inst["status"] == "error":
            if inst["vmid"] and settings.configured():
                try:
                    _provider().destroy(_ref(inst["vmid"], inst["kind"] or "lxc"))
                except Exception:
                    pass
            db.delete_instance(inst["id"])
    if missing:
        asyncio.create_task(deploy_templates_async(
            project_id, project["release"], missing, req.model_dump(), db, settings))
    return {"redeploying": missing}


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


@app.get("/api/projects/{project_id}/drift")
def project_drift_api(project_id: int, _: str | None = Depends(require_auth)) -> dict:
    """Compare every instance against its release manifest (SPEC § 16, M9).

    Read-only: it resolves each guest's address, reads the Z-Wave JS schema off
    the live websocket, compares against the manifest pins and `requires`, and
    returns a row per instance. The version dimension needs an in-guest exec
    transport that is not wired to the restricted host key yet (§ 18), so it is
    left unconfigured here and reports UNKNOWN rather than a false OK."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(404)

    manifests = load_all_manifests()
    manifest = manifests.get(project["release"])
    specs = manifest.templates if manifest else {}
    instances = db.list_instances(project_id)

    get_config = None
    if settings.configured():
        provider = _provider()

        def get_config(vmid: int, kind: str) -> dict:  # noqa: F811
            return provider.get_config(_ref(vmid, kind))

    rows = scan_project(instances, specs, get_config=get_config)
    worst = max((Severity[r.severity] for r in rows), default=Severity.OK)
    return {
        "project_id": project_id,
        "release": project["release"],
        "manifest_present": manifest is not None,
        "severity": worst.name,
        "rows": [r.to_dict() for r in rows],
    }


@app.delete("/api/projects/{project_id}/instances/{instance_id}", status_code=204)
def delete_instance_api(
    project_id: int,
    instance_id: int,
    destroy: bool = False,
    _: str | None = Depends(require_auth),
) -> None:
    inst = db.get_instance_by_vmid(0)  # just to get the row shape; fetch by id below
    # fetch by id via list
    instances = db.list_instances(project_id)
    inst = next((i for i in instances if i["id"] == instance_id), None)
    if not inst:
        raise HTTPException(404)
    if destroy and settings.configured():
        try:
            kind = inst["kind"] if inst["kind"] else "lxc"
            _provider().destroy(_ref(inst["vmid"], kind))
        except Exception:
            pass
    db.delete_instance(instance_id)


@app.delete("/api/projects/{project_id}", status_code=204)
def delete_project_api(
    project_id: int,
    destroy: bool = False,
    _: str | None = Depends(require_auth),
) -> None:
    if not db.get_project(project_id):
        raise HTTPException(404)
    if destroy and settings.configured():
        instances = db.list_instances(project_id)
        px = _provider()
        for inst in instances:
            try:
                kind = inst["kind"] if inst["kind"] else "lxc"
                px.destroy(_ref(inst["vmid"], kind))
            except Exception:
                pass
    if not db.delete_project(project_id):
        raise HTTPException(404)


# ---------------------------------------------------------------------------
# Host device inventory (USB / PCI)
# ---------------------------------------------------------------------------

@app.get("/api/host/devices/usb")
def list_usb(_: str | None = Depends(require_auth)) -> list[dict]:
    if not settings.configured():
        return []
    return _provider().list_usb_devices()


@app.get("/api/host/devices/pci")
def list_pci(_: str | None = Depends(require_auth)) -> list[dict]:
    if not settings.configured():
        return []
    return _provider().list_pci_devices()


# ---------------------------------------------------------------------------
# Guest config — on-boot, USB, PCI passthrough, disk resize
# ---------------------------------------------------------------------------

@app.get("/api/guests/{vmid}/config")
def get_guest_config(vmid: int, _: str | None = Depends(require_auth)) -> dict:
    inst = db.get_instance_by_vmid(vmid)
    kind = inst["kind"] if inst else "lxc"
    # Fallback: try both and return whichever works
    px = _provider()
    try:
        return {"kind": kind, "config": px.get_config(_ref(vmid, kind))}
    except Exception:
        other = "qemu" if kind == "lxc" else "lxc"
        try:
            return {"kind": other, "config": px.get_config(_ref(vmid, other))}
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
    px = _provider()
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
        px.update_config(_ref(vmid, kind), changes or None, deletes or None)
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
        _provider().resize_disk(_ref(vmid, kind), req.disk, req.size)
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
    kind = inst["kind"] if inst else "lxc"

    px = _provider()
    ref = _ref(vmid, kind)
    if action == "start":
        px.start(ref)
    elif action == "stop":
        px.stop(ref)
    elif action == "reboot":
        px.reboot(ref)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Built templates (user-auth — for UI display)
# ---------------------------------------------------------------------------

@app.get("/api/releases/{release}/built-templates")
def list_built_templates(release: str, _: str | None = Depends(require_auth)) -> list[dict]:
    """Templates that can be deployed for a release.

    Was `db.list_templates()` — the templates BUILT by the old
    template-and-clone model. ctrlable-build was rewritten to deploy instances
    directly, so nothing is ever recorded there and this always returned []. The
    Add Instance dialog reads this endpoint, so it always showed "No built
    templates" and a single component could never be deployed on its own: after
    any partial stack failure the only route back was redeploying everything.

    The manifest is the real source of what a release can deploy, so read that.
    Rows recorded by db.list_templates are merged in when present, to keep any
    extra fields (template_vmid, app_version) a legacy deployment may still show.
    """
    built = {r["name"]: dict(r) for r in db.list_templates(release)}
    # load_all_manifests() returns pydantic ReleaseManifest objects, not dicts —
    # templates is dict[str, TemplateSpec], so reach through attributes.
    manifest = load_all_manifests().get(release)
    rows: list[dict] = []
    for name, tmpl in (manifest.templates if manifest else {}).items():
        row = {"name": name,
               "kind": getattr(tmpl, "kind", "lxc"),
               "app_version": getattr(tmpl, "app_version", None),
               "template_vmid": None}
        row.update(built.get(name) or {})
        rows.append(row)
    # Anything recorded but no longer in the manifest still deserves listing.
    for name, row in built.items():
        if not any(r["name"] == name for r in rows):
            rows.append(row)
    return rows


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
# Internal management LAN (SPEC § 20) — vmbr1 + dnsmasq + per-guest net1.
# New appliances get this at bootstrap. These endpoints bring it up on an
# ALREADY-DEPLOYED appliance remotely: the host work runs in ctrlable-build
# over the same command-restricted key as a deploy.
# ---------------------------------------------------------------------------

def _broker_instance() -> dict | None:
    """The deployed HAOS VM (template `ctrlable-pro`), or None."""
    for row in db.list_all_instances():
        if row["type"] == inet.BROKER_TEMPLATE:
            return dict(row)
    return None


@app.get("/api/lan/internal")
def internal_lan_status(_: str | None = Depends(require_auth)) -> dict:
    """The internal-LAN PLAN for this appliance's release and whether the broker
    VM is known. Live host state (bridge up, broker on .10) is reported by the
    enable run's own probe — the orchestrator cannot see the host bridge from here."""
    broker = _broker_instance()
    release = broker["release"] if broker else None
    net = None
    if release:
        manifests = load_all_manifests()
        if release in manifests:
            net = inet.from_manifest(manifests[release])
    if not net:
        return {"configured": False, "broker_known": broker is not None}
    return {
        "configured":   True,
        "bridge":       net.bridge,
        "subnet":       net.subnet,
        "gateway":      net.gateway,
        "broker_ip":    net.mqtt_host(),
        "broker_vmid":  broker["vmid"] if broker else None,
        "broker_known": broker is not None,
        "assignments":  {t: net.ip_for(t) for t in net.assignments},
    }


@app.post("/api/lan/internal/enable")
async def internal_lan_enable(_: str | None = Depends(require_auth)) -> dict:
    """Bring the internal LAN up on this appliance's host and attach the broker
    VM. Idempotent; does NOT reboot the HA VM (net1 is hot-plugged, the result
    says whether it came up or a reboot is needed)."""
    if not settings.build_configured():
        raise HTTPException(400, "host trigger not configured (build key/host)")
    broker = _broker_instance()
    if not broker:
        raise HTTPException(409, "no deployed ctrlable-pro VM found; cannot resolve "
                                 "the release or the broker vmid to attach")
    release, ha_vmid = broker["release"], broker["vmid"]
    cmd = [
        "ssh", "-i", settings.build_key_path,
        "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=60",
        f"root@{settings.pve_host}",
        f"ctrlable-build enable-internal-net {release} {ha_vmid}",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _err = await proc.communicate()
    output = out.decode(errors="replace")
    ok = proc.returncode == 0
    reachable = "OK: broker reachable" in output
    return {
        "ok": ok,
        "release": release,
        "broker_vmid": ha_vmid,
        "broker_reachable": reachable,
        "reboot_suggested": ok and not reachable,
        "output": output,
    }


class MigrateInternalRequest(BaseModel):
    vmid: int
    template: str


@app.post("/api/lan/internal/migrate")
async def internal_lan_migrate(
    req: MigrateInternalRequest,
    _: str | None = Depends(require_auth),
) -> dict:
    """Move an already-deployed LXC service onto the internal LAN in place:
    attach its net1 and re-home its app wiring (for a broker client like z2m,
    rewrite its MQTT server to the internal broker and restart). Idempotent."""
    if not settings.build_configured():
        raise HTTPException(400, "host trigger not configured (build key/host)")
    if req.template == inet.BROKER_TEMPLATE:
        raise HTTPException(400, "the broker VM is the hub of the internal LAN, not a service to migrate")
    broker = _broker_instance()
    if not broker:
        raise HTTPException(409, "no deployed ctrlable-pro VM found; cannot resolve the release")
    release = broker["release"]
    manifests = load_all_manifests()
    net = inet.from_manifest(manifests[release]) if release in manifests else None
    if not net or net.ip_for(req.template) is None:
        raise HTTPException(400, f"{req.template} has no internal-LAN assignment in release {release}")
    cmd = [
        "ssh", "-i", settings.build_key_path,
        "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=60",
        f"root@{settings.pve_host}",
        f"ctrlable-build migrate-internal {release} {req.template} {req.vmid}",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _err = await proc.communicate()
    output = out.decode(errors="replace")
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "template": req.template,
        "vmid": req.vmid,
        "internal_ip": net.ip_for(req.template),
        # the host tool prints "OK:" once the move is complete (reconnected, or
        # NIC-attached for an HA-served service); "reconnected" marks a live
        # broker client that re-attached to MQTT.
        "done": ok and "OK:" in output,
        "reconnected": "reconnected to the broker" in output,
        "output": output,
    }


# ---------------------------------------------------------------------------
# Provisioner self-update (SPEC § 16.6)
# ---------------------------------------------------------------------------

class SelfUpdateRequest(BaseModel):
    ref: str | None = None   # git ref to update to; defaults to the installed ref


@app.post("/api/system/self-update")
async def system_self_update(
    req: SelfUpdateRequest,
    _: str | None = Depends(require_auth),
) -> dict:
    """Update the orchestrator to a ref and refresh the host tools, then restart.
    Refused while a build or deploy is in flight (§ 16.6)."""
    from . import build as _build
    if _build._running:
        raise HTTPException(409, f"a build is in progress ({', '.join(sorted(_build._running))}); "
                                 f"retry once it finishes")
    inflight = [r["hostname"] for r in db.list_all_instances()
                if r["status"] in ("pending", "provisioning")]
    if inflight:
        raise HTTPException(409, f"a deploy is in progress ({', '.join(inflight)}); "
                                 f"retry once it finishes")
    ref = req.ref or _install_record().get("ref") or "main"
    # The subprocess work is blocking; keep the event loop free.
    result = await asyncio.to_thread(selfupdate.run_self_update, ref, settings)
    if not result.get("ok"):
        # Report the failing stage but keep 200 so the client sees the step log;
        # the orchestrator did NOT restart and was rolled back where possible.
        return {"ok": False, **result}
    return result


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/api/dashboard")
def dashboard(_: str | None = Depends(require_auth)) -> dict:
    if not settings.configured():
        return {"configured": False, "host": None, "guests": []}

    try:
        px = _provider()
        host_health = px.node_health()
        raw_guests = px.list_guests(
            exclude={str(v) for v in settings.exclude_vmids_set()})
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
