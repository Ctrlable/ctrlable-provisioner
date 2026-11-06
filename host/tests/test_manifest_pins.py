"""The newest release manifest must pin exactly, and its compat claims must be true.

Two separate failures live here, and they are not the same bug.

DRIFT: app_version said "9.x" and "1.x" in every manifest ever written
(2026.06, 2026.08, 2026.11). Nothing reads app_version for the two
community-script templates, so the field did nothing and nobody bumped it. By
2026-11 zwave-js-ui was at 11.x and zigbee2mqtt at 2.x — the manifest was not
vague about what it installed, it was wrong, while promising that "two
appliances built from this manifest are then the same software".

OUTAGE: Core 2026.11's zwave_js_server client pins MIN == MAX == schema 51.
Pinecrest ran @zwave-js/server 3.10.0 (maxSchemaVersion 50), so zwave_js never
loaded and every Z-Wave entity — including the keymaster service-entrance lock —
was simply absent. No file anywhere stated the requirement, so nothing could
check it.

These EXECUTE against the real npm registry and GitHub (needs network), because
a test that greps our own YAML for a string we just wrote proves only that we
can write a string.
"""
import json
import pathlib
import sys
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.manifest import load_manifest  # noqa: E402

NEWEST = sorted((ROOT / "releases").glob("*.yaml"))[-1]
MANIFEST = load_manifest(NEWEST)


def _get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _is_wildcard(version: str) -> bool:
    """A pin is a wildcard if any dot-separated part is not a number."""
    return any(not part.isdigit() for part in version.split("-")[0].split("."))


def test_the_wildcard_detector_actually_bites():
    # Without this, every assertion below could be vacuous.
    assert _is_wildcard("9.x")
    assert _is_wildcard("1.x")
    assert not _is_wildcard("11.25.2")
    assert not _is_wildcard("0.18.0-ctrlable.1")


def test_newest_release_pins_every_app_version_exactly():
    floating = {
        name: tmpl.app_version
        for name, tmpl in MANIFEST.templates.items()
        if tmpl.app_version and _is_wildcard(tmpl.app_version)
    }
    assert not floating, (
        f"{NEWEST.name} still has floating app_version(s): {floating}. "
        "A monitor cannot report 'off-manifest' against a wildcard."
    )


def test_zwavejs_declares_the_schema_core_requires():
    req = MANIFEST.templates["zwavejs"].requires
    assert req is not None, "zwavejs declares no requires block"
    assert req.zwave_js_api_schema, "zwavejs does not declare zwave_js_api_schema"
    assert req.zwave_js_server, "zwavejs does not declare a zwave_js_server floor"


def test_declared_server_version_really_provides_the_declared_schema():
    """The claim 'server >=X provides schema N' must hold in the published package."""
    req = MANIFEST.templates["zwavejs"].requires
    floor = req.zwave_js_server.lstrip(">=~^ ")
    meta = json.loads(_get(f"https://registry.npmjs.org/@zwave-js%2Fserver/{floor}"))

    import io, tarfile

    tgz = _get(meta["dist"]["tarball"], timeout=60)
    found = set()
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.endswith(".js"):
                continue
            text = tf.extractfile(m).read().decode("utf-8", "replace")
            for line in text.splitlines():
                if "maxSchemaVersion = " in line:
                    found.add(line.split("maxSchemaVersion = ")[1].split(";")[0].strip())

    assert str(req.zwave_js_api_schema) in found, (
        f"@zwave-js/server {floor} exposes maxSchemaVersion {sorted(found)}, "
        f"but the manifest claims schema {req.zwave_js_api_schema}"
    )


def test_pinned_zwavejs_release_bundles_that_server():
    """Pinning the app is pointless if the app bundles an older server."""
    tmpl = MANIFEST.templates["zwavejs"]
    pkg = json.loads(
        _get(
            "https://raw.githubusercontent.com/zwave-js/zwave-js-ui/"
            f"v{tmpl.app_version}/package.json"
        )
    )
    dep = pkg["dependencies"]["@zwave-js/server"]
    floor = tmpl.requires.zwave_js_server.lstrip(">=~^ ")
    want = ".".join(floor.split(".")[:2])
    assert want in dep, (
        f"zwave-js-ui {tmpl.app_version} bundles @zwave-js/server {dep}, "
        f"which does not match the declared floor {floor}"
    )


def test_zigbee2mqtt_gates_its_breaking_boundary():
    req = MANIFEST.templates["zigbee2mqtt"].requires
    assert req is not None and req.gated_across, (
        "zigbee2mqtt must declare gated_across: crossing z2m 2.0 rewrites a "
        "working site's configuration.yaml and must never be unattended"
    )
    pinned = MANIFEST.templates["zigbee2mqtt"].app_version
    assert pinned.split(".")[0] >= req.gated_across.split(".")[0]
