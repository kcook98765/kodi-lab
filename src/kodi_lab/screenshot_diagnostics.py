"""Bounded, observational diagnostics for the production screenshot pipeline.

The trace is process-local observability only. Canonical replay and outcome
state remains in the existing screenshot receipts and bridge events. Diagnostic
construction, validation, serialization and logging are deliberately no-throw:
observer failure must never affect screenshot behavior or durable state.

Every emitted field is bounded semantic metadata. Image bytes, base64,
credentials and raw requests/responses are deliberately outside this interface.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

LOGGER = logging.getLogger("kodi_lab.screenshot.diagnostic")

MAX_SCREENSHOT_DIAGNOSTIC_EVENTS: Final = 64
MAX_SCREENSHOT_DIAGNOSTIC_LOG_CHARACTERS: Final = 4096
MAX_DIAGNOSTIC_CONTENT_TYPE_CHARACTERS: Final = 128
MAX_DIAGNOSTIC_COUNT: Final = 2**31 - 1
MAX_DIAGNOSTIC_REVISION: Final = 2**63 - 1

_SAFE_STAGES = frozenset(
    {
        "mcp_authenticated",
        "catalog_permission",
        "target_grant",
        "visual_authority",
        "bridge_anchor",
        "capability_intersection",
        "http_acknowledgement",
        "bridge_request_prepared",
        "bridge_request_sent",
        "bridge_response_received",
        "bridge_operation_returned",
        "response_schema",
        "image_payload",
        "image_validation",
        "receipt_persistence",
        "mcp_result",
        "reachability_classification",
    }
)
_SAFE_OUTCOMES = frozenset({"entered", "passed", "failed", "not_applicable"})
_SAFE_CODE = re.compile(r"[a-z0-9_]{1,64}")
_SAFE_MIME_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+\-]+/[A-Za-z0-9!#$&^_.+\-]+")
_SAFE_SHA256 = re.compile(r"[0-9a-f]{64}")
_INVALID = object()

_COUNT_METADATA = frozenset(
    {
        "response_byte_length",
        "encoded_character_length",
        "decoded_byte_length",
    }
)
_ENUM_METADATA: dict[str, frozenset[str]] = {
    "detected_format": frozenset({"png", "unknown"}),
    "color_mode": frozenset({"rgb", "unknown"}),
    "receipt_kind": frozenset({"bridge", "mcp"}),
    "receipt_state": frozenset({"pending", "completed", "ambiguous", "rejected"}),
    "reachability_before": frozenset(
        {
            "absent",
            "unknown",
            "reachable",
            "unreachable",
            "authentication_failed",
            "identity_mismatch",
            "protocol_unsupported",
        }
    ),
    "reachability_after": frozenset(
        {
            "absent",
            "unknown",
            "reachable",
            "unreachable",
            "authentication_failed",
            "identity_mismatch",
            "protocol_unsupported",
        }
    ),
    "classification_source": frozenset(
        {
            "authenticated_screenshot_success",
            "semantic_screenshot_failure",
            "transport_or_trust_failure",
        }
    ),
}
_SAFE_METADATA = frozenset(
    {
        "http_status",
        "content_type",
        *_COUNT_METADATA,
        "payload_present",
        "image_sha256",
        "detected_format",
        "width",
        "height",
        "color_mode",
        *_ENUM_METADATA,
    }
)


@dataclass(slots=True)
class ScreenshotDiagnosticTrace:
    protocol_request: dict[str, str | int]
    client_id: UUID
    target_id: UUID
    request_id: UUID
    bridge_revision: int | None = None
    events: list[dict[str, object]] = field(default_factory=list)
    _order: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(
        self,
        stage: str,
        outcome: str,
        code: str,
        metadata: dict[str, object],
    ) -> None:
        """Best-effort emission that can never escape into screenshot control flow."""

        try:
            self._emit(stage, outcome, code, metadata)
        except BaseException:
            # Do not recursively report an observer failure through the same
            # logger or any other sink.
            return

    def _emit(
        self,
        stage: str,
        outcome: str,
        code: str,
        metadata: dict[str, object],
    ) -> None:
        if stage not in _SAFE_STAGES or outcome not in _SAFE_OUTCOMES:
            return
        bounded_code = (
            code
            if isinstance(code, str) and _SAFE_CODE.fullmatch(code)
            else "unclassified"
        )
        bounded_metadata = _bounded_metadata(metadata)
        protocol_request = _bounded_protocol_request(self.protocol_request)
        if not all(
            isinstance(value, UUID)
            for value in (self.client_id, self.target_id, self.request_id)
        ):
            return
        bridge_revision = _bounded_revision(self.bridge_revision)
        with self._lock:
            if self._order >= MAX_SCREENSHOT_DIAGNOSTIC_EVENTS:
                return
            order = self._order + 1
            event: dict[str, object] = {
                "schema_version": 1,
                "event": "production_screenshot_stage",
                "protocol_request": protocol_request,
                "client_id": str(self.client_id),
                "target_id": str(self.target_id),
                "request_id": str(self.request_id),
                "bridge_revision": bridge_revision,
                "stage": stage,
                "outcome": outcome,
                "code": bounded_code,
                "order": order,
                "occurred_at": _timestamp(),
                **bounded_metadata,
            }
            self.events.append(event)
            self._order = order
        try:
            serialized = json.dumps(
                event,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            if len(serialized) > MAX_SCREENSHOT_DIAGNOSTIC_LOG_CHARACTERS:
                return
            LOGGER.info("production_screenshot_stage %s", serialized)
        except BaseException:
            return


_TRACE: contextvars.ContextVar[ScreenshotDiagnosticTrace | None] = (
    contextvars.ContextVar("production_screenshot_diagnostic_trace", default=None)
)


@contextmanager
def screenshot_diagnostic_trace(
    *,
    protocol_request: dict[str, str | int],
    client_id: UUID,
    target_id: UUID,
    request_id: UUID,
) -> Iterator[ScreenshotDiagnosticTrace | None]:
    trace: ScreenshotDiagnosticTrace | None = None
    reset: contextvars.Token[ScreenshotDiagnosticTrace | None] | None = None
    try:
        if all(isinstance(value, UUID) for value in (client_id, target_id, request_id)):
            trace = ScreenshotDiagnosticTrace(
                protocol_request=_bounded_protocol_request(protocol_request),
                client_id=client_id,
                target_id=target_id,
                request_id=request_id,
            )
            reset = _TRACE.set(trace)
    except BaseException:
        trace = None
        reset = None
    try:
        yield trace
    finally:
        if reset is not None:
            with suppress(BaseException):
                _TRACE.reset(reset)


def current_screenshot_trace() -> ScreenshotDiagnosticTrace | None:
    try:
        return _TRACE.get()
    except BaseException:
        return None


def bind_screenshot_bridge_revision(revision: int) -> None:
    try:
        trace = _TRACE.get()
        if trace is not None and _bounded_revision(revision) is not None:
            trace.bridge_revision = revision
    except BaseException:
        return


def emit_screenshot_stage(
    stage: str, outcome: str, code: str, **metadata: object
) -> None:
    try:
        trace = _TRACE.get()
        if trace is not None:
            trace.emit(stage, outcome, code, metadata)
    except BaseException:
        return


def _bounded_protocol_request(value: object) -> dict[str, str | int]:
    try:
        if not isinstance(value, dict):
            return {"type": "unrecognized"}
        request_type = value.get("type")
        if request_type == "missing" and len(value) == 1 and "type" in value:
            return {"type": "missing"}
        if request_type not in {"integer", "string"}:
            return {"type": "unrecognized"}
        byte_length = value.get("byte_length")
        sha256 = value.get("sha256")
        if (
            not _is_integer(byte_length, minimum=0, maximum=MAX_DIAGNOSTIC_REVISION)
            or not isinstance(sha256, str)
            or _SAFE_SHA256.fullmatch(sha256) is None
        ):
            return {"type": "unrecognized"}
        return {
            "type": request_type,
            "byte_length": byte_length,
            "sha256": sha256,
        }
    except BaseException:
        return {"type": "unrecognized"}


def _bounded_metadata(metadata: object) -> dict[str, object]:
    if not isinstance(metadata, dict):
        return {}
    bounded: dict[str, object] = {}
    for key in _SAFE_METADATA:
        try:
            if key not in metadata:
                continue
            value = _bounded_metadata_value(key, metadata[key])
            if value is not _INVALID:
                bounded[key] = value
        except BaseException:
            continue
    return bounded


def _bounded_metadata_value(key: str, value: object) -> object:
    if key == "http_status":
        return value if _is_integer(value, minimum=100, maximum=599) else _INVALID
    if key == "content_type":
        if (
            isinstance(value, str)
            and len(value) <= MAX_DIAGNOSTIC_CONTENT_TYPE_CHARACTERS
            and _SAFE_MIME_TYPE.fullmatch(value) is not None
        ):
            return value
        return "unrecognized"
    if key in _COUNT_METADATA:
        return (
            value
            if _is_integer(value, minimum=0, maximum=MAX_DIAGNOSTIC_COUNT)
            else _INVALID
        )
    if key == "payload_present":
        return value if isinstance(value, bool) else _INVALID
    if key == "image_sha256":
        return (
            value
            if isinstance(value, str) and _SAFE_SHA256.fullmatch(value) is not None
            else _INVALID
        )
    if key == "width":
        return value if _is_integer(value, minimum=1, maximum=4096) else _INVALID
    if key == "height":
        return value if _is_integer(value, minimum=1, maximum=2160) else _INVALID
    allowed = _ENUM_METADATA.get(key)
    if allowed is not None:
        return value if isinstance(value, str) and value in allowed else "unrecognized"
    return _INVALID


def _bounded_revision(value: object) -> int | None:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= MAX_DIAGNOSTIC_REVISION
    ):
        return value
    return None


def _is_integer(value: object, *, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
    )


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
