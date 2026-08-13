#!/usr/bin/env bash
# Ctrlable Provisioner builder — Ctrlable Hardware Manager.
#
# Invoked by `ctrlable-build` for the manifest template whose builder is
# `ctrlable/hardware-manager.sh`. Bundled on the build host (synced to
# /opt/ctrlable/builders by install.sh), not a community-script.
#
# It delegates to the manager's own provisioner
# (deploy/provision-manager-lxc.sh), which creates the CT, installs the panel
# and the optional announcement service into it, and installs the read-only
# agent on the PVE host.
#
# Env (from ctrlable-build): VMID, CT_HOSTNAME, var_container_storage.
# HW_MANAGER_SRC — path to the source on the build host (staged by the release
#   process; cloned here if absent).
#
# ── This builder touches the HOST, which the others do not ───────────────────
#
# Every other template here produces a self-contained container. This one also
# installs a systemd unit on the hypervisor, because the agent reads USB
# topology, ALSA cards and PulseAudio -- none of which exist inside a container.
# That is worth knowing before running it on an appliance you did not intend to
# change: it is additive and read-only, but it is not confined to the CT.
set -euo pipefail

VMID="${VMID:?ctrlable-build must export VMID}"
CT_HOSTNAME="${CT_HOSTNAME:-ctrlable-hardware-manager}"
STORAGE="${var_container_storage:-local-lvm}"
SRC="${HW_MANAGER_SRC:-/opt/ctrlable/hardware-manager-src}"
REPO="${HW_MANAGER_REPO:-https://github.com/Ctrlable/ctrlable-hardware-manager-src.git}"
REF="${HW_MANAGER_REF:-main}"
KEY="${HW_MANAGER_SSH_KEY:-/etc/ctrlable/hardware-manager-deploy-key}"
TOKEN="${HW_MANAGER_TOKEN:-}"

# Announcement wiring. Left EMPTY on purpose.
#
# The announce service needs an announcement snapserver to talk to, and most
# appliances do not have one -- only a site running the Ctrlable audio stack
# does. Installing a service whose only possible behaviour is failure would put
# a red panel on a healthy appliance, so the provisioner skips it unless a
# snapserver is named. ctrlable-firstboot wires it on site, the same way
# wire_dali() does for the DALI bridge.
SNAPCAST_HOST="${HW_MANAGER_SNAPCAST_HOST:-}"
SNAPCAST_PORT="${HW_MANAGER_SNAPCAST_PORT:-1715}"

# Fetch the source (pinned) if it isn't already staged. Private repo, so prefer
# the read-only deploy key and fall back to an HTTPS token -- same pattern as
# dali-bridge.sh.
if [ ! -x "$SRC/deploy/provision-manager-lxc.sh" ]; then
  rm -rf "$SRC"
  echo "hardware-manager builder: cloning $REPO @ $REF -> $SRC"
  if [ -f "$KEY" ]; then
    ssh_url="$REPO"
    case "$REPO" in https://github.com/*) ssh_url="git@github.com:${REPO#https://github.com/}";; esac
    GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
      git clone --depth 1 --branch "$REF" "$ssh_url" "$SRC"
  elif [ -n "$TOKEN" ]; then
    git clone --depth 1 --branch "$REF" "https://x-access-token:${TOKEN}@${REPO#https://}" "$SRC"
  else
    echo "hardware-manager builder: no deploy key at $KEY and no HW_MANAGER_TOKEN" >&2
    exit 1
  fi
  [ -f "$SRC/deploy/provision-manager-lxc.sh" ] || {
    echo "hardware-manager builder: clone failed" >&2; exit 1; }
fi
chmod +x "$SRC/deploy/provision-manager-lxc.sh"

args=(
  --vmid "$VMID"
  --hostname "$CT_HOSTNAME"
  --storage "$STORAGE"
  --src "$SRC"
)
if [ -n "$SNAPCAST_HOST" ]; then
  args+=(--snapcast-host "$SNAPCAST_HOST" --snapcast-port "$SNAPCAST_PORT")
else
  args+=(--no-announce)
fi

# exec so the provisioner's [PHASE] and [RESULT] lines reach ctrlable-build
# unbuffered and its exit status is ours.
exec "$SRC/deploy/provision-manager-lxc.sh" "${args[@]}"
