#!/usr/bin/env bash
#
# ctrlable-firstboot.sh — zero-touch provisioning, runs ONCE on first boot of a flashed
# appliance. No human input: the orchestrator key is baked into /root/.ssh/authorized_keys
# and Incus is pre-installed, so this brings up the persistent pool and launches the
# ORCHESTRATOR container, which then owns the rest.
#
# DIVISION OF LABOUR (important — first-boot does NOT reimplement enrol/deploy):
#   first-boot (here):  Incus + the `data` pool  ->  launch ctrlable-orchestrator
#   orchestrator (the provisioner app, platform.py/deploy.py):
#       self-enrols with the portal using the baked provisioning key (POST /api/platform
#       /enroll + heartbeat), fetches this appliance's release manifest, and deploys the
#       fleet containers (Ctrlable Pro = Supervised-LXC, services) via the Incus provider.
#   So the "call-home + manifest apply" lives in the app we already have, reached over the
#   host Incus socket we hand the orchestrator below — not duplicated in this script.
#
set -euo pipefail
STAMP=/var/lib/ctrlable/.firstboot-done
[ -f "$STAMP" ] && exit 0
install -d /var/lib/ctrlable
log(){ echo "[ctrlable-firstboot] $*"; }

# Baked appliance config: PORTAL_URL, PROVISIONING_KEY, RELEASE, ORCHESTRATOR_IMAGE.
# Written at image-build / flash time (see image/config/orchestrator/appliance.conf.example).
CONF=/etc/ctrlable/appliance.conf
[ -f "$CONF" ] && . "$CONF" || log "WARN no $CONF — orchestrator will start unconfigured"
ORCHESTRATOR_IMAGE="${ORCHESTRATOR_IMAGE:-ctrlable-orchestrator}"
ORCH_NAME="ctrlable-orchestrator"

# 1. Host sysctl that HA-Supervised-in-LXC needs (the installer writes it; an unprivileged
#    container can't, so the host must — PoC finding, SPEC-incus-poc.md §3). The orchestrator
#    deploys Ctrlable Pro as Supervised-LXC, so the host must allow this before that happens.
sysctl -w kernel.dmesg_restrict=0 || true
grep -q '^kernel.dmesg_restrict' /etc/sysctl.conf 2>/dev/null || echo 'kernel.dmesg_restrict=0' >> /etc/sysctl.conf

# 2. Incus + the persistent `data` btrfs pool on its OWN partition (never an OS slot).
export DATA_POOL=data DATA_SOURCE=/dev/disk/by-partlabel/data
/opt/ctrlable/incus-bootstrap.sh stage1

# 3. Launch the orchestrator container, which self-enrols + deploys the manifest.
if incus info "$ORCH_NAME" >/dev/null 2>&1; then
  log "orchestrator already present; ensuring it is running"
  incus start "$ORCH_NAME" 2>/dev/null || true
else
  # Prefer a baked image tarball (offline-capable factory flash); fall back to the alias
  # (set at build time) or an orchestrator-published image.
  baked="/opt/ctrlable/images/${ORCHESTRATOR_IMAGE}.tar.zst"
  if [ -f "$baked" ] && ! incus image alias list 2>/dev/null | grep -q "$ORCHESTRATOR_IMAGE"; then
    log "import baked orchestrator image $baked"
    incus image import "$baked" --alias "$ORCHESTRATOR_IMAGE"
  fi
  if incus image alias list 2>/dev/null | grep -q "$ORCHESTRATOR_IMAGE"; then
    log "launch $ORCH_NAME from $ORCHESTRATOR_IMAGE"
    incus launch "$ORCHESTRATOR_IMAGE" "$ORCH_NAME"
  else
    # TODO(orchestrator image): publish ctrlable-orchestrator as an Incus image and bake or
    # pull it here. Until then first-boot stops at a ready host + data pool; the orchestrator
    # can be deployed manually. This is the one remaining gap to full zero-touch.
    log "WARN orchestrator image '$ORCHESTRATOR_IMAGE' not available — host is ready; orchestrator not launched"
    rauc status mark-good booted || true
    touch "$STAMP"; exit 0
  fi
  sleep 3

  # Hand the orchestrator the host Incus API so it can drive the host (deploy the fleet),
  # per SPEC §4 (Provisioner -> Incus REST API -> host). A proxy device bridges the host
  # unix socket into the container.
  incus config device add "$ORCH_NAME" incus-sock proxy \
      connect=unix:/var/lib/incus/unix.socket \
      listen=unix:/run/incus/host.socket bind=container 2>/dev/null || \
      log "WARN could not add incus-sock proxy device"

  # Inject enrol config so the orchestrator's platform.py can self-enrol with the portal.
  incus exec "$ORCH_NAME" -- mkdir -p /etc/ctrlable
  incus exec "$ORCH_NAME" -- bash -c "cat > /etc/ctrlable/.env" <<ENV
provider=incus
incus_remote=
provisioning_key=${PROVISIONING_KEY:-}
portal_url=${PORTAL_URL:-https://portal.ctrlable.com}
release=${RELEASE:-}
ENV
  incus exec "$ORCH_NAME" -- systemctl restart ctrlable-provisioner 2>/dev/null || \
      log "note: orchestrator service restart skipped (image may autostart it)"
fi

log "orchestrator up; it now self-enrols + deploys the manifest (platform.py/deploy.py)"

# 4. Mark this freshly-flashed OS slot good (resets the GRUB boot-try flag).
rauc status mark-good booted || true

touch "$STAMP"
log "first boot complete"
