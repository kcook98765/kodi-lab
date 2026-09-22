"""Durable replay and bounded dispatch for privacy-sensitive production pixels."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_bridges import (
    ExternalBridgeService,
    ExternalBridgeServiceError,
    ExternalScreenshotCapture,
)
from kodi_lab.storage.models import McpProductionScreenshotReceiptRow

PRODUCTION_SCREENSHOT_RECEIPT_TTL = timedelta(hours=24)
MAX_PRODUCTION_SCREENSHOT_RECEIPTS_PER_TARGET = 256
MAX_PRODUCTION_SCREENSHOT_RECEIPTS_GLOBAL = 4096


class ProductionScreenshotError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProductionScreenshotService:
    """Claim client replay identity before rate/concurrency and never store pixels."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        bridges: ExternalBridgeService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessions = session_factory
        self._bridges = bridges
        self._clock = clock
        self._monotonic = monotonic
        self._rate_guard = threading.Lock()
        self._last_new_capture: dict[tuple[UUID, UUID], float] = {}

    def capture(
        self,
        *,
        client_id: UUID,
        target_id: UUID,
        request_id: UUID,
        identity_generation: int,
        bridge_trust_revision: int,
    ) -> ExternalScreenshotCapture:
        parameter_digest = self.parameter_digest(target_id)
        existing = self._receipt(client_id, request_id)
        if existing is not None:
            if (
                existing.target_id != str(target_id)
                or existing.parameter_digest != parameter_digest
            ):
                raise ProductionScreenshotError("request_id_conflict")
            if (
                existing.identity_generation != identity_generation
                or existing.bridge_trust_revision != bridge_trust_revision
            ):
                raise ProductionScreenshotError("screenshot_replay_unavailable")
            if existing.state in {"ambiguous", "rejected"}:
                raise ProductionScreenshotError("screenshot_replay_unavailable")
            bridge_request_id = UUID(existing.bridge_request_id)
            try:
                result = self._bridges.capture_screenshot(
                    target_id, request_id=bridge_request_id
                )
            except ExternalBridgeServiceError as error:
                raise ProductionScreenshotError(error.code) from error
            try:
                self._complete(client_id, request_id, result)
            except Exception:
                self._bridges.discard_production_pixels(target_id, bridge_request_id)
                raise
            return self._client_result(result, request_id)

        now = self._clock()
        bridge_request_id = self._claim(
            client_id=client_id,
            target_id=target_id,
            request_id=request_id,
            parameter_digest=parameter_digest,
            identity_generation=identity_generation,
            bridge_trust_revision=bridge_trust_revision,
            now=now,
        )
        try:
            self._admit_new(client_id, target_id)
            result = self._bridges.capture_screenshot(
                target_id, request_id=bridge_request_id
            )
        except (ExternalBridgeServiceError, ProductionScreenshotError) as error:
            code = getattr(error, "code", "screenshot_capture_failed")
            self._fail(client_id, request_id, code)
            raise ProductionScreenshotError(code) from error
        try:
            self._complete(client_id, request_id, result)
        except Exception:
            self._bridges.discard_production_pixels(target_id, bridge_request_id)
            raise
        return self._client_result(result, request_id)

    def discard_pixels(
        self, client_id: UUID, target_id: UUID, request_id: UUID
    ) -> None:
        receipt = self._receipt(client_id, request_id)
        if receipt is not None:
            self._bridges.discard_production_pixels(
                target_id, UUID(receipt.bridge_request_id)
            )

    @staticmethod
    def parameter_digest(target_id: UUID) -> str:
        return hashlib.sha256(
            json.dumps(
                {"target_id": str(target_id)}, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()

    def _admit_new(self, client_id: UUID, target_id: UUID) -> None:
        now = self._monotonic()
        key = (client_id, target_id)
        with self._rate_guard:
            stale = [
                item
                for item, admitted_at in self._last_new_capture.items()
                if now - admitted_at >= 900.0
            ]
            for item in stale:
                del self._last_new_capture[item]
            previous = self._last_new_capture.get(key)
            if previous is not None and now - previous < 30.0:
                raise ProductionScreenshotError("rate_limited")
            if (
                key not in self._last_new_capture
                and len(self._last_new_capture) >= 1024
            ):
                raise ProductionScreenshotError("rate_limited")
            self._last_new_capture[key] = now

    def _claim(
        self,
        *,
        client_id: UUID,
        target_id: UUID,
        request_id: UUID,
        parameter_digest: str,
        identity_generation: int,
        bridge_trust_revision: int,
        now: datetime,
    ) -> UUID:
        bridge_request_id = uuid4()
        with self._sessions.begin() as session:
            session.execute(
                delete(McpProductionScreenshotReceiptRow).where(
                    McpProductionScreenshotReceiptRow.expires_at < self._text(now),
                    McpProductionScreenshotReceiptRow.state.in_(
                        ("completed", "ambiguous", "rejected")
                    ),
                )
            )
            target_count = session.scalar(
                select(func.count())
                .select_from(McpProductionScreenshotReceiptRow)
                .where(McpProductionScreenshotReceiptRow.target_id == str(target_id))
            )
            global_count = session.scalar(
                select(func.count()).select_from(McpProductionScreenshotReceiptRow)
            )
            if (
                target_count is None
                or global_count is None
                or target_count >= MAX_PRODUCTION_SCREENSHOT_RECEIPTS_PER_TARGET
                or global_count >= MAX_PRODUCTION_SCREENSHOT_RECEIPTS_GLOBAL
            ):
                raise ProductionScreenshotError("screenshot_replay_unavailable")
            session.add(
                McpProductionScreenshotReceiptRow(
                    client_id=str(client_id),
                    request_id=str(request_id),
                    target_id=str(target_id),
                    parameter_digest=parameter_digest,
                    identity_generation=identity_generation,
                    bridge_trust_revision=bridge_trust_revision,
                    bridge_request_id=str(bridge_request_id),
                    state="pending",
                    response_digest=None,
                    byte_size=None,
                    width=None,
                    height=None,
                    created_at=self._text(now),
                    completed_at=None,
                    expires_at=self._text(now + PRODUCTION_SCREENSHOT_RECEIPT_TTL),
                )
            )
        return bridge_request_id

    def _receipt(
        self, client_id: UUID, request_id: UUID
    ) -> McpProductionScreenshotReceiptRow | None:
        with self._sessions() as session:
            row = session.get(
                McpProductionScreenshotReceiptRow,
                {"client_id": str(client_id), "request_id": str(request_id)},
            )
            if row is not None:
                session.expunge(row)
            return row

    def _complete(
        self,
        client_id: UUID,
        request_id: UUID,
        result: ExternalScreenshotCapture,
    ) -> None:
        with self._sessions.begin() as session:
            row = session.get(
                McpProductionScreenshotReceiptRow,
                {"client_id": str(client_id), "request_id": str(request_id)},
            )
            if row is None:
                raise ProductionScreenshotError("screenshot_replay_unavailable")
            if row.state == "completed":
                if (
                    row.response_digest != result.digest
                    or row.byte_size != result.byte_size
                    or row.width != result.width
                    or row.height != result.height
                ):
                    raise ProductionScreenshotError("screenshot_replay_unavailable")
                return
            if row.state != "pending":
                raise ProductionScreenshotError("screenshot_replay_unavailable")
            row.state = "completed"
            row.response_digest = result.digest
            row.byte_size = result.byte_size
            row.width = result.width
            row.height = result.height
            row.completed_at = self._text(self._clock())

    def _fail(self, client_id: UUID, request_id: UUID, code: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(
                McpProductionScreenshotReceiptRow,
                {"client_id": str(client_id), "request_id": str(request_id)},
            )
            if row is not None and row.state == "pending":
                row.state = (
                    "ambiguous"
                    if code in {"bridge_unreachable", "bridge_replay_rejected"}
                    else "rejected"
                )
                row.completed_at = self._text(self._clock())

    @staticmethod
    def _client_result(
        result: ExternalScreenshotCapture, request_id: UUID
    ) -> ExternalScreenshotCapture:
        return ExternalScreenshotCapture(
            capture_id=request_id,
            target_id=result.target_id,
            bridge_id=result.bridge_id,
            captured_at=result.captured_at,
            width=result.width,
            height=result.height,
            byte_size=result.byte_size,
            digest=result.digest,
            png=result.png,
        )

    @staticmethod
    def _text(value: datetime) -> str:
        return (
            value.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
