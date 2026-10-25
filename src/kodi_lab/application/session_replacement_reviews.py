"""Durable Fresh/Switch review and evidence holds, without lifecycle dispatch.

One add-on, one current session and one open review. A saved review is historical
intent, not disposal authority or proof of current readiness. No worker runs it.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from kodi_lab.application.development_sessions import DevelopmentSessionService
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.retained_diagnostics import MAX_PAYLOAD_BYTES, MIN_FREE_BYTES
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.errors import ClientError
from kodi_lab.broker.stopped_logs import StoppedLogSnapshot
from kodi_lab.domain import AgentWorkspaceBytesState, AgentWorkspaceState, InstanceType
from kodi_lab.storage.development_sessions import active_session
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import (
    DevelopmentSessionEvidenceHoldRow,
    DevelopmentSessionReplacementReviewRow,
    DevelopmentSessionReplacementRow,
    ManagedDiagnosticCaptureRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
    ManagedInstanceRepository,
    RuntimeContainerArtifactRepository,
)

MAX_REVIEWS = 128
MAX_SNAPSHOT_BYTES = 16384


class SessionReplacementReviewError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ReplacementSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: int = Field(ge=1)
    expected_runtime_revision_id: UUID
    expected_container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expected_workspace_generation: int = Field(ge=0)
    expected_workspace_tree_digest: str | None = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    next_target_id: UUID
    next_runtime_revision_id: UUID
    next_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ReplacementEvidenceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capture_id: UUID
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReplacementReviewRequest(ReplacementSelection):
    request_id: UUID
    evidence: list[ReplacementEvidenceSelection] = Field(min_length=1, max_length=8)
    confirm_review: Literal[True]
    acknowledge_uncollected_crash_reports: Literal[True]

    @field_validator(
        "confirm_review", "acknowledge_uncollected_crash_reports", mode="before"
    )
    @classmethod
    def exact_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("explicit boolean confirmation required")
        return value

    @field_validator("evidence")
    @classmethod
    def distinct_evidence(
        cls, value: list[ReplacementEvidenceSelection]
    ) -> list[ReplacementEvidenceSelection]:
        if len({item.capture_id for item in value}) != len(value):
            raise ValueError("duplicate capture selection")
        return sorted(value, key=lambda item: str(item.capture_id))


class ReplacementReviewSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: ReplacementReviewRequest
    target_id: UUID
    workspace_id: UUID
    source_id: UUID
    addon_id: str = Field(min_length=1, max_length=128)
    mode: Literal["fresh_test", "switch_version"]
    observed_at: datetime
    native_crash_reports: Literal["not_collected"] = "not_collected"
    profile_disposal_authorized: Literal[False] = False
    preserves: tuple[str, ...] = (
        "source",
        "workspace",
        "git_history",
        "selected_evidence",
    )


class ReplacementReviewStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: UUID
    state: Literal["open", "cancelled", "completed"]
    snapshot_sha256: str
    snapshot: ReplacementReviewSnapshot
    cancelled_at: datetime | None
    successor_generation: int | None = Field(default=None, ge=2)
    executes_replacement: Literal[False] = False


class ReplacementReviewCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_cancel: Literal[True]

    @field_validator("confirm_cancel", mode="before")
    @classmethod
    def exact_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("explicit boolean confirmation required")
        return value


class SessionReplacementReviewService:
    def __init__(self, sessions: DevelopmentSessionService) -> None:
        self._sessions = sessions
        # Stored reconciliation only: no broker/native probe, sweep or repair.
        self._reconciliation = ManagedInstanceReconciliationService(sessions._sessions)

    def _scope(
        self, session: Session, next_target_id: UUID
    ) -> tuple[ReplacementSelection, UUID, UUID, UUID, str]:
        current = active_session(session)
        if current is None or current.phase != "bound" or current.container_id is None:
            raise SessionReplacementReviewError(
                "session_replacement_bound_session_required"
            )
        self._sessions._idle(session)
        instance = ManagedInstanceRepository(session).get(UUID(current.target_id))
        next_instance = ManagedInstanceRepository(session).get(next_target_id)
        artifacts = RuntimeContainerArtifactRepository(session)
        old_artifact = artifacts.get(UUID(current.runtime_revision_id))
        next_artifact = (
            None
            if next_instance is None
            else artifacts.get(next_instance.runtime_revision_id)
        )
        workspace = AgentWorkspaceRepository(session).get(UUID(current.workspace_id))
        source = DevelopmentSourceRepository(session).get(UUID(current.source_id))
        if (
            instance is None
            or instance.runtime_revision_id != UUID(current.runtime_revision_id)
            or instance.instance_type is not InstanceType.DISPOSABLE
            or next_instance is None
            or next_instance.instance_type is not InstanceType.DISPOSABLE
            or old_artifact is None
            or next_artifact is None
            or workspace is None
            or workspace.state is not AgentWorkspaceState.READY
            or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
            or source is None
            or workspace.development_source_id != source.id
            or workspace.source_addon_id != source.addon_id
        ):
            raise SessionReplacementReviewError(
                "session_replacement_selection_unavailable"
            )
        self._sessions._runtime(
            session, next_target_id, next_instance.runtime_revision_id
        )
        for target_id in {UUID(current.target_id), next_target_id}:
            status = self._reconciliation.status(target_id)
            if status.required or status.blocking:
                raise SessionReplacementReviewError(
                    "session_replacement_reconciliation_required"
                )
        records = self._sessions._inventory().containers
        if len(records) != 1 or (
            records[0].managed_instance_id,
            records[0].runtime_revision_id,
            records[0].container_id,
            records[0].image_id,
            records[0].runtime_state,
        ) != (
            current.target_id,
            current.runtime_revision_id,
            current.container_id,
            old_artifact.image_id,
            ContainerRuntimeState.STOPPED,
        ):
            raise SessionReplacementReviewError(
                "session_replacement_stopped_identity_required"
            )
        selection = ReplacementSelection(
            expected_generation=current.generation,
            expected_runtime_revision_id=UUID(current.runtime_revision_id),
            expected_container_id=current.container_id,
            expected_image_id=old_artifact.image_id,
            expected_workspace_generation=workspace.generation,
            expected_workspace_tree_digest=workspace.current_tree_digest,
            next_target_id=next_target_id,
            next_runtime_revision_id=next_instance.runtime_revision_id,
            next_image_id=next_artifact.image_id,
        )
        return (
            selection,
            UUID(current.target_id),
            workspace.id,
            source.id,
            source.addon_id,
        )

    def offer(self, next_target_id: UUID) -> ReplacementSelection:
        """Read current selection; an offer never reserves or authorizes anything."""
        with self._sessions._sessions() as session:
            return self._scope(session, next_target_id)[0]

    @staticmethod
    def _validate_evidence(
        session: Session, operation: ReplacementReviewRequest, target_id: UUID
    ) -> None:
        for selected in operation.evidence:
            row = session.get(ManagedDiagnosticCaptureRow, str(selected.capture_id))
            if (
                row is None
                or (
                    row.state,
                    row.target_id,
                    row.runtime_revision_id,
                    row.container_id,
                    row.image_id,
                    row.payload_sha256,
                )
                != (
                    "retained",
                    str(target_id),
                    str(operation.expected_runtime_revision_id),
                    operation.expected_container_id,
                    operation.expected_image_id,
                    selected.payload_sha256,
                )
                or row.payload_json is None
            ):
                raise SessionReplacementReviewError(
                    "session_replacement_evidence_changed"
                )
            payload = row.payload_json.encode()
            try:
                if (
                    len(payload) > MAX_PAYLOAD_BYTES
                    or hashlib.sha256(payload).hexdigest() != selected.payload_sha256
                ):
                    raise ValueError("invalid digest")
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
                    raise ValueError("invalid binding")
            except ValueError:
                raise SessionReplacementReviewError(
                    "session_replacement_evidence_invalid"
                ) from None

    @staticmethod
    def _status(
        session: Session, row: DevelopmentSessionReplacementReviewRow
    ) -> ReplacementReviewStatus:
        try:
            encoded = row.snapshot_json.encode()
            if (
                len(encoded) > MAX_SNAPSHOT_BYTES
                or hashlib.sha256(encoded).hexdigest() != row.snapshot_sha256
            ):
                raise ValueError("invalid digest")
            snapshot = ReplacementReviewSnapshot.model_validate_json(encoded)
            if (
                snapshot.request.expected_generation != row.session_generation
                or str(snapshot.request.request_id) != row.request_id
            ):
                raise ValueError("invalid identity")
            holds = {
                (item.capture_id, item.payload_sha256)
                for item in session.scalars(
                    select(DevelopmentSessionEvidenceHoldRow).where(
                        DevelopmentSessionEvidenceHoldRow.review_id == row.id
                    )
                )
            }
            if holds != {
                (str(item.capture_id), item.payload_sha256)
                for item in snapshot.request.evidence
            }:
                raise ValueError("invalid holds")
            return ReplacementReviewStatus.model_validate(
                dict(
                    review_id=row.id,
                    state="completed"
                    if row.successor_generation is not None
                    else row.state,
                    snapshot_sha256=row.snapshot_sha256,
                    snapshot=snapshot,
                    cancelled_at=row.cancelled_at,
                    successor_generation=row.successor_generation,
                )
            )
        except ValueError:
            raise SessionReplacementReviewError(
                "session_replacement_review_invalid"
            ) from None

    def create(self, operation: ReplacementReviewRequest) -> ReplacementReviewStatus:
        try:
            with self._sessions._sessions() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                prior = session.scalar(
                    select(DevelopmentSessionReplacementReviewRow).where(
                        DevelopmentSessionReplacementReviewRow.request_id
                        == str(operation.request_id)
                    )
                )
                if prior is not None:
                    result = self._status(session, prior)
                    if result.snapshot.request != operation:
                        raise SessionReplacementReviewError(
                            "session_replacement_request_conflict"
                        )
                    return result
                if (
                    session.scalar(
                        select(DevelopmentSessionReplacementReviewRow).where(
                            DevelopmentSessionReplacementReviewRow.state == "open",
                            DevelopmentSessionReplacementReviewRow.successor_generation.is_(
                                None
                            ),
                        )
                    )
                    is not None
                ):
                    raise SessionReplacementReviewError(
                        "session_replacement_review_already_open"
                    )
                if (
                    session.scalar(
                        select(func.count()).select_from(
                            DevelopmentSessionReplacementReviewRow
                        )
                    )
                    or 0
                ) >= MAX_REVIEWS:
                    raise SessionReplacementReviewError(
                        "session_replacement_history_capacity"
                    )
                selection, target_id, workspace_id, source_id, addon_id = self._scope(
                    session, operation.next_target_id
                )
                if selection != ReplacementSelection.model_validate(
                    operation.model_dump(include=set(ReplacementSelection.model_fields))
                ):
                    raise SessionReplacementReviewError(
                        "session_replacement_selection_changed"
                    )
                self._validate_evidence(session, operation, target_id)
                roots = {self._sessions._data_root}
                engine = self._sessions._sessions.kw.get("bind")
                database = getattr(getattr(engine, "url", None), "database", None)
                if isinstance(database, str) and database != ":memory:":
                    roots.add(Path(database).resolve().parent)
                if any(
                    shutil.disk_usage(root).free < MIN_FREE_BYTES + MAX_SNAPSHOT_BYTES
                    for root in roots
                ):
                    raise SessionReplacementReviewError(
                        "session_replacement_storage_reserve"
                    )
                now = datetime.now(UTC)
                snapshot = ReplacementReviewSnapshot(
                    request=operation,
                    target_id=target_id,
                    workspace_id=workspace_id,
                    source_id=source_id,
                    addon_id=addon_id,
                    observed_at=now,
                    mode="fresh_test"
                    if target_id == operation.next_target_id
                    else "switch_version",
                )
                payload = snapshot.model_dump_json()
                if len(payload.encode()) > MAX_SNAPSHOT_BYTES:
                    raise SessionReplacementReviewError(
                        "session_replacement_snapshot_bound"
                    )
                row = DevelopmentSessionReplacementReviewRow(
                    id=str(uuid4()),
                    request_id=str(operation.request_id),
                    session_generation=operation.expected_generation,
                    state="open",
                    snapshot_json=payload,
                    snapshot_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                    created_at=timestamp(now),
                )
                session.add(row)
                session.flush()
                for item in operation.evidence:
                    session.add(
                        DevelopmentSessionEvidenceHoldRow(
                            review_id=row.id,
                            capture_id=str(item.capture_id),
                            payload_sha256=item.payload_sha256,
                        )
                    )
                session.flush()
                result = self._status(session, row)
                session.commit()
                return result
        except (SQLAlchemyError, OSError, ClientError):
            raise SessionReplacementReviewError(
                "session_replacement_review_unavailable"
            ) from None

    def active(self) -> ReplacementReviewStatus | None:
        with self._sessions._sessions() as session:
            row = session.scalar(
                select(DevelopmentSessionReplacementReviewRow).where(
                    DevelopmentSessionReplacementReviewRow.state == "open",
                    DevelopmentSessionReplacementReviewRow.successor_generation.is_(
                        None
                    ),
                )
            )
            return None if row is None else self._status(session, row)

    def get(self, review_id: UUID) -> ReplacementReviewStatus:
        with self._sessions._sessions() as session:
            row = session.get(DevelopmentSessionReplacementReviewRow, str(review_id))
            if row is None:
                raise SessionReplacementReviewError(
                    "session_replacement_review_not_found"
                )
            return self._status(session, row)

    def cancel(
        self, review_id: UUID, operation: ReplacementReviewCancel
    ) -> ReplacementReviewStatus:
        with self._sessions._sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(DevelopmentSessionReplacementReviewRow, str(review_id))
            if row is None:
                raise SessionReplacementReviewError(
                    "session_replacement_review_not_found"
                )
            result = self._status(session, row)
            if result.snapshot_sha256 != operation.expected_snapshot_sha256:
                raise SessionReplacementReviewError(
                    "session_replacement_review_changed"
                )
            replacement = session.get(DevelopmentSessionReplacementRow, str(review_id))
            if row.successor_generation is not None or (
                replacement is not None and replacement.released_at is None
            ):
                raise SessionReplacementReviewError(
                    "session_replacement_execution_requires_review"
                )
            if row.state == "open":
                row.state, row.cancelled_at = "cancelled", timestamp(datetime.now(UTC))
                session.commit()
            return self._status(session, row)
