#!/usr/bin/env bash
#
# pack-image.sh — assemble the flashable GPT A/B image from $WORK/rootfs.
#   esp (FAT32) + root-a (populated) + root-b (empty) + data (clean, no FS; pool made at first boot).
# Run as root on a Debian 13 build host (needs sgdisk, losetup, grub-efi-amd64-bin).
#
set -euo pipefail
: "${WORK:=work}"; : "${VERSION:=$(date +%Y.%m.%d)}"
here="$(cd "$(dirname "$0")/.." && pwd)"
root="$WORK/rootfs"
img="$WORK/ctrlable-pro-$VERSION.img"

[ "$(id -u)" = 0 ] || { echo "pack-image: run as root"; exit 1; }
[ -d "$root" ] || { echo "pack-image: $root missing — run build-rootfs first"; exit 1; }
for t in sgdisk losetup mkfs.vfat mkfs.ext4 mkfs.btrfs; do command -v "$t" >/dev/null || { echo "pack-image: need $t"; exit 1; }; done

ESP_MB=512; SLOT_MB=4096; DATA_MB=8192           # DATA_MB = headroom for the PoC image; real disks grow it
IMG_MB=$((ESP_MB + SLOT_MB*2 + DATA_MB + 8))

echo "pack-image: GPT layout -> $img (${IMG_MB}M)"
rm -f "$img"; truncate -s ${IMG_MB}M "$img"
sgdisk -Z "$img" >/dev/null
sgdisk -n1:0:+${ESP_MB}M  -t1:ef00 -c1:esp    "$img" >/dev/null
sgdisk -n2:0:+${SLOT_MB}M -t2:8304 -c2:root-a "$img" >/dev/null
sgdisk -n3:0:+${SLOT_MB}M -t3:8304 -c3:root-b "$img" >/dev/null
sgdisk -n4:0:0            -t4:8300 -c4:data   "$img" >/dev/null

lo="$(losetup --show -fP "$img")"
trap 'mountpoint -q "${mnt:-/nonexistent}" && umount "$mnt"; losetup -d "$lo" 2>/dev/null || true' EXIT

echo "pack-image: filesystems"
mkfs.vfat  -n ESP          "${lo}p1" >/dev/null
mkfs.ext4  -F -L root-a -d "$root" "${lo}p2" >/dev/null   # slot A = the rootfs tree
mkfs.ext4  -F -L root-b       "${lo}p3" >/dev/null        # slot B empty; first OTA fills it
wipefs -a                     "${lo}p4" >/dev/null 2>&1 || true  # leave data partition CLEAN (no FS): firstboot/incus-bootstrap creates the btrfs 'data' pool on it. Pre-formatting here made `incus storage create` fail ("existing filesystem").

echo "pack-image: GRUB (EFI) + A/B grub.cfg + fresh grubenv on the ESP"
mnt="$(mktemp -d)"; mount "${lo}p1" "$mnt"
if command -v grub-install >/dev/null; then
  grub-install --target=x86_64-efi --efi-directory="$mnt" --boot-directory="$mnt" \
      --removable --no-nvram "$lo" || echo "pack-image: TODO grub-install failed (need grub-efi-amd64-bin)"
else
  echo "pack-image: TODO grub-install not present on build host"
fi
install -D -m0644 "$here/config/grub/grub.cfg.in" "$mnt/grub/grub.cfg"
grub-editenv "$mnt/grub/grubenv" create
grub-editenv "$mnt/grub/grubenv" set ORDER="A B" A_OK=1 B_OK=0 A_TRY=0 B_TRY=0
umount "$mnt"; rmdir "$mnt"; mnt=""

echo "pack-image: done -> $img"
echo "  flash:   dd if=$img of=/dev/sdX bs=4M status=progress conv=fsync"
echo "  or ship to the USB installer / netboot target (SPEC §P5 step 2)."
