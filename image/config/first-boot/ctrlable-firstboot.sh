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

# 3. Bring up the orchestrator. HOST mode (appliance default): run it as a NATIVE host service,
#    with all mutable state on the `data` partition so it survives A/B slot swaps while the code
#    stays in the immutable A/B root. CONTAINER mode (ORCHESTRATOR_MODE=container, e.g. Proxmox or
#    another appliance): launch it as an LXC — the legacy path, unchanged.
ORCHESTRATOR_MODE="${ORCHESTRATOR_MODE:-host}"
if [ "$ORCHESTRATOR_MODE" = host ]; then
  log "orchestrator: HOST mode (native service; state on /data/orchestrator, survives A/B)"
  # Dedicated btrfs subvolume on the data partition for orchestrator state (DB, .env, enrol/WG
  # keys), mounted at /data/orchestrator by data-orchestrator.mount (baked in both slots). The
  # subvolume lives alongside Incus's own subvolumes on the same `data` filesystem — create it once.
  if ! btrfs subvolume show /data/orchestrator >/dev/null 2>&1; then
    tmp=$(mktemp -d)
    if mount -o subvolid=5 /dev/disk/by-partlabel/data "$tmp" 2>/dev/null; then
      btrfs subvolume create "$tmp/orchestrator" 2>/dev/null || mkdir -p "$tmp/orchestrator"
      umount "$tmp"
    fi
    rmdir "$tmp" 2>/dev/null || true
  fi
  mkdir -p /data/orchestrator
  systemctl start data-orchestrator.mount 2>/dev/null || \
    mount -o subvol=orchestrator /dev/disk/by-partlabel/data /data/orchestrator 2>/dev/null || true
  # The code's backend/.env is a symlink to this file; write it once (jwt_secret persists so logins
  # survive restarts). platform.py reads it to self-enrol + fetch the release manifest.
  if [ ! -f /data/orchestrator/.env ]; then
    cat > /data/orchestrator/.env <<ENV
provider=incus
incus_remote=
db_path=/data/orchestrator/ctrlable.db
jwt_secret=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
provisioning_key=${PROVISIONING_KEY:-}
portal_url=${PORTAL_URL:-https://portal.ctrlable.com}
release=${RELEASE:-}
ENV
    chmod 600 /data/orchestrator/.env
  fi
  systemctl restart ctrlable-orchestrator 2>/dev/null || \
    log "note: ctrlable-orchestrator deferred (starts next boot once /data/orchestrator/.env exists)"
  log "orchestrator up as a host service on :8000; it self-enrols + deploys the manifest"

else
  # --- CONTAINER / LXC mode (Proxmox + other appliances; legacy path, unchanged) ---
  log "orchestrator: CONTAINER mode"
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
    # PRIVILEGED on purpose: the orchestrator is the host CONTROL PLANE. It already holds the host
    # Incus socket (below) — host-root-equivalent: it can create/configure/destroy every instance
    # and attach host hardware. Unprivileged adds no real isolation on top of that, while blocking
    # host-level reads (bind-mounting the host /proc for true host metrics). So run it privileged,
    # consistent with Ctrlable Pro itself. See SPEC §16.1.
    log "launch $ORCH_NAME from $ORCHESTRATOR_IMAGE (privileged host control plane)"
    incus launch "$ORCHESTRATOR_IMAGE" "$ORCH_NAME" -c security.privileged=true
  else
    # The orchestrator image is produced by image/builders/build-orchestrator.sh and baked at
    # /opt/ctrlable/images/<name>.tar.zst (or pulled). Validated 2026-11-17: launched this way it
    # drives the host Incus over the proxied socket and serves the API on :8000. If it is somehow
    # absent, stop at a ready host + data pool (orchestrator can then be deployed manually).
    log "WARN orchestrator image '$ORCHESTRATOR_IMAGE' not available — host is ready; orchestrator not launched"
    rauc status mark-good booted || true
    touch "$STAMP"; exit 0
  fi
  sleep 3

  # Hand the orchestrator the host Incus API so it can drive the host (deploy the fleet),
  # per SPEC §4 (Provisioner -> Incus REST API -> host). A proxy device bridges the host unix
  # socket into the container AT THE DEFAULT CLIENT PATH, so the orchestrator's `incus` client
  # (incus-client, baked into the image — no daemon in the container) just works unconfigured.
  incus config device add "$ORCH_NAME" incus-sock proxy \
      connect=unix:/var/lib/incus/unix.socket \
      listen=unix:/var/lib/incus/unix.socket bind=container 2>/dev/null || \
      log "WARN could not add incus-sock proxy device"

  # Expose the orchestrator SPA/API (:8000 in the container) on the HOST loopback so the host's
  # nginx front door can proxy to it (ws/SSE pass-through). bind=host → listen on the host.
  incus config device add "$ORCH_NAME" orch-http proxy \
      listen=tcp:127.0.0.1:8000 connect=tcp:127.0.0.1:8000 bind=host 2>/dev/null || \
      log "WARN could not add orch-http proxy device (host front door will show 'starting')"

  # Bind the host's /proc (read-only) into the orchestrator so its dashboard host card reflects
  # the PHYSICAL host, not the container (incus_provider.node_health reads /host/proc when present).
  # Works now that the orchestrator is privileged (an unprivileged container can't mount /proc).
  incus config device add "$ORCH_NAME" hostproc disk source=/proc path=/host/proc readonly=true 2>/dev/null \
      || log "note: could not bind host /proc into the orchestrator (host metrics will show the container)"
  # (The host's real NAME comes from the Incus API's environment.server_name, read over the socket
  # by node_health — /proc's hostname is namespace-virtualized, so no mount helps there.)

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
fi   # end ORCHESTRATOR_MODE branch

log "orchestrator up; it now self-enrols + deploys the manifest (platform.py/deploy.py)"

# 4. Mark this freshly-flashed OS slot good (resets the GRUB boot-try flag).
rauc status mark-good booted || true

touch "$STAMP"
log "first boot complete"
