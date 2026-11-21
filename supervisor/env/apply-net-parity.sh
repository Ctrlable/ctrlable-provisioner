#!/usr/bin/env bash
# apply-net-parity.sh — Layer-0 connectivity parity for Ctrlable Pro in an LXC.
# VALIDATED 2026-11-18 on ctrlable-pro-1: hands eth0 to NetworkManager (the HAOS
# networking model) so a STOCK Supervisor reports host_internet/supervisor_internet
# true and add-ons install — with ZERO Supervisor fork. Confirmed end-to-end:
# core_mosquitto installed + started on stock Supervisor after this ran.
#
# Key lesson: networkd and NM must NOT both touch eth0 (the fight breaks DHCP and
# NM then reports connectivity "limited"). So we take networkd fully OUT for the
# NIC and let NM alone do DHCP. Idempotent; rolls back to networkd if egress breaks.
set -euo pipefail
log(){ echo "[net-parity] $*"; }
IFACE="${IFACE:-eth0}"
CON="${CON:-${IFACE}-dhcp}"

restore_networkd(){
  log "ROLLBACK: restoring systemd-networkd DHCP on $IFACE"
  systemctl disable --now NetworkManager 2>/dev/null || true
  rm -f /etc/NetworkManager/conf.d/10-globally-managed-devices.conf
  cat > /etc/systemd/network/10-${IFACE}.network <<NET
[Match]
Name=${IFACE}
[Network]
DHCP=yes
NET
  systemctl unmask systemd-networkd 2>/dev/null || true
  systemctl enable --now systemd-networkd 2>/dev/null || true
  systemctl restart systemd-networkd; sleep 4
  networkctl reconfigure "$IFACE" 2>/dev/null || true; sleep 3
}

command -v nmcli >/dev/null || { export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq network-manager; }

# 1. NM manages everything (HAOS model); networkd must not claim the NIC.
install -D -m0644 "$(dirname "$0")/10-globally-managed-devices.conf" \
    /etc/NetworkManager/conf.d/10-globally-managed-devices.conf
rm -f "/etc/systemd/network/10-${IFACE}.network"
# stop + MASK networkd so it never fights NM over the veth on reboot
systemctl stop systemd-networkd 2>/dev/null || true
systemctl disable systemd-networkd 2>/dev/null || true
systemctl mask systemd-networkd.service systemd-networkd.socket 2>/dev/null || true

# 2. NM alone does DHCP on the NIC, via a persistent connection profile.
systemctl enable --now NetworkManager
systemctl restart NetworkManager; sleep 3
nmcli dev set "$IFACE" managed yes || true
nmcli con add type ethernet ifname "$IFACE" con-name "$CON" 2>/dev/null || true
nmcli con up "$CON" 2>/dev/null || nmcli dev connect "$IFACE" 2>/dev/null || true
sleep 7

# 3. Verify: full connectivity + real egress, else roll back to networkd.
conn="$(nmcli networking connectivity 2>/dev/null || echo unknown)"
if [ "$conn" = full ] && getent hosts github.com >/dev/null 2>&1; then
  log "OK: $IFACE under NetworkManager, connectivity=$conn, egress works"
  nmcli -t -f DEVICE,STATE,CONNECTION dev | grep "^$IFACE" || true
  exit 0
fi
log "FAIL: connectivity=$conn / egress down under NM"
restore_networkd
getent hosts github.com >/dev/null 2>&1 && log "egress restored via networkd" || log "WARN still no egress"
exit 1
