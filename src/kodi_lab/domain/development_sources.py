"""Global local-directory development-source catalog records."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

MAX_DEVELOPMENT_SOURCE_LOGICAL_NAME = 120
MAX_DEVELOPMENT_SOURCE_LOCATOR = 1024
MAX_ADDON_ID = 128
MAX_ADDON_NAME = 256
MAX_ADDON_VERSION = 128
MAX_EXTENSION_TYPE = 128
MAX_EXTENSION_TYPES = 64

_ADDON_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_EXTENSION_TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class DevelopmentSourceKind(StrEnum):
    """Closed development-source kinds supported by the current catalog."""

    LOCAL_DIRECTORY = "local_directory"


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def normalize_development_source_logical_name(value: str) -> str:
    """Return a bounded operator-facing name with no path semantics."""

    normalized = unicodedata.normalize("NFC", value.strip())
    if not 1 <= len(normalized) <= MAX_DEVELOPMENT_SOURCE_LOGICAL_NAME:
        raise ValueError("logical name must contain 1 to 120 Unicode code points")
    if _has_control(normalized):
        raise ValueError("logical name must not contain control characters")
    if "/" in normalized or "\\" in normalized:
        raise ValueError("logical name must not contain path separators")
    return normalized


def normalize_development_source_locator(value: str) -> str:
    """Validate and return one canonical POSIX-style root-relative locator."""

    if not value or "\x00" in value or "\\" in value or value.startswith("/"):
        raise ValueError("source locator must be a safe relative path")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError(
            "source locator must not contain empty or traversal components"
        )
    normalized = "/".join(components)
    if len(normalized) > MAX_DEVELOPMENT_SOURCE_LOCATOR:
        raise ValueError("source locator must contain at most 1024 code points")
    if any(len(component.encode("utf-8")) > 255 for component in components):
        raise ValueError("source locator component exceeds 255 UTF-8 bytes")
    return normalized


def validate_addon_id(value: str) -> str:
    """Validate a Kodi-compatible, path-safe add-on identifier."""

    if _ADDON_ID_PATTERN.fullmatch(value) is None or ".." in value:
        raise ValueError(
            "add-on ID must use 1 to 128 lowercase letters, numbers, '.', '_', "
            "or '-', start with a letter or number, and contain no '..'"
        )
    return value


def validate_observed_metadata(value: str, *, field: str, maximum: int) -> str:
    """Validate one exact parsed metadata observation without rewriting it."""

    if not 1 <= len(value) <= maximum or not value.strip():
        raise ValueError(
            f"observed add-on {field} must contain 1 to {maximum} characters"
        )
    if _has_control(value):
        raise ValueError(f"observed add-on {field} must not contain control characters")
    return value


def normalize_extension_types(values: tuple[str, ...]) -> tuple[str, ...]:
    """Return the bounded sorted unique extension-point representation."""

    if not values:
        raise ValueError("addon.xml must declare at least one extension point")
    unique = tuple(sorted(set(values)))
    if len(unique) > MAX_EXTENSION_TYPES:
        raise ValueError("addon.xml declares more than 64 extension points")
    if any(_EXTENSION_TYPE_PATTERN.fullmatch(value) is None for value in unique):
        raise ValueError(
            "extension points must use bounded lowercase identifier characters"
        )
    return unique


def normalize_utc_timestamp(value: datetime, *, field: str) -> datetime:
    """Normalize one aware timestamp to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DevelopmentSource:
    """One immutable registration-time observation of a mutable local source."""

    id: UUID
    logical_name: str
    source_kind: DevelopmentSourceKind
    relative_locator: str
    addon_id: str
    observed_name: str
    observed_version: str
    extension_types: tuple[str, ...]
    registered_at: datetime
    validated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "logical_name",
            normalize_development_source_logical_name(self.logical_name),
        )
        object.__setattr__(
            self,
            "relative_locator",
            normalize_development_source_locator(self.relative_locator),
        )
        object.__setattr__(self, "addon_id", validate_addon_id(self.addon_id))
        object.__setattr__(
            self,
            "observed_name",
            validate_observed_metadata(
                self.observed_name, field="name", maximum=MAX_ADDON_NAME
            ),
        )
        object.__setattr__(
            self,
            "observed_version",
            validate_observed_metadata(
                self.observed_version, field="version", maximum=MAX_ADDON_VERSION
            ),
        )
        object.__setattr__(
            self, "extension_types", normalize_extension_types(self.extension_types)
        )
        object.__setattr__(
            self,
            "registered_at",
            normalize_utc_timestamp(self.registered_at, field="registered_at"),
        )
        object.__setattr__(
            self,
            "validated_at",
            normalize_utc_timestamp(self.validated_at, field="validated_at"),
        )
        if self.validated_at < self.registered_at:
            raise ValueError("validated_at must not precede registered_at")
