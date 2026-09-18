"""Explicit repositories for targets and managed instances."""

import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from kodi_lab.domain import (
    AddonZipArtifact,
    AddonZipDependency,
    AddonZipEnablementOperation,
    AddonZipEnablementOperationPhase,
    AddonZipEnablementOperationState,
    AddonZipEnablementResult,
    AddonZipImportOperation,
    AddonZipImportOperationPhase,
    AddonZipImportOperationState,
    AddonZipInstallationConfidence,
    AddonZipInstallationEvidence,
    AddonZipInstallationOperation,
    AddonZipInstallationOperationPhase,
    AddonZipInstallationOperationState,
    AddonZipPayloadVerificationOutcome,
    DevelopmentSource,
    DevelopmentSourceAttachmentOperation,
    DevelopmentSourceDeployment,
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentOperation,
    DevelopmentSourceDeploymentOperationPhase,
    DevelopmentSourceDeploymentOperationState,
    DevelopmentSourceEnablementOperation,
    DevelopmentSourceEnablementOperationPhase,
    DevelopmentSourceEnablementOperationState,
    DevelopmentSourceEnablementResult,
    DevelopmentSourceSmokeTestExecutionResult,
    DevelopmentSourceSmokeTestOperation,
    DevelopmentSourceSmokeTestOperationPhase,
    DevelopmentSourceSmokeTestOperationState,
    DevelopmentSourceSmokeTestOverallResult,
    DevelopmentSourceSmokeTestPostProvenance,
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
    AddonZipArtifactRow,
    AddonZipEnablementOperationRow,
    AddonZipImportOperationRow,
    AddonZipInstallationEvidenceRow,
    AddonZipInstallationOperationRow,
    DevelopmentSourceAttachmentOperationRow,
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    DevelopmentSourceEnablementOperationRow,
    DevelopmentSourceRow,
    DevelopmentSourceSmokeTestOperationRow,
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


def _addon_zip_dependencies_to_json(values: tuple[AddonZipDependency, ...]) -> str:
    return json.dumps(
        [
            {
                "id": item.dependency_id,
                "minversion": item.minversion,
                "optional": item.optional,
                "version": item.version,
            }
            for item in values
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _addon_zip_dependencies_from_json(value: str) -> tuple[AddonZipDependency, ...]:
    raw = json.loads(value)
    if not isinstance(raw, list) or len(raw) > 256:
        raise ValueError("stored ZIP dependencies are invalid")
    result: list[AddonZipDependency] = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "version", "minversion", "optional"}
            or not isinstance(item["id"], str)
            or (item["version"] is not None and not isinstance(item["version"], str))
            or (
                item["minversion"] is not None
                and not isinstance(item["minversion"], str)
            )
            or not isinstance(item["optional"], bool)
        ):
            raise ValueError("stored ZIP dependencies are invalid")
        result.append(
            AddonZipDependency(
                dependency_id=item["id"],
                version=item["version"],
                minversion=item["minversion"],
                optional=item["optional"],
            )
        )
    return tuple(result)


def _addon_zip_artifact_from_row(row: AddonZipArtifactRow) -> AddonZipArtifact:
    return AddonZipArtifact(
        id=UUID(row.id),
        logical_label=row.logical_label,
        sha256_digest=row.sha256_digest,
        compressed_size=row.compressed_size,
        total_uncompressed_bytes=row.total_uncompressed_bytes,
        member_count=row.member_count,
        addon_id=row.addon_id,
        addon_name=row.addon_name,
        addon_version=row.addon_version,
        extension_classification=row.extension_classification,
        dependencies=_addon_zip_dependencies_from_json(row.dependencies_json),
        payload_manifest_digest=row.payload_manifest_digest,
        imported_at=_timestamp_from_text(row.imported_at),
        origin=row.origin,
        validation_state=row.validation_state,
    )


def _addon_zip_import_from_row(
    row: AddonZipImportOperationRow,
) -> AddonZipImportOperation:
    return AddonZipImportOperation(
        id=UUID(row.id),
        artifact_id=UUID(row.artifact_id),
        logical_label=row.logical_label,
        requested_at=_timestamp_from_text(row.requested_at),
        state=row.state,
        phase=row.phase,
        started_at=None
        if row.started_at is None
        else _timestamp_from_text(row.started_at),
        completed_at=None
        if row.completed_at is None
        else _timestamp_from_text(row.completed_at),
        failure_reason=row.failure_reason,
        lease_token_digest=row.lease_token_digest,
        lease_expires_at=None
        if row.lease_expires_at is None
        else _timestamp_from_text(row.lease_expires_at),
    )


def _addon_zip_installation_from_row(
    row: AddonZipInstallationOperationRow,
) -> AddonZipInstallationOperation:
    return AddonZipInstallationOperation(
        id=UUID(row.id),
        original_managed_instance_id=UUID(row.original_managed_instance_id),
        managed_instance_id=None
        if row.managed_instance_id is None
        else UUID(row.managed_instance_id),
        artifact_id=UUID(row.artifact_id),
        addon_id=row.addon_id,
        addon_version=row.addon_version,
        runtime_revision_id=UUID(row.runtime_revision_id),
        requested_at=_timestamp_from_text(row.requested_at),
        state=row.state,
        phase=row.phase,
        expected_container_id=row.expected_container_id,
        application_started=row.application_started,
        stopped_readback_matched=row.stopped_readback_matched,
        final_readback_matched=row.final_readback_matched,
        recognition_enabled=row.recognition_enabled,
        dependency_baseline_json=row.dependency_baseline_json,
        dependency_post_json=row.dependency_post_json,
        started_at=None
        if row.started_at is None
        else _timestamp_from_text(row.started_at),
        heartbeat_at=None
        if row.heartbeat_at is None
        else _timestamp_from_text(row.heartbeat_at),
        completed_at=None
        if row.completed_at is None
        else _timestamp_from_text(row.completed_at),
        failure_reason=row.failure_reason,
        reconciliation_required=row.reconciliation_required,
        lease_token_digest=row.lease_token_digest,
        lease_expires_at=None
        if row.lease_expires_at is None
        else _timestamp_from_text(row.lease_expires_at),
    )


def _addon_zip_evidence_from_row(
    row: AddonZipInstallationEvidenceRow,
) -> AddonZipInstallationEvidence:
    return AddonZipInstallationEvidence(
        managed_instance_id=UUID(row.managed_instance_id),
        original_managed_instance_id=UUID(row.original_managed_instance_id),
        artifact_id=UUID(row.artifact_id),
        installation_operation_id=UUID(row.installation_operation_id),
        runtime_revision_id=UUID(row.runtime_revision_id),
        trusted_container_id=row.trusted_container_id,
        addon_id=row.addon_id,
        addon_version=row.addon_version,
        payload_manifest_digest=row.payload_manifest_digest,
        verified_at=_timestamp_from_text(row.verified_at),
        confidence=row.confidence,
        result=row.result,
    )


def _addon_zip_enablement_from_row(
    row: AddonZipEnablementOperationRow,
) -> AddonZipEnablementOperation:
    return AddonZipEnablementOperation(
        id=UUID(row.id),
        original_managed_instance_id=UUID(row.original_managed_instance_id),
        managed_instance_id=(
            None if row.managed_instance_id is None else UUID(row.managed_instance_id)
        ),
        artifact_id=UUID(row.artifact_id),
        installation_operation_id=UUID(row.installation_operation_id),
        addon_id=row.addon_id,
        addon_version=row.addon_version,
        runtime_revision_id=UUID(row.runtime_revision_id),
        expected_container_id=row.expected_container_id,
        requested_at=_timestamp_from_text(row.requested_at),
        started_at=_timestamp_from_text(row.started_at),
        state=row.state,
        phase=row.phase,
        result_classification=row.result_classification,
        payload_verification=row.payload_verification,
        prior_enabled=row.prior_enabled,
        result_enabled=row.result_enabled,
        enable_performed=row.enable_performed,
        blocked_dependency_id=row.blocked_dependency_id,
        blocked_dependency_reason=row.blocked_dependency_reason,
        completed_at=(
            None if row.completed_at is None else _timestamp_from_text(row.completed_at)
        ),
        failure_reason=row.failure_reason,
        reconciliation_required=row.reconciliation_required,
    )


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


def _development_source_deployment_from_row(
    row: DevelopmentSourceDeploymentRow,
) -> DevelopmentSourceDeployment:
    return DevelopmentSourceDeployment(
        managed_instance_id=UUID(row.managed_instance_id),
        development_source_id=UUID(row.development_source_id),
        addon_id=row.addon_id,
        mode=row.mode,
        last_successful_operation_id=UUID(row.last_successful_operation_id),
        source_manifest_digest=row.source_manifest_digest,
        verified_destination_digest=row.verified_destination_digest,
        runtime_revision_id=UUID(row.runtime_revision_id),
        trusted_container_id=row.trusted_container_id,
        observed_source_name=row.observed_source_name,
        observed_source_version=row.observed_source_version,
        verified_at=_timestamp_from_text(row.verified_at),
        confidence=row.confidence,
    )


def _development_source_deployment_operation_from_row(
    row: DevelopmentSourceDeploymentOperationRow,
) -> DevelopmentSourceDeploymentOperation:
    return DevelopmentSourceDeploymentOperation(
        id=UUID(row.id),
        original_managed_instance_id=UUID(row.original_managed_instance_id),
        managed_instance_id=(
            None if row.managed_instance_id is None else UUID(row.managed_instance_id)
        ),
        development_source_id=UUID(row.development_source_id),
        source_logical_name=row.source_logical_name,
        addon_id=row.addon_id,
        runtime_revision_id=UUID(row.runtime_revision_id),
        request_kind=row.request_kind,
        requested_at=_timestamp_from_text(row.requested_at),
        state=row.state,
        phase=row.phase,
        result_classification=row.result_classification,
        new_count=row.new_count,
        changed_count=row.changed_count,
        unchanged_count=row.unchanged_count,
        destination_only_count=row.destination_only_count,
        transferred_bytes=row.transferred_bytes,
        expected_container_id=row.expected_container_id,
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


def _development_source_enablement_operation_from_row(
    row: DevelopmentSourceEnablementOperationRow,
) -> DevelopmentSourceEnablementOperation:
    return DevelopmentSourceEnablementOperation(
        id=UUID(row.id),
        original_managed_instance_id=UUID(row.original_managed_instance_id),
        managed_instance_id=(
            None if row.managed_instance_id is None else UUID(row.managed_instance_id)
        ),
        development_source_id=UUID(row.development_source_id),
        source_logical_name=row.source_logical_name,
        addon_id=row.addon_id,
        addon_version=row.addon_version,
        runtime_revision_id=UUID(row.runtime_revision_id),
        requested_at=_timestamp_from_text(row.requested_at),
        started_at=_timestamp_from_text(row.started_at),
        state=row.state,
        phase=row.phase,
        result_classification=row.result_classification,
        prior_enabled=row.prior_enabled,
        result_enabled=row.result_enabled,
        enable_performed=row.enable_performed,
        blocked_dependency_id=row.blocked_dependency_id,
        blocked_dependency_reason=row.blocked_dependency_reason,
        completed_at=(
            None if row.completed_at is None else _timestamp_from_text(row.completed_at)
        ),
        failure_reason=row.failure_reason,
        reconciliation_required=row.reconciliation_required,
    )


def _development_source_smoke_test_operation_from_row(
    row: DevelopmentSourceSmokeTestOperationRow,
) -> DevelopmentSourceSmokeTestOperation:
    return DevelopmentSourceSmokeTestOperation(
        id=UUID(row.id),
        original_managed_instance_id=UUID(row.original_managed_instance_id),
        managed_instance_id=(
            None if row.managed_instance_id is None else UUID(row.managed_instance_id)
        ),
        development_source_id=UUID(row.development_source_id),
        source_logical_name=row.source_logical_name,
        addon_id=row.addon_id,
        addon_version=row.addon_version,
        runtime_revision_id=UUID(row.runtime_revision_id),
        requested_at=_timestamp_from_text(row.requested_at),
        state=row.state,
        phase=row.phase,
        overall_result=row.overall_result,
        execution_result=row.execution_result,
        post_execution_provenance=row.post_execution_provenance,
        invocation_dispatched=row.invocation_dispatched,
        invocation_duration_ms=row.invocation_duration_ms,
        returned_item_count=row.returned_item_count,
        blocked_dependency_id=row.blocked_dependency_id,
        blocked_dependency_reason=row.blocked_dependency_reason,
        expected_container_id=row.expected_container_id,
        started_at=(
            None if row.started_at is None else _timestamp_from_text(row.started_at)
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


class AddonZipArtifactRepository:
    """Create/read-only repository for accepted immutable ZIP artifacts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, artifact: AddonZipArtifact) -> None:
        self._session.add(
            AddonZipArtifactRow(
                id=str(artifact.id),
                logical_label=artifact.logical_label,
                sha256_digest=artifact.sha256_digest,
                compressed_size=artifact.compressed_size,
                total_uncompressed_bytes=artifact.total_uncompressed_bytes,
                member_count=artifact.member_count,
                addon_id=artifact.addon_id,
                addon_name=artifact.addon_name,
                addon_version=artifact.addon_version,
                extension_classification=artifact.extension_classification,
                dependencies_json=_addon_zip_dependencies_to_json(
                    artifact.dependencies
                ),
                payload_manifest_digest=artifact.payload_manifest_digest,
                imported_at=_timestamp_to_text(artifact.imported_at),
                origin=artifact.origin,
                validation_state=artifact.validation_state,
            )
        )

    def get(self, artifact_id: UUID) -> AddonZipArtifact | None:
        row = self._session.get(AddonZipArtifactRow, str(artifact_id))
        return None if row is None else _addon_zip_artifact_from_row(row)

    def list(self) -> list[AddonZipArtifact]:
        rows = self._session.scalars(
            select(AddonZipArtifactRow).order_by(
                AddonZipArtifactRow.imported_at.desc(), AddonZipArtifactRow.id.desc()
            )
        )
        return [_addon_zip_artifact_from_row(row) for row in rows]


class AddonZipImportOperationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: AddonZipImportOperation) -> None:
        self._session.add(
            AddonZipImportOperationRow(
                id=str(operation.id),
                artifact_id=str(operation.artifact_id),
                logical_label=operation.logical_label,
                requested_at=_timestamp_to_text(operation.requested_at),
                state=operation.state,
                phase=operation.phase,
                started_at=None
                if operation.started_at is None
                else _timestamp_to_text(operation.started_at),
                completed_at=None
                if operation.completed_at is None
                else _timestamp_to_text(operation.completed_at),
                failure_reason=operation.failure_reason,
                lease_token_digest=operation.lease_token_digest,
                lease_expires_at=None
                if operation.lease_expires_at is None
                else _timestamp_to_text(operation.lease_expires_at),
            )
        )

    def get(self, operation_id: UUID) -> AddonZipImportOperation | None:
        row = self._session.get(AddonZipImportOperationRow, str(operation_id))
        return None if row is None else _addon_zip_import_from_row(row)

    def list_queued(self) -> list[AddonZipImportOperation]:
        rows = self._session.scalars(
            select(AddonZipImportOperationRow)
            .where(
                AddonZipImportOperationRow.state == AddonZipImportOperationState.QUEUED
            )
            .order_by(
                AddonZipImportOperationRow.requested_at,
                AddonZipImportOperationRow.id,
            )
        )
        return [_addon_zip_import_from_row(row) for row in rows]

    def acquire(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(AddonZipImportOperationRow)
            .where(
                AddonZipImportOperationRow.id == str(operation_id),
                AddonZipImportOperationRow.state == AddonZipImportOperationState.QUEUED,
                AddonZipImportOperationRow.lease_token_digest.is_(None),
            )
            .values(
                state=AddonZipImportOperationState.RUNNING,
                phase=AddonZipImportOperationPhase.VALIDATING,
                started_at=_timestamp_to_text(now),
                lease_token_digest=token_digest,
                lease_expires_at=_timestamp_to_text(expires_at),
            )
        )
        return bool(getattr(result, "rowcount", 0))

    def update_phase(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        phase: AddonZipImportOperationPhase,
    ) -> bool:
        result = self._session.execute(
            update(AddonZipImportOperationRow)
            .where(
                AddonZipImportOperationRow.id == str(operation_id),
                AddonZipImportOperationRow.state
                == AddonZipImportOperationState.RUNNING,
                AddonZipImportOperationRow.lease_token_digest == token_digest,
            )
            .values(phase=phase)
        )
        return bool(getattr(result, "rowcount", 0))

    def finish(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        succeeded: bool,
        now: datetime,
        reason: str | None,
    ) -> bool:
        result = self._session.execute(
            update(AddonZipImportOperationRow)
            .where(
                AddonZipImportOperationRow.id == str(operation_id),
                AddonZipImportOperationRow.state
                == AddonZipImportOperationState.RUNNING,
                AddonZipImportOperationRow.lease_token_digest == token_digest,
            )
            .values(
                state=AddonZipImportOperationState.SUCCEEDED
                if succeeded
                else AddonZipImportOperationState.FAILED,
                phase=AddonZipImportOperationPhase.FINALIZE,
                completed_at=_timestamp_to_text(now),
                failure_reason=reason,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        return bool(getattr(result, "rowcount", 0))

    def mark_stale_failed(
        self, operation_id: UUID, *, token_digest: str, now: datetime
    ) -> bool:
        result = self._session.execute(
            update(AddonZipImportOperationRow)
            .where(
                AddonZipImportOperationRow.id == str(operation_id),
                AddonZipImportOperationRow.state
                == AddonZipImportOperationState.RUNNING,
                AddonZipImportOperationRow.lease_token_digest == token_digest,
            )
            .values(
                state=AddonZipImportOperationState.FAILED,
                phase=AddonZipImportOperationPhase.FINALIZE,
                completed_at=_timestamp_to_text(now),
                failure_reason="Uploaded ZIP validation authority expired.",
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        return bool(getattr(result, "rowcount", 0))


class AddonZipInstallationEvidenceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self, instance_id: UUID, addon_id: str
    ) -> AddonZipInstallationEvidence | None:
        row = self._session.get(
            AddonZipInstallationEvidenceRow, (str(instance_id), addon_id)
        )
        return None if row is None else _addon_zip_evidence_from_row(row)

    def list_for_instance(
        self, instance_id: UUID
    ) -> list[AddonZipInstallationEvidence]:
        rows = self._session.scalars(
            select(AddonZipInstallationEvidenceRow)
            .where(
                AddonZipInstallationEvidenceRow.managed_instance_id == str(instance_id)
            )
            .order_by(AddonZipInstallationEvidenceRow.addon_id)
        )
        return [_addon_zip_evidence_from_row(row) for row in rows]

    def upsert(self, evidence: AddonZipInstallationEvidence) -> None:
        row = self._session.get(
            AddonZipInstallationEvidenceRow,
            (str(evidence.managed_instance_id), evidence.addon_id),
        )
        values = dict(
            original_managed_instance_id=str(evidence.original_managed_instance_id),
            artifact_id=str(evidence.artifact_id),
            installation_operation_id=str(evidence.installation_operation_id),
            runtime_revision_id=str(evidence.runtime_revision_id),
            trusted_container_id=evidence.trusted_container_id,
            addon_id=evidence.addon_id,
            addon_version=evidence.addon_version,
            payload_manifest_digest=evidence.payload_manifest_digest,
            verified_at=_timestamp_to_text(evidence.verified_at),
            confidence=evidence.confidence,
            result=evidence.result,
        )
        if row is None:
            self._session.add(
                AddonZipInstallationEvidenceRow(
                    managed_instance_id=str(evidence.managed_instance_id), **values
                )
            )
        else:
            for field, value in values.items():
                setattr(row, field, value)

    def mark_confidence(
        self, instance_id: UUID, confidence: AddonZipInstallationConfidence
    ) -> int:
        result = self._session.execute(
            update(AddonZipInstallationEvidenceRow)
            .where(
                AddonZipInstallationEvidenceRow.managed_instance_id == str(instance_id)
            )
            .values(confidence=confidence)
        )
        return int(getattr(result, "rowcount", 0))

    def mark_artifact_confidence(
        self,
        instance_id: UUID,
        artifact_id: UUID,
        confidence: AddonZipInstallationConfidence,
    ) -> int:
        result = self._session.execute(
            update(AddonZipInstallationEvidenceRow)
            .where(
                AddonZipInstallationEvidenceRow.managed_instance_id == str(instance_id),
                AddonZipInstallationEvidenceRow.artifact_id == str(artifact_id),
            )
            .values(confidence=confidence)
        )
        return int(getattr(result, "rowcount", 0))

    def delete_for_instance(self, instance_id: UUID) -> int:
        result = self._session.execute(
            delete(AddonZipInstallationEvidenceRow).where(
                AddonZipInstallationEvidenceRow.managed_instance_id == str(instance_id)
            )
        )
        return int(getattr(result, "rowcount", 0))


class AddonZipInstallationOperationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: AddonZipInstallationOperation) -> None:
        self._session.add(
            AddonZipInstallationOperationRow(
                id=str(operation.id),
                original_managed_instance_id=str(
                    operation.original_managed_instance_id
                ),
                managed_instance_id=None
                if operation.managed_instance_id is None
                else str(operation.managed_instance_id),
                artifact_id=str(operation.artifact_id),
                addon_id=operation.addon_id,
                addon_version=operation.addon_version,
                runtime_revision_id=str(operation.runtime_revision_id),
                requested_at=_timestamp_to_text(operation.requested_at),
                state=operation.state,
                phase=operation.phase,
                expected_container_id=operation.expected_container_id,
                application_started=operation.application_started,
                stopped_readback_matched=operation.stopped_readback_matched,
                final_readback_matched=operation.final_readback_matched,
                recognition_enabled=operation.recognition_enabled,
                dependency_baseline_json=operation.dependency_baseline_json,
                dependency_post_json=operation.dependency_post_json,
                started_at=None
                if operation.started_at is None
                else _timestamp_to_text(operation.started_at),
                heartbeat_at=None
                if operation.heartbeat_at is None
                else _timestamp_to_text(operation.heartbeat_at),
                completed_at=None
                if operation.completed_at is None
                else _timestamp_to_text(operation.completed_at),
                failure_reason=operation.failure_reason,
                reconciliation_required=operation.reconciliation_required,
                lease_token_digest=operation.lease_token_digest,
                lease_expires_at=None
                if operation.lease_expires_at is None
                else _timestamp_to_text(operation.lease_expires_at),
            )
        )

    def get(self, operation_id: UUID) -> AddonZipInstallationOperation | None:
        row = self._session.get(AddonZipInstallationOperationRow, str(operation_id))
        return None if row is None else _addon_zip_installation_from_row(row)

    def list_for_original_instance(
        self, instance_id: UUID
    ) -> list[AddonZipInstallationOperation]:
        rows = self._session.scalars(
            select(AddonZipInstallationOperationRow)
            .where(
                AddonZipInstallationOperationRow.original_managed_instance_id
                == str(instance_id)
            )
            .order_by(
                AddonZipInstallationOperationRow.requested_at.desc(),
                AddonZipInstallationOperationRow.id.desc(),
            )
        )
        return [_addon_zip_installation_from_row(row) for row in rows]

    def list_for_artifact(
        self, artifact_id: UUID
    ) -> list[AddonZipInstallationOperation]:
        rows = self._session.scalars(
            select(AddonZipInstallationOperationRow)
            .where(AddonZipInstallationOperationRow.artifact_id == str(artifact_id))
            .order_by(
                AddonZipInstallationOperationRow.requested_at.desc(),
                AddonZipInstallationOperationRow.id.desc(),
            )
        )
        return [_addon_zip_installation_from_row(row) for row in rows]

    def list_queued(self) -> list[AddonZipInstallationOperation]:
        rows = self._session.scalars(
            select(AddonZipInstallationOperationRow)
            .where(
                AddonZipInstallationOperationRow.state
                == AddonZipInstallationOperationState.QUEUED
            )
            .order_by(
                AddonZipInstallationOperationRow.requested_at,
                AddonZipInstallationOperationRow.id,
            )
        )
        return [_addon_zip_installation_from_row(row) for row in rows]

    def active_for_instance(
        self, instance_id: UUID
    ) -> AddonZipInstallationOperation | None:
        row = self._session.scalar(
            select(AddonZipInstallationOperationRow).where(
                AddonZipInstallationOperationRow.managed_instance_id
                == str(instance_id),
                AddonZipInstallationOperationRow.state.in_(
                    (
                        AddonZipInstallationOperationState.QUEUED,
                        AddonZipInstallationOperationState.RUNNING,
                    )
                ),
            )
        )
        return None if row is None else _addon_zip_installation_from_row(row)

    def acquire(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(AddonZipInstallationOperationRow)
            .where(
                AddonZipInstallationOperationRow.id == str(operation_id),
                AddonZipInstallationOperationRow.state
                == AddonZipInstallationOperationState.QUEUED,
                AddonZipInstallationOperationRow.lease_token_digest.is_(None),
            )
            .values(
                state=AddonZipInstallationOperationState.RUNNING,
                phase=AddonZipInstallationOperationPhase.PREFLIGHT,
                started_at=_timestamp_to_text(now),
                heartbeat_at=_timestamp_to_text(now),
                lease_token_digest=token_digest,
                lease_expires_at=_timestamp_to_text(expires_at),
            )
        )
        return bool(getattr(result, "rowcount", 0))

    def update_fields(
        self, operation_id: UUID, *, token_digest: str, **values: object
    ) -> bool:
        allowed = {
            "phase",
            "expected_container_id",
            "application_started",
            "stopped_readback_matched",
            "final_readback_matched",
            "recognition_enabled",
            "dependency_baseline_json",
            "dependency_post_json",
            "heartbeat_at",
            "lease_expires_at",
        }
        if not values or not set(values) <= allowed:
            raise ValueError("unsupported ZIP installation operation update")
        for field in ("heartbeat_at", "lease_expires_at"):
            timestamp = values.get(field)
            if isinstance(timestamp, datetime):
                values[field] = _timestamp_to_text(timestamp)
        result = self._session.execute(
            update(AddonZipInstallationOperationRow)
            .where(
                AddonZipInstallationOperationRow.id == str(operation_id),
                AddonZipInstallationOperationRow.state
                == AddonZipInstallationOperationState.RUNNING,
                AddonZipInstallationOperationRow.lease_token_digest == token_digest,
            )
            .values(**values)
        )
        return bool(getattr(result, "rowcount", 0))

    def finish(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        state: AddonZipInstallationOperationState,
        now: datetime,
        reason: str | None = None,
    ) -> bool:
        result = self._session.execute(
            update(AddonZipInstallationOperationRow)
            .where(
                AddonZipInstallationOperationRow.id == str(operation_id),
                AddonZipInstallationOperationRow.state
                == AddonZipInstallationOperationState.RUNNING,
                AddonZipInstallationOperationRow.lease_token_digest == token_digest,
            )
            .values(
                state=state,
                phase=AddonZipInstallationOperationPhase.FINALIZE,
                completed_at=_timestamp_to_text(now),
                failure_reason=reason,
                reconciliation_required=state
                is AddonZipInstallationOperationState.RECONCILIATION_REQUIRED,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        return bool(getattr(result, "rowcount", 0))

    def reconciliation_for_instance(
        self, instance_id: UUID
    ) -> list[AddonZipInstallationOperation]:
        rows = self._session.scalars(
            select(AddonZipInstallationOperationRow).where(
                AddonZipInstallationOperationRow.original_managed_instance_id
                == str(instance_id),
                AddonZipInstallationOperationRow.state
                == AddonZipInstallationOperationState.RECONCILIATION_REQUIRED,
            )
        )
        return [_addon_zip_installation_from_row(row) for row in rows]

    def mark_stale_reconciliation(
        self, operation_id: UUID, *, token_digest: str, now: datetime
    ) -> bool:
        result = self._session.execute(
            update(AddonZipInstallationOperationRow)
            .where(
                AddonZipInstallationOperationRow.id == str(operation_id),
                AddonZipInstallationOperationRow.state
                == AddonZipInstallationOperationState.RUNNING,
                AddonZipInstallationOperationRow.lease_token_digest == token_digest,
            )
            .values(
                state=AddonZipInstallationOperationState.RECONCILIATION_REQUIRED,
                phase=AddonZipInstallationOperationPhase.FINALIZE,
                completed_at=_timestamp_to_text(now),
                failure_reason="Uploaded-ZIP installation authority expired.",
                reconciliation_required=True,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        return bool(getattr(result, "rowcount", 0))

    def clear_live_instance(self, instance_id: UUID) -> int:
        result = self._session.execute(
            update(AddonZipInstallationOperationRow)
            .where(
                AddonZipInstallationOperationRow.managed_instance_id == str(instance_id)
            )
            .values(managed_instance_id=None)
        )
        return int(getattr(result, "rowcount", 0))

    def prune_terminal(self, instance_id: UUID, *, retain: int = 20) -> int:
        rows = list(
            self._session.scalars(
                select(AddonZipInstallationOperationRow.id)
                .where(
                    AddonZipInstallationOperationRow.original_managed_instance_id
                    == str(instance_id),
                    AddonZipInstallationOperationRow.state.in_(
                        (
                            AddonZipInstallationOperationState.SUCCEEDED,
                            AddonZipInstallationOperationState.FAILED,
                        )
                    ),
                    AddonZipInstallationOperationRow.id.not_in(
                        select(
                            AddonZipInstallationEvidenceRow.installation_operation_id
                        )
                    ),
                )
                .order_by(
                    AddonZipInstallationOperationRow.requested_at.desc(),
                    AddonZipInstallationOperationRow.id.desc(),
                )
                .offset(retain)
            )
        )
        if not rows:
            return 0
        result = self._session.execute(
            delete(AddonZipInstallationOperationRow).where(
                AddonZipInstallationOperationRow.id.in_(rows)
            )
        )
        return int(getattr(result, "rowcount", 0))


class AddonZipEnablementOperationRepository:
    """Durable typed synchronous ZIP enablement persistence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: AddonZipEnablementOperation) -> None:
        self._session.add(
            AddonZipEnablementOperationRow(
                id=str(operation.id),
                original_managed_instance_id=str(
                    operation.original_managed_instance_id
                ),
                managed_instance_id=(
                    None
                    if operation.managed_instance_id is None
                    else str(operation.managed_instance_id)
                ),
                artifact_id=str(operation.artifact_id),
                installation_operation_id=str(operation.installation_operation_id),
                addon_id=operation.addon_id,
                addon_version=operation.addon_version,
                runtime_revision_id=str(operation.runtime_revision_id),
                expected_container_id=operation.expected_container_id,
                requested_at=_timestamp_to_text(operation.requested_at),
                started_at=_timestamp_to_text(operation.started_at),
                state=operation.state,
                phase=operation.phase,
                result_classification=operation.result_classification,
                payload_verification=operation.payload_verification,
                prior_enabled=operation.prior_enabled,
                result_enabled=operation.result_enabled,
                enable_performed=operation.enable_performed,
                blocked_dependency_id=operation.blocked_dependency_id,
                blocked_dependency_reason=operation.blocked_dependency_reason,
                completed_at=(
                    None
                    if operation.completed_at is None
                    else _timestamp_to_text(operation.completed_at)
                ),
                failure_reason=operation.failure_reason,
                reconciliation_required=operation.reconciliation_required,
            )
        )
        self._session.flush()

    def get(self, operation_id: UUID) -> AddonZipEnablementOperation | None:
        row = self._session.get(AddonZipEnablementOperationRow, str(operation_id))
        return None if row is None else _addon_zip_enablement_from_row(row)

    def list_for_original_instance(
        self, instance_id: UUID
    ) -> list[AddonZipEnablementOperation]:
        rows = self._session.scalars(
            select(AddonZipEnablementOperationRow)
            .where(
                AddonZipEnablementOperationRow.original_managed_instance_id
                == str(instance_id)
            )
            .order_by(
                AddonZipEnablementOperationRow.requested_at.desc(),
                AddonZipEnablementOperationRow.id.desc(),
            )
        )
        return [_addon_zip_enablement_from_row(row) for row in rows]

    def list_for_artifact(self, artifact_id: UUID) -> list[AddonZipEnablementOperation]:
        rows = self._session.scalars(
            select(AddonZipEnablementOperationRow)
            .where(AddonZipEnablementOperationRow.artifact_id == str(artifact_id))
            .order_by(
                AddonZipEnablementOperationRow.requested_at.desc(),
                AddonZipEnablementOperationRow.id.desc(),
            )
        )
        return [_addon_zip_enablement_from_row(row) for row in rows]

    def active_for_instance(
        self, instance_id: UUID
    ) -> AddonZipEnablementOperation | None:
        row = self._session.scalar(
            select(AddonZipEnablementOperationRow).where(
                AddonZipEnablementOperationRow.managed_instance_id == str(instance_id),
                AddonZipEnablementOperationRow.state
                == AddonZipEnablementOperationState.RUNNING,
            )
        )
        return None if row is None else _addon_zip_enablement_from_row(row)

    def update_fields(self, operation_id: UUID, **values: object) -> bool:
        allowed = {
            "phase",
            "expected_container_id",
            "payload_verification",
            "prior_enabled",
            "enable_performed",
        }
        if not values or not set(values) <= allowed:
            raise ValueError("unsupported ZIP enablement operation update")
        result = self._session.execute(
            update(AddonZipEnablementOperationRow)
            .where(
                AddonZipEnablementOperationRow.id == str(operation_id),
                AddonZipEnablementOperationRow.state
                == AddonZipEnablementOperationState.RUNNING,
            )
            .values(**values)
        )
        return bool(getattr(result, "rowcount", 0))

    def terminalize(
        self,
        operation_id: UUID,
        *,
        state: AddonZipEnablementOperationState,
        result_classification: AddonZipEnablementResult,
        payload_verification: AddonZipPayloadVerificationOutcome,
        result_enabled: bool | None,
        blocked_dependency_id: str | None,
        blocked_dependency_reason: str | None,
        failure_reason: str | None,
        reconciliation_required: bool,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(AddonZipEnablementOperationRow)
            .where(
                AddonZipEnablementOperationRow.id == str(operation_id),
                AddonZipEnablementOperationRow.state
                == AddonZipEnablementOperationState.RUNNING,
            )
            .values(
                state=state,
                phase=AddonZipEnablementOperationPhase.FINALIZE,
                result_classification=result_classification,
                payload_verification=payload_verification,
                result_enabled=result_enabled,
                blocked_dependency_id=blocked_dependency_id,
                blocked_dependency_reason=blocked_dependency_reason,
                completed_at=_timestamp_to_text(now),
                failure_reason=failure_reason,
                reconciliation_required=reconciliation_required,
            )
        )
        return bool(getattr(result, "rowcount", 0))

    def mark_stale_reconciliation(self, operation_id: UUID, *, now: datetime) -> bool:
        row = self._session.get(AddonZipEnablementOperationRow, str(operation_id))
        if row is None or row.state is not AddonZipEnablementOperationState.RUNNING:
            return False
        if not row.enable_performed:
            return self.terminalize(
                operation_id,
                state=AddonZipEnablementOperationState.FAILED,
                result_classification=AddonZipEnablementResult.FAILED,
                payload_verification=row.payload_verification,
                result_enabled=None,
                blocked_dependency_id=None,
                blocked_dependency_reason=None,
                failure_reason="zip_enablement_failed_before_mutation",
                reconciliation_required=False,
                now=now,
            )
        return self.terminalize(
            operation_id,
            state=AddonZipEnablementOperationState.RECONCILIATION_REQUIRED,
            result_classification=AddonZipEnablementResult.RECONCILIATION_REQUIRED,
            payload_verification=row.payload_verification,
            result_enabled=None,
            blocked_dependency_id=None,
            blocked_dependency_reason=None,
            failure_reason="zip_enablement_reconciliation_required",
            reconciliation_required=True,
            now=now,
        )

    def reconciliation_for_instance(
        self, instance_id: UUID, *, limit: int = 2
    ) -> list[AddonZipEnablementOperation]:
        rows = self._session.scalars(
            select(AddonZipEnablementOperationRow)
            .where(
                AddonZipEnablementOperationRow.original_managed_instance_id
                == str(instance_id),
                AddonZipEnablementOperationRow.reconciliation_required.is_(True),
            )
            .order_by(AddonZipEnablementOperationRow.requested_at.desc())
            .limit(limit)
        )
        return [_addon_zip_enablement_from_row(row) for row in rows]

    def clear_live_instance(self, instance_id: UUID) -> int:
        result = self._session.execute(
            update(AddonZipEnablementOperationRow)
            .where(
                AddonZipEnablementOperationRow.managed_instance_id == str(instance_id)
            )
            .values(managed_instance_id=None)
        )
        return int(getattr(result, "rowcount", 0))

    def prune_terminal(self, instance_id: UUID, *, retain: int = 20) -> int:
        rows = list(
            self._session.scalars(
                select(AddonZipEnablementOperationRow.id)
                .where(
                    AddonZipEnablementOperationRow.original_managed_instance_id
                    == str(instance_id),
                    AddonZipEnablementOperationRow.state.in_(
                        (
                            AddonZipEnablementOperationState.SUCCEEDED,
                            AddonZipEnablementOperationState.FAILED,
                        )
                    ),
                )
                .order_by(
                    AddonZipEnablementOperationRow.requested_at.desc(),
                    AddonZipEnablementOperationRow.id.desc(),
                )
                .offset(retain)
            )
        )
        if not rows:
            return 0
        result = self._session.execute(
            delete(AddonZipEnablementOperationRow).where(
                AddonZipEnablementOperationRow.id.in_(rows)
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
                python_bytecode_policy=profile.python_bytecode_policy,
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
            python_bytecode_policy=row.python_bytecode_policy,
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


class DevelopmentSourceDeploymentRepository:
    """Current last-verified deployment evidence, separate from attachment."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self, instance_id: UUID, source_id: UUID
    ) -> DevelopmentSourceDeployment | None:
        row = self._session.get(
            DevelopmentSourceDeploymentRow, (str(instance_id), str(source_id))
        )
        return None if row is None else _development_source_deployment_from_row(row)

    def list_for_instance(self, instance_id: UUID) -> list[DevelopmentSourceDeployment]:
        rows = self._session.scalars(
            select(DevelopmentSourceDeploymentRow)
            .where(
                DevelopmentSourceDeploymentRow.managed_instance_id == str(instance_id)
            )
            .order_by(DevelopmentSourceDeploymentRow.development_source_id)
        )
        return [_development_source_deployment_from_row(row) for row in rows]

    def upsert_verified(self, evidence: DevelopmentSourceDeployment) -> None:
        row = self._session.get(
            DevelopmentSourceDeploymentRow,
            (str(evidence.managed_instance_id), str(evidence.development_source_id)),
        )
        values = {
            "addon_id": evidence.addon_id,
            "mode": evidence.mode,
            "last_successful_operation_id": str(evidence.last_successful_operation_id),
            "source_manifest_digest": evidence.source_manifest_digest,
            "verified_destination_digest": evidence.verified_destination_digest,
            "runtime_revision_id": str(evidence.runtime_revision_id),
            "trusted_container_id": evidence.trusted_container_id,
            "observed_source_name": evidence.observed_source_name,
            "observed_source_version": evidence.observed_source_version,
            "verified_at": _timestamp_to_text(evidence.verified_at),
            "confidence": evidence.confidence,
        }
        if row is None:
            self._session.add(
                DevelopmentSourceDeploymentRow(
                    managed_instance_id=str(evidence.managed_instance_id),
                    development_source_id=str(evidence.development_source_id),
                    **values,
                )
            )
        else:
            for name, value in values.items():
                setattr(row, name, value)
        self._session.flush()

    def mark_instance_confidence(
        self,
        instance_id: UUID,
        confidence: DevelopmentSourceDeploymentConfidence,
    ) -> int:
        result = self._session.execute(
            update(DevelopmentSourceDeploymentRow)
            .where(
                DevelopmentSourceDeploymentRow.managed_instance_id == str(instance_id)
            )
            .values(confidence=confidence)
        )
        return int(getattr(result, "rowcount", 0))

    def mark_confidence(
        self,
        instance_id: UUID,
        source_id: UUID,
        confidence: DevelopmentSourceDeploymentConfidence,
    ) -> bool:
        result = self._session.execute(
            update(DevelopmentSourceDeploymentRow)
            .where(
                DevelopmentSourceDeploymentRow.managed_instance_id == str(instance_id),
                DevelopmentSourceDeploymentRow.development_source_id == str(source_id),
            )
            .values(confidence=confidence)
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def delete_for_instance(self, instance_id: UUID) -> int:
        result = self._session.execute(
            delete(DevelopmentSourceDeploymentRow).where(
                DevelopmentSourceDeploymentRow.managed_instance_id == str(instance_id)
            )
        )
        return int(getattr(result, "rowcount", 0))


class _DevelopmentSourceDeploymentOperationRepositoryBase:
    """Durable typed copy/sync operation persistence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: DevelopmentSourceDeploymentOperation) -> None:
        self._session.add(
            DevelopmentSourceDeploymentOperationRow(
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
                runtime_revision_id=str(operation.runtime_revision_id),
                request_kind=operation.request_kind,
                requested_at=_timestamp_to_text(operation.requested_at),
                state=operation.state,
                phase=operation.phase,
                reconciliation_required=operation.reconciliation_required,
            )
        )
        self._session.flush()

    def get(self, operation_id: UUID) -> DevelopmentSourceDeploymentOperation | None:
        row = self._session.get(
            DevelopmentSourceDeploymentOperationRow, str(operation_id)
        )
        return (
            None
            if row is None
            else _development_source_deployment_operation_from_row(row)
        )

    def active_for_instance(
        self, instance_id: UUID
    ) -> DevelopmentSourceDeploymentOperation | None:
        row = self._session.scalar(
            select(DevelopmentSourceDeploymentOperationRow)
            .where(
                DevelopmentSourceDeploymentOperationRow.managed_instance_id
                == str(instance_id),
                DevelopmentSourceDeploymentOperationRow.state.in_(
                    (
                        DevelopmentSourceDeploymentOperationState.QUEUED,
                        DevelopmentSourceDeploymentOperationState.RUNNING,
                    )
                ),
            )
            .order_by(DevelopmentSourceDeploymentOperationRow.requested_at.desc())
            .limit(1)
        )
        return (
            None
            if row is None
            else _development_source_deployment_operation_from_row(row)
        )

    def list_queued(self) -> list[DevelopmentSourceDeploymentOperation]:
        rows = self._session.scalars(
            select(DevelopmentSourceDeploymentOperationRow)
            .where(
                DevelopmentSourceDeploymentOperationRow.state
                == DevelopmentSourceDeploymentOperationState.QUEUED
            )
            .order_by(DevelopmentSourceDeploymentOperationRow.requested_at)
        )
        return [_development_source_deployment_operation_from_row(row) for row in rows]

    def list_for_original_instance(
        self, instance_id: UUID
    ) -> list[DevelopmentSourceDeploymentOperation]:
        rows = self._session.scalars(
            select(DevelopmentSourceDeploymentOperationRow)
            .where(
                DevelopmentSourceDeploymentOperationRow.original_managed_instance_id
                == str(instance_id)
            )
            .order_by(
                DevelopmentSourceDeploymentOperationRow.requested_at.desc(),
                DevelopmentSourceDeploymentOperationRow.id.desc(),
            )
        )
        return [_development_source_deployment_operation_from_row(row) for row in rows]

    def clear_live_instance(self, instance_id: UUID) -> int:
        result = self._session.execute(
            update(DevelopmentSourceDeploymentOperationRow)
            .where(
                DevelopmentSourceDeploymentOperationRow.managed_instance_id
                == str(instance_id)
            )
            .values(managed_instance_id=None)
        )
        return int(getattr(result, "rowcount", 0))


class DevelopmentSourceEnablementOperationRepository:
    """Durable typed synchronous enablement persistence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: DevelopmentSourceEnablementOperation) -> None:
        self._session.add(
            DevelopmentSourceEnablementOperationRow(
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
                addon_version=operation.addon_version,
                runtime_revision_id=str(operation.runtime_revision_id),
                requested_at=_timestamp_to_text(operation.requested_at),
                started_at=_timestamp_to_text(operation.started_at),
                state=operation.state,
                phase=operation.phase,
                prior_enabled=operation.prior_enabled,
                result_enabled=operation.result_enabled,
                enable_performed=operation.enable_performed,
                reconciliation_required=operation.reconciliation_required,
            )
        )
        self._session.flush()

    def get(self, operation_id: UUID) -> DevelopmentSourceEnablementOperation | None:
        row = self._session.get(
            DevelopmentSourceEnablementOperationRow, str(operation_id)
        )
        return (
            None
            if row is None
            else _development_source_enablement_operation_from_row(row)
        )

    def list_for_original_instance(
        self, instance_id: UUID
    ) -> list[DevelopmentSourceEnablementOperation]:
        rows = self._session.scalars(
            select(DevelopmentSourceEnablementOperationRow)
            .where(
                DevelopmentSourceEnablementOperationRow.original_managed_instance_id
                == str(instance_id)
            )
            .order_by(
                DevelopmentSourceEnablementOperationRow.requested_at.desc(),
                DevelopmentSourceEnablementOperationRow.id.desc(),
            )
        )
        return [_development_source_enablement_operation_from_row(row) for row in rows]

    def active_for_instance(
        self, instance_id: UUID
    ) -> DevelopmentSourceEnablementOperation | None:
        row = self._session.scalar(
            select(DevelopmentSourceEnablementOperationRow)
            .where(
                DevelopmentSourceEnablementOperationRow.managed_instance_id
                == str(instance_id),
                DevelopmentSourceEnablementOperationRow.state
                == DevelopmentSourceEnablementOperationState.RUNNING,
            )
            .limit(1)
        )
        return (
            None
            if row is None
            else _development_source_enablement_operation_from_row(row)
        )

    def update_phase(
        self,
        operation_id: UUID,
        phase: DevelopmentSourceEnablementOperationPhase,
    ) -> bool:
        result = self._session.execute(
            update(DevelopmentSourceEnablementOperationRow)
            .where(
                DevelopmentSourceEnablementOperationRow.id == str(operation_id),
                DevelopmentSourceEnablementOperationRow.state
                == DevelopmentSourceEnablementOperationState.RUNNING,
            )
            .values(phase=phase)
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def set_preflight_state(self, operation_id: UUID, *, prior_enabled: bool) -> bool:
        result = self._session.execute(
            update(DevelopmentSourceEnablementOperationRow)
            .where(
                DevelopmentSourceEnablementOperationRow.id == str(operation_id),
                DevelopmentSourceEnablementOperationRow.state
                == DevelopmentSourceEnablementOperationState.RUNNING,
            )
            .values(prior_enabled=prior_enabled)
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def mark_mutation_started(self, operation_id: UUID) -> bool:
        result = self._session.execute(
            update(DevelopmentSourceEnablementOperationRow)
            .where(
                DevelopmentSourceEnablementOperationRow.id == str(operation_id),
                DevelopmentSourceEnablementOperationRow.state
                == DevelopmentSourceEnablementOperationState.RUNNING,
                DevelopmentSourceEnablementOperationRow.enable_performed.is_(False),
            )
            .values(
                phase=DevelopmentSourceEnablementOperationPhase.MUTATION,
                enable_performed=True,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def terminalize(
        self,
        operation_id: UUID,
        *,
        state: DevelopmentSourceEnablementOperationState,
        result_classification: DevelopmentSourceEnablementResult,
        result_enabled: bool | None,
        blocked_dependency_id: str | None,
        blocked_dependency_reason: str | None,
        failure_reason: str | None,
        reconciliation_required: bool,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(DevelopmentSourceEnablementOperationRow)
            .where(
                DevelopmentSourceEnablementOperationRow.id == str(operation_id),
                DevelopmentSourceEnablementOperationRow.state
                == DevelopmentSourceEnablementOperationState.RUNNING,
            )
            .values(
                state=state,
                phase=DevelopmentSourceEnablementOperationPhase.FINALIZE,
                result_classification=result_classification,
                result_enabled=result_enabled,
                blocked_dependency_id=blocked_dependency_id,
                blocked_dependency_reason=blocked_dependency_reason,
                completed_at=_timestamp_to_text(now),
                failure_reason=failure_reason,
                reconciliation_required=reconciliation_required,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def mark_stale_reconciliation(self, operation_id: UUID, *, now: datetime) -> bool:
        row = self._session.get(
            DevelopmentSourceEnablementOperationRow, str(operation_id)
        )
        if (
            row is None
            or row.state is not DevelopmentSourceEnablementOperationState.RUNNING
        ):
            return False
        if not row.enable_performed:
            return self.terminalize(
                operation_id,
                state=DevelopmentSourceEnablementOperationState.FAILED,
                result_classification=DevelopmentSourceEnablementResult.FAILED,
                result_enabled=None,
                blocked_dependency_id=None,
                blocked_dependency_reason=None,
                failure_reason="enablement_failed_before_mutation",
                reconciliation_required=False,
                now=now,
            )
        return self.terminalize(
            operation_id,
            state=DevelopmentSourceEnablementOperationState.RECONCILIATION_REQUIRED,
            result_classification=DevelopmentSourceEnablementResult.RECONCILIATION_REQUIRED,
            result_enabled=None,
            blocked_dependency_id=None,
            blocked_dependency_reason=None,
            failure_reason="enablement_reconciliation_required",
            reconciliation_required=True,
            now=now,
        )

    def reconciliation_for_instance(
        self, instance_id: UUID, *, limit: int = 2
    ) -> list[DevelopmentSourceEnablementOperation]:
        rows = self._session.scalars(
            select(DevelopmentSourceEnablementOperationRow)
            .where(
                DevelopmentSourceEnablementOperationRow.original_managed_instance_id
                == str(instance_id),
                DevelopmentSourceEnablementOperationRow.reconciliation_required.is_(
                    True
                ),
            )
            .order_by(DevelopmentSourceEnablementOperationRow.requested_at.desc())
            .limit(limit)
        )
        return [_development_source_enablement_operation_from_row(row) for row in rows]

    def clear_live_instance(self, instance_id: UUID) -> int:
        result = self._session.execute(
            update(DevelopmentSourceEnablementOperationRow)
            .where(
                DevelopmentSourceEnablementOperationRow.managed_instance_id
                == str(instance_id)
            )
            .values(managed_instance_id=None)
        )
        return int(getattr(result, "rowcount", 0))

    def prune_terminal(self, instance_id: UUID, *, retain: int = 20) -> int:
        rows = list(
            self._session.scalars(
                select(DevelopmentSourceEnablementOperationRow.id)
                .where(
                    DevelopmentSourceEnablementOperationRow.original_managed_instance_id
                    == str(instance_id),
                    DevelopmentSourceEnablementOperationRow.state.in_(
                        (
                            DevelopmentSourceEnablementOperationState.SUCCEEDED,
                            DevelopmentSourceEnablementOperationState.FAILED,
                        )
                    ),
                )
                .order_by(
                    DevelopmentSourceEnablementOperationRow.requested_at.desc(),
                    DevelopmentSourceEnablementOperationRow.id.desc(),
                )
                .offset(retain)
            )
        )
        if not rows:
            return 0
        result = self._session.execute(
            delete(DevelopmentSourceEnablementOperationRow).where(
                DevelopmentSourceEnablementOperationRow.id.in_(rows)
            )
        )
        return int(getattr(result, "rowcount", 0))


class DevelopmentSourceSmokeTestOperationRepository:
    """Durable smoke-test history with a fixed 90-second running lease."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, operation: DevelopmentSourceSmokeTestOperation) -> None:
        self._session.add(
            DevelopmentSourceSmokeTestOperationRow(
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
                addon_version=operation.addon_version,
                runtime_revision_id=str(operation.runtime_revision_id),
                requested_at=_timestamp_to_text(operation.requested_at),
                state=operation.state,
                phase=operation.phase,
                overall_result=operation.overall_result,
                execution_result=operation.execution_result,
                post_execution_provenance=operation.post_execution_provenance,
                invocation_dispatched=operation.invocation_dispatched,
                reconciliation_required=operation.reconciliation_required,
            )
        )
        self._session.flush()

    def get(self, operation_id: UUID) -> DevelopmentSourceSmokeTestOperation | None:
        row = self._session.get(
            DevelopmentSourceSmokeTestOperationRow, str(operation_id)
        )
        return (
            None
            if row is None
            else _development_source_smoke_test_operation_from_row(row)
        )

    def list_for_original_instance(
        self, instance_id: UUID
    ) -> list[DevelopmentSourceSmokeTestOperation]:
        rows = self._session.scalars(
            select(DevelopmentSourceSmokeTestOperationRow)
            .where(
                DevelopmentSourceSmokeTestOperationRow.original_managed_instance_id
                == str(instance_id)
            )
            .order_by(
                DevelopmentSourceSmokeTestOperationRow.requested_at.desc(),
                DevelopmentSourceSmokeTestOperationRow.id.desc(),
            )
        )
        return [_development_source_smoke_test_operation_from_row(row) for row in rows]

    def list_queued(self) -> list[DevelopmentSourceSmokeTestOperation]:
        rows = self._session.scalars(
            select(DevelopmentSourceSmokeTestOperationRow)
            .where(
                DevelopmentSourceSmokeTestOperationRow.state
                == DevelopmentSourceSmokeTestOperationState.QUEUED
            )
            .order_by(
                DevelopmentSourceSmokeTestOperationRow.requested_at,
                DevelopmentSourceSmokeTestOperationRow.id,
            )
        )
        return [_development_source_smoke_test_operation_from_row(row) for row in rows]

    def active_for_instance(
        self, instance_id: UUID
    ) -> DevelopmentSourceSmokeTestOperation | None:
        row = self._session.scalar(
            select(DevelopmentSourceSmokeTestOperationRow)
            .where(
                DevelopmentSourceSmokeTestOperationRow.managed_instance_id
                == str(instance_id),
                DevelopmentSourceSmokeTestOperationRow.state.in_(
                    (
                        DevelopmentSourceSmokeTestOperationState.QUEUED,
                        DevelopmentSourceSmokeTestOperationState.RUNNING,
                    )
                ),
            )
            .limit(1)
        )
        return (
            None
            if row is None
            else _development_source_smoke_test_operation_from_row(row)
        )

    def acquire(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        expected_container_id: str | None,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(DevelopmentSourceSmokeTestOperationRow)
            .where(
                DevelopmentSourceSmokeTestOperationRow.id == str(operation_id),
                DevelopmentSourceSmokeTestOperationRow.state
                == DevelopmentSourceSmokeTestOperationState.QUEUED,
            )
            .values(
                state=DevelopmentSourceSmokeTestOperationState.RUNNING,
                phase=DevelopmentSourceSmokeTestOperationPhase.ACQUIRE,
                expected_container_id=expected_container_id,
                started_at=_timestamp_to_text(now),
                lease_token_digest=token_digest,
                lease_expires_at=_timestamp_to_text(lease_expires_at),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def set_expected_container(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        expected_container_id: str,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(DevelopmentSourceSmokeTestOperationRow)
            .where(
                DevelopmentSourceSmokeTestOperationRow.id == str(operation_id),
                DevelopmentSourceSmokeTestOperationRow.state
                == DevelopmentSourceSmokeTestOperationState.RUNNING,
                DevelopmentSourceSmokeTestOperationRow.lease_token_digest
                == token_digest,
                DevelopmentSourceSmokeTestOperationRow.lease_expires_at
                > _timestamp_to_text(now),
                DevelopmentSourceSmokeTestOperationRow.expected_container_id.is_(None),
            )
            .values(expected_container_id=expected_container_id)
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def update_phase(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        phase: DevelopmentSourceSmokeTestOperationPhase,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(DevelopmentSourceSmokeTestOperationRow)
            .where(
                DevelopmentSourceSmokeTestOperationRow.id == str(operation_id),
                DevelopmentSourceSmokeTestOperationRow.state
                == DevelopmentSourceSmokeTestOperationState.RUNNING,
                DevelopmentSourceSmokeTestOperationRow.lease_token_digest
                == token_digest,
                DevelopmentSourceSmokeTestOperationRow.lease_expires_at
                > _timestamp_to_text(now),
            )
            .values(phase=phase)
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def mark_invocation_dispatched(
        self, operation_id: UUID, *, token_digest: str, now: datetime
    ) -> bool:
        result = self._session.execute(
            update(DevelopmentSourceSmokeTestOperationRow)
            .where(
                DevelopmentSourceSmokeTestOperationRow.id == str(operation_id),
                DevelopmentSourceSmokeTestOperationRow.state
                == DevelopmentSourceSmokeTestOperationState.RUNNING,
                DevelopmentSourceSmokeTestOperationRow.lease_token_digest
                == token_digest,
                DevelopmentSourceSmokeTestOperationRow.lease_expires_at
                > _timestamp_to_text(now),
                DevelopmentSourceSmokeTestOperationRow.invocation_dispatched.is_(False),
            )
            .values(
                phase=DevelopmentSourceSmokeTestOperationPhase.INVOKE,
                invocation_dispatched=True,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def finish(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        state: DevelopmentSourceSmokeTestOperationState,
        overall_result: DevelopmentSourceSmokeTestOverallResult | None,
        execution_result: DevelopmentSourceSmokeTestExecutionResult,
        post_execution_provenance: DevelopmentSourceSmokeTestPostProvenance,
        invocation_duration_ms: int | None,
        returned_item_count: int | None,
        blocked_dependency_id: str | None,
        blocked_dependency_reason: str | None,
        failure_reason: str | None,
        reconciliation_required: bool,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(DevelopmentSourceSmokeTestOperationRow)
            .where(
                DevelopmentSourceSmokeTestOperationRow.id == str(operation_id),
                DevelopmentSourceSmokeTestOperationRow.state
                == DevelopmentSourceSmokeTestOperationState.RUNNING,
                DevelopmentSourceSmokeTestOperationRow.lease_token_digest
                == token_digest,
                DevelopmentSourceSmokeTestOperationRow.lease_expires_at
                > _timestamp_to_text(now),
            )
            .values(
                state=state,
                phase=DevelopmentSourceSmokeTestOperationPhase.FINALIZE,
                overall_result=overall_result,
                execution_result=execution_result,
                post_execution_provenance=post_execution_provenance,
                invocation_duration_ms=invocation_duration_ms,
                returned_item_count=returned_item_count,
                blocked_dependency_id=blocked_dependency_id,
                blocked_dependency_reason=blocked_dependency_reason,
                completed_at=_timestamp_to_text(now),
                failure_reason=failure_reason,
                reconciliation_required=reconciliation_required,
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def mark_stale(
        self, operation_id: UUID, *, token_digest: str, now: datetime
    ) -> bool:
        row = self._session.get(
            DevelopmentSourceSmokeTestOperationRow, str(operation_id)
        )
        if (
            row is None
            or row.state is not DevelopmentSourceSmokeTestOperationState.RUNNING
            or row.lease_token_digest != token_digest
            or row.lease_expires_at is None
            or row.lease_expires_at > _timestamp_to_text(now)
        ):
            return False
        if row.invocation_dispatched:
            values = {
                "state": (
                    DevelopmentSourceSmokeTestOperationState.RECONCILIATION_REQUIRED
                ),
                "overall_result": (
                    DevelopmentSourceSmokeTestOverallResult.RECONCILIATION_REQUIRED
                ),
                "execution_result": (
                    DevelopmentSourceSmokeTestExecutionResult.AMBIGUOUS
                ),
                "post_execution_provenance": (
                    DevelopmentSourceSmokeTestPostProvenance.UNKNOWN
                ),
                "failure_reason": "smoke_execution_abandoned",
                "reconciliation_required": True,
            }
        else:
            values = {
                "state": DevelopmentSourceSmokeTestOperationState.FAILED,
                "overall_result": None,
                "execution_result": (
                    DevelopmentSourceSmokeTestExecutionResult.NOT_ATTEMPTED
                ),
                "post_execution_provenance": (
                    DevelopmentSourceSmokeTestPostProvenance.NOT_CHECKED
                ),
                "failure_reason": "smoke_failed_before_invocation",
                "reconciliation_required": False,
            }
        result = self._session.execute(
            update(DevelopmentSourceSmokeTestOperationRow)
            .where(
                DevelopmentSourceSmokeTestOperationRow.id == str(operation_id),
                DevelopmentSourceSmokeTestOperationRow.state
                == DevelopmentSourceSmokeTestOperationState.RUNNING,
                DevelopmentSourceSmokeTestOperationRow.lease_token_digest
                == token_digest,
            )
            .values(
                **values,
                phase=DevelopmentSourceSmokeTestOperationPhase.FINALIZE,
                completed_at=_timestamp_to_text(now),
            )
        )
        return int(getattr(result, "rowcount", 0)) == 1

    def reconciliation_for_instance(
        self, instance_id: UUID, *, limit: int = 2
    ) -> list[DevelopmentSourceSmokeTestOperation]:
        rows = self._session.scalars(
            select(DevelopmentSourceSmokeTestOperationRow)
            .where(
                DevelopmentSourceSmokeTestOperationRow.original_managed_instance_id
                == str(instance_id),
                DevelopmentSourceSmokeTestOperationRow.reconciliation_required.is_(
                    True
                ),
            )
            .order_by(
                DevelopmentSourceSmokeTestOperationRow.requested_at.desc(),
                DevelopmentSourceSmokeTestOperationRow.id.desc(),
            )
            .limit(limit)
        )
        return [_development_source_smoke_test_operation_from_row(row) for row in rows]

    def clear_live_instance(self, instance_id: UUID) -> int:
        result = self._session.execute(
            update(DevelopmentSourceSmokeTestOperationRow)
            .where(
                DevelopmentSourceSmokeTestOperationRow.managed_instance_id
                == str(instance_id)
            )
            .values(managed_instance_id=None)
        )
        return int(getattr(result, "rowcount", 0))

    def prune_terminal(self, instance_id: UUID, *, retain: int = 20) -> int:
        rows = list(
            self._session.scalars(
                select(DevelopmentSourceSmokeTestOperationRow.id)
                .where(
                    DevelopmentSourceSmokeTestOperationRow.original_managed_instance_id
                    == str(instance_id),
                    DevelopmentSourceSmokeTestOperationRow.state.in_(
                        (
                            DevelopmentSourceSmokeTestOperationState.SUCCEEDED,
                            DevelopmentSourceSmokeTestOperationState.FAILED,
                        )
                    ),
                )
                .order_by(
                    DevelopmentSourceSmokeTestOperationRow.requested_at.desc(),
                    DevelopmentSourceSmokeTestOperationRow.id.desc(),
                )
                .offset(retain)
            )
        )
        if not rows:
            return 0
        result = self._session.execute(
            delete(DevelopmentSourceSmokeTestOperationRow).where(
                DevelopmentSourceSmokeTestOperationRow.id.in_(rows)
            )
        )
        return int(getattr(result, "rowcount", 0))


class DevelopmentSourceDeploymentOperationRepository(
    _DevelopmentSourceDeploymentOperationRepositoryBase
):
    def reconciliation_for_instance(
        self, instance_id: UUID, *, limit: int = 2
    ) -> list[DevelopmentSourceDeploymentOperation]:
        rows = self._session.scalars(
            select(DevelopmentSourceDeploymentOperationRow)
            .where(
                DevelopmentSourceDeploymentOperationRow.original_managed_instance_id
                == str(instance_id),
                DevelopmentSourceDeploymentOperationRow.reconciliation_required.is_(
                    True
                ),
            )
            .order_by(DevelopmentSourceDeploymentOperationRow.requested_at.desc())
            .limit(limit)
        )
        return [_development_source_deployment_operation_from_row(row) for row in rows]

    def acquire(
        self,
        operation_id: UUID,
        *,
        token_digest: str,
        expected_container_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(DevelopmentSourceDeploymentOperationRow)
            .where(
                DevelopmentSourceDeploymentOperationRow.id == str(operation_id),
                DevelopmentSourceDeploymentOperationRow.state
                == DevelopmentSourceDeploymentOperationState.QUEUED,
            )
            .values(
                state=DevelopmentSourceDeploymentOperationState.RUNNING,
                phase=DevelopmentSourceDeploymentOperationPhase.ACQUIRE,
                expected_container_id=expected_container_id,
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
        phase: DevelopmentSourceDeploymentOperationPhase,
        now: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        result = self._session.execute(
            update(DevelopmentSourceDeploymentOperationRow)
            .where(
                DevelopmentSourceDeploymentOperationRow.id == str(operation_id),
                DevelopmentSourceDeploymentOperationRow.state
                == DevelopmentSourceDeploymentOperationState.RUNNING,
                DevelopmentSourceDeploymentOperationRow.lease_token_digest
                == token_digest,
                DevelopmentSourceDeploymentOperationRow.lease_expires_at
                > _timestamp_to_text(now),
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
        state: DevelopmentSourceDeploymentOperationState,
        result_classification: object | None,
        new_count: int | None,
        changed_count: int | None,
        unchanged_count: int | None,
        destination_only_count: int | None,
        transferred_bytes: int | None,
        failure_reason: str | None,
        reconciliation_required: bool,
        now: datetime,
    ) -> bool:
        result = self._session.execute(
            update(DevelopmentSourceDeploymentOperationRow)
            .where(
                DevelopmentSourceDeploymentOperationRow.id == str(operation_id),
                DevelopmentSourceDeploymentOperationRow.state
                == DevelopmentSourceDeploymentOperationState.RUNNING,
                DevelopmentSourceDeploymentOperationRow.lease_token_digest
                == token_digest,
            )
            .values(
                state=state,
                phase=DevelopmentSourceDeploymentOperationPhase.FINALIZE,
                result_classification=result_classification,
                new_count=new_count,
                changed_count=changed_count,
                unchanged_count=unchanged_count,
                destination_only_count=destination_only_count,
                transferred_bytes=transferred_bytes,
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
            update(DevelopmentSourceDeploymentOperationRow)
            .where(
                DevelopmentSourceDeploymentOperationRow.id == str(operation_id),
                DevelopmentSourceDeploymentOperationRow.state
                == DevelopmentSourceDeploymentOperationState.RUNNING,
                DevelopmentSourceDeploymentOperationRow.lease_token_digest
                == token_digest,
                DevelopmentSourceDeploymentOperationRow.lease_expires_at
                <= _timestamp_to_text(now),
            )
            .values(
                state=(
                    DevelopmentSourceDeploymentOperationState.RECONCILIATION_REQUIRED
                ),
                completed_at=_timestamp_to_text(now),
                heartbeat_at=_timestamp_to_text(now),
                failure_reason=func.coalesce(
                    DevelopmentSourceDeploymentOperationRow.failure_reason,
                    "Development-source deployment requires reconciliation.",
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
                select(DevelopmentSourceDeploymentOperationRow.id)
                .where(
                    DevelopmentSourceDeploymentOperationRow.original_managed_instance_id
                    == str(instance_id),
                    DevelopmentSourceDeploymentOperationRow.state.in_(
                        (
                            DevelopmentSourceDeploymentOperationState.SUCCEEDED,
                            DevelopmentSourceDeploymentOperationState.FAILED,
                        )
                    ),
                    DevelopmentSourceDeploymentOperationRow.id.not_in(
                        select(
                            DevelopmentSourceDeploymentRow.last_successful_operation_id
                        )
                    ),
                )
                .order_by(
                    DevelopmentSourceDeploymentOperationRow.requested_at.desc(),
                    DevelopmentSourceDeploymentOperationRow.id.desc(),
                )
                .offset(retain)
            )
        )
        if not rows:
            return 0
        result = self._session.execute(
            delete(DevelopmentSourceDeploymentOperationRow).where(
                DevelopmentSourceDeploymentOperationRow.id.in_(rows)
            )
        )
        return int(getattr(result, "rowcount", 0))
