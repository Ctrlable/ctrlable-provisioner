"""A community script that creates nothing must FAIL, never adopt an existing CT.

2026-11-03: freepbx's script 404'd and created no container; the fallback took
the newest existing container -- the Frigate CT built minutes before -- renamed
it to freepbx and overwrote its firstboot config. Executes deploy_lxc with the
host calls stubbed.
"""
import importlib.machinery, importlib.util, pathlib, sys, types

ROOT = pathlib.Path(__file__).resolve().parents[2]
path = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "host" / "ctrlable-build")
loader = importlib.machinery.SourceFileLoader("cb", path)
mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("cb", loader))
loader.exec_module(mod)


class Died(Exception):
    pass


def harness(existing, created):
    state = {"cts": set(existing), "renamed": []}
    mod.next_vmid = lambda: 102
    mod.list_ct_vmids = lambda: set(state["cts"])
    mod.fetch_text = lambda url, *a, **k: "#!/bin/bash\nexit 0\n"
    mod.cs_pin_dir = getattr(mod, "cs_pin_dir", lambda ref: "/tmp")
    def fake_run(cmd, *a, **k):
        # only the community script itself creates containers (build_env's
        # pvesm calls go through here too)
        if cmd and cmd[0] == "bash":
            state["cts"] |= set(created)
        return types.SimpleNamespace(returncode=0, stdout=b"")
    mod.subprocess = types.SimpleNamespace(run=fake_run, DEVNULL=None, PIPE=None, STDOUT=None)
    mod.is_running = lambda v: True
    mod.set_lxc_hostname = lambda v, h: state["renamed"].append((v, h))
    mod.inject_firstboot = lambda *a, **k: None
    mod.phase = lambda *a, **k: None
    mod.log = lambda *a, **k: None
    def die(msg):
        raise Died(msg)
    mod.die = die
    return state


TMPL = {"builder": "ct/freepbx.sh", "resources": {}}
MAN = {"community_scripts_ref": "2026-06-01"}


def test_nothing_created_fails_and_touches_no_existing_ct():
    st = harness(existing={101, 900}, created=set())
    try:
        mod.deploy_lxc("freepbx", "site-freepbx", TMPL, MAN, "", "", {})
    except Died:
        pass
    else:
        raise AssertionError("deploy succeeded although nothing was created")
    assert st["renamed"] == [], f"renamed an existing container: {st['renamed']}"


def test_container_under_another_id_is_adopted():
    st = harness(existing={101, 900}, created={105})
    assert mod.deploy_lxc("freepbx", "site-freepbx", TMPL, MAN, "", "", {}) == 105
    assert st["renamed"] == [(105, "site-freepbx")]


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"ok   {name}")
            except Exception as exc:
                failed += 1; print(f"FAIL {name}: {str(exc)[:160]}")
    print("all passed" if not failed else f"{failed} failed")
    sys.exit(1 if failed else 0)
