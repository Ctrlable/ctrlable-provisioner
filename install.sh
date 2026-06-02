#!/usr/bin/env bash
# Ctrlable Provisioner — bootstrap installer
# Run on a fresh Proxmox VE 8.x host.
#
# Remote install (production):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/ctrlable/ctrlable-provisioner/main/install.sh)"
#
# Local install (development — run from the repo root on the PVE host):
#   ./install.sh --local .

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults (override via flags)
# ---------------------------------------------------------------------------
VMID=900
LXC_HOSTNAME=ctrlable-orchestrator
MEMORY=2048
CORES=2
DISK_SIZE=8
BRIDGE=vmbr0
STORAGE=local-lvm
REPO_URL=https://github.com/ctrlable/ctrlable-provisioner
REPO_REF=main
LOCAL_SRC=""            # path to local repo checkout (--local <path>)

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
usage() {
    echo "Usage: $0 [--vmid N] [--bridge BR] [--storage ST] [--local PATH] [--repo URL] [--ref REF]"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --vmid)    VMID="$2";      shift 2 ;;
        --bridge)  BRIDGE="$2";    shift 2 ;;
        --storage) STORAGE="$2";   shift 2 ;;
        --local)   LOCAL_SRC="$2"; shift 2 ;;
        --repo)    REPO_URL="$2";  shift 2 ;;
        --ref)     REPO_REF="$2";  shift 2 ;;
        --help|-h) usage ;;
        *) echo "Unknown argument: $1"; usage ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "${CYAN}[install]${NC} $*"; }
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
die()  { echo -e "${RED}[✗] $*${NC}" >&2; exit 1; }

pct_exec() { pct exec "$VMID" -- "$@"; }
pct_push() { pct push "$VMID" "$1" "$2"; }
pct_pull() { pct pull "$VMID" "$1" "$2"; }

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
preflight() {
    command -v pct    >/dev/null 2>&1 || die "pct not found — run this on a Proxmox VE host"
    command -v pveum  >/dev/null 2>&1 || die "pveum not found"
    command -v pvesh  >/dev/null 2>&1 || die "pvesh not found"
    command -v pveam  >/dev/null 2>&1 || die "pveam not found"
    [[ $(id -u) -eq 0 ]] || die "must run as root"

    PVE_NODE=$(hostname -s)
    PVE_HOST=$(hostname -I | awk '{print $1}')
    ok "PVE node: ${PVE_NODE} (${PVE_HOST})"

    if [[ -n "$LOCAL_SRC" ]]; then
        [[ -d "$LOCAL_SRC/backend" ]] || die "--local path does not look like the provisioner repo: $LOCAL_SRC"
        LOCAL_SRC=$(realpath "$LOCAL_SRC")
        ok "local source: $LOCAL_SRC"
    fi
}

# ---------------------------------------------------------------------------
# Debian 12 LXC template
# ---------------------------------------------------------------------------
get_template() {
    log "checking for Debian 12 LXC template"
    pveam update >/dev/null 2>&1 || true
    TEMPLATE=$(pveam available --section system 2>/dev/null \
        | grep "debian-12-standard" | tail -1 | awk '{print $2}')
    [[ -n "$TEMPLATE" ]] || die "no Debian 12 standard template found in pveam"

    if ! pveam list local 2>/dev/null | grep -q "$TEMPLATE"; then
        log "downloading $TEMPLATE (this may take a minute)"
        pveam download local "$TEMPLATE"
    fi
    TEMPLATE_STOR="local:vztmpl/$TEMPLATE"
    ok "template: $TEMPLATE"
}

# ---------------------------------------------------------------------------
# Create + start orchestrator LXC
# ---------------------------------------------------------------------------
create_lxc() {
    if pct status "$VMID" >/dev/null 2>&1; then
        log "VMID $VMID already exists — resuming"
        pct start "$VMID" 2>/dev/null || true
    else
        log "creating orchestrator LXC (VMID=$VMID, storage=$STORAGE, bridge=$BRIDGE)"
        pct create "$VMID" "$TEMPLATE_STOR" \
            --hostname  "$LXC_HOSTNAME" \
            --memory    "$MEMORY" \
            --cores     "$CORES" \
            --rootfs    "${STORAGE}:${DISK_SIZE}" \
            --net0      "name=eth0,bridge=${BRIDGE},ip=dhcp" \
            --unprivileged 1 \
            --features  nesting=1 \
            --start     0
        pct start "$VMID"
    fi

    log "waiting for DHCP"
    local ip="" i
    for i in $(seq 1 40); do
        ip=$(pct exec "$VMID" -- hostname -I 2>/dev/null | awk '{print $1}') || true
        [[ -n "$ip" ]] && break
        sleep 3
    done
    [[ -n "$ip" ]] || die "LXC did not obtain an IP after 120 s"
    ORCHESTRATOR_IP="$ip"
    ok "orchestrator IP: $ORCHESTRATOR_IP"
}

# ---------------------------------------------------------------------------
# PVE API token
# ---------------------------------------------------------------------------
setup_pve_auth() {
    log "creating PVE API role and token"

    pveum role add CtrlableProvisioner \
        --privs "VM.Allocate,VM.Clone,VM.Config.CPU,VM.Config.Disk,VM.Config.Memory,VM.Config.Network,VM.Config.Options,VM.Monitor,VM.PowerMgmt,Datastore.AllocateSpace" \
        2>/dev/null || true   # idempotent; comma-separated is required by pveum

    pveum user add provisioner@pve 2>/dev/null || true
    pveum aclmod / --user provisioner@pve --role CtrlableProvisioner

    # Revoke first so re-runs don't fail on "token exists"
    pveum user token remove provisioner@pve provisioner 2>/dev/null || true
    local json
    json=$(pveum user token add provisioner@pve provisioner --privsep=0 --output-format json)
    TOKEN_SECRET=$(python3 -c "import json,sys; print(json.load(sys.stdin)['value'])" <<< "$json")
    TOKEN_ID="provisioner@pve!provisioner"
    ok "PVE token: $TOKEN_ID"
}

# ---------------------------------------------------------------------------
# Build-plane SSH key
# ---------------------------------------------------------------------------
setup_build_key() {
    log "generating build SSH key"
    mkdir -p /etc/ctrlable
    chmod 700 /etc/ctrlable

    if [[ ! -f /etc/ctrlable/build_key ]]; then
        ssh-keygen -t ed25519 -f /etc/ctrlable/build_key -N "" \
            -C "ctrlable-build@${PVE_NODE}" -q
    fi
    BUILD_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(16))")

    # Add command=-restricted entry (idempotent)
    local pubkey entry
    pubkey=$(cat /etc/ctrlable/build_key.pub)
    entry="command=\"/usr/local/bin/ctrlable-build\",restrict,no-pty ${pubkey}"
    mkdir -p /root/.ssh
    touch /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    if ! grep -qF "ctrlable-build" /root/.ssh/authorized_keys; then
        echo "$entry" >> /root/.ssh/authorized_keys
    fi
    ok "build SSH key installed"
}

# ---------------------------------------------------------------------------
# Inner LXC setup
# ---------------------------------------------------------------------------
setup_lxc() {
    log "installing packages inside LXC (this takes a few minutes)"
    pct_exec bash -c "
        set -euo pipefail
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq python3 python3-pip python3-venv git curl ca-certificates gnupg
    "

    log "installing Node.js 20 LTS"
    pct_exec bash -c "
        set -euo pipefail
        curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
            | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg
        echo 'deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main' \
            > /etc/apt/sources.list.d/nodesource.list
        apt-get update -qq
        apt-get install -y -qq nodejs
    "

    if [[ -n "$LOCAL_SRC" ]]; then
        log "packing and pushing local repo"
        tar czf /tmp/ctrlable-provisioner.tar.gz \
            --exclude='.venv' --exclude='node_modules' --exclude='__pycache__' \
            --exclude='*.pyc' --exclude='.git' --exclude='frontend/dist' \
            -C "$LOCAL_SRC" .
        pct_push /tmp/ctrlable-provisioner.tar.gz /tmp/ctrlable-provisioner.tar.gz
        pct_exec bash -c "
            mkdir -p /opt/ctrlable-provisioner
            tar xzf /tmp/ctrlable-provisioner.tar.gz -C /opt/ctrlable-provisioner
        "
        rm /tmp/ctrlable-provisioner.tar.gz
    else
        log "cloning provisioner repo (${REPO_REF})"
        pct_exec git clone --branch "$REPO_REF" "$REPO_URL" /opt/ctrlable-provisioner
    fi

    log "installing Python dependencies"
    pct_exec bash -c "
        cd /opt/ctrlable-provisioner
        python3 -m venv .venv
        .venv/bin/pip install --quiet -r backend/requirements.txt
    "

    log "building frontend"
    pct_exec bash -c "
        cd /opt/ctrlable-provisioner/frontend
        npm ci --silent
        npm run build
    "

    log "installing orchestrator systemd service"
    pct_exec bash -c "
        install -m 644 /opt/ctrlable-provisioner/deploy/ctrlable-provisioner.service \
            /etc/systemd/system/
        systemctl daemon-reload
        systemctl enable ctrlable-provisioner
    "
    ok "LXC setup complete"
}

# ---------------------------------------------------------------------------
# Write configuration
# ---------------------------------------------------------------------------
write_config() {
    log "writing orchestrator .env"
    local env_content
    env_content=$(cat <<ENV
PVE_HOST=${PVE_HOST}
PVE_TOKEN_ID=${TOKEN_ID}
PVE_TOKEN_SECRET=${TOKEN_SECRET}
PVE_NODE=${PVE_NODE}
PVE_VERIFY_SSL=false
BUILD_KEY_PATH=/etc/ctrlable/build_key
BUILD_TOKEN=${BUILD_TOKEN}
ORCHESTRATOR_URL=http://${ORCHESTRATOR_IP}:8000
ENV
)
    printf '%s\n' "$env_content" > /tmp/ctrlable.env
    pct_push /tmp/ctrlable.env /opt/ctrlable-provisioner/backend/.env
    pct_exec chmod 600 /opt/ctrlable-provisioner/backend/.env
    rm /tmp/ctrlable.env

    log "writing host build.conf"
    cat > /etc/ctrlable/build.conf <<JSON
{
  "orchestrator_url": "http://${ORCHESTRATOR_IP}:8000",
  "build_token": "${BUILD_TOKEN}"
}
JSON
    chmod 600 /etc/ctrlable/build.conf
    ok "configuration written"
}

# ---------------------------------------------------------------------------
# Install ctrlable-build on host
# ---------------------------------------------------------------------------
install_host_tools() {
    log "installing ctrlable-build on host"
    pct_pull /opt/ctrlable-provisioner/host/ctrlable-build /usr/local/bin/ctrlable-build
    chmod +x /usr/local/bin/ctrlable-build
    ok "ctrlable-build installed at /usr/local/bin/ctrlable-build"
}

# ---------------------------------------------------------------------------
# Start orchestrator
# ---------------------------------------------------------------------------
start_orchestrator() {
    log "starting orchestrator service"
    pct_exec systemctl start ctrlable-provisioner

    local i
    for i in $(seq 1 20); do
        if pct_exec curl -sf http://localhost:8000/health >/dev/null 2>&1; then
            ok "orchestrator is up"
            return 0
        fi
        sleep 3
    done
    echo "Warning: orchestrator health check timed out — check 'pct exec $VMID -- journalctl -u ctrlable-provisioner'" >&2
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print_summary() {
    echo ""
    echo -e "${BOLD}=== Ctrlable Provisioner installed ===${NC}"
    echo ""
    echo -e "  Orchestrator LXC  : ${BOLD}${VMID}${NC} (${LXC_HOSTNAME})"
    echo -e "  Web UI            : ${BOLD}http://${ORCHESTRATOR_IP}:8000${NC}"
    echo -e "  PVE API token     : ${TOKEN_ID}"
    echo -e "  Build key         : /etc/ctrlable/build_key"
    echo ""
    echo -e "${BOLD}Next steps:${NC}"
    echo "  1. Open the web UI"
    echo "  2. Go to Releases → Build release 2026.06"
    echo "  3. Wait for all LXC templates to build (~10 min)"
    echo "  4. Deploy your first stack"
    echo ""
    echo -e "  To view logs: ${CYAN}pct exec $VMID -- journalctl -fu ctrlable-provisioner${NC}"
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}Ctrlable Provisioner — bootstrap installer${NC}"
echo ""

preflight
get_template
create_lxc
setup_pve_auth
setup_build_key
setup_lxc
write_config
install_host_tools
start_orchestrator
print_summary
