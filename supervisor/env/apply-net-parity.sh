#!/usr/bin/env bash
# apply-net-parity.sh — run INSIDE the Ctrlable Pro container to hand eth0 to
# NetworkManager (Layer-0 connectivity parity). Idempotent. Keeps a rollback:
# if NM can't bring eth0 up with egress, it restores the networkd config and exits
# non-zero so the caller (bootstrap) can decide to fall back to the C1 patch.
set -euo pipefail
log(){ echo "[net-parity] $*"; }
IFACE="${IFACE:-eth0}"
NETWORKD_FILE="/etc/systemd/network/10-${IFACE}-internal.network"   # bootstrap's file
BACKUP="/var/lib/ctrlable/net-parity.bak"; install -d /var/lib/ctrlable

command -v nmcli >/dev/null || { export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq network-manager; }
install -D -m0644 "$(dirname "$0")/10-globally-managed-devices.conf" \
    /etc/NetworkManager/conf.d/10-globally-managed-devices.conf

# move networkd's claim on the iface out of the way (reversible)
if ls /etc/systemd/network/*.network >/dev/null 2>&1; then
  mkdir -p "$BACKUP"; cp -a /etc/systemd/network/*.network "$BACKUP/" 2>/dev/null || true
  grep -l "Name=${IFACE}\|Name=eth\*" /etc/systemd/network/*.network 2>/dev/null | while read -r f; do
    log "disable networkd claim $f"; mv "$f" "$f.disabled"
  done
fi
systemctl enable --now NetworkManager
systemctl restart systemd-networkd 2>/dev/null || true
nmcli dev set "$IFACE" managed yes || true
nmcli networking on || true
nmcli dev connect "$IFACE" 2>/dev/null || nmcli con up "$IFACE" 2>/dev/null || true

# verify egress; roll back if we broke it
for i in $(seq 1 10); do getent hosts github.com >/dev/null 2>&1 && ok=1 && break; sleep 2; done
if [ "${ok:-0}" = 1 ]; then
  log "OK: $IFACE under NetworkManager with egress"
  nmcli -t -f DEVICE,STATE,MANAGED dev | grep "^$IFACE" || true
  exit 0
fi
log "FAIL: no egress under NM — restoring networkd and leaving connectivity to C1 patch"
for f in /etc/systemd/network/*.network.disabled; do [ -f "$f" ] && mv "$f" "${f%.disabled}"; done
systemctl restart systemd-networkd || true
exit 1
