#!/usr/bin/env bash
# Ctrlable appliance bootstrap -- fresh Proxmox VE host to a deployed Ctrlable stack.
#
# Run as root on the Proxmox host:
#
#   export CTRLABLE_PORTAL_PASSWORD='...'   # portal enrolment (kept out of ps/history)
#   export GITHUB_TOKEN='...'               # admin:public_key -- registers read-only
#                                           # deploy keys for the private builders
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/ctrlable/ctrlable-provisioner/main/bootstrap-appliance.sh)" \
#     _ --portal-email info@ctrlable.com --site-name "Client Name"
#
# Steps (each is safe to repeat; re-running the whole thing is the recovery path):
#   1. Proxmox: enterprise repos -> no-subscription, full upgrade
#   2. Firmware: report UEFI / Secure Boot / VT-x / VT-d; set power-on-after-AC-loss
#      through IPMI when there is a BMC, otherwise say exactly what to set in setup
#   3. install.sh: management network static -> DHCP, orchestrator LXC, portal
#      enrolment, deploy keys
#   4. Orchestrator API: admin password, release preflight, deploy every template
#      in the release, wait, report
#
# Options:
#   --site-name NAME        project/site name (default: this host's name)
#   --release R             release manifest to deploy (default 2026.11)
#   --portal-email EMAIL    with $CTRLABLE_PORTAL_PASSWORD: auto-enrol in the portal
#   --github-token PAT      or $GITHUB_TOKEN
#   --ref REF               provisioner git ref for install.sh + orchestrator (default main)
#   --mqtt-host H --mqtt-port P --mqtt-user U --mqtt-pass P   site broker, if known
#   --skip-upgrade          leave packages alone
#   --skip-deploy           stop after the orchestrator is installed
#   --reboot                reboot at the end if a new kernel was installed
#
# A kernel upgrade is followed by an IMMEDIATE reboot, before anything is
# installed, and the run resumes by itself afterwards (systemd unit
# ctrlable-bootstrap-resume; log /var/log/ctrlable-bootstrap.log). Creating
# unprivileged containers fails on the old running kernel once the new
# Proxmox userspace is in ("tar: .../rootfs: Cannot open: Permission denied").
# Env:
#   CTRLABLE_ADMIN_PASSWORD  orchestrator admin password to set (default: generated,
#                            saved to /root/ctrlable-admin-password)
set -euo pipefail

SITE_NAME="$(hostname -s)"
RELEASE="2026.11"
PORTAL_EMAIL=""
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
REF="main"
MQTT_HOST="" MQTT_PORT=1883 MQTT_USER="" MQTT_PASS=""
SKIP_UPGRADE=0 SKIP_DEPLOY=0 DO_REBOOT=0 RESUMED=0
ORIG_ARGS=("$@")
RAW="https://raw.githubusercontent.com/ctrlable/ctrlable-provisioner"

while [[ $# -gt 0 ]]; do
    case $1 in
        --site-name)    SITE_NAME="$2"; shift 2 ;;
        --release)      RELEASE="$2"; shift 2 ;;
        --portal-email) PORTAL_EMAIL="$2"; shift 2 ;;
        --github-token) GITHUB_TOKEN="$2"; shift 2 ;;
        --ref)          REF="$2"; shift 2 ;;
        --mqtt-host)    MQTT_HOST="$2"; shift 2 ;;
        --mqtt-port)    MQTT_PORT="$2"; shift 2 ;;
        --mqtt-user)    MQTT_USER="$2"; shift 2 ;;
        --mqtt-pass)    MQTT_PASS="$2"; shift 2 ;;
        --skip-upgrade) SKIP_UPGRADE=1; shift ;;
        --skip-deploy)  SKIP_DEPLOY=1; shift ;;
        --reboot)       DO_REBOOT=1; shift ;;
        --resumed)      RESUMED=1; shift ;;
        -h|--help)      sed -n '2,40p' "$0" 2>/dev/null || true; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

BOLD='\033[1m'; CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; NC='\033[0m'
step() { echo -e "\n${BOLD}== $*${NC}"; }
log()  { echo -e "${CYAN}[bootstrap]${NC} $*"; }
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; WARNINGS+=("$*"); }
die()  { echo -e "${RED}[✗] $*${NC}" >&2; exit 1; }
WARNINGS=()

[[ $(id -u) -eq 0 ]] || die "run as root"
command -v pveversion >/dev/null || die "not a Proxmox VE host"
ok "$(pveversion)"

# ---------------------------------------------------------------------------
step "1/4 Proxmox repositories and updates"
# ---------------------------------------------------------------------------
CODENAME=$(. /etc/os-release; echo "$VERSION_CODENAME")
disable_enterprise() {
    local f
    # deb822 (.sources, PVE 9): Enabled: no
    for f in /etc/apt/sources.list.d/*.sources; do
        [[ -f "$f" ]] || continue
        if grep -q "enterprise.proxmox.com" "$f" && ! grep -qi "^Enabled: *no" "$f"; then
            echo "Enabled: no" >> "$f"; log "disabled $(basename "$f") (needs a subscription)"
        fi
    done
    # one-line (.list, PVE 8): comment out
    for f in /etc/apt/sources.list.d/*.list; do
        [[ -f "$f" ]] || continue
        if grep -q "^deb .*enterprise.proxmox.com" "$f"; then
            sed -i 's|^deb \(.*enterprise.proxmox.com\)|# deb \1|' "$f"; log "disabled $(basename "$f")"
        fi
    done
}
if [[ "$SKIP_UPGRADE" == 1 ]]; then
    log "--skip-upgrade: leaving packages alone"
else
    disable_enterprise
    if ! grep -rqs "pve-no-subscription" /etc/apt/sources.list /etc/apt/sources.list.d/; then
        cat > /etc/apt/sources.list.d/pve-no-subscription.sources <<EOF
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: ${CODENAME}
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF
        log "added pve-no-subscription (${CODENAME})"
    fi
    export DEBIAN_FRONTEND=noninteractive
    n=1; until apt-get update -qq; do (( n++ >= 5 )) && die "apt-get update keeps failing"; sleep $((n * 5)); done
    upgradable=$(apt list --upgradable 2>/dev/null | grep -c / || true)
    log "upgrading ${upgradable} packages (this can take a while)"
    n=1
    until apt-get -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
            dist-upgrade -y -qq >/var/log/ctrlable-bootstrap-upgrade.log 2>&1; do
        (( n++ >= 3 )) && die "upgrade failed -- see /var/log/ctrlable-bootstrap-upgrade.log"
        dpkg --configure -a >/dev/null 2>&1 || true; sleep 10
    done
    ok "Proxmox up to date: $(pveversion | cut -d/ -f2)"
fi
NEWEST_KERNEL=$(ls -1 /boot/vmlinuz-*-pve 2>/dev/null | sed 's|/boot/vmlinuz-||' | sort -V | tail -1)
REBOOT_NEEDED=0
if [[ -n "$NEWEST_KERNEL" && "$NEWEST_KERNEL" != "$(uname -r)" ]]; then
    REBOOT_NEEDED=1
fi

# Reboot into the new kernel NOW and resume afterwards. (Observed 2026-11-03 on
# a fresh 9.1 -> 9.2.25 upgrade: the orchestrator CT failed to extract until
# the host was running the new kernel.)
if [[ ( "$REBOOT_NEEDED" == 1 || -n "${BOOTSTRAP_FORCE_RESUME_TEST:-}" ) && "$RESUMED" == 0 ]]; then
    step "Rebooting into ${NEWEST_KERNEL} -- the bootstrap continues by itself afterwards"
    install -d -m 700 /var/lib/ctrlable-bootstrap
    # The script itself: a file when run as one, else the "bash -c" string.
    if [[ -f "$0" && "$0" == *.sh ]]; then
        cp "$0" /var/lib/ctrlable-bootstrap/bootstrap-appliance.sh
    elif [[ -n "${BASH_EXECUTION_STRING:-}" ]]; then
        printf '%s\n' "$BASH_EXECUTION_STRING" > /var/lib/ctrlable-bootstrap/bootstrap-appliance.sh
    else
        curl -fsSL "${RAW}/${REF}/bootstrap-appliance.sh" -o /var/lib/ctrlable-bootstrap/bootstrap-appliance.sh \
            || die "cannot save the bootstrap for resuming"
    fi
    # Arguments and secrets for the resumed run (root-only; deleted on resume).
    ( umask 077
      {
        printf 'ARGS=('; printf '%q ' "${ORIG_ARGS[@]}"; printf ')\n'
        [[ -n "${CTRLABLE_PORTAL_PASSWORD:-}" ]] && printf 'export CTRLABLE_PORTAL_PASSWORD=%q\n' "$CTRLABLE_PORTAL_PASSWORD"
        [[ -n "$GITHUB_TOKEN" ]] && printf 'export GITHUB_TOKEN=%q\n' "$GITHUB_TOKEN"
        [[ -n "${CTRLABLE_ADMIN_PASSWORD:-}" ]] && printf 'export CTRLABLE_ADMIN_PASSWORD=%q\n' "$CTRLABLE_ADMIN_PASSWORD"
        true   # an unset last secret must not fail the block under set -e
      } > /var/lib/ctrlable-bootstrap/resume.env )
    cat > /var/lib/ctrlable-bootstrap/resume.sh <<'EOF'
#!/bin/bash
systemctl disable ctrlable-bootstrap-resume.service >/dev/null 2>&1
. /var/lib/ctrlable-bootstrap/resume.env
rm -f /var/lib/ctrlable-bootstrap/resume.env
exec bash /var/lib/ctrlable-bootstrap/bootstrap-appliance.sh "${ARGS[@]}" --resumed
EOF
    cat > /etc/systemd/system/ctrlable-bootstrap-resume.service <<'EOF'
[Unit]
Description=Ctrlable appliance bootstrap (resume after kernel reboot)
Wants=network-online.target
After=network-online.target pve-cluster.service pvedaemon.service

[Service]
Type=simple
ExecStart=/bin/bash -c 'bash /var/lib/ctrlable-bootstrap/resume.sh >> /var/log/ctrlable-bootstrap.log 2>&1'
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable ctrlable-bootstrap-resume.service >/dev/null 2>&1
    echo "  Follow it after the reboot with:  tail -f /var/log/ctrlable-bootstrap.log"
    sleep 5
    systemctl reboot
    exit 0
fi
[[ "$RESUMED" == 1 ]] && ok "resumed after reboot, running $(uname -r)"
[[ "$REBOOT_NEEDED" == 1 ]] && warn "still running $(uname -r) although ${NEWEST_KERNEL} is installed -- reboot when convenient"

# ---------------------------------------------------------------------------
step "2/4 Firmware settings"
# ---------------------------------------------------------------------------
[[ -d /sys/firmware/efi ]] && ok "boot mode: UEFI" || warn "boot mode: legacy BIOS -- UEFI recommended"

if command -v mokutil >/dev/null; then
    sb=$(mokutil --sb-state 2>&1 | head -1)
    case "$sb" in
        *disabled*) ok "Secure Boot: disabled" ;;
        *enabled*)  warn "Secure Boot is ENABLED -- disable it in firmware setup (Security -> Secure Boot) for the Ctrlable environment" ;;
        *)          log "Secure Boot: ${sb}" ;;
    esac
fi

if grep -qwE "vmx|svm" /proc/cpuinfo && [[ -e /dev/kvm ]]; then
    ok "CPU virtualization (VT-x/AMD-V): enabled"
else
    warn "CPU virtualization is OFF -- enable VT-x / SVM in firmware setup; the Ctrlable Pro VM cannot start without it"
fi
if dmesg 2>/dev/null | grep -qE "DMAR: IOMMU enabled|AMD-Vi: .*IOMMU|DMAR-IR: Enabled"; then
    ok "IOMMU (VT-d/AMD-Vi): enabled"
else
    log "IOMMU (VT-d) not reported -- only needed for PCI passthrough (e.g. a Coral M.2/PCIe)"
fi

# Power on after AC loss. With a BMC this is one IPMI call; without one there is
# no safe, vendor-neutral way to change it from the OS (it lives in the
# firmware's own NVRAM "Setup" variable, at a board-specific offset).
modprobe -q ipmi_si 2>/dev/null || true
modprobe -q ipmi_devintf 2>/dev/null || true
if ls /dev/ipmi* >/dev/null 2>&1; then
    command -v ipmitool >/dev/null || apt-get install -y -qq ipmitool >/dev/null 2>&1 || true
    if ipmitool chassis policy always-on >/dev/null 2>&1; then
        ok "power restore policy: always-on (set via BMC)"
    else
        warn "BMC present but setting the power restore policy failed -- set 'Restore on AC power loss: Power On' in the BMC/firmware"
    fi
else
    vendor=$(cat /sys/class/dmi/id/board_vendor 2>/dev/null)
    warn "no BMC: set power-on-after-AC-loss ONCE in firmware setup (${vendor:-AMI}: Chipset -> South Bridge/PCH -> 'Restore AC Power Loss' / 'State After G3' = Power On / S0)"
fi

# ---------------------------------------------------------------------------
step "3/4 Ctrlable provisioner (network -> DHCP, orchestrator, enrolment)"
# ---------------------------------------------------------------------------
install_args=(--ref "$REF")
if [[ -n "$PORTAL_EMAIL" && -z "${CTRLABLE_PORTAL_PASSWORD:-}" ]]; then
    warn "--portal-email without CTRLABLE_PORTAL_PASSWORD -- not enrolling in the portal"
elif [[ -n "$PORTAL_EMAIL" ]]; then
    install_args+=(--portal-email "$PORTAL_EMAIL")
fi
[[ -n "$GITHUB_TOKEN" ]] && install_args+=(--github-token "$GITHUB_TOKEN")
installer=$(curl -fsSL "${RAW}/${REF}/install.sh") || die "could not download install.sh @ ${REF}"
bash -c "$installer" _ "${install_args[@]}" || die "install.sh failed"

[[ "$SKIP_DEPLOY" == 1 ]] && { ok "--skip-deploy: stopping after the orchestrator"; exit 0; }

# ---------------------------------------------------------------------------
step "4/4 Deploy release ${RELEASE}"
# ---------------------------------------------------------------------------
ORCH_IP=$(pct exec 900 -- hostname -I | awk '{print $1}')
API="http://${ORCH_IP}:8000"
for _ in $(seq 1 30); do curl -sf "$API/health" >/dev/null && break; sleep 2; done
curl -sf "$API/health" >/dev/null || die "orchestrator not answering at $API"

jfield() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get(sys.argv[1], ''))" "$1"; }
api() {  # api METHOD PATH [JSON]
    local args=(-s -X "$1" -H "Content-Type: application/json")
    [[ -n "$TOKEN" ]] && args+=(-H "Authorization: Bearer $TOKEN")
    [[ $# -ge 3 ]] && args+=(-d "$3")
    curl "${args[@]}" "$API$2"
}
TOKEN=""

PWFILE=/root/ctrlable-admin-password
ADMIN_PW="${CTRLABLE_ADMIN_PASSWORD:-}"
[[ -z "$ADMIN_PW" && -s "$PWFILE" ]] && ADMIN_PW=$(cat "$PWFILE")
setup_resp=""
if [[ -z "$ADMIN_PW" ]]; then
    ADMIN_PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(12))")
fi
body=$(python3 -c "import json,sys; print(json.dumps({'new_password': sys.argv[1]}))" "$ADMIN_PW")
setup_resp=$(api POST /api/auth/setup "$body")
if grep -q '"token"' <<< "$setup_resp"; then
    ( umask 077; printf '%s\n' "$ADMIN_PW" > "$PWFILE" )
    ok "orchestrator admin password set (saved to $PWFILE)"
fi
body=$(python3 -c "import json,sys; print(json.dumps({'username':'admin','password':sys.argv[1]}))" "$ADMIN_PW")
TOKEN=$(api POST /api/auth/login "$body" | jfield token) || true
[[ -n "$TOKEN" ]] || die "cannot log in to the orchestrator -- set CTRLABLE_ADMIN_PASSWORD to its current admin password"

# Already deployed? Re-running must not build a second stack.
existing=$(api GET /api/projects | python3 -c "
import json,sys
ps=[p for p in json.load(sys.stdin) if p.get('release')==sys.argv[1]]
print(ps[-1]['id'] if ps else '')" "$RELEASE")

if [[ -z "$existing" ]]; then
    log "preflight for ${RELEASE} (checks this host; creates nothing)"
    resp=$(api POST /api/builds "{\"release\":\"${RELEASE}\"}")
    build_id=$(jfield build_id <<< "$resp") || true
    [[ -n "$build_id" ]] || die "could not start preflight: $resp"
    for _ in $(seq 1 120); do
        st=$(api GET "/api/builds/$build_id" | jfield status)
        [[ "$st" != "running" ]] && break; sleep 5
    done
    if [[ "$st" != "success" ]]; then
        api GET "/api/builds/$build_id" | jfield log | grep -E "FAIL|ERROR" | sed 's/^/    /'
        die "preflight failed -- fix the checks above and re-run"
    fi
    ok "preflight passed"

    body=$(python3 -c "
import json,sys
print(json.dumps({'site_name':sys.argv[1],'release':sys.argv[2],'mqtt_host':sys.argv[3],
  'mqtt_port':int(sys.argv[4]),'mqtt_user':sys.argv[5],'mqtt_pass':sys.argv[6],
  'zigbee_coordinator':'','zwave_coordinator':''}))" \
        "$SITE_NAME" "$RELEASE" "$MQTT_HOST" "$MQTT_PORT" "$MQTT_USER" "$MQTT_PASS")
    project=$(api POST /api/projects "$body" | jfield project_id)
    [[ -n "$project" ]] || die "could not start the deploy"
    log "deploying project ${project} -- one service at a time, ~30-60 min"
else
    project=$existing
    log "release ${RELEASE} already has project ${project} -- not deploying again, reporting it"
fi

EXPECTED=$(pct exec 900 -- /opt/ctrlable-provisioner/.venv/bin/python -c "
import yaml,sys; print(len(yaml.safe_load(open('/opt/ctrlable-provisioner/releases/'+sys.argv[1]+'.yaml'))['templates']))" "$RELEASE")
last=""
for _ in $(seq 1 1080); do   # up to 3 h
    snap=$(api GET "/api/projects/$project" | python3 -c "
import json,sys
p=json.load(sys.stdin); ins=p.get('instances',[])
print(len(ins), sum(i['status']=='provisioning' for i in ins), ' '.join(f\"{i['type']}={i['status']}\" for i in ins))")
    read -r total busy states <<< "$snap"
    [[ "$states" != "$last" ]] && { log "${states}"; last="$states"; }
    (( total >= EXPECTED && busy == 0 )) && break
    sleep 10
done

# Everything must come back by itself after a power cut: the orchestrator is
# created without onboot by install.sh, and community scripts differ.
for id in $(pct list | awk 'NR>1{print $1}'); do pct set "$id" --onboot 1 >/dev/null 2>&1 || true; done
for id in $(qm list | awk 'NR>1{print $1}'); do qm set "$id" --onboot 1 >/dev/null 2>&1 || true; done
ok "every guest set to start on boot"

echo -e "\n${BOLD}=== Ctrlable appliance: ${SITE_NAME} (release ${RELEASE}) ===${NC}"
api GET "/api/projects/$project" | python3 -c "
import json,sys
for i in json.load(sys.stdin).get('instances',[]):
    mark={'active':'OK   ','error':'ERROR','provisioning':'BUSY ','pending':'WAIT '}.get(i['status'],i['status'])
    print(f\"  {mark} {i['type']:17} vmid {i['vmid']:<5} {i['hostname']}\")"
failed=$(api GET "/api/projects/$project" | python3 -c "
import json,sys; print(sum(i['status']=='error' for i in json.load(sys.stdin).get('instances',[])))")
echo
echo "  Orchestrator : http://${ORCH_IP}:8000  (admin / see $PWFILE)"
echo "  Proxmox      : https://$(hostname -I | awk '{print $1}'):8006"
[[ -z "$MQTT_HOST" ]] && warn "no MQTT broker given: Zigbee2MQTT / Frigate / DALI wait for one -- set it up in Ctrlable Pro (Mosquitto) and re-wire"
for w in "${WARNINGS[@]}"; do echo -e "  ${YELLOW}!${NC} $w"; done
(( failed > 0 )) && echo -e "  ${RED}${failed} service(s) failed${NC} -- details in the orchestrator UI (Projects), then re-run this command"

if [[ "$REBOOT_NEEDED" == 1 ]]; then
    if [[ "$DO_REBOOT" == 1 ]]; then log "rebooting into ${NEWEST_KERNEL} in 10 s"; sleep 10; systemctl reboot
    else echo "  Reboot to finish the Proxmox update (kernel ${NEWEST_KERNEL}); every guest is set to start on boot."; fi
fi
(( failed == 0 ))
