#!/usr/bin/env bash
#
# build-rootfs.sh — build the Ctrlable Pro HOST rootfs tree with mmdebstrap, then bake in our
# identity, services, first-boot logic, orchestrator key, and incus-bootstrap. Output: $WORK/rootfs.
# Run as root on a Debian 13 build host.
#
set -euo pipefail
: "${WORK:=work}"; : "${SUITE:=trixie}"
here="$(cd "$(dirname "$0")/.." && pwd)"   # the image/ dir
repo="$(cd "$here/.." && pwd)"             # provisioner repo root
root="$WORK/rootfs"

[ "$(id -u)" = 0 ] || { echo "build-rootfs: run as root"; exit 1; }
command -v mmdebstrap >/dev/null || { echo "build-rootfs: install mmdebstrap"; exit 1; }

pkgs="$(grep -vE '^\s*#|^\s*$' "$here/config/mmdebstrap/packages" | paste -sd, -)"

echo "build-rootfs: mmdebstrap $SUITE -> $root"
rm -rf "$root"; mkdir -p "$root"
mmdebstrap --variant=important \
  --components="main contrib non-free-firmware" \
  --include="$pkgs" \
  "$SUITE" "$root" \
  "http://deb.debian.org/debian"

echo "build-rootfs: bake Ctrlable payload"
install -D -m0755 "$repo/poc/incus-bootstrap.sh"           "$root/opt/ctrlable/incus-bootstrap.sh"
install -D -m0755 "$repo/poc/branding/apply-branding.sh"   "$root/opt/ctrlable/apply-branding.sh"
install -D -m0755 "$here/config/first-boot/ctrlable-firstboot.sh" "$root/opt/ctrlable/ctrlable-firstboot.sh"
install -D -m0644 "$here/config/first-boot/ctrlable-firstboot.service" "$root/etc/systemd/system/ctrlable-firstboot.service"
install -D -m0644 "$here/config/first-boot/ctrlable-mark-good.service" "$root/etc/systemd/system/ctrlable-mark-good.service"
install -D -m0755 "$here/config/first-boot/ctrlable-host-enroll.sh"      "$root/opt/ctrlable/ctrlable-host-enroll.sh"
install -D -m0644 "$here/config/first-boot/ctrlable-host-enroll.service" "$root/etc/systemd/system/ctrlable-host-enroll.service"
# dealer claim code display (CLI + login banner)
install -D -m0755 "$here/config/first-boot/ctrlable-claim"               "$root/usr/local/bin/ctrlable-claim"
install -D -m0644 "$here/config/first-boot/ctrlable-claim-profile.sh"    "$root/etc/profile.d/ctrlable-claim.sh"
# host reverse-proxy front door (always-up entry -> orchestrator container :8000)
install -D -m0644 "$here/config/nginx/ctrlable.conf"  "$root/etc/nginx/conf.d/ctrlable.conf"
install -D -m0644 "$here/config/nginx/starting.html"  "$root/usr/share/ctrlable/www/starting.html"
rm -f "$root/etc/nginx/sites-enabled/default"
install -D -m0644 "$here/config/rauc/system.conf"          "$root/etc/rauc/system.conf"

# trust anchors in /root/.ssh/authorized_keys:
#   - orchestrator automation key (orchestrator -> host; zero-touch deploy)
#   - ops/support key (humans + portal Wetty gateway -> host over the VPN; troubleshooting backdoor)
#   - optional OPS_PUBKEY injected at build time (dev / boot-test images)
install -d -m0700 "$root/root/.ssh"
: > "$root/root/.ssh/authorized_keys"
cat "$here/config/orchestrator/authorized_keys" >> "$root/root/.ssh/authorized_keys"
[ -f "$here/config/orchestrator/ops_authorized_keys" ] && cat "$here/config/orchestrator/ops_authorized_keys" >> "$root/root/.ssh/authorized_keys"
[ -n "${OPS_PUBKEY:-}" ] && printf "%s\n" "$OPS_PUBKEY" >> "$root/root/.ssh/authorized_keys"
chmod 600 "$root/root/.ssh/authorized_keys"

# baked appliance config (placeholders here; CI/flash injects real PROVISIONING_KEY etc.)
install -D -m0600 "$here/config/orchestrator/appliance.conf.example" "$root/etc/ctrlable/appliance.conf"

# RAUC keyring = the cert used to VERIFY bundles (dev cert if built; prod cert supplied in CI)
if [ -f "$here/keys/dev-cert.pem" ]; then
  install -D -m0644 "$here/keys/dev-cert.pem" "$root/etc/rauc/keyring.pem"
else
  echo "build-rootfs: WARN no keys/dev-cert.pem -> /etc/rauc/keyring.pem missing (run 'make keys' or supply prod cert)"
fi

# LAN bridge (the Proxmox vmbr0 equivalent): enslave the physical NIC to br-lan so the fleet's
# bridged NICs are first-class on the SITE LAN — instances pull 172.16.x from LAN DHCP instead of
# sitting behind incusbr0 NAT (Ctrlable Pro needs LAN visibility for mDNS/SSDP/HomeKit/Matter +
# direct device access). The HOST itself DHCPs on the bridge. STP/forward-delay off: a single-uplink
# appliance, so skip the ~30 s listening delay before carrier. The bridge inherits the enslaved
# NIC's MAC (kernel default) → stable DHCP lease.
install -D -m0644 /dev/stdin "$root/etc/systemd/network/05-br-lan.netdev" <<'NETDEV'
[NetDev]
Name=br-lan
Kind=bridge
[Bridge]
STP=false
ForwardDelaySec=0
NETDEV
# physical NIC: no IP of its own — it is a bridge port
install -D -m0644 /dev/stdin "$root/etc/systemd/network/10-wan.network" <<'NET'
[Match]
Name=en* eth*
[Network]
Bridge=br-lan
NET
# the bridge carries the host's LAN address (DHCP)
install -D -m0644 /dev/stdin "$root/etc/systemd/network/20-br-lan.network" <<'NET'
[Match]
Name=br-lan
[Network]
DHCP=yes
LinkLocalAddressing=ipv6
[Link]
RequiredForOnline=routable
NET

echo "build-rootfs: enable services + apply host branding (in chroot)"
chroot "$root" systemctl enable systemd-networkd systemd-resolved ssh incus rauc nginx \
    ctrlable-firstboot ctrlable-host-enroll ctrlable-mark-good 2>/dev/null || true
# branding: issue/motd/ssh banner get written; sshd reload + update-grub no-op in a chroot
# (our hand-authored A/B grub.cfg from pack-image.sh is authoritative; GRUB_DISTRIBUTOR is cosmetic).
chroot "$root" /opt/ctrlable/apply-branding.sh || true

echo "build-rootfs: done -> $root"
