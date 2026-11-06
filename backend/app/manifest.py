from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel


class Resources(BaseModel):
    cpu: int
    ram: int
    disk: int


class OsSpec(BaseModel):
    distro: str
    version: str


class Requires(BaseModel):
    """Compatibility the SITE must satisfy — not something the builder installs.

    Deliberately separate from app_version. app_version says what we intend to
    install; requires says what the rest of the appliance needs that software to
    be compatible WITH. They fail differently and a monitor must not conflate
    them: off app_version is drift (cosmetic until it isn't), off requires is an
    outage.

    Pinecrest, 2026-11-06: zwave-js-ui shipping @zwave-js/server 3.10.0
    (maxSchemaVersion 50) against a Core whose client pins schema 51 left
    zwave_js unloaded — every Z-Wave entity gone, and the keymaster
    service-entrance lock with it. Nothing in the manifest could have predicted
    that, because the requirement was never written down.
    """

    # Core's zwave_js_server client sets MIN == MAX, so this is an EQUALITY and
    # not a floor: newer is just as broken as older.
    zwave_js_api_schema: int | None = None
    # The @zwave-js/server version that provides the schema above.
    zwave_js_server: str | None = None
    # Updating ACROSS this version rewrites a working site's config, so it is
    # never applied unattended — the monitor may flag it, a human applies it.
    gated_across: str | None = None


class TemplateSpec(BaseModel):
    kind: Literal["lxc", "vm"]
    builder: str
    app_version: str | None = None
    requires: Requires | None = None
    haos_version: str | None = None
    rebrand: bool = False
    base_backup: str | None = None
    vmid_base: int | None = None
    os: OsSpec | None = None
    resources: Resources | None = None
    unprivileged: bool | None = None
    portainer: bool = False


class ReleaseManifest(BaseModel):
    release: str
    community_scripts_ref: str
    proxmox_min_version: str
    templates: dict[str, TemplateSpec]


def load_manifest(path: Path) -> ReleaseManifest:
    data = yaml.safe_load(path.read_text())
    return ReleaseManifest.model_validate(data)


def manifests_dir() -> Path:
    return Path(__file__).parent.parent.parent / "releases"


def load_all_manifests() -> dict[str, ReleaseManifest]:
    return {
        m.release: m
        for p in sorted(manifests_dir().glob("*.yaml"))
        if (m := load_manifest(p))
    }
