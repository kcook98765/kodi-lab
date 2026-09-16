"""Explicit repositories for targets and managed instances."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from kodi_lab.domain import (
    FixtureDefinition,
    GlobalHeavyOperationKind,
    InstanceRuntimeState,
    ManagedInstance,
    ManagedInstanceMutationKind,
    ResetOperation,
    ResetOperationPhase,
    ResetOperationState,
    RuntimeContainerArtifact,
    RuntimeDefinition,
    RuntimeExecutionProfile,
    RuntimeRevision,
    RuntimeSupportTier,
    RuntimeValidationEvidence,
    Snapshot,
    SnapshotCreateOperation,
    SnapshotCreateOperationPhase,
    SnapshotCreateOperationState,
    SnapshotState,
    SnapshotValidationResult,
    Target,
)
from kodi_lab.storage.models import (
    FixtureDefinitionRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ResetOperationRow,
    RuntimeContainerArtifactRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    RuntimeValidationEvidenceRow,
    SnapshotCreateOperationRow,
    SnapshotRow,
    TargetRow,
)


class EntityNotFoundError(LookupError):
    """Raised when an update target no longer exists."""


def _snapshot_from_row(row: SnapshotRow) -> Snapshot:
    return Snapshot(
        id=UUID(row.id),
        source_managed_instance_id=UUID(row.source_managed_instance_id),
        runtime_revision_id=UUID(row.runtime_revision_id),
        captured_fixture_definition_id=(
            None
            if row.captured_fixture_definition_id is None
            else UUID(row.captured_fixture_definition_id)
        ),
        captured_at=_timestamp_from_text(row.captured_at),
        prior_runtime_state=row.prior_runtime_state,
        label=row.label,
        manifest_version=row.manifest_version,
        archive_digest=row.archive_digest,
        manifest_digest=row.manifest_digest,
        archive_size=row.archive_size,
        member_count=row.member_count,
        state=row.state,
        validation_at=(
            None
            if row.validation_at is None
            else _timestamp_from_text(row.validation_at)
        ),
        validation_result=row.validation_result,
    )


def _snapshot_operation_from_row(
    row: SnapshotCreateOperationRow,
) -> SnapshotCreateOperation:
    return SnapshotCreateOperation(
        id=UUID(row.id),
        snapshot_id=UUID(row.snapshot_id),
        managed_instance_id=UUID(row.managed_instance_id),
        requested_at=_timestamp_from_text(row.requested_at),
        state=row.state,
        phase=row.phase,
        started_at=(
            None if row.started_at is None else _timestamp_from_text(row.started_at)
        ),
        heartbeat_at=(
            None if row.heartbeat_at is None else _timestamp_from_text(row.heartbeat_at)
        ),
        completed_at=(
            None if row.completed_at is None else _timestamp_from_text(row.completed_at)
        ),
        failure_reason=row.failure_reason,
        reconciliation_required=row.reconciliation_required,
        lease_token_digest=row.lease_token_digest,
        lease_expires_at=(
            None
            if row.lease_expires_at is None
            else _timestamp_from_text(row.lease_expires_at)
        ),
    )


def _target_from_row(row: TargetRow) -> Target:
    return Target(
        id=UUID(row.id),
        name=row.name,
        target_type=row.target_type,
        kodi_version=row.kodi_version,
        availability=row.availability,
        health=row.health,
        connection_state=row.connection_state,
    )


def _instance_from_row(row: ManagedInstanceRow) -> ManagedInstance:
    return ManagedInstance(
        target_id=UUID(row.target_id),
        instance_type=row.instance_type,
        runtime_revision_id=UUID(row.runtime_revision_id),
        runtime_state=row.runtime_state,
        start_on_host_boot=row.start_on_host_boot,
        notes=row.notes,
        fixture_definition_id=(
            None
            if row.fixture_definition_id is None
            else UUID(row.fixture_definition_id)
        ),
    )


def _timestamp_to_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp_from_text(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _definition_from_row(row: RuntimeDefinitionRow) -> RuntimeDefinition:
    return RuntimeDefinition(
        id=UUID(row.id),
        slug=row.slug,
        kodi_version=row.kodi_version,
        support_tier=RuntimeSupportTier(row.support_tier),
    )


def _revision_from_row(row: RuntimeRevisionRow) -> RuntimeRevision:
    return RuntimeRevision(
        id=UUID(row.id),
        runtime_definition_id=UUID(row.runtime_definition_id),
        revision_number=row.revision_number,
        source_kind=row.source_kind,
        source_reference=row.source_reference,
        content_digest=row.content_digest,
        manifest_reference=row.manifest_reference,
        manifest_digest=row.manifest_digest,
        built_at=_timestamp_from_text(row.built_at),
        build_log_reference=row.build_log_reference,
        approval_state=row.approval_state,
        approved_at=(
            None if row.approved_at is None else _timestamp_from_text(row.approved_at)
        ),
        approval_note=row.approval_note,
        requires_trusted_build_confirmation=row.requires_trusted_build_confirmation,
        trusted_build_confirmed_at=(
            None
            if row.trusted_build_confirmed_at is None
            else _timestamp_from_text(row.trusted_build_confirmed_at)
        ),
    )


class TargetRepository:
    """Persistence operations for normalized targets."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, target: Target) -> None:
        self._session.add(
            TargetRow(
                id=str(target.id),
                name=target.name,
                target_type=target.target_type,
                kodi_version=target.kodi_version,
                availability=target.availability,
                health=target.health,
                connection_state=target.connection_state,
            )
        )
        self._session.flush()

    def get(self, target_id: UUID) -> Target | None:
        row = self._session.get(TargetRow, str(target_id))
        return None if row is None else _target_from_row(row)

    def list(self) -> list[Target]:
        rows = self._session.scalars(
            select(TargetRow).order_by(TargetRow.name, TargetRow.id)
        )
        return [_target_from_row(row) for row in rows]

    def update(self, target: Target) -> None:
        row = self._session.get(TargetRow, str(target.id))
        if row is None:
            raise EntityNotFoundError(f"target {target.id} does not exist")
        row.name = target.name
        row.target_type = target.target_type
        row.kodi_version = target.kodi_version
        row.availability = target.availability
        row.health = target.health
        row.connection_state = target.connection_state
        self._session.flush()

    def delete(self, target_id: UUID) -> bool:
        row = self._session.get(TargetRow, str(target_id))
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True


class ManagedInstanceRepository:
    """Persistence operations for managed-instance subtype data."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, instance: ManagedInstance) -> None:
        self._session.add(
            ManagedInstanceRow(
                target_id=str(instance.target_id),
                instance_type=instance.instance_type,
                runtime_revision_id=str(instance.runtime_revision_id),
                runtime_state=instance.runtime_state,
                start_on_host_boot=instance.start_on_host_boot,
                notes=instance.notes,
                fixture_definition_id=(
                    None
                    if instance.fixture_definition_id is None
                    else str(instance.fixture_definition_id)
                ),
            )
        )
        self._session.flush()

    def get(self, target_id: UUID) -> ManagedInstance | None:
        row = self._session.get(ManagedInstanceRow, str(target_id))
        return None if row is None else _instance_from_row(row)

    def list(self) -> list[ManagedInstance]:
        rows = self._session.scalars(
            select(ManagedInstanceRow).order_by(ManagedInstanceRow.target_id)
        )
        return [_instance_from_row(row) for row in rows]

    def update(self, instance: ManagedInstance) -> None:
        row = self._session.get(ManagedInstanceRow, str(instance.target_id))
        if row is None:
            raise EntityNotFoundError(
                f"managed instance {instance.target_id} does not exist"
            )
        row.instance_type = instance.instance_type
        row.runtime_revision_id = str(instance.runtime_revision_id)
        row.runtime_state = instance.runtime_state
        row.start_on_host_boot = instance.start_on_host_boot
        row.notes = instance.notes
        row.fixture_definition_id = (
            None
            if instance.fixture_definition_id is None
            else str(instance.fixture_definition_id)
        )
        self._session.flush()


class FixtureDefinitionRepository:
    """Create/read-only persistence for finalized fixture definitions."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, fixture: FixtureDefinition) -> None:
        self._session.add(
            FixtureDefinitionRow(
                id=str(fixture.id),
                slug=fixture.slug,
                fixture_version=fixture.fixture_version,
                size_category=fixture.size_category,
                content_category=fixture.content_category,
                content_digest=fixture.content_digest,
                compatible_kodi_version=fixture.compatible_kodi_version,
                description=fixture.description,
            )
        )
        self._session.flush()

    def get(self, fixture_id: UUID) -> FixtureDefinition | None:
        row = self._session.get(FixtureDefinitionRow, str(fixture_id))
        return None if row is None else self._from_row(row)

    def get_by_slug(self, slug: str) -> FixtureDefinition | None:
        row = self._session.scalar(
            select(FixtureDefinitionRow).where(FixtureDefinitionRow.slug == slug)
        )
        return None if row is None else self._from_row(row)

    @staticmethod
    def _from_row(row: FixtureDefinitionRow) -> FixtureDefinition:
        return FixtureDefinition(
            id=UUID(row.id),
            slug=row.slug,
            fixture_version=row.fixture_version,
            size_category=row.size_category,
            content_category=row.content_category,
            content_digest=row.content_digest,
            compatible_kodi_version=row.compatible_kodi_version,
            description=row.description,
        )


def _reset_operation_from_row(row: ResetOperationRow) -> ResetOperation:
    return ResetOperation(
        id=UUID(row.id),
        managed_instance_id=UUID(row.managed_instance_id),
        requested_at=_timestamp_from_text(row.requested_at),
        started_at=None
        if row.started_at is None
        else _timestamp_from_text(row.started_at),
        heartbeat_at=(
            None if row.heartbeat_at is None else _timestamp_from_text(row.heartbeat_at)
        ),
        completed_at=(
            None if row.completed_at is None else _timestamp_from_text(row.completed_at)
        ),
        state=row.state,
        phase=row.phase,
        failure_reason=row.failure_reason,
        reconciliation_required=row.reconciliation_required,
        lease_token_digest=row.lease_token_digest,
        lease_expires_at=(
            None
            if row.lease_expires_at is None
            else _timestamp_from_text(row.lease_expires_at)
        ),
    )


class ResetOperationRepository:
    """Short-transaction coordination for reset-specific durable work."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: ResetOperation) -> None:
        self._session.add(
            ResetOperationRow(
                id=str(operation.id),
                managed_instance_id=str(operation.managed_instance_id),
                requested_at=_timestamp_to_text(operation.requested_at),
                state=operation.state,
                phase=operation.phase,
                reconciliation_required=operation.reconciliation_required,
            )
        )
        self._session.flush()

    def get(self, operation_id: UUID) -> ResetOperation | None:
        row = self._session.get(ResetOperationRow, str(operation_id))
        return None if row is None else _reset_operation_from_row(row)

    def active_for_instance(self, instance_id: UUID) -> ResetOperation | None:
        row = self._session.scalar(
            select(ResetOperationRow)
            .where(
                ResetOperationRow.managed_instance_id == str(instance_id),
                ResetOperationRow.state.in_(
                    [ResetOperationState.QUEUED, ResetOperationState.RUNNING]
                ),
            )
            .order_by(
                ResetOperationRow.requested_at.desc(), ResetOperationRow.id.desc()
            )
        )
        return None if row is None else _reset_operation_from_row(row)

    def latest_for_instance(self, instance_id: UUID) -> ResetOperation | None:
        row = self._session.scalar(
            select(ResetOperationRow)
            .where(ResetOperationRow.managed_instance_id == str(instance_id))
            .order_by(
                ResetOperationRow.requested_at.desc(), ResetOperationRow.id.desc()
            )
            .limit(1)
        )
        return None if row is None else _reset_operation_from_row(row)

    def list_queued(self) -> list[ResetOperation]:
        rows = self._session.scalars(
            select(ResetOperationRow)
            .where(ResetOperationRow.state == ResetOperationState.QUEUED)
            .order_by(ResetOperationRow.requested_at, ResetOperationRow.id)
        )
        return [_reset_operation_from_row(row) for row in rows]

    def acquire(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(ResetOperationRow)
            .where(
                ResetOperationRow.id == str(operation_id),
                ResetOperationRow.state == ResetOperationState.QUEUED,
            )
            .values(
                state=ResetOperationState.RUNNING,
                phase=ResetOperationPhase.PREFLIGHT,
                started_at=_timestamp_to_text(now),
                heartbeat_at=_timestamp_to_text(now),
                lease_token_digest=token_digest,
                lease_expires_at=_timestamp_to_text(lease_expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def update_phase(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        phase: ResetOperationPhase,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(ResetOperationRow)
            .where(
                ResetOperationRow.id == str(operation_id),
                ResetOperationRow.state == ResetOperationState.RUNNING,
                ResetOperationRow.lease_token_digest == token_digest,
                ResetOperationRow.lease_expires_at > _timestamp_to_text(now),
            )
            .values(
                phase=phase,
                heartbeat_at=_timestamp_to_text(now),
                lease_expires_at=_timestamp_to_text(lease_expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def finish(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        state: ResetOperationState,
        phase: ResetOperationPhase,
        failure_reason: str | None,
        reconciliation_required: bool,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(ResetOperationRow)
            .where(
                ResetOperationRow.id == str(operation_id),
                ResetOperationRow.state == ResetOperationState.RUNNING,
                ResetOperationRow.lease_token_digest == token_digest,
                ResetOperationRow.lease_expires_at > _timestamp_to_text(now),
            )
            .values(
                state=state,
                phase=phase,
                completed_at=_timestamp_to_text(now),
                heartbeat_at=_timestamp_to_text(now),
                failure_reason=failure_reason,
                reconciliation_required=reconciliation_required,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def mark_stale_reconciliation(self, operation_id: UUID, *, now: datetime) -> bool:
        result = self._session.execute(
            update(ResetOperationRow)
            .where(
                ResetOperationRow.id == str(operation_id),
                ResetOperationRow.state == ResetOperationState.RUNNING,
                ResetOperationRow.lease_expires_at <= _timestamp_to_text(now),
            )
            .values(
                state=ResetOperationState.RECONCILIATION_REQUIRED,
                completed_at=_timestamp_to_text(now),
                failure_reason=(
                    "Reset worker lease expired; reconciliation is required."
                ),
                reconciliation_required=True,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def prune_terminal(self, instance_id: UUID, *, retain: int = 20) -> int:
        old_ids = list(
            self._session.scalars(
                select(ResetOperationRow.id)
                .where(
                    ResetOperationRow.managed_instance_id == str(instance_id),
                    ResetOperationRow.state.not_in(
                        [ResetOperationState.QUEUED, ResetOperationState.RUNNING]
                    ),
                )
                .order_by(
                    ResetOperationRow.requested_at.desc(), ResetOperationRow.id.desc()
                )
                .offset(retain)
            )
        )
        if not old_ids:
            return 0
        result = self._session.execute(
            delete(ResetOperationRow).where(ResetOperationRow.id.in_(old_ids))
        )
        return int(getattr(result, "rowcount", 0))


class RuntimeDefinitionRepository:
    """Persistence operations for logical runtime definitions."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, definition: RuntimeDefinition) -> None:
        self._session.add(
            RuntimeDefinitionRow(
                id=str(definition.id),
                slug=definition.slug,
                kodi_version=definition.kodi_version,
                support_tier=definition.support_tier.value,
            )
        )
        self._session.flush()

    def get(self, definition_id: UUID) -> RuntimeDefinition | None:
        row = self._session.get(RuntimeDefinitionRow, str(definition_id))
        return None if row is None else _definition_from_row(row)

    def get_by_slug(self, slug: str) -> RuntimeDefinition | None:
        row = self._session.scalar(
            select(RuntimeDefinitionRow).where(RuntimeDefinitionRow.slug == slug)
        )
        return None if row is None else _definition_from_row(row)

    def list(self) -> list[RuntimeDefinition]:
        rows = self._session.scalars(
            select(RuntimeDefinitionRow).order_by(
                RuntimeDefinitionRow.slug, RuntimeDefinitionRow.id
            )
        )
        return [_definition_from_row(row) for row in rows]

    def transition_support_tier(
        self, definition_id: UUID, support_tier: RuntimeSupportTier
    ) -> RuntimeDefinition:
        row = self._session.get(RuntimeDefinitionRow, str(definition_id))
        if row is None:
            raise EntityNotFoundError(
                f"runtime definition {definition_id} does not exist"
            )
        row.support_tier = support_tier.value
        self._session.flush()
        return _definition_from_row(row)


class RuntimeRevisionRepository:
    """Creation, lookup, and explicit lifecycle changes for immutable revisions."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, revision: RuntimeRevision) -> None:
        self._session.add(
            RuntimeRevisionRow(
                id=str(revision.id),
                runtime_definition_id=str(revision.runtime_definition_id),
                revision_number=revision.revision_number,
                source_kind=revision.source_kind,
                source_reference=revision.source_reference,
                content_digest=revision.content_digest,
                manifest_reference=revision.manifest_reference,
                manifest_digest=revision.manifest_digest,
                built_at=_timestamp_to_text(revision.built_at),
                build_log_reference=revision.build_log_reference,
                approval_state=revision.approval_state,
                approved_at=(
                    None
                    if revision.approved_at is None
                    else _timestamp_to_text(revision.approved_at)
                ),
                approval_note=revision.approval_note,
                requires_trusted_build_confirmation=(
                    revision.requires_trusted_build_confirmation
                ),
                trusted_build_confirmed_at=(
                    None
                    if revision.trusted_build_confirmed_at is None
                    else _timestamp_to_text(revision.trusted_build_confirmed_at)
                ),
            )
        )
        self._session.flush()

    def get(self, revision_id: UUID) -> RuntimeRevision | None:
        row = self._session.get(RuntimeRevisionRow, str(revision_id))
        return None if row is None else _revision_from_row(row)

    def list_for_definition(self, definition_id: UUID) -> list[RuntimeRevision]:
        rows = self._session.scalars(
            select(RuntimeRevisionRow)
            .where(RuntimeRevisionRow.runtime_definition_id == str(definition_id))
            .order_by(RuntimeRevisionRow.revision_number, RuntimeRevisionRow.id)
        )
        return [_revision_from_row(row) for row in rows]

    def newest_approved(self, definition_id: UUID) -> RuntimeRevision | None:
        row = self._session.scalar(
            select(RuntimeRevisionRow)
            .where(
                RuntimeRevisionRow.runtime_definition_id == str(definition_id),
                RuntimeRevisionRow.approval_state == "approved",
            )
            .order_by(RuntimeRevisionRow.revision_number.desc())
            .limit(1)
        )
        return None if row is None else _revision_from_row(row)

    def list_for_definition_descending(
        self, definition_id: UUID
    ) -> list[RuntimeRevision]:
        rows = self._session.scalars(
            select(RuntimeRevisionRow)
            .where(RuntimeRevisionRow.runtime_definition_id == str(definition_id))
            .order_by(RuntimeRevisionRow.revision_number.desc(), RuntimeRevisionRow.id)
        )
        return [_revision_from_row(row) for row in rows]

    def approve(
        self, revision_id: UUID, *, approved_at: datetime, note: str | None = None
    ) -> RuntimeRevision:
        current, row = self._require_revision(revision_id)
        updated = current.approve(approved_at=approved_at, note=note)
        normalized_approved_at = updated.approved_at
        assert normalized_approved_at is not None
        row.approval_state = updated.approval_state
        row.approved_at = _timestamp_to_text(normalized_approved_at)
        row.approval_note = updated.approval_note
        self._session.flush()
        return updated

    def revoke_approval(self, revision_id: UUID) -> RuntimeRevision:
        current, row = self._require_revision(revision_id)
        updated = current.revoke_approval()
        row.approval_state = updated.approval_state
        row.approved_at = None
        row.approval_note = None
        self._session.flush()
        return updated

    def confirm_trusted_build(
        self, revision_id: UUID, *, confirmed_at: datetime
    ) -> RuntimeRevision:
        current, row = self._require_revision(revision_id)
        updated = current.confirm_trusted_build(confirmed_at=confirmed_at)
        confirmed = updated.trusted_build_confirmed_at
        assert confirmed is not None
        row.trusted_build_confirmed_at = _timestamp_to_text(confirmed)
        self._session.flush()
        return updated

    def revoke_trusted_build_confirmation(self, revision_id: UUID) -> RuntimeRevision:
        current, row = self._require_revision(revision_id)
        updated = current.revoke_trusted_build_confirmation()
        row.trusted_build_confirmed_at = None
        self._session.flush()
        return updated

    def _require_revision(
        self, revision_id: UUID
    ) -> tuple[RuntimeRevision, RuntimeRevisionRow]:
        row = self._session.get(RuntimeRevisionRow, str(revision_id))
        if row is None:
            raise EntityNotFoundError(f"runtime revision {revision_id} does not exist")
        return _revision_from_row(row), row


class RuntimeContainerArtifactRepository:
    """Create/read-only persistence for immutable container artifacts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, artifact: RuntimeContainerArtifact) -> None:
        self._session.add(
            RuntimeContainerArtifactRow(
                runtime_revision_id=str(artifact.runtime_revision_id),
                image_id=artifact.image_id,
            )
        )
        self._session.flush()

    def get(self, runtime_revision_id: UUID) -> RuntimeContainerArtifact | None:
        row = self._session.get(RuntimeContainerArtifactRow, str(runtime_revision_id))
        if row is None:
            return None
        return RuntimeContainerArtifact(
            runtime_revision_id=UUID(row.runtime_revision_id),
            image_id=row.image_id,
        )


class RuntimeExecutionProfileRepository:
    """Create/read-only persistence for immutable execution profiles."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, profile: RuntimeExecutionProfile) -> None:
        self._session.add(
            RuntimeExecutionProfileRow(
                runtime_revision_id=str(profile.runtime_revision_id),
                runtime_kind=profile.runtime_kind,
                expected_application_name=profile.expected_application_name,
                expected_kodi_version=profile.expected_kodi_version,
                expected_version_tag=profile.expected_version_tag,
                expected_prerelease_ordinal=profile.expected_prerelease_ordinal,
                jsonrpc_port=profile.jsonrpc_port,
                jsonrpc_path=profile.jsonrpc_path,
                process_observation=profile.process_observation,
                readiness_probe=profile.readiness_probe,
                readiness_timeout_seconds=profile.readiness_timeout_seconds,
                poll_interval_seconds=profile.poll_interval_seconds,
                http_timeout_seconds=profile.http_timeout_seconds,
            )
        )
        self._session.flush()

    def get(self, runtime_revision_id: UUID) -> RuntimeExecutionProfile | None:
        row = self._session.get(RuntimeExecutionProfileRow, str(runtime_revision_id))
        if row is None:
            return None
        return RuntimeExecutionProfile(
            runtime_revision_id=UUID(row.runtime_revision_id),
            runtime_kind=row.runtime_kind,
            expected_application_name=row.expected_application_name,
            expected_kodi_version=row.expected_kodi_version,
            expected_version_tag=row.expected_version_tag,
            expected_prerelease_ordinal=row.expected_prerelease_ordinal,
            jsonrpc_port=row.jsonrpc_port,
            jsonrpc_path=row.jsonrpc_path,
            process_observation=row.process_observation,
            readiness_probe=row.readiness_probe,
            readiness_timeout_seconds=row.readiness_timeout_seconds,
            poll_interval_seconds=row.poll_interval_seconds,
            http_timeout_seconds=row.http_timeout_seconds,
        )


class RuntimeValidationEvidenceRepository:
    """Append-only persistence and effective evidence retrieval."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, evidence: RuntimeValidationEvidence) -> None:
        self._session.add(
            RuntimeValidationEvidenceRow(
                id=str(evidence.id),
                runtime_revision_id=str(evidence.runtime_revision_id),
                contract_version=evidence.contract_version,
                result=evidence.result,
                started_at=_timestamp_to_text(evidence.started_at),
                completed_at=_timestamp_to_text(evidence.completed_at),
                evidence_reference=evidence.evidence_reference,
                summary=evidence.summary,
                recorded_at=_timestamp_to_text(evidence.recorded_at),
            )
        )
        self._session.flush()

    def list_for_revision(self, revision_id: UUID) -> list[RuntimeValidationEvidence]:
        rows = self._session.scalars(
            select(RuntimeValidationEvidenceRow)
            .where(RuntimeValidationEvidenceRow.runtime_revision_id == str(revision_id))
            .order_by(
                RuntimeValidationEvidenceRow.recorded_at,
                RuntimeValidationEvidenceRow.id,
            )
        )
        return [self._from_row(row) for row in rows]

    def latest_for_contract(
        self, revision_id: UUID, contract_version: str
    ) -> RuntimeValidationEvidence | None:
        row = self._session.scalar(
            select(RuntimeValidationEvidenceRow)
            .where(
                RuntimeValidationEvidenceRow.runtime_revision_id == str(revision_id),
                RuntimeValidationEvidenceRow.contract_version == contract_version,
            )
            .order_by(
                RuntimeValidationEvidenceRow.recorded_at.desc(),
                RuntimeValidationEvidenceRow.id.desc(),
            )
            .limit(1)
        )
        return None if row is None else self._from_row(row)

    @staticmethod
    def _from_row(row: RuntimeValidationEvidenceRow) -> RuntimeValidationEvidence:
        return RuntimeValidationEvidence(
            id=UUID(row.id),
            runtime_revision_id=UUID(row.runtime_revision_id),
            contract_version=row.contract_version,
            result=row.result,
            started_at=_timestamp_from_text(row.started_at),
            completed_at=_timestamp_from_text(row.completed_at),
            evidence_reference=row.evidence_reference,
            summary=row.summary,
            recorded_at=_timestamp_from_text(row.recorded_at),
        )


class SnapshotRepository:
    """Create-only artifact identity with explicit terminal validation updates."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, snapshot: Snapshot) -> None:
        self._session.add(
            SnapshotRow(
                id=str(snapshot.id),
                source_managed_instance_id=str(snapshot.source_managed_instance_id),
                runtime_revision_id=str(snapshot.runtime_revision_id),
                captured_fixture_definition_id=(
                    None
                    if snapshot.captured_fixture_definition_id is None
                    else str(snapshot.captured_fixture_definition_id)
                ),
                captured_at=_timestamp_to_text(snapshot.captured_at),
                prior_runtime_state=snapshot.prior_runtime_state,
                label=snapshot.label,
                manifest_version=snapshot.manifest_version,
                archive_digest=snapshot.archive_digest,
                manifest_digest=snapshot.manifest_digest,
                archive_size=snapshot.archive_size,
                member_count=snapshot.member_count,
                state=snapshot.state,
                validation_at=(
                    None
                    if snapshot.validation_at is None
                    else _timestamp_to_text(snapshot.validation_at)
                ),
                validation_result=snapshot.validation_result,
            )
        )
        self._session.flush()

    def get(self, snapshot_id: UUID) -> Snapshot | None:
        row = self._session.get(SnapshotRow, str(snapshot_id))
        return None if row is None else _snapshot_from_row(row)

    def list_for_instance(self, instance_id: UUID) -> list[Snapshot]:
        rows = self._session.scalars(
            select(SnapshotRow)
            .where(SnapshotRow.source_managed_instance_id == str(instance_id))
            .order_by(SnapshotRow.captured_at.desc(), SnapshotRow.id.desc())
        )
        return [_snapshot_from_row(row) for row in rows]

    def set_prior_runtime_state(
        self,
        snapshot_id: UUID,
        prior_runtime_state: InstanceRuntimeState,
    ) -> bool:
        result = self._session.execute(
            update(SnapshotRow)
            .where(
                SnapshotRow.id == str(snapshot_id),
                SnapshotRow.state == SnapshotState.CREATING,
                SnapshotRow.prior_runtime_state.is_(None),
            )
            .values(prior_runtime_state=prior_runtime_state)
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def mark_available(
        self,
        snapshot_id: UUID,
        *,
        archive_digest: str,
        manifest_digest: str,
        archive_size: int,
        member_count: int,
        validated_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(SnapshotRow)
            .where(
                SnapshotRow.id == str(snapshot_id),
                SnapshotRow.state == SnapshotState.CREATING,
            )
            .values(
                archive_digest=archive_digest,
                manifest_digest=manifest_digest,
                archive_size=archive_size,
                member_count=member_count,
                state=SnapshotState.AVAILABLE,
                validation_at=_timestamp_to_text(validated_at),
                validation_result=SnapshotValidationResult.PASSED,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def mark_invalid(
        self,
        snapshot_id: UUID,
        *,
        quarantined: bool,
        validated_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(SnapshotRow)
            .where(
                SnapshotRow.id == str(snapshot_id),
                SnapshotRow.state == SnapshotState.CREATING,
            )
            .values(
                state=(
                    SnapshotState.QUARANTINED if quarantined else SnapshotState.FAILED
                ),
                validation_at=_timestamp_to_text(validated_at),
                validation_result=SnapshotValidationResult.FAILED,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1


class SnapshotCreateOperationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: SnapshotCreateOperation) -> None:
        self._session.add(
            SnapshotCreateOperationRow(
                id=str(operation.id),
                snapshot_id=str(operation.snapshot_id),
                managed_instance_id=str(operation.managed_instance_id),
                requested_at=_timestamp_to_text(operation.requested_at),
                state=operation.state,
                phase=operation.phase,
                reconciliation_required=False,
            )
        )
        self._session.flush()

    def get(self, operation_id: UUID) -> SnapshotCreateOperation | None:
        row = self._session.get(SnapshotCreateOperationRow, str(operation_id))
        return None if row is None else _snapshot_operation_from_row(row)

    def active_for_instance(self, instance_id: UUID) -> SnapshotCreateOperation | None:
        row = self._session.scalar(
            select(SnapshotCreateOperationRow)
            .where(
                SnapshotCreateOperationRow.managed_instance_id == str(instance_id),
                SnapshotCreateOperationRow.state.in_(
                    (
                        SnapshotCreateOperationState.QUEUED,
                        SnapshotCreateOperationState.RUNNING,
                    )
                ),
            )
            .order_by(SnapshotCreateOperationRow.requested_at.desc())
            .limit(1)
        )
        return None if row is None else _snapshot_operation_from_row(row)

    def latest_for_instance(self, instance_id: UUID) -> SnapshotCreateOperation | None:
        row = self._session.scalar(
            select(SnapshotCreateOperationRow)
            .where(SnapshotCreateOperationRow.managed_instance_id == str(instance_id))
            .order_by(
                SnapshotCreateOperationRow.requested_at.desc(),
                SnapshotCreateOperationRow.id.desc(),
            )
            .limit(1)
        )
        return None if row is None else _snapshot_operation_from_row(row)

    def list_queued(self) -> list[SnapshotCreateOperation]:
        rows = self._session.scalars(
            select(SnapshotCreateOperationRow)
            .where(
                SnapshotCreateOperationRow.state == SnapshotCreateOperationState.QUEUED
            )
            .order_by(SnapshotCreateOperationRow.requested_at)
        )
        return [_snapshot_operation_from_row(row) for row in rows]

    def acquire(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(SnapshotCreateOperationRow)
            .where(
                SnapshotCreateOperationRow.id == str(operation_id),
                SnapshotCreateOperationRow.state == SnapshotCreateOperationState.QUEUED,
            )
            .values(
                state=SnapshotCreateOperationState.RUNNING,
                phase=SnapshotCreateOperationPhase.ACQUIRING_LEASES,
                started_at=_timestamp_to_text(now),
                heartbeat_at=_timestamp_to_text(now),
                lease_token_digest=token_digest,
                lease_expires_at=_timestamp_to_text(lease_expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def update_phase(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        phase: SnapshotCreateOperationPhase,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(SnapshotCreateOperationRow)
            .where(
                SnapshotCreateOperationRow.id == str(operation_id),
                SnapshotCreateOperationRow.state
                == SnapshotCreateOperationState.RUNNING,
                SnapshotCreateOperationRow.lease_token_digest == token_digest,
                SnapshotCreateOperationRow.lease_expires_at > _timestamp_to_text(now),
            )
            .values(
                phase=phase,
                heartbeat_at=_timestamp_to_text(now),
                lease_expires_at=_timestamp_to_text(lease_expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def finish(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        state: SnapshotCreateOperationState,
        phase: SnapshotCreateOperationPhase,
        failure_reason: str | None,
        reconciliation_required: bool,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(SnapshotCreateOperationRow)
            .where(
                SnapshotCreateOperationRow.id == str(operation_id),
                SnapshotCreateOperationRow.state
                == SnapshotCreateOperationState.RUNNING,
                SnapshotCreateOperationRow.lease_token_digest == token_digest,
            )
            .values(
                state=state,
                phase=phase,
                heartbeat_at=_timestamp_to_text(now),
                completed_at=_timestamp_to_text(now),
                failure_reason=failure_reason,
                reconciliation_required=reconciliation_required,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def mark_stale_reconciliation(self, operation_id: UUID, *, now: datetime) -> bool:
        result = self._session.execute(
            update(SnapshotCreateOperationRow)
            .where(
                SnapshotCreateOperationRow.id == str(operation_id),
                SnapshotCreateOperationRow.state
                == SnapshotCreateOperationState.RUNNING,
                SnapshotCreateOperationRow.lease_expires_at <= _timestamp_to_text(now),
            )
            .values(
                state=SnapshotCreateOperationState.RECONCILIATION_REQUIRED,
                phase=SnapshotCreateOperationPhase.RECONCILIATION_REQUIRED,
                heartbeat_at=_timestamp_to_text(now),
                completed_at=_timestamp_to_text(now),
                failure_reason="Snapshot capture requires operator reconciliation.",
                reconciliation_required=True,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def prune_terminal(self, instance_id: UUID, *, retain: int = 20) -> int:
        rows = list(
            self._session.scalars(
                select(SnapshotCreateOperationRow.id)
                .where(
                    SnapshotCreateOperationRow.managed_instance_id == str(instance_id),
                    SnapshotCreateOperationRow.state.not_in(
                        (
                            SnapshotCreateOperationState.QUEUED,
                            SnapshotCreateOperationState.RUNNING,
                        )
                    ),
                )
                .order_by(
                    SnapshotCreateOperationRow.requested_at.desc(),
                    SnapshotCreateOperationRow.id.desc(),
                )
                .offset(retain)
            )
        )
        if not rows:
            return 0
        result = self._session.execute(
            delete(SnapshotCreateOperationRow).where(
                SnapshotCreateOperationRow.id.in_(rows)
            )
        )
        return int(getattr(result, "rowcount", 0))


class ManagedInstanceMutationLeaseRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def reserve(
        self,
        instance_id: UUID,
        *,
        kind: ManagedInstanceMutationKind,
        operation_id: UUID,
    ) -> None:
        self._session.add(
            ManagedInstanceMutationLeaseRow(
                managed_instance_id=str(instance_id),
                operation_kind=kind,
                operation_id=str(operation_id),
            )
        )
        self._session.flush()

    def release_expired_short_mutation(
        self, instance_id: UUID, *, now: datetime
    ) -> bool:
        result = self._session.execute(
            delete(ManagedInstanceMutationLeaseRow).where(
                ManagedInstanceMutationLeaseRow.managed_instance_id == str(instance_id),
                ManagedInstanceMutationLeaseRow.operation_kind.in_(
                    (
                        ManagedInstanceMutationKind.LIFECYCLE,
                        ManagedInstanceMutationKind.FIXTURE_ASSIGNMENT,
                        ManagedInstanceMutationKind.RUNTIME_REASSIGNMENT,
                    )
                ),
                ManagedInstanceMutationLeaseRow.lease_token_digest.is_not(None),
                ManagedInstanceMutationLeaseRow.lease_expires_at
                <= _timestamp_to_text(now),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def claim(
        self,
        instance_id: UUID,
        *,
        operation_id: UUID,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(ManagedInstanceMutationLeaseRow)
            .where(
                ManagedInstanceMutationLeaseRow.managed_instance_id == str(instance_id),
                ManagedInstanceMutationLeaseRow.operation_id == str(operation_id),
                ManagedInstanceMutationLeaseRow.lease_token_digest.is_(None),
            )
            .values(
                lease_token_digest=token_digest,
                acquired_at=_timestamp_to_text(now),
                heartbeat_at=_timestamp_to_text(now),
                lease_expires_at=_timestamp_to_text(expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def renew(
        self,
        instance_id: UUID,
        *,
        operation_id: UUID,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(ManagedInstanceMutationLeaseRow)
            .where(
                ManagedInstanceMutationLeaseRow.managed_instance_id == str(instance_id),
                ManagedInstanceMutationLeaseRow.operation_id == str(operation_id),
                ManagedInstanceMutationLeaseRow.lease_token_digest == token_digest,
                ManagedInstanceMutationLeaseRow.lease_expires_at
                > _timestamp_to_text(now),
            )
            .values(
                heartbeat_at=_timestamp_to_text(now),
                lease_expires_at=_timestamp_to_text(expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def release(
        self, instance_id: UUID, *, operation_id: UUID, token_digest: str
    ) -> bool:
        result = self._session.execute(
            delete(ManagedInstanceMutationLeaseRow).where(
                ManagedInstanceMutationLeaseRow.managed_instance_id == str(instance_id),
                ManagedInstanceMutationLeaseRow.operation_id == str(operation_id),
                ManagedInstanceMutationLeaseRow.lease_token_digest == token_digest,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def release_for_operation(self, instance_id: UUID, operation_id: UUID) -> bool:
        result = self._session.execute(
            delete(ManagedInstanceMutationLeaseRow).where(
                ManagedInstanceMutationLeaseRow.managed_instance_id == str(instance_id),
                ManagedInstanceMutationLeaseRow.operation_id == str(operation_id),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def current(self, instance_id: UUID) -> ManagedInstanceMutationLeaseRow | None:
        return self._session.get(ManagedInstanceMutationLeaseRow, str(instance_id))


class GlobalHeavyOperationLeaseRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def reserve(self, *, kind: GlobalHeavyOperationKind, operation_id: UUID) -> None:
        self._session.add(
            GlobalHeavyOperationLeaseRow(
                singleton_id=1, operation_kind=kind, operation_id=str(operation_id)
            )
        )
        self._session.flush()

    def claim(
        self,
        *,
        operation_id: UUID,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(GlobalHeavyOperationLeaseRow)
            .where(
                GlobalHeavyOperationLeaseRow.singleton_id == 1,
                GlobalHeavyOperationLeaseRow.operation_id == str(operation_id),
                GlobalHeavyOperationLeaseRow.lease_token_digest.is_(None),
            )
            .values(
                lease_token_digest=token_digest,
                acquired_at=_timestamp_to_text(now),
                heartbeat_at=_timestamp_to_text(now),
                lease_expires_at=_timestamp_to_text(expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def renew(
        self,
        *,
        operation_id: UUID,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(GlobalHeavyOperationLeaseRow)
            .where(
                GlobalHeavyOperationLeaseRow.singleton_id == 1,
                GlobalHeavyOperationLeaseRow.operation_id == str(operation_id),
                GlobalHeavyOperationLeaseRow.lease_token_digest == token_digest,
                GlobalHeavyOperationLeaseRow.lease_expires_at > _timestamp_to_text(now),
            )
            .values(
                heartbeat_at=_timestamp_to_text(now),
                lease_expires_at=_timestamp_to_text(expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def release(self, *, operation_id: UUID, token_digest: str) -> bool:
        result = self._session.execute(
            delete(GlobalHeavyOperationLeaseRow).where(
                GlobalHeavyOperationLeaseRow.singleton_id == 1,
                GlobalHeavyOperationLeaseRow.operation_id == str(operation_id),
                GlobalHeavyOperationLeaseRow.lease_token_digest == token_digest,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def release_for_operation(self, operation_id: UUID) -> bool:
        result = self._session.execute(
            delete(GlobalHeavyOperationLeaseRow).where(
                GlobalHeavyOperationLeaseRow.singleton_id == 1,
                GlobalHeavyOperationLeaseRow.operation_id == str(operation_id),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def current(self) -> GlobalHeavyOperationLeaseRow | None:
        return self._session.get(GlobalHeavyOperationLeaseRow, 1)
