"""Explicit Web-admin retention of bounded stopped-runtime evidence.

Only new application rows change. No Kodi mutation, lease repair, grant,
background pruning or automatic disposal. Captures survive resource deletion.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.errors import ClientError, RemoteBrokerError
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.broker.stopped_logs import StoppedLogSnapshot
from kodi_lab.domain import InstanceType, TargetType
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import (
    DevelopmentSessionEvidenceHoldRow,
    DevelopmentSessionReplacementReviewRow,
    ManagedDiagnosticCaptureRow,
    ManagedInstanceRow,
    McpWorkspaceDeploymentOperationRow,
    TargetRow,
)
from kodi_lab.storage.repositories import RuntimeContainerArtifactRepository

MAX_PAYLOAD_BYTES = 49152
MAX_BODIES = 64
MAX_HISTORY = 256
MIN_FREE_BYTES = 2 * 1024**3
REVIEW_DAYS = 30


class RetainedDiagnosticsError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DiagnosticCaptureInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_runtime_revision_id: UUID
    expected_container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    deployment_operation_id: UUID | None = None
    retain_redacted_logs: Literal[True]

    @field_validator("retain_redacted_logs", mode="before")
    @classmethod
    def exact_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("explicit boolean confirmation required")
        return value


class DiagnosticDiscardInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_state: Literal["capturing", "retained", "failed"]
    expected_payload_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirm_discard: Literal[True]

    @field_validator("confirm_discard", mode="before")
    @classmethod
    def exact_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("explicit boolean confirmation required")
        return value


class DiagnosticOperationContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relationship: Literal["same_container_after_deployment"] = (
        "same_container_after_deployment"
    )
    operation_id: UUID
    workspace_id: UUID
    generation: int = Field(ge=0)
    addon_id: str = Field(min_length=1, max_length=128)
    state: Literal["succeeded", "failed", "ambiguous", "reconciliation_required"]
    phase: str = Field(min_length=1, max_length=32)
    completed_at: datetime


class DiagnosticCaptureSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capture_id: UUID
    request_id: UUID
    target_id: UUID
    runtime_revision_id: UUID
    container_id: str
    image_id: str
    state: Literal["capturing", "retained", "failed", "discarded"]
    created_at: datetime
    completed_at: datetime | None
    review_after: datetime
    review_due: bool
    discarded_at: datetime | None
    payload_sha256: str | None
    failure_code: str | None
    operation_context: DiagnosticOperationContext | None
    authorizes_profile_disposal: Literal[False] = False
    access: Literal["web_administrator_only"] = "web_administrator_only"


class RetainedDiagnosticEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capture: DiagnosticCaptureSummary
    logs: StoppedLogSnapshot


class _Broker(Protocol):
    def managed_container_inventory(
        self, *, managed_instance_id: str | None = None
    ) -> ManagedContainerInventoryResult: ...
    def read_stopped_managed_logs(
        self,
        *,
        managed_instance_id: str,
        expected_runtime_revision_id: str,
        expected_container_id: str,
    ) -> StoppedLogSnapshot: ...


def _safe_failure(error: BaseException) -> str:
    if isinstance(error, RetainedDiagnosticsError):
        return error.code
    if isinstance(error, RemoteBrokerError) and error.code in {
        "managed_diagnostics_identity_changed",
        "managed_diagnostics_not_stopped",
        "managed_diagnostics_deadline",
        "managed_diagnostics_unavailable",
        "managed_diagnostics_private_profile_required",
        "managed_diagnostics_log_layout_unsupported",
        "docker_daemon_unavailable",
    }:
        return error.code
    return "diagnostic_capture_unavailable"


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _summary(
    row: ManagedDiagnosticCaptureRow, now: datetime
) -> DiagnosticCaptureSummary:
    context = json.loads(row.context_json)
    return DiagnosticCaptureSummary(
        capture_id=UUID(row.id),
        request_id=UUID(row.request_id),
        target_id=UUID(row.target_id),
        runtime_revision_id=UUID(row.runtime_revision_id),
        container_id=row.container_id,
        image_id=row.image_id,
        state=cast(Literal["capturing", "retained", "failed", "discarded"], row.state),
        created_at=datetime.fromisoformat(row.created_at),
        completed_at=None
        if row.completed_at is None
        else datetime.fromisoformat(row.completed_at),
        review_after=datetime.fromisoformat(row.review_after),
        review_due=now >= datetime.fromisoformat(row.review_after),
        discarded_at=None
        if row.discarded_at is None
        else datetime.fromisoformat(row.discarded_at),
        payload_sha256=row.payload_sha256,
        failure_code=row.failure_code,
        operation_context=DiagnosticOperationContext.model_validate(context)
        if context
        else None,
    )


class RetainedDiagnosticsService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        broker: _Broker,
        *,
        data_root: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions, self._broker, self._data_root = sessions, broker, data_root
        self._clock = clock or (lambda: datetime.now(UTC))

    def _context(
        self, session: Session, target_id: UUID, operation: DiagnosticCaptureInput
    ) -> str:
        if operation.deployment_operation_id is None:
            return "{}"
        row = session.get(
            McpWorkspaceDeploymentOperationRow, str(operation.deployment_operation_id)
        )
        if (
            row is None
            or row.target_class != "internal_lab"
            or row.deployment_mode != "direct"
            or row.target_id != str(target_id)
            or row.runtime_revision_id != str(operation.expected_runtime_revision_id)
            or row.original_container_id != operation.expected_container_id
            or row.state
            not in {"succeeded", "failed", "ambiguous", "reconciliation_required"}
            or row.completed_at is None
        ):
            raise RetainedDiagnosticsError("diagnostic_operation_binding_unavailable")
        try:
            value = DiagnosticOperationContext.model_validate(
                dict(
                    operation_id=row.id,
                    workspace_id=row.workspace_id,
                    generation=row.generation,
                    addon_id=row.addon_id,
                    state=row.state,
                    phase=row.phase,
                    completed_at=row.completed_at,
                )
            )
            if value.completed_at.tzinfo is None or value.completed_at > self._clock():
                raise ValueError("operation completion is not established")
        except ValueError:
            raise RetainedDiagnosticsError(
                "diagnostic_operation_binding_unavailable"
            ) from None
        return _json(value.model_dump(mode="json"))

    def _scope(
        self, session: Session, target_id: UUID, operation: DiagnosticCaptureInput
    ) -> str:
        target = session.get(TargetRow, str(target_id))
        instance = session.get(ManagedInstanceRow, str(target_id))
        artifact = RuntimeContainerArtifactRepository(session).get(
            operation.expected_runtime_revision_id
        )
        if (
            target is None
            or target.target_type is not TargetType.MANAGED
            or instance is None
            or instance.instance_type is not InstanceType.DISPOSABLE
            or instance.runtime_revision_id
            != str(operation.expected_runtime_revision_id)
            or artifact is None
        ):
            raise RetainedDiagnosticsError("diagnostic_target_binding_changed")
        inventory = self._broker.managed_container_inventory(
            managed_instance_id=str(target_id)
        ).containers
        if (
            len(inventory) != 1
            or inventory[0].managed_instance_id != str(target_id)
            or inventory[0].container_id != operation.expected_container_id
            or inventory[0].runtime_revision_id
            != str(operation.expected_runtime_revision_id)
            or inventory[0].image_id != artifact.image_id
            or inventory[0].runtime_state is not ContainerRuntimeState.STOPPED
        ):
            raise RetainedDiagnosticsError("diagnostic_target_binding_changed")
        return artifact.image_id

    @staticmethod
    def _same_request(
        row: ManagedDiagnosticCaptureRow,
        target_id: UUID,
        operation: DiagnosticCaptureInput,
    ) -> None:
        stored = json.loads(row.context_json)
        op_id = stored.get("operation_id")
        if (row.target_id, row.runtime_revision_id, row.container_id, op_id) != (
            str(target_id),
            str(operation.expected_runtime_revision_id),
            operation.expected_container_id,
            None
            if operation.deployment_operation_id is None
            else str(operation.deployment_operation_id),
        ):
            raise RetainedDiagnosticsError("diagnostic_request_conflict")

    def capture(
        self, target_id: UUID, operation: DiagnosticCaptureInput
    ) -> DiagnosticCaptureSummary:
        try:
            with self._sessions() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                prior = session.scalar(
                    select(ManagedDiagnosticCaptureRow).where(
                        ManagedDiagnosticCaptureRow.request_id
                        == str(operation.request_id)
                    )
                )
                if prior is not None:
                    self._same_request(prior, target_id, operation)
                    return _summary(prior, self._clock())
                if (
                    session.scalar(
                        select(func.count()).select_from(ManagedDiagnosticCaptureRow)
                    )
                    or 0
                ) >= MAX_HISTORY:
                    raise RetainedDiagnosticsError("diagnostic_history_capacity")
                occupied = session.scalar(
                    select(func.count())
                    .select_from(ManagedDiagnosticCaptureRow)
                    .where(
                        ManagedDiagnosticCaptureRow.state.in_(["capturing", "retained"])
                    )
                )
                if (occupied or 0) >= MAX_BODIES:
                    raise RetainedDiagnosticsError("diagnostic_body_capacity")
                if (
                    session.scalar(
                        select(ManagedDiagnosticCaptureRow.id).where(
                            ManagedDiagnosticCaptureRow.state == "capturing",
                        )
                    )
                    is not None
                ):
                    raise RetainedDiagnosticsError("diagnostic_capture_busy")
                roots = {self._data_root}
                engine = self._sessions.kw.get("bind")
                database = getattr(getattr(engine, "url", None), "database", None)
                if isinstance(database, str) and database != ":memory:":
                    roots.add(Path(database).resolve().parent)
                if any(
                    shutil.disk_usage(root).free < MIN_FREE_BYTES + MAX_PAYLOAD_BYTES
                    for root in roots
                ):
                    raise RetainedDiagnosticsError("diagnostic_storage_reserve")
                image_id = self._scope(session, target_id, operation)
                context = self._context(session, target_id, operation)
                now = self._clock()
                capture_id = str(uuid4())
                row = ManagedDiagnosticCaptureRow(
                    id=capture_id,
                    request_id=str(operation.request_id),
                    target_id=str(target_id),
                    runtime_revision_id=str(operation.expected_runtime_revision_id),
                    container_id=operation.expected_container_id,
                    image_id=image_id,
                    context_json=context,
                    state="capturing",
                    created_at=timestamp(now),
                    review_after=timestamp(now + timedelta(days=REVIEW_DAYS)),
                )
                session.add(row)
                session.commit()
        except (SQLAlchemyError, OSError):
            raise RetainedDiagnosticsError("diagnostic_storage_unavailable") from None
        except ClientError as error:
            raise RetainedDiagnosticsError(_safe_failure(error)) from None
        try:
            raw = self._broker.read_stopped_managed_logs(
                managed_instance_id=str(target_id),
                expected_runtime_revision_id=str(
                    operation.expected_runtime_revision_id
                ),
                expected_container_id=operation.expected_container_id,
            )
            logs = StoppedLogSnapshot.model_validate(raw.model_dump(mode="json"))
            observed = logs.exit_metadata
            if (
                observed.managed_instance_id,
                observed.runtime_revision_id,
                observed.container_id,
                observed.image_id,
            ) != (
                str(target_id),
                str(operation.expected_runtime_revision_id),
                operation.expected_container_id,
                image_id,
            ):
                raise RetainedDiagnosticsError("diagnostic_target_binding_changed")
            payload = _json(logs.model_dump(mode="json"))
            if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                raise RetainedDiagnosticsError("diagnostic_payload_bound")
            return self._publish(capture_id, target_id, operation, context, payload)
        except BaseException as error:
            code = _safe_failure(error)
            # A failed failure marker leaves pending evidence for explicit review.
            with suppress(SQLAlchemyError):
                self._fail(capture_id, code)
            if not isinstance(error, Exception):
                raise
            raise RetainedDiagnosticsError(code) from None

    def _publish(
        self,
        capture_id: str,
        target_id: UUID,
        operation: DiagnosticCaptureInput,
        context: str,
        payload: str,
    ) -> DiagnosticCaptureSummary:
        with self._sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(ManagedDiagnosticCaptureRow, capture_id)
            if row is None or row.state != "capturing":
                raise RetainedDiagnosticsError("diagnostic_capture_state_changed")
            if (
                self._scope(session, target_id, operation) != row.image_id
                or self._context(session, target_id, operation) != context
            ):
                raise RetainedDiagnosticsError("diagnostic_target_binding_changed")
            row.state, row.payload_json = "retained", payload
            row.payload_sha256 = hashlib.sha256(payload.encode()).hexdigest()
            row.completed_at = timestamp(self._clock())
            session.commit()
            return _summary(row, self._clock())

    def _fail(self, capture_id: str, code: str) -> None:
        with self._sessions.begin() as session:
            # Never erase a publication committed before cancellation/readback failure.
            session.execute(
                update(ManagedDiagnosticCaptureRow)
                .where(
                    ManagedDiagnosticCaptureRow.id == capture_id,
                    ManagedDiagnosticCaptureRow.state == "capturing",
                )
                .values(
                    state="failed",
                    completed_at=timestamp(self._clock()),
                    failure_code=code,
                )
            )

    def list(self, target_id: UUID | None = None) -> list[DiagnosticCaptureSummary]:
        with self._sessions() as session:
            query = (
                select(ManagedDiagnosticCaptureRow)
                .order_by(
                    ManagedDiagnosticCaptureRow.created_at.desc(),
                    ManagedDiagnosticCaptureRow.id,
                )
                .limit(MAX_HISTORY)
            )
            if target_id is not None:
                query = query.where(
                    ManagedDiagnosticCaptureRow.target_id == str(target_id)
                )
            return [_summary(row, self._clock()) for row in session.scalars(query)]

    def status(self, capture_id: UUID) -> DiagnosticCaptureSummary:
        with self._sessions() as session:
            row = session.get(ManagedDiagnosticCaptureRow, str(capture_id))
            if row is None:
                raise RetainedDiagnosticsError("diagnostic_capture_not_found")
            return _summary(row, self._clock())

    def read(self, capture_id: UUID) -> RetainedDiagnosticEvidence:
        with self._sessions() as session:
            row = session.get(ManagedDiagnosticCaptureRow, str(capture_id))
            if row is None:
                raise RetainedDiagnosticsError("diagnostic_capture_not_found")
            if (
                row.state != "retained"
                or row.payload_json is None
                or row.payload_sha256 is None
            ):
                raise RetainedDiagnosticsError("diagnostic_body_unavailable")
            payload = row.payload_json
            if (
                len(payload.encode()) > MAX_PAYLOAD_BYTES
                or hashlib.sha256(payload.encode()).hexdigest() != row.payload_sha256
            ):
                raise RetainedDiagnosticsError("diagnostic_evidence_invalid")
            try:
                logs = StoppedLogSnapshot.model_validate_json(payload)
                observed = logs.exit_metadata
                if (
                    observed.managed_instance_id,
                    observed.runtime_revision_id,
                    observed.container_id,
                    observed.image_id,
                ) != (
                    row.target_id,
                    row.runtime_revision_id,
                    row.container_id,
                    row.image_id,
                ):
                    raise ValueError("binding mismatch")
            except ValueError:
                raise RetainedDiagnosticsError("diagnostic_evidence_invalid") from None
            return RetainedDiagnosticEvidence(
                capture=_summary(row, self._clock()), logs=logs
            )

    def discard(
        self, capture_id: UUID, operation: DiagnosticDiscardInput
    ) -> DiagnosticCaptureSummary:
        with self._sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(ManagedDiagnosticCaptureRow, str(capture_id))
            if row is None:
                raise RetainedDiagnosticsError("diagnostic_capture_not_found")
            if (
                session.scalar(
                    select(DevelopmentSessionEvidenceHoldRow)
                    .join(DevelopmentSessionReplacementReviewRow)
                    .where(
                        DevelopmentSessionEvidenceHoldRow.capture_id == row.id,
                        DevelopmentSessionReplacementReviewRow.state == "open",
                    )
                    .limit(1)
                )
                is not None
            ):
                raise RetainedDiagnosticsError(
                    "diagnostic_capture_held_for_session_review"
                )
            if (
                row.payload_sha256 != operation.expected_payload_sha256
                or row.state not in {operation.expected_state, "discarded"}
            ):
                raise RetainedDiagnosticsError("diagnostic_capture_state_changed")
            if row.state != "discarded":
                row.state, row.payload_json, row.failure_code = "discarded", None, None
                row.discarded_at = timestamp(self._clock())
                row.completed_at = row.completed_at or row.discarded_at
                session.commit()
            return _summary(row, self._clock())
