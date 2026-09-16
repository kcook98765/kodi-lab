"""Typed bootstrap configuration for Kodi Lab."""

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
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
    development_root: Path | None = None
    web_host: Literal["127.0.0.1"] = "127.0.0.1"
    web_port: int = Field(default=8100, ge=1, le=65_535)
    web_session_cookie_name: Literal["kodi_lab_session"] = "kodi_lab_session"
    web_cookie_secure: Literal[False] = False
    web_session_idle_seconds: Literal[43_200] = 43_200
    web_session_absolute_seconds: Literal[604_800] = 604_800
    trusted_custom_builds_enabled: bool = False

    @field_validator("development_root")
    @classmethod
    def _safe_development_root(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        if not value.is_absolute():
            raise ValueError("development root must be an absolute path")
        normalized = Path(os.path.normpath(value))
        if normalized != value or normalized == Path("/"):
            raise ValueError("development root must be normalized and must not be /")
        return normalized

    @field_validator("web_port")
    @classmethod
    def _approved_web_port(cls, value: int) -> int:
        if value not in {8100, 8180}:
            raise ValueError("Phase 4 Web port must be 8100 or 8180")
        return value

    @property
    def database_url(self) -> str:
        """Return the SQLite URL derived from the configured data root."""

        database_path = self.data_root / "database" / "kodi-lab.db"
        return f"sqlite:///{database_path}"
