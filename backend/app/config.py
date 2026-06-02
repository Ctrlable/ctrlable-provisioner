from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from .manifest import load_all_manifests


class Settings(BaseSettings):
    pve_host: str = ""
    pve_token_id: str = ""       # "user@realm!tokenname"
    pve_token_secret: str = ""
    pve_node: str = "pve"
    pve_verify_ssl: bool = False

    # Build plane
    build_key_path: str = "/etc/ctrlable/build_key"   # SSH private key for host trigger
    build_token: str = ""                              # token for manifest + firstboot fetches
    orchestrator_url: str = ""                         # URL baked into templates for firstboot call-home

    db_path: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def db_file(self) -> Path:
        if self.db_path:
            return Path(self.db_path)
        return Path(__file__).parent.parent / "ctrlable.db"

    def configured(self) -> bool:
        return bool(self.pve_host and self.pve_token_id and self.pve_token_secret)

    def build_configured(self) -> bool:
        return bool(self.pve_host and self.build_key_path and self.build_token)

    def community_scripts_ref_for(self, release: str) -> str:
        manifests = load_all_manifests()
        return manifests[release].community_scripts_ref if release in manifests else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
