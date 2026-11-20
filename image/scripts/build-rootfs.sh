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

# fstab: mount the shared ESP at /boot/efi so RAUC can write grubenv (the A/B slot flags) from
# userspace — `rauc status mark-good` edits /boot/efi/grub/grubenv. GRUB itself reads grubenv
# directly off the FAT (its own driver) so boot works without this, but mark-good / OTA-confirm
# does NOT. Root (/) is picked per-slot by the kernel cmdline (root=PARTLABEL=root-a|b) so it is
# deliberately NOT in fstab; the Incus `data` pool is mounted by data-orchestrator.mount. `nofail`
# = a missing ESP never blocks boot; the mount completes before ctrlable-mark-good (multi-user).
install -d -m0755 "$root/boot/efi"
cat > "$root/etc/fstab" <<'FSTAB'
# <device>       <mountpoint>  <type>  <options>                                        <dump> <pass>
PARTLABEL=esp    /boot/efi     vfat    umask=0077,nofail,x-systemd.device-timeout=5s    0      0
FSTAB

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

# Root CONSOLE password for field service. The crypt hash ($6$…) is injected at build time via
# ROOT_PWHASH (never committed). Without it the root account stays locked (SSH-key access only).
# Generate one with:  openssl passwd -6
if [ -n "${ROOT_PWHASH:-}" ]; then
  echo "build-rootfs: set root console password from ROOT_PWHASH"
  sed -i -E "s#^root:[^:]*:#root:${ROOT_PWHASH}:#" "$root/etc/shadow"
fi

# baked appliance config. The PROVISIONING_KEY is the portal's shared admission key
# (settings.ADDON_PROVISIONING_KEY) — it only gates /v1/auto-enroll; the portal still mints a UNIQUE
# per-appliance identity (device_id, token, tunnel IP, WG keypair, claim code), deduped on NIC MAC.
# Injected at build time via env (never committed); without it the box ships unenrolled (placeholder).
if [ -n "${PROVISIONING_KEY:-}" ]; then
  echo "build-rootfs: bake appliance.conf with PROVISIONING_KEY (zero-touch auto-enrol)"
  install -D -m0600 /dev/stdin "$root/etc/ctrlable/appliance.conf" <<CONF
# Baked appliance config — read by ctrlable-firstboot.sh + ctrlable-host-enroll.sh for zero-touch
# enrolment. PROVISIONING_KEY is the portal's shared admission key; identity is minted per-box.
PORTAL_URL="${PORTAL_URL:-https://portal.ctrlable.com}"
PROVISIONING_KEY="${PROVISIONING_KEY}"
RELEASE="${RELEASE:-}"
ORCHESTRATOR_IMAGE="${ORCHESTRATOR_IMAGE:-ctrlable-orchestrator}"
CONF
else
  echo "build-rootfs: WARN no PROVISIONING_KEY env -> appliance ships UNENROLLED (placeholder appliance.conf)"
  install -D -m0600 "$here/config/orchestrator/appliance.conf.example" "$root/etc/ctrlable/appliance.conf"
fi

# RAUC keyring = the cert baked in to VERIFY every future OTA bundle (the PERMANENT trust anchor;
# changing it later means reflashing the box). Dev = the self-signed cert; prod = the CA cert, so
# the signer (a leaf) can rotate without touching fielded boxes. Selected by RAUC_KEYRING (Makefile);
# resolve it relative to cwd (make runs in image/) then fall back to the legacy dev cert path.
KR="${RAUC_KEYRING:-}"
# RAUC_KEYRING may be given relative to cwd (image/, where make runs) — also try under $here.
if [ -n "$KR" ] && [ ! -f "$KR" ] && [ -f "$here/$KR" ]; then KR="$here/$KR"; fi
# Fall back to the legacy dev cert when RAUC_KEYRING is unset or points nowhere.
if { [ -z "$KR" ] || [ ! -f "$KR" ]; } && [ -f "$here/keys/dev-cert.pem" ]; then
  KR="$here/keys/dev-cert.pem"
fi
if [ -n "$KR" ] && [ -f "$KR" ]; then
  echo "build-rootfs: RAUC keyring <- $KR"
  install -D -m0644 "$KR" "$root/etc/rauc/keyring.pem"
else
  echo "build-rootfs: WARN no RAUC keyring (RAUC_KEYRING unset + no keys/dev-cert.pem) -> /etc/rauc/keyring.pem missing (run 'make keys' or 'make prod-ca')"
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

# Install the orchestrator INTO the host rootfs (host mode is the appliance default; firstboot
# selects it). The container/LXC path (image/builders/build-orchestrator.sh) is separate and
# unchanged for Proxmox + other appliances.
"$here/builders/install-orchestrator-host.sh" "$root" "$repo"

echo "build-rootfs: enable services + apply host branding"
# Enable units OFFLINE with `systemctl --root=` (writes the [Install] symlinks against the image
# tree, no running systemd needed). Do NOT use `chroot "$root" systemctl enable`: in an offline
# mmdebstrap chroot there is no running PID 1 (and often no /proc), so it silently fails — and the
# old `|| true` hid that, shipping an image with systemd-networkd AND ctrlable-firstboot DISABLED
# while ifupdown's networking.service (pulled in as a dependency) won, so the box booted with NO
# network and NO firstboot. Enable each unit individually so a D-Bus-activated/absent unit (rauc)
# can't abort the rest, then mask ifupdown so it never fights the systemd-networkd br-lan bridge.
for u in systemd-networkd systemd-networkd.socket systemd-resolved ssh incus nginx \
         ctrlable-firstboot ctrlable-host-enroll ctrlable-mark-good ctrlable-orchestrator \
         data-orchestrator.mount; do
  systemctl --root="$root" enable "$u" >/dev/null 2>&1 || echo "build-rootfs: WARN could not enable $u"
done
systemctl --root="$root" mask networking.service >/dev/null 2>&1 || true
# branding: issue/motd/ssh banner get written; sshd reload + update-grub no-op in a chroot
# (our hand-authored A/B grub.cfg from pack-image.sh is authoritative; GRUB_DISTRIBUTOR is cosmetic).
chroot "$root" /opt/ctrlable/apply-branding.sh || true

echo "build-rootfs: done -> $root"
