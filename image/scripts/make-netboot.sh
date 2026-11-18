#!/usr/bin/env bash
#
# make-netboot.sh — stage a network-install of the Ctrlable Pro appliance: a custom
# netboot.xyz / iPXE entry that boots a tiny Alpine writer which streams our REAL flashable
# image (zstd-compressed) straight to the local disk, then reboots into the appliance. The
# appliance's own first-boot (incus-bootstrap + orchestrator) and the host VPN enrol then run
# unchanged — so one PXE boot takes a bare box all the way to a provisioned, VPN-reachable node.
#
# IMPORTANT (why dd the image, not a preseed installer): the A/B RAUC slot geometry, grubenv
# path and verity expectations are defined by pack-image.sh. We write the SAME artifact we flash
# by hand so the netboot box is bit-identical to a flashed one — never a separately-reconstructed
# layout. This doubles as the factory-provisioning / field-reimage mechanism.
#
#   usage:  make-netboot.sh <BASE_URL> [--serve PORT]
#     BASE_URL  the HTTP root these files will be served from, e.g. http://172.16.1.165:8080
#     --serve   after staging, serve $WORK/netboot on PORT (python3 http.server) for a live test
#
set -euo pipefail
: "${WORK:=work}"
here="$(cd "$(dirname "$0")/.." && pwd)"          # image/ dir
VERSION="${VERSION:-$(date +%Y.%m.%d)}"
IMG="${IMG:-$WORK/ctrlable-pro-$VERSION.img}"
ND="$WORK/netboot"

# Alpine netboot writer source (override ALPINE_VER / ALPINE_MIRROR for an air-gapped mirror).
ALPINE_VER="${ALPINE_VER:-3.21}"
ALPINE_ARCH="${ALPINE_ARCH:-x86_64}"
ALPINE_MIRROR="${ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}"
ALPINE_NB="$ALPINE_MIRROR/v$ALPINE_VER/releases/$ALPINE_ARCH/netboot"
ALPINE_REPO="$ALPINE_MIRROR/v$ALPINE_VER/main"

[ "${1:-}" ] || { grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'; exit 1; }
BASE="${1%/}"; shift
SERVE_PORT=""
[ "${1:-}" = "--serve" ] && SERVE_PORT="${2:?--serve needs a PORT}"

log(){ echo "[netboot] $*"; }
[ -f "$IMG" ] || { echo "[netboot] image not found: $IMG  (run 'make image' first)"; exit 1; }
mkdir -p "$ND/alpine"

# 1. Compress the real image. It is mostly zeros (empty B slot + unused slack), so even a low
#    level shrinks it hugely; --long improves the long-range zero runs. -T0 = all cores.
zimg="$ND/ctrlable-pro.img.zst"
if [ ! -f "$zimg" ] || [ "$IMG" -nt "$zimg" ]; then
  log "compressing $IMG -> $zimg (zstd, this is the slow part)"
  zstd -q -f -6 -T0 --long=27 "$IMG" -o "$zimg"
fi
log "image: $(du -h "$zimg" | cut -f1) compressed (from $(du -h "$IMG" | cut -f1))"

# 2. Fetch the Alpine netboot writer kernel/initramfs/modloop (cached).
for f in "vmlinuz-virt" "initramfs-virt" "modloop-virt"; do
  if [ ! -s "$ND/alpine/$f" ]; then
    log "fetch $ALPINE_NB/$f"
    curl -fL# "$ALPINE_NB/$f" -o "$ND/alpine/$f"
  fi
done

# 3. Build the Alpine apkovl overlay: an auto-run writer that streams the image to disk.
ov="$(mktemp -d)"
mkdir -p "$ov/etc/local.d" "$ov/etc/runlevels/default"
cat > "$ov/etc/local.d/00-ctrlable-write.start" <<WRITER
#!/bin/sh
# Ctrlable Pro netboot writer — streams the appliance image to the LARGEST local disk, then
# reboots INTO it only on a verified write (>1GB). A failed/short write halts (poweroff) so a
# blank box never loops. Progress is beaconed as HTTP GETs to the server (visible in its access
# log) so the install is observable even when the guest serial console is not captured.
exec >/dev/console 2>&1
BASE="$BASE"
IMGURL="\$BASE/ctrlable-pro.img.zst"
beacon(){ wget -q -O /dev/null "\$BASE/BEACON/\$1" 2>/dev/null || curl -fs -o /dev/null "\$BASE/BEACON/\$1" 2>/dev/null || true; }
beacon start
set -x
command -v zstd >/dev/null 2>&1 || { apk add --no-cache zstd >/dev/null 2>&1 || apk add zstd >/dev/null 2>&1; }
beacon "zstd_bin=\$(command -v zstd 2>/dev/null | tr '/' '_')__ver=\$(zstd --version 2>&1 | tr ' /,()' '______')"
if ! command -v zstd >/dev/null 2>&1; then beacon FATAL_no_zstd; sleep 5; poweroff -f; fi
# Probe virtio transports, then pick the largest whole disk >1GiB from /proc/partitions (the
# authoritative kernel view; #blocks is KiB). Retry: diskless virtio enumerates a beat late.
modprobe virtio_pci 2>/dev/null; modprobe virtio_blk 2>/dev/null; modprobe virtio_scsi 2>/dev/null; modprobe sd_mod 2>/dev/null
beacon "parts0=\$(sed -n '2,25p' /proc/partitions | tr -s ' ' '_' | tr '\n' '|' | cut -c1-200)"
beacon "mods=\$(lsmod 2>/dev/null | awk '/virtio|_blk|scsi|sd_mod/{print \$1}' | tr '\n' ',')__dev=\$(ls /dev 2>/dev/null | grep -E '^(vd|sd|nvme)' | tr '\n' ',')"
DISK=""; tries=0
while [ \$tries -lt 40 ]; do
  DISK=\$(awk 'NR>2{n=\$4;b=\$3; if(n~/^(loop|ram|sr|fd|dm-)/)next; if(n~/[0-9]\$/ && n!~/nvme[0-9]+n[0-9]+\$/)next; if(b+0>1048576 && b+0>m){m=b+0;best=n}} END{if(best)print "/dev/" best}' /proc/partitions)
  [ -n "\$DISK" ] && break
  modprobe virtio_blk 2>/dev/null; modprobe virtio_scsi 2>/dev/null; modprobe sd_mod 2>/dev/null
  sleep 1; tries=\$((tries+1))
done
beacon "disk=\$(echo \${DISK:-NONE} | tr '/' '_')__waited=\$tries"
if [ -z "\$DISK" ]; then beacon FATAL_no_target_disk; sleep 5; poweroff -f; fi
if command -v curl >/dev/null 2>&1; then DL="curl -fL \$IMGURL"; else DL="wget -O - \$IMGURL"; fi
beacon "dl=\$(echo \$DL | tr ' /:.' '____')"
# decompress smoke-test: pull a little, decompress, count bytes + capture any zstd error
\$DL 2>/dev/null | head -c 4000000 | zstd -dc --long=31 2>/tmp/z.err | wc -c > /tmp/z.cnt
beacon "smoke_out=\$(cat /tmp/z.cnt 2>/dev/null)__err=\$(cat /tmp/z.err 2>/dev/null | tr ' \n/,()' '______' | cut -c1-90)"
# real write
\$DL | zstd -dc --long=31 | dd of="\$DISK" bs=4M 2>/tmp/dd.err
bytes=\$(awk '/copied|bytes/{print \$1; exit}' /tmp/dd.err)
beacon "write_bytes=\${bytes:-0}__dderr=\$(cat /tmp/dd.err 2>/dev/null | tr ' \n/,()' '______' | cut -c1-90)"
if [ "\${bytes:-0}" -gt 1000000000 ]; then
  sync; beacon RESULT_OK; echo "WRITE OK"; sleep 5; reboot -f
else
  sync; beacon RESULT_FAIL; echo "WRITE FAILED"; sleep 5; poweroff -f
fi
WRITER
chmod +x "$ov/etc/local.d/00-ctrlable-write.start"
# enable the 'local' service so local.d scripts run at boot
ln -sf /etc/init.d/local "$ov/etc/runlevels/default/local"
tar -C "$ov" -czf "$ND/ctrlable-writer.apkovl.tar.gz" etc
rm -rf "$ov"
log "apkovl: $ND/ctrlable-writer.apkovl.tar.gz"

# 4. Render the iPXE boot script (served at $BASE/boot.ipxe).
cat > "$ND/boot.ipxe" <<IPXE
#!ipxe
# Ctrlable Pro network installer — writes the real appliance image to disk.
set base $BASE
echo
echo Ctrlable Pro network install — writing image to local disk
echo base: \${base}
echo
kernel \${base}/alpine/vmlinuz-virt initrd=initramfs-virt modules=loop,squashfs,sd-mod,usb-storage,virtio_blk,virtio_net \\
  alpine_repo=$ALPINE_REPO modloop=\${base}/alpine/modloop-virt apkovl=\${base}/ctrlable-writer.apkovl.tar.gz \\
  ip=dhcp console=tty0 console=ttyS0,115200
initrd \${base}/alpine/initramfs-virt
boot
IPXE
log "ipxe: $ND/boot.ipxe"

# 5. A drop-in custom menu entry for an existing netboot.xyz instance (local menus).
cat > "$ND/ctrlable-menu.ipxe" <<MENU
#!ipxe
# Drop into netboot.xyz local custom menus and :start_custom to list it.
menu Ctrlable
item ctrlable_install  Install Ctrlable Pro appliance (write image to disk)
choose target && goto \${target}
:ctrlable_install
chain --autofree $BASE/boot.ipxe
MENU
log "netboot.xyz custom entry: $ND/ctrlable-menu.ipxe"

cat <<DONE

staged under $ND :
  boot.ipxe                    iPXE entry (chainload target)
  ctrlable-menu.ipxe           netboot.xyz custom-menu snippet
  ctrlable-pro.img.zst         the real appliance image, compressed
  ctrlable-writer.apkovl.tar.gz Alpine auto-writer overlay
  alpine/                      Alpine netboot kernel/initramfs/modloop

Incus UEFI test VM (dnsmasq hands iPXE our boot.ipxe):
  incus network set <bridge> raw.dnsmasq - <<'DNS'
  dhcp-match=set:ipxe,175
  dhcp-boot=tag:ipxe,$BASE/boot.ipxe
  DNS
  incus init --empty --vm nbtest -c limits.memory=2GiB -d root,size=20GiB -n <bridge>
  incus config set nbtest security.secureboot=false
  incus start nbtest --console=vga
DONE

if [ -n "$SERVE_PORT" ]; then
  command -v python3 >/dev/null || { echo "[netboot] python3 needed for --serve"; exit 1; }
  log "serving $ND on :$SERVE_PORT  (Ctrl-C to stop)"
  cd "$ND" && exec python3 -m http.server "$SERVE_PORT"
fi
