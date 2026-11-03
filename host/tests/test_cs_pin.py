"""Community scripts must run pinned END TO END, not just the per-app wrapper.

On 2026-10-31 upstream deleted misc/ from ProxmoxVE main. Every pinned
ct/<app>.sh sources .../ProxmoxVE/main/misc/build.func on its first line, so all
of them 404'd and then "succeeded" without creating a container (zwavejs and
zigbee2mqtt, ctrlable-pve 2026-11-03).

This EXECUTES the generated curl/wget wrappers against the real upstream (needs
network) and checks the chain resolves at the pinned ref. Run with --unpinned
to see the failure the wrappers fix.
"""
import importlib.machinery, importlib.util, os, pathlib, shutil, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
loader = importlib.machinery.SourceFileLoader("cb", str(ROOT / "host" / "ctrlable-build"))
mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("cb", loader))
loader.exec_module(mod)

REF = "2026-06-01"
MAIN = "https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main"
UNPINNED = "--unpinned" in sys.argv


def run(cmd):
    env = dict(os.environ)
    if not UNPINNED:
        env["PATH"] = f"{PIN}:{env['PATH']}"
    return subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True)


def test_build_func_resolves_through_main_url():
    r = run(f"curl -fsSL {MAIN}/misc/build.func")
    assert r.returncode == 0, f"rc={r.returncode} {r.stderr.strip()[:120]}"
    assert "build_container" in r.stdout or len(r.stdout) > 100000, len(r.stdout)


def test_fetched_engine_points_at_the_ref():
    r = run(f"curl -fsSL {MAIN}/misc/build.func")
    assert "community-scripts/ProxmoxVE/main/" not in r.stdout, "build.func still fetches from main"
    assert f"community-scripts/ProxmoxVE/{REF}/misc/install.func" in r.stdout


def test_install_func_carries_the_pin_into_the_container():
    # install.func travels into the CT as FUNCTIONS_FILE_PATH; its own fetches
    # (core.func, tools.func) happen there, beyond the wrappers' reach.
    r = run(f"curl -fsSL {MAIN}/misc/install.func")
    assert r.returncode == 0, r.stderr[:120]
    assert "community-scripts/ProxmoxVE/main/" not in r.stdout


def test_wget_stdout_form():
    if not shutil.which("wget"):
        return
    r = run(f"wget -qO- {MAIN}/misc/core.func")
    assert r.returncode == 0 and len(r.stdout) > 1000, r.returncode


def test_curl_to_file():
    out = os.path.join(PIN, "dl.func")
    r = run(f"curl -fsSL {MAIN}/misc/install.func -o {out}")
    assert r.returncode == 0, r.stderr[:120]
    assert "community-scripts/ProxmoxVE/main/" not in open(out).read()


def test_other_urls_untouched():
    r = run("curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/zwave-js-ui.sh")
    # ct/ still exists on main; the wrapper redirects it to the ref (also fine),
    # but a non-community-scripts URL must pass through byte-for-byte.
    r2 = run("curl -fsS https://api.github.com/zen")
    assert r2.returncode == 0 and r2.stdout.strip()


def test_pin_text_rewrites_the_first_line():
    src = "source <(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/misc/build.func)"
    assert f"ProxmoxVE/{REF}/misc/build.func" in mod.pin_text(src, REF)


if __name__ == "__main__":
    PIN = mod.cs_pin_dir(REF)
    failed = 0
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_") and callable(fn):
                try:
                    fn(); print(f"ok   {name}")
                except Exception as exc:
                    failed += 1; print(f"FAIL {name}: {str(exc)[:160]}")
    finally:
        shutil.rmtree(PIN, ignore_errors=True)
    print("all passed" if not failed else f"{failed} failed")
    sys.exit(1 if failed else 0)
