#!/usr/bin/env bash
#
# install-orchestrator-host.sh — install the provisioner/orchestrator INTO THE HOST ROOTFS so the
# appliance runs it as a native systemd service (HOST mode), not in an LXC. The container path
# (image/builders/build-orchestrator.sh) is UNCHANGED and still used for Proxmox + other appliances;
# this is purely additive — the appliance image defaults to host mode (firstboot selects it).
#
# Why host mode: one enrolment for the whole box (bare-metal VPN access for support), native Incus
# over the LOCAL /var/lib/incus/unix.socket (no proxy device, no privileged container, no /host/proc
# mount), and the orchestrator becomes part of the atomically-versioned A/B firmware.
#
# Immutable-root rule: build the venv + SPA at the REAL runtime path /opt/ctrlable-provisioner (so the
# venv shebangs are valid on the booted appliance), copy that tree into $ROOT, and keep ALL mutable
# state (DB, .env, enrol/WG keys) on the `data` partition via a .env symlink — the A/B root carries
# only code, state survives every slot swap.
#
#   usage: install-orchestrator-host.sh <ROOT> <SRC>
#     ROOT  image rootfs (…/work/rootfs)
#     SRC   repo checkout (backend/ frontend/ deploy/)
set -euo pipefail
ROOT="${1:?ROOT (rootfs) required}"
SRC="${2:?SRC (repo) required}"
here="$(cd "$(dirname "$0")/.." && pwd)"   # image/
log(){ echo "[install-orchestrator-host] $*"; }

[ -d "$SRC/backend" ] || { echo "SRC has no backend/"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

log "build deps (python venv + node for the SPA)"
apt-get install -y --no-install-recommends python3 python3-venv python3-pip nodejs npm ca-certificates \
  >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip nodejs npm ca-certificates; }

APP=/opt/ctrlable-provisioner
rm -rf "$APP"; mkdir -p "$APP"
log "copy backend + deploy -> $APP"
cp -a "$SRC/backend" "$APP/backend"
cp -a "$SRC/deploy"  "$APP/deploy" 2>/dev/null || true

log "frontend SPA -> $APP/frontend/dist"
if [ -f "$SRC/frontend/dist/index.html" ]; then
  mkdir -p "$APP/frontend"; cp -a "$SRC/frontend/dist" "$APP/frontend/dist"
else
  ( cd "$SRC/frontend" && npm ci --silent && npm run build --silent )
  mkdir -p "$APP/frontend"; cp -a "$SRC/frontend/dist" "$APP/frontend/dist"
fi

log "python venv (at the runtime path) + requirements"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/backend/requirements.txt"

log "install record (version/revision -> /health + Platform tab)"
VER="$(git -C "$SRC" describe --tags --always --dirty 2>/dev/null || echo unknown)"
REV="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || echo unknown)"
DIRTY=false; git -C "$SRC" diff --quiet 2>/dev/null || DIRTY=true
cat > "$APP/install.json" <<JSON
{"version":"$VER","revision":"$REV","dirty":$DIRTY,"mode":"host","built_by":"install-orchestrator-host.sh","built_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
JSON

log "state on data: backend/.env -> /data/orchestrator/.env (dangling until firstboot creates it)"
rm -f "$APP/backend/.env"
ln -s /data/orchestrator/.env "$APP/backend/.env"

log "copy the app tree into the rootfs + install the host service unit"
mkdir -p "$ROOT/opt"
cp -a "$APP" "$ROOT/opt/"
install -D -m0644 "$here/config/orchestrator/ctrlable-orchestrator.service" \
    "$ROOT/etc/systemd/system/ctrlable-orchestrator.service"
install -D -m0644 "$here/config/orchestrator/data-orchestrator.mount" \
    "$ROOT/etc/systemd/system/data-orchestrator.mount"

log "done — firstboot (host mode) creates /data/orchestrator + .env and starts the service"
