#!/usr/bin/env bash
#
# ctrlable-firstboot.sh — zero-touch provisioning, runs ONCE on first boot of a flashed appliance.
# No human input: the orchestrator key is already baked into /root/.ssh/authorized_keys and Incus
# is pre-installed, so this just brings up the persistent pool + containers and calls home.
#
set -euo pipefail
STAMP=/var/lib/ctrlable/.firstboot-done
[ -f "$STAMP" ] && exit 0
install -d /var/lib/ctrlable
log(){ echo "[ctrlable-firstboot] $*"; }

# 1. Host sysctl that HA-Supervised-in-LXC needs (the installer writes it; an unprivileged
#    container can't, so the host must — PoC finding, SPEC-incus-poc.md §3).
sysctl -w kernel.dmesg_restrict=0 || true
grep -q '^kernel.dmesg_restrict' /etc/sysctl.conf 2>/dev/null || echo 'kernel.dmesg_restrict=0' >> /etc/sysctl.conf

# 2. Incus + the persistent `data` btrfs pool on its OWN partition (NOT a loop file, NOT an OS
#    slot). This is the invariant that lets OS slots be rolled without touching appliance state.
export DATA_POOL=data
export DATA_SOURCE=/dev/disk/by-partlabel/data
/opt/ctrlable/incus-bootstrap.sh stage1

# 3. Register with the orchestrator and pull this appliance's manifest, then launch its
#    containers (Ctrlable Pro Supervised-LXC + services) from the published images.
#    TODO(orchestrator): implement call-home + manifest apply. Reuse the ORCHESTRATOR_URL
#    auto-detect (commit 363ee62) and `incus image import` of the orchestrator-published images.
#    For now stage1 (Incus + data pool) is the verified part; partA/partB run against published
#    images in production rather than building on-box.
log "TODO: orchestrator call-home + manifest apply (containers launched from published images)"

# 4. Tell RAUC/GRUB this freshly-flashed slot booted cleanly (resets the boot-try flag).
rauc status mark-good booted || true

touch "$STAMP"
log "first boot complete"
