#!/usr/bin/env bash
# Ctrlable Provisioner builder — DALI → MQTT bridge appliance.
#
# Invoked by `ctrlable-build` for the manifest template whose builder is
# `ctrlable/dali-bridge.sh`. Unlike community-scripts, this is bundled on the
# build host (synced to $CTRLABLE_BUILDERS_DIR by install.sh).
#
# It delegates to the bridge's own, tested provisioner
# (dali-bridge/deploy/provision-dali-lxc.sh): create a privileged CT with the
# pre-allocated VMID, install python-dali + the bridge + the admin web UI, set up
# hasseb USB pass-through, and lay down a config.yaml with a PLACEHOLDER broker.
# The real MQTT settings are written on first boot by ctrlable-firstboot's
# wire_dali(); a plugged-in master is bound via `dali-usb-refresh <vmid>` on site.
#
# Env (from ctrlable-build): VMID, CT_HOSTNAME, var_container_storage.
# DALI_BRIDGE_SRC — path to the dali-bridge source on the build host
#   (placed there by the release process; defaults below).
set -euo pipefail

VMID="${VMID:?ctrlable-build must export VMID}"
CT_HOSTNAME="${CT_HOSTNAME:-dali-bridge}"
STORAGE="${var_container_storage:-local-lvm}"
SRC="${DALI_BRIDGE_SRC:-/opt/ctrlable/dali-bridge-src}"

[ -x "$SRC/deploy/provision-dali-lxc.sh" ] || {
  echo "dali-bridge builder: bridge source not found at $SRC (set DALI_BRIDGE_SRC)" >&2
  exit 1
}

# Placeholder MQTT (127.0.0.1) satisfies the provisioner; wire_dali() overwrites
# it on first boot with the site broker. The bridge retries MQTT harmlessly until
# then. USB pass-through writes the cgroup allow now; bind mounts appear once a
# master is present + `dali-usb-refresh $VMID` runs.
exec "$SRC/deploy/provision-dali-lxc.sh" \
  --vmid "$VMID" \
  --hostname "$CT_HOSTNAME" \
  --storage "$STORAGE" \
  --mqtt-host 127.0.0.1 \
  --bridge-src "$SRC"
