"""Fixed, bounded live Kodi add-on inventory adapter."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import cast

from kodi_lab.kodi.readiness import (
    KodiApplicationIdentity,
)

INVENTORY_PAGE_SIZE = 64
MAX_INVENTORY_ITEMS = 2_048
MAX_INVENTORY_TOTAL_SECONDS = 6.0
MAX_ADDON_ID_LENGTH = 128
MAX_ADDON_NAME_LENGTH = 256
MAX_ADDON_VERSION_LENGTH = 128
MAX_ADDON_TYPE_LENGTH = 128
SMOKE_INVOCATION_TIMEOUT_SECONDS = 10.0
KODI_ERROR_MESSAGE_MAX_LENGTH = 192
_KODI_ERROR_CREDENTIAL = re.compile(
    r"(?i)\b(?:password|passwd|token|api[_-]?key|secret|authorization)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_KODI_ERROR_AUTH_HEADER = re.compile(
    r"(?i)\bauthorization\s*[:=]\s*(?:basic|bearer)\s+[^\s,;]+"
)
_KODI_ERROR_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_KODI_ERROR_URL = re.compile(r"(?i)\b(?:https?|smb|nfs)://[^\s]+")
_KODI_ERROR_UNC = re.compile(r"\\\\[^\s]+")
_KODI_ERROR_DRIVE = re.compile(r"(?i)\b[A-Z]:[\\/][^\s]+")
_KODI_ERROR_UNIX_PATH = re.compile(r"(?<![\w/])/(?:[^\s,;]+)")


def sanitize_kodi_error_message(message: str) -> str | None:
    """Retain only bounded single-line text from a validated Kodi error string."""

    if not isinstance(message, str):
        raise TypeError("Kodi error message must be a string")
    normalized = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf"} else char for char in message
    )
    normalized = _KODI_ERROR_AUTH_HEADER.sub("[redacted]", normalized)
    normalized = _KODI_ERROR_CREDENTIAL.sub("[redacted]", normalized)
    normalized = _KODI_ERROR_BEARER.sub("[redacted]", normalized)
    normalized = _KODI_ERROR_URL.sub("[redacted]", normalized)
    normalized = _KODI_ERROR_UNC.sub("[redacted]", normalized)
    normalized = _KODI_ERROR_DRIVE.sub("[redacted]", normalized)
    normalized = _KODI_ERROR_UNIX_PATH.sub("[redacted]", normalized)
    normalized = " ".join(normalized.split())
    return normalized[:KODI_ERROR_MESSAGE_MAX_LENGTH] or None


@dataclass(frozen=True, slots=True)
class KodiJsonRpcDiagnostic:
    """Only the safe fields of one correlated fixed-execution rejection."""

    code: int
    message: str | None

    def __post_init__(self) -> None:
        if type(self.code) is not int:
            raise TypeError("Kodi JSON-RPC code must be an integer")
        if self.message is not None:
            object.__setattr__(
                self, "message", sanitize_kodi_error_message(self.message)
            )


class KodiAddonInventoryUnsupportedResponseError(RuntimeError):
    """Kodi returned an unsupported JSON-RPC result shape."""


class KodiAddonInventoryMalformedDataError(RuntimeError):
    """At least one add-on record could not be trusted."""


class KodiAddonInventoryLimitExceededError(RuntimeError):
    """The complete inventory exceeds its fixed bound."""


class KodiAddonInventoryIdentityError(RuntimeError):
    """The fresh Kodi identity does not match trusted product state."""


class KodiAddonDetailsUnsupportedResponseError(RuntimeError):
    """Kodi returned an unsupported fixed add-on detail response."""


class KodiAddonEnableRejectedError(RuntimeError):
    """Kodi returned a valid correlated JSON-RPC rejection."""


class KodiAddonEnablementAmbiguousError(RuntimeError):
    """The fixed enable request may have been accepted without trusted proof."""


class KodiAddonSmokeJsonRpcError(RuntimeError):
    """Kodi returned one correlated error envelope for the fixed invocation."""

    def __init__(
        self, duration_ms: int, *, diagnostic: KodiJsonRpcDiagnostic | None = None
    ) -> None:
        super().__init__("Kodi returned a correlated plugin execution error")
        self.duration_ms = duration_ms
        self.diagnostic = diagnostic


class KodiAddonSmokeTimeoutError(RuntimeError):
    """The client deadline expired after the fixed invocation was dispatched."""

    def __init__(self, duration_ms: int) -> None:
        super().__init__("Kodi plugin invocation reached its fixed deadline")
        self.duration_ms = duration_ms


class KodiAddonSmokeAmbiguousError(RuntimeError):
    """Post-dispatch completion cannot be established safely."""


@dataclass(frozen=True, slots=True)
class KodiAddonSmokeResult:
    item_count: int
    duration_ms: int


@dataclass(frozen=True, slots=True)
class KodiAddonInventoryRecord:
    addon_id: str
    name: str
    version: str
    enabled: bool
    kodi_type: str


@dataclass(frozen=True, slots=True)
class KodiAddonInventoryResult:
    identity: KodiApplicationIdentity
    items: tuple[KodiAddonInventoryRecord, ...]


@dataclass(frozen=True, slots=True)
class KodiReportedAddonDependency:
    dependency_id: str
    version: str | None
    optional: bool
    minversion: str | None = None


@dataclass(frozen=True, slots=True)
class KodiAddonDetailsRecord:
    addon_id: str
    name: str
    version: str
    enabled: bool
    kodi_type: str
    dependencies: tuple[KodiReportedAddonDependency, ...]


class KodiAddonInventoryAdapter:
    @classmethod
    def _normalize_item(cls, raw_item: object) -> KodiAddonInventoryRecord:
        if not isinstance(raw_item, dict) or set(raw_item) != {
            "addonid",
            "name",
            "version",
            "enabled",
            "type",
        }:
            raise KodiAddonInventoryMalformedDataError(
                "add-on inventory item shape is invalid"
            )
        enabled = raw_item.get("enabled")
        if type(enabled) is not bool:
            raise KodiAddonInventoryMalformedDataError(
                "add-on enabled state is not Boolean"
            )
        return KodiAddonInventoryRecord(
            addon_id=cls._bounded_string(
                raw_item.get("addonid"), "add-on ID", MAX_ADDON_ID_LENGTH
            ),
            name=cls._bounded_string(
                raw_item.get("name"), "add-on name", MAX_ADDON_NAME_LENGTH
            ),
            version=cls._bounded_string(
                raw_item.get("version"), "add-on version", MAX_ADDON_VERSION_LENGTH
            ),
            enabled=enabled,
            kodi_type=cls._bounded_string(
                raw_item.get("type"), "add-on type", MAX_ADDON_TYPE_LENGTH
            ),
        )

    @staticmethod
    def _bounded_string(value: object, field: str, maximum: int) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > maximum
            or any(unicodedata.category(character) == "Cc" for character in value)
        ):
            raise KodiAddonInventoryMalformedDataError(f"{field} is invalid")
        return value


class KodiAddonSmokeAdapter:
    @staticmethod
    def _validate_directory_result(result: dict[str, object]) -> int:
        if not set(result) <= {"files", "limits"} or "limits" not in result:
            raise KodiAddonSmokeAmbiguousError(
                "Kodi plugin directory result is unsupported"
            )
        files = result.get("files", [])
        limits = result.get("limits")
        if (
            not isinstance(files, list)
            or len(files) > 1
            or not isinstance(limits, dict)
        ):
            raise KodiAddonSmokeAmbiguousError(
                "Kodi plugin directory result exceeds its fixed bound"
            )
        if set(limits) != {"start", "end", "total"} or any(
            type(limits.get(key)) is not int for key in ("start", "end", "total")
        ):
            raise KodiAddonSmokeAmbiguousError(
                "Kodi plugin directory limits are unsupported"
            )
        if (
            limits["start"] != 0
            or limits["end"] != len(files)
            or limits["total"] < limits["end"]
            or limits["total"] < 0
        ):
            raise KodiAddonSmokeAmbiguousError(
                "Kodi plugin directory limits are inconsistent"
            )
        for item in files:
            if (
                not isinstance(item, dict)
                or set(item) != {"file", "filetype", "label", "type"}
                or not isinstance(item.get("file"), str)
                or not 1 <= len(cast(str, item["file"])) <= 4096
                or item.get("filetype") not in {"file", "directory"}
                or not isinstance(item.get("label"), str)
                or len(cast(str, item["label"])) > 1024
                or not isinstance(item.get("type"), str)
                or not 1 <= len(cast(str, item["type"])) <= 128
                or any(
                    unicodedata.category(character) == "Cc"
                    for character in cast(str, item["type"])
                )
            ):
                raise KodiAddonSmokeAmbiguousError(
                    "Kodi plugin directory item is unsupported"
                )
        return len(files)
