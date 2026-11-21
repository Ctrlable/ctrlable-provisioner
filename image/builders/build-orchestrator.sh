#!/usr/bin/env bash
#
# build-orchestrator.sh — publish the Ctrlable provisioner/orchestrator as an Incus image.
#
# This closes the zero-touch gap: first boot launches `ctrlable-orchestrator` from this image
# (ctrlable-firstboot.sh), and the app self-enrols + deploys the fleet. The image replicates
# exactly how the provisioner runs today (deploy/ctrlable-provisioner.service):
#     /opt/ctrlable-provisioner/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
# plus the one migration change: provider=incus, and an `incus-client` (no daemon) that talks
# to the HOST daemon over the socket first-boot proxies in at /var/lib/incus/unix.socket.
#
# Run as root on a Debian 13 host with Incus. SRC must be a checkout of this repo (backend/,
# frontend/dist/, deploy/). Produces an image aliased `ctrlable-orchestrator` and, with
# --export, a tarball at $OUT for baking into the appliance image (/opt/ctrlable/images/).
#
set -euo pipefail
: "${SRC:=/root/ctrlable-build/provisioner-src}"   # repo checkout with backend/ frontend/dist/ deploy/
: "${ALIAS:=ctrlable-orchestrator}"
: "${BASE:=images:debian/13}"
: "${OUT:=/root/ctrlable-build/${ALIAS}.tar.zst}"
BUILD="ctrlable-orch-build"
EXPORT=0; [ "${1:-}" = "--export" ] && EXPORT=1
log(){ echo "[build-orchestrator] $*"; }

[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
command -v incus >/dev/null || { echo "incus not installed"; exit 1; }
[ -d "$SRC/backend" ] || { echo "SRC=$SRC has no backend/ — point SRC at a repo checkout"; exit 1; }

log "launch build container"
incus delete -f "$BUILD" 2>/dev/null || true
incus launch "$BASE" "$BUILD" </dev/null
sleep 4
for i in $(seq 1 30); do incus exec "$BUILD" -- getent hosts deb.debian.org >/dev/null 2>&1 && break; sleep 2; done

log "install runtime: python venv + the incus CLI (Debian has no client-only pkg, so install"
log "  the full 'incus' and MASK its daemon — the CLI then uses the host socket proxied in)"
incus exec "$BUILD" -- bash -c '
  set -e; export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip ca-certificates incus >/dev/null
  # never run a nested incus daemon in the orchestrator container; the client talks to the
  # HOST daemon via the socket first-boot proxies to /var/lib/incus/unix.socket.
  systemctl mask incus.service incus.socket incus-startup.service incus-user.socket incus-user.service 2>/dev/null || true
  systemctl disable incus.service incus.socket 2>/dev/null || true'

log "push the app source -> /opt/ctrlable-provisioner"
incus exec "$BUILD" -- mkdir -p /opt/ctrlable-provisioner
tar -C "$SRC" -cf - backend frontend/dist deploy 2>/dev/null | \
  incus exec "$BUILD" -- tar -C /opt/ctrlable-provisioner -xf -

log "write the install record (so /health + the Platform tab report a real version, not 'unknown')"
VER="$(git -C "$SRC" describe --tags --always --dirty 2>/dev/null || echo "${ORCH_VERSION:-unknown}")"
REV="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || echo "${ORCH_REVISION:-unknown}")"
DIRTY=false; git -C "$SRC" diff --quiet 2>/dev/null || DIRTY=true
incus exec "$BUILD" -- sh -c 'cat > /opt/ctrlable-provisioner/install.json' <<JSON
{"version":"$VER","revision":"$REV","dirty":$DIRTY,"built_by":"build-orchestrator.sh","built_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
JSON

log "create venv + install backend requirements"
incus exec "$BUILD" -- bash -c '
  set -e
  python3 -m venv /opt/ctrlable-provisioner/.venv
  /opt/ctrlable-provisioner/.venv/bin/pip install -q --upgrade pip
  /opt/ctrlable-provisioner/.venv/bin/pip install -q -r /opt/ctrlable-provisioner/backend/requirements.txt'

log "install the service unit + set provider=incus"
incus exec "$BUILD" -- bash -c '
  set -e
  install -D -m0644 /opt/ctrlable-provisioner/deploy/ctrlable-provisioner.service \
      /etc/systemd/system/ctrlable-provisioner.service
  mkdir -p /etc/ctrlable
  # default env; first-boot overwrites /etc/ctrlable/.env with the real enrol config
  grep -q "^provider=" /opt/ctrlable-provisioner/backend/.env 2>/dev/null || \
      printf "provider=incus\n" >> /opt/ctrlable-provisioner/backend/.env
  systemctl enable ctrlable-provisioner >/dev/null 2>&1'

log "verify it imports + starts (sanity, not a full run)"
incus exec "$BUILD" -- /opt/ctrlable-provisioner/.venv/bin/python -c \
  "import sys; sys.path.insert(0,'/opt/ctrlable-provisioner/backend'); import app.main; print('app.main imports OK')"

log "publish image -> alias $ALIAS"
incus stop "$BUILD" </dev/null
incus image delete "$ALIAS" 2>/dev/null || true
incus publish "$BUILD" --alias "$ALIAS" </dev/null
incus delete -f "$BUILD" </dev/null
incus image list "$ALIAS"

if [ "$EXPORT" = 1 ]; then
  log "export image tarball -> $OUT (for baking into the appliance image)"
  mkdir -p "$(dirname "$OUT")"
  incus image export "$ALIAS" "${OUT%.tar.zst}" </dev/null   # incus appends .tar.gz/.tar.zst
  log "exported (see $(dirname "$OUT"))"
fi
log "done: $ALIAS ready; first-boot launches it + proxies the host Incus socket in"
