"""An LXC deploy must leave the instance claimable by its firstboot.

deploy_instance() settled every LXC as "active" on build success, and
get_assignment() refuses "active" with 409 -- so since 2026-07-31 no firstboot
ever got its wiring (seen on ctrlable-pve 2026-11-03: all six CTs failed with
"could not reach orchestrator", the real answer being 409 "already active").

Executes the real StateDB + provision functions against a temp sqlite file and
the real final-status line from deploy.py. Pass a deploy.py / provision.py
directory as argv[1] to run against another revision.
"""
import importlib, pathlib, sys, tempfile, types

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "app"
sys.path.insert(0, str(APP.parent))
pkg = APP.name
state = importlib.import_module(f"{pkg}.state")
provision = importlib.import_module(f"{pkg}.provision")
DEPLOY_SRC = (APP / "deploy.py").read_text()


def final_status(kind: str) -> str:
    """The status deploy_instance() settles a successful build in."""
    if 'final = "pending" if kind == "lxc" else "active"' in DEPLOY_SRC:
        return "pending" if kind == "lxc" else "active"
    if 'finish_instance, hostname, vmid, kind, "active")' in DEPLOY_SRC:
        return "active"
    raise AssertionError("could not find deploy_instance's final status")


def fresh_db():
    db = state.StateDB(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    db.upsert_release("2026.11", "2026-06-01")
    secret = db.get_release("2026.11")["firstboot_secret"]
    pid = db.create_project("Site", "2026.11")
    return db, secret, pid


def deploy(db, pid, name, kind):
    host = f"site-{name}"
    db.create_instance(pid, name, 0, host, {"mqtt_host": "10.0.0.9"}, kind, "provisioning")
    db.finish_instance(host, 104, kind, final_status(kind))
    return host


def test_lxc_firstboot_gets_its_wiring():
    db, secret, pid = fresh_db()
    host = deploy(db, pid, "zwavejs", "lxc")
    a = provision.get_assignment(db, host, secret)  # raised ValueError (409) before
    assert a["wire_to"] == {"mqtt_host": "10.0.0.9"}


def test_lxc_ends_active_after_firstboot():
    db, secret, pid = fresh_db()
    host = deploy(db, pid, "zwavejs", "lxc")
    provision.get_assignment(db, host, secret)
    provision.complete_provisioning(db, host, secret)
    assert db.get_instance_by_hostname(host)["status"] == "active"


def test_rerunning_firstboot_on_active_is_allowed():
    db, secret, pid = fresh_db()
    host = deploy(db, pid, "zwavejs", "lxc")
    provision.get_assignment(db, host, secret)
    provision.complete_provisioning(db, host, secret)
    provision.get_assignment(db, host, secret)  # idempotent re-wire
    assert db.get_instance_by_hostname(host)["status"] == "active"


def test_vm_is_active_when_built():
    db, secret, pid = fresh_db()
    host = deploy(db, pid, "ctrlable-pro", "qemu")
    assert db.get_instance_by_hostname(host)["status"] == "active"


def test_bad_secret_still_refused():
    db, secret, pid = fresh_db()
    host = deploy(db, pid, "zwavejs", "lxc")
    try:
        provision.get_assignment(db, host, "nope")
    except PermissionError:
        return
    raise AssertionError("wrong secret accepted")


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"ok   {name}")
            except Exception as exc:
                failed += 1; print(f"FAIL {name}: {type(exc).__name__}: {str(exc)[:140]}")
    print("all passed" if not failed else f"{failed} failed")
    sys.exit(1 if failed else 0)
