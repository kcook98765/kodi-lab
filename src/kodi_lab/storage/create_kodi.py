"""SQLite-backed Create ledger. Commands never execute or inspect child services."""

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain.create_kodi import (
    CREATE_SCOPE,
    STAGES,
    CreateAcceptance,
    CreateCheckpoint,
    CreateChildResult,
    CreateConflict,
    CreateDispatchClaim,
    CreateIntent,
    CreateOperation,
    CreateOutcome,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.storage.models import CreateKodiOperationRow as Row

_CHILD_COLUMNS = {
    CreateStage.INSTALLATION: "installation_operation_id",
    CreateStage.PROVISIONING: "provision_operation_id",
    CreateStage.STARTUP: "start_operation_id",
}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _uuid(value: str | None) -> UUID | None:
    return UUID(value) if value is not None else None


def _snapshot(row: Row) -> CreateOperation:
    return CreateOperation(
        id=UUID(row.id),
        request_id=UUID(row.request_id),
        target_id=UUID(row.target_id),
        generation=row.generation,
        recipe_id=row.recipe_id,
        recipe_revision=row.recipe_revision,
        recipe_digest=row.recipe_digest,
        scope=row.scope,
        stage=CreateStage(row.stage),
        checkpoint=CreateCheckpoint(row.checkpoint),
        version=row.version,
        approved_at=row.approved_at,
        updated_at=row.updated_at,
        dispatched_at=row.dispatched_at,
        completed_at=row.completed_at,
        outcome=CreateOutcome(row.outcome) if row.outcome else None,
        reason=row.reason,
        installation_operation_id=_uuid(row.installation_operation_id),
        provision_operation_id=_uuid(row.provision_operation_id),
        start_operation_id=_uuid(row.start_operation_id),
        runtime_revision_id=_uuid(row.runtime_revision_id),
        image_id=row.image_id,
        container_id=row.container_id,
        runtime_state_revision=row.runtime_state_revision,
    )


class CreateKodiRepository:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @contextmanager
    def _writer(self) -> Iterator[Session]:
        # Acquire the database writer lock BEFORE reading, across processes.
        # Uniqueness is also enforced by the table, independently of this code.
        with self._sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def get(self, operation_id: UUID) -> CreateOperation | None:
        with self._sessions() as session:
            row = session.get(Row, str(operation_id))
            return _snapshot(row) if row else None

    def for_target(self, target_id: UUID) -> CreateOperation | None:
        with self._sessions() as session:
            row = session.scalar(select(Row).where(Row.target_id == str(target_id)))
            return _snapshot(row) if row else None

    def accept(
        self,
        *,
        request_id: UUID,
        generation: str,
        recipe_digest: str,
        resolve_intent: Callable[[], CreateIntent],
    ) -> CreateAcceptance:
        """Internal admission; the application validates explicit approval first."""
        with self._writer() as session:
            row = session.scalar(select(Row).where(Row.request_id == str(request_id)))
            if row:
                if (row.generation, row.recipe_digest, row.scope) != (
                    generation,
                    recipe_digest,
                    CREATE_SCOPE,
                ):
                    raise CreateConflict("request_inputs_conflict", UUID(row.id))
                return CreateAcceptance(_snapshot(row), replayed=True)
            # Resolve only NEW requests: upgrades must not rewrite accepted intent.
            intent = resolve_intent()
            if (intent.generation, intent.recipe_digest) != (generation, recipe_digest):
                raise ValueError("approved recipe does not match reviewed selection")
            existing = session.scalar(
                select(Row).where(Row.target_id == str(intent.target_id))
            )
            if existing:
                raise CreateConflict("canonical_create_exists", UUID(existing.id))
            now = _now()
            row = Row(
                id=str(uuid4()),
                request_id=str(request_id),
                target_id=str(intent.target_id),
                generation=generation,
                recipe_id=intent.recipe_id,
                recipe_revision=intent.recipe_revision,
                recipe_digest=recipe_digest,
                scope=CREATE_SCOPE,
                stage=CreateStage.INSTALLATION,
                checkpoint=CreateCheckpoint.ACCEPTED,
                version=0,
                approved_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return CreateAcceptance(_snapshot(row), replayed=False)

    @contextmanager
    def _change(
        self,
        operation_id: UUID,
        version: int,
        *allowed: CreateCheckpoint,
    ) -> Iterator[Row]:
        with self._writer() as session:
            row = session.get(Row, str(operation_id))
            if row is None:
                raise CreateTransitionError("operation_not_found")
            if row.version != version:
                raise CreateTransitionError("stale_version")
            if row.checkpoint not in allowed:
                raise CreateTransitionError("invalid_checkpoint")
            row.version += 1
            row.updated_at = _now()
            yield row
            session.flush()

    def plan(self, operation_id: UUID, *, version: int) -> CreateOperation:
        with self._change(operation_id, version, CreateCheckpoint.ACCEPTED) as row:
            column = _CHILD_COLUMNS.get(CreateStage(row.stage))
            if column:
                if getattr(row, column) is not None:
                    raise CreateTransitionError("child_already_planned")
                setattr(row, column, str(uuid4()))
            row.checkpoint = CreateCheckpoint.PLANNED
            return _snapshot(row)

    def claim_dispatch(
        self, operation_id: UUID, *, version: int
    ) -> CreateDispatchClaim:
        """Only the successful caller receives a fresh dispatch claim.

        A later read of DISPATCHED is evidence of possible execution, NEVER a
        dispatch claim. A lost command response therefore requires inspection.
        """
        with self._change(operation_id, version, CreateCheckpoint.PLANNED) as row:
            row.checkpoint = CreateCheckpoint.DISPATCHED
            row.dispatched_at = _now()
            return CreateDispatchClaim(_snapshot(row))

    def record_result(
        self,
        operation_id: UUID,
        *,
        version: int,
        result: CreateChildResult,
    ) -> CreateOperation:
        if not isinstance(result.stage, CreateStage) or not isinstance(
            result.outcome, CreateOutcome
        ):
            raise CreateTransitionError("invalid_child_vocabulary")
        if result.runtime_revision_id is not None and not isinstance(
            result.runtime_revision_id, UUID
        ):
            raise CreateTransitionError("invalid_runtime_revision")
        with self._change(operation_id, version, CreateCheckpoint.DISPATCHED) as row:
            current = _snapshot(row)
            if (
                result.stage,
                result.child_id,
                result.target_id,
                result.recipe_digest,
            ) != (
                current.stage,
                current.child_id,
                current.target_id,
                current.recipe_digest,
            ):
                raise CreateTransitionError("child_identity_mismatch")
            self._bind(row, result)
            if result.outcome is CreateOutcome.SUCCEEDED:
                if result.reason is not None:
                    raise CreateTransitionError("success_has_reason")
                row.checkpoint = CreateCheckpoint.CONFIRMED
                row.outcome = CreateOutcome.SUCCEEDED
            else:
                self._attention(row, result.outcome, result.reason)
            return _snapshot(row)

    @staticmethod
    def _bind(row: Row, result: CreateChildResult) -> None:
        """Freeze exact identities; adapters must supply authoritative evidence."""
        success = result.outcome is CreateOutcome.SUCCEEDED
        installation = result.stage is CreateStage.INSTALLATION
        provisioning = result.stage is CreateStage.PROVISIONING
        startup = result.stage is CreateStage.STARTUP
        revision = (
            str(result.runtime_revision_id) if result.runtime_revision_id else None
        )
        if result.image_id is not None and not re.fullmatch(
            r"sha256:[0-9a-f]{64}", result.image_id
        ):
            raise CreateTransitionError("invalid_image")
        if result.container_id is not None and not re.fullmatch(
            r"[0-9a-f]{64}", result.container_id
        ):
            raise CreateTransitionError("invalid_container")
        if result.runtime_state_revision is not None and (
            type(result.runtime_state_revision) is not int
            or result.runtime_state_revision < 0
        ):
            raise CreateTransitionError("invalid_runtime_state_revision")
        for supplied, known in (
            (revision, row.runtime_revision_id),
            (result.image_id, row.image_id),
            (result.container_id, row.container_id),
        ):
            if supplied is not None and known is not None and supplied != known:
                raise CreateTransitionError("runtime_identity_changed")
        if success:
            if revision is None or result.image_id is None:
                raise CreateTransitionError("runtime_evidence_missing")
            if not installation and (revision, result.image_id) != (
                row.runtime_revision_id,
                row.image_id,
            ):
                raise CreateTransitionError("runtime_identity_changed")
            if provisioning or startup or result.stage is CreateStage.READINESS:
                if result.container_id is None or result.runtime_state_revision is None:
                    raise CreateTransitionError("container_evidence_missing")
                if startup:
                    if (
                        row.runtime_state_revision is None
                        or result.runtime_state_revision <= row.runtime_state_revision
                    ):
                        raise CreateTransitionError("startup_revision_not_advanced")
                elif not provisioning and (
                    result.runtime_state_revision != row.runtime_state_revision
                ):
                    raise CreateTransitionError("runtime_state_changed")
            elif (
                result.container_id is not None
                or result.runtime_state_revision is not None
            ):
                raise CreateTransitionError("unexpected_container_evidence")
            row.runtime_revision_id, row.image_id = revision, result.image_id
            if provisioning or startup:
                row.container_id = result.container_id
                row.runtime_state_revision = result.runtime_state_revision
        elif (
            result.runtime_state_revision is not None
            and result.runtime_state_revision != row.runtime_state_revision
        ):
            raise CreateTransitionError("runtime_state_changed")

    @staticmethod
    def _attention(row: Row, outcome: CreateOutcome, reason: str | None) -> None:
        if (
            outcome is CreateOutcome.SUCCEEDED
            or not isinstance(reason, str)
            or not (re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason))
        ):
            raise CreateTransitionError("invalid_attention_reason")
        row.checkpoint = CreateCheckpoint.NEEDS_ATTENTION
        row.outcome, row.reason, row.completed_at = outcome, reason, _now()

    def note_uncertain(
        self,
        operation_id: UUID,
        *,
        version: int,
        reason: str,
    ) -> CreateOperation:
        with self._change(operation_id, version, CreateCheckpoint.DISPATCHED) as row:
            self._attention(row, CreateOutcome.UNCERTAIN, reason)
            return _snapshot(row)

    def refuse(
        self,
        operation_id: UUID,
        *,
        version: int,
        reason: str,
    ) -> CreateOperation:
        with self._change(
            operation_id, version, CreateCheckpoint.ACCEPTED, CreateCheckpoint.PLANNED
        ) as row:
            self._attention(row, CreateOutcome.REFUSED, reason)
            return _snapshot(row)

    def advance(self, operation_id: UUID, *, version: int) -> CreateOperation:
        with self._change(operation_id, version, CreateCheckpoint.CONFIRMED) as row:
            stage = CreateStage(row.stage)
            if stage is CreateStage.READINESS:
                row.checkpoint = CreateCheckpoint.COMPLETE
                row.completed_at = _now()
            else:
                row.stage = STAGES[STAGES.index(stage) + 1]
                row.checkpoint = CreateCheckpoint.ACCEPTED
                row.dispatched_at = None
                row.outcome = None
            return _snapshot(row)
