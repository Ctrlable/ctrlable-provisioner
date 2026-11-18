#!/usr/bin/env bash
#
# make-bundle.sh — wrap $WORK/rootfs into a signed RAUC OTA bundle ($WORK/*.raucb).
# This is what ships to the fleet for every update after the first flash.
# Needs rauc + mkfs.ext4 on the build host, and signing keys.
#
set -euo pipefail
: "${WORK:=work}"; : "${COMPATIBLE:=ctrlable-pro-x86-64}"; : "${VERSION:=$(date +%Y.%m.%d)}"
: "${RAUC_CERT:=keys/dev-cert.pem}"; : "${RAUC_KEY:=keys/dev-key.pem}"
here="$(cd "$(dirname "$0")/.." && pwd)"
root="$WORK/rootfs"
out="$WORK/ctrlable-pro-$VERSION.raucb"

command -v rauc >/dev/null || { echo "make-bundle: install rauc on the build host"; exit 1; }
[ -d "$root" ] || { echo "make-bundle: $root missing — run build-rootfs first"; exit 1; }
[ -f "$RAUC_CERT" ] && [ -f "$RAUC_KEY" ] || {
  echo "make-bundle: need signing keys — 'make keys' (dev) or set RAUC_CERT/RAUC_KEY (prod secrets)"; exit 1; }

echo "make-bundle: rootfs tree -> ext4 slot image"
fs="$WORK/rootfs.ext4"; rm -f "$fs"
szm=$(du -sm "$root" | cut -f1); szm=$((szm + szm/5 + 256))     # +20% +256M headroom
truncate -s ${szm}M "$fs"
mkfs.ext4 -F -L root -d "$root" "$fs" >/dev/null

echo "make-bundle: manifest + sign (verity format)"
bd="$WORK/bundle"; rm -rf "$bd"; mkdir -p "$bd"
cp "$fs" "$bd/rootfs.ext4"
cat > "$bd/manifest.raucm" <<MAN
[update]
compatible=$COMPATIBLE
version=$VERSION

[bundle]
format=verity

[image.rootfs]
filename=rootfs.ext4
MAN

rm -f "$out"
rauc bundle --cert="$RAUC_CERT" --key="$RAUC_KEY" "$bd" "$out"
echo "make-bundle: done -> $out"
echo "  install on a running appliance:  rauc install $out   (writes inactive slot, flips GRUB, reboot)"
