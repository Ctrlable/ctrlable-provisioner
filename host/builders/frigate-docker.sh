#!/usr/bin/env bash
# Ctrlable Provisioner builder — Frigate NVR in a Docker LXC.
#
# Invoked by `ctrlable-build` for the manifest template whose builder is
# `ctrlable/frigate-docker.sh` (bundled on the build host by install.sh).
#
# Replaces the community-scripts native Frigate install (ct/frigate.sh), which
# builds Frigate from source inside the CT: slow, pinned to whatever that script
# can still compile, and upgraded by re-running it. Here Frigate is the
# CTRLABLE image (github.com/Ctrlable/frigate, branch ctrlable/0.18): upstream
# 0.18 plus our fixes -- API handlers that no longer block the event loop, the
# ZMQ subscriber leak (#23999), watchdog restart backoff, camera reachability
# probe, staggered region-grid rebuilds. An upgrade is a tag bump and a restart.
#
# The config it lays down is the Ctrlable baseline, each line a lesson from a
# live site (see ctrlable_baseline_config below).
#
# The CT is privileged with nesting (Docker inside LXC) and gets the host iGPU
# render node for VAAPI decode when the host has one.
#
# Contract with the rest of the provisioner:
#   - config lives at /etc/frigate/config.yml, mounted as /config, which is the
#     file ctrlable-firstboot's wire_frigate() writes MQTT settings into;
#   - a `frigate` systemd unit wraps the compose project, so wire_frigate()'s
#     `systemctl restart frigate` recreates the container with that config.
#
# Env (from ctrlable-build): VMID, CT_HOSTNAME, var_container_storage,
# var_template_storage, var_cpu, var_ram, var_disk.
# FRIGATE_IMAGE — override the image (default pinned below).
set -euo pipefail

VMID="${VMID:?ctrlable-build must export VMID}"
CT_HOSTNAME="${CT_HOSTNAME:-frigate}"
STORAGE="${var_container_storage:-local-lvm}"
TSTORAGE="${var_template_storage:-local}"
CORES="${var_cpu:-4}"
RAM="${var_ram:-4096}"
DISK="${var_disk:-32}"
BRIDGE="${CTRLABLE_BRIDGE:-vmbr0}"
# Tag = the release manifest's app_version; FRIGATE_IMAGE overrides the whole ref.
IMAGE="${FRIGATE_IMAGE:-ghcr.io/ctrlable/frigate:${APP_VERSION:-0.18.0-ctrlable.1}}"

log() { echo "frigate-docker builder: $*"; }

# Debian 12 template: install.sh already downloaded it for the orchestrator.
pveam update >/dev/null 2>&1 || true
TEMPLATE=$(pveam list "$TSTORAGE" 2>/dev/null | awk '/debian-12-standard/{print $1}' | sort -V | tail -1)
if [[ -z "$TEMPLATE" ]]; then
  name=$(pveam available --section system | awk '/debian-12-standard/{print $2}' | sort -V | tail -1)
  [[ -n "$name" ]] || { log "no Debian 12 template available"; exit 1; }
  pveam download "$TSTORAGE" "$name"
  TEMPLATE="${TSTORAGE}:vztmpl/${name}"
fi

log "creating CT $VMID ($CT_HOSTNAME) from $TEMPLATE on $STORAGE"
pct create "$VMID" "$TEMPLATE" \
  --hostname "$CT_HOSTNAME" \
  --cores "$CORES" --memory "$RAM" --swap 0 \
  --rootfs "${STORAGE}:${DISK}" \
  --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
  --unprivileged 0 \
  --features nesting=1,keyctl=1 \
  --onboot 1 \
  --start 0

# iGPU render node for VAAPI decode. Optional: a host without one still gets a
# working (CPU-decoding) Frigate.
GPU=0
if [[ -e /dev/dri/renderD128 ]]; then
  cat >> "/etc/pve/lxc/${VMID}.conf" <<EOF
lxc.cgroup2.devices.allow: c 226:* rwm
lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir
EOF
  GPU=1
  log "passing /dev/dri into the CT"
fi

pct start "$VMID"

log "waiting for network in CT"
for _ in $(seq 1 60); do
  pct exec "$VMID" -- bash -c 'getent hosts deb.debian.org >/dev/null' 2>/dev/null && break
  sleep 2
done

retry() {
  local n=1
  until "$@"; do
    (( n >= 5 )) && return 1
    sleep $((n * 5)); n=$((n + 1))
  done
}

log "installing Docker"
retry pct exec "$VMID" -- bash -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg python3 python3-yaml >/dev/null
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release; echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
  systemctl enable --now docker >/dev/null
'

log "pulling $IMAGE"
retry pct exec "$VMID" -- docker pull -q "$IMAGE"

# VAAPI driver. iHD covers Broadwell+ in practice, but Gen8 Atoms (Braswell
# N3xxx/J3xxx) and older only work with i965 -- and a wrong driver shows up as
# ffmpeg crash loops, not as an error at start. Ask the image itself.
HWACCEL=""
LIBVA=""
if [[ "$GPU" == 1 ]]; then
  for drv in iHD i965; do
    if pct exec "$VMID" -- docker run --rm --device /dev/dri/renderD128 -e LIBVA_DRIVER_NAME=$drv \
         --entrypoint vainfo "$IMAGE" --display drm --device /dev/dri/renderD128 >/dev/null 2>&1; then
      LIBVA=$drv; break
    fi
  done
  if [[ -n "$LIBVA" ]]; then
    # NOT preset-vaapi: it keeps frames on the GPU (-hwaccel_output_format
    # vaapi) and the following hwdownload failed "Failed to sync surface" every
    # ~45 s per camera at Pinecrest (825 crashes/h). Plain -hwaccel vaapi
    # decodes on the GPU and lets Frigate scale on the CPU: 0 crashes.
    HWACCEL="-hwaccel vaapi -hwaccel_device /dev/dri/renderD128"
    log "VAAPI works with LIBVA_DRIVER_NAME=$LIBVA"
  else
    log "VAAPI unusable with iHD or i965 -- decoding on the CPU"
  fi
fi

CT_IP=$(pct exec "$VMID" -- hostname -I | awk '{print $1}')

log "writing baseline config, compose project and frigate unit"
pct exec "$VMID" -- mkdir -p /opt/frigate /etc/frigate /var/lib/frigate/media
# Baseline config, written only if none exists (a re-run never clobbers a site's
# cameras). Block style throughout: flow-style YAML re-emitted by ruamel once
# dropped 16 go2rtc restreams. ctrlable-firstboot's wire_frigate() merges the
# site MQTT settings into this file; it does not replace it.
pct exec "$VMID" -- env CT_IP="$CT_IP" HWACCEL="$HWACCEL" python3 - <<'PY'
import os, yaml
path = "/etc/frigate/config.yml"
if os.path.exists(path) and os.path.getsize(path):
    raise SystemExit(0)
cfg = {
    "mqtt": {"enabled": False},
    # Semantic search re-indexing pinned Miami's CPU alongside a leaking go2rtc;
    # opt in per site once the box has headroom (and a GPU/NPU for embeddings).
    "semantic_search": {"enabled": False},
    "go2rtc": {
        # trace-level go2rtc logging was half of Miami's CPU saturation.
        "log": {"level": "info"},
        "webrtc": {
            # host:8555 for the LAN, and stun:8555 for the public address. The
            # STUN servers are IPv4 LITERALS on purpose: by name, Go resolves
            # AAAA first, go2rtc learns its own IPv6, and off-LAN WebRTC never
            # gets a usable IPv4 candidate -- silently.
            "candidates": [f"{os.environ['CT_IP']}:8555", "stun:8555"],
            "ice_servers": [
                {"urls": ["stun:162.159.207.0:3478"]},    # stun.cloudflare.com
                {"urls": ["stun:74.125.250.129:19302"]},  # stun.l.google.com
            ],
        },
    },
    "cameras": {},
}
if os.environ.get("HWACCEL"):
    cfg["ffmpeg"] = {"hwaccel_args": os.environ["HWACCEL"]}
with open(path, "w") as f:
    yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
PY

env_block=""
[[ -n "$LIBVA" ]] && env_block="    environment:
      - LIBVA_DRIVER_NAME=${LIBVA}"
devices=""
[[ "$GPU" == 1 ]] && devices='    devices:
      - /dev/dri/renderD128:/dev/dri/renderD128'
pct exec "$VMID" -- bash -c "
  set -e
  cat > /opt/frigate/compose.yml <<'YAML'
services:
  frigate:
    container_name: frigate
    image: ${IMAGE}
    restart: unless-stopped
    network_mode: host
    shm_size: 512mb
    stop_grace_period: 30s
${devices}
${env_block}
    volumes:
      - /etc/localtime:/etc/localtime:ro
      - /etc/frigate:/config
      - /var/lib/frigate/media:/media/frigate
      - type: tmpfs
        target: /tmp/cache
        tmpfs:
          size: 1000000000
YAML
  cat > /etc/systemd/system/frigate.service <<'UNIT'
[Unit]
Description=Frigate NVR (docker compose)
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/frigate
ExecStart=/usr/bin/docker compose -f /opt/frigate/compose.yml up -d --remove-orphans
ExecStop=/usr/bin/docker compose -f /opt/frigate/compose.yml down
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
"

log "starting frigate"
pct exec "$VMID" -- systemctl enable --now frigate >/dev/null
log "done (CT $VMID, ip=$CT_IP, gpu=$GPU, libva=${LIBVA:-none})"
