"""Bounded per-request diagnostics for the production screenshot pipeline.

The trace is process-local observability only.  Canonical replay and outcome
state remains in the existing screenshot receipts and bridge events.  Every
emitted field is semantic metadata; image bytes, base64, credentials and raw
requests/responses are deliberately outside this interface.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

LOGGER = logging.getLogger("kodi_lab.screenshot.diagnostic")

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
_SAFE_METADATA = frozenset(
    {
        "http_status",
        "content_type",
        "response_byte_length",
        "payload_present",
        "encoded_character_length",
        "decoded_byte_length",
        "image_sha256",
        "detected_format",
        "width",
        "height",
        "color_mode",
        "normalized_byte_length",
        "normalized_sha256",
        "receipt_kind",
        "receipt_state",
        "reachability_before",
        "reachability_after",
        "classification_source",
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
        if stage not in _SAFE_STAGES or outcome not in _SAFE_OUTCOMES:
            raise ValueError("screenshot diagnostic stage/outcome is not closed")
        if not code or len(code) > 64 or not code.replace("_", "").isalnum():
            raise ValueError("screenshot diagnostic code is not bounded")
        if not set(metadata).issubset(_SAFE_METADATA):
            raise ValueError("screenshot diagnostic metadata is not closed")
        with self._lock:
            self._order += 1
            event: dict[str, object] = {
                "schema_version": 1,
                "event": "production_screenshot_stage",
                "protocol_request": self.protocol_request,
                "client_id": str(self.client_id),
                "target_id": str(self.target_id),
                "request_id": str(self.request_id),
                "bridge_revision": self.bridge_revision,
                "stage": stage,
                "outcome": outcome,
                "code": code,
                "order": self._order,
                "occurred_at": _timestamp(),
                **metadata,
            }
            self.events.append(event)
        LOGGER.info(
            "production_screenshot_stage %s",
            json.dumps(event, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        )


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
) -> Iterator[ScreenshotDiagnosticTrace]:
    trace = ScreenshotDiagnosticTrace(
        protocol_request=dict(protocol_request),
        client_id=client_id,
        target_id=target_id,
        request_id=request_id,
    )
    reset = _TRACE.set(trace)
    try:
        yield trace
    finally:
        _TRACE.reset(reset)


def current_screenshot_trace() -> ScreenshotDiagnosticTrace | None:
    return _TRACE.get()


def bind_screenshot_bridge_revision(revision: int) -> None:
    trace = _TRACE.get()
    if trace is not None:
        if revision < 1:
            raise ValueError("bridge revision must be positive")
        trace.bridge_revision = revision


def emit_screenshot_stage(
    stage: str, outcome: str, code: str, **metadata: object
) -> None:
    trace = _TRACE.get()
    if trace is not None:
        trace.emit(stage, outcome, code, metadata)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
