#!/usr/bin/env bash
#
# incus-bootstrap.sh — Ctrlable appliance bootstrap (Incus PoC → fleet first-boot seed)
#
# WHAT THIS IS
#   The automated provisioning recipe for a Ctrlable appliance on plain Debian + Incus,
#   per SPEC-incus-migration.md (decisions) and SPEC-incus-poc.md (runbook). It is written
#   to run NON-INTERACTIVELY as root on the target box — the same logic the RAUC-for-host
#   image will run from a first-boot systemd unit (§14b). There are NO hand-typed steps:
#   every value is derived or defaulted, so it is safe to bake into an image.
#
#   On the throwaway PoC box it is driven over the jump chain as root; in production the
#   image bakes the orchestrator key + pre-installs Incus and this runs on first boot.
#
# DESIGN RULES (learned the hard way during the PoC — do not regress these)
#   * Idempotent: every stage guards on existing state so re-running is safe.
#   * Interface names <= 15 chars (Linux limit) — `incusbr-int`, never `incusbr-internal`.
#   * `incus storage create` and friends read a YAML config from stdin when stdin is not a
#     tty; redirect `</dev/null` so they never slurp a surrounding script. (Bit us when the
#     script was piped to `bash -s` over ssh.)
#   * The data pool is a SEPARATE btrfs filesystem from the OS — the non-negotiable invariant
#     (§14a). On this PoC box it is file-backed; in the image it is the dedicated `data`
#     partition that survives A/B OS-slot swaps.
#
# USAGE
#   incus-bootstrap.sh [stage1|partA|partB|all]   (default: all)
#
set -euo pipefail

# ---- tunables (image bakes these; PoC uses defaults) -----------------------
DATA_POOL="${DATA_POOL:-data}"
DATA_SIZE="${DATA_SIZE:-30GiB}"            # PoC: file-backed loop size; image: unset (real partition)
DATA_SOURCE="${DATA_SOURCE:-}"             # image: /dev/<data-partition>; PoC empty => loop file
INT_NET="${INT_NET:-incusbr-int}"          # internal backplane bridge (<=15 chars!)
INT_CIDR="${INT_CIDR:-198.18.0.1/24}"
ORCH_URL="${ORCH_URL:-http://198.18.0.1:8000}"
BASE_IMG="${BASE_IMG:-images:debian/12}"

log(){ echo "[$(date +%H:%M:%S)] $*"; }

# ---- stage 1: Incus + btrfs data pool --------------------------------------
stage1(){
  log "stage1: install incus + btrfs-progs"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq incus btrfs-progs >/dev/null
  log "stage1: incus $(incus version | tr '\n' ' ')"

  if incus profile show default >/dev/null 2>&1 && incus network show incusbr0 >/dev/null 2>&1; then
    log "stage1: incus already initialized"
  else
    incus admin init --minimal </dev/null
  fi

  if incus storage show "$DATA_POOL" >/dev/null 2>&1; then
    log "stage1: pool '$DATA_POOL' exists"
  elif [ -n "$DATA_SOURCE" ]; then
    log "stage1: create btrfs pool '$DATA_POOL' on $DATA_SOURCE (dedicated partition)"
    incus storage create "$DATA_POOL" btrfs source="$DATA_SOURCE" </dev/null
  else
    log "stage1: create file-backed btrfs pool '$DATA_POOL' ($DATA_SIZE) [PoC stand-in]"
    incus storage create "$DATA_POOL" btrfs size="$DATA_SIZE" </dev/null
  fi

  # new instances land on the data pool, never the OS slot
  incus profile device set default root pool="$DATA_POOL" </dev/null 2>/dev/null \
    || incus profile device add default root disk path=/ pool="$DATA_POOL" </dev/null
  log "stage1: default root pool = $(incus profile device get default root pool)"
}

# ---- internal backplane network --------------------------------------------
ensure_int_net(){
  if incus network show "$INT_NET" >/dev/null 2>&1; then
    log "net: $INT_NET exists"
  else
    log "net: create $INT_NET ($INT_CIDR, no NAT, dhcp)"
    incus network create "$INT_NET" \
      ipv4.address="$INT_CIDR" ipv4.nat=false ipv4.dhcp=true \
      ipv4.dhcp.ranges=198.18.0.100-198.18.0.200 ipv6.address=none </dev/null
  fi
}

# ---- Part A: service container (zwavejs) -----------------------------------
# A1: build a distro-agnostic service image (the recipe we own instead of community-scripts).
build_zwavejs_image(){
  if incus image show ctrlable-zwavejs >/dev/null 2>&1; then
    log "partA: image ctrlable-zwavejs exists"; return 0
  fi
  incus delete -f zwavejs-build 2>/dev/null || true
  local t0 t1; t0=$(date +%s)
  log "partA: launch build container"
  incus launch "$BASE_IMG" zwavejs-build </dev/null
  sleep 5
  for i in $(seq 1 30); do incus exec zwavejs-build -- getent hosts github.com >/dev/null 2>&1 && break; sleep 2; done
  log "partA: install zwave-js-ui prebuilt binary (no community-scripts/pct)"
  incus exec zwavejs-build -- bash -c '
    set -e
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq curl ca-certificates unzip >/dev/null
    URL=$(curl -fsSL https://api.github.com/repos/zwave-js/zwave-js-ui/releases/latest \
          | grep -oE "https://[^\"]+zwave-js-ui-[^\"]+-linux\.zip" | head -1)
    echo "asset: $URL"
    mkdir -p /opt/zwave-js-ui
    curl -fsSL "$URL" -o /tmp/zjs.zip
    unzip -o /tmp/zjs.zip -d /opt/zwave-js-ui >/dev/null
    chmod +x /opt/zwave-js-ui/zwave-js-ui-linux      # NOTE: binary is *-linux
    cat > /etc/systemd/system/zwave-js-ui.service <<UNIT
[Unit]
Description=Z-Wave JS UI (Ctrlable)
After=network-online.target
[Service]
ExecStart=/opt/zwave-js-ui/zwave-js-ui-linux
WorkingDirectory=/opt/zwave-js-ui
Restart=on-failure
Environment=STORE_DIR=/opt/zwave-js-ui/store
[Install]
WantedBy=multi-user.target
UNIT
    systemctl enable zwave-js-ui >/dev/null 2>&1
    mkdir -p /etc/ctrlable
  '
  t1=$(date +%s); log "partA: builder wall-time $((t1-t0))s (per-service cost, §7)"
  incus stop zwavejs-build </dev/null
  incus publish zwavejs-build --alias ctrlable-zwavejs </dev/null
  incus delete -f zwavejs-build </dev/null
}

# A2: deploy an instance with firstboot + the internal NIC at a fixed address.
deploy_zwavejs(){
  local name="${1:-zwavejs-1}" ip="${2:-198.18.0.20}"
  incus delete -f "$name" 2>/dev/null || true
  log "partA: deploy $name"
  incus launch ctrlable-zwavejs "$name" </dev/null
  sleep 3
  incus exec "$name" -- mkdir -p /etc/ctrlable
  printf '{"orchestrator_url":"%s","template_type":"zwavejs","instance":"%s"}\n' "$ORCH_URL" "$name" \
    | incus exec "$name" -- bash -c 'cat > /etc/ctrlable/firstboot.conf'
  log "partA: attach internal NIC ($ip on $INT_NET)"
  incus config device add "$name" eth1 nic network="$INT_NET" ipv4.address="$ip" </dev/null
  # base image only ships an eth0 networkd unit; the internal NIC is firstboot's job:
  incus exec "$name" -- bash -c "cat > /etc/systemd/network/10-eth1-internal.network <<NET
[Match]
Name=eth1
[Network]
DHCP=ipv4
NET
systemctl restart systemd-networkd"
  incus exec "$name" -- systemctl start zwave-js-ui
  sleep 6
  log "partA: verify"
  incus exec "$name" -- ip -4 -br addr
  incus exec "$name" -- systemctl is-active zwave-js-ui
}

partA(){ ensure_int_net; build_zwavejs_image; deploy_zwavejs "$@"; }

# ---- Part B: Ctrlable Pro = HA Supervised in an LXC ------------------------
# VERIFIED 2026-11-17 on this box: Supervisor + 97-addon store + Core on :8123,
# unprivileged container, no AppArmor disable. Remaining: `healthy/supported=false`
# banner (OURS to flip via our Supervisor fork, §6) and USB passthrough (partB_usb).
CPRO="${CPRO:-ctrlable-pro-1}"
HA_MACHINE="${HA_MACHINE:-qemux86-64}"

partB(){
  log "partB: launch $CPRO on Debian 13 (installer hard-requires debian 13)"
  incus info "$CPRO" >/dev/null 2>&1 || incus launch images:debian/13 "$CPRO" \
    -c security.nesting=true \
    -c security.syscalls.intercept.mknod=true \
    -c security.syscalls.intercept.setxattr=true </dev/null
  sleep 4
  for i in $(seq 1 30); do incus exec "$CPRO" -- getent hosts download.docker.com >/dev/null 2>&1 && break; sleep 2; done

  # HOST prereq: the supervised installer writes kernel.dmesg_restrict (a host-global
  # sysctl) — read-only inside an unprivileged CT. Pre-set it on the host so the
  # installer sees 0 and skips the write. (Our image/first-boot sets this at build time.)
  log "partB: host sysctl kernel.dmesg_restrict=0"
  sysctl -w kernel.dmesg_restrict=0 >/dev/null
  grep -q '^kernel.dmesg_restrict' /etc/sysctl.conf 2>/dev/null || echo 'kernel.dmesg_restrict=0' >> /etc/sysctl.conf

  log "partB: Docker (overlay2 over btrfs) inside $CPRO"
  incus exec "$CPRO" -- bash -c '
    set -e; export DEBIAN_FRONTEND=noninteractive
    command -v docker >/dev/null && exit 0
    mkdir -p /etc/docker; printf "%s\n" "{ \"storage-driver\": \"overlay2\" }" > /etc/docker/daemon.json
    curl -fsSL https://get.docker.com | sh >/dev/null 2>&1
  '

  log "partB: HA prereqs + os-agent"
  incus exec "$CPRO" -- bash -c '
    set -e; export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq apparmor bluez cifs-utils curl dbus jq libglib2.0-bin lsb-release \
      network-manager nfs-common systemd-journal-remote systemd-resolved udisks2 wget ca-certificates >/dev/null 2>&1 || true
    if ! gdbus introspect --system --dest io.hass.os --object-path /io/hass/os >/dev/null 2>&1; then
      URL=$(curl -fsSL https://api.github.com/repos/home-assistant/os-agent/releases/latest | grep -oE "https://[^\"]+os-agent_[^\"]+_linux_x86_64\.deb" | head -1)
      curl -fsSL "$URL" -o /tmp/os-agent.deb; dpkg -i /tmp/os-agent.deb >/dev/null 2>&1 || apt-get -f install -y -qq >/dev/null 2>&1
    fi
  '

  log "partB: install homeassistant-supervised (machine=$HA_MACHINE)"
  incus exec "$CPRO" -- bash -c "
    docker ps --format '{{.Names}}' 2>/dev/null | grep -q hassio_supervisor && exit 0
    export DEBIAN_FRONTEND=noninteractive
    echo 'homeassistant-supervised homeassistant/machine select $HA_MACHINE' | debconf-set-selections || true
    curl -fsSL -o /tmp/hass-supervised.deb https://github.com/home-assistant/supervised-installer/releases/latest/download/homeassistant-supervised.deb
    apt-get install -y /tmp/hass-supervised.deb
  "
  log "partB: Supervisor starting; store + Core pull in background. Verify with: incus exec $CPRO -- ha supervisor info"
  # TODO(fork): flip healthy/supported banner via our Supervisor fork; apply ctrlable rebrand.
}

# USB coordinator passthrough — run when a Z-Wave/Zigbee stick is plugged into the host.
# VERIFIED 2026-11-17 with a SONOFF Dongle Plus MG24 (CP210x): node bound at the same by-id
# path, survives `incus restart`, and Supervisor lists it after a udev trigger.
partB_usb(){
  local byid="${1:?usage: partB_usb /dev/serial/by-id/usb-...}"
  local dev="${2:-zigbeestick}"
  incus config device remove "$CPRO" "$dev" 2>/dev/null || true
  incus config device add "$CPRO" "$dev" unix-char source="$byid" path="$byid" </dev/null
  # bound after boot => no udev "add" event fired; trigger so Supervisor's hardware scan sees it.
  # (At real boot the stick is present and enumerates naturally; this is only for hot-plug.)
  incus exec "$CPRO" -- bash -lc 'udevadm trigger 2>/dev/null; udevadm settle 2>/dev/null' || true
  incus exec "$CPRO" -- ls -l "$byid"
  log "partB_usb: $byid bound into $CPRO (same by-id path, restart-stable, Supervisor-visible)"
}

# ---- Part C: frigate (iGPU VAAPI transcode) --------------------------------
# VERIFIED 2026-11-17: iGPU VAAPI (H264 decode+encode) works in an Incus container, but frigate
# needs a PRIVILEGED container for the /dev/dri node mount (unprivileged forkmount fails; privileged
# succeeds) -- unlike Supervised. Matches host/builders/frigate-docker.sh. Only the render node is
# needed (not card0). Driver: i965 on Gen8 Braswell/CherryView, iHD on Broadwell+.
FRIGATE_NAME="${FRIGATE_NAME:-frigate-1}"
FRIGATE_IMG="${FRIGATE_IMG:-ghcr.io/blakeblackshear/frigate:stable}"
RENDER_NODE="${RENDER_NODE:-/dev/dri/renderD128}"

partC_frigate(){
  if [ ! -e "$RENDER_NODE" ]; then
    log "partC: no $RENDER_NODE on host (no iGPU) -> frigate would run CPU-only; skipping GPU setup"
    return 0
  fi
  log "partC: privileged CT $FRIGATE_NAME + render-node passthrough (GPU needs privileged)"
  incus info "$FRIGATE_NAME" >/dev/null 2>&1 || incus launch images:debian/13 "$FRIGATE_NAME" \
    -c security.privileged=true -c security.nesting=true \
    -c security.syscalls.intercept.mknod=true -c security.syscalls.intercept.setxattr=true </dev/null
  incus config device get "$FRIGATE_NAME" renderD128 source >/dev/null 2>&1 || \
    incus config device add "$FRIGATE_NAME" renderD128 unix-char \
        source="$RENDER_NODE" path="$RENDER_NODE" </dev/null

  log "partC: Docker-in-LXC + pick the VAAPI driver the way frigate-docker.sh does"
  incus exec "$FRIGATE_NAME" -- bash -c '
    set -e; export DEBIAN_FRONTEND=noninteractive
    command -v docker >/dev/null || { mkdir -p /etc/docker
      printf "%s\n" "{ \"storage-driver\": \"overlay2\" }" > /etc/docker/daemon.json
      curl -fsSL https://get.docker.com | sh >/dev/null 2>&1; }'
  incus exec "$FRIGATE_NAME" -- docker pull -q "$FRIGATE_IMG" >/dev/null
  # iHD covers Broadwell+, i965 the Gen8 Atoms -- ask the image which one actually decodes.
  local drv=""
  for d in iHD i965; do
    if incus exec "$FRIGATE_NAME" -- docker run --rm --device "$RENDER_NODE" \
         -e LIBVA_DRIVER_NAME="$d" --entrypoint vainfo "$FRIGATE_IMG" \
         --display drm --device "$RENDER_NODE" >/dev/null 2>&1; then drv="$d"; break; fi
  done
  if [ -n "$drv" ]; then
    log "partC: VAAPI works with LIBVA_DRIVER_NAME=$drv (frigate: -hwaccel vaapi -hwaccel_device $RENDER_NODE)"
  else
    log "partC: VAAPI unusable with iHD or i965 -> frigate decodes on CPU"
  fi
  # The frigate container itself (config.yml + cameras) is deployed from the release manifest,
  # same as every other service -- this function establishes the GPU-capable, Docker-ready base.
}

# ---- Generic service builder (the mechanical tier, §7a) --------------------
# community-scripts don't run on Incus; each mechanical service is a short distro-agnostic
# install snippet in a base container, then `incus publish` -- the exact shape partA proved with
# zwavejs. Deploy-time concerns (internal NIC, USB coordinator) are partA/partB_usb, not build.
#   build_service <name> '<install-snippet run as root in the base CT>'
build_service(){
  local svc="$1" install="$2" alias="ctrlable-$1"
  if incus image show "$alias" >/dev/null 2>&1; then log "image $alias exists"; return 0; fi
  incus delete -f "$svc-build" 2>/dev/null || true
  incus launch "$BASE_IMG" "$svc-build" </dev/null; sleep 5
  for i in $(seq 1 30); do incus exec "$svc-build" -- getent hosts deb.debian.org >/dev/null 2>&1 && break; sleep 2; done
  incus exec "$svc-build" -- bash -c "set -e; export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; $install; mkdir -p /etc/ctrlable"
  incus stop "$svc-build" </dev/null
  incus publish "$svc-build" --alias "$alias" </dev/null
  incus delete -f "$svc-build" </dev/null
  log "built $alias"
}

# Concrete easy-tier recipe (pure Debian package). mosquitto is the canonical trivial case that
# proves the generic builder end-to-end.
build_mosquitto(){ build_service mosquitto 'apt-get install -y -qq mosquitto >/dev/null; systemctl enable mosquitto >/dev/null 2>&1'; }

partD_services(){ ensure_int_net; build_mosquitto; }
# TODO(P2): the rest port through build_service with the same shape:
#   go2rtc / matter-server  -> a release binary or pip install (like zwavejs/mosquitto),
#   dali-bridge             -> host/builders/dali-bridge.sh + partB_usb-style USB passthrough,
#   hardware-manager        -> host/builders/hardware-manager.sh,
#   freepbx                 -> host/builders/freepbx.sh; SIP/RTP ports on the site NIC at deploy
#                              (networking config, not an Incus blocker).

main(){
  case "${1:-all}" in
    stage1) stage1 ;;
    partA)  shift || true; partA "$@" ;;
    partB)  partB ;;
    partC)  partC_frigate ;;
    partD)  partD_services ;;
    all)    stage1; partA; partB; partC_frigate; partD_services ;;
    *) echo "usage: $0 [stage1|partA|partB|partC|partD|all]"; exit 2 ;;
  esac
  log "done: ${1:-all}"
}
main "$@"
