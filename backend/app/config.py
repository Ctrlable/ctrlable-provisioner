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

    # Auth — always enabled. Default password is "admin"; UI forces a change on first login.
    # Set ADMIN_PASSWORD_HASH in .env to activate a real password and lift the forced-change flag.
    admin_user: str = "admin"
    admin_password_hash: str = ""   # leave blank to use default "admin" password (forced change on login)
    jwt_secret: str = ""            # leave blank to use an ephemeral secret (tokens reset on restart)
    jwt_expire_hours: int = 24

    # Pre-computed bcrypt hash of the string "admin" (rounds=10).
    # When admin_password_hash is not set this is the active hash and must_change_password is True.
    _DEFAULT_ADMIN_HASH: str = "$2b$10$uTYtcigetHsjTvbH4bjjnuVhafL2uN2pTSw/XfOcOBPp9al/Lj6hS"

    @property
    def effective_password_hash(self) -> str:
        return self.admin_password_hash or self._DEFAULT_ADMIN_HASH

    @property
    def must_change_password(self) -> bool:
        """True when the admin password is still the factory default."""
        return not bool(self.admin_password_hash)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def env_file_path(self) -> Path:
        """Absolute path to the .env file — avoids CWD-dependent relative lookups."""
        return Path(__file__).parent.parent / ".env"

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
