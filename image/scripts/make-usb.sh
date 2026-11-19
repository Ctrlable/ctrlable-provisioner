#!/usr/bin/env bash
#
# make-usb.sh — build a bootable USB *installer* stick for the Ctrlable Pro appliance, for boxes
# that cannot (or should not) PXE/netboot. It is the SAME Alpine writer used by make-netboot.sh —
# it streams the REAL flashable image (the bit-identical artifact pack-image.sh produces) onto the
# box's internal disk, then reboots into the appliance whose own first-boot + VPN enrol take over.
# The only differences from netboot: GRUB (UEFI) boots the writer off the stick instead of iPXE,
# and the image is read from the stick (gzip, so Alpine's busybox `zcat` handles it with NO network
# and NO extra packages) instead of over HTTP.
#
# Output: $WORK/ctrlable-pro-usb-$VERSION.img  — write it to a USB stick with:
#     dd if=ctrlable-pro-usb-<ver>.img of=/dev/sdX bs=4M status=progress conv=fsync   (Linux/mac)
#   or balenaEtcher / Rufus ("DD/image" mode) on Windows/mac.
#
# UEFI only (modern mini-PCs). Secure Boot must be OFF (the Alpine writer kernel is unsigned).
#
#   usage:  make-usb.sh            # builds from $WORK/ctrlable-pro-$VERSION.img
#
set -euo pipefail
: "${WORK:=work}"
here="$(cd "$(dirname "$0")/.." && pwd)"          # image/ dir
VERSION="${VERSION:-$(date +%Y.%m.%d)}"
IMG="${IMG:-$WORK/ctrlable-pro-$VERSION.img}"
OUT="${OUT:-$WORK/ctrlable-pro-usb-$VERSION.img}"
UD="$WORK/usb"                                     # staging

ALPINE_VER="${ALPINE_VER:-3.21}"
ALPINE_ARCH="${ALPINE_ARCH:-x86_64}"
# lts = full bare-metal driver set (physical NVMe/AHCI/SATA + NICs). virt = VM-only and would not
# see a real mini-PC's internal disk. The writer needs no network (image is local) but DOES need
# the storage drivers that live in modloop-lts.
ALPINE_FLAVOR="${ALPINE_FLAVOR:-lts}"
ALPINE_MIRROR="${ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}"
ALPINE_NB="$ALPINE_MIRROR/v$ALPINE_VER/releases/$ALPINE_ARCH/netboot"
# Where the booted installer pulls Alpine's base packages from. Default: public mirror (the target
# box has LAN+internet). Override ALPINE_REPO_URL to a LAN mirror (e.g. the .84 netboot host) for a
# fully-local install. Only ~10 MB of base packages are fetched; the appliance image is read locally.
ALPINE_REPO_URL="${ALPINE_REPO_URL:-$ALPINE_MIRROR/v$ALPINE_VER/main}"
# USB_DEBUG=1 -> serial-primary + verbose init (for VM boot tests). Default -> screen-primary, quiet.
if [ -n "${USB_DEBUG:-}" ]; then KCON="console=tty0 console=ttyS0,115200 debug_init"; else KCON="quiet console=ttyS0,115200 console=tty0"; fi

LABEL="CTRLABLE"                                   # FAT label GRUB + Alpine key off
log(){ echo "[usb] $*"; }

[ -f "$IMG" ] || { echo "[usb] image not found: $IMG  (run 'make image' first)"; exit 1; }
for t in sgdisk mkfs.vfat mcopy mmd grub-mkstandalone; do
  command -v "$t" >/dev/null || { echo "[usb] missing tool: $t"; exit 1; }
done
rm -rf "$UD"; mkdir -p "$UD/alpine" "$UD/root/boot/grub" "$UD/root/EFI/BOOT"

# 1. gzip the real image. busybox `zcat` on the writer decompresses it offline (no zstd/apk needed).
#    pigz if present (17G source), else gzip. The image is mostly zeros -> compresses hugely anyway.
gz="$UD/root/ctrlable-pro.img.gz"
gzcache="$WORK/.usb-cache/ctrlable-pro-$VERSION.img.gz"; mkdir -p "$WORK/.usb-cache"
if command -v pigz >/dev/null; then GZ="pigz -6"; else GZ="gzip -6"; fi
if [ -s "$gzcache" ] && [ "$gzcache" -nt "$IMG" ]; then
  log "reusing cached gzip ($(du -h "$gzcache" | cut -f1))"; cp "$gzcache" "$gz"
else
  log "compressing $IMG -> ctrlable-pro.img.gz with ${GZ% *} (slow part)"
  $GZ -c "$IMG" > "$gz"; cp "$gz" "$gzcache"
fi
log "image: $(du -h "$gz" | cut -f1) compressed (from $(du -h "$IMG" | cut -f1))"

# 2. Alpine netboot kernel/initramfs/modloop (cached under $UD/alpine across runs is lost on rm; keep
#    a sibling cache in $WORK/.alpine-cache so repeated builds don't re-download).
cache="$WORK/.alpine-cache"; mkdir -p "$cache"
for f in vmlinuz-$ALPINE_FLAVOR initramfs-$ALPINE_FLAVOR modloop-$ALPINE_FLAVOR; do
  if [ ! -s "$cache/$f" ]; then log "fetch $ALPINE_NB/$f"; curl -fL# "$ALPINE_NB/$f" -o "$cache/$f"; fi
  cp "$cache/$f" "$UD/root/boot/$f"
done

# 3. The OFFLINE writer overlay (apkovl). Auto-discovered by Alpine's nlplug (it is *.apkovl.tar.gz
#    at the medium root). Fully self-contained: finds the image + target disk locally, EXCLUDES the
#    USB stick it booted from, shows the target with a countdown, then zcat|dd and reboots.
ov="$(mktemp -d)"
mkdir -p "$ov/etc/local.d" "$ov/etc/runlevels/default" "$ov/etc/apk"
# diskless Alpine builds its RAM root by `apk add`-ing this world set. alpine-base pulls busybox +
# openrc + /sbin/init so the system actually boots; the packages come from the repo given on the
# kernel cmdline (local LAN mirror if reachable, else the public mirror). The big appliance image is
# still read LOCALLY off the stick — only Alpine's ~10 MB base is fetched.
printf 'alpine-base\n' > "$ov/etc/apk/world"
cat > "$ov/etc/local.d/00-ctrlable-write.start" <<'WRITER'
#!/bin/sh
# Ctrlable Pro USB installer — writes the appliance image to the box's INTERNAL disk.
exec >/dev/console 2>&1
echo; echo "================ Ctrlable Pro appliance installer ================"
# Kernel modules for the INTERNAL target (NVMe/SATA/SCSI/eMMC) live in modloop-lts on the stick.
# Alpine's modloop service doesn't mount it in this minimal setup, so do it ourselves: find the
# squashfs on the boot medium, loop-mount it, and expose its modules at /lib/modules. Without this
# there are NO modules (/lib/modules is absent) and the target disk is invisible.
if [ ! -d "/lib/modules/$(uname -r)" ]; then
  modprobe squashfs 2>/dev/null; modprobe loop 2>/dev/null
  for b in /media/*/boot /mnt/*/boot /media/* /mnt/*; do
    ML="$b/modloop-lts"; [ -f "$ML" ] || continue
    mkdir -p /.modloop
    if mount -o loop,ro -t squashfs "$ML" /.modloop 2>/dev/null && [ -d /.modloop/modules ]; then
      mkdir -p /lib/modules
      mount --bind /.modloop/modules /lib/modules 2>/dev/null || cp -a /.modloop/modules/* /lib/modules/ 2>/dev/null
      echo "modloop mounted from $ML"; break
    fi
  done
fi
# Now load the target's storage/bus drivers, repopulate /dev, and let it settle.
modprobe -a nvme nvme_core ahci libahci ata_piix ata_generic sd_mod sr_mod scsi_mod \
            virtio_pci virtio_blk virtio_scsi mmc_block sdhci sdhci_pci \
            xhci_pci xhci_hcd ehci_pci uas 2>/dev/null
mdev -s 2>/dev/null
sleep 2
# Locate the stick (the medium carrying our image) and the disk it lives on (to EXCLUDE as target).
SRC=""; for m in /media/* ; do [ -f "$m/ctrlable-pro.img.gz" ] && SRC="$m" && break; done
if [ -z "$SRC" ]; then
  # not auto-mounted? scan+mount vfat/ext parts looking for the image
  for p in $(awk 'NR>2{print $4}' /proc/partitions); do
    case "$p" in loop*|ram*|sr*|dm-*) continue;; esac
    mkdir -p "/mnt/$p" 2>/dev/null
    mount -o ro "/dev/$p" "/mnt/$p" 2>/dev/null || continue
    if [ -f "/mnt/$p/ctrlable-pro.img.gz" ]; then SRC="/mnt/$p"; break; fi
    umount "/mnt/$p" 2>/dev/null
  done
fi
if [ -z "$SRC" ]; then echo "FATAL: ctrlable-pro.img.gz not found on the USB medium"; echo "Dropping to a shell."; exec /bin/sh; fi
echo "installer image: $SRC/ctrlable-pro.img.gz"
SRCPART=$(df -P "$SRC/ctrlable-pro.img.gz" 2>/dev/null | tail -1 | awk '{print $1}')   # e.g. /dev/sdb1
SRCDISK=$(echo "$SRCPART" | sed -E 's#^/dev/##; s#p?[0-9]+$##')                         # e.g. sdb
echo "booted from: $SRCPART  (disk $SRCDISK — will NOT be touched)"
# Pick the largest whole disk >8GiB that is NOT the USB stick. (#blocks in /proc/partitions = KiB.)
# Retry for ~20s: NVMe/SCSI can enumerate a beat after the drivers load.
TARGET=""; m=0; try=0
while [ "$try" -lt 10 ]; do
  TARGET=""; m=0
  for line in $(awk 'NR>2{print $4":"$3}' /proc/partitions); do
    n=${line%:*}; b=${line#*:}
    case "$n" in loop*|ram*|sr*|fd*|dm-*) continue;; esac
    # whole disks only: drop partitions (sdX[0-9], nvmeXnYpZ)
    case "$n" in
      nvme*n*p[0-9]*) continue;;
      *[0-9]) case "$n" in nvme*n[0-9]) : ;; *) continue;; esac;;
    esac
    [ "$n" = "$SRCDISK" ] && continue
    if [ "$b" -gt 8388608 ] && [ "$b" -gt "$m" ]; then m=$b; TARGET="$n"; fi
  done
  [ -n "$TARGET" ] && break
  modprobe -a nvme ahci virtio_scsi sd_mod 2>/dev/null; mdev -s 2>/dev/null; sleep 2; try=$((try+1))
done
if [ -z "$TARGET" ]; then echo "FATAL: no internal target disk found (>8GiB, not the USB)"; echo "Disks:"; awk 'NR>2{print " "$4" "$3"KiB"}' /proc/partitions; exec /bin/sh; fi
GB=$(( m / 1048576 ))
echo
echo "  TARGET DISK : /dev/$TARGET  (${GB} GiB)  <-- WILL BE COMPLETELY ERASED"
echo
printf "Installing to /dev/%s in 15s. Press any key to ABORT to a shell... " "$TARGET"
if read -t 15 -n 1 -r _ </dev/console 2>/dev/null; then echo; echo "Aborted by operator. Shell:"; exec /bin/sh; fi
echo; echo "Writing image -> /dev/$TARGET ..."
set -x
zcat "$SRC/ctrlable-pro.img.gz" | dd of="/dev/$TARGET" bs=4M 2>/tmp/dd.err
set +x
bytes=$(awk '/copied|bytes/{print $1; exit}' /tmp/dd.err)
cat /tmp/dd.err
if [ "${bytes:-0}" -gt 1000000000 ]; then
  sync; echo; echo "=== WRITE OK ($bytes bytes). Remove the USB stick; rebooting in 10s. ==="; sleep 10; reboot -f
else
  sync; echo; echo "=== WRITE FAILED (only ${bytes:-0} bytes). Shell: ==="; exec /bin/sh
fi
WRITER
chmod +x "$ov/etc/local.d/00-ctrlable-write.start"
ln -sf /etc/init.d/local "$ov/etc/runlevels/default/local"
tar -C "$ov" -czf "$UD/root/ctrlable-writer.apkovl.tar.gz" etc
rm -rf "$ov"
log "apkovl: ctrlable-writer.apkovl.tar.gz"

# 4. Mark the stick as Alpine boot media so nlplug keeps it mounted (modloop lives at boot/modloop-*).
: > "$UD/root/.boot_repository"

# 5. GRUB menu on the stick (editable without rebuilding the EFI binary).
cat > "$UD/root/boot/grub/grub.cfg" <<GRUBCFG
set timeout=10
set default=0
insmod all_video
insmod gzio
search --no-floppy --label $LABEL --set=root
menuentry "Install Ctrlable Pro appliance  (ERASES the internal disk)" {
    echo "Loading Ctrlable Pro installer..."
    linux (\$root)/boot/vmlinuz-$ALPINE_FLAVOR modules=loop,squashfs,sd-mod,usb-storage,nvme,ahci,ata_piix,virtio_blk,virtio_scsi,ext4,vfat ip=dhcp alpine_repo=$ALPINE_REPO_URL $KCON
    initrd (\$root)/boot/initramfs-$ALPINE_FLAVOR
}
menuentry "Boot from the first internal disk (do not install)" {
    insmod chain
    insmod part_gpt
    search --no-floppy --file --set=hdroot /EFI/BOOT/BOOTX64.EFI --hint hd0
    exit
}
GRUBCFG

# 6. GRUB EFI binary (removable path EFI/BOOT/BOOTX64.EFI -> boots with no NVRAM entry, like the
#    appliance). Its embedded cfg just finds the stick and hands off to the editable on-disk cfg.
emb="$(mktemp)"
cat > "$emb" <<EMB
search --no-floppy --label $LABEL --set=root
set prefix=(\$root)/boot/grub
configfile (\$root)/boot/grub/grub.cfg
EMB
grub-mkstandalone -O x86_64-efi -o "$UD/root/EFI/BOOT/BOOTX64.EFI" \
  --modules="part_gpt part_msdos fat ext2 normal linux echo all_video test true search search_label search_fs_uuid probe gzio configfile loadenv chain" \
  "boot/grub/grub.cfg=$emb"
rm -f "$emb"
log "grub: EFI/BOOT/BOOTX64.EFI + boot/grub/grub.cfg"

# 7. Assemble the GPT disk image with one EFI-System FAT32 partition, populated via mtools (no loop).
part_bytes=$(du -sb "$UD/root" | cut -f1)
part_bytes=$(( part_bytes + 64*1024*1024 ))                      # slack for FAT overhead
part_mib=$(( (part_bytes + 1048575) / 1048576 + 16 ))            # round up + margin
part_sectors=$(( part_mib * 2048 ))
start=2048                                                       # 1MiB align
disk_sectors=$(( start + part_sectors + 2048 ))                 # + backup GPT room
log "partition ${part_mib} MiB; disk $(( disk_sectors/2048 )) MiB"

absUD="$(cd "$UD" && pwd)"            # absolute: the mcopy block cd's into $UD/root
part="$absUD/part.img"
rm -f "$part"; truncate -s $(( part_sectors * 512 )) "$part"
mkfs.vfat -F32 -n "$LABEL" "$part" >/dev/null
# create dirs then copy the whole tree
( cd "$absUD/root"
  find . -type d ! -name . | sed 's#^\./##' | while read -r d; do mmd -i "$part" "::/$d" 2>/dev/null || true; done
  find . -type f | sed 's#^\./##' | while read -r f; do mcopy -i "$part" -o "$f" "::/$f"; done
)

rm -f "$OUT"; truncate -s $(( disk_sectors * 512 )) "$OUT"
sgdisk -o "$OUT" >/dev/null
sgdisk -n 1:${start}:$(( start + part_sectors - 1 )) -t 1:EF00 -c 1:"$LABEL" "$OUT" >/dev/null
dd if="$part" of="$OUT" bs=512 seek=$start conv=notrunc status=none
rm -f "$part"

log "DONE -> $OUT  ($(du -h "$OUT" | cut -f1))"
cat <<DONE

Write it to a USB stick (UEFI boot, Secure Boot OFF), then boot the mini-PC from it:
  Linux/macOS:  sudo dd if=$OUT of=/dev/sdX bs=4M status=progress conv=fsync
  Windows:      balenaEtcher or Rufus in DD / image mode
The installer shows the detected internal target disk and a 15s abort window, then
writes the image and reboots into the appliance (unenrolled; enrol from the orchestrator UI).
DONE
