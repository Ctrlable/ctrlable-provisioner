#!/usr/bin/env bash
# Runs once on first boot of a cloned Ctrlable guest.
# Baked into every Debian-based LXC template at build time.
# Reads /etc/ctrlable/firstboot.conf (JSON, written by ctrlable-build).
set -euo pipefail

CONF=/etc/ctrlable/firstboot.conf
LOG_TAG=ctrlable-firstboot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { echo "[ctrlable-firstboot] $*" | systemd-cat -t "$LOG_TAG" 2>/dev/null; echo "[ctrlable-firstboot] $*"; }
die()  { log "FATAL: $*"; exit 1; }

jget() {
    # Usage: jget <json-string> <key> [default]
    python3 -c "
import json, sys
d = json.loads(sys.argv[1])
print(d.get(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ''))
" "$1" "$2" "${3:-}"
}

http_get() {
    # Retry GET with exponential back-off. Prints response body; returns 0 on success.
    local url="$1" attempt delay=5
    for attempt in 1 2 3 4 5; do
        local body
        body=$(curl -sf --max-time 20 "$url" 2>/dev/null) && { printf '%s' "$body"; return 0; }
        log "GET $url failed (attempt $attempt/5), retrying in ${delay}s"
        sleep "$delay"
        delay=$(( delay * 2 ))
    done
    return 1
}

http_post() {
    # POST JSON body; returns 0 on success.
    local url="$1" body="$2" attempt delay=5
    for attempt in 1 2 3 4 5; do
        curl -sf --max-time 20 -X POST -H "Content-Type: application/json" -d "$body" "$url" \
            >/dev/null 2>&1 && return 0
        log "POST $url failed (attempt $attempt/5), retrying in ${delay}s"
        sleep "$delay"
        delay=$(( delay * 2 ))
    done
    return 1
}

wait_network() {
    log "waiting for network"
    local i
    for i in $(seq 1 30); do
        if curl -sf --max-time 3 https://1.1.1.1 >/dev/null 2>&1 \
            || curl -sf --max-time 3 http://1.1.1.1 >/dev/null 2>&1; then
            log "network up"
            return 0
        fi
        sleep 2
    done
    # Not a fatal error — orchestrator may be LAN-only
    log "external network check timed out; continuing (orchestrator may be LAN-only)"
}

# ---------------------------------------------------------------------------
# Identity reset
# ---------------------------------------------------------------------------

reset_machine_id() {
    log "resetting machine-id"
    truncate -s0 /etc/machine-id
    rm -f /var/lib/dbus/machine-id
    systemd-machine-id-setup
    # Symlink dbus machine-id to systemd's
    ln -sf /etc/machine-id /var/lib/dbus/machine-id
}

regen_ssh_keys() {
    log "regenerating SSH host keys"
    rm -f /etc/ssh/ssh_host_*
    dpkg-reconfigure -f noninteractive openssh-server 2>/dev/null \
        || ssh-keygen -A   # fallback if dpkg-reconfigure unavailable
    systemctl restart ssh 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Service wiring
# ---------------------------------------------------------------------------

wire_zigbee2mqtt() {
    local wire_to="$1"
    local mqtt_host mqtt_port mqtt_user mqtt_pass coord_url
    mqtt_host=$(jget "$wire_to" "mqtt_host" "localhost")
    mqtt_port=$(jget "$wire_to" "mqtt_port" "1883")
    mqtt_user=$(jget "$wire_to" "mqtt_user" "")
    mqtt_pass=$(jget "$wire_to" "mqtt_pass" "")
    coord_url=$(jget "$wire_to" "coordinator_url" "tcp://localhost:6638")

    local cfg_dir=/opt/zigbee2mqtt/data
    mkdir -p "$cfg_dir"

    cat > "$cfg_dir/configuration.yaml" <<YAML
homeassistant: false
permit_join: false
mqtt:
  base_topic: zigbee2mqtt
  server: 'mqtt://${mqtt_host}:${mqtt_port}'
  user: '${mqtt_user}'
  password: '${mqtt_pass}'
serial:
  port: '${coord_url}'
advanced:
  log_level: info
frontend:
  port: 8080
YAML

    log "z2m config written to $cfg_dir/configuration.yaml"
    systemctl restart zigbee2mqtt 2>/dev/null || true
}

wire_zwavejs() {
    local wire_to="$1"
    local mqtt_host mqtt_port mqtt_user mqtt_pass coord_url
    mqtt_host=$(jget "$wire_to" "mqtt_host" "localhost")
    mqtt_port=$(jget "$wire_to" "mqtt_port" "1883")
    mqtt_user=$(jget "$wire_to" "mqtt_user" "")
    mqtt_pass=$(jget "$wire_to" "mqtt_pass" "")
    coord_url=$(jget "$wire_to" "coordinator_url" "")

    local store_dir=/opt/zwave-js-ui/store
    mkdir -p "$store_dir"

    # Z-Wave JS UI reads MQTT settings from settings.json on startup.
    # Preserve any existing non-MQTT settings (driver config, devices, etc.)
    # by merging — but on a fresh template there is nothing to preserve.
    cat > "$store_dir/settings.json" <<JSON
{
  "mqtt": {
    "name": "zwavejs2mqtt",
    "host": "${mqtt_host}",
    "port": ${mqtt_port},
    "topic": "zwave",
    "qos": 1,
    "prefix": "",
    "username": "${mqtt_user}",
    "password": "${mqtt_pass}",
    "reconnectPeriod": 3000,
    "auth": true,
    "_version": 0
  },
  "zwave": {
    "port": "${coord_url}",
    "logEnabled": true,
    "logLevel": "info",
    "_version": 0
  },
  "gateway": {
    "type": 2,
    "payloadType": 0,
    "nodeNames": true,
    "hassDiscovery": true,
    "discoveryPrefix": "homeassistant",
    "logEnabled": true,
    "logLevel": "info",
    "_version": 0
  }
}
JSON

    log "z-wave-js-ui settings written to $store_dir/settings.json"
    systemctl restart zwave-js-ui 2>/dev/null || true
}

wire_frigate() {
    local wire_to="$1"
    local mqtt_host mqtt_port mqtt_user mqtt_pass
    mqtt_host=$(jget "$wire_to" "mqtt_host" "localhost")
    mqtt_port=$(jget "$wire_to" "mqtt_port" "1883")
    mqtt_user=$(jget "$wire_to" "mqtt_user" "")
    mqtt_pass=$(jget "$wire_to" "mqtt_pass" "")

    local cfg_dir=/etc/frigate
    mkdir -p "$cfg_dir"

    # Preserve any existing cameras config if present
    local cameras_block="cameras: {}"
    if [[ -f "$cfg_dir/config.yml" ]]; then
        cameras_block=$(python3 -c "
import sys
try:
    import yaml
    cfg = yaml.safe_load(open('$cfg_dir/config.yml'))
    if cfg and 'cameras' in cfg:
        print('cameras:')
        for k, v in cfg['cameras'].items():
            print(f'  {k}: ...')
except Exception:
    pass
print('cameras: {}')
" 2>/dev/null || echo "cameras: {}")
    fi

    cat > "$cfg_dir/config.yml" <<YAML
mqtt:
  enabled: true
  host: ${mqtt_host}
  port: ${mqtt_port}
  user: ${mqtt_user}
  password: ${mqtt_pass}
${cameras_block}
YAML

    log "frigate config written to $cfg_dir/config.yml"
    systemctl restart frigate 2>/dev/null || true
}

wire_docker_portainer() {
    # Docker + Portainer has no standard MQTT wiring.
    # Service wiring is done manually through Portainer UI post-deploy.
    log "docker-portainer: no automated wiring; configure stacks via Portainer UI"
}

wire_freepbx() {
    # FreePBX wiring requires web admin interaction.
    # TODO: implement via FreePBX REST API in a future milestone.
    log "freepbx: automated wiring not yet implemented — configure via FreePBX admin UI"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    log "starting — hostname=$(hostname)"

    [[ -f "$CONF" ]] || die "firstboot config not found: $CONF"

    local conf_json
    conf_json=$(cat "$CONF")
    local orchestrator_url secret template_type
    orchestrator_url=$(jget "$conf_json" "orchestrator_url")
    secret=$(jget "$conf_json" "firstboot_secret")
    template_type=$(jget "$conf_json" "template_type")

    [[ -n "$orchestrator_url" ]] || die "orchestrator_url missing from $CONF"
    [[ -n "$secret"           ]] || die "firstboot_secret missing from $CONF"
    [[ -n "$template_type"    ]] || die "template_type missing from $CONF"

    # Step 1: wait for LAN reachability
    wait_network

    # Step 2: reset machine identity
    reset_machine_id

    # Step 3: regenerate SSH host keys
    regen_ssh_keys

    # Step 4: call home for service-wiring assignment
    local hostname
    hostname=$(hostname)
    log "fetching assignment for hostname=${hostname}"

    local assignment
    assignment=$(http_get \
        "${orchestrator_url}/api/provision/assignment?hostname=${hostname}&secret=${secret}") \
        || die "could not reach orchestrator at ${orchestrator_url} after retries"

    local wire_to
    wire_to=$(python3 -c "import json,sys; print(json.dumps(json.loads(sys.argv[1])['wire_to']))" \
        "$assignment") || die "malformed assignment response"

    # Step 5: apply service wiring
    log "applying wiring for type=${template_type}"
    case "$template_type" in
        zigbee2mqtt)     wire_zigbee2mqtt "$wire_to" ;;
        zwavejs)         wire_zwavejs     "$wire_to" ;;
        frigate)         wire_frigate     "$wire_to" ;;
        docker-portainer) wire_docker_portainer "$wire_to" ;;
        freepbx)         wire_freepbx    "$wire_to" ;;
        *)               log "unknown template type '${template_type}' — skipping wiring" ;;
    esac

    # Step 6: report completion
    log "reporting completion to orchestrator"
    local complete_body
    complete_body=$(python3 -c "
import json, sys
print(json.dumps({'hostname': sys.argv[1], 'secret': sys.argv[2]}))
" "$hostname" "$secret")

    http_post "${orchestrator_url}/api/provision/complete" "$complete_body" \
        || die "could not POST /api/provision/complete"

    # Step 7: disable ourselves — never run again
    systemctl disable ctrlable-firstboot.service 2>/dev/null || true

    log "personalization complete"
}

main
