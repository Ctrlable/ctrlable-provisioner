#!/usr/bin/env bash
#
# The management-interface conversion in install.sh, exercised on real files.
#
# It rewrites the networking of a hypervisor that may be a thousand miles from
# anyone who could fix it, so the cases that matter most are the ones where it
# must NOT act: a host already on DHCP, and a second bridge carrying something
# else.
#
#     bash tests/test-network-transform.sh
#
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The same two steps install.sh performs: find the address, then rewrite.
run_transform() {
    local f="$1" keep
    keep=$(awk '/^[[:space:]]*iface[[:space:]]+vmbr0[[:space:]]+inet[[:space:]]+static/{s=1;next}
                s && /^[[:space:]]*iface[[:space:]]/{exit}
                s && /^[[:space:]]*address[[:space:]]/{print $2; exit}' "$f")
    awk -v keep="$keep" '
      /^[[:space:]]*iface[[:space:]]+vmbr0[[:space:]]+inet[[:space:]]+static/ {
          print "iface vmbr0 inet dhcp"
          if (keep != "") { print "        post-up ip addr add " keep " dev vmbr0 || true" }
          s = 1; next
      }
      s && /^[[:space:]]*(auto|iface|source)[[:space:]]/ { s = 0 }
      s && /^[[:space:]]*(address|gateway)[[:space:]]/   { next }
      { print }
    ' "$f"
}

fail=0
expect()     { grep -q "$2" <<<"$1" || { echo "- $3: expected /$2/"; fail=1; }; }
expect_not() { grep -q "$2" <<<"$1" && { echo "- $3: should not contain /$2/"; fail=1; }; return 0; }

# 1. The appliance that prompted this: static, with a gateway that does not
#    exist on the LAN it was shipped to.
out=$(run_transform "$HERE/interfaces")
expect     "$out" "iface vmbr0 inet dhcp"               "static bridge becomes dhcp"
expect_not "$out" "iface vmbr0 inet static"             "static stanza replaced"
expect     "$out" "post-up ip addr add 172.16.0.115/23" "old address retained"
expect_not "$out" "gateway 172.16.0.1"                  "dead gateway dropped"
expect     "$out" "bridge-ports nic0"                   "bridge ports preserved"
expect     "$out" "iface nic1 inet manual"              "other interfaces preserved"

# 2. Already on DHCP: nothing to do, and nothing mangled.
cat > "$WORK/already" <<'EOF'
auto vmbr0
iface vmbr0 inet dhcp
        bridge-ports nic0
EOF
out=$(run_transform "$WORK/already")
expect     "$out" "iface vmbr0 inet dhcp" "already-dhcp untouched"
expect_not "$out" "post-up"               "no duplicate post-up added"

# 3. A second bridge — a camera or AV VLAN — must survive exactly as written.
cat > "$WORK/twobridges" <<'EOF'
auto vmbr0
iface vmbr0 inet static
        address 172.16.0.115/23
        gateway 172.16.0.1
        bridge-ports nic0

auto vmbr1
iface vmbr1 inet static
        address 10.99.0.1/24
        bridge-ports nic1
EOF
out=$(run_transform "$WORK/twobridges")
expect "$out" "iface vmbr1 inet static" "only vmbr0 is converted"
expect "$out" "address 10.99.0.1/24"    "the other bridge keeps its address"
expect "$out" "iface vmbr0 inet dhcp"   "vmbr0 still converted alongside it"

if [[ $fail -eq 0 ]]; then
    echo "network transform: all cases pass"
fi
exit $fail
