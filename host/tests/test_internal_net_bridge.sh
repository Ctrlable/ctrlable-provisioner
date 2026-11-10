#!/usr/bin/env bash
# _bridge_usable / _bridge_has_phys in setup-internal-net.sh: a bridge that
# belongs to another network (a physical port, or a non-internal address — e.g.
# a Wi-Fi uplink bridge) is NEVER reused for the internal LAN. SPEC § 20.
#
# Run:  bash host/tests/test_internal_net_bridge.sh
#
# The script's `if [[ BASH_SOURCE == $0 ]]` guard means sourcing it defines the
# functions without running setup_internal_net. `ip` and the sysfs probe are
# stubbed so this runs anywhere (no PVE host, no /sys).

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1090
source "$HERE/../setup-internal-net.sh"
set +eu   # the script sets -euo pipefail; relax it for the test harness

fail=0
ck() {  # ck <label> <got> <want>
    if [ "$2" = "$3" ]; then echo "  ok   $1"
    else echo "  FAIL $1 -- got '$2' want '$3'"; fail=1; fi
}

# Stub the two things _bridge_usable touches: the sysfs physical-member probe
# and the `ip` command. Scenarios are encoded by the bridge NAME.
_bridge_has_phys() { [ "$1" = "wifi_phys" ] && return 0; return 1; }
ip() {
    if [ "$1" = "link" ]; then                 # ip link show <b>
        [ "$3" = "absent" ] && return 1
        return 0
    fi
    # ip -o -4 addr show dev <b>  -> last arg is the bridge
    local b; for b in "$@"; do :; done
    case "$b" in
        ours)         echo "9: ours    inet 198.18.0.1/24 scope global ours" ;;
        foreign_addr) echo "9: foreign_addr    inet 192.168.1.1/24 scope global foreign_addr" ;;
        *)            : ;;                      # no address
    esac
    return 0
}

ck "absent -> usable (we create it)"       "$(_bridge_usable absent;       echo $?)" "0"
ck "ours (internal addr) -> usable"        "$(_bridge_usable ours;         echo $?)" "0"
ck "ours (portless, no addr) -> usable"    "$(_bridge_usable ours_empty;   echo $?)" "0"
ck "physical port (Wi-Fi) -> NOT usable"   "$(_bridge_usable wifi_phys;    echo $?)" "1"
ck "foreign IPv4 -> NOT usable"            "$(_bridge_usable foreign_addr; echo $?)" "1"

echo
if [ "$fail" -ne 0 ]; then echo "FAILED"; exit 1; fi
echo "all internal-net bridge-selection checks passed"
