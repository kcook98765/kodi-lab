"""Typed bootstrap configuration for Kodi Lab."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Bootstrap settings with environment-over-default precedence."""

    model_config = SettingsConfigDict(
        env_prefix="KODI_LAB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_root: Path = Field(default=Path("/var/lib/kodi-lab"))
    runtime_root: Path = Field(default=Path("/run/kodi-lab"))

    @property
    def database_url(self) -> str:
        """Return the SQLite URL derived from the configured data root."""

        database_path = self.data_root / "database" / "kodi-lab.db"
        return f"sqlite:///{database_path}"
