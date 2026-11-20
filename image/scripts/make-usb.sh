#!/usr/bin/env bash
#
# make-usb.sh — build a bootable USB *installer* stick for the Ctrlable Pro appliance, for boxes
# that cannot (or should not) PXE/netboot. FULLY OFFLINE, all-Debian: GRUB (UEFI) boots the
# appliance's OWN Debian kernel with a purpose-built initramfs whose /init is our writer. It runs
# entirely from RAM — no network, no clock, no apk, no openrc, no modloop — finds the appliance
# image on the stick, streams it (gzip, busybox zcat) to the largest internal disk, and reboots.
#
# Why this design (vs the earlier Alpine-diskless approach): Alpine's diskless boot builds its RAM
# root by apk-add over the network (needs DHCP + repo + correct clock + valid TLS), its modloop
# service was flaky in our layout, and apk wipes untracked files from /etc/local.d (deleting the
# writer). A minimal Debian initramfs with busybox has none of those dependencies.
#
# Output: $WORK/ctrlable-pro-usb-$VERSION.img — write it to a USB stick with:
#     dd if=ctrlable-pro-usb-<ver>.img of=/dev/sdX bs=4M status=progress conv=fsync   (Linux/mac)
#   or balenaEtcher / Rufus ("DD/image" mode) on Windows/mac.
# UEFI only (modern mini-PCs). Secure Boot must be OFF (the kernel/initramfs are unsigned).
#
#   usage:  make-usb.sh            # needs $WORK/rootfs (make rootfs) + $WORK/ctrlable-pro-$VERSION.img (make image)
#   env:    USB_DEBUG=1            # serial-primary console + verbose, for VM boot tests
#
set -euo pipefail
: "${WORK:=work}"
here="$(cd "$(dirname "$0")/.." && pwd)"          # image/ dir
repo="$(cd "$here/.." && pwd)"                     # provisioner repo root
VERSION="${VERSION:-$(date +%Y.%m.%d)}"
IMG="${IMG:-$WORK/ctrlable-pro-$VERSION.img}"
OUT="${OUT:-$WORK/ctrlable-pro-usb-$VERSION.img}"
UD="$WORK/usb"                                     # staging
ROOTFS="${ROOTFS:-$WORK/rootfs}"                   # the appliance Debian rootfs (kernel + modules)
LABEL="CTRLABLE"                                   # FAT label GRUB keys off
# USB_DEBUG=1 -> serial-primary + verbose (VM tests). Default -> screen (tty0) primary.
if [ -n "${USB_DEBUG:-}" ]; then KCON="console=tty0 console=ttyS0,115200 ctrlable_debug=1"; else KCON="console=ttyS0,115200 console=tty0"; fi
log(){ echo "[usb] $*"; }

[ -f "$IMG" ]        || { echo "[usb] image not found: $IMG  (run 'make image' first)"; exit 1; }
[ -d "$ROOTFS/lib/modules" ] || { echo "[usb] rootfs not found: $ROOTFS  (run 'make rootfs' first)"; exit 1; }
for t in sgdisk mkfs.vfat mcopy mmd grub-mkstandalone busybox depmod xz cpio; do
  command -v "$t" >/dev/null || { echo "[usb] missing tool: $t (apt: busybox-static kmod xz-utils cpio gdisk dosfstools mtools grub2-common grub-efi-amd64-bin)"; exit 1; }
done

KVER="$(ls "$ROOTFS/lib/modules" | head -1)"
VMLINUZ="$ROOTFS/boot/vmlinuz-$KVER"
[ -f "$VMLINUZ" ] || VMLINUZ="$(ls "$ROOTFS/boot/vmlinuz-"* 2>/dev/null | head -1)"
[ -f "$VMLINUZ" ] || { echo "[usb] no kernel in $ROOTFS/boot"; exit 1; }
log "kernel $KVER  ($(basename "$VMLINUZ"))"

rm -rf "$UD"; mkdir -p "$UD/root/boot/grub" "$UD/root/EFI/BOOT"

# 1. gzip the appliance image (busybox zcat decompresses it offline). Cache it across rebuilds.
gz="$UD/root/ctrlable-pro.img.gz"
gzcache="$WORK/.usb-cache/ctrlable-pro-$VERSION.img.gz"; mkdir -p "$WORK/.usb-cache"
if command -v pigz >/dev/null; then GZ="pigz -6"; else GZ="gzip -6"; fi
if [ -s "$gzcache" ] && [ "$gzcache" -nt "$IMG" ]; then
  log "reusing cached gzip ($(du -h "$gzcache" | cut -f1))"; cp "$gzcache" "$gz"
else
  log "compressing $IMG -> ctrlable-pro.img.gz with ${GZ% *} (slow part)"
  $GZ -c "$IMG" > "$gz"; cp "$gz" "$gzcache"
fi

# 2. Build the installer initramfs: busybox + /init (the writer) + the appliance's kernel modules
#    (decompressed so busybox can insmod them). Runs as PID 1, fully in RAM.
ID="$UD/initramfs"
rm -rf "$ID"; mkdir -p "$ID"/{bin,sbin,proc,sys,dev,run,mnt,media,tmp,etc,lib/modules}
cp "$(command -v busybox)" "$ID/bin/busybox"
log "modules -> initramfs (copy + decompress .ko.xz so busybox can load them)"
cp -a "$ROOTFS/lib/modules/$KVER" "$ID/lib/modules/$KVER"
find "$ID/lib/modules/$KVER" -name '*.ko.xz'  -exec xz -d {} + 2>/dev/null || true
find "$ID/lib/modules/$KVER" -name '*.ko.zst' -exec zstd -d --rm {} + 2>/dev/null || true
find "$ID/lib/modules/$KVER" -name '*.ko.gz'  -exec gunzip {} + 2>/dev/null || true
depmod -b "$ID" "$KVER"

cat > "$ID/init" <<INIT
#!/bin/busybox sh
# Ctrlable Pro appliance installer — runs as PID 1 in the initramfs. Writes the appliance image
# (local, gzip) to the box's INTERNAL disk, then reboots. No network, no clock, no services.
/bin/busybox --install -s /bin 2>/dev/null
mount -t proc     proc     /proc  2>/dev/null
mount -t sysfs    sysfs    /sys   2>/dev/null
mount -t devtmpfs devtmpfs /dev   2>/dev/null
mount -t tmpfs    tmpfs    /run   2>/dev/null
DEBUG="\$(sed -n 's/.*ctrlable_debug=\([0-9]\).*/\1/p' /proc/cmdline 2>/dev/null)"
exec >/dev/console 2>&1
echo; echo "================ Ctrlable Pro appliance installer ================"
# Load storage/USB/filesystem drivers (from the modules baked into this initramfs).
modprobe -a nvme nvme_core ahci libahci ata_piix ata_generic libata sd_mod sr_mod scsi_mod \\
            usb-common usbcore xhci_hcd xhci_pci ehci_hcd ehci_pci ohci_hcd uas usb-storage \\
            virtio virtio_pci virtio_ring virtio_blk virtio_scsi \\
            mmc_core mmc_block sdhci sdhci_pci sdhci_acpi \\
            vfat fat nls_cp437 nls_iso8859-1 nls_ascii 2>/dev/null
mdev -s 2>/dev/null
sleep 3
[ "\$DEBUG" = 1 ] && { echo "--- /proc/partitions ---"; cat /proc/partitions; }

# Locate the stick (the medium carrying our image) and the disk it lives on (to EXCLUDE as target).
SRC=""
for p in \$(awk 'NR>2{print \$4}' /proc/partitions); do
  case "\$p" in loop*|ram*|sr*|dm-*|md*) continue;; esac
  mkdir -p "/mnt/\$p" 2>/dev/null
  mount -t vfat -o ro "/dev/\$p" "/mnt/\$p" 2>/dev/null || mount -o ro "/dev/\$p" "/mnt/\$p" 2>/dev/null || continue
  if [ -f "/mnt/\$p/ctrlable-pro.img.gz" ]; then SRC="/mnt/\$p"; break; fi
  umount "/mnt/\$p" 2>/dev/null
done
if [ -z "\$SRC" ]; then echo "FATAL: ctrlable-pro.img.gz not found on any medium"; echo "Disks:"; cat /proc/partitions; echo "Recovery shell:"; exec /bin/busybox sh; fi
echo "installer image: \$SRC/ctrlable-pro.img.gz"
SRCPART=\$(df "\$SRC/ctrlable-pro.img.gz" 2>/dev/null | tail -1 | awk '{print \$1}')          # e.g. /dev/sdb1
SRCDISK=\$(echo "\$SRCPART" | sed -E 's#^/dev/##; s#p?[0-9]+\$##')                            # e.g. sdb
echo "booted from: \$SRCPART  (disk \$SRCDISK — will NOT be touched)"

# Pick the largest whole disk >8GiB that is NOT the USB stick (#blocks in /proc/partitions = KiB).
TARGET=""; m=0; try=0
while [ "\$try" -lt 8 ]; do
  TARGET=""; m=0
  for line in \$(awk 'NR>2{print \$4":"\$3}' /proc/partitions); do
    n=\${line%:*}; b=\${line#*:}
    case "\$n" in loop*|ram*|sr*|fd*|dm-*|md*) continue;; esac
    case "\$n" in
      nvme*n*p[0-9]*) continue;;
      *[0-9]) case "\$n" in nvme*n[0-9]) : ;; mmcblk[0-9]) : ;; *) continue;; esac;;
    esac
    [ "\$n" = "\$SRCDISK" ] && continue
    if [ "\$b" -gt 8388608 ] && [ "\$b" -gt "\$m" ]; then m=\$b; TARGET="\$n"; fi
  done
  [ -n "\$TARGET" ] && break
  mdev -s 2>/dev/null; sleep 2; try=\$((try+1))
done
if [ -z "\$TARGET" ]; then echo "FATAL: no internal target disk found (>8GiB, not the USB)"; echo "Disks:"; cat /proc/partitions; echo "Recovery shell:"; exec /bin/busybox sh; fi
GB=\$(( m / 1048576 ))
echo
echo "  TARGET DISK : /dev/\$TARGET  (\${GB} GiB)  <-- WILL BE COMPLETELY ERASED"
echo
printf "Installing to /dev/%s in 15s. Press ENTER to ABORT to a shell... " "\$TARGET"
if read -t 15 -r _ </dev/console 2>/dev/null; then echo; echo "Aborted by operator. Recovery shell:"; exec /bin/busybox sh; fi
echo; echo "Writing image -> /dev/\$TARGET  (streams ~16 GB, 1-3 min, no progress bar)..."
# zcat straight to the device: the shell redirect writes every decompressed byte, and zcat's exit
# status reflects the gzip CRC — exit 0 means a COMPLETE, intact image was written (far more reliable
# than parsing busybox dd's byte count, which has no "copied" line).
if zcat "\$SRC/ctrlable-pro.img.gz" > "/dev/\$TARGET" 2>/tmp/z.err; then
  sync; sync
  echo; echo "=== WRITE OK. Remove the USB stick; rebooting in 10s. ==="
  [ "\$DEBUG" = 1 ] && { echo "DEBUG: not rebooting, recovery shell:"; exec /bin/busybox sh; }
  sleep 10; reboot -f 2>/dev/null; echo b > /proc/sysrq-trigger
else
  sync
  echo; echo "=== WRITE FAILED: \$(cat /tmp/z.err 2>/dev/null) ==="; echo "Recovery shell:"; exec /bin/busybox sh
fi
INIT
chmod +x "$ID/init"

log "pack initramfs"
( cd "$ID" && find . | cpio -o -H newc 2>/dev/null | gzip -6 ) > "$UD/root/boot/initramfs-ctrlable.img"
cp "$VMLINUZ" "$UD/root/boot/vmlinuz"
log "initramfs $(du -h "$UD/root/boot/initramfs-ctrlable.img" | cut -f1); kernel $(du -h "$UD/root/boot/vmlinuz" | cut -f1)"

# 3. GRUB menu (editable without rebuilding the EFI binary). No root=, no network: the kernel runs
#    /init from the initramfs and our writer takes over.
cat > "$UD/root/boot/grub/grub.cfg" <<GRUBCFG
set timeout=10
set default=0
insmod all_video
insmod gzio
search --no-floppy --label $LABEL --set=root
menuentry "Install Ctrlable Pro appliance  (ERASES the internal disk)" {
    echo "Loading Ctrlable Pro installer..."
    linux (\$root)/boot/vmlinuz rdinit=/init $KCON
    initrd (\$root)/boot/initramfs-ctrlable.img
}
menuentry "Install Ctrlable Pro appliance  (verbose / debug)" {
    linux (\$root)/boot/vmlinuz rdinit=/init console=tty0 console=ttyS0,115200 ctrlable_debug=1
    initrd (\$root)/boot/initramfs-ctrlable.img
}
GRUBCFG

# 4. GRUB EFI (removable path, boots with no NVRAM entry). Embedded cfg hands off to the on-disk one.
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

# 5. Assemble the GPT disk image with one EFI-System FAT32 partition, populated via mtools (no loop).
part_bytes=$(du -sb "$UD/root" | cut -f1)
part_mib=$(( (part_bytes + 128*1024*1024) / 1048576 ))               # + slack for FAT overhead
part_sectors=$(( part_mib * 2048 ))
start=2048
disk_sectors=$(( start + part_sectors + 2048 ))
log "partition ${part_mib} MiB; disk $(( disk_sectors/2048 )) MiB"

absUD="$(cd "$UD" && pwd)"
part="$absUD/part.img"
rm -f "$part"; truncate -s $(( part_sectors * 512 )) "$part"
mkfs.vfat -F32 -n "$LABEL" "$part" >/dev/null
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
Fully offline: it needs NO network. It shows the detected internal target disk and a 15s abort
window, then writes the image and reboots into the appliance (unenrolled; enrol from the UI).
DONE
