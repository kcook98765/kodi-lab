"""SQLAlchemy mappings for core targets, instances, and runtime catalog."""

from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from kodi_lab.domain import (
    ConnectionState,
    DevelopmentSourceAttachmentOperationKind,
    DevelopmentSourceAttachmentOperationState,
    DevelopmentSourceKind,
    FixtureContentCategory,
    FixtureSizeCategory,
    GlobalHeavyOperationKind,
    HealthStatus,
    InstanceRuntimeState,
    InstanceType,
    ManagedInstanceDeleteOperationPhase,
    ManagedInstanceDeleteOperationState,
    ManagedInstanceDeletePriorState,
    ManagedInstanceLifecycleKind,
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceMutationKind,
    ManagedInstanceSemanticState,
    ProcessObservationKind,
    ReadinessProbeKind,
    ResetOperationPhase,
    ResetOperationState,
    RevisionApprovalState,
    RuntimeKind,
    RuntimeSupportTier,
    RuntimeValidationResult,
    SnapshotCreateOperationPhase,
    SnapshotCreateOperationState,
    SnapshotDeleteOperationPhase,
    SnapshotDeleteOperationState,
    SnapshotRestoreOperationPhase,
    SnapshotRestoreOperationState,
    SnapshotSourceBinding,
    SnapshotState,
    SnapshotValidationResult,
    TargetAvailability,
    TargetType,
)
from kodi_lab.storage.metadata import Base


def _enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [item.value for item in enum_type]


class DevelopmentSourceRow(Base):
    """Persisted registration-time observation of one local development source."""

    __tablename__ = "development_sources"
    __table_args__ = (
        UniqueConstraint("logical_name", name="uq_development_source_logical_name"),
        UniqueConstraint(
            "relative_locator", name="uq_development_source_relative_locator"
        ),
        UniqueConstraint("addon_id", name="uq_development_source_addon_id"),
        CheckConstraint(
            "length(logical_name) BETWEEN 1 AND 120 "
            "AND logical_name = trim(logical_name)",
            name="development_source_logical_name_bounded",
        ),
        CheckConstraint(
            "length(relative_locator) BETWEEN 1 AND 1024 "
            "AND substr(relative_locator, 1, 1) <> '/' "
            "AND instr(relative_locator, '\\') = 0 "
            "AND instr(relative_locator, '//') = 0 "
            "AND relative_locator <> '.' AND relative_locator <> '..' "
            "AND relative_locator NOT LIKE './%' "
            "AND relative_locator NOT LIKE '../%' "
            "AND relative_locator NOT LIKE '%/./%' "
            "AND relative_locator NOT LIKE '%/../%' "
            "AND relative_locator NOT LIKE '%/.' "
            "AND relative_locator NOT LIKE '%/..'",
            name="development_source_relative_locator_safe",
        ),
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 "
            "AND addon_id = lower(addon_id) "
            "AND addon_id = trim(addon_id) "
            "AND instr(addon_id, '/') = 0 AND instr(addon_id, '\\') = 0 "
            "AND instr(addon_id, '..') = 0",
            name="development_source_addon_id_bounded",
        ),
        CheckConstraint(
            "length(observed_name) BETWEEN 1 AND 256",
            name="development_source_observed_name_bounded",
        ),
        CheckConstraint(
            "length(observed_version) BETWEEN 1 AND 128",
            name="development_source_observed_version_bounded",
        ),
        CheckConstraint(
            "length(extension_types_json) BETWEEN 2 AND 8192",
            name="development_source_extensions_bounded",
        ),
        CheckConstraint(
            "length(registered_at) BETWEEN 20 AND 32 "
            "AND length(validated_at) BETWEEN 20 AND 32",
            name="development_source_timestamps_bounded",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    logical_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_kind: Mapped[DevelopmentSourceKind] = mapped_column(
        Enum(
            DevelopmentSourceKind,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="development_source_kind",
            length=32,
        ),
        nullable=False,
    )
    relative_locator: Mapped[str] = mapped_column(String(1024), nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    observed_name: Mapped[str] = mapped_column(String(256), nullable=False)
    observed_version: Mapped[str] = mapped_column(String(128), nullable=False)
    extension_types_json: Mapped[str] = mapped_column(Text, nullable=False)
    registered_at: Mapped[str] = mapped_column(String(32), nullable=False)
    validated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class TargetRow(Base):
    """Persisted normalized target."""

    __tablename__ = "targets"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="target_name_not_blank"),
        CheckConstraint(
            "length(trim(kodi_version)) > 0", name="target_kodi_version_not_blank"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[TargetType] = mapped_column(
        Enum(
            TargetType,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="target_type",
        ),
        nullable=False,
    )
    kodi_version: Mapped[str] = mapped_column(Text, nullable=False)
    availability: Mapped[TargetAvailability] = mapped_column(
        Enum(
            TargetAvailability,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="target_availability",
        ),
        nullable=False,
    )
    health: Mapped[HealthStatus] = mapped_column(
        Enum(
            HealthStatus,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="health_status",
        ),
        nullable=False,
    )
    connection_state: Mapped[ConnectionState] = mapped_column(
        Enum(
            ConnectionState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="connection_state",
        ),
        nullable=False,
    )


class ManagedInstanceRow(Base):
    """Managed-target subtype data with a shared stable identity."""

    __tablename__ = "managed_instances"
    __table_args__ = (
        CheckConstraint(
            "notes IS NULL OR length(notes) <= 4096",
            name="managed_instance_notes_bounded",
        ),
    )
    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("targets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    instance_type: Mapped[InstanceType] = mapped_column(
        Enum(
            InstanceType,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="instance_type",
        ),
        nullable=False,
    )
    runtime_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    runtime_state: Mapped[InstanceRuntimeState] = mapped_column(
        Enum(
            InstanceRuntimeState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="instance_runtime_state",
        ),
        nullable=False,
    )
    start_on_host_boot: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
    )
    notes: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    fixture_definition_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("fixture_definitions.id", ondelete="RESTRICT"),
        nullable=True,
    )


class ManagedInstanceDevelopmentSourceRow(Base):
    """Mode-neutral durable association between an instance and source."""

    __tablename__ = "managed_instance_development_sources"
    __table_args__ = (
        UniqueConstraint(
            "managed_instance_id",
            "development_source_id",
            name="uq_managed_instance_development_source",
        ),
        CheckConstraint(
            "length(attached_at) BETWEEN 20 AND 32",
            name="development_source_attachment_timestamp_bounded",
        ),
        Index(
            "ix_managed_instance_development_source_source",
            "development_source_id",
        ),
    )

    managed_instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("managed_instances.target_id", ondelete="CASCADE"),
        primary_key=True,
    )
    development_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_sources.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    attached_at: Mapped[str] = mapped_column(String(32), nullable=False)


class DevelopmentSourceAttachmentOperationRow(Base):
    """Typed terminal configuration history for source attachment changes."""

    __tablename__ = "development_source_attachment_operations"
    __table_args__ = (
        CheckConstraint(
            "length(source_logical_name) BETWEEN 1 AND 120",
            name="source_attachment_history_name_bounded",
        ),
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128",
            name="source_attachment_history_addon_id_bounded",
        ),
        CheckConstraint(
            "length(requested_at) BETWEEN 20 AND 32 "
            "AND length(completed_at) BETWEEN 20 AND 32",
            name="source_attachment_history_timestamps_bounded",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="source_attachment_history_reason_bounded",
        ),
        CheckConstraint(
            "(state = 'succeeded' AND failure_reason IS NULL) OR "
            "(state = 'failed' AND failure_reason IS NOT NULL)",
            name="source_attachment_history_state_consistent",
        ),
        Index(
            "ix_source_attachment_history_original_requested",
            "original_managed_instance_id",
            "requested_at",
        ),
        Index(
            "ix_source_attachment_history_source",
            "development_source_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    original_managed_instance_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    managed_instance_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("managed_instances.target_id", ondelete="SET NULL"),
        nullable=True,
    )
    development_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_logical_name: Mapped[str] = mapped_column(String(120), nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[DevelopmentSourceAttachmentOperationKind] = mapped_column(
        Enum(
            DevelopmentSourceAttachmentOperationKind,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="development_source_attachment_operation_kind",
        ),
        nullable=False,
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    completed_at: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[DevelopmentSourceAttachmentOperationState] = mapped_column(
        Enum(
            DevelopmentSourceAttachmentOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="development_source_attachment_operation_state",
        ),
        nullable=False,
    )
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)


class FixtureDefinitionRow(Base):
    """Finalized metadata for one trusted package-owned fixture version."""

    __tablename__ = "fixture_definitions"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_fixture_definitions_slug"),
        CheckConstraint("fixture_version > 0", name="fixture_version_positive"),
        CheckConstraint(
            "length(content_digest) = 71 "
            "AND substr(content_digest, 1, 7) = 'sha256:' "
            "AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'",
            name="fixture_content_digest_sha256",
        ),
        CheckConstraint(
            "length(trim(compatible_kodi_version)) BETWEEN 1 AND 32",
            name="fixture_compatible_release_bounded",
        ),
        CheckConstraint(
            "length(trim(description)) BETWEEN 1 AND 512",
            name="fixture_description_bounded",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    fixture_version: Mapped[int] = mapped_column(Integer, nullable=False)
    size_category: Mapped[FixtureSizeCategory] = mapped_column(
        Enum(
            FixtureSizeCategory,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="fixture_size_category",
            length=16,
        ),
        nullable=False,
    )
    content_category: Mapped[FixtureContentCategory] = mapped_column(
        Enum(
            FixtureContentCategory,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="fixture_content_category",
            length=16,
        ),
        nullable=False,
    )
    content_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    compatible_kodi_version: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)


class ResetOperationRow(Base):
    """Bounded durable coordination for one destructive disposable reset."""

    __tablename__ = "reset_operations"
    __table_args__ = (
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="reset_operation_failure_reason_bounded",
        ),
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="reset_operation_lease_digest_sha256",
        ),
        CheckConstraint(
            "(state = 'queued' AND started_at IS NULL AND completed_at IS NULL "
            "AND lease_token_digest IS NULL AND lease_expires_at IS NULL) OR "
            "(state = 'running' AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND heartbeat_at IS NOT NULL "
            "AND lease_token_digest IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state IN ('succeeded', 'failed', 'reconciliation_required') "
            "AND completed_at IS NOT NULL AND lease_token_digest IS NULL "
            "AND lease_expires_at IS NULL)",
            name="reset_operation_state_fields_consistent",
        ),
        Index(
            "uq_reset_operations_active_instance",
            "managed_instance_id",
            unique=True,
            sqlite_where=text("state IN ('queued', 'running')"),
        ),
        Index(
            "ix_reset_operations_instance_requested",
            "managed_instance_id",
            "requested_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    managed_instance_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[ResetOperationState] = mapped_column(
        Enum(
            ResetOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="reset_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[ResetOperationPhase] = mapped_column(
        Enum(
            ResetOperationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="reset_operation_phase",
        ),
        nullable=False,
    )
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class SnapshotRow(Base):
    """Immutable metadata for one centrally stored managed-instance snapshot."""

    __tablename__ = "snapshots"
    __table_args__ = (
        CheckConstraint("manifest_version = 1", name="snapshot_manifest_version_v1"),
        CheckConstraint(
            "(source_binding = 'attached' AND source_managed_instance_id IS NOT NULL "
            "AND original_source_managed_instance_id = source_managed_instance_id "
            "AND source_instance_name IS NULL AND source_deleted_at IS NULL) OR "
            "(source_binding = 'deleted_source' AND source_managed_instance_id IS NULL "
            "AND source_instance_name IS NOT NULL AND length(source_instance_name) > 0 "
            "AND source_deleted_at IS NOT NULL)",
            name="snapshot_source_binding_consistent",
        ),
        CheckConstraint(
            "label IS NULL OR length(label) BETWEEN 1 AND 480",
            name="snapshot_label_bounded",
        ),
        CheckConstraint(
            "archive_digest IS NULL OR (length(archive_digest) = 71 "
            "AND substr(archive_digest, 1, 7) = 'sha256:' "
            "AND substr(archive_digest, 8) NOT GLOB '*[^0-9a-f]*')",
            name="snapshot_archive_digest_sha256",
        ),
        CheckConstraint(
            "manifest_digest IS NULL OR (length(manifest_digest) = 71 "
            "AND substr(manifest_digest, 1, 7) = 'sha256:' "
            "AND substr(manifest_digest, 8) NOT GLOB '*[^0-9a-f]*')",
            name="snapshot_manifest_digest_sha256",
        ),
        CheckConstraint(
            "archive_size IS NULL OR archive_size >= 0",
            name="snapshot_archive_size_nonnegative",
        ),
        CheckConstraint(
            "member_count IS NULL OR member_count > 0",
            name="snapshot_member_count_positive",
        ),
        CheckConstraint(
            "(state = 'creating' AND validation_result = 'pending' "
            "AND validation_at IS NULL AND archive_digest IS NULL "
            "AND manifest_digest IS NULL AND archive_size IS NULL "
            "AND member_count IS NULL) OR "
            "(state = 'available' AND validation_result = 'passed' "
            "AND validation_at IS NOT NULL AND archive_digest IS NOT NULL "
            "AND manifest_digest IS NOT NULL AND archive_size IS NOT NULL "
            "AND member_count IS NOT NULL AND prior_runtime_state IS NOT NULL) OR "
            "(state IN ('failed', 'quarantined') "
            "AND validation_result = 'failed' AND validation_at IS NOT NULL) OR "
            "(state = 'deleting' AND validation_result IN ('passed', 'failed') "
            "AND validation_at IS NOT NULL)",
            name="snapshot_state_fields_consistent",
        ),
        Index(
            "ix_snapshots_instance_captured",
            "source_managed_instance_id",
            "captured_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_managed_instance_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("managed_instances.target_id", ondelete="RESTRICT"),
        nullable=True,
    )
    original_source_managed_instance_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    source_binding: Mapped[SnapshotSourceBinding] = mapped_column(
        Enum(
            SnapshotSourceBinding,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="snapshot_source_binding",
        ),
        nullable=False,
    )
    source_instance_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_deleted_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    runtime_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    captured_fixture_definition_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("fixture_definitions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    captured_at: Mapped[str] = mapped_column(String(32), nullable=False)
    prior_runtime_state: Mapped[InstanceRuntimeState | None] = mapped_column(
        Enum(
            InstanceRuntimeState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="snapshot_prior_runtime_state",
        ),
        nullable=True,
    )
    label: Mapped[str | None] = mapped_column(String(480), nullable=True)
    manifest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    archive_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    manifest_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    archive_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    member_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[SnapshotState] = mapped_column(
        Enum(
            SnapshotState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="snapshot_state",
        ),
        nullable=False,
    )
    validation_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    validation_result: Mapped[SnapshotValidationResult] = mapped_column(
        Enum(
            SnapshotValidationResult,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="snapshot_validation_result",
        ),
        nullable=False,
    )


class SnapshotCreateOperationRow(Base):
    """Durable create-only snapshot execution and recovery state."""

    __tablename__ = "snapshot_create_operations"
    __table_args__ = (
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="snapshot_create_failure_reason_bounded",
        ),
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="snapshot_create_lease_digest_sha256",
        ),
        CheckConstraint(
            "(state = 'queued' AND started_at IS NULL AND completed_at IS NULL "
            "AND lease_token_digest IS NULL AND lease_expires_at IS NULL) OR "
            "(state = 'running' AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND heartbeat_at IS NOT NULL "
            "AND lease_token_digest IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state IN ('succeeded', 'failed', 'reconciliation_required') "
            "AND completed_at IS NOT NULL AND lease_token_digest IS NULL "
            "AND lease_expires_at IS NULL)",
            name="snapshot_create_state_fields_consistent",
        ),
        Index(
            "uq_snapshot_create_active_instance",
            "managed_instance_id",
            unique=True,
            sqlite_where=text("state IN ('queued', 'running')"),
        ),
        Index(
            "ix_snapshot_create_instance_requested",
            "managed_instance_id",
            "requested_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    managed_instance_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[SnapshotCreateOperationState] = mapped_column(
        Enum(
            SnapshotCreateOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="snapshot_create_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[SnapshotCreateOperationPhase] = mapped_column(
        Enum(
            SnapshotCreateOperationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="snapshot_create_operation_phase",
            length=30,
        ),
        nullable=False,
    )
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class SnapshotRestoreOperationRow(Base):
    """Durable destructive snapshot-restore execution and recovery state."""

    __tablename__ = "snapshot_restore_operations"
    __table_args__ = (
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="snapshot_restore_failure_reason_bounded",
        ),
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="snapshot_restore_lease_digest_sha256",
        ),
        CheckConstraint(
            "old_container_id IS NULL OR (length(old_container_id) = 64 "
            "AND old_container_id NOT GLOB '*[^0-9a-f]*')",
            name="snapshot_restore_old_container_id",
        ),
        CheckConstraint(
            "fresh_container_id IS NULL OR (length(fresh_container_id) = 64 "
            "AND fresh_container_id NOT GLOB '*[^0-9a-f]*')",
            name="snapshot_restore_fresh_container_id",
        ),
        CheckConstraint(
            "(state = 'queued' AND started_at IS NULL AND completed_at IS NULL "
            "AND lease_token_digest IS NULL AND lease_expires_at IS NULL) OR "
            "(state = 'running' AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND heartbeat_at IS NOT NULL "
            "AND lease_token_digest IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state IN ('succeeded', 'failed', 'reconciliation_required') "
            "AND completed_at IS NOT NULL AND lease_token_digest IS NULL "
            "AND lease_expires_at IS NULL)",
            name="snapshot_restore_state_fields_consistent",
        ),
        Index(
            "uq_snapshot_restore_active_instance",
            "managed_instance_id",
            unique=True,
            sqlite_where=text("state IN ('queued', 'running')"),
        ),
        Index(
            "ix_snapshot_restore_instance_requested",
            "managed_instance_id",
            "requested_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    managed_instance_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    prior_source_runtime_state: Mapped[InstanceRuntimeState | None] = mapped_column(
        Enum(
            InstanceRuntimeState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="snapshot_restore_prior_runtime_state",
        ),
        nullable=True,
    )
    old_container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fresh_container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[SnapshotRestoreOperationState] = mapped_column(
        Enum(
            SnapshotRestoreOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="snapshot_restore_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[SnapshotRestoreOperationPhase] = mapped_column(
        Enum(
            SnapshotRestoreOperationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="snapshot_restore_operation_phase",
            length=32,
        ),
        nullable=False,
    )
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class SnapshotDeleteOperationRow(Base):
    """Durable explicit snapshot deletion history independent of Snapshot rows."""

    __tablename__ = "snapshot_delete_operations"
    __table_args__ = (
        CheckConstraint(
            "prior_snapshot_state IN ('available','failed','quarantined')",
            name="snapshot_delete_prior_state",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="snapshot_delete_failure_reason_bounded",
        ),
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="snapshot_delete_lease_digest_sha256",
        ),
        CheckConstraint(
            "(state = 'queued' AND started_at IS NULL AND completed_at IS NULL "
            "AND lease_token_digest IS NULL AND lease_expires_at IS NULL) OR "
            "(state = 'running' AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND heartbeat_at IS NOT NULL "
            "AND lease_token_digest IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state IN ('succeeded', 'failed', 'reconciliation_required') "
            "AND completed_at IS NOT NULL AND lease_token_digest IS NULL "
            "AND lease_expires_at IS NULL)",
            name="snapshot_delete_state_fields_consistent",
        ),
        Index(
            "uq_snapshot_delete_active_snapshot",
            "snapshot_id",
            unique=True,
            sqlite_where=text("state IN ('queued', 'running')"),
        ),
        Index(
            "ix_snapshot_delete_instance_requested",
            "managed_instance_id",
            "requested_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    managed_instance_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
    )
    prior_snapshot_state: Mapped[SnapshotState] = mapped_column(
        Enum(
            SnapshotState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            length=11,
        ),
        nullable=False,
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[SnapshotDeleteOperationState] = mapped_column(
        Enum(
            SnapshotDeleteOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="snapshot_delete_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[SnapshotDeleteOperationPhase] = mapped_column(
        Enum(
            SnapshotDeleteOperationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="snapshot_delete_operation_phase",
            length=24,
        ),
        nullable=False,
    )
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ManagedInstanceDeleteOperationRow(Base):
    """Durable disposable-instance deletion and retained historical identity."""

    __tablename__ = "managed_instance_delete_operations"
    __table_args__ = (
        CheckConstraint(
            "length(source_instance_name) BETWEEN 1 AND 256",
            name="instance_delete_source_name_bounded",
        ),
        CheckConstraint(
            "snapshot_count >= 0 AND completed_snapshot_count "
            "BETWEEN 0 AND snapshot_count",
            name="instance_delete_snapshot_progress_valid",
        ),
        CheckConstraint(
            "aggregate_snapshot_bytes >= 0",
            name="instance_delete_snapshot_bytes_nonnegative",
        ),
        CheckConstraint(
            "notes_at_deletion IS NULL OR length(notes_at_deletion) <= 4096",
            name="instance_delete_notes_bounded",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="instance_delete_failure_reason_bounded",
        ),
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="instance_delete_lease_digest_sha256",
        ),
        CheckConstraint(
            "expected_container_id IS NULL OR (length(expected_container_id) = 64 "
            "AND expected_container_id NOT GLOB '*[^0-9a-f]*')",
            name="instance_delete_expected_container_id",
        ),
        CheckConstraint(
            "observed_container_id IS NULL OR (length(observed_container_id) = 64 "
            "AND observed_container_id NOT GLOB '*[^0-9a-f]*')",
            name="instance_delete_observed_container_id",
        ),
        CheckConstraint(
            "(state = 'queued' AND started_at IS NULL AND completed_at IS NULL "
            "AND lease_token_digest IS NULL AND lease_expires_at IS NULL) OR "
            "(state = 'running' AND started_at IS NOT NULL AND completed_at IS NULL "
            "AND heartbeat_at IS NOT NULL AND lease_token_digest IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(state IN ('succeeded','failed','reconciliation_required') "
            "AND completed_at IS NOT NULL AND lease_token_digest IS NULL "
            "AND lease_expires_at IS NULL)",
            name="instance_delete_state_fields_consistent",
        ),
        Index(
            "uq_instance_delete_active_instance",
            "original_managed_instance_id",
            unique=True,
            sqlite_where=text("state IN ('queued', 'running')"),
        ),
        Index(
            "ix_instance_delete_original_requested",
            "original_managed_instance_id",
            "requested_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    original_managed_instance_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    source_instance_name: Mapped[str] = mapped_column(String(256), nullable=False)
    runtime_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    fixture_definition_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("fixture_definitions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    preserve_snapshots: Mapped[bool] = mapped_column(Boolean, nullable=False)
    snapshot_count: Mapped[int] = mapped_column(Integer, nullable=False)
    aggregate_snapshot_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    completed_snapshot_count: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prior_state: Mapped[ManagedInstanceDeletePriorState | None] = mapped_column(
        Enum(
            ManagedInstanceDeletePriorState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="managed_instance_delete_prior_state",
        ),
        nullable=True,
    )
    notes_at_deletion: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[ManagedInstanceDeleteOperationState] = mapped_column(
        Enum(
            ManagedInstanceDeleteOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="managed_instance_delete_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[ManagedInstanceDeleteOperationPhase] = mapped_column(
        Enum(
            ManagedInstanceDeleteOperationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="managed_instance_delete_operation_phase",
            length=32,
        ),
        nullable=False,
    )
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ManagedInstanceLifecycleOperationRow(Base):
    """Durable typed history for one synchronous lifecycle request."""

    __tablename__ = "managed_instance_lifecycle_operations"
    __table_args__ = (
        CheckConstraint(
            "length(instance_name) BETWEEN 1 AND 256",
            name="lifecycle_operation_instance_name_bounded",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="lifecycle_operation_failure_reason_bounded",
        ),
        CheckConstraint(
            "resulting_state IS NULL OR resulting_state IN "
            "('unprovisioned','stopped','running_usable','running_not_ready',"
            "'transitional','ambiguous')",
            name="lifecycle_operation_resulting_state_closed",
        ),
        CheckConstraint(
            "(state = 'running' AND completed_at IS NULL "
            "AND resulting_state IS NULL AND failure_reason IS NULL "
            "AND reconciliation_required = 0) OR "
            "(state = 'succeeded' AND completed_at IS NOT NULL "
            "AND resulting_state IS NOT NULL AND failure_reason IS NULL "
            "AND reconciliation_required = 0) OR "
            "(state = 'failed' AND completed_at IS NOT NULL "
            "AND resulting_state IS NOT NULL AND failure_reason IS NOT NULL "
            "AND reconciliation_required = 0) OR "
            "(state = 'reconciliation_required' AND completed_at IS NOT NULL "
            "AND resulting_state IS NOT NULL AND failure_reason IS NOT NULL "
            "AND reconciliation_required = 1)",
            name="lifecycle_operation_state_fields_consistent",
        ),
        Index(
            "uq_lifecycle_operation_running_instance",
            "original_managed_instance_id",
            unique=True,
            sqlite_where=text("state = 'running'"),
        ),
        Index(
            "ix_lifecycle_operation_original_requested",
            "original_managed_instance_id",
            "requested_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    original_managed_instance_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    managed_instance_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("managed_instances.target_id", ondelete="SET NULL"),
        nullable=True,
    )
    instance_name: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[ManagedInstanceLifecycleKind] = mapped_column(
        Enum(
            ManagedInstanceLifecycleKind,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="managed_instance_lifecycle_kind",
        ),
        nullable=False,
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[str] = mapped_column(String(32), nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prior_state: Mapped[ManagedInstanceSemanticState] = mapped_column(
        Enum(
            ManagedInstanceSemanticState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="managed_instance_lifecycle_semantic_state",
        ),
        nullable=False,
    )
    resulting_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    state: Mapped[ManagedInstanceLifecycleOperationState] = mapped_column(
        Enum(
            ManagedInstanceLifecycleOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="managed_instance_lifecycle_operation_state",
        ),
        nullable=False,
    )
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )


class ManagedInstanceMutationLeaseRow(Base):
    """One closed-kind mutation reservation or active lease per instance."""

    __tablename__ = "managed_instance_mutation_leases"
    __table_args__ = (
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="managed_mutation_lease_digest_sha256",
        ),
    )

    managed_instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("managed_instances.target_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    operation_kind: Mapped[ManagedInstanceMutationKind] = mapped_column(
        Enum(
            ManagedInstanceMutationKind,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="managed_instance_mutation_kind",
            length=32,
        ),
        nullable=False,
    )
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    acquired_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class GlobalHeavyOperationLeaseRow(Base):
    """Singleton reservation/lease for canonical one-heavy-operation execution."""

    __tablename__ = "global_heavy_operation_lease"
    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="global_heavy_singleton"),
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="global_heavy_lease_digest_sha256",
        ),
    )

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_kind: Mapped[GlobalHeavyOperationKind] = mapped_column(
        Enum(
            GlobalHeavyOperationKind,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="global_heavy_operation_kind",
        ),
        nullable=False,
    )
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    acquired_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class RuntimeDefinitionRow(Base):
    """Persisted logical runtime catalog identity."""

    __tablename__ = "runtime_definitions"
    __table_args__ = (
        CheckConstraint("length(trim(slug)) > 0", name="runtime_slug_not_blank"),
        CheckConstraint(
            "length(trim(kodi_version)) > 0",
            name="runtime_definition_kodi_version_not_blank",
        ),
        UniqueConstraint("slug", name="uq_runtime_definitions_slug"),
        CheckConstraint(
            "support_tier IN ('current', 'legacy', 'preview', 'retired')",
            name="runtime_support_tier",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    kodi_version: Mapped[str] = mapped_column(Text, nullable=False)
    support_tier: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=RuntimeSupportTier.CURRENT,
        server_default=text("'current'"),
    )


class RuntimeRevisionRow(Base):
    """Persisted finalized revision and its mutable lifecycle metadata."""

    __tablename__ = "runtime_revisions"
    __table_args__ = (
        UniqueConstraint(
            "runtime_definition_id",
            "revision_number",
            name="uq_runtime_revision_number",
        ),
        CheckConstraint("revision_number > 0", name="runtime_revision_number_positive"),
        CheckConstraint(
            "length(trim(source_kind)) > 0",
            name="runtime_revision_source_kind_not_blank",
        ),
        CheckConstraint(
            "length(trim(source_reference)) > 0",
            name="runtime_revision_source_reference_not_blank",
        ),
        CheckConstraint(
            "length(trim(manifest_reference)) > 0",
            name="runtime_revision_manifest_reference_not_blank",
        ),
        CheckConstraint(
            "length(content_digest) = 71 "
            "AND substr(content_digest, 1, 7) = 'sha256:' "
            "AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'",
            name="runtime_revision_content_digest_sha256",
        ),
        CheckConstraint(
            "length(manifest_digest) = 71 "
            "AND substr(manifest_digest, 1, 7) = 'sha256:' "
            "AND substr(manifest_digest, 8) NOT GLOB '*[^0-9a-f]*'",
            name="runtime_revision_manifest_digest_sha256",
        ),
        CheckConstraint(
            "build_log_reference IS NULL OR length(trim(build_log_reference)) > 0",
            name="runtime_revision_build_log_reference_not_blank",
        ),
        CheckConstraint(
            "length(trim(built_at)) > 0",
            name="runtime_revision_built_at_not_blank",
        ),
        CheckConstraint(
            "(approval_state = 'approved' AND approved_at IS NOT NULL "
            "AND length(trim(approved_at)) > 0) OR "
            "(approval_state = 'not_approved' AND approved_at IS NULL "
            "AND approval_note IS NULL)",
            name="runtime_revision_approval_consistent",
        ),
        CheckConstraint(
            "approval_note IS NULL OR length(trim(approval_note)) > 0",
            name="runtime_revision_approval_note_not_blank",
        ),
        CheckConstraint(
            "requires_trusted_build_confirmation = 1 "
            "OR trusted_build_confirmed_at IS NULL",
            name="runtime_revision_trust_confirmation_consistent",
        ),
        CheckConstraint(
            "trusted_build_confirmed_at IS NULL "
            "OR length(trim(trusted_build_confirmed_at)) > 0",
            name="runtime_revision_trust_timestamp_not_blank",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    runtime_definition_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runtime_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_reference: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    manifest_reference: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    built_at: Mapped[str] = mapped_column(Text, nullable=False)
    build_log_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_state: Mapped[RevisionApprovalState] = mapped_column(
        Enum(
            RevisionApprovalState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="revision_approval_state",
        ),
        nullable=False,
    )
    approved_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_trusted_build_confirmation: Mapped[bool] = mapped_column(
        Boolean(
            create_constraint=True, name="requires_trusted_build_confirmation_bool"
        ),
        nullable=False,
        default=False,
        server_default=text("0"),
    )
    trusted_build_confirmed_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class RuntimeContainerArtifactRow(Base):
    """One immutable Docker image artifact for a finalized runtime revision."""

    __tablename__ = "runtime_container_artifacts"
    __table_args__ = (
        CheckConstraint(
            "length(image_id) = 71 AND substr(image_id, 1, 7) = 'sha256:' "
            "AND substr(image_id, 8) NOT GLOB '*[^0-9a-f]*'",
            name="runtime_container_artifact_image_id_sha256",
        ),
    )

    runtime_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    image_id: Mapped[str] = mapped_column(String(71), nullable=False)


class RuntimeExecutionProfileRow(Base):
    """Immutable trusted execution profile for one finalized revision."""

    __tablename__ = "runtime_execution_profiles"
    __table_args__ = (
        CheckConstraint(
            "expected_version_tag IN ('stable', 'alpha', 'beta', 'rc')",
            name="runtime_profile_release_channel",
        ),
        CheckConstraint(
            "(expected_version_tag = 'stable' AND "
            "expected_prerelease_ordinal IS NULL) OR "
            "(expected_version_tag != 'stable' AND "
            "expected_prerelease_ordinal > 0)",
            name="runtime_profile_release_ordinal",
        ),
        CheckConstraint(
            "jsonrpc_port BETWEEN 1 AND 65535",
            name="runtime_profile_jsonrpc_port_range",
        ),
        CheckConstraint(
            "readiness_timeout_seconds BETWEEN 1 AND 600",
            name="runtime_profile_total_timeout_range",
        ),
        CheckConstraint(
            "poll_interval_seconds >= 0.1 AND poll_interval_seconds <= 10.0",
            name="runtime_profile_poll_interval_range",
        ),
        CheckConstraint(
            "http_timeout_seconds >= 0.1 AND http_timeout_seconds <= 5.0",
            name="runtime_profile_http_timeout_range",
        ),
    )

    runtime_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    runtime_kind: Mapped[RuntimeKind] = mapped_column(
        Enum(RuntimeKind, values_callable=_enum_values, native_enum=False),
        nullable=False,
    )
    expected_application_name: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_kodi_version: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_version_tag: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_prerelease_ordinal: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    jsonrpc_port: Mapped[int] = mapped_column(Integer, nullable=False)
    jsonrpc_path: Mapped[str] = mapped_column(String(64), nullable=False)
    process_observation: Mapped[ProcessObservationKind] = mapped_column(
        Enum(ProcessObservationKind, values_callable=_enum_values, native_enum=False),
        nullable=False,
    )
    readiness_probe: Mapped[ReadinessProbeKind] = mapped_column(
        Enum(ReadinessProbeKind, values_callable=_enum_values, native_enum=False),
        nullable=False,
    )
    readiness_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    poll_interval_seconds: Mapped[float] = mapped_column(nullable=False)
    http_timeout_seconds: Mapped[float] = mapped_column(nullable=False)


class RuntimeValidationEvidenceRow(Base):
    """Append-only validation evidence for one finalized revision."""

    __tablename__ = "runtime_validation_evidence"
    __table_args__ = (
        Index(
            "ix_runtime_validation_effective",
            "runtime_revision_id",
            "contract_version",
            "recorded_at",
        ),
        CheckConstraint(
            "length(contract_version) BETWEEN 1 AND 64",
            name="runtime_validation_contract_bounded",
        ),
        CheckConstraint(
            "length(trim(evidence_reference)) BETWEEN 1 AND 512",
            name="runtime_validation_reference_bounded",
        ),
        CheckConstraint(
            "summary IS NULL OR length(trim(summary)) BETWEEN 1 AND 512",
            name="runtime_validation_summary_bounded",
        ),
        CheckConstraint(
            "completed_at >= started_at AND recorded_at >= completed_at",
            name="runtime_validation_timestamp_order",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    runtime_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[RuntimeValidationResult] = mapped_column(
        Enum(
            RuntimeValidationResult,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="runtime_validation_result",
        ),
        nullable=False,
    )
    started_at: Mapped[str] = mapped_column(String(27), nullable=False)
    completed_at: Mapped[str] = mapped_column(String(27), nullable=False)
    evidence_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    recorded_at: Mapped[str] = mapped_column(String(27), nullable=False)


class WebAdminCredentialRow(Base):
    """The single local administrator credential."""

    __tablename__ = "web_admin_credentials"
    __table_args__ = (
        CheckConstraint("admin_key = 'local-admin'", name="web_admin_single_identity"),
        CheckConstraint(
            "length(trim(password_hash)) > 0", name="web_admin_password_hash_not_blank"
        ),
        CheckConstraint(
            "length(password_updated_at) = 27",
            name="web_admin_password_updated_at_format",
        ),
    )

    admin_key: Mapped[str] = mapped_column(String(11), primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    password_updated_at: Mapped[str] = mapped_column(String(27), nullable=False)


class WebSessionRow(Base):
    """A server-side Web session addressed by a bearer-token digest."""

    __tablename__ = "web_sessions"
    __table_args__ = (
        CheckConstraint(
            "length(token_digest) = 64 AND token_digest NOT GLOB '*[^0-9a-f]*'",
            name="web_session_token_digest_sha256",
        ),
        CheckConstraint("admin_key = 'local-admin'", name="web_session_single_admin"),
        CheckConstraint(
            "length(csrf_token) = 43", name="web_session_csrf_token_length"
        ),
        CheckConstraint(
            "length(created_at) = 27 AND length(last_used_at) = 27 "
            "AND length(absolute_expires_at) = 27",
            name="web_session_timestamp_format",
        ),
    )

    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_key: Mapped[str] = mapped_column(
        String(11),
        ForeignKey("web_admin_credentials.admin_key", ondelete="RESTRICT"),
        nullable=False,
    )
    csrf_token: Mapped[str] = mapped_column(String(43), nullable=False)
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    last_used_at: Mapped[str] = mapped_column(String(27), nullable=False)
    absolute_expires_at: Mapped[str] = mapped_column(String(27), nullable=False)
