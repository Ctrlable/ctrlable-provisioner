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


class TemplateSpec(BaseModel):
    kind: Literal["lxc", "vm"]
    builder: str
    app_version: str | None = None
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
