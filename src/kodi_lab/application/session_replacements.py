"""Explicit sequential profile replacement over existing lifecycle and admission.

No scheduler or general instance manager. A request binds one reviewed session
to one retirement, then atomically reserves its successor before any new create.
Status/replay never redispatches; only explicit continuation advances a confirmed
retirement. Unknown creates and removals keep their existing durable fences.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from kodi_lab.application.development_sessions import (
    DevelopmentSessionService,
    DevelopmentSessionStatus,
)
from kodi_lab.application.development_sessions import (
    _status as session_status,
)
from kodi_lab.application.lifecycle_operations import RunningLifecycleOperation
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationRequiredError,
    ManagedInstanceReconciliationService,
)
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_retirement_receipts import (
    RuntimeRetirementReceiptError,
    RuntimeRetirementScope,
    retirement_receipt_in_session,
)
from kodi_lab.application.session_replacement_reviews import (
    ReplacementReviewStatus,
    ReplacementSelection,
    SessionReplacementReviewError,
    SessionReplacementReviewService,
)
from kodi_lab.domain import (
    AgentWorkspaceBytesState,
    AgentWorkspaceState,
    InstanceType,
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceMutationKind,
)
from kodi_lab.storage.development_sessions import (
    DevelopmentSessionConflict,
    active_session,
)
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    DevelopmentSessionReplacementReviewRow,
    DevelopmentSessionReplacementRow,
    DevelopmentSessionRow,
    ManagedInstanceMutationLeaseRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
    ManagedInstanceLifecycleOperationRepository,
    ManagedInstanceRepository,
    RuntimeContainerArtifactRepository,
)

MAX_GENERATIONS = 256


class SessionReplacementError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SessionReplacementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirm_profile_disposal: Literal[True]

    @field_validator("confirm_profile_disposal", mode="before")
    @classmethod
    def exact_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("explicit boolean confirmation required")
        return value


class SessionReplacementStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: UUID
    request_id: UUID
    phase: Literal[
        "retirement_unconfirmed",
        "retired",
        "successor_reserved",
        "preparation_incomplete",
        "successor_bound",
        "successor_closed",
        "released_after_retirement",
    ]
    retirement_operation_id: UUID
    retirement_state: ManagedInstanceLifecycleOperationState
    retirement_confirmation: Literal["not_confirmed", "confirmed"]
    prepare_operation_id: UUID | None
    prepare_state: ManagedInstanceLifecycleOperationState | None = Field(
        description="Original guided preparation child; later actions are not guessed."
    )
    prepare_history: Literal["not_claimed", "retained", "not_retained"]
    successor: DevelopmentSessionStatus | None
    live_runtime_state: Literal["not_observed"] = "not_observed"
    native_and_bridge_setup: Literal["independent_review_required"] = (
        "independent_review_required"
    )
    deployment_baseline: Literal["not_inherited"] = "not_inherited"


def _scope(review: ReplacementReviewStatus) -> RuntimeRetirementScope:
    request = review.snapshot.request
    return RuntimeRetirementScope(
        managed_instance_id=review.snapshot.target_id,
        runtime_revision_id=request.expected_runtime_revision_id,
        container_id=request.expected_container_id,
        image_id=request.expected_image_id,
    )


class SessionReplacementService:
    def __init__(
        self,
        sessions: DevelopmentSessionService,
        lifecycle: ManagedRuntimeLifecycleService,
    ) -> None:
        self._sessions, self._lifecycle = sessions, lifecycle
        self._reviews = SessionReplacementReviewService(sessions)
        self._reconciliation = ManagedInstanceReconciliationService(sessions._sessions)

    @staticmethod
    def _review(
        session: Session, review_id: UUID, operation: SessionReplacementRequest
    ) -> ReplacementReviewStatus:
        row = session.get(DevelopmentSessionReplacementReviewRow, str(review_id))
        if row is None:
            raise SessionReplacementError("session_replacement_review_not_found")
        review = SessionReplacementReviewService._status(session, row)
        if (
            review.snapshot_sha256 != operation.expected_snapshot_sha256
            or operation.confirm_profile_disposal is not True
        ):
            raise SessionReplacementError("session_replacement_review_changed")
        return review

    @staticmethod
    def _prior(
        session: Session, review_id: UUID, operation: SessionReplacementRequest
    ) -> DevelopmentSessionReplacementRow | None:
        rows = session.scalars(
            select(DevelopmentSessionReplacementRow).where(
                or_(
                    DevelopmentSessionReplacementRow.review_id == str(review_id),
                    DevelopmentSessionReplacementRow.request_id
                    == str(operation.request_id),
                )
            )
        ).all()
        if not rows:
            return None
        if len(rows) != 1 or (
            rows[0].review_id,
            rows[0].request_id,
            rows[0].review_snapshot_sha256,
            rows[0].profile_disposal_confirmed,
        ) != (
            str(review_id),
            str(operation.request_id),
            operation.expected_snapshot_sha256,
            True,
        ):
            raise SessionReplacementError("session_replacement_request_conflict")
        return rows[0]

    def _fresh_review(
        self, session: Session, review_id: UUID, operation: SessionReplacementRequest
    ) -> ReplacementReviewStatus:
        review = self._review(session, review_id, operation)
        if (
            self._prior(session, review_id, operation) is not None
            or review.state != "open"
        ):
            raise SessionReplacementError("session_replacement_already_claimed")
        selection, target_id, workspace_id, source_id, addon_id = self._reviews._scope(
            session, review.snapshot.request.next_target_id
        )
        expected = ReplacementSelection.model_validate(
            review.snapshot.request.model_dump(
                include=set(ReplacementSelection.model_fields)
            )
        )
        if selection != expected or (target_id, workspace_id, source_id, addon_id) != (
            review.snapshot.target_id,
            review.snapshot.workspace_id,
            review.snapshot.source_id,
            review.snapshot.addon_id,
        ):
            raise SessionReplacementError("session_replacement_selection_changed")
        latest = session.scalar(select(func.max(DevelopmentSessionRow.generation))) or 0
        if latest != expected.expected_generation or latest >= MAX_GENERATIONS:
            raise SessionReplacementError("session_replacement_generation_capacity")
        self._reviews._validate_evidence(session, review.snapshot.request, target_id)
        return review

    def _bind(
        self,
        session: Session,
        handle: RunningLifecycleOperation,
        review_id: UUID,
        operation: SessionReplacementRequest,
    ) -> None:
        review = self._review(session, review_id, operation)
        lease = session.get(
            ManagedInstanceMutationLeaseRow, str(review.snapshot.target_id)
        )
        receipt = retirement_receipt_in_session(session, handle.operation_id)
        if (
            review.state != "open"
            or lease is None
            or lease.operation_kind is not ManagedInstanceMutationKind.LIFECYCLE
            or lease.operation_id != str(handle.operation_id)
            or lease.lease_token_digest != handle.token_digest
            or receipt.confirmation != "not_confirmed"
            or receipt.operation_state
            is not ManagedInstanceLifecycleOperationState.RUNNING
            or receipt.scope != _scope(review)
            or self._prior(session, review_id, operation) is not None
        ):
            raise SessionReplacementError("session_replacement_claim_changed")
        session.add(
            DevelopmentSessionReplacementRow(
                review_id=str(review_id),
                request_id=str(operation.request_id),
                review_snapshot_sha256=review.snapshot_sha256,
                retirement_operation_id=str(handle.operation_id),
                profile_disposal_confirmed=True,
                requested_at=timestamp(datetime.now(UTC)),
            )
        )
        session.flush()

    def _status(
        self, session: Session, row: DevelopmentSessionReplacementRow
    ) -> SessionReplacementStatus:
        review = self._review(
            session,
            UUID(row.review_id),
            SessionReplacementRequest(
                request_id=UUID(row.request_id),
                expected_snapshot_sha256=row.review_snapshot_sha256,
                confirm_profile_disposal=True,
            ),
        )
        receipt = retirement_receipt_in_session(
            session, UUID(row.retirement_operation_id)
        )
        if receipt.scope != _scope(review) or not row.profile_disposal_confirmed:
            raise SessionReplacementError("session_replacement_history_invalid")
        old = session.get(
            DevelopmentSessionRow, review.snapshot.request.expected_generation
        )
        if (
            old is None
            or (
                old.target_id,
                old.runtime_revision_id,
                old.workspace_id,
                old.source_id,
            )
            != (
                str(review.snapshot.target_id),
                str(review.snapshot.request.expected_runtime_revision_id),
                str(review.snapshot.workspace_id),
                str(review.snapshot.source_id),
            )
            or (
                (review.successor_generation is not None or row.released_at is not None)
                and old.phase != "closed"
            )
        ):
            raise SessionReplacementError("session_replacement_history_invalid")
        successor = None
        phase = (
            "retirement_unconfirmed"
            if receipt.confirmation != "confirmed"
            else "retired"
        )
        if review.successor_generation is not None:
            current = session.get(DevelopmentSessionRow, review.successor_generation)
            selection = review.snapshot.request
            if (
                current is None
                or receipt.confirmation != "confirmed"
                or row.released_at is not None
                or (
                    current.request_id,
                    current.target_id,
                    current.runtime_revision_id,
                    current.workspace_id,
                    current.source_id,
                )
                != (
                    str(uuid5(UUID(row.review_id), "successor-session")),
                    str(selection.next_target_id),
                    str(selection.next_runtime_revision_id),
                    str(review.snapshot.workspace_id),
                    str(review.snapshot.source_id),
                )
            ):
                raise SessionReplacementError("session_replacement_history_invalid")
            successor = session_status(current)
            phase = {
                "reserved": "successor_reserved",
                "bound": "successor_bound",
                "closed": "successor_closed",
            }.get(current.phase, "preparation_incomplete")
        if row.released_at is not None:
            if receipt.confirmation != "confirmed" or review.state != "cancelled":
                raise SessionReplacementError("session_replacement_history_invalid")
            phase = "released_after_retirement"
        preparation = (
            None
            if row.prepare_operation_id is None
            else ManagedInstanceLifecycleOperationRepository(session).get(
                UUID(row.prepare_operation_id)
            )
        )
        if preparation is not None and (
            successor is None
            or preparation.original_managed_instance_id != successor.target_id
            or preparation.kind.value != "provision"
        ):
            raise SessionReplacementError("session_replacement_history_invalid")
        return SessionReplacementStatus.model_validate(
            dict(
                review_id=row.review_id,
                request_id=row.request_id,
                phase=phase,
                retirement_operation_id=row.retirement_operation_id,
                retirement_state=receipt.operation_state,
                retirement_confirmation=receipt.confirmation,
                prepare_operation_id=row.prepare_operation_id,
                successor=successor,
                prepare_state=None if preparation is None else preparation.state,
                prepare_history="not_claimed"
                if row.prepare_operation_id is None
                else "not_retained"
                if preparation is None
                else "retained",
            )
        )

    def get(self, review_id: UUID) -> SessionReplacementStatus | None:
        """Stored status only; never observes, sweeps, repairs or redispatches."""
        with self._sessions._sessions() as session:
            row = session.get(DevelopmentSessionReplacementRow, str(review_id))
            return None if row is None else self._status(session, row)

    def execute(
        self, review_id: UUID, operation: SessionReplacementRequest
    ) -> SessionReplacementStatus:
        with self._sessions._sessions() as session:
            prior = self._prior(session, review_id, operation)
            if prior is not None:
                return self._status(session, prior)
            review = self._review(session, review_id, operation)

        def protect(session: Session) -> None:
            self._fresh_review(session, review_id, operation)

        try:
            self._lifecycle.deprovision(
                review.snapshot.target_id,
                expected_scope=_scope(review),
                operation_id=uuid5(review_id, "retirement"),
                precondition=protect,
                on_claim=lambda session, handle: self._bind(
                    session, handle, review_id, operation
                ),
            )
        except Exception as error:
            # A committed claim is inspectable, never retried from absence. A
            # pre-claim refusal has no dispatch authority or replacement record.
            with self._sessions._sessions() as session:
                prior = self._prior(session, review_id, operation)
                if prior is not None:
                    return self._status(session, prior)
            if isinstance(
                error,
                (
                    SessionReplacementError,
                    SessionReplacementReviewError,
                    DevelopmentSessionConflict,
                    RuntimeRetirementReceiptError,
                    ManagedInstanceReconciliationRequiredError,
                ),
            ):
                raise
            raise SessionReplacementError(
                "session_replacement_retirement_not_started"
            ) from None
        self._handoff(review_id, operation)
        self._prepare(review_id, operation)
        result = self.get(review_id)
        assert result is not None
        return result

    def _retired(
        self, session: Session, review_id: UUID, operation: SessionReplacementRequest
    ) -> tuple[
        DevelopmentSessionReplacementRow, ReplacementReviewStatus, DevelopmentSessionRow
    ]:
        row = self._prior(session, review_id, operation)
        review = self._review(session, review_id, operation)
        if row is None or row.released_at is not None or review.state != "open":
            raise SessionReplacementError("session_replacement_not_retired")
        receipt = retirement_receipt_in_session(
            session, UUID(row.retirement_operation_id)
        )
        current = active_session(session)
        if receipt.confirmation != "confirmed" or receipt.scope != _scope(review):
            raise SessionReplacementError("session_replacement_retirement_unconfirmed")
        if current is None or (
            current.generation,
            current.target_id,
            current.runtime_revision_id,
            current.container_id,
            current.phase,
            current.workspace_id,
            current.source_id,
        ) != (
            review.snapshot.request.expected_generation,
            str(review.snapshot.target_id),
            str(review.snapshot.request.expected_runtime_revision_id),
            review.snapshot.request.expected_container_id,
            "bound",
            str(review.snapshot.workspace_id),
            str(review.snapshot.source_id),
        ):
            raise SessionReplacementError("session_replacement_session_changed")
        self._sessions._idle(session)
        if self._sessions._inventory().containers:
            raise SessionReplacementError("session_replacement_existing_resource")
        for target_id in {
            review.snapshot.target_id,
            review.snapshot.request.next_target_id,
        }:
            status = self._reconciliation.status(target_id)
            if status.required or status.blocking:
                raise SessionReplacementError(
                    "session_replacement_reconciliation_required"
                )
        self._reviews._validate_evidence(
            session, review.snapshot.request, review.snapshot.target_id
        )
        return row, review, current

    def _handoff(self, review_id: UUID, operation: SessionReplacementRequest) -> int:
        with self._sessions._sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = self._prior(session, review_id, operation)
            review = self._review(session, review_id, operation)
            if row is not None and review.successor_generation is not None:
                self._status(session, row)
                return review.successor_generation
            _row, review, current = self._retired(session, review_id, operation)
            selection = review.snapshot.request
            next_instance = ManagedInstanceRepository(session).get(
                selection.next_target_id
            )
            artifact = RuntimeContainerArtifactRepository(session).get(
                selection.next_runtime_revision_id
            )
            workspace = AgentWorkspaceRepository(session).get(
                review.snapshot.workspace_id
            )
            source = DevelopmentSourceRepository(session).get(review.snapshot.source_id)
            if (
                next_instance is None
                or next_instance.instance_type is not InstanceType.DISPOSABLE
                or next_instance.runtime_revision_id
                != selection.next_runtime_revision_id
                or artifact is None
                or artifact.image_id != selection.next_image_id
                or workspace is None
                or workspace.state is not AgentWorkspaceState.READY
                or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
                or source is None
                or workspace.development_source_id != source.id
                or workspace.source_addon_id != source.addon_id
                or source.addon_id != review.snapshot.addon_id
            ):
                raise SessionReplacementError("session_replacement_selection_changed")
            self._sessions._runtime(
                session, selection.next_target_id, selection.next_runtime_revision_id
            )
            latest = (
                session.scalar(select(func.max(DevelopmentSessionRow.generation))) or 0
            )
            if latest != current.generation or latest >= MAX_GENERATIONS:
                raise SessionReplacementError("session_replacement_generation_capacity")
            now = timestamp(datetime.now(UTC))
            successor = DevelopmentSessionRow(
                generation=latest + 1,
                request_id=str(uuid5(review_id, "successor-session")),
                target_id=str(selection.next_target_id),
                runtime_revision_id=str(selection.next_runtime_revision_id),
                workspace_id=current.workspace_id,
                source_id=current.source_id,
                container_id=None,
                phase="reserved",
                created_at=now,
                updated_at=now,
            )
            session.add(successor)
            session.flush()
            policy = session.get(DevelopmentSessionPolicyRow, 1)
            stored_review = session.get(
                DevelopmentSessionReplacementReviewRow, str(review_id)
            )
            assert policy is not None and stored_review is not None
            current.phase, current.updated_at = "closed", now
            policy.active_generation = successor.generation
            stored_review.successor_generation = successor.generation
            session.commit()
            return successor.generation

    def _prepare(self, review_id: UUID, operation: SessionReplacementRequest) -> None:
        with self._sessions._sessions() as session:
            row = self._prior(session, review_id, operation)
            review = self._review(session, review_id, operation)
            if (
                row is None
                or review.successor_generation is None
                or row.released_at is not None
            ):
                raise SessionReplacementError(
                    "session_replacement_successor_unavailable"
                )
            if row.prepare_operation_id is not None:
                return  # No second preparation dispatch after a lost reply.
            successor = session.get(DevelopmentSessionRow, review.successor_generation)
            assert successor is not None
            target_id, revision_id, generation = (
                UUID(successor.target_id),
                UUID(successor.runtime_revision_id),
                successor.generation,
            )

        def protect(session: Session) -> None:
            current = active_session(session)
            row = self._prior(session, review_id, operation)
            instance = ManagedInstanceRepository(session).get(target_id)
            artifact = RuntimeContainerArtifactRepository(session).get(revision_id)
            if (
                current is None
                or row is None
                or row.prepare_operation_id is not None
                or (
                    current.generation,
                    current.target_id,
                    current.runtime_revision_id,
                    current.phase,
                )
                != (generation, str(target_id), str(revision_id), "reserved")
                or (
                    instance is None
                    or instance.instance_type is not InstanceType.DISPOSABLE
                    or instance.runtime_revision_id != revision_id
                    or artifact is None
                    or artifact.image_id != review.snapshot.request.next_image_id
                )
            ):
                raise SessionReplacementError("session_replacement_preparation_changed")
            self._sessions._runtime(session, target_id, revision_id)

        def claimed(session: Session, handle: RunningLifecycleOperation) -> None:
            row = self._prior(session, review_id, operation)
            assert row is not None
            row.prepare_operation_id = str(handle.operation_id)

        try:
            self._lifecycle.provision(
                target_id,
                operation_id=uuid5(review_id, "preparation"),
                precondition=protect,
                on_claim=claimed,
            )
        except Exception:
            # Existing session/provision/lifecycle records preserve the outcome.
            # A known pre-create refusal leaves an explicit reserved session;
            # unknown create remains fenced. Ordinary Prepare is separately reviewed.
            return

    def continue_retired(
        self, review_id: UUID, operation: SessionReplacementRequest
    ) -> SessionReplacementStatus:
        self._handoff(review_id, operation)
        self._prepare(review_id, operation)
        result = self.get(review_id)
        assert result is not None
        return result

    def release_retired(
        self, review_id: UUID, operation: SessionReplacementRequest
    ) -> SessionReplacementStatus:
        """Release only a confirmed retired empty slot; retain uncertain creates."""
        with self._sessions._sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = self._prior(session, review_id, operation)
            if row is not None and row.released_at is not None:
                return self._status(session, row)
            row, _review, current = self._retired(session, review_id, operation)
            now = timestamp(datetime.now(UTC))
            policy = session.get(DevelopmentSessionPolicyRow, 1)
            stored_review = session.get(
                DevelopmentSessionReplacementReviewRow, str(review_id)
            )
            assert policy is not None and stored_review is not None
            current.phase, current.updated_at = "closed", now
            policy.active_generation = None
            stored_review.state, stored_review.cancelled_at = "cancelled", now
            row.released_at = now
            result = self._status(session, row)
            session.commit()
            return result
