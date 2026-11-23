"""Administrator preview and explicit one-shot approval of source-copy additions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_deployments import (
    DevelopmentSourceDeploymentError,
    DevelopmentSourceDeploymentOperationCoordinator,
    DevelopmentSourceDeploymentStatus,
)
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.development.addition_reviews import SourceAdditionProof
from kodi_lab.domain import DevelopmentSourceDeploymentOperation
from kodi_lab.storage.models import (
    DevelopmentSourceRow,
    SourceAdditionReviewRow,
    TargetRow,
)
from kodi_lab.storage.source_addition_reviews import addition_review_scope

APPROVAL_TTL = timedelta(minutes=15)
MAX_REVIEWS = 1024


class _Broker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...
    def review_source_additions(
        self,
        *,
        managed_instance_id: str,
        development_source_id: str,
        expected_container_id: str,
        paths: tuple[str, ...],
    ) -> SourceAdditionProof: ...


class SourceAdditionReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    target_id: UUID
    source_id: UUID
    target_name: str | None
    source_name: str | None
    addon_id: str | None
    container_id: str
    proof: SourceAdditionProof
    created_at: str
    expires_at: str
    approved_at: str | None
    dispatched_at: str | None
    operation: DevelopmentSourceDeploymentStatus | None
    operation_id: UUID | None


def _conflict() -> DevelopmentSourceDeploymentError:
    return DevelopmentSourceDeploymentError(
        "Review is stale, expired, already used, or does not approve every displayed "
        "file. Create a new review after inspecting the saved outcome.",
        code="development_source_additions_review_conflict",
    )


class SourceAdditionReviewService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        broker: _Broker,
        coordinator: DevelopmentSourceDeploymentOperationCoordinator,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._broker = broker
        self._coordinator = coordinator
        self._clock = clock

    def get(self, review_id: UUID) -> SourceAdditionReview | None:
        with self._sessions() as session:
            row = session.get(SourceAdditionReviewRow, str(review_id))
            if row is None:
                return None
            target = session.get(TargetRow, row.target_id)
            source = session.get(DevelopmentSourceRow, row.source_id)
            operation = (
                None
                if row.operation_id is None
                else self._coordinator.get(UUID(row.operation_id))
            )
            return SourceAdditionReview(
                id=UUID(row.id),
                target_id=UUID(row.target_id),
                source_id=UUID(row.source_id),
                target_name=None if target is None else target.name,
                source_name=None if source is None else source.logical_name,
                addon_id=None if source is None else source.addon_id,
                container_id=row.container_id,
                proof=SourceAdditionProof.model_validate_json(row.proof_json),
                created_at=row.created_at,
                expires_at=row.expires_at,
                approved_at=row.approved_at,
                dispatched_at=row.dispatched_at,
                operation_id=None
                if row.operation_id is None
                else UUID(row.operation_id),
                operation=None
                if operation is None
                else self._coordinator.to_status(operation),
            )

    def preview(
        self, target_id: UUID, source_id: UUID, paths: tuple[str, ...]
    ) -> SourceAdditionReview:
        container_id = self._coordinator._require_stopped(target_id)
        proof = self._broker.review_source_additions(
            managed_instance_id=str(target_id),
            development_source_id=str(source_id),
            expected_container_id=container_id,
            paths=paths,
        )
        now = self._clock()
        review_id = uuid4()
        with self._sessions() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            count = session.scalar(
                select(func.count()).select_from(SourceAdditionReviewRow)
            )
            if count is None or count >= MAX_REVIEWS:
                raise DevelopmentSourceDeploymentError(
                    "Addition review history is full.",
                    code="source_addition_review_history_full",
                )
            if proof.scope_digest != addition_review_scope(
                session, str(target_id), str(source_id), container_id
            ):
                raise _conflict()
            session.add(
                SourceAdditionReviewRow(
                    id=str(review_id),
                    target_id=str(target_id),
                    source_id=str(source_id),
                    container_id=container_id,
                    proof_json=proof.model_dump_json(),
                    created_at=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    expires_at=(now + APPROVAL_TTL).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                )
            )
            session.commit()
        result = self.get(review_id)
        assert result is not None
        return result

    def approve(
        self,
        review_id: UUID,
        approved_paths: tuple[str, ...],
        submit: Callable[[UUID], None],
    ) -> SourceAdditionReview:
        review = self.get(review_id)
        if review is None:
            raise _conflict()
        if len(set(approved_paths)) != len(approved_paths) or set(approved_paths) != {
            item.path for item in review.proof.additions
        }:
            raise _conflict()
        if review.operation_id is not None:
            return review

        def on_claim(
            session: Session, operation: DevelopmentSourceDeploymentOperation
        ) -> None:
            row = session.get(SourceAdditionReviewRow, str(review_id))
            stamp = self._clock().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            if row is None or row.operation_id is not None or row.expires_at <= stamp:
                raise _conflict()
            if (
                operation.expected_container_id != row.container_id
                or str(operation.managed_instance_id) != row.target_id
                or str(operation.development_source_id) != row.source_id
                or addition_review_scope(
                    session, row.target_id, row.source_id, row.container_id
                )
                != review.proof.scope_digest
                or row.proof_json != review.proof.model_dump_json()
            ):
                raise _conflict()
            row.operation_id = str(operation.id)
            row.approved_at = stamp

        operation = self._coordinator.queue(
            review.target_id, review.source_id, on_claim=on_claim
        )
        # The durable ordinary operation owns uncertainty if submission loses its reply.
        submit(operation.operation_id)
        result = self.get(review_id)
        assert result is not None
        return result
