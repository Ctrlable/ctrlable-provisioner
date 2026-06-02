from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .build import trigger_build
from .config import get_settings
from .manifest import load_all_manifests
from .provision import complete_provisioning, get_assignment
from .proxmox import ProxmoxClient
from .state import StateDB

settings = get_settings()
db = StateDB(settings.db_file())


@asynccontextmanager
async def lifespan(app: FastAPI):
    for m in load_all_manifests().values():
        db.seed_release(m)
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
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"ok": True}


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

@app.get("/api/releases")
def list_releases() -> list[dict]:
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
async def start_build(req: BuildRequest) -> dict:
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


@app.get("/api/builds")
def list_builds() -> list[dict]:
    return [dict(r) for r in db.list_builds()]


@app.get("/api/builds/{build_id}")
def get_build(build_id: int) -> dict:
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
def firstboot_script(token: str = Query(...)) -> str:
    """Shell script baked into Debian LXC templates; fetched by ctrlable-build."""
    _check_build_token(token)
    p = _firstboot_path("ctrlable-firstboot.sh")
    if not p.exists():
        raise HTTPException(404, "firstboot script not found")
    return p.read_text()


@app.get("/api/provision/firstboot-service-unit")
def firstboot_service_unit(token: str = Query(...)) -> str:
    """Systemd unit baked into Debian LXC templates; fetched by ctrlable-build."""
    _check_build_token(token)
    p = _firstboot_path("ctrlable-firstboot.service")
    if not p.exists():
        raise HTTPException(404, "firstboot service unit not found")
    return p.read_text()


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
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/api/dashboard")
def dashboard() -> dict:
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
