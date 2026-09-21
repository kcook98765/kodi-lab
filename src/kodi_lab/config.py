"""Typed bootstrap configuration for Kodi Lab."""

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
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
    external_target_master_key: SecretStr | None = None
    external_target_master_key_version: int = Field(default=1, ge=1, le=2_147_483_647)
    external_target_allowed_ports: tuple[int, ...] = (8080,)
    external_bridge_allowed_ports: tuple[int, ...] = (8765,)
    external_target_ipv6_ula_networks: tuple[str, ...] = ()
    external_target_test_loopback_enabled: bool = False

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

    @field_validator("external_target_allowed_ports", "external_bridge_allowed_ports")
    @classmethod
    def _approved_external_target_ports(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        normalized = tuple(sorted(set(value)))
        if (
            not normalized
            or len(normalized) > 8
            or any(port < 1 or port > 65_535 for port in normalized)
        ):
            raise ValueError("external target ports must be 1-8 valid TCP ports")
        return normalized

    @property
    def database_url(self) -> str:
        """Return the SQLite URL derived from the configured data root."""

        database_path = self.data_root / "database" / "kodi-lab.db"
        return f"sqlite:///{database_path}"
