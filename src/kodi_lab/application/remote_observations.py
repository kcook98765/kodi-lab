"""Managed-only, independently authorized UI observation and exact capture reuse."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_remote_actions import McpRemoteActionResult
from kodi_lab.application.remote_observation_models import RemotePostActionObservation
from kodi_lab.application.screenshots import (
    ManagedScreenshotService,
    ManagedScreenshotStatus,
)
from kodi_lab.kodi.ui_state import UiObservation
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    McpRemoteActionReceiptRow,
    McpRemoteObservationRow,
)
from kodi_lab.storage.repositories import _timestamp_to_text

CAPTURE_REUSE_RETENTION = timedelta(minutes=2)


class CaptureReuseError(RuntimeError):
    """A closed reason, never a filesystem path or transport exception."""


@dataclass(frozen=True)
class RuntimeObservationBinding:
    runtime_revision_id: str
    state_revision: int
    container_id: str


class RemoteObservationService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        screenshots: ManagedScreenshotService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._screenshots = screenshots
        self._clock = clock

    def identity(self, target_id: UUID) -> RuntimeObservationBinding:
        with self._sessions() as session:
            instance = session.get(ManagedInstanceRow, str(target_id))
            if instance is None:
                raise CaptureReuseError("managed_observation_unsupported")
            binding = RuntimeObservationBinding(
                instance.runtime_revision_id,
                instance.runtime_state_revision,
                self._screenshots._eligible_container(target_id),
            )
        return binding

    def prepare(
        self, client_id: UUID, target_id: UUID, request_id: UUID
    ) -> tuple[bool, RuntimeObservationBinding | None]:
        with self._sessions() as session:
            existing = session.scalar(
                select(McpRemoteActionReceiptRow.id).where(
                    McpRemoteActionReceiptRow.client_id == str(client_id),
                    McpRemoteActionReceiptRow.request_id == str(request_id),
                )
            )
        if existing is not None:
            return False, None
        try:
            return True, self.identity(target_id)
        except Exception:
            # Observation availability must never become action authority.
            return True, None

    def complete(
        self,
        result: McpRemoteActionResult,
        *,
        new_action: bool,
        before: RuntimeObservationBinding | None,
        inspect: Callable[[], UiObservation],
        authorize_inspect: Callable[[], object],
    ) -> RemotePostActionObservation:
        try:
            with self._sessions() as session:
                row = session.get(McpRemoteObservationRow, str(result.invocation_id))
            if row is not None:
                return self._read(row, authorize_inspect)
            if not new_action:
                return RemotePostActionObservation(
                    status="not_retained",
                    error_code="observation_not_retained",
                )
            after = None
            with suppress(Exception):
                after = self.identity(result.target_id)
            same_runtime = before is not None and before == after
            row = McpRemoteObservationRow(
                receipt_id=str(result.invocation_id),
                ui_status="pending",
                observed_at=None,
                error_code=None,
                ui_json=None,
                runtime_revision_id=None
                if before is None or not same_runtime
                else before.runtime_revision_id,
                runtime_state_revision=None
                if before is None or not same_runtime
                else before.state_revision,
                container_id=None
                if before is None or not same_runtime
                else before.container_id,
                capture_id=None,
                captured_at=None,
            )
            refresh = result.screenshot_refresh
            if (
                same_runtime
                and refresh.capture_id is not None
                and refresh.captured_at is not None
            ):
                with suppress(Exception):
                    status = self._screenshots.get_status(result.target_id)
                    if (
                        status.capture_id == refresh.capture_id
                        and status.captured_at == refresh.captured_at
                    ):
                        row.capture_id = str(refresh.capture_id)
                        row.captured_at = _timestamp_to_text(refresh.captured_at)
            try:
                with self._sessions.begin() as session:
                    session.add(row)
            except IntegrityError:
                with self._sessions() as session:
                    existing_row = session.get(McpRemoteObservationRow, row.receipt_id)
                    if existing_row is None:
                        raise
                    return self._read(existing_row, authorize_inspect)
            try:
                authorize_inspect()
            except Exception:
                row.ui_status, row.error_code = "failed", "observation_denied"
            else:
                try:
                    ui = inspect()
                    if not same_runtime or self.identity(result.target_id) != before:
                        row.ui_status, row.error_code = "failed", "runtime_changed"
                    else:
                        payload = ui.model_dump_json()
                        if len(payload.encode()) > 16384:
                            raise ValueError("Bounded UI observation exceeded")
                        row.ui_status, row.ui_json = "succeeded", payload
                except Exception:
                    row.ui_status, row.error_code = "failed", "observation_unavailable"
            row.observed_at = _timestamp_to_text(self._clock())
            with self._sessions.begin() as session:
                session.merge(row)
            return self._read(row, authorize_inspect)
        except Exception:
            # The action already has a durable outcome. Never turn an observation
            # persistence error into an apparent navigation failure or redispatch.
            return RemotePostActionObservation(
                status="failed", error_code="observation_persistence_failed"
            )

    @staticmethod
    def _read(
        row: McpRemoteObservationRow, authorize: Callable[[], object]
    ) -> RemotePostActionObservation:
        if row.ui_status == "succeeded":
            try:
                authorize()
            except Exception:
                return RemotePostActionObservation(
                    status="failed", error_code="observation_denied"
                )
        return RemotePostActionObservation.model_validate(
            {
                "status": row.ui_status,
                "error_code": row.error_code,
                "observed_at": row.observed_at,
                "runtime_state_revision": row.runtime_state_revision,
                "ui_state": None
                if row.ui_json is None
                else UiObservation.model_validate_json(row.ui_json),
            }
        )

    def retrieve(
        self,
        client_id: UUID,
        target_id: UUID,
        request_id: UUID,
        capture_id: UUID,
    ) -> tuple[ManagedScreenshotStatus, bytes, int, int]:
        with self._sessions() as session:
            row = session.scalar(
                select(McpRemoteObservationRow)
                .join(
                    McpRemoteActionReceiptRow,
                    McpRemoteObservationRow.receipt_id == McpRemoteActionReceiptRow.id,
                )
                .where(
                    McpRemoteActionReceiptRow.client_id == str(client_id),
                    McpRemoteActionReceiptRow.target_id == str(target_id),
                    McpRemoteActionReceiptRow.request_id == str(request_id),
                )
            )
        if row is None or row.capture_id != str(capture_id) or row.captured_at is None:
            raise CaptureReuseError("capture_not_retained")
        captured_at = datetime.fromisoformat(row.captured_at.replace("Z", "+00:00"))
        age = self._clock() - captured_at
        if age < timedelta(0) or age > CAPTURE_REUSE_RETENTION:
            raise CaptureReuseError("capture_expired")
        expected = RuntimeObservationBinding(
            row.runtime_revision_id or "",
            row.runtime_state_revision or 0,
            row.container_id or "",
        )
        if self.identity(target_id) != expected:
            raise CaptureReuseError("capture_runtime_changed")
        status, png = self._screenshots.read_capture(target_id, capture_id)
        if status.captured_at != captured_at or self.identity(target_id) != expected:
            raise CaptureReuseError("capture_runtime_changed")
        age = self._clock() - captured_at
        if age < timedelta(0) or age > CAPTURE_REUSE_RETENTION:
            raise CaptureReuseError("capture_expired")
        return status, png, int(age.total_seconds()), expected.state_revision
