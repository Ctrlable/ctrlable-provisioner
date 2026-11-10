"""install.sh (bootstrap) and host/setup-internal-net.sh (remote enable) must
set up the internal LAN identically.

Run:  python3 host/tests/test_internal_net_sync.py

There are deliberately two copies of `setup_internal_net`: install.sh runs
piped from curl with no repo on the host (before host tools exist), so it
cannot source the script; the script is what `ctrlable-build enable-internal-net`
runs on an already-deployed host. Two copies of fragile host-networking is
exactly what bit this project before, so this pins them byte-for-byte: the
`_subnet_in_use` and `setup_internal_net` function bodies must match.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{('  -- ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


def extract_func(text: str, name: str) -> str | None:
    """Body of a top-level `name() { ... }` bash function, brace-matched."""
    m = re.search(rf"^{re.escape(name)}\(\)\s*\{{", text, re.MULTILINE)
    if not m:
        return None
    i = m.end() - 1  # at the opening brace
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j].strip("\n")
    return None


install_sh = open(os.path.join(ROOT, "install.sh")).read()
script = open(os.path.join(ROOT, "host", "setup-internal-net.sh")).read()

for fn in ("_subnet_in_use", "_bridge_has_phys", "_bridge_usable", "setup_internal_net"):
    a = extract_func(install_sh, fn)
    b = extract_func(script, fn)
    check(f"install.sh defines {fn}", a is not None)
    check(f"setup-internal-net.sh defines {fn}", b is not None)
    if a is not None and b is not None:
        check(f"{fn} bodies are byte-identical", a == b,
              "DRIFT — update both copies in lockstep")

# And the invariants learned the hard way (2026-11-09) are actually present in
# the script, so a future "tidy-up" that drops them fails here too.
for needle in ("port=0", "bind-dynamic",
               "dhcp-hostsfile=$resv", "/var/lib/misc/ctrlable-reservations",
               "chmod 644"):
    check(f"script keeps invariant: {needle}", needle in script, needle)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("all internal-net sync checks passed")
