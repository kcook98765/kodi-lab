"""SQLite-backed Create ledger. Commands never execute or inspect child services."""

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session, object_session, sessionmaker

from kodi_lab.domain import (
    ManagedInstanceLifecycleKind,
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceMutationKind,
)
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
from kodi_lab.storage.models import (
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedStartAuthorizationRow,
    RuntimeRecipeInstallationRow,
)

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
        start_after_setup=row.start_after_setup,
        preview_approved=row.preview_approved,
        bridge_required=row.bridge_required,
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

    def for_request(self, request_id: UUID) -> CreateOperation | None:
        with self._sessions() as session:
            row = session.scalar(select(Row).where(Row.request_id == str(request_id)))
            return _snapshot(row) if row else None

    def owns_provision_claim(self, operation_id: UUID, token_digest: str) -> bool:
        """Read-only: a rolled-back claimant cannot fence a later claim's owner."""
        with self._sessions() as session:
            row = session.get(Row, str(operation_id))
            lease = (
                session.get(ManagedInstanceMutationLeaseRow, row.target_id)
                if row
                else None
            )
            return bool(
                row
                and lease
                and row.stage == CreateStage.PROVISIONING
                and lease.operation_id == row.provision_operation_id
                and lease.lease_token_digest == token_digest
            )

    def owns_start_claim(self, operation_id: UUID, token_digest: str) -> bool:
        with self._sessions() as session:
            row = session.get(Row, str(operation_id))
            lease = (
                session.get(ManagedInstanceMutationLeaseRow, row.target_id)
                if row
                else None
            )
            return bool(
                row
                and lease
                and row.stage == CreateStage.STARTUP
                and lease.operation_id == row.start_operation_id
                and lease.lease_token_digest == token_digest
            )

    def has_start_evidence(self, operation_id: UUID) -> bool:
        with self._sessions() as session:
            row = session.get(Row, str(operation_id))
            child_id = row.start_operation_id if row else None
            return bool(
                child_id
                and (
                    session.get(ManagedInstanceLifecycleOperationRow, child_id)
                    is not None
                    or session.get(ManagedStartAuthorizationRow, child_id) is not None
                )
            )

    def inspect_lifecycle_dispatch(
        self, operation_id: UUID, *, version: int
    ) -> CreateOperation:
        """Wait only for the exact active child; otherwise fence, never dispatch.

        Successful provision/Start confirmation already commits with the child
        terminal receipt. A terminal child without that parent confirmation is
        inconsistent evidence, not a new result-recovery or retry authority.
        """
        with self._writer() as session:
            row = session.get(Row, str(operation_id))
            if row is None or row.version != version:
                raise CreateTransitionError("stale_version")
            if row.checkpoint != CreateCheckpoint.DISPATCHED or row.stage not in {
                CreateStage.PROVISIONING,
                CreateStage.STARTUP,
            }:
                raise CreateTransitionError("not_dispatched_lifecycle")
            starting = row.stage == CreateStage.STARTUP
            child_id = (
                row.start_operation_id if starting else row.provision_operation_id
            )
            child = (
                session.get(ManagedInstanceLifecycleOperationRow, child_id)
                if child_id
                else None
            )
            lease = session.get(ManagedInstanceMutationLeaseRow, row.target_id)
            instance = session.get(ManagedInstanceRow, row.target_id)
            active = (
                child is not None
                and child.state is ManagedInstanceLifecycleOperationState.RUNNING
                and child.kind
                is (
                    ManagedInstanceLifecycleKind.START
                    if starting
                    else ManagedInstanceLifecycleKind.PROVISION
                )
                and child.original_managed_instance_id
                == child.managed_instance_id
                == row.target_id
                and not child.reconciliation_required
                and lease is not None
                and lease.operation_kind is ManagedInstanceMutationKind.LIFECYCLE
                and lease.operation_id == child_id
                and lease.lease_token_digest is not None
                and lease.lease_expires_at is not None
                and lease.lease_expires_at > _now()
                and instance is not None
                and instance.runtime_revision_id == row.runtime_revision_id
            )
            if active and starting:
                auth = session.get(ManagedStartAuthorizationRow, child_id)
                active = (
                    auth is not None
                    and lease is not None
                    and instance is not None
                    and (
                        auth.target_id,
                        auth.runtime_revision_id,
                        auth.container_id,
                        auth.image_id,
                    )
                    == (
                        row.target_id,
                        row.runtime_revision_id,
                        row.container_id,
                        row.image_id,
                    )
                    and auth.lease_digest == lease.lease_token_digest
                    and auth.runtime_state_revision == instance.runtime_state_revision
                    and row.runtime_state_revision
                    in {auth.runtime_state_revision - 1, auth.runtime_state_revision}
                )
            elif active and row.runtime_state_revision is not None:
                active = (
                    instance is not None
                    and instance.runtime_state_revision == row.runtime_state_revision
                )
            if active:
                return _snapshot(row)
            self._attention(
                row,
                CreateOutcome.UNCERTAIN,
                "start_outcome_unconfirmed"
                if starting
                else "provision_outcome_unconfirmed",
            )
            row.version += 1
            row.updated_at = _now()
            session.flush()
            return _snapshot(row)

    def prepare_startup(
        self,
        operation_id: UUID,
        *,
        version: int,
        verify: Callable[[Session, CreateOperation], None],
    ) -> CreateOperation:
        """Explicitly advance a verified confirmed provision, without execution."""
        with self._change(operation_id, version, CreateCheckpoint.CONFIRMED) as row:
            if row.stage != CreateStage.PROVISIONING:
                raise CreateTransitionError("provision_not_confirmed")
            session = object_session(row)
            assert session is not None
            verify(session, _snapshot(row))
            row.stage, row.checkpoint = CreateStage.STARTUP, CreateCheckpoint.ACCEPTED
            row.outcome, row.dispatched_at = None, None
            return _snapshot(row)

    def prepare_readiness(
        self,
        operation_id: UUID,
        *,
        version: int,
        verify: Callable[[Session, CreateOperation], None],
    ) -> CreateOperation:
        with self._change(operation_id, version, CreateCheckpoint.CONFIRMED) as row:
            if row.stage != CreateStage.STARTUP:
                raise CreateTransitionError("startup_not_confirmed")
            session = object_session(row)
            assert session is not None
            verify(session, _snapshot(row))
            row.stage, row.checkpoint = CreateStage.READINESS, CreateCheckpoint.ACCEPTED
            row.outcome, row.dispatched_at = None, None
            return _snapshot(row)

    def claim_readiness_probe(
        self,
        operation_id: UUID,
        *,
        version: int,
        verify: Callable[[Session, CreateOperation], None],
    ) -> CreateOperation:
        """Only this read-only stage permits an explicit repeated dispatch.

        A new version fences an interrupted/older observer, without changing the
        fixed correlation identity or replaying any installation/lifecycle work.
        """
        with self._change(
            operation_id, version, CreateCheckpoint.PLANNED, CreateCheckpoint.DISPATCHED
        ) as row:
            if row.stage != CreateStage.READINESS:
                raise CreateTransitionError("not_native_readiness")
            session = object_session(row)
            assert session is not None
            verify(session, _snapshot(row))
            row.checkpoint, row.dispatched_at = CreateCheckpoint.DISPATCHED, _now()
            return _snapshot(row)

    def complete_readiness(
        self,
        operation_id: UUID,
        *,
        version: int,
        verify: Callable[[Session, CreateOperation], None],
    ) -> CreateOperation:
        """Commit a fresh verified native result and its final fence together."""
        with self._change(operation_id, version, CreateCheckpoint.DISPATCHED) as row:
            if row.stage != CreateStage.READINESS:
                raise CreateTransitionError("not_native_readiness")
            session = object_session(row)
            assert session is not None
            verify(session, _snapshot(row))
            row.checkpoint, row.outcome = (
                CreateCheckpoint.COMPLETE,
                CreateOutcome.SUCCEEDED,
            )
            row.completed_at, row.reason = _now(), None
            return _snapshot(row)

    def accept(
        self,
        *,
        request_id: UUID,
        generation: str,
        recipe_digest: str,
        resolve_intent: Callable[[], CreateIntent],
        start_after_setup: bool = True,
        preview_approved: bool = False,
        bridge_required: bool = False,
    ) -> CreateAcceptance:
        """Internal admission; the application validates explicit approval first."""
        with self._writer() as session:
            row = session.scalar(select(Row).where(Row.request_id == str(request_id)))
            if row:
                if (
                    row.generation,
                    row.recipe_digest,
                    row.scope,
                    row.start_after_setup,
                    row.preview_approved,
                    row.bridge_required,
                ) != (
                    generation,
                    recipe_digest,
                    CREATE_SCOPE,
                    start_after_setup,
                    preview_approved,
                    bridge_required,
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
                start_after_setup=start_after_setup,
                preview_approved=preview_approved,
                bridge_required=bridge_required,
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
        return self._record_result(operation_id, version=version, result=result)

    def resolve_installation_result(
        self,
        operation_id: UUID,
        *,
        version: int,
        result: CreateChildResult,
        verify_result: Callable[[], bool],
    ) -> CreateOperation:
        """Explicitly resolve uncertainty from the original installation receipt.

        No reset, new attempt, UUID replacement or broader stage recovery.
        """
        return self._record_result(
            operation_id,
            version=version,
            result=result,
            verify_resolution=verify_result,
        )

    def _record_result(
        self,
        operation_id: UUID,
        *,
        version: int,
        result: CreateChildResult,
        verify_resolution: Callable[[], bool] | None = None,
    ) -> CreateOperation:
        if not isinstance(result.stage, CreateStage) or not isinstance(
            result.outcome, CreateOutcome
        ):
            raise CreateTransitionError("invalid_child_vocabulary")
        if result.runtime_revision_id is not None and not isinstance(
            result.runtime_revision_id, UUID
        ):
            raise CreateTransitionError("invalid_runtime_revision")
        allowed = (
            (CreateCheckpoint.NEEDS_ATTENTION,)
            if verify_resolution is not None
            else (CreateCheckpoint.DISPATCHED,)
        )
        with self._change(operation_id, version, *allowed) as row:
            if verify_resolution is not None and (
                row.stage != CreateStage.INSTALLATION
                or row.outcome != CreateOutcome.UNCERTAIN
                or row.dispatched_at is None
                or result.outcome not in {CreateOutcome.SUCCEEDED, CreateOutcome.FAILED}
            ):
                raise CreateTransitionError("installation_resolution_not_supported")
            if verify_resolution is not None:
                session = object_session(row)
                assert session is not None
                source = session.get(RuntimeRecipeInstallationRow, str(result.child_id))
                if (
                    source is None
                    or (
                        source.generation,
                        source.recipe_id,
                        source.recipe_revision,
                        source.recipe_digest,
                        source.state.value,
                        source.runtime_revision_id,
                        source.image_id if result.image_id else None,
                    )
                    != (
                        row.generation,
                        row.recipe_id,
                        row.recipe_revision,
                        row.recipe_digest,
                        "ready"
                        if result.outcome is CreateOutcome.SUCCEEDED
                        else "failed",
                        str(result.runtime_revision_id)
                        if result.runtime_revision_id
                        else None,
                        result.image_id,
                    )
                    or verify_resolution() is not True
                ):
                    raise CreateTransitionError("original_installation_not_verified")
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
                row.reason = row.completed_at = None
            else:
                self._attention(row, result.outcome, result.reason)
            return _snapshot(row)

    def resume_refused_installation(
        self,
        operation_id: UUID,
        *,
        version: int,
    ) -> CreateOperation:
        """Explicit continuation of a refusal that could not have dispatched."""
        with self._change(
            operation_id, version, CreateCheckpoint.NEEDS_ATTENTION
        ) as row:
            if (
                row.stage != CreateStage.INSTALLATION
                or row.outcome != CreateOutcome.REFUSED
                or row.dispatched_at is not None
            ):
                raise CreateTransitionError("installation_resume_not_supported")
            session = object_session(row)
            assert session is not None
            if (
                row.installation_operation_id
                and session.get(
                    RuntimeRecipeInstallationRow, row.installation_operation_id
                )
                is not None
            ):
                raise CreateTransitionError("unexpected_installation_receipt")
            row.checkpoint = (
                CreateCheckpoint.PLANNED
                if row.installation_operation_id
                else CreateCheckpoint.ACCEPTED
            )
            row.outcome = row.reason = row.completed_at = None
            return _snapshot(row)

    def reuse_installation(
        self,
        operation_id: UUID,
        *,
        version: int,
        source_operation_id: UUID,
        runtime_revision_id: UUID,
        image_id: str,
        verify_runtime: Callable[[], bool],
    ) -> CreateOperation:
        """Atomically confirm verified reuse and advance, without fake dispatch.

        Preserve an already planned (unused) UUID. The cached receipt remains
        authoritatively correlated by exact recipe/revision/image, via the
        existing ready_for_revision lookup; it is not adopted as that child.
        """
        with self._change(
            operation_id, version, CreateCheckpoint.ACCEPTED, CreateCheckpoint.PLANNED
        ) as row:
            if row.stage != CreateStage.INSTALLATION or row.dispatched_at is not None:
                raise CreateTransitionError("installation_reuse_not_supported")
            session = object_session(row)
            assert session is not None
            source = session.get(RuntimeRecipeInstallationRow, str(source_operation_id))
            if source is None or (
                source.state.value,
                source.generation,
                source.recipe_id,
                source.recipe_revision,
                source.recipe_digest,
                source.runtime_revision_id,
                source.image_id,
            ) != (
                "ready",
                row.generation,
                row.recipe_id,
                row.recipe_revision,
                row.recipe_digest,
                str(runtime_revision_id),
                image_id,
            ):
                raise CreateTransitionError("cached_installation_mismatch")
            if (
                row.installation_operation_id is not None
                and session.get(
                    RuntimeRecipeInstallationRow, row.installation_operation_id
                )
                is not None
            ):
                raise CreateTransitionError("unexpected_installation_receipt")
            instance = session.get(ManagedInstanceRow, row.target_id)
            if instance is not None and instance.runtime_revision_id != str(
                runtime_revision_id
            ):
                raise CreateTransitionError("canonical_pin_conflict")
            # Recheck eligibility/image while holding the database writer lock;
            # concurrent revocation cannot slip between this proof and commit.
            if verify_runtime() is not True:
                raise CreateTransitionError("cached_runtime_not_verified")
            if row.installation_operation_id is None:
                row.installation_operation_id = str(source_operation_id)
            row.runtime_revision_id, row.image_id = str(runtime_revision_id), image_id
            row.stage = CreateStage.REGISTRATION
            # Confirmation + advance is atomic for reuse. No dispatch occurred.
            row.checkpoint = CreateCheckpoint.ACCEPTED
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

    def confirm_registration(
        self,
        operation_id: UUID,
        *,
        version: int,
        verify: Callable[[Session, CreateOperation], bool],
    ) -> CreateOperation:
        """Confirm exact local DB evidence; undispatched reuse advances atomically."""
        with self._change(
            operation_id,
            version,
            CreateCheckpoint.ACCEPTED,
            CreateCheckpoint.PLANNED,
            CreateCheckpoint.DISPATCHED,
            CreateCheckpoint.NEEDS_ATTENTION,
        ) as row:
            if row.stage != CreateStage.REGISTRATION or (
                row.checkpoint == CreateCheckpoint.NEEDS_ATTENTION
                and row.outcome != CreateOutcome.UNCERTAIN
            ):
                raise CreateTransitionError("registration_confirmation_not_supported")
            session = object_session(row)
            assert session is not None
            if verify(session, _snapshot(row)) is not True:
                raise CreateTransitionError("canonical_registration_not_verified")
            if row.dispatched_at is None:
                row.stage, row.checkpoint = (
                    CreateStage.PROVISIONING,
                    CreateCheckpoint.ACCEPTED,
                )
            else:
                row.checkpoint, row.outcome = (
                    CreateCheckpoint.CONFIRMED,
                    CreateOutcome.SUCCEEDED,
                )
                row.reason = row.completed_at = None
            return _snapshot(row)
