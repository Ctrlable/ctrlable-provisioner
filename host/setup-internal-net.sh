#!/usr/bin/env bash
# Ctrlable internal management LAN — host setup (SPEC § 20).
#
# Creates the isolated `vmbr1` bridge and a DHCP-only dnsmasq that serves
# reserved leases to the guests ctrlable-build attaches to it. This is the
# EXACT same setup install.sh runs at bootstrap, factored out so an ALREADY
# DEPLOYED appliance can gain the internal LAN without a reinstall:
# `ctrlable-build enable-internal-net` invokes this on the host.
#
# install.sh keeps its own inline copy because it runs piped from curl with no
# repo on the host yet (it runs BEFORE host tools are installed). The two must
# stay in lockstep; host/tests/test_internal_net_sync.py asserts they do.
#
# Idempotent and safe to re-run: an existing bridge or dnsmasq config is left
# in place, the reservations file is created-not-truncated.
#
# Env:
#   INTERNAL_SUBNET=198.18.0.0/24   preferred /24 (falls back if in use)
#   NO_INTERNAL_NET=1               do nothing (opt out)
set -euo pipefail

# Minimal standalone log helpers (install.sh supplies its own; here we are run
# directly by ctrlable-build, which captures stdout/stderr into the build log).
if ! declare -F log >/dev/null 2>&1; then
    log()  { echo "[internal-net] $*"; }
    ok()   { echo "[internal-net] [ok] $*"; }
    warn() { echo "[internal-net] [!] $*" >&2; }
fi

_subnet_in_use() {
    local pfx="${1%%/*}"; pfx="${pfx%.*}."          # 198.18.0.0/24 -> 198.18.0.
    local re="${pfx//./\\.}"
    ip -o -4 addr show 2>/dev/null | grep -qE "inet ${re}" && return 0
    ip -4 route show 2>/dev/null   | grep -qE "(^| )${re}" && return 0
    return 1
}

# True if <bridge> bridges a physical NIC — a real port, not a guest tap/veth or
# a PVE firewall iface. Such a bridge is another network's (e.g. a Wi-Fi uplink).
_bridge_has_phys() {
    local b="$1" m
    for m in "/sys/class/net/$b/brif/"*; do
        [ -e "$m" ] || continue
        case "${m##*/}" in
            tap*|veth*|fwln*|fwpr*|fwbr*) ;;                # virtual guest/fw ports
            *) return 0 ;;                                  # a real NIC -> foreign
        esac
    done
    return 1
}

# True if <bridge> is free for the internal LAN: absent (we create it), or
# already ours (no physical port and no non-internal address). A bridge with a
# physical port or a foreign IPv4 — e.g. a future Wi-Fi uplink bridge — is left
# ALONE: reusing it would run a DHCP server onto, or attach guests to, the
# customer's own network. SPEC § 20.
_bridge_usable() {
    local b="$1"
    ip link show "$b" >/dev/null 2>&1 || return 0          # absent -> we create it
    _bridge_has_phys "$b" && return 1                       # real port -> foreign
    ip -o -4 addr show dev "$b" 2>/dev/null | awk '{print $4}' \
        | grep -qvE '^198\.1[89]\.' && return 1             # non-internal addr -> foreign
    return 0
}

setup_internal_net() {
    if [[ "${NO_INTERNAL_NET:-0}" == "1" ]]; then
        log "internal management LAN disabled (NO_INTERNAL_NET=1)"; return 0
    fi
    local bridge="" b chosen="" c
    for c in "${INTERNAL_SUBNET:-198.18.0.0/24}" 198.19.0.0/24 198.18.1.0/24; do
        _subnet_in_use "$c" || { chosen="$c"; break; }
    done
    if [[ -z "$chosen" ]]; then
        warn "internal LAN: every candidate subnet is already in use here — skipping"
        warn "  (set INTERNAL_SUBNET to a free /24, or NO_INTERNAL_NET=1 to silence)"
        return 0
    fi
    [[ "$chosen" == "${INTERNAL_SUBNET:-198.18.0.0/24}" ]] \
        || warn "internal LAN: preferred subnet was in use; using $chosen"

    # Pick a bridge that is ours or free. A pre-existing vmbr1 that belongs to
    # another network (e.g. a Wi-Fi uplink) is NOT reused — we relocate to the
    # next free vmbrN, or skip entirely rather than disturb it. SPEC § 20.
    for b in vmbr1 vmbr2 vmbr3; do
        _bridge_usable "$b" && { bridge="$b"; break; }
    done
    if [[ -z "$bridge" ]]; then
        warn "internal LAN: vmbr1..vmbr3 are all in use by other networks —"
        warn "  refusing to reuse one. Free a vmbrN (or set one up by hand on an"
        warn "  internal 198.18/198.19 subnet) and re-run. Existing bridges untouched."
        return 0
    fi
    [[ "$bridge" == vmbr1 ]] \
        || warn "internal LAN: vmbr1 is in use by another network; using $bridge"

    local base="${chosen%%/*}"; base="${base%.*}"   # 198.18.0
    local gw="${base}.1" prefix="${chosen##*/}"

    mkdir -p /etc/ctrlable
    printf 'subnet=%s\nbridge=%s\ngateway=%s\n' "$chosen" "$bridge" "$gw" \
        > /etc/ctrlable/internal-net.conf

    # 1) the bridge — its own interfaces.d file, no physical ports
    local ifd=/etc/network/interfaces.d
    mkdir -p "$ifd"
    grep -qF "source /etc/network/interfaces.d/" /etc/network/interfaces 2>/dev/null \
        || echo "source /etc/network/interfaces.d/*" >> /etc/network/interfaces
    if ! ip link show "$bridge" >/dev/null 2>&1; then
        cat > "$ifd/ctrlable-internal" <<EOF
# Ctrlable internal management LAN (SPEC § 20) — isolated, no physical ports.
auto $bridge
iface $bridge inet static
    address $gw/$prefix
    bridge-ports none
    bridge-stp off
    bridge-fd 0
EOF
        ifreload -a 2>/dev/null || ifup "$bridge" 2>/dev/null || true
    fi
    ip link show "$bridge" >/dev/null 2>&1 \
        || { warn "internal LAN: $bridge did not come up — skipping dnsmasq"; return 0; }
    ok "internal bridge $bridge up at $gw"

    # 2) dnsmasq, DHCP-only (port=0) bound to the internal bridge only, for the
    #    HAOS VM's reserved lease (ctrlable-build drops per-VM dhcp-host files here).
    if ! command -v dnsmasq >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y dnsmasq >/dev/null 2>&1 \
            || { warn "internal LAN: dnsmasq install failed — LXCs still get a static net1, the VM lease won't be served"; return 0; }
    fi
    # Per-guest reservations go in a dhcp-hostsfile, NOT a config drop-in:
    # dnsmasq reads `dhcp-host=` from config files only at startup, but re-reads
    # a dhcp-hostsfile on reload. It lives in /var/lib/misc (where dnsmasq
    # already reads its lease file) so the unprivileged dnsmasq can read it —
    # /etc/ctrlable is 700 for build.conf. Created, never truncated, on re-run.
    local resv=/var/lib/misc/ctrlable-reservations
    [[ -f "$resv" ]] || : > "$resv"
    chmod 644 "$resv"
    cat > /etc/dnsmasq.d/ctrlable-internal.conf <<EOF
# Ctrlable internal LAN — DHCP only, bound to $bridge (SPEC § 20).
port=0
interface=$bridge
bind-dynamic
dhcp-range=${base}.100,${base}.200,255.255.255.0,12h
dhcp-authoritative
dhcp-hostsfile=$resv
EOF
    systemctl enable dnsmasq >/dev/null 2>&1 || true
    systemctl restart dnsmasq 2>/dev/null || true
    if systemctl is-active --quiet dnsmasq; then
        ok "internal LAN DHCP (dnsmasq) serving $chosen on $bridge"
    else
        warn "internal LAN: dnsmasq did not start — see 'journalctl -u dnsmasq'"
    fi
}

# When run directly (not sourced), do the setup.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    setup_internal_net
fi
