"""Typed bootstrap configuration for Kodi Lab."""

import ipaddress
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from kodi_lab.mcp.lan import validate_lan_url


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
    web_host: Literal["127.0.0.1", "10.251.0.10"] = "127.0.0.1"
    web_bind_host: Literal["127.0.0.1", "10.251.0.10", "0.0.0.0"] | None = None
    web_allowed_hosts: tuple[str, ...] = ()
    mcp_lan_url: str | None = None
    mcp_lan_ca_file: Path | None = None
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
    # An administrator may reserve an IPv4 pool for reviewed runtime build and
    # validation bridges. An absent pool retains Docker's ordinary allocator.
    temporary_network_pool: str | None = None
    temporary_network_child_prefix: int = Field(default=24, ge=24, le=28)

    @field_validator("mcp_lan_url")
    @classmethod
    def _approved_mcp_lan_url(cls, value: str | None) -> str | None:
        return validate_lan_url(value)

    @field_validator("mcp_lan_ca_file", mode="before")
    @classmethod
    def _approved_mcp_lan_ca_file(cls, value: object) -> object:
        if value == "":
            return None
        if value is not None and not Path(str(value)).is_absolute():
            raise ValueError("MCP LAN CA file must be an absolute path")
        return value

    @field_validator("temporary_network_pool")
    @classmethod
    def _approved_temporary_network_pool(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        try:
            pool = ipaddress.IPv4Network(value, strict=True)
        except (TypeError, ValueError) as error:
            raise ValueError("temporary network pool must be an IPv4 CIDR") from error
        private = (
            ipaddress.IPv4Network("10.0.0.0/8"),
            ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.168.0.0/16"),
        )
        if not any(pool.subnet_of(parent) for parent in private):
            raise ValueError("temporary network pool must use private IPv4 space")
        return str(pool)

    @model_validator(mode="after")
    def _approved_temporary_network_size(self) -> "Settings":
        if self.temporary_network_pool is not None:
            pool = ipaddress.IPv4Network(self.temporary_network_pool)
            if self.temporary_network_child_prefix <= pool.prefixlen:
                raise ValueError("temporary network pool needs 2 to 256 child subnets")
            children = 1 << (self.temporary_network_child_prefix - pool.prefixlen)
            if not 2 <= children <= 256:
                raise ValueError("temporary network pool needs 2 to 256 child subnets")
        return self

    @field_validator("web_allowed_hosts")
    @classmethod
    def _approved_web_allowed_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 8 or len(set(value)) != len(value):
            raise ValueError(
                "Web allowed hosts must contain at most eight unique hosts"
            )
        for host in value:
            if not host or len(host) > 253 or host != host.lower():
                raise ValueError(
                    "Web allowed hosts must be normalized host names or IPs"
                )
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                labels = host.split(".")
                if not all(
                    re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in labels
                ):
                    raise ValueError(
                        "Web allowed hosts must be DNS names or IPs"
                    ) from None
            else:
                if address.is_unspecified or address.version != 4:
                    raise ValueError(
                        "Web allowed hosts must be concrete IPv4 addresses"
                    )
        return value

    @model_validator(mode="after")
    def _approved_web_binding(self) -> "Settings":
        if self.web_bind_host == "0.0.0.0" and not self.web_allowed_hosts:
            raise ValueError("wildcard Web bind requires explicit allowed hosts")
        return self

    @property
    def effective_web_bind_host(self) -> str:
        return self.web_bind_host or self.web_host

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


def safe_settings_error(error: ValidationError) -> str:
    """Report configuration fields without echoing environment input values."""

    items = error.errors(include_input=False)
    if any(
        "wildcard Web bind requires explicit allowed hosts" in str(item.get("msg"))
        for item in items
    ):
        return "wildcard Web bind requires KODI_LAB_WEB_ALLOWED_HOSTS"
    fields = sorted(
        {
            "KODI_LAB_" + item["loc"][0].upper()
            for item in items
            if item["loc"] and isinstance(item["loc"][0], str)
        }
    )
    if fields:
        return "invalid Kodi Lab configuration: " + ", ".join(fields[:4])
    return "invalid Kodi Lab configuration; check documented KODI_LAB_* settings"
