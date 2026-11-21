#!/usr/bin/env bash
#
# ctrlable-host-enroll.sh — put the bare-metal HOST on the Ctrlable VPN as its own tunnel peer:
# an SSH-over-WireGuard troubleshooting backdoor, independent of the orchestrator container and
# of Incus. Uses the SAME zero-touch path as the HA add-on and the orchestrator appliance enrol:
# POST /v1/auto-enroll with the baked provisioning key (no per-box token, no GitHub creds on the
# box). Parses with jq only (no python on the lean host). Idempotent and self-retrying; never
# blocks boot — a failed attempt is re-tried on the next boot/run.
#
set -uo pipefail
log(){ echo "[host-enroll] $*"; }

# MODE: "boot" (default, run once at boot) or "heal" (periodic timer). In heal mode a tunnel that is
# merely "interface up" is NOT trusted — we check the WireGuard HANDSHAKE age and re-enrol if it has
# gone stale. This is what lets a fielded box self-heal onto a MIGRATED portal (new WG endpoint/key):
# the portal's /v1/auto-enroll hands back a fresh wg_config, which we rewrite + bring up. Boot mode
# keeps the cheap "iface up = good" check (a fresh boot may not have handshaked yet; the timer heals).
MODE="${1:-boot}"
STALE_SECS="${CTRLABLE_WG_STALE_SECS:-180}"   # no handshake within this window (heal mode) => re-enrol

# handshake_fresh <iface>: 0 (true) iff some peer handshaked within STALE_SECS. `wg show … latest-
# handshakes` prints "<pubkey>\t<epoch>"; 0 = never.
handshake_fresh(){
  local ifc="$1" now newest=0 _pk hs
  now=$(date +%s)
  while read -r _pk hs; do [ "${hs:-0}" -gt "$newest" ] && newest="$hs"; done \
    < <(wg show "$ifc" latest-handshakes 2>/dev/null)
  [ "$newest" -gt 0 ] && [ $((now - newest)) -le "$STALE_SECS" ]
}

CONF=/etc/ctrlable/appliance.conf
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF" || { log "no $CONF — skipping host VPN enrol"; exit 0; }
PORTAL_URL="${PORTAL_URL:-https://portal.ctrlable.com}"
if [ -z "${PROVISIONING_KEY:-}" ]; then
  log "no PROVISIONING_KEY in $CONF — skipping host VPN enrol"; exit 0
fi

WG_DIR=/etc/wireguard
STATE_DIR=/var/lib/ctrlable
STATE="$STATE_DIR/host-enroll.conf"
install -d -m0700 "$WG_DIR"; install -d -m0755 "$STATE_DIR"

# Already enrolled? Ensure the tunnel is healthy, then exit. In heal mode "healthy" means a fresh
# handshake; a stale one (dead endpoint after a portal migration) falls through to re-enrol below.
if [ -f "$STATE" ]; then
  # shellcheck disable=SC1090
  . "$STATE"
  if ip link show "${WG_IFACE:-}" >/dev/null 2>&1; then
    if [ "$MODE" = heal ]; then
      if handshake_fresh "$WG_IFACE"; then
        log "heal: ${WG_IFACE} (${TUNNEL_IP:-?}) handshake fresh (<=${STALE_SECS}s) — healthy"; exit 0
      fi
      log "heal: ${WG_IFACE} up but handshake STALE (>${STALE_SECS}s) — re-enrolling to refresh endpoint/config"
      # fall through to re-enrol (portal may have moved / rotated its WG key)
    else
      log "already enrolled on ${WG_IFACE} (${TUNNEL_IP:-?}); tunnel up"; exit 0
    fi
  elif [ -n "${WG_IFACE:-}" ] && wg-quick up "$WG_IFACE" >/dev/null 2>&1; then
    if [ "$MODE" != heal ]; then
      log "already enrolled; brought ${WG_IFACE} back up (${TUNNEL_IP:-?})"; exit 0
    fi
    sleep 3
    if handshake_fresh "$WG_IFACE"; then log "heal: ${WG_IFACE} back up + handshaking"; exit 0; fi
    log "heal: ${WG_IFACE} up but not handshaking — re-enrolling"
    # fall through to re-enrol
  fi
fi

IFACE=$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}'); IFACE="${IFACE:-eth0}"
MAC=$(cat "/sys/class/net/$IFACE/address" 2>/dev/null | tr '[:lower:]' '[:upper:]'); MAC="${MAC:-00:00:00:00:00:00}"
HN=$(hostname 2>/dev/null || echo ctrlable-host)
req=$(printf '{"mac_address":"%s","platform":"debian-host","hostname":"%s","device_name":"%s (host)"}' "$MAC" "$HN" "$HN")

# boot: persist through a slow network (up to ~5min). heal: bail fast — the timer retries soon.
attempt=0; resp=""; [ "$MODE" = heal ] && max=3 || max=20
while :; do
  attempt=$((attempt+1))
  if resp=$(curl -fsSk --max-time 30 -X POST "$PORTAL_URL/v1/auto-enroll" \
        -H "Content-Type: application/json" \
        -H "X-Provisioning-Key: $PROVISIONING_KEY" \
        -d "$req" 2>/dev/null) && [ -n "$resp" ]; then
    break
  fi
  if [ "$attempt" -ge "$max" ]; then
    log "WARN /v1/auto-enroll unreachable after $attempt tries — host NOT on VPN yet (retries next boot/run)"
    exit 0
  fi
  log "auto-enroll attempt $attempt failed; retry in 15s"; sleep 15
done

detail=$(printf '%s' "$resp" | jq -r '.detail // empty' 2>/dev/null)
if [ -n "$detail" ]; then log "ERROR portal rejected enrol: $detail"; exit 0; fi

DEVICE_ID=$(printf '%s' "$resp"  | jq -r '.device_id // empty')
TUNNEL_IP=$(printf '%s' "$resp"  | jq -r '.tunnel_ip // empty')
WG_IFACE=$(printf '%s' "$resp"   | jq -r '.interface // empty')
DEVICE_TOKEN=$(printf '%s' "$resp"| jq -r '.device_token // empty')
CLAIM_CODE=$(printf '%s' "$resp" | jq -r '.claim_code // empty')
if [ -z "$DEVICE_ID" ] || [ -z "$WG_IFACE" ]; then
  log "ERROR unexpected portal response: $(printf '%s' "$resp" | head -c 200)"; exit 0
fi

# wg_config is base64; drop DNS= lines (the lean host has no resolvconf).
conf="$WG_DIR/$WG_IFACE.conf"
printf '%s' "$resp" | jq -r '.wg_config | @base64d' | grep -vE '^[[:space:]]*DNS' > "$conf"
chmod 600 "$conf"

umask 077
cat > "$STATE" <<EOF
DEVICE_ID=$DEVICE_ID
DEVICE_TOKEN=$DEVICE_TOKEN
TUNNEL_IP=$TUNNEL_IP
WG_IFACE=$WG_IFACE
EOF

# Dealer claim code: world-readable so a tech on the console (or the local UI) can read it to
# claim this appliance from the portal. The portal stores only its hash; this is the plaintext.
if [ -n "$CLAIM_CODE" ]; then
  ( umask 022
    printf '%s\n' "$CLAIM_CODE" > /etc/ctrlable/claim-code
    printf '%s\n' "$MAC"        > /etc/ctrlable/appliance-mac )
  log "claim code available on the host (run: ctrlable-claim)"
fi

modprobe wireguard 2>/dev/null || true
wg-quick down "$WG_IFACE" >/dev/null 2>&1 || true
if wg-quick up "$WG_IFACE"; then
  systemctl enable "wg-quick@$WG_IFACE" >/dev/null 2>&1 || true
  log "host on VPN: $WG_IFACE = $TUNNEL_IP (device $DEVICE_ID) — SSH backdoor live over the tunnel"
else
  log "WARN wg-quick up $WG_IFACE failed — config written; will retry next boot/run"
fi
exit 0
