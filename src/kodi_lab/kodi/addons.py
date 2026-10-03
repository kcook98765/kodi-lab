"""Fixed, bounded live Kodi add-on inventory adapter."""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.kodi.readiness import (
    JsonRpcInvalidResponseError,
    JsonRpcUnavailableError,
    KodiApplicationIdentity,
    KodiJsonRpcReadinessAdapter,
    _BoundedHttpTransport,
    _HttpTransport,
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
    """Enumerate all installed add-ons from one broker-trusted Kodi endpoint."""

    def __init__(
        self,
        observation: ObserveManagedKodiRuntimeResult,
        profile: RuntimeExecutionProfile,
        *,
        clock: Callable[[], float] = time.monotonic,
        transport: _HttpTransport | None = None,
    ) -> None:
        if observation.runtime_revision_id != str(profile.runtime_revision_id):
            raise ValueError("observation and execution profile revisions differ")
        if observation.jsonrpc_port != profile.jsonrpc_port:
            raise ValueError("observation and execution profile ports differ")
        self._observation = observation
        self._profile = profile
        self._clock = clock
        self._transport = transport or _BoundedHttpTransport()

    def get_inventory(self) -> KodiAddonInventoryResult:
        deadline = self._clock() + MAX_INVENTORY_TOTAL_SECONDS
        try:
            identity = KodiJsonRpcReadinessAdapter(
                self._observation,
                self._profile,
                timeout=self._remaining_timeout(deadline),
                transport=self._transport,
            ).get_application_identity()
        except JsonRpcInvalidResponseError as error:
            raise KodiAddonInventoryUnsupportedResponseError(
                "fresh Kodi identity response is unsupported"
            ) from error
        if (
            identity.name != self._profile.expected_application_name
            or identity.kodi_release != self._profile.kodi_release
        ):
            raise KodiAddonInventoryIdentityError(
                "fresh Kodi identity does not match the managed runtime"
            )

        records: list[KodiAddonInventoryRecord] = []
        addon_ids: set[str] = set()
        expected_total: int | None = None
        start = 0
        while expected_total is None or start < expected_total:
            end = min(start + INVENTORY_PAGE_SIZE, MAX_INVENTORY_ITEMS)
            document = self._request_page(start, end, deadline)
            result = document.get("result")
            if not isinstance(result, dict) or not set(result) <= {"addons", "limits"}:
                raise KodiAddonInventoryUnsupportedResponseError(
                    "add-on inventory result is unsupported"
                )
            limits = result.get("limits")
            if not isinstance(limits, dict) or set(limits) != {"start", "end", "total"}:
                raise KodiAddonInventoryUnsupportedResponseError(
                    "add-on inventory limits are unsupported"
                )
            returned_start = self._strict_nonnegative_int(limits.get("start"))
            returned_end = self._strict_nonnegative_int(limits.get("end"))
            returned_total = self._strict_nonnegative_int(limits.get("total"))
            if returned_start is None or returned_end is None or returned_total is None:
                raise KodiAddonInventoryUnsupportedResponseError(
                    "add-on inventory limits are invalid"
                )
            if returned_total > MAX_INVENTORY_ITEMS:
                raise KodiAddonInventoryLimitExceededError(
                    "add-on inventory item limit exceeded"
                )
            if expected_total is None:
                expected_total = returned_total
            elif returned_total != expected_total:
                raise KodiAddonInventoryUnsupportedResponseError(
                    "add-on inventory total changed during enumeration"
                )
            raw_items = result.get("addons", [])
            if not isinstance(raw_items, list):
                raise KodiAddonInventoryUnsupportedResponseError(
                    "add-on inventory items are unsupported"
                )
            if (
                returned_start != start
                or returned_end != start + len(raw_items)
                or returned_end > returned_total
                or len(raw_items) > INVENTORY_PAGE_SIZE
            ):
                raise KodiAddonInventoryUnsupportedResponseError(
                    "add-on inventory pagination is inconsistent"
                )
            if returned_end == start and returned_end < returned_total:
                raise KodiAddonInventoryUnsupportedResponseError(
                    "add-on inventory pagination made no progress"
                )
            for raw_item in raw_items:
                record = self._normalize_item(raw_item)
                if record.addon_id in addon_ids:
                    raise KodiAddonInventoryMalformedDataError(
                        "add-on inventory contains a duplicate ID"
                    )
                addon_ids.add(record.addon_id)
                records.append(record)
            start = returned_end

        if expected_total is None or len(records) != expected_total:
            raise KodiAddonInventoryUnsupportedResponseError(
                "add-on inventory is incomplete"
            )
        return KodiAddonInventoryResult(
            identity=identity,
            items=tuple(sorted(records, key=lambda item: item.addon_id)),
        )

    def _request_page(self, start: int, end: int, deadline: float) -> dict[str, object]:
        request_id = f"kodi-lab-addon-inventory-{start}"
        request: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "Addons.GetAddons",
            "params": {
                "type": "unknown",
                "installed": True,
                "enabled": "all",
                "properties": ["name", "version", "enabled"],
                "limits": {"start": start, "end": end},
            },
        }
        try:
            payload = self._transport.post(
                host=self._observation.ipv4_address,
                port=self._profile.jsonrpc_port,
                path=self._profile.jsonrpc_path,
                body=json.dumps(request, separators=(",", ":")).encode(),
                timeout=self._remaining_timeout(deadline),
            )
        except JsonRpcInvalidResponseError as error:
            raise KodiAddonInventoryUnsupportedResponseError(
                "add-on inventory response is unsupported"
            ) from error
        if self._clock() >= deadline:
            raise JsonRpcUnavailableError("Kodi inventory deadline expired")
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise KodiAddonInventoryUnsupportedResponseError(
                "add-on inventory response is not valid JSON"
            ) from error
        if (
            not isinstance(document, dict)
            or document.get("jsonrpc") != "2.0"
            or document.get("id") != request_id
            or "error" in document
            or "result" not in document
            or not set(document) <= {"jsonrpc", "id", "result"}
        ):
            raise KodiAddonInventoryUnsupportedResponseError(
                "add-on inventory envelope is unsupported"
            )
        return cast(dict[str, object], document)

    def _remaining_timeout(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise JsonRpcUnavailableError("Kodi inventory deadline expired")
        return min(self._profile.http_timeout_seconds, 3.0, remaining)

    @staticmethod
    def _strict_nonnegative_int(value: object) -> int | None:
        if type(value) is not int or value < 0:
            return None
        return value

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


class KodiAddonEnablementAdapter:
    """Fixed details and enable-only JSON-RPC calls for one trusted endpoint."""

    def __init__(
        self,
        observation: ObserveManagedKodiRuntimeResult,
        profile: RuntimeExecutionProfile,
        *,
        transport: _HttpTransport | None = None,
    ) -> None:
        if observation.runtime_revision_id != str(profile.runtime_revision_id):
            raise ValueError("observation and execution profile revisions differ")
        if observation.jsonrpc_port != profile.jsonrpc_port:
            raise ValueError("observation and execution profile ports differ")
        self._observation = observation
        self._profile = profile
        self._transport = transport or _BoundedHttpTransport()

    def get_addon_details(self, addon_id: str) -> KodiAddonDetailsRecord:
        request_id = "kodi-lab-addon-enablement-details"
        request: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "Addons.GetAddonDetails",
            "params": {
                "addonid": addon_id,
                "properties": ["name", "version", "enabled", "dependencies"],
            },
        }
        try:
            payload = self._post(request)
            document = json.loads(payload)
        except JsonRpcUnavailableError:
            raise
        except (
            JsonRpcInvalidResponseError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise KodiAddonDetailsUnsupportedResponseError(
                "add-on detail response is unsupported"
            ) from error
        if (
            isinstance(document, dict)
            and document.get("jsonrpc") == "2.0"
            and document.get("id") == request_id
            and set(document) == {"jsonrpc", "id", "error"}
            and isinstance(document.get("error"), dict)
        ):
            return self._get_details_from_fixed_inventory(addon_id)
        if (
            not isinstance(document, dict)
            or set(document) != {"jsonrpc", "id", "result"}
            or document.get("jsonrpc") != "2.0"
            or document.get("id") != request_id
            or not isinstance(document.get("result"), dict)
            or set(cast(dict[str, object], document["result"]))
            not in ({"addon"}, {"addon", "limits"})
        ):
            raise KodiAddonDetailsUnsupportedResponseError(
                "add-on detail envelope is unsupported"
            )
        result = cast(dict[str, object], document["result"])
        if "limits" in result:
            limits = result["limits"]
            if (
                not isinstance(limits, dict)
                or set(limits) != {"start", "end", "total"}
                or type(limits.get("start")) is not int
                or type(limits.get("end")) is not int
                or type(limits.get("total")) is not int
                or limits != {"start": 0, "end": 1, "total": 1}
            ):
                raise KodiAddonDetailsUnsupportedResponseError(
                    "add-on detail limits are unsupported"
                )
        return self._normalize_details(result["addon"])

    def _get_details_from_fixed_inventory(
        self, addon_id: str
    ) -> KodiAddonDetailsRecord:
        """Recover declarations for a broken add-on that singular lookup rejects."""

        request_id = "kodi-lab-addon-enablement-dependencies"
        request: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "Addons.GetAddons",
            "params": {
                "type": "unknown",
                "installed": True,
                "enabled": "all",
                "properties": ["name", "version", "enabled", "dependencies"],
                "limits": {"start": 0, "end": MAX_INVENTORY_ITEMS},
            },
        }
        try:
            document = json.loads(self._post(request))
        except (
            JsonRpcUnavailableError,
            JsonRpcInvalidResponseError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise KodiAddonDetailsUnsupportedResponseError(
                "fixed dependency inventory is unsupported"
            ) from error
        if (
            not isinstance(document, dict)
            or set(document) != {"jsonrpc", "id", "result"}
            or document.get("jsonrpc") != "2.0"
            or document.get("id") != request_id
            or not isinstance(document.get("result"), dict)
            or not set(cast(dict[str, object], document["result"]))
            <= {"addons", "limits"}
        ):
            raise KodiAddonDetailsUnsupportedResponseError(
                "fixed dependency inventory envelope is unsupported"
            )
        result = cast(dict[str, object], document["result"])
        limits = result.get("limits")
        raw_addons = result.get("addons", [])
        if (
            not isinstance(limits, dict)
            or set(limits) != {"start", "end", "total"}
            or type(limits.get("start")) is not int
            or type(limits.get("end")) is not int
            or type(limits.get("total")) is not int
            or limits["start"] != 0
            or limits["end"] != limits["total"]
            or limits["total"] > MAX_INVENTORY_ITEMS
            or not isinstance(raw_addons, list)
            or len(raw_addons) != limits["total"]
        ):
            raise KodiAddonDetailsUnsupportedResponseError(
                "fixed dependency inventory is incomplete"
            )
        matches: list[KodiAddonDetailsRecord] = []
        seen: set[str] = set()
        for raw in raw_addons:
            normalized = self._normalize_details(raw)
            if normalized.addon_id in seen:
                raise KodiAddonDetailsUnsupportedResponseError(
                    "fixed dependency inventory contains duplicate IDs"
                )
            seen.add(normalized.addon_id)
            if normalized.addon_id == addon_id:
                matches.append(normalized)
        if len(matches) != 1:
            raise KodiAddonDetailsUnsupportedResponseError(
                "exact add-on details are unavailable"
            )
        return matches[0]

    @staticmethod
    def _normalize_details(raw: object) -> KodiAddonDetailsRecord:
        if not isinstance(raw, dict) or set(raw) != {
            "addonid",
            "type",
            "name",
            "version",
            "enabled",
            "dependencies",
        }:
            raise KodiAddonDetailsUnsupportedResponseError(
                "add-on detail shape is unsupported"
            )
        enabled = raw.get("enabled")
        dependencies = raw.get("dependencies")
        if type(enabled) is not bool or not isinstance(dependencies, list):
            raise KodiAddonDetailsUnsupportedResponseError(
                "add-on detail fields are unsupported"
            )
        normalized_dependencies: list[KodiReportedAddonDependency] = []
        seen: set[str] = set()
        for dependency in dependencies:
            if not isinstance(dependency, dict) or set(dependency) not in (
                {"addonid", "version", "optional"},
                {"addonid", "version", "minversion", "optional"},
            ):
                raise KodiAddonDetailsUnsupportedResponseError(
                    "add-on dependency shape is unsupported"
                )
            dependency_id = KodiAddonInventoryAdapter._bounded_string(
                dependency.get("addonid"), "dependency ID", MAX_ADDON_ID_LENGTH
            )
            version = KodiAddonEnablementAdapter._optional_version_string(
                dependency.get("version"), "dependency version"
            )
            minversion = KodiAddonEnablementAdapter._optional_version_string(
                dependency.get("minversion", ""), "dependency minimum version"
            )
            optional = dependency.get("optional")
            if type(optional) is not bool or dependency_id in seen:
                raise KodiAddonDetailsUnsupportedResponseError(
                    "add-on dependency fields are unsupported"
                )
            seen.add(dependency_id)
            normalized_dependencies.append(
                KodiReportedAddonDependency(
                    dependency_id,
                    version,
                    optional,
                    minversion=minversion,
                )
            )
        return KodiAddonDetailsRecord(
            addon_id=KodiAddonInventoryAdapter._bounded_string(
                raw.get("addonid"), "add-on ID", MAX_ADDON_ID_LENGTH
            ),
            name=KodiAddonInventoryAdapter._bounded_string(
                raw.get("name"), "add-on name", MAX_ADDON_NAME_LENGTH
            ),
            version=KodiAddonInventoryAdapter._bounded_string(
                raw.get("version"), "add-on version", MAX_ADDON_VERSION_LENGTH
            ),
            enabled=enabled,
            kodi_type=KodiAddonInventoryAdapter._bounded_string(
                raw.get("type"), "add-on type", MAX_ADDON_TYPE_LENGTH
            ),
            dependencies=tuple(
                sorted(normalized_dependencies, key=lambda item: item.dependency_id)
            ),
        )

    @staticmethod
    def _optional_version_string(value: object, field: str) -> str | None:
        if value == "":
            return None
        return KodiAddonInventoryAdapter._bounded_string(
            value, field, MAX_ADDON_VERSION_LENGTH
        )

    def enable(self, addon_id: str) -> None:
        """Send literal Boolean true once; any untrusted response is ambiguous."""

        request_id = "kodi-lab-addon-enable"
        request: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "Addons.SetAddonEnabled",
            "params": {"addonid": addon_id, "enabled": True},
        }
        try:
            payload = self._post(request)
            document = json.loads(payload)
        except (
            JsonRpcUnavailableError,
            JsonRpcInvalidResponseError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise KodiAddonEnablementAmbiguousError(
                "Kodi enable response is unavailable or unsupported"
            ) from error
        if (
            isinstance(document, dict)
            and document.get("jsonrpc") == "2.0"
            and document.get("id") == request_id
            and set(document) == {"jsonrpc", "id", "error"}
            and isinstance(document.get("error"), dict)
        ):
            raise KodiAddonEnableRejectedError("Kodi rejected add-on enablement")
        if (
            not isinstance(document, dict)
            or set(document) != {"jsonrpc", "id", "result"}
            or document.get("jsonrpc") != "2.0"
            or document.get("id") != request_id
            or document.get("result") != "OK"
        ):
            raise KodiAddonEnablementAmbiguousError(
                "Kodi enable response is unsupported"
            )

    def _post(self, request: dict[str, object]) -> bytes:
        return self._transport.post(
            host=self._observation.ipv4_address,
            port=self._profile.jsonrpc_port,
            path=self._profile.jsonrpc_path,
            body=json.dumps(request, separators=(",", ":")).encode(),
            timeout=min(self._profile.http_timeout_seconds, 3.0),
        )


class KodiAddonSmokeAdapter:
    """Construct exactly one bounded Files.GetDirectory video-plugin request."""

    def __init__(
        self,
        observation: ObserveManagedKodiRuntimeResult,
        profile: RuntimeExecutionProfile,
        *,
        clock: Callable[[], float] = time.monotonic,
        transport: _HttpTransport | None = None,
    ) -> None:
        if observation.runtime_revision_id != str(profile.runtime_revision_id):
            raise ValueError("observation and execution profile revisions differ")
        if observation.jsonrpc_port != profile.jsonrpc_port:
            raise ValueError("observation and execution profile ports differ")
        self._observation = observation
        self._profile = profile
        self._clock = clock
        self._transport = transport or _BoundedHttpTransport()

    def invoke(self, addon_id: str) -> KodiAddonSmokeResult:
        request_id = "kodi-lab-addon-smoke"
        request: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "Files.GetDirectory",
            "params": {
                "directory": f"plugin://{addon_id}/",
                "media": "video",
                "properties": [],
                "limits": {"start": 0, "end": 1},
            },
        }
        started = self._clock()
        try:
            payload = self._transport.post(
                host=self._observation.ipv4_address,
                port=self._profile.jsonrpc_port,
                path=self._profile.jsonrpc_path,
                body=json.dumps(request, separators=(",", ":")).encode(),
                timeout=SMOKE_INVOCATION_TIMEOUT_SECONDS,
            )
            document = json.loads(payload)
        except JsonRpcUnavailableError as error:
            duration = self._duration_ms(started)
            if self._timed_out(error, duration):
                raise KodiAddonSmokeTimeoutError(duration) from error
            raise KodiAddonSmokeAmbiguousError(
                "Kodi plugin invocation completion is ambiguous"
            ) from error
        except (
            JsonRpcInvalidResponseError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise KodiAddonSmokeAmbiguousError(
                "Kodi plugin invocation response is unsupported"
            ) from error
        duration = self._duration_ms(started)
        if (
            isinstance(document, dict)
            and document.get("jsonrpc") == "2.0"
            and document.get("id") == request_id
            and set(document) == {"jsonrpc", "id", "error"}
            and self._valid_error(document.get("error"))
        ):
            jsonrpc_error = cast(dict[str, object], document["error"])
            raise KodiAddonSmokeJsonRpcError(
                duration,
                diagnostic=KodiJsonRpcDiagnostic(
                    code=cast(int, jsonrpc_error["code"]),
                    message=cast(str, jsonrpc_error["message"]),
                ),
            )
        if (
            not isinstance(document, dict)
            or document.get("jsonrpc") != "2.0"
            or document.get("id") != request_id
            or set(document) != {"jsonrpc", "id", "result"}
            or not isinstance(document.get("result"), dict)
        ):
            raise KodiAddonSmokeAmbiguousError(
                "Kodi plugin invocation envelope is unsupported"
            )
        item_count = self._validate_directory_result(
            cast(dict[str, object], document["result"])
        )
        return KodiAddonSmokeResult(item_count=item_count, duration_ms=duration)

    def _duration_ms(self, started: float) -> int:
        return max(0, min(90_000, round((self._clock() - started) * 1000)))

    @staticmethod
    def _timed_out(error: BaseException, duration_ms: int) -> bool:
        current: BaseException | None = error
        while current is not None:
            if isinstance(current, TimeoutError) or "timed out" in str(current).lower():
                return True
            current = current.__cause__
        return duration_ms >= 9_500

    @staticmethod
    def _valid_error(value: object) -> bool:
        if not isinstance(value, dict) or not {"code", "message"} <= set(value):
            return False
        return type(value.get("code")) is int and isinstance(value.get("message"), str)

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
