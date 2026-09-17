"""Explicit repositories for targets and managed instances."""

import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from kodi_lab.domain import (
    DevelopmentSource,
    DevelopmentSourceAttachmentOperation,
    FixtureDefinition,
    GlobalHeavyOperationKind,
    InstanceRuntimeState,
    ManagedInstance,
    ManagedInstanceDeleteOperation,
    ManagedInstanceDeleteOperationPhase,
    ManagedInstanceDeleteOperationState,
    ManagedInstanceDeletePriorState,
    ManagedInstanceDevelopmentSource,
    ManagedInstanceLifecycleOperation,
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceMutationKind,
    ManagedInstanceSemanticState,
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
    SnapshotDeleteOperation,
    SnapshotDeleteOperationPhase,
    SnapshotDeleteOperationState,
    SnapshotRestoreOperation,
    SnapshotRestoreOperationPhase,
    SnapshotRestoreOperationState,
    SnapshotSourceBinding,
    SnapshotState,
    SnapshotValidationResult,
    Target,
)
from kodi_lab.storage.models import (
    DevelopmentSourceAttachmentOperationRow,
    DevelopmentSourceRow,
    FixtureDefinitionRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceDeleteOperationRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ResetOperationRow,
    RuntimeContainerArtifactRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    RuntimeValidationEvidenceRow,
    SnapshotCreateOperationRow,
    SnapshotDeleteOperationRow,
    SnapshotRestoreOperationRow,
    SnapshotRow,
    TargetRow,
)


class EntityNotFoundError(LookupError):
    """Raised when an update target no longer exists."""


def _snapshot_from_row(row: SnapshotRow) -> Snapshot:
    return Snapshot(
        id=UUID(row.id),
        source_managed_instance_id=(
            None
            if row.source_managed_instance_id is None
            else UUID(row.source_managed_instance_id)
        ),
        original_source_managed_instance_id=UUID(
            row.original_source_managed_instance_id
        ),
        source_binding=row.source_binding,
        source_instance_name=row.source_instance_name,
        source_deleted_at=(
            None
            if row.source_deleted_at is None
            else _timestamp_from_text(row.source_deleted_at)
        ),
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


def _snapshot_restore_operation_from_row(
    row: SnapshotRestoreOperationRow,
) -> SnapshotRestoreOperation:
    return SnapshotRestoreOperation(
        id=UUID(row.id),
        snapshot_id=UUID(row.snapshot_id),
        managed_instance_id=UUID(row.managed_instance_id),
        requested_at=_timestamp_from_text(row.requested_at),
        state=row.state,
        phase=row.phase,
        prior_source_runtime_state=row.prior_source_runtime_state,
        old_container_id=row.old_container_id,
        fresh_container_id=row.fresh_container_id,
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


def _snapshot_delete_operation_from_row(
    row: SnapshotDeleteOperationRow,
) -> SnapshotDeleteOperation:
    return SnapshotDeleteOperation(
        id=UUID(row.id),
        snapshot_id=UUID(row.snapshot_id),
        managed_instance_id=UUID(row.managed_instance_id),
        prior_snapshot_state=row.prior_snapshot_state,
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


def _managed_instance_delete_operation_from_row(
    row: ManagedInstanceDeleteOperationRow,
) -> ManagedInstanceDeleteOperation:
    return ManagedInstanceDeleteOperation(
        id=UUID(row.id),
        original_managed_instance_id=UUID(row.original_managed_instance_id),
        source_instance_name=row.source_instance_name,
        runtime_revision_id=UUID(row.runtime_revision_id),
        fixture_definition_id=(
            None
            if row.fixture_definition_id is None
            else UUID(row.fixture_definition_id)
        ),
        preserve_snapshots=row.preserve_snapshots,
        snapshot_count=row.snapshot_count,
        aggregate_snapshot_bytes=row.aggregate_snapshot_bytes,
        current_snapshot_id=(
            None if row.current_snapshot_id is None else UUID(row.current_snapshot_id)
        ),
        completed_snapshot_count=row.completed_snapshot_count,
        expected_container_id=row.expected_container_id,
        observed_container_id=row.observed_container_id,
        prior_state=row.prior_state,
        notes_at_deletion=row.notes_at_deletion,
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


def _managed_instance_lifecycle_operation_from_row(
    row: ManagedInstanceLifecycleOperationRow,
) -> ManagedInstanceLifecycleOperation:
    return ManagedInstanceLifecycleOperation(
        id=UUID(row.id),
        original_managed_instance_id=UUID(row.original_managed_instance_id),
        managed_instance_id=(
            None if row.managed_instance_id is None else UUID(row.managed_instance_id)
        ),
        instance_name=row.instance_name,
        kind=row.kind,
        requested_at=_timestamp_from_text(row.requested_at),
        started_at=_timestamp_from_text(row.started_at),
        completed_at=(
            None if row.completed_at is None else _timestamp_from_text(row.completed_at)
        ),
        prior_state=row.prior_state,
        resulting_state=(
            None
            if row.resulting_state is None
            else ManagedInstanceSemanticState(row.resulting_state)
        ),
        state=row.state,
        failure_reason=row.failure_reason,
        reconciliation_required=row.reconciliation_required,
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


def _development_source_from_row(row: DevelopmentSourceRow) -> DevelopmentSource:
    extension_types = json.loads(row.extension_types_json)
    if not isinstance(extension_types, list) or not all(
        isinstance(value, str) for value in extension_types
    ):
        raise ValueError("stored development-source extension metadata is invalid")
    return DevelopmentSource(
        id=UUID(row.id),
        logical_name=row.logical_name,
        source_kind=row.source_kind,
        relative_locator=row.relative_locator,
        addon_id=row.addon_id,
        observed_name=row.observed_name,
        observed_version=row.observed_version,
        extension_types=tuple(extension_types),
        registered_at=_timestamp_from_text(row.registered_at),
        validated_at=_timestamp_from_text(row.validated_at),
    )


def _development_source_attachment_operation_from_row(
    row: DevelopmentSourceAttachmentOperationRow,
) -> DevelopmentSourceAttachmentOperation:
    return DevelopmentSourceAttachmentOperation(
        id=UUID(row.id),
        original_managed_instance_id=UUID(row.original_managed_instance_id),
        managed_instance_id=(
            None if row.managed_instance_id is None else UUID(row.managed_instance_id)
        ),
        development_source_id=UUID(row.development_source_id),
        source_logical_name=row.source_logical_name,
        addon_id=row.addon_id,
        kind=row.kind,
        requested_at=_timestamp_from_text(row.requested_at),
        completed_at=_timestamp_from_text(row.completed_at),
        state=row.state,
        failure_reason=row.failure_reason,
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


class DevelopmentSourceRepository:
    """Create/read-only persistence for global development-source registrations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, source: DevelopmentSource) -> None:
        self._session.add(
            DevelopmentSourceRow(
                id=str(source.id),
                logical_name=source.logical_name,
                source_kind=source.source_kind,
                relative_locator=source.relative_locator,
                addon_id=source.addon_id,
                observed_name=source.observed_name,
                observed_version=source.observed_version,
                extension_types_json=json.dumps(
                    list(source.extension_types), separators=(",", ":")
                ),
                registered_at=_timestamp_to_text(source.registered_at),
                validated_at=_timestamp_to_text(source.validated_at),
            )
        )
        self._session.flush()

    def get(self, source_id: UUID) -> DevelopmentSource | None:
        row = self._session.get(DevelopmentSourceRow, str(source_id))
        return None if row is None else _development_source_from_row(row)

    def get_by_logical_name(self, logical_name: str) -> DevelopmentSource | None:
        row = self._session.scalar(
            select(DevelopmentSourceRow).where(
                DevelopmentSourceRow.logical_name == logical_name
            )
        )
        return None if row is None else _development_source_from_row(row)

    def get_by_relative_locator(
        self, relative_locator: str
    ) -> DevelopmentSource | None:
        row = self._session.scalar(
            select(DevelopmentSourceRow).where(
                DevelopmentSourceRow.relative_locator == relative_locator
            )
        )
        return None if row is None else _development_source_from_row(row)

    def get_by_addon_id(self, addon_id: str) -> DevelopmentSource | None:
        row = self._session.scalar(
            select(DevelopmentSourceRow).where(
                DevelopmentSourceRow.addon_id == addon_id
            )
        )
        return None if row is None else _development_source_from_row(row)

    def list(self) -> list[DevelopmentSource]:
        rows = self._session.scalars(
            select(DevelopmentSourceRow).order_by(
                DevelopmentSourceRow.logical_name, DevelopmentSourceRow.id
            )
        )
        return [_development_source_from_row(row) for row in rows]


class ManagedInstanceDevelopmentSourceRepository:
    """Persistence for mode-neutral per-instance source associations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, attachment: ManagedInstanceDevelopmentSource) -> None:
        self._session.add(
            ManagedInstanceDevelopmentSourceRow(
                managed_instance_id=str(attachment.managed_instance_id),
                development_source_id=str(attachment.development_source_id),
                attached_at=_timestamp_to_text(attachment.attached_at),
            )
        )
        self._session.flush()

    def get(
        self, instance_id: UUID, source_id: UUID
    ) -> ManagedInstanceDevelopmentSource | None:
        row = self._session.get(
            ManagedInstanceDevelopmentSourceRow,
            (str(instance_id), str(source_id)),
        )
        return (
            None
            if row is None
            else ManagedInstanceDevelopmentSource(
                managed_instance_id=UUID(row.managed_instance_id),
                development_source_id=UUID(row.development_source_id),
                attached_at=_timestamp_from_text(row.attached_at),
            )
        )

    def get_by_addon_id(
        self, instance_id: UUID, addon_id: str
    ) -> ManagedInstanceDevelopmentSource | None:
        row = self._session.scalar(
            select(ManagedInstanceDevelopmentSourceRow)
            .join(
                DevelopmentSourceRow,
                DevelopmentSourceRow.id
                == ManagedInstanceDevelopmentSourceRow.development_source_id,
            )
            .where(
                ManagedInstanceDevelopmentSourceRow.managed_instance_id
                == str(instance_id),
                DevelopmentSourceRow.addon_id == addon_id,
            )
        )
        return (
            None
            if row is None
            else ManagedInstanceDevelopmentSource(
                managed_instance_id=UUID(row.managed_instance_id),
                development_source_id=UUID(row.development_source_id),
                attached_at=_timestamp_from_text(row.attached_at),
            )
        )

    def list_for_instance(
        self, instance_id: UUID
    ) -> list[ManagedInstanceDevelopmentSource]:
        rows = self._session.scalars(
            select(ManagedInstanceDevelopmentSourceRow)
            .join(
                DevelopmentSourceRow,
                DevelopmentSourceRow.id
                == ManagedInstanceDevelopmentSourceRow.development_source_id,
            )
            .where(
                ManagedInstanceDevelopmentSourceRow.managed_instance_id
                == str(instance_id)
            )
            .order_by(DevelopmentSourceRow.logical_name, DevelopmentSourceRow.id)
        )
        return [
            ManagedInstanceDevelopmentSource(
                managed_instance_id=UUID(row.managed_instance_id),
                development_source_id=UUID(row.development_source_id),
                attached_at=_timestamp_from_text(row.attached_at),
            )
            for row in rows
        ]

    def list_instance_ids_for_source(self, source_id: UUID) -> list[UUID]:
        values = self._session.scalars(
            select(ManagedInstanceDevelopmentSourceRow.managed_instance_id)
            .where(
                ManagedInstanceDevelopmentSourceRow.development_source_id
                == str(source_id)
            )
            .order_by(ManagedInstanceDevelopmentSourceRow.managed_instance_id)
        )
        return [UUID(value) for value in values]

    def counts_by_source(self) -> dict[UUID, int]:
        """Count current live attachments for every represented source."""

        rows = self._session.execute(
            select(
                ManagedInstanceDevelopmentSourceRow.development_source_id,
                func.count(),
            )
            .group_by(ManagedInstanceDevelopmentSourceRow.development_source_id)
            .order_by(ManagedInstanceDevelopmentSourceRow.development_source_id)
        )
        return {UUID(source_id): int(count) for source_id, count in rows}

    def delete(self, instance_id: UUID, source_id: UUID) -> bool:
        result = self._session.execute(
            delete(ManagedInstanceDevelopmentSourceRow).where(
                ManagedInstanceDevelopmentSourceRow.managed_instance_id
                == str(instance_id),
                ManagedInstanceDevelopmentSourceRow.development_source_id
                == str(source_id),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def delete_for_instance(self, instance_id: UUID) -> int:
        result = self._session.execute(
            delete(ManagedInstanceDevelopmentSourceRow).where(
                ManagedInstanceDevelopmentSourceRow.managed_instance_id
                == str(instance_id)
            )
        )
        return int(getattr(result, "rowcount", 0))


class DevelopmentSourceAttachmentOperationRepository:
    """Typed terminal history for development-source attachment changes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: DevelopmentSourceAttachmentOperation) -> None:
        self._session.add(
            DevelopmentSourceAttachmentOperationRow(
                id=str(operation.id),
                original_managed_instance_id=str(
                    operation.original_managed_instance_id
                ),
                managed_instance_id=(
                    None
                    if operation.managed_instance_id is None
                    else str(operation.managed_instance_id)
                ),
                development_source_id=str(operation.development_source_id),
                source_logical_name=operation.source_logical_name,
                addon_id=operation.addon_id,
                kind=operation.kind,
                requested_at=_timestamp_to_text(operation.requested_at),
                completed_at=_timestamp_to_text(operation.completed_at),
                state=operation.state,
                failure_reason=operation.failure_reason,
            )
        )
        self._session.flush()

    def get(self, operation_id: UUID) -> DevelopmentSourceAttachmentOperation | None:
        row = self._session.get(
            DevelopmentSourceAttachmentOperationRow, str(operation_id)
        )
        return (
            None
            if row is None
            else _development_source_attachment_operation_from_row(row)
        )

    def list_for_original_instance(
        self, instance_id: UUID
    ) -> list[DevelopmentSourceAttachmentOperation]:
        rows = self._session.scalars(
            select(DevelopmentSourceAttachmentOperationRow)
            .where(
                DevelopmentSourceAttachmentOperationRow.original_managed_instance_id
                == str(instance_id)
            )
            .order_by(
                DevelopmentSourceAttachmentOperationRow.requested_at.desc(),
                DevelopmentSourceAttachmentOperationRow.id.desc(),
            )
        )
        return [_development_source_attachment_operation_from_row(row) for row in rows]

    def clear_live_instance(self, instance_id: UUID) -> int:
        result = self._session.execute(
            update(DevelopmentSourceAttachmentOperationRow)
            .where(
                DevelopmentSourceAttachmentOperationRow.managed_instance_id
                == str(instance_id)
            )
            .values(managed_instance_id=None)
        )
        return int(getattr(result, "rowcount", 0))


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

    def delete(self, target_id: UUID) -> bool:
        row = self._session.get(ManagedInstanceRow, str(target_id))
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True


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

    def list_for_instance(self, instance_id: UUID) -> list[ResetOperation]:
        rows = self._session.scalars(
            select(ResetOperationRow)
            .where(ResetOperationRow.managed_instance_id == str(instance_id))
            .order_by(
                ResetOperationRow.requested_at.desc(), ResetOperationRow.id.desc()
            )
        )
        return [_reset_operation_from_row(row) for row in rows]

    def reconciliation_for_instance(
        self, instance_id: UUID, *, limit: int = 2
    ) -> list[ResetOperation]:
        rows = self._session.scalars(
            select(ResetOperationRow)
            .where(
                ResetOperationRow.managed_instance_id == str(instance_id),
                ResetOperationRow.reconciliation_required.is_(True),
            )
            .order_by(
                ResetOperationRow.requested_at.desc(), ResetOperationRow.id.desc()
            )
            .limit(limit)
        )
        return [_reset_operation_from_row(row) for row in rows]

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

    def mark_stale_reconciliation(
        self, operation_id: UUID, *, token_digest: str, now: datetime
    ) -> bool:
        result = self._session.execute(
            update(ResetOperationRow)
            .where(
                ResetOperationRow.id == str(operation_id),
                ResetOperationRow.state == ResetOperationState.RUNNING,
                ResetOperationRow.lease_token_digest == token_digest,
                ResetOperationRow.lease_expires_at <= _timestamp_to_text(now),
            )
            .values(
                state=ResetOperationState.RECONCILIATION_REQUIRED,
                completed_at=_timestamp_to_text(now),
                failure_reason=func.coalesce(
                    ResetOperationRow.failure_reason,
                    "Reset worker lease expired; reconciliation is required.",
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
                    ResetOperationRow.state.in_(
                        [ResetOperationState.SUCCEEDED, ResetOperationState.FAILED]
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
                runtime_uid=profile.runtime_uid,
                runtime_gid=profile.runtime_gid,
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
            runtime_uid=row.runtime_uid,
            runtime_gid=row.runtime_gid,
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
        original_source_id = snapshot.original_source_managed_instance_id
        if original_source_id is None:
            raise ValueError("snapshot original source identity is required")
        self._session.add(
            SnapshotRow(
                id=str(snapshot.id),
                source_managed_instance_id=(
                    None
                    if snapshot.source_managed_instance_id is None
                    else str(snapshot.source_managed_instance_id)
                ),
                original_source_managed_instance_id=str(original_source_id),
                source_binding=snapshot.source_binding,
                source_instance_name=snapshot.source_instance_name,
                source_deleted_at=(
                    None
                    if snapshot.source_deleted_at is None
                    else _timestamp_to_text(snapshot.source_deleted_at)
                ),
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

    def list_preserved(self) -> list[Snapshot]:
        rows = self._session.scalars(
            select(SnapshotRow)
            .where(SnapshotRow.source_binding == SnapshotSourceBinding.DELETED_SOURCE)
            .order_by(SnapshotRow.captured_at.desc(), SnapshotRow.id.desc())
        )
        return [_snapshot_from_row(row) for row in rows]

    def list_for_original_instance(self, instance_id: UUID) -> list[Snapshot]:
        rows = self._session.scalars(
            select(SnapshotRow)
            .where(SnapshotRow.original_source_managed_instance_id == str(instance_id))
            .order_by(SnapshotRow.id)
        )
        return [_snapshot_from_row(row) for row in rows]

    def detach_for_deleted_source(
        self, instance_id: UUID, *, source_name: str, deleted_at: datetime
    ) -> int:
        result = self._session.execute(
            update(SnapshotRow)
            .where(
                SnapshotRow.source_managed_instance_id == str(instance_id),
                SnapshotRow.source_binding == SnapshotSourceBinding.ATTACHED,
            )
            .values(
                source_managed_instance_id=None,
                source_binding=SnapshotSourceBinding.DELETED_SOURCE,
                source_instance_name=source_name,
                source_deleted_at=_timestamp_to_text(deleted_at),
            )
        )
        return int(getattr(result, "rowcount", 0))

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

    def mark_deleting(self, snapshot_id: UUID, *, prior_state: SnapshotState) -> bool:
        result = self._session.execute(
            update(SnapshotRow)
            .where(SnapshotRow.id == str(snapshot_id), SnapshotRow.state == prior_state)
            .values(state=SnapshotState.DELETING)
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def delete_deleting(self, snapshot_id: UUID) -> bool:
        result = self._session.execute(
            delete(SnapshotRow).where(
                SnapshotRow.id == str(snapshot_id),
                SnapshotRow.state == SnapshotState.DELETING,
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

    def list_for_instance(self, instance_id: UUID) -> list[SnapshotCreateOperation]:
        rows = self._session.scalars(
            select(SnapshotCreateOperationRow)
            .where(SnapshotCreateOperationRow.managed_instance_id == str(instance_id))
            .order_by(
                SnapshotCreateOperationRow.requested_at.desc(),
                SnapshotCreateOperationRow.id.desc(),
            )
        )
        return [_snapshot_operation_from_row(row) for row in rows]

    def reconciliation_for_instance(
        self, instance_id: UUID, *, limit: int = 2
    ) -> list[SnapshotCreateOperation]:
        rows = self._session.scalars(
            select(SnapshotCreateOperationRow)
            .where(
                SnapshotCreateOperationRow.managed_instance_id == str(instance_id),
                SnapshotCreateOperationRow.reconciliation_required.is_(True),
            )
            .order_by(
                SnapshotCreateOperationRow.requested_at.desc(),
                SnapshotCreateOperationRow.id.desc(),
            )
            .limit(limit)
        )
        return [_snapshot_operation_from_row(row) for row in rows]

    def reconciliation_for_snapshot(
        self, snapshot_id: UUID, *, limit: int = 2
    ) -> list[SnapshotCreateOperation]:
        rows = self._session.scalars(
            select(SnapshotCreateOperationRow)
            .where(
                SnapshotCreateOperationRow.snapshot_id == str(snapshot_id),
                SnapshotCreateOperationRow.reconciliation_required.is_(True),
            )
            .order_by(
                SnapshotCreateOperationRow.requested_at.desc(),
                SnapshotCreateOperationRow.id.desc(),
            )
            .limit(limit)
        )
        return [_snapshot_operation_from_row(row) for row in rows]

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

    def mark_stale_reconciliation(
        self, operation_id: UUID, *, token_digest: str, now: datetime
    ) -> bool:
        result = self._session.execute(
            update(SnapshotCreateOperationRow)
            .where(
                SnapshotCreateOperationRow.id == str(operation_id),
                SnapshotCreateOperationRow.state
                == SnapshotCreateOperationState.RUNNING,
                SnapshotCreateOperationRow.lease_token_digest == token_digest,
                SnapshotCreateOperationRow.lease_expires_at <= _timestamp_to_text(now),
            )
            .values(
                state=SnapshotCreateOperationState.RECONCILIATION_REQUIRED,
                heartbeat_at=_timestamp_to_text(now),
                completed_at=_timestamp_to_text(now),
                failure_reason=func.coalesce(
                    SnapshotCreateOperationRow.failure_reason,
                    "Snapshot capture requires operator reconciliation.",
                ),
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
                    SnapshotCreateOperationRow.state.in_(
                        (
                            SnapshotCreateOperationState.SUCCEEDED,
                            SnapshotCreateOperationState.FAILED,
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


class SnapshotRestoreOperationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: SnapshotRestoreOperation) -> None:
        self._session.add(
            SnapshotRestoreOperationRow(
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

    def get(self, operation_id: UUID) -> SnapshotRestoreOperation | None:
        row = self._session.get(SnapshotRestoreOperationRow, str(operation_id))
        return None if row is None else _snapshot_restore_operation_from_row(row)

    def reconciliation_for_instance(
        self, instance_id: UUID, *, limit: int = 2
    ) -> list[SnapshotRestoreOperation]:
        rows = self._session.scalars(
            select(SnapshotRestoreOperationRow)
            .where(
                SnapshotRestoreOperationRow.managed_instance_id == str(instance_id),
                SnapshotRestoreOperationRow.reconciliation_required.is_(True),
            )
            .order_by(
                SnapshotRestoreOperationRow.requested_at.desc(),
                SnapshotRestoreOperationRow.id.desc(),
            )
            .limit(limit)
        )
        return [_snapshot_restore_operation_from_row(row) for row in rows]

    def active_for_instance(self, instance_id: UUID) -> SnapshotRestoreOperation | None:
        row = self._session.scalar(
            select(SnapshotRestoreOperationRow)
            .where(
                SnapshotRestoreOperationRow.managed_instance_id == str(instance_id),
                SnapshotRestoreOperationRow.state.in_(
                    (
                        SnapshotRestoreOperationState.QUEUED,
                        SnapshotRestoreOperationState.RUNNING,
                    )
                ),
            )
            .order_by(SnapshotRestoreOperationRow.requested_at.desc())
            .limit(1)
        )
        return None if row is None else _snapshot_restore_operation_from_row(row)

    def latest_for_instance(self, instance_id: UUID) -> SnapshotRestoreOperation | None:
        row = self._session.scalar(
            select(SnapshotRestoreOperationRow)
            .where(SnapshotRestoreOperationRow.managed_instance_id == str(instance_id))
            .order_by(
                SnapshotRestoreOperationRow.requested_at.desc(),
                SnapshotRestoreOperationRow.id.desc(),
            )
            .limit(1)
        )
        return None if row is None else _snapshot_restore_operation_from_row(row)

    def list_for_instance(self, instance_id: UUID) -> list[SnapshotRestoreOperation]:
        rows = self._session.scalars(
            select(SnapshotRestoreOperationRow)
            .where(SnapshotRestoreOperationRow.managed_instance_id == str(instance_id))
            .order_by(
                SnapshotRestoreOperationRow.requested_at.desc(),
                SnapshotRestoreOperationRow.id.desc(),
            )
        )
        return [_snapshot_restore_operation_from_row(row) for row in rows]

    def list_queued(self) -> list[SnapshotRestoreOperation]:
        rows = self._session.scalars(
            select(SnapshotRestoreOperationRow)
            .where(
                SnapshotRestoreOperationRow.state
                == SnapshotRestoreOperationState.QUEUED
            )
            .order_by(SnapshotRestoreOperationRow.requested_at)
        )
        return [_snapshot_restore_operation_from_row(row) for row in rows]

    def acquire(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(SnapshotRestoreOperationRow)
            .where(
                SnapshotRestoreOperationRow.id == str(operation_id),
                SnapshotRestoreOperationRow.state
                == SnapshotRestoreOperationState.QUEUED,
            )
            .values(
                state=SnapshotRestoreOperationState.RUNNING,
                phase=SnapshotRestoreOperationPhase.ACQUIRING_LEASES,
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
        phase: SnapshotRestoreOperationPhase,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(SnapshotRestoreOperationRow)
            .where(
                SnapshotRestoreOperationRow.id == str(operation_id),
                SnapshotRestoreOperationRow.state
                == SnapshotRestoreOperationState.RUNNING,
                SnapshotRestoreOperationRow.lease_token_digest == token_digest,
                SnapshotRestoreOperationRow.lease_expires_at > _timestamp_to_text(now),
            )
            .values(
                phase=phase,
                heartbeat_at=_timestamp_to_text(now),
                lease_expires_at=_timestamp_to_text(lease_expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def set_prior_state(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        state: InstanceRuntimeState,
    ) -> bool:
        result = self._session.execute(
            update(SnapshotRestoreOperationRow)
            .where(
                SnapshotRestoreOperationRow.id == str(operation_id),
                SnapshotRestoreOperationRow.state
                == SnapshotRestoreOperationState.RUNNING,
                SnapshotRestoreOperationRow.lease_token_digest == token_digest,
                SnapshotRestoreOperationRow.prior_source_runtime_state.is_(None),
            )
            .values(prior_source_runtime_state=state)
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def set_container_identity(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        old_container_id: str | None = None,
        fresh_container_id: str | None = None,
    ) -> bool:
        values: dict[str, str] = {}
        conditions = [
            SnapshotRestoreOperationRow.id == str(operation_id),
            SnapshotRestoreOperationRow.state == SnapshotRestoreOperationState.RUNNING,
            SnapshotRestoreOperationRow.lease_token_digest == token_digest,
        ]
        if old_container_id is not None:
            values["old_container_id"] = old_container_id
            conditions.append(SnapshotRestoreOperationRow.old_container_id.is_(None))
        if fresh_container_id is not None:
            values["fresh_container_id"] = fresh_container_id
            conditions.append(SnapshotRestoreOperationRow.fresh_container_id.is_(None))
        if not values:
            return False
        result = self._session.execute(
            update(SnapshotRestoreOperationRow).where(*conditions).values(**values)
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def finish(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        state: SnapshotRestoreOperationState,
        phase: SnapshotRestoreOperationPhase,
        failure_reason: str | None,
        reconciliation_required: bool,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(SnapshotRestoreOperationRow)
            .where(
                SnapshotRestoreOperationRow.id == str(operation_id),
                SnapshotRestoreOperationRow.state
                == SnapshotRestoreOperationState.RUNNING,
                SnapshotRestoreOperationRow.lease_token_digest == token_digest,
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

    def mark_stale_reconciliation(
        self, operation_id: UUID, *, token_digest: str, now: datetime
    ) -> bool:
        result = self._session.execute(
            update(SnapshotRestoreOperationRow)
            .where(
                SnapshotRestoreOperationRow.id == str(operation_id),
                SnapshotRestoreOperationRow.state
                == SnapshotRestoreOperationState.RUNNING,
                SnapshotRestoreOperationRow.lease_token_digest == token_digest,
                SnapshotRestoreOperationRow.lease_expires_at <= _timestamp_to_text(now),
            )
            .values(
                state=SnapshotRestoreOperationState.RECONCILIATION_REQUIRED,
                heartbeat_at=_timestamp_to_text(now),
                completed_at=_timestamp_to_text(now),
                failure_reason=func.coalesce(
                    SnapshotRestoreOperationRow.failure_reason,
                    "Snapshot restore requires operator reconciliation.",
                ),
                reconciliation_required=True,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def prune_terminal(self, instance_id: UUID, *, retain: int = 20) -> int:
        rows = list(
            self._session.scalars(
                select(SnapshotRestoreOperationRow.id)
                .where(
                    SnapshotRestoreOperationRow.managed_instance_id == str(instance_id),
                    SnapshotRestoreOperationRow.state.in_(
                        (
                            SnapshotRestoreOperationState.SUCCEEDED,
                            SnapshotRestoreOperationState.FAILED,
                        )
                    ),
                )
                .order_by(
                    SnapshotRestoreOperationRow.requested_at.desc(),
                    SnapshotRestoreOperationRow.id.desc(),
                )
                .offset(retain)
            )
        )
        if not rows:
            return 0
        result = self._session.execute(
            delete(SnapshotRestoreOperationRow).where(
                SnapshotRestoreOperationRow.id.in_(rows)
            )
        )
        return int(getattr(result, "rowcount", 0))


class SnapshotDeleteOperationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: SnapshotDeleteOperation) -> None:
        self._session.add(
            SnapshotDeleteOperationRow(
                id=str(operation.id),
                snapshot_id=str(operation.snapshot_id),
                managed_instance_id=str(operation.managed_instance_id),
                prior_snapshot_state=operation.prior_snapshot_state,
                requested_at=_timestamp_to_text(operation.requested_at),
                state=operation.state,
                phase=operation.phase,
                reconciliation_required=False,
            )
        )
        self._session.flush()

    def get(self, operation_id: UUID) -> SnapshotDeleteOperation | None:
        row = self._session.get(SnapshotDeleteOperationRow, str(operation_id))
        return None if row is None else _snapshot_delete_operation_from_row(row)

    def list_for_instance(self, instance_id: UUID) -> list[SnapshotDeleteOperation]:
        rows = self._session.scalars(
            select(SnapshotDeleteOperationRow)
            .where(SnapshotDeleteOperationRow.managed_instance_id == str(instance_id))
            .order_by(
                SnapshotDeleteOperationRow.requested_at.desc(),
                SnapshotDeleteOperationRow.id.desc(),
            )
        )
        return [_snapshot_delete_operation_from_row(row) for row in rows]

    def reconciliation_for_snapshot(
        self, snapshot_id: UUID, *, limit: int = 2
    ) -> list[SnapshotDeleteOperation]:
        rows = self._session.scalars(
            select(SnapshotDeleteOperationRow)
            .where(
                SnapshotDeleteOperationRow.snapshot_id == str(snapshot_id),
                SnapshotDeleteOperationRow.reconciliation_required.is_(True),
            )
            .order_by(
                SnapshotDeleteOperationRow.requested_at.desc(),
                SnapshotDeleteOperationRow.id.desc(),
            )
            .limit(limit)
        )
        return [_snapshot_delete_operation_from_row(row) for row in rows]

    def active_for_snapshot(self, snapshot_id: UUID) -> SnapshotDeleteOperation | None:
        row = self._session.scalar(
            select(SnapshotDeleteOperationRow)
            .where(
                SnapshotDeleteOperationRow.snapshot_id == str(snapshot_id),
                SnapshotDeleteOperationRow.state.in_(
                    (
                        SnapshotDeleteOperationState.QUEUED,
                        SnapshotDeleteOperationState.RUNNING,
                    )
                ),
            )
            .order_by(SnapshotDeleteOperationRow.requested_at.desc())
            .limit(1)
        )
        return None if row is None else _snapshot_delete_operation_from_row(row)

    def list_queued(self) -> list[SnapshotDeleteOperation]:
        rows = self._session.scalars(
            select(SnapshotDeleteOperationRow)
            .where(
                SnapshotDeleteOperationRow.state == SnapshotDeleteOperationState.QUEUED
            )
            .order_by(SnapshotDeleteOperationRow.requested_at)
        )
        return [_snapshot_delete_operation_from_row(row) for row in rows]

    def acquire(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(SnapshotDeleteOperationRow)
            .where(
                SnapshotDeleteOperationRow.id == str(operation_id),
                SnapshotDeleteOperationRow.state == SnapshotDeleteOperationState.QUEUED,
            )
            .values(
                state=SnapshotDeleteOperationState.RUNNING,
                phase=SnapshotDeleteOperationPhase.ACQUIRING_HEAVY_LEASE,
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
        phase: SnapshotDeleteOperationPhase,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(SnapshotDeleteOperationRow)
            .where(
                SnapshotDeleteOperationRow.id == str(operation_id),
                SnapshotDeleteOperationRow.state
                == SnapshotDeleteOperationState.RUNNING,
                SnapshotDeleteOperationRow.lease_token_digest == token_digest,
                SnapshotDeleteOperationRow.lease_expires_at > _timestamp_to_text(now),
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
        state: SnapshotDeleteOperationState,
        phase: SnapshotDeleteOperationPhase,
        failure_reason: str | None,
        reconciliation_required: bool,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(SnapshotDeleteOperationRow)
            .where(
                SnapshotDeleteOperationRow.id == str(operation_id),
                SnapshotDeleteOperationRow.state
                == SnapshotDeleteOperationState.RUNNING,
                SnapshotDeleteOperationRow.lease_token_digest == token_digest,
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

    def mark_stale_reconciliation(
        self, operation_id: UUID, *, token_digest: str, now: datetime
    ) -> bool:
        result = self._session.execute(
            update(SnapshotDeleteOperationRow)
            .where(
                SnapshotDeleteOperationRow.id == str(operation_id),
                SnapshotDeleteOperationRow.state
                == SnapshotDeleteOperationState.RUNNING,
                SnapshotDeleteOperationRow.lease_token_digest == token_digest,
                SnapshotDeleteOperationRow.lease_expires_at <= _timestamp_to_text(now),
            )
            .values(
                state=SnapshotDeleteOperationState.RECONCILIATION_REQUIRED,
                heartbeat_at=_timestamp_to_text(now),
                completed_at=_timestamp_to_text(now),
                failure_reason=func.coalesce(
                    SnapshotDeleteOperationRow.failure_reason,
                    "Snapshot deletion requires operator reconciliation.",
                ),
                reconciliation_required=True,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def prune_terminal(self, instance_id: UUID, *, retain: int = 20) -> int:
        rows = list(
            self._session.scalars(
                select(SnapshotDeleteOperationRow.id)
                .where(
                    SnapshotDeleteOperationRow.managed_instance_id == str(instance_id),
                    SnapshotDeleteOperationRow.state.in_(
                        (
                            SnapshotDeleteOperationState.SUCCEEDED,
                            SnapshotDeleteOperationState.FAILED,
                        )
                    ),
                )
                .order_by(
                    SnapshotDeleteOperationRow.requested_at.desc(),
                    SnapshotDeleteOperationRow.id.desc(),
                )
                .offset(retain)
            )
        )
        if not rows:
            return 0
        result = self._session.execute(
            delete(SnapshotDeleteOperationRow).where(
                SnapshotDeleteOperationRow.id.in_(rows)
            )
        )
        return int(getattr(result, "rowcount", 0))

    def active_for_instance(self, instance_id: UUID) -> SnapshotDeleteOperation | None:
        row = self._session.scalar(
            select(SnapshotDeleteOperationRow)
            .where(
                SnapshotDeleteOperationRow.managed_instance_id == str(instance_id),
                SnapshotDeleteOperationRow.state.in_(
                    (
                        SnapshotDeleteOperationState.QUEUED,
                        SnapshotDeleteOperationState.RUNNING,
                    )
                ),
            )
            .order_by(SnapshotDeleteOperationRow.requested_at.desc())
            .limit(1)
        )
        return None if row is None else _snapshot_delete_operation_from_row(row)


class ManagedInstanceDeleteOperationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: ManagedInstanceDeleteOperation) -> None:
        self._session.add(
            ManagedInstanceDeleteOperationRow(
                id=str(operation.id),
                original_managed_instance_id=str(
                    operation.original_managed_instance_id
                ),
                source_instance_name=operation.source_instance_name,
                runtime_revision_id=str(operation.runtime_revision_id),
                fixture_definition_id=(
                    None
                    if operation.fixture_definition_id is None
                    else str(operation.fixture_definition_id)
                ),
                preserve_snapshots=operation.preserve_snapshots,
                snapshot_count=operation.snapshot_count,
                aggregate_snapshot_bytes=operation.aggregate_snapshot_bytes,
                current_snapshot_id=(
                    None
                    if operation.current_snapshot_id is None
                    else str(operation.current_snapshot_id)
                ),
                completed_snapshot_count=operation.completed_snapshot_count,
                expected_container_id=operation.expected_container_id,
                observed_container_id=operation.observed_container_id,
                prior_state=operation.prior_state,
                notes_at_deletion=operation.notes_at_deletion,
                requested_at=_timestamp_to_text(operation.requested_at),
                state=operation.state,
                phase=operation.phase,
                reconciliation_required=operation.reconciliation_required,
            )
        )
        self._session.flush()

    def get(self, operation_id: UUID) -> ManagedInstanceDeleteOperation | None:
        row = self._session.get(ManagedInstanceDeleteOperationRow, str(operation_id))
        return None if row is None else _managed_instance_delete_operation_from_row(row)

    def list_for_original_instance(
        self, instance_id: UUID
    ) -> list[ManagedInstanceDeleteOperation]:
        rows = self._session.scalars(
            select(ManagedInstanceDeleteOperationRow)
            .where(
                ManagedInstanceDeleteOperationRow.original_managed_instance_id
                == str(instance_id)
            )
            .order_by(
                ManagedInstanceDeleteOperationRow.requested_at.desc(),
                ManagedInstanceDeleteOperationRow.id.desc(),
            )
        )
        return [_managed_instance_delete_operation_from_row(row) for row in rows]

    def list_all(self) -> list[ManagedInstanceDeleteOperation]:
        rows = self._session.scalars(
            select(ManagedInstanceDeleteOperationRow).order_by(
                ManagedInstanceDeleteOperationRow.requested_at.desc(),
                ManagedInstanceDeleteOperationRow.id.desc(),
            )
        )
        return [_managed_instance_delete_operation_from_row(row) for row in rows]

    def active_for_instance(
        self, instance_id: UUID
    ) -> ManagedInstanceDeleteOperation | None:
        row = self._session.scalar(
            select(ManagedInstanceDeleteOperationRow)
            .where(
                ManagedInstanceDeleteOperationRow.original_managed_instance_id
                == str(instance_id),
                ManagedInstanceDeleteOperationRow.state.in_(
                    (
                        ManagedInstanceDeleteOperationState.QUEUED,
                        ManagedInstanceDeleteOperationState.RUNNING,
                    )
                ),
            )
            .order_by(ManagedInstanceDeleteOperationRow.requested_at.desc())
            .limit(1)
        )
        return None if row is None else _managed_instance_delete_operation_from_row(row)

    def list_queued(self) -> list[ManagedInstanceDeleteOperation]:
        rows = self._session.scalars(
            select(ManagedInstanceDeleteOperationRow)
            .where(
                ManagedInstanceDeleteOperationRow.state
                == ManagedInstanceDeleteOperationState.QUEUED
            )
            .order_by(ManagedInstanceDeleteOperationRow.requested_at)
        )
        return [_managed_instance_delete_operation_from_row(row) for row in rows]

    def list_expired_running(
        self, *, now: datetime
    ) -> list[ManagedInstanceDeleteOperation]:
        rows = self._session.scalars(
            select(ManagedInstanceDeleteOperationRow).where(
                ManagedInstanceDeleteOperationRow.state
                == ManagedInstanceDeleteOperationState.RUNNING,
                ManagedInstanceDeleteOperationRow.lease_expires_at
                <= _timestamp_to_text(now),
            )
        )
        return [_managed_instance_delete_operation_from_row(row) for row in rows]

    def reconciliation_for_instance(
        self, instance_id: UUID, *, limit: int = 2
    ) -> list[ManagedInstanceDeleteOperation]:
        rows = self._session.scalars(
            select(ManagedInstanceDeleteOperationRow)
            .where(
                ManagedInstanceDeleteOperationRow.original_managed_instance_id
                == str(instance_id),
                ManagedInstanceDeleteOperationRow.reconciliation_required.is_(True),
            )
            .order_by(ManagedInstanceDeleteOperationRow.requested_at.desc())
            .limit(limit)
        )
        return [_managed_instance_delete_operation_from_row(row) for row in rows]

    def acquire(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(ManagedInstanceDeleteOperationRow)
            .where(
                ManagedInstanceDeleteOperationRow.id == str(operation_id),
                ManagedInstanceDeleteOperationRow.state
                == ManagedInstanceDeleteOperationState.QUEUED,
            )
            .values(
                state=ManagedInstanceDeleteOperationState.RUNNING,
                phase=ManagedInstanceDeleteOperationPhase.ACQUIRING_LEASES,
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
        phase: ManagedInstanceDeleteOperationPhase,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(ManagedInstanceDeleteOperationRow)
            .where(
                ManagedInstanceDeleteOperationRow.id == str(operation_id),
                ManagedInstanceDeleteOperationRow.state
                == ManagedInstanceDeleteOperationState.RUNNING,
                ManagedInstanceDeleteOperationRow.lease_token_digest == token_digest,
                ManagedInstanceDeleteOperationRow.lease_expires_at
                > _timestamp_to_text(now),
            )
            .values(
                phase=phase,
                heartbeat_at=_timestamp_to_text(now),
                lease_expires_at=_timestamp_to_text(lease_expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def set_container_resolution(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        prior_state: ManagedInstanceDeletePriorState,
        container_id: str | None,
    ) -> bool:
        result = self._session.execute(
            update(ManagedInstanceDeleteOperationRow)
            .where(
                ManagedInstanceDeleteOperationRow.id == str(operation_id),
                ManagedInstanceDeleteOperationRow.state
                == ManagedInstanceDeleteOperationState.RUNNING,
                ManagedInstanceDeleteOperationRow.lease_token_digest == token_digest,
            )
            .values(
                prior_state=prior_state,
                expected_container_id=container_id,
                observed_container_id=container_id,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def update_progress(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        current_snapshot_id: UUID | None,
        completed_snapshot_count: int,
    ) -> bool:
        result = self._session.execute(
            update(ManagedInstanceDeleteOperationRow)
            .where(
                ManagedInstanceDeleteOperationRow.id == str(operation_id),
                ManagedInstanceDeleteOperationRow.state
                == ManagedInstanceDeleteOperationState.RUNNING,
                ManagedInstanceDeleteOperationRow.lease_token_digest == token_digest,
            )
            .values(
                current_snapshot_id=(
                    None if current_snapshot_id is None else str(current_snapshot_id)
                ),
                completed_snapshot_count=completed_snapshot_count,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def finish(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        state: ManagedInstanceDeleteOperationState,
        phase: ManagedInstanceDeleteOperationPhase,
        failure_reason: str | None,
        reconciliation_required: bool,
        notes_at_deletion: str | None,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(ManagedInstanceDeleteOperationRow)
            .where(
                ManagedInstanceDeleteOperationRow.id == str(operation_id),
                ManagedInstanceDeleteOperationRow.state
                == ManagedInstanceDeleteOperationState.RUNNING,
                ManagedInstanceDeleteOperationRow.lease_token_digest == token_digest,
            )
            .values(
                state=state,
                phase=phase,
                heartbeat_at=_timestamp_to_text(now),
                completed_at=_timestamp_to_text(now),
                failure_reason=failure_reason,
                reconciliation_required=reconciliation_required,
                notes_at_deletion=notes_at_deletion,
                current_snapshot_id=None,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def mark_stale_reconciliation(
        self, operation_id: UUID, *, token_digest: str, now: datetime
    ) -> bool:
        result = self._session.execute(
            update(ManagedInstanceDeleteOperationRow)
            .where(
                ManagedInstanceDeleteOperationRow.id == str(operation_id),
                ManagedInstanceDeleteOperationRow.state
                == ManagedInstanceDeleteOperationState.RUNNING,
                ManagedInstanceDeleteOperationRow.lease_token_digest == token_digest,
                ManagedInstanceDeleteOperationRow.lease_expires_at
                <= _timestamp_to_text(now),
            )
            .values(
                state=ManagedInstanceDeleteOperationState.RECONCILIATION_REQUIRED,
                heartbeat_at=_timestamp_to_text(now),
                completed_at=_timestamp_to_text(now),
                failure_reason=func.coalesce(
                    ManagedInstanceDeleteOperationRow.failure_reason,
                    "Instance deletion requires operator reconciliation.",
                ),
                reconciliation_required=True,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1


class ManagedInstanceLifecycleOperationRepository:
    """Transactional persistence for typed synchronous lifecycle history."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: ManagedInstanceLifecycleOperation) -> None:
        self._session.add(
            ManagedInstanceLifecycleOperationRow(
                id=str(operation.id),
                original_managed_instance_id=str(
                    operation.original_managed_instance_id
                ),
                managed_instance_id=(
                    None
                    if operation.managed_instance_id is None
                    else str(operation.managed_instance_id)
                ),
                instance_name=operation.instance_name,
                kind=operation.kind,
                requested_at=_timestamp_to_text(operation.requested_at),
                started_at=_timestamp_to_text(operation.started_at),
                completed_at=(
                    None
                    if operation.completed_at is None
                    else _timestamp_to_text(operation.completed_at)
                ),
                prior_state=operation.prior_state,
                resulting_state=operation.resulting_state,
                state=operation.state,
                failure_reason=operation.failure_reason,
                reconciliation_required=operation.reconciliation_required,
            )
        )
        self._session.flush()

    def get(self, operation_id: UUID) -> ManagedInstanceLifecycleOperation | None:
        row = self._session.get(ManagedInstanceLifecycleOperationRow, str(operation_id))
        return (
            None if row is None else _managed_instance_lifecycle_operation_from_row(row)
        )

    def list_for_original_instance(
        self, instance_id: UUID
    ) -> list[ManagedInstanceLifecycleOperation]:
        rows = self._session.scalars(
            select(ManagedInstanceLifecycleOperationRow)
            .where(
                ManagedInstanceLifecycleOperationRow.original_managed_instance_id
                == str(instance_id)
            )
            .order_by(
                ManagedInstanceLifecycleOperationRow.requested_at.desc(),
                ManagedInstanceLifecycleOperationRow.id.desc(),
            )
        )
        return [_managed_instance_lifecycle_operation_from_row(row) for row in rows]

    def reconciliation_for_instance(
        self, instance_id: UUID, *, limit: int = 2
    ) -> list[ManagedInstanceLifecycleOperation]:
        rows = self._session.scalars(
            select(ManagedInstanceLifecycleOperationRow)
            .where(
                ManagedInstanceLifecycleOperationRow.original_managed_instance_id
                == str(instance_id),
                ManagedInstanceLifecycleOperationRow.reconciliation_required.is_(True),
            )
            .order_by(
                ManagedInstanceLifecycleOperationRow.requested_at.desc(),
                ManagedInstanceLifecycleOperationRow.id.desc(),
            )
            .limit(limit)
        )
        return [_managed_instance_lifecycle_operation_from_row(row) for row in rows]

    def terminalize(
        self,
        operation_id: UUID,
        *,
        state: ManagedInstanceLifecycleOperationState,
        resulting_state: ManagedInstanceSemanticState,
        failure_reason: str | None,
        reconciliation_required: bool,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(ManagedInstanceLifecycleOperationRow)
            .where(
                ManagedInstanceLifecycleOperationRow.id == str(operation_id),
                ManagedInstanceLifecycleOperationRow.state
                == ManagedInstanceLifecycleOperationState.RUNNING,
            )
            .values(
                state=state,
                resulting_state=resulting_state,
                completed_at=_timestamp_to_text(now),
                failure_reason=failure_reason,
                reconciliation_required=reconciliation_required,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def update_prior_state(
        self,
        operation_id: UUID,
        *,
        prior_state: ManagedInstanceSemanticState,
    ) -> bool:
        result = self._session.execute(
            update(ManagedInstanceLifecycleOperationRow)
            .where(
                ManagedInstanceLifecycleOperationRow.id == str(operation_id),
                ManagedInstanceLifecycleOperationRow.state
                == ManagedInstanceLifecycleOperationState.RUNNING,
            )
            .values(prior_state=prior_state)
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def mark_stale_reconciliation(
        self,
        operation_id: UUID,
        *,
        resulting_state: ManagedInstanceSemanticState,
        now: datetime,
    ) -> bool:
        return self.terminalize(
            operation_id,
            state=ManagedInstanceLifecycleOperationState.RECONCILIATION_REQUIRED,
            resulting_state=resulting_state,
            failure_reason=(
                "Lifecycle worker lease expired; reconciliation is required."
            ),
            reconciliation_required=True,
            now=now,
        )

    def prune_terminal(self, instance_id: UUID, *, retain: int = 20) -> int:
        old_ids = list(
            self._session.scalars(
                select(ManagedInstanceLifecycleOperationRow.id)
                .where(
                    ManagedInstanceLifecycleOperationRow.original_managed_instance_id
                    == str(instance_id),
                    ManagedInstanceLifecycleOperationRow.state.in_(
                        (
                            ManagedInstanceLifecycleOperationState.SUCCEEDED,
                            ManagedInstanceLifecycleOperationState.FAILED,
                        )
                    ),
                )
                .order_by(
                    ManagedInstanceLifecycleOperationRow.requested_at.desc(),
                    ManagedInstanceLifecycleOperationRow.id.desc(),
                )
                .offset(retain)
            )
        )
        if not old_ids:
            return 0
        result = self._session.execute(
            delete(ManagedInstanceLifecycleOperationRow).where(
                ManagedInstanceLifecycleOperationRow.id.in_(old_ids)
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
                        ManagedInstanceMutationKind.FIXTURE_ASSIGNMENT,
                        ManagedInstanceMutationKind.RUNTIME_REASSIGNMENT,
                        ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ATTACHMENT,
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

    def list_current(self) -> list[ManagedInstanceMutationLeaseRow]:
        return list(self._session.scalars(select(ManagedInstanceMutationLeaseRow)))

    def release_expired_exact(
        self,
        instance_id: UUID,
        *,
        kind: ManagedInstanceMutationKind,
        operation_id: UUID,
        token_digest: str,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            delete(ManagedInstanceMutationLeaseRow).where(
                ManagedInstanceMutationLeaseRow.managed_instance_id == str(instance_id),
                ManagedInstanceMutationLeaseRow.operation_kind == kind,
                ManagedInstanceMutationLeaseRow.operation_id == str(operation_id),
                ManagedInstanceMutationLeaseRow.lease_token_digest == token_digest,
                ManagedInstanceMutationLeaseRow.lease_expires_at
                <= _timestamp_to_text(now),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1


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

    def release_expired_exact(
        self,
        *,
        kind: GlobalHeavyOperationKind,
        operation_id: UUID,
        token_digest: str,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            delete(GlobalHeavyOperationLeaseRow).where(
                GlobalHeavyOperationLeaseRow.singleton_id == 1,
                GlobalHeavyOperationLeaseRow.operation_kind == kind,
                GlobalHeavyOperationLeaseRow.operation_id == str(operation_id),
                GlobalHeavyOperationLeaseRow.lease_token_digest == token_digest,
                GlobalHeavyOperationLeaseRow.lease_expires_at
                <= _timestamp_to_text(now),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1
