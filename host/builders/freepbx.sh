#!/usr/bin/env bash
# Ctrlable Provisioner builder — FreePBX 17 (Sangoma's Debian installer) in an LXC.
#
# Invoked by `ctrlable-build` for the manifest template whose builder is
# `ctrlable/freepbx.sh` (bundled on the build host by install.sh).
#
# Replaces ct/freepbx.sh, which runs Sangoma's sng_freepbx_debian_install.sh
# from master, all-or-nothing: on ctrlable-pve (Pentium N3700, 2026-11-03)
# FreePBX 17 itself installed fine, then the OPTIONAL bulk module refresh
# (`fwconsole ma upgradeall`, ~100 modules with a full permissions pass after
# each) hit FreePBX's hardcoded 750 s PHP limit inside Chown.class.php, the
# installer exited 255, and build.func's error handler deleted the container.
#
# Here:
#   - the Sangoma installer is pinned to a commit (SNG_REF), not master;
#   - the bulk module upgrade is non-fatal inside it, and retried on its own
#     afterwards -- FreePBX runs on the modules it installed with;
#   - a failed build leaves the container in place for inspection.
#
# Env (from ctrlable-build): VMID, CT_HOSTNAME, var_container_storage,
# var_template_storage, var_cpu, var_ram, var_disk, var_unprivileged.
# SNG_REF — override the pinned installer commit.
set -euo pipefail

VMID="${VMID:?ctrlable-build must export VMID}"
CT_HOSTNAME="${CT_HOSTNAME:-freepbx}"
STORAGE="${var_container_storage:-local-lvm}"
TSTORAGE="${var_template_storage:-local}"
CORES="${var_cpu:-2}"
RAM="${var_ram:-2048}"
DISK="${var_disk:-16}"
UNPRIV="${var_unprivileged:-1}"
BRIDGE="${CTRLABLE_BRIDGE:-vmbr0}"
SNG_REF="${SNG_REF:-b023b8426786}"   # FreePBX/sng_freepbx_debian_install, SCRIPTVER 1.15
SNG_URL="https://raw.githubusercontent.com/FreePBX/sng_freepbx_debian_install/${SNG_REF}/sng_freepbx_debian_install.sh"

log() { echo "freepbx builder: $*"; }
retry() {
  local n=1
  until "$@"; do
    (( n >= 5 )) && return 1
    sleep $((n * 5)); n=$((n + 1))
  done
}

# Sangoma's installer targets Debian 12 (bookworm).
pveam update >/dev/null 2>&1 || true
TEMPLATE=$(pveam list "$TSTORAGE" 2>/dev/null | awk '/debian-12-standard/{print $1}' | sort -V | tail -1)
if [[ -z "$TEMPLATE" ]]; then
  name=$(pveam available --section system | awk '/debian-12-standard/{print $2}' | sort -V | tail -1)
  [[ -n "$name" ]] || { log "no Debian 12 template available"; exit 1; }
  pveam download "$TSTORAGE" "$name"
  TEMPLATE="${TSTORAGE}:vztmpl/${name}"
fi

log "creating CT $VMID ($CT_HOSTNAME) from $TEMPLATE on $STORAGE"
pct create "$VMID" "$TEMPLATE" \
  --hostname "$CT_HOSTNAME" \
  --cores "$CORES" --memory "$RAM" --swap 512 \
  --rootfs "${STORAGE}:${DISK}" \
  --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
  --unprivileged "$UNPRIV" \
  --features nesting=1 \
  --onboot 1 \
  --start 1

log "waiting for network in CT"
for _ in $(seq 1 60); do
  pct exec "$VMID" -- bash -c 'getent hosts deb.debian.org >/dev/null' 2>/dev/null && break
  sleep 2
done

log "preparing the container"
retry pct exec "$VMID" -- bash -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates python3 >/dev/null
'

log "fetching Sangoma installer @ ${SNG_REF}"
retry pct exec "$VMID" -- curl -fsSL "$SNG_URL" -o /opt/sng_freepbx_debian_install.sh
# The bulk module refresh must not decide whether the PBX exists.
pct exec "$VMID" -- sed -i \
  's|^\(\s*\)fwconsole ma upgradeall >> "\$log"$|\1fwconsole ma upgradeall >> "$log" \|\| message "module upgrade incomplete -- retried by the Ctrlable builder"|' \
  /opt/sng_freepbx_debian_install.sh
pct exec "$VMID" -- grep -q 'retried by the Ctrlable builder' /opt/sng_freepbx_debian_install.sh \
  || { log "installer layout changed at ${SNG_REF}: upgradeall line not found"; exit 1; }

log "running the Sangoma installer (FreePBX 17 + Asterisk; slow -- 30-90 min on small hosts)"
# --skipversion: don't let the installer replace itself with master mid-run.
if ! pct exec "$VMID" -- bash -c 'cd /opt && bash sng_freepbx_debian_install.sh --skipversion'; then
  log "Sangoma installer failed -- container $VMID kept for inspection (/var/log/pbx/)"
  exit 1
fi

log "refreshing modules (non-fatal)"
for attempt in 1 2; do
  if pct exec "$VMID" -- bash -c 'fwconsole ma upgradeall >/var/log/pbx/ctrlable-upgradeall.log 2>&1'; then
    log "modules up to date"; break
  fi
  log "module refresh attempt $attempt incomplete (see /var/log/pbx/ctrlable-upgradeall.log)"
done
pct exec "$VMID" -- bash -c 'fwconsole chown >/dev/null 2>&1; fwconsole reload >/dev/null 2>&1' || true

pct exec "$VMID" -- bash -c 'systemctl is-active --quiet freepbx || fwconsole start >/dev/null 2>&1' || true
state=$(pct exec "$VMID" -- bash -c 'fwconsole -V 2>/dev/null | head -1; asterisk -rx "core show version" 2>/dev/null | head -1')
log "done (CT $VMID): ${state//$'\n'/ | }"
