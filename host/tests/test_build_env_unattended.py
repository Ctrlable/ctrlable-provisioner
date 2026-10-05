"""A non-interactive runner has to say so, in the words upstream reads.

community-scripts' build.func asks questions. It gained one -- "an update for
the Proxmox LXC stack is available: [1] upgrade [2] ignore [3] cancel" -- and
reads the answer with `read -rp ... </dev/tty`. ctrlable-build runs it over
SSH with command=/no-pty and stdin at DEVNULL, so there is no /dev/tty:

    /dev/fd/63: line 6370: /dev/tty: No such device or address
    /dev/fd/63: line 6397: _ans: unbound variable
    [ERROR] [docker-portainer] community script exited 1

Every deploy died, on a host where nothing had changed, because the question
set is upstream's to change and we were not declaring ourselves unattended.
build.func checks both of these BEFORE that read:

    local _disable_update="${DISABLE_UPDATE:-no}"
    if [[ "${PHS_SILENT:-0}" == "1" || "${_disable_update,,}" == "yes" ]]; then
      return 2      # ignore, continue without upgrading
    fi

This is also why the pinned community_scripts_ref is not the protection it
looks like: ct/<app>.sh is fetched at the pin, but its line 2 sources
build.func from main, unpinned. See the note in ctrlable-build.

Run directly:  python3 host/tests/test_build_env_unattended.py
"""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "host" / "ctrlable-build"

loader = importlib.machinery.SourceFileLoader("cb", str(BUILD))
mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("cb", loader))
loader.exec_module(mod)

# Both storages given, so build_env does not shell out to detect them.
CONF = {"container_storage": "local-lvm", "template_storage": "local"}


def env_for(tmpl=None):
    return mod.build_env(tmpl or {"name": "docker-portainer", "kind": "lxc"},
                         CONF, vmid=109)


def test_declares_itself_unattended():
    env = env_for()
    assert env.get("DISABLE_UPDATE", "").lower() == "yes", (
        "without DISABLE_UPDATE=yes build.func reaches `read </dev/tty` and "
        "the deploy dies on an unbound $_ans")
    assert env.get("PHS_SILENT") == "1", (
        "PHS_SILENT is the general 'ask me nothing' flag; without it the next "
        "prompt upstream adds breaks every deploy again")


def test_still_noninteractive_for_debian_and_the_method():
    """The flags that were already right must stay right."""
    env = env_for()
    assert env["DEBIAN_FRONTEND"] == "noninteractive"
    assert env["METHOD"] == "default"
    assert env["mode"] == "1"


def test_does_not_choose_to_upgrade_the_hypervisor():
    """Option 1 of that prompt runs apt upgrade on the HOST. Creating a
    container must never do that as a side effect, so nothing in the env may
    opt into it."""
    env = env_for()
    for k, v in env.items():
        if k in ("DISABLE_UPDATE", "PHS_SILENT"):
            continue
        assert "upgrade" not in str(v).lower(), (
            "env %s=%r looks like it opts into a host upgrade" % (k, v))
    assert env.get("DISABLE_UPDATE", "").lower() != "no"


def test_storages_are_passed_through():
    env = env_for()
    assert env["var_container_storage"] == "local-lvm"
    assert env["var_template_storage"] == "local"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print("ok   %s" % name)
        except AssertionError as exc:
            failures += 1
            print("FAIL %s: %s" % (name, exc))
    print("all passed" if not failures else "%d FAILED" % failures)
    sys.exit(1 if failures else 0)
