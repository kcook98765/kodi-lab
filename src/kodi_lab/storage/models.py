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
    AddonRepositoryAttachmentKind,
    AddonRepositoryAttachmentState,
    AddonRepositoryCandidateClassification,
    AddonRepositoryCandidateSupport,
    AddonRepositoryFetchPhase,
    AddonRepositoryOperationState,
    AddonRepositoryOrigin,
    AddonRepositoryRefreshPhase,
    AddonZipEnablementOperationPhase,
    AddonZipEnablementOperationState,
    AddonZipEnablementResult,
    AddonZipExtensionClassification,
    AddonZipImportOperationPhase,
    AddonZipImportOperationState,
    AddonZipInstallationConfidence,
    AddonZipInstallationOperationPhase,
    AddonZipInstallationOperationState,
    AddonZipInstallationResult,
    AddonZipOrigin,
    AddonZipPayloadVerificationOutcome,
    AddonZipSmokeTestExecutionResult,
    AddonZipSmokeTestOperationPhase,
    AddonZipSmokeTestOperationState,
    AddonZipSmokeTestOverallResult,
    AddonZipSmokeTestPostProvenance,
    AddonZipValidationState,
    AgentWorkspaceCreationPhase,
    AgentWorkspaceCreationState,
    AgentWorkspaceMutationKind,
    AgentWorkspacePromotionChangeKind,
    AgentWorkspacePromotionJournalState,
    AgentWorkspacePromotionOperationState,
    AgentWorkspacePromotionPhase,
    AgentWorkspacePromotionPlanState,
    AgentWorkspaceState,
    ConnectionState,
    DevelopmentSourceAttachmentOperationKind,
    DevelopmentSourceAttachmentOperationState,
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentMode,
    DevelopmentSourceDeploymentOperationPhase,
    DevelopmentSourceDeploymentOperationState,
    DevelopmentSourceDeploymentRequestKind,
    DevelopmentSourceDeploymentResult,
    DevelopmentSourceEnablementOperationPhase,
    DevelopmentSourceEnablementOperationState,
    DevelopmentSourceEnablementResult,
    DevelopmentSourceKind,
    DevelopmentSourceSmokeTestExecutionResult,
    DevelopmentSourceSmokeTestOperationPhase,
    DevelopmentSourceSmokeTestOperationState,
    DevelopmentSourceSmokeTestOverallResult,
    DevelopmentSourceSmokeTestPostProvenance,
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
    PromotionLogResult,
    PromotionVerificationPhase,
    PromotionVerificationStageResult,
    PromotionVerificationState,
    PythonBytecodePolicy,
    ReadinessProbeKind,
    RepositoryReplacementIncumbentClass,
    RepositoryReplacementOperationPhase,
    RepositoryReplacementOperationState,
    RepositoryReplacementPlanState,
    RepositoryReplacementTerminalOutcome,
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


class AgentWorkspaceRow(Base):
    """Path-free durable identity for one protected working copy."""

    __tablename__ = "agent_workspaces"
    __table_args__ = (
        CheckConstraint(
            "generation >= 0", name="agent_workspace_generation_nonnegative"
        ),
        CheckConstraint(
            "length(source_addon_id) BETWEEN 1 AND 128 AND "
            "length(source_logical_name) BETWEEN 1 AND 120 AND "
            "length(source_observed_name) BETWEEN 1 AND 256 AND "
            "length(source_observed_version) BETWEEN 1 AND 128",
            name="agent_workspace_source_snapshot_bounded",
        ),
        CheckConstraint(
            "(baseline_manifest_digest IS NULL OR "
            "(length(baseline_manifest_digest)=71 AND "
            "substr(baseline_manifest_digest,1,7)='sha256:')) AND "
            "(current_tree_digest IS NULL OR "
            "(length(current_tree_digest)=71 AND "
            "substr(current_tree_digest,1,7)='sha256:'))",
            name="agent_workspace_digests_valid",
        ),
        CheckConstraint(
            "state <> 'ready' OR (baseline_manifest_digest IS NOT NULL AND "
            "current_tree_digest IS NOT NULL AND captured_at IS NOT NULL)",
            name="agent_workspace_ready_complete",
        ),
        Index(
            "ix_agent_workspace_source_created", "development_source_id", "created_at"
        ),
        Index("ix_agent_workspace_state", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    development_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_logical_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_observed_name: Mapped[str] = mapped_column(String(256), nullable=False)
    source_observed_version: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[AgentWorkspaceState] = mapped_column(
        Enum(
            AgentWorkspaceState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="agent_workspace_state",
        ),
        nullable=False,
    )
    baseline_artifact_id: Mapped[str] = mapped_column(
        String(36), nullable=False, unique=True
    )
    baseline_manifest_digest: Mapped[str | None] = mapped_column(String(71))
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_tree_digest: Mapped[str | None] = mapped_column(String(71))
    capture_started_at: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class AgentWorkspaceCreationOperationRow(Base):
    """Bounded typed history for one asynchronous workspace creation."""

    __tablename__ = "agent_workspace_creation_operations"
    __table_args__ = (
        CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128",
            name="agent_workspace_creation_failure_bounded",
        ),
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest)=64 AND "
            "lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="agent_workspace_creation_lease_digest",
        ),
        CheckConstraint(
            "(state IN ('queued','running') AND completed_at IS NULL AND "
            "failure_code IS NULL) OR (state='succeeded' AND completed_at IS NOT NULL "
            "AND failure_code IS NULL) OR "
            "(state IN ('failed','reconciliation_required') "
            "AND completed_at IS NOT NULL AND failure_code IS NOT NULL)",
            name="agent_workspace_creation_terminal_consistent",
        ),
        Index(
            "ix_agent_workspace_creation_source_requested",
            "development_source_id",
            "requested_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_workspaces.id", ondelete="RESTRICT"), unique=True
    )
    development_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("development_sources.id", ondelete="RESTRICT")
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(32))
    completed_at: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[AgentWorkspaceCreationState] = mapped_column(
        Enum(
            AgentWorkspaceCreationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="agent_workspace_creation_state",
        ),
        nullable=False,
    )
    phase: Mapped[AgentWorkspaceCreationPhase] = mapped_column(
        Enum(
            AgentWorkspaceCreationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="agent_workspace_creation_phase",
        ),
        nullable=False,
    )
    failure_code: Mapped[str | None] = mapped_column(String(128))
    lease_token_digest: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[str | None] = mapped_column(String(32))


class AgentWorkspaceMutationEventRow(Base):
    """Content-free synchronous history for one published workspace mutation."""

    __tablename__ = "agent_workspace_mutation_events"
    __table_args__ = (
        CheckConstraint(
            "generation >= 1", name="agent_workspace_mutation_generation_positive"
        ),
        CheckConstraint(
            "length(relative_path) BETWEEN 1 AND 512",
            name="agent_workspace_mutation_path_bounded",
        ),
        UniqueConstraint(
            "workspace_id", "generation", name="uq_workspace_mutation_generation"
        ),
        Index("ix_agent_workspace_mutation_occurred", "workspace_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_workspaces.id", ondelete="RESTRICT")
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[AgentWorkspaceMutationKind] = mapped_column(
        Enum(
            AgentWorkspaceMutationKind,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="agent_workspace_mutation_kind",
        ),
        nullable=False,
    )
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    occurred_at: Mapped[str] = mapped_column(String(32), nullable=False)


class AgentWorkspacePromotionPlanRow(Base):
    """Private exact review plan bound to workspace, source, and future target."""

    __tablename__ = "agent_workspace_promotion_plans"
    __table_args__ = (
        CheckConstraint(
            "workspace_generation >= 0", name="workspace_promotion_generation"
        ),
        CheckConstraint(
            "changed_file_count BETWEEN 1 AND 100 AND "
            "review_text_bytes BETWEEN 0 AND 4194304",
            name="workspace_promotion_review_bounds",
        ),
        CheckConstraint(
            "length(workspace_digest)=71 AND length(baseline_digest)=71 AND "
            "length(source_capture_digest)=71 AND length(target_source_digest)=71 "
            "AND length(target_destination_digest)=71",
            name="workspace_promotion_digest_bounds",
        ),
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(current_version) BETWEEN 1 AND 128 AND "
            "length(promoted_name) BETWEEN 1 AND 256 AND "
            "length(promoted_version) BETWEEN 1 AND 128 AND "
            "length(target_container_id)=64",
            name="workspace_promotion_identity_bounds",
        ),
        CheckConstraint(
            "invalidation_reason IS NULL OR "
            "length(invalidation_reason) BETWEEN 1 AND 128",
            name="workspace_promotion_invalidation_bounded",
        ),
        Index(
            "uq_workspace_promotion_active_workspace",
            "workspace_id",
            unique=True,
            sqlite_where=text(
                "state IN ('preparing','reviewable','approved_and_queued')"
            ),
        ),
        Index(
            "ix_workspace_promotion_source_created",
            "development_source_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    development_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("managed_instances.target_id", ondelete="RESTRICT"),
        nullable=False,
    )
    workspace_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    baseline_artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    baseline_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    source_capture_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_version: Mapped[str] = mapped_column(String(128), nullable=False)
    promoted_name: Mapped[str] = mapped_column(String(256), nullable=False)
    promoted_version: Mapped[str] = mapped_column(String(128), nullable=False)
    target_runtime_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_container_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_deployment_operation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_source_deployment_operations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_source_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    target_destination_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    expected_enabled: Mapped[bool | None] = mapped_column(Boolean)
    changed_file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    review_text_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewable_at: Mapped[str | None] = mapped_column(String(32))
    expires_at: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[AgentWorkspacePromotionPlanState] = mapped_column(
        Enum(
            AgentWorkspacePromotionPlanState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="agent_workspace_promotion_plan_state",
        ),
        nullable=False,
    )
    source_operation_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    invalidation_reason: Mapped[str | None] = mapped_column(String(128))


class AgentWorkspacePromotionPlanPathRow(Base):
    """Private complete review material and exact affected-path identities."""

    __tablename__ = "agent_workspace_promotion_plan_paths"
    __table_args__ = (
        CheckConstraint(
            "ordinal BETWEEN 0 AND 99", name="workspace_promotion_path_ordinal"
        ),
        CheckConstraint(
            "length(relative_path) BETWEEN 1 AND 512",
            name="workspace_promotion_path_bounded",
        ),
        CheckConstraint(
            "length(post_sha256)=64 AND post_sha256 NOT GLOB '*[^0-9a-f]*' AND "
            "(pre_sha256 IS NULL OR (length(pre_sha256)=64 AND "
            "pre_sha256 NOT GLOB '*[^0-9a-f]*'))",
            name="workspace_promotion_path_hashes",
        ),
        CheckConstraint(
            "(change_kind='add' AND pre_sha256 IS NULL AND pre_size IS NULL AND "
            "pre_mode IS NULL AND before_text IS NULL) OR "
            "(change_kind='modify' AND pre_sha256 IS NOT NULL AND "
            "pre_size IS NOT NULL AND pre_mode IS NOT NULL AND "
            "before_text IS NOT NULL)",
            name="workspace_promotion_path_preimage_shape",
        ),
        UniqueConstraint(
            "plan_id", "relative_path", name="uq_workspace_promotion_path"
        ),
    )

    plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_workspace_promotion_plans.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    change_kind: Mapped[AgentWorkspacePromotionChangeKind] = mapped_column(
        Enum(
            AgentWorkspacePromotionChangeKind,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="agent_workspace_promotion_change_kind",
        ),
        nullable=False,
    )
    pre_sha256: Mapped[str | None] = mapped_column(String(64))
    pre_size: Mapped[int | None] = mapped_column(BigInteger)
    pre_mode: Mapped[int | None] = mapped_column(Integer)
    post_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    post_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    post_mode: Mapped[int] = mapped_column(Integer, nullable=False)
    before_text: Mapped[str | None] = mapped_column(Text)
    after_text: Mapped[str] = mapped_column(Text, nullable=False)
    conflict_reason: Mapped[str | None] = mapped_column(String(64))


class AgentWorkspacePromotionOperationRow(Base):
    """One permanently single-use source-application attempt."""

    __tablename__ = "agent_workspace_promotion_operations"
    __table_args__ = (
        CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128",
            name="workspace_promotion_failure_bounded",
        ),
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest)=64 AND "
            "lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="workspace_promotion_lease_digest",
        ),
        CheckConstraint(
            "(state='queued' AND started_at IS NULL AND completed_at IS NULL AND "
            "lease_token_digest IS NULL AND lease_expires_at IS NULL) OR "
            "(state='running' AND started_at IS NOT NULL AND completed_at IS NULL "
            "AND lease_token_digest IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state IN ('succeeded','failed','reconciliation_required') AND "
            "completed_at IS NOT NULL AND lease_token_digest IS NULL AND "
            "lease_expires_at IS NULL)",
            name="workspace_promotion_operation_state_fields",
        ),
        CheckConstraint(
            "application_started=0 OR (backup_verified=1 AND "
            "backup_artifact_id IS NOT NULL)",
            name="workspace_promotion_marker_requires_backup",
        ),
        Index(
            "uq_workspace_promotion_active_source",
            "development_source_id",
            unique=True,
            sqlite_where=text("state IN ('queued','running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_workspace_promotion_plans.id", ondelete="RESTRICT"),
        unique=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    development_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("managed_instances.target_id", ondelete="RESTRICT"),
        nullable=False,
    )
    state: Mapped[AgentWorkspacePromotionOperationState] = mapped_column(
        Enum(
            AgentWorkspacePromotionOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="agent_workspace_promotion_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[AgentWorkspacePromotionPhase] = mapped_column(
        Enum(
            AgentWorkspacePromotionPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="agent_workspace_promotion_phase",
        ),
        nullable=False,
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    application_started: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    backup_artifact_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    backup_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    started_at: Mapped[str | None] = mapped_column(String(32))
    completed_at: Mapped[str | None] = mapped_column(String(32))
    failure_code: Mapped[str | None] = mapped_column(String(128))
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    lease_token_digest: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[str | None] = mapped_column(String(32))


class AgentWorkspacePromotionJournalRow(Base):
    """Content-free per-path source application progress."""

    __tablename__ = "agent_workspace_promotion_journal"
    __table_args__ = (
        UniqueConstraint(
            "operation_id", "relative_path", name="uq_workspace_promotion_journal_path"
        ),
        CheckConstraint(
            "(state='applied' AND applied_device IS NOT NULL AND "
            "applied_inode IS NOT NULL) OR (state!='applied' AND "
            "applied_device IS NULL AND applied_inode IS NULL)",
            name="workspace_promotion_journal_identity_shape",
        ),
    )

    operation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_workspace_promotion_operations.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[AgentWorkspacePromotionJournalState] = mapped_column(
        Enum(
            AgentWorkspacePromotionJournalState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="agent_workspace_promotion_journal_state",
        ),
        nullable=False,
    )
    applied_device: Mapped[int | None] = mapped_column(BigInteger)
    applied_inode: Mapped[int | None] = mapped_column(BigInteger)


class AgentWorkspacePromotionHandoffRow(Base):
    """Private exact Slice-4 sync handoff; never current deployment evidence."""

    __tablename__ = "agent_workspace_promotion_handoffs"
    __table_args__ = (
        CheckConstraint(
            "(verification_operation_id IS NULL AND consumed_at IS NULL) OR "
            "(verification_operation_id IS NOT NULL AND expected_addon_id IS NOT NULL "
            "AND expected_name IS NOT NULL AND expected_version IS NOT NULL "
            "AND expected_enabled IS NOT NULL)",
            name="promotion_handoff_verification_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_workspace_promotion_operations.id", ondelete="RESTRICT"),
        unique=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_workspaces.id", ondelete="RESTRICT")
    )
    development_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("development_sources.id", ondelete="RESTRICT")
    )
    target_instance_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("managed_instances.target_id", ondelete="RESTRICT")
    )
    runtime_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runtime_revisions.id", ondelete="RESTRICT")
    )
    trusted_container_id: Mapped[str] = mapped_column(String(64), nullable=False)
    prior_deployment_operation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_source_deployment_operations.id", ondelete="RESTRICT"),
    )
    prior_source_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    prior_destination_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_addon_id: Mapped[str | None] = mapped_column(String(128))
    expected_name: Mapped[str | None] = mapped_column(String(256))
    expected_version: Mapped[str | None] = mapped_column(String(128))
    expected_enabled: Mapped[bool | None] = mapped_column(Boolean)
    verification_operation_id: Mapped[str | None] = mapped_column(
        String(36), unique=True
    )
    consumed_at: Mapped[str | None] = mapped_column(String(32))


class PromotionVerificationOperationRow(Base):
    """One durable, single-target post-promotion verification."""

    __tablename__ = "promotion_verification_operations"
    __table_args__ = (
        CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128",
            name="promotion_verification_failure_bounded",
        ),
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest)=64 AND "
            "lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="promotion_verification_lease_digest",
        ),
        CheckConstraint(
            "(state='queued' AND started_at IS NULL AND completed_at IS NULL AND "
            "lease_token_digest IS NULL AND lease_expires_at IS NULL) OR "
            "(state='running' AND started_at IS NOT NULL AND completed_at IS NULL "
            "AND lease_token_digest IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state IN ('succeeded','failed','incomplete',"
            "'reconciliation_required') AND completed_at IS NOT NULL AND "
            "lease_token_digest IS NULL AND lease_expires_at IS NULL)",
            name="promotion_verification_state_fields",
        ),
        CheckConstraint(
            "sync_completed=0 OR sync_started=1",
            name="promotion_verification_sync_order",
        ),
        CheckConstraint(
            "handoff_consumed=sync_completed",
            name="promotion_verification_consumption_order",
        ),
        CheckConstraint(
            "start_dispatched=0 OR sync_completed=1",
            name="promotion_verification_start_order",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_promotion_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_workspace_promotion_operations.id", ondelete="RESTRICT"),
        unique=True,
    )
    handoff_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_workspace_promotion_handoffs.id", ondelete="RESTRICT"),
        unique=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_workspaces.id", ondelete="RESTRICT")
    )
    development_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("development_sources.id", ondelete="RESTRICT")
    )
    target_instance_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("managed_instances.target_id", ondelete="RESTRICT")
    )
    runtime_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runtime_revisions.id", ondelete="RESTRICT")
    )
    deployment_operation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_source_deployment_operations.id", ondelete="RESTRICT"),
        unique=True,
    )
    state: Mapped[PromotionVerificationState] = mapped_column(
        Enum(
            PromotionVerificationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="promotion_verification_state",
        )
    )
    phase: Mapped[PromotionVerificationPhase] = mapped_column(
        Enum(
            PromotionVerificationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="promotion_verification_phase",
        )
    )
    addon_id: Mapped[str] = mapped_column(String(128))
    expected_name: Mapped[str] = mapped_column(String(256))
    expected_version: Mapped[str] = mapped_column(String(128))
    expected_enabled: Mapped[bool] = mapped_column(Boolean)
    sync_result: Mapped[PromotionVerificationStageResult] = mapped_column(
        Enum(
            PromotionVerificationStageResult,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="promotion_verification_stage_result",
        )
    )
    reload_result: Mapped[PromotionVerificationStageResult] = mapped_column(
        Enum(
            PromotionVerificationStageResult,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            name="promotion_verification_stage_result",
        )
    )
    inventory_result: Mapped[PromotionVerificationStageResult] = mapped_column(
        Enum(
            PromotionVerificationStageResult,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=False,
            name="promotion_verification_stage_result",
        )
    )
    log_result: Mapped[PromotionLogResult] = mapped_column(
        Enum(
            PromotionLogResult,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="promotion_log_result",
        )
    )
    current_version: Mapped[str | None] = mapped_column(String(128))
    current_enabled: Mapped[bool | None] = mapped_column(Boolean)
    target_error_count: Mapped[int] = mapped_column(Integer, default=0)
    unrelated_warning_count: Mapped[int] = mapped_column(Integer, default=0)
    stop_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    sync_started: Mapped[bool] = mapped_column(Boolean, default=False)
    sync_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    start_dispatched: Mapped[bool] = mapped_column(Boolean, default=False)
    handoff_consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    requested_at: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[str | None] = mapped_column(String(32))
    completed_at: Mapped[str | None] = mapped_column(String(32))
    failure_code: Mapped[str | None] = mapped_column(String(128))
    lease_token_digest: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[str | None] = mapped_column(String(32))


class PromotionVerificationLogEvidenceRow(Base):
    """Private bounded snippets; deliberately separate from ordinary history."""

    __tablename__ = "promotion_verification_log_evidence"
    __table_args__ = (
        CheckConstraint(
            "length(target_error_snippets_json) <= 12288",
            name="promotion_verification_private_log_bounds",
        ),
    )

    verification_operation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("promotion_verification_operations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    target_error_snippets_json: Mapped[str] = mapped_column(Text, default="[]")


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


class DevelopmentSourceDeploymentRow(Base):
    """Last verified copy deployment evidence for one instance/source pair."""

    __tablename__ = "development_source_deployments"
    __table_args__ = (
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128",
            name="development_source_deployment_addon_id_bounded",
        ),
        CheckConstraint(
            "length(source_manifest_digest) = 71 AND "
            "substr(source_manifest_digest, 1, 7) = 'sha256:' AND "
            "substr(source_manifest_digest, 8) NOT GLOB '*[^0-9a-f]*'",
            name="development_source_deployment_source_digest",
        ),
        CheckConstraint(
            "length(verified_destination_digest) = 71 AND "
            "substr(verified_destination_digest, 1, 7) = 'sha256:' AND "
            "substr(verified_destination_digest, 8) NOT GLOB '*[^0-9a-f]*'",
            name="development_source_deployment_destination_digest",
        ),
        CheckConstraint(
            "length(trusted_container_id) = 64 AND "
            "trusted_container_id NOT GLOB '*[^0-9a-f]*'",
            name="development_source_deployment_container_id",
        ),
        CheckConstraint(
            "length(observed_source_name) BETWEEN 1 AND 256 AND "
            "length(observed_source_version) BETWEEN 1 AND 128",
            name="development_source_deployment_observation_bounded",
        ),
        CheckConstraint(
            "length(verified_at) BETWEEN 20 AND 32",
            name="development_source_deployment_verified_at_bounded",
        ),
        Index(
            "ix_development_source_deployment_source",
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
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[DevelopmentSourceDeploymentMode] = mapped_column(
        Enum(
            DevelopmentSourceDeploymentMode,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="development_source_deployment_mode",
        ),
        nullable=False,
    )
    last_successful_operation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_source_deployment_operations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_manifest_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    verified_destination_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    runtime_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    trusted_container_id: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_source_name: Mapped[str] = mapped_column(String(256), nullable=False)
    observed_source_version: Mapped[str] = mapped_column(String(128), nullable=False)
    verified_at: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[DevelopmentSourceDeploymentConfidence] = mapped_column(
        Enum(
            DevelopmentSourceDeploymentConfidence,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="development_source_deployment_confidence",
        ),
        nullable=False,
    )


class DevelopmentSourceDeploymentOperationRow(Base):
    """Durable copy/sync operation history and exact coordination ownership."""

    __tablename__ = "development_source_deployment_operations"
    __table_args__ = (
        CheckConstraint(
            "length(source_logical_name) BETWEEN 1 AND 120 AND "
            "length(addon_id) BETWEEN 1 AND 128",
            name="development_source_deployment_history_identity_bounded",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="development_source_deployment_failure_reason_bounded",
        ),
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="development_source_deployment_lease_digest",
        ),
        CheckConstraint(
            "expected_container_id IS NULL OR (length(expected_container_id) = 64 "
            "AND expected_container_id NOT GLOB '*[^0-9a-f]*')",
            name="development_source_deployment_expected_container",
        ),
        CheckConstraint(
            "(new_count IS NULL OR new_count >= 0) AND "
            "(changed_count IS NULL OR changed_count >= 0) AND "
            "(unchanged_count IS NULL OR unchanged_count >= 0) AND "
            "(destination_only_count IS NULL OR destination_only_count >= 0) AND "
            "(transferred_bytes IS NULL OR transferred_bytes >= 0)",
            name="development_source_deployment_aggregates_nonnegative",
        ),
        Index(
            "uq_development_source_deployment_active_instance",
            "managed_instance_id",
            unique=True,
            sqlite_where=text("state IN ('queued', 'running')"),
        ),
        Index(
            "ix_development_source_deployment_original_requested",
            "original_managed_instance_id",
            "requested_at",
        ),
        Index(
            "ix_development_source_deployment_operation_source",
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
    runtime_revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_kind: Mapped[DevelopmentSourceDeploymentRequestKind] = mapped_column(
        Enum(
            DevelopmentSourceDeploymentRequestKind,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="development_source_deployment_request_kind",
        ),
        nullable=False,
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[DevelopmentSourceDeploymentOperationState] = mapped_column(
        Enum(
            DevelopmentSourceDeploymentOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="development_source_deployment_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[DevelopmentSourceDeploymentOperationPhase] = mapped_column(
        Enum(
            DevelopmentSourceDeploymentOperationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="development_source_deployment_operation_phase",
            length=24,
        ),
        nullable=False,
    )
    result_classification: Mapped[DevelopmentSourceDeploymentResult | None] = (
        mapped_column(
            Enum(
                DevelopmentSourceDeploymentResult,
                values_callable=_enum_values,
                native_enum=False,
                create_constraint=True,
                name="development_source_deployment_result",
            ),
            nullable=True,
        )
    )
    new_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    changed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unchanged_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    destination_only_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transferred_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expected_container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class DevelopmentSourceEnablementOperationRow(Base):
    """Typed synchronous copied-source enablement history."""

    __tablename__ = "development_source_enablement_operations"
    __table_args__ = (
        CheckConstraint(
            "length(source_logical_name) BETWEEN 1 AND 120 AND "
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 128",
            name="development_source_enablement_identity_bounded",
        ),
        CheckConstraint(
            "blocked_dependency_id IS NULL OR "
            "length(blocked_dependency_id) BETWEEN 1 AND 128",
            name="development_source_enablement_blocked_id_bounded",
        ),
        CheckConstraint(
            "blocked_dependency_reason IS NULL OR "
            "length(blocked_dependency_reason) BETWEEN 1 AND 64",
            name="development_source_enablement_blocked_reason_bounded",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="development_source_enablement_failure_bounded",
        ),
        Index(
            "uq_development_source_enablement_active_instance",
            "managed_instance_id",
            unique=True,
            sqlite_where=text("state = 'running'"),
        ),
        Index(
            "ix_development_source_enablement_original_requested",
            "original_managed_instance_id",
            "requested_at",
        ),
        Index(
            "ix_development_source_enablement_source",
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
        String(36), ForeignKey("development_sources.id", ondelete="RESTRICT")
    )
    source_logical_name: Mapped[str] = mapped_column(String(120), nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    addon_version: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runtime_revisions.id", ondelete="RESTRICT")
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[DevelopmentSourceEnablementOperationState] = mapped_column(
        Enum(
            DevelopmentSourceEnablementOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="development_source_enablement_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[DevelopmentSourceEnablementOperationPhase] = mapped_column(
        Enum(
            DevelopmentSourceEnablementOperationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="development_source_enablement_operation_phase",
        ),
        nullable=False,
    )
    result_classification: Mapped[DevelopmentSourceEnablementResult | None] = (
        mapped_column(
            Enum(
                DevelopmentSourceEnablementResult,
                values_callable=_enum_values,
                native_enum=False,
                create_constraint=True,
                name="development_source_enablement_result",
            ),
            nullable=True,
        )
    )
    prior_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    enable_performed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    blocked_dependency_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    blocked_dependency_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )


class DevelopmentSourceSmokeTestOperationRow(Base):
    """Durable bounded copied-source smoke-test history and coordination."""

    __tablename__ = "development_source_smoke_test_operations"
    __table_args__ = (
        CheckConstraint(
            "length(source_logical_name) BETWEEN 1 AND 120 AND "
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 128",
            name="development_source_smoke_test_identity_bounded",
        ),
        CheckConstraint(
            "invocation_duration_ms IS NULL OR "
            "invocation_duration_ms BETWEEN 0 AND 90000",
            name="development_source_smoke_test_duration_bounded",
        ),
        CheckConstraint(
            "returned_item_count IS NULL OR returned_item_count BETWEEN 0 AND 1",
            name="development_source_smoke_test_item_count_bounded",
        ),
        CheckConstraint(
            "blocked_dependency_id IS NULL OR "
            "length(blocked_dependency_id) BETWEEN 1 AND 128",
            name="development_source_smoke_test_blocked_id_bounded",
        ),
        CheckConstraint(
            "blocked_dependency_reason IS NULL OR "
            "length(blocked_dependency_reason) BETWEEN 1 AND 64",
            name="development_source_smoke_test_blocked_reason_bounded",
        ),
        CheckConstraint(
            "expected_container_id IS NULL OR (length(expected_container_id) = 64 "
            "AND expected_container_id NOT GLOB '*[^0-9a-f]*')",
            name="development_source_smoke_test_expected_container",
        ),
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="development_source_smoke_test_lease_digest",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="development_source_smoke_test_failure_bounded",
        ),
        Index(
            "uq_development_source_smoke_test_active_instance",
            "managed_instance_id",
            unique=True,
            sqlite_where=text("state IN ('queued','running')"),
        ),
        Index(
            "ix_development_source_smoke_test_original_requested",
            "original_managed_instance_id",
            "requested_at",
        ),
        Index(
            "ix_development_source_smoke_test_source",
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
        String(36), ForeignKey("development_sources.id", ondelete="RESTRICT")
    )
    source_logical_name: Mapped[str] = mapped_column(String(120), nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    addon_version: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runtime_revisions.id", ondelete="RESTRICT")
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[DevelopmentSourceSmokeTestOperationState] = mapped_column(
        Enum(
            DevelopmentSourceSmokeTestOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="development_source_smoke_test_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[DevelopmentSourceSmokeTestOperationPhase] = mapped_column(
        Enum(
            DevelopmentSourceSmokeTestOperationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="development_source_smoke_test_operation_phase",
        ),
        nullable=False,
    )
    overall_result: Mapped[DevelopmentSourceSmokeTestOverallResult | None] = (
        mapped_column(
            Enum(
                DevelopmentSourceSmokeTestOverallResult,
                values_callable=_enum_values,
                native_enum=False,
                create_constraint=True,
                name="development_source_smoke_test_overall_result",
            ),
            nullable=True,
        )
    )
    execution_result: Mapped[DevelopmentSourceSmokeTestExecutionResult] = mapped_column(
        Enum(
            DevelopmentSourceSmokeTestExecutionResult,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="development_source_smoke_test_execution_result",
        ),
        nullable=False,
        default=DevelopmentSourceSmokeTestExecutionResult.NOT_ATTEMPTED,
        server_default=text("'not_attempted'"),
    )
    post_execution_provenance: Mapped[DevelopmentSourceSmokeTestPostProvenance] = (
        mapped_column(
            Enum(
                DevelopmentSourceSmokeTestPostProvenance,
                values_callable=_enum_values,
                native_enum=False,
                create_constraint=True,
                name="development_source_smoke_test_post_provenance",
            ),
            nullable=False,
            default=DevelopmentSourceSmokeTestPostProvenance.NOT_CHECKED,
            server_default=text("'not_checked'"),
        )
    )
    invocation_dispatched: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    invocation_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    returned_item_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blocked_dependency_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    blocked_dependency_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    expected_container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AddonZipSmokeTestOperationRow(Base):
    """Durable bounded ZIP-specific smoke-test history and coordination."""

    __tablename__ = "addon_zip_smoke_test_operations"
    __table_args__ = (
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 128",
            name="addon_zip_smoke_test_identity_bounded",
        ),
        CheckConstraint(
            "invocation_duration_ms IS NULL OR "
            "invocation_duration_ms BETWEEN 0 AND 90000",
            name="addon_zip_smoke_test_duration_bounded",
        ),
        CheckConstraint(
            "returned_item_count IS NULL OR returned_item_count BETWEEN 0 AND 1",
            name="addon_zip_smoke_test_item_count_bounded",
        ),
        CheckConstraint(
            "blocked_dependency_id IS NULL OR "
            "length(blocked_dependency_id) BETWEEN 1 AND 128",
            name="addon_zip_smoke_test_blocked_id_bounded",
        ),
        CheckConstraint(
            "blocked_dependency_reason IS NULL OR "
            "length(blocked_dependency_reason) BETWEEN 1 AND 64",
            name="addon_zip_smoke_test_blocked_reason_bounded",
        ),
        CheckConstraint(
            "expected_container_id IS NULL OR (length(expected_container_id) = 64 "
            "AND expected_container_id NOT GLOB '*[^0-9a-f]*')",
            name="addon_zip_smoke_test_expected_container",
        ),
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="addon_zip_smoke_test_lease_digest",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="addon_zip_smoke_test_failure_bounded",
        ),
        Index(
            "uq_addon_zip_smoke_test_active_instance",
            "managed_instance_id",
            unique=True,
            sqlite_where=text("state IN ('queued','running')"),
        ),
        Index(
            "ix_addon_zip_smoke_test_original_requested",
            "original_managed_instance_id",
            "requested_at",
        ),
        Index(
            "ix_addon_zip_smoke_test_artifact",
            "artifact_id",
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
    artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("addon_zip_artifacts.id", ondelete="RESTRICT")
    )
    installation_operation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("addon_zip_installation_operations.id", ondelete="RESTRICT"),
    )
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    addon_version: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runtime_revisions.id", ondelete="RESTRICT")
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[AddonZipSmokeTestOperationState] = mapped_column(
        Enum(
            AddonZipSmokeTestOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_smoke_test_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[AddonZipSmokeTestOperationPhase] = mapped_column(
        Enum(
            AddonZipSmokeTestOperationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_smoke_test_operation_phase",
        ),
        nullable=False,
    )
    overall_result: Mapped[AddonZipSmokeTestOverallResult | None] = mapped_column(
        Enum(
            AddonZipSmokeTestOverallResult,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_smoke_test_overall_result",
        ),
        nullable=True,
    )
    execution_result: Mapped[AddonZipSmokeTestExecutionResult] = mapped_column(
        Enum(
            AddonZipSmokeTestExecutionResult,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_smoke_test_execution_result",
        ),
        nullable=False,
        default=AddonZipSmokeTestExecutionResult.NOT_ATTEMPTED,
        server_default=text("'not_attempted'"),
    )
    post_execution_provenance: Mapped[AddonZipSmokeTestPostProvenance] = mapped_column(
        Enum(
            AddonZipSmokeTestPostProvenance,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_smoke_test_post_provenance",
        ),
        nullable=False,
        default=AddonZipSmokeTestPostProvenance.NOT_CHECKED,
        server_default=text("'not_checked'"),
    )
    invocation_dispatched: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    invocation_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    returned_item_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blocked_dependency_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    blocked_dependency_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    expected_container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AddonRepositorySourceRow(Base):
    __tablename__ = "addon_repository_sources"
    __table_args__ = (
        UniqueConstraint("logical_name", name="uq_addon_repository_logical_name"),
        UniqueConstraint(
            "canonical_index_url", name="uq_addon_repository_canonical_url"
        ),
        CheckConstraint(
            "length(logical_name) BETWEEN 1 AND 120",
            name="addon_repository_logical_name_bounded",
        ),
        CheckConstraint(
            "length(canonical_index_url) BETWEEN 1 AND 2304",
            name="addon_repository_url_bounded",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    logical_name: Mapped[str] = mapped_column(String(120), nullable=False)
    canonical_index_url: Mapped[str] = mapped_column(String(2304), nullable=False)
    origin: Mapped[AddonRepositoryOrigin] = mapped_column(
        Enum(
            AddonRepositoryOrigin,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_repository_origin",
        ),
        nullable=False,
    )
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class ManagedInstanceAddonRepositorySourceRow(Base):
    __tablename__ = "managed_instance_addon_repository_sources"

    managed_instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("managed_instances.target_id", ondelete="CASCADE"),
        primary_key=True,
    )
    repository_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("addon_repository_sources.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    attached_at: Mapped[str] = mapped_column(String(32), nullable=False)


class AddonRepositoryAttachmentOperationRow(Base):
    __tablename__ = "addon_repository_attachment_operations"
    __table_args__ = (
        CheckConstraint(
            "length(repository_logical_name) BETWEEN 1 AND 120",
            name="addon_repository_attachment_name_bounded",
        ),
        CheckConstraint(
            "(state='succeeded' AND failure_reason IS NULL) OR "
            "(state='failed' AND failure_reason IS NOT NULL)",
            name="addon_repository_attachment_state_consistent",
        ),
        Index(
            "ix_addon_repository_attachment_original_requested",
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
    repository_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("addon_repository_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    repository_logical_name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[AddonRepositoryAttachmentKind] = mapped_column(
        Enum(
            AddonRepositoryAttachmentKind,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_repository_attachment_kind",
        ),
        nullable=False,
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    completed_at: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[AddonRepositoryAttachmentState] = mapped_column(
        Enum(
            AddonRepositoryAttachmentState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_repository_attachment_state",
        ),
        nullable=False,
    )
    failure_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)


class AddonRepositoryMetadataSnapshotRow(Base):
    __tablename__ = "addon_repository_metadata_snapshots"
    __table_args__ = (
        CheckConstraint(
            "transfer_size BETWEEN 1 AND 8388608 AND "
            "expanded_size BETWEEN 1 AND 33554432 AND "
            "entry_count BETWEEN 0 AND 2048",
            name="addon_repository_snapshot_aggregates_bounded",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repository_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("addon_repository_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    fetched_at: Mapped[str] = mapped_column(String(32), nullable=False)
    transfer_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    canonical_metadata_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    transfer_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expanded_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False)


class AddonRepositoryCandidateRow(Base):
    __tablename__ = "addon_repository_candidates"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "addon_id", "version", name="uq_addon_repository_candidate"
        ),
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND length(name) BETWEEN 1 AND 256 "
            "AND length(version) BETWEEN 1 AND 128",
            name="addon_repository_candidate_identity_bounded",
        ),
        CheckConstraint(
            "unsupported_reason IS NULL OR length(unsupported_reason) BETWEEN 1 AND 64",
            name="addon_repository_candidate_reason_bounded",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("addon_repository_metadata_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    repository_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("addon_repository_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    classification: Mapped[AddonRepositoryCandidateClassification] = mapped_column(
        Enum(
            AddonRepositoryCandidateClassification,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_repository_candidate_classification",
        ),
        nullable=False,
    )
    dependencies_json: Mapped[str] = mapped_column(Text, nullable=False)
    support: Mapped[AddonRepositoryCandidateSupport] = mapped_column(
        Enum(
            AddonRepositoryCandidateSupport,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_repository_candidate_support",
        ),
        nullable=False,
    )
    unsupported_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AddonRepositoryRefreshOperationRow(Base):
    __tablename__ = "addon_repository_refresh_operations"
    __table_args__ = (
        Index(
            "uq_addon_repository_refresh_active",
            "repository_source_id",
            unique=True,
            sqlite_where=text("state IN ('queued','running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repository_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("addon_repository_sources.id", ondelete="RESTRICT")
    )
    snapshot_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("addon_repository_metadata_snapshots.id", ondelete="RESTRICT"),
        nullable=True,
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[AddonRepositoryOperationState] = mapped_column(
        Enum(
            AddonRepositoryOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_repository_refresh_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[AddonRepositoryRefreshPhase] = mapped_column(
        Enum(
            AddonRepositoryRefreshPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_repository_refresh_phase",
        ),
        nullable=False,
    )
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AddonRepositoryFetchOperationRow(Base):
    __tablename__ = "addon_repository_fetch_operations"
    __table_args__ = (
        Index(
            "uq_addon_repository_fetch_active",
            "repository_source_id",
            unique=True,
            sqlite_where=text("state IN ('queued','running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repository_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("addon_repository_sources.id", ondelete="RESTRICT")
    )
    snapshot_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("addon_repository_metadata_snapshots.id", ondelete="RESTRICT"),
    )
    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("addon_repository_candidates.id", ondelete="RESTRICT")
    )
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[AddonRepositoryOperationState] = mapped_column(
        Enum(
            AddonRepositoryOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_repository_fetch_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[AddonRepositoryFetchPhase] = mapped_column(
        Enum(
            AddonRepositoryFetchPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_repository_fetch_phase",
        ),
        nullable=False,
    )
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AddonZipArtifactRow(Base):
    """Accepted immutable uploaded-ZIP artifact metadata."""

    __tablename__ = "addon_zip_artifacts"
    __table_args__ = (
        CheckConstraint(
            "length(logical_label) BETWEEN 1 AND 120",
            name="addon_zip_artifact_label_bounded",
        ),
        CheckConstraint(
            "length(sha256_digest) = 71 AND substr(sha256_digest,1,7)='sha256:'",
            name="addon_zip_artifact_digest_bounded",
        ),
        CheckConstraint(
            "length(payload_manifest_digest) = 71 AND "
            "substr(payload_manifest_digest,1,7)='sha256:'",
            name="addon_zip_payload_digest_bounded",
        ),
        CheckConstraint(
            "compressed_size BETWEEN 1 AND 268435456 AND "
            "total_uncompressed_bytes BETWEEN 0 AND 1073741824 AND "
            "member_count BETWEEN 1 AND 50000",
            name="addon_zip_artifact_aggregates_bounded",
        ),
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_name) BETWEEN 1 AND 256 AND "
            "length(addon_version) BETWEEN 1 AND 128",
            name="addon_zip_artifact_identity_bounded",
        ),
        CheckConstraint(
            "(origin='uploaded_zip' AND repository_source_id IS NULL AND "
            "repository_snapshot_id IS NULL AND repository_candidate_id IS NULL AND "
            "repository_fetched_at IS NULL) OR "
            "(origin='repository_download' AND repository_source_id IS NOT NULL AND "
            "repository_snapshot_id IS NOT NULL AND "
            "repository_candidate_id IS NOT NULL "
            "AND repository_fetched_at IS NOT NULL)",
            name="addon_zip_artifact_repository_lineage",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    logical_label: Mapped[str] = mapped_column(String(120), nullable=False)
    sha256_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    compressed_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_uncompressed_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    addon_name: Mapped[str] = mapped_column(String(256), nullable=False)
    addon_version: Mapped[str] = mapped_column(String(128), nullable=False)
    extension_classification: Mapped[AddonZipExtensionClassification] = mapped_column(
        Enum(
            AddonZipExtensionClassification,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_extension_classification",
        ),
        nullable=False,
    )
    dependencies_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_manifest_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    imported_at: Mapped[str] = mapped_column(String(32), nullable=False)
    origin: Mapped[AddonZipOrigin] = mapped_column(
        Enum(
            AddonZipOrigin,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_origin",
        ),
        nullable=False,
    )
    validation_state: Mapped[AddonZipValidationState] = mapped_column(
        Enum(
            AddonZipValidationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_validation_state",
        ),
        nullable=False,
    )
    repository_source_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("addon_repository_sources.id", ondelete="RESTRICT"),
        nullable=True,
    )
    repository_snapshot_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("addon_repository_metadata_snapshots.id", ondelete="RESTRICT"),
        nullable=True,
    )
    repository_candidate_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("addon_repository_candidates.id", ondelete="RESTRICT"),
        nullable=True,
    )
    repository_fetched_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AddonZipImportOperationRow(Base):
    """Durable global-heavy import/validation operation."""

    __tablename__ = "addon_zip_import_operations"
    __table_args__ = (
        CheckConstraint(
            "length(logical_label) BETWEEN 1 AND 120",
            name="addon_zip_import_label_bounded",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="addon_zip_import_failure_bounded",
        ),
        Index(
            "uq_addon_zip_import_active",
            "state",
            unique=True,
            sqlite_where=text("state IN ('queued','running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    logical_label: Mapped[str] = mapped_column(String(120), nullable=False)
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[AddonZipImportOperationState] = mapped_column(
        Enum(
            AddonZipImportOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_import_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[AddonZipImportOperationPhase] = mapped_column(
        Enum(
            AddonZipImportOperationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_import_operation_phase",
        ),
        nullable=False,
    )
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AddonZipInstallationOperationRow(Base):
    """Durable dual-coordinated running-instance ZIP installation."""

    __tablename__ = "addon_zip_installation_operations"
    __table_args__ = (
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 128",
            name="addon_zip_installation_identity_bounded",
        ),
        CheckConstraint(
            "expected_container_id IS NULL OR length(expected_container_id)=64",
            name="addon_zip_installation_container_bounded",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="addon_zip_installation_failure_bounded",
        ),
        CheckConstraint(
            "dependency_baseline_json IS NULL OR "
            "length(dependency_baseline_json)<=16384",
            name="addon_zip_installation_dependency_baseline_bounded",
        ),
        CheckConstraint(
            "dependency_post_json IS NULL OR length(dependency_post_json)<=16384",
            name="addon_zip_installation_dependency_post_bounded",
        ),
        Index(
            "uq_addon_zip_installation_active_instance",
            "managed_instance_id",
            unique=True,
            sqlite_where=text("state IN ('queued','running')"),
        ),
        Index(
            "ix_addon_zip_installation_original_requested",
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
    artifact_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("addon_zip_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    addon_version: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[AddonZipInstallationOperationState] = mapped_column(
        Enum(
            AddonZipInstallationOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_installation_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[AddonZipInstallationOperationPhase] = mapped_column(
        Enum(
            AddonZipInstallationOperationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_installation_operation_phase",
        ),
        nullable=False,
    )
    expected_container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    application_started: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    stopped_readback_matched: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    final_readback_matched: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    recognition_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    dependency_baseline_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    dependency_post_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class RepositoryReplacementPlanRow(Base):
    """Exact short-lived repository replacement approval plan."""

    __tablename__ = "repository_replacement_plans"
    __table_args__ = (
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(incoming_version) BETWEEN 1 AND 128",
            name="repository_replacement_plan_identity_bounded",
        ),
        CheckConstraint(
            "length(expected_container_id)=64",
            name="repository_replacement_plan_container_bounded",
        ),
        CheckConstraint(
            "length(incoming_payload_manifest_digest)=71 AND "
            "substr(incoming_payload_manifest_digest,1,7)='sha256:'",
            name="repository_replacement_plan_incoming_digest",
        ),
        CheckConstraint(
            "invalidation_reason IS NULL OR "
            "length(invalidation_reason) BETWEEN 1 AND 128",
            name="repository_replacement_plan_reason_bounded",
        ),
        CheckConstraint(
            "(incumbent_class='attached_unmaterialized' AND "
            "development_deployment_operation_id IS NULL AND "
            "development_source_manifest_digest IS NULL AND "
            "development_destination_digest IS NULL AND incumbent_version IS NULL AND "
            "incumbent_type IS NULL AND incumbent_enabled IS NULL AND "
            "qualifying_snapshot_id IS NULL) OR "
            "(incumbent_class='verified_copied' AND "
            "development_deployment_operation_id IS NOT NULL AND "
            "development_source_manifest_digest IS NOT NULL AND "
            "development_destination_digest IS NOT NULL AND "
            "incumbent_version IS NOT NULL AND "
            "incumbent_type IS NOT NULL AND incumbent_enabled IS NOT NULL AND "
            "qualifying_snapshot_id IS NOT NULL)",
            name="repository_replacement_plan_incumbent_consistent",
        ),
        Index(
            "ix_repository_replacement_plan_instance_created",
            "original_managed_instance_id",
            "created_at",
        ),
        Index(
            "uq_repository_replacement_plan_active_instance",
            "managed_instance_id",
            unique=True,
            sqlite_where=text("state IN ('pending','approved')"),
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
    repository_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("addon_repository_sources.id", ondelete="RESTRICT")
    )
    repository_attachment_at: Mapped[str] = mapped_column(String(32), nullable=False)
    repository_snapshot_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("addon_repository_metadata_snapshots.id", ondelete="RESTRICT"),
    )
    repository_candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("addon_repository_candidates.id", ondelete="RESTRICT")
    )
    incoming_artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("addon_zip_artifacts.id", ondelete="RESTRICT")
    )
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    incumbent_class: Mapped[RepositoryReplacementIncumbentClass] = mapped_column(
        Enum(
            RepositoryReplacementIncumbentClass,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="repository_replacement_incumbent_class",
        )
    )
    development_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("development_sources.id", ondelete="RESTRICT")
    )
    development_source_attachment_at: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    development_deployment_operation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("development_source_deployment_operations.id", ondelete="RESTRICT"),
    )
    development_source_manifest_digest: Mapped[str | None] = mapped_column(String(71))
    development_destination_digest: Mapped[str | None] = mapped_column(String(71))
    incumbent_version: Mapped[str | None] = mapped_column(String(128))
    incumbent_type: Mapped[str | None] = mapped_column(String(128))
    incumbent_enabled: Mapped[bool | None] = mapped_column(Boolean)
    runtime_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runtime_revisions.id", ondelete="RESTRICT")
    )
    expected_container_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Immutable historical binding: Snapshot deletion invalidates a pending plan but
    # must not erase which Snapshot protected a consumed operation.
    qualifying_snapshot_id: Mapped[str | None] = mapped_column(String(36))
    incoming_version: Mapped[str] = mapped_column(String(128), nullable=False)
    incoming_payload_manifest_digest: Mapped[str] = mapped_column(
        String(71), nullable=False
    )
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[RepositoryReplacementPlanState] = mapped_column(
        Enum(
            RepositoryReplacementPlanState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="repository_replacement_plan_state",
        )
    )
    consumed_execution_operation_id: Mapped[str | None] = mapped_column(String(36))
    terminal_outcome: Mapped[RepositoryReplacementTerminalOutcome | None] = (
        mapped_column(
            Enum(
                RepositoryReplacementTerminalOutcome,
                values_callable=_enum_values,
                native_enum=False,
                create_constraint=True,
                name="repository_replacement_terminal_outcome",
            )
        )
    )
    invalidation_reason: Mapped[str | None] = mapped_column(String(128))


class RepositoryReplacementOperationRow(Base):
    """One execution attempt bound to one exact replacement plan."""

    __tablename__ = "repository_replacement_operations"
    __table_args__ = (
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(incoming_version) BETWEEN 1 AND 128",
            name="repository_replacement_operation_identity_bounded",
        ),
        CheckConstraint(
            "length(expected_container_id)=64",
            name="repository_replacement_operation_container_bounded",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="repository_replacement_operation_reason_bounded",
        ),
        CheckConstraint(
            "(state='queued' AND started_at IS NULL AND completed_at IS NULL AND "
            "lease_token_digest IS NULL AND lease_expires_at IS NULL AND "
            "application_started=0) OR "
            "(state='running' AND started_at IS NOT NULL AND completed_at IS NULL "
            "AND lease_token_digest IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state IN ('succeeded','failed','reconciliation_required') AND "
            "completed_at IS NOT NULL AND lease_token_digest IS NULL AND "
            "lease_expires_at IS NULL)",
            name="repository_replacement_operation_state_consistent",
        ),
        CheckConstraint(
            "(state='succeeded' AND application_started=1 AND "
            "stopped_readback_matched=1 AND final_readback_matched=1 AND "
            "recognition_enabled IS NOT NULL AND dependency_baseline_json IS NOT NULL "
            "AND dependency_post_json IS NOT NULL AND reconciliation_required=0) OR "
            "(state='failed' AND application_started=0 AND "
            "reconciliation_required=0) OR "
            "(state='reconciliation_required' AND application_started=1 AND "
            "reconciliation_required=1) OR state IN ('queued','running')",
            name="repository_replacement_operation_result_consistent",
        ),
        Index(
            "uq_repository_replacement_operation_active_instance",
            "managed_instance_id",
            unique=True,
            sqlite_where=text("state IN ('queued','running')"),
        ),
        Index(
            "ix_repository_replacement_operation_original_requested",
            "original_managed_instance_id",
            "requested_at",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("repository_replacement_plans.id", ondelete="RESTRICT"),
        unique=True,
    )
    original_managed_instance_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    managed_instance_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("managed_instances.target_id", ondelete="SET NULL")
    )
    incoming_artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("addon_zip_artifacts.id", ondelete="RESTRICT")
    )
    development_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("development_sources.id", ondelete="RESTRICT")
    )
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    incoming_version: Mapped[str] = mapped_column(String(128), nullable=False)
    incumbent_class: Mapped[RepositoryReplacementIncumbentClass] = mapped_column(
        Enum(
            RepositoryReplacementIncumbentClass,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="repository_replacement_operation_incumbent_class",
        )
    )
    runtime_revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    expected_container_id: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[RepositoryReplacementOperationState] = mapped_column(
        Enum(
            RepositoryReplacementOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="repository_replacement_operation_state",
        )
    )
    phase: Mapped[RepositoryReplacementOperationPhase] = mapped_column(
        Enum(
            RepositoryReplacementOperationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="repository_replacement_operation_phase",
        )
    )
    application_started: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    stopped_readback_matched: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    final_readback_matched: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    recognition_enabled: Mapped[bool | None] = mapped_column(Boolean)
    dependency_baseline_json: Mapped[str | None] = mapped_column(Text)
    dependency_post_json: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(String(32))
    heartbeat_at: Mapped[str | None] = mapped_column(String(32))
    completed_at: Mapped[str | None] = mapped_column(String(32))
    failure_reason: Mapped[str | None] = mapped_column(String(256))
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    lease_token_digest: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[str | None] = mapped_column(String(32))


class AddonZipInstallationEvidenceRow(Base):
    """Current typed evidence for one successful absent-only ZIP install."""

    __tablename__ = "addon_zip_installation_evidence"
    __table_args__ = (
        CheckConstraint(
            "length(trusted_container_id)=64 AND length(addon_id) BETWEEN 1 AND 128 "
            "AND length(addon_version) BETWEEN 1 AND 128",
            name="addon_zip_evidence_identity_bounded",
        ),
        CheckConstraint(
            "length(payload_manifest_digest)=71 AND "
            "substr(payload_manifest_digest,1,7)='sha256:'",
            name="addon_zip_evidence_digest_bounded",
        ),
        CheckConstraint(
            "(artifact_origin='uploaded_zip' AND repository_source_id IS NULL AND "
            "repository_snapshot_id IS NULL AND repository_candidate_id IS NULL) OR "
            "(artifact_origin='repository_download' AND "
            "repository_source_id IS NOT NULL AND repository_snapshot_id IS NOT NULL "
            "AND repository_candidate_id IS NOT NULL)",
            name="addon_zip_evidence_repository_lineage",
        ),
        CheckConstraint(
            "(installation_operation_id IS NOT NULL AND "
            "replacement_operation_id IS NULL "
            "AND result='installed_recognized_disabled') OR "
            "(installation_operation_id IS NULL AND "
            "replacement_operation_id IS NOT NULL "
            "AND result='repository_replacement_recognized')",
            name="addon_zip_evidence_producer_consistent",
        ),
    )

    managed_instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("managed_instances.target_id", ondelete="CASCADE"),
        primary_key=True,
    )
    original_managed_instance_id: Mapped[str] = mapped_column(
        String(36), nullable=False
    )
    artifact_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("addon_zip_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    installation_operation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("addon_zip_installation_operations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    replacement_operation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("repository_replacement_operations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    runtime_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    trusted_container_id: Mapped[str] = mapped_column(String(64), nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False, primary_key=True)
    addon_version: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_manifest_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    verified_at: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[AddonZipInstallationConfidence] = mapped_column(
        Enum(
            AddonZipInstallationConfidence,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_installation_confidence",
        ),
        nullable=False,
    )
    result: Mapped[AddonZipInstallationResult] = mapped_column(
        Enum(
            AddonZipInstallationResult,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_installation_result",
        ),
        nullable=False,
    )
    artifact_origin: Mapped[AddonZipOrigin] = mapped_column(
        Enum(
            AddonZipOrigin,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_evidence_origin",
        ),
        nullable=False,
        default=AddonZipOrigin.UPLOADED_ZIP,
        server_default=text("'uploaded_zip'"),
    )
    repository_source_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("addon_repository_sources.id", ondelete="RESTRICT"),
        nullable=True,
    )
    repository_snapshot_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("addon_repository_metadata_snapshots.id", ondelete="RESTRICT"),
        nullable=True,
    )
    repository_candidate_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("addon_repository_candidates.id", ondelete="RESTRICT"),
        nullable=True,
    )


class AddonZipEnablementOperationRow(Base):
    """ZIP-specific synchronous enablement history."""

    __tablename__ = "addon_zip_enablement_operations"
    __table_args__ = (
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 128",
            name="addon_zip_enablement_identity_bounded",
        ),
        CheckConstraint(
            "expected_container_id IS NULL OR (length(expected_container_id)=64 AND "
            "expected_container_id NOT GLOB '*[^0-9a-f]*')",
            name="addon_zip_enablement_container_bounded",
        ),
        CheckConstraint(
            "blocked_dependency_id IS NULL OR "
            "length(blocked_dependency_id) BETWEEN 1 AND 128",
            name="addon_zip_enablement_blocked_id_bounded",
        ),
        CheckConstraint(
            "blocked_dependency_reason IS NULL OR "
            "length(blocked_dependency_reason) BETWEEN 1 AND 64",
            name="addon_zip_enablement_blocked_reason_bounded",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="addon_zip_enablement_failure_bounded",
        ),
        Index(
            "uq_addon_zip_enablement_active_instance",
            "managed_instance_id",
            unique=True,
            sqlite_where=text("state='running'"),
        ),
        Index(
            "ix_addon_zip_enablement_original_requested",
            "original_managed_instance_id",
            "requested_at",
        ),
        Index("ix_addon_zip_enablement_artifact", "artifact_id"),
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
    artifact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("addon_zip_artifacts.id", ondelete="RESTRICT")
    )
    installation_operation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("addon_zip_installation_operations.id", ondelete="RESTRICT"),
    )
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    addon_version: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runtime_revisions.id", ondelete="RESTRICT")
    )
    expected_container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[AddonZipEnablementOperationState] = mapped_column(
        Enum(
            AddonZipEnablementOperationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_enablement_operation_state",
        ),
        nullable=False,
    )
    phase: Mapped[AddonZipEnablementOperationPhase] = mapped_column(
        Enum(
            AddonZipEnablementOperationPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_enablement_operation_phase",
        ),
        nullable=False,
    )
    result_classification: Mapped[AddonZipEnablementResult | None] = mapped_column(
        Enum(
            AddonZipEnablementResult,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_enablement_result",
        ),
        nullable=True,
    )
    payload_verification: Mapped[AddonZipPayloadVerificationOutcome] = mapped_column(
        Enum(
            AddonZipPayloadVerificationOutcome,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="addon_zip_payload_verification_outcome",
        ),
        nullable=False,
        default=AddonZipPayloadVerificationOutcome.NOT_CHECKED,
        server_default=text("'not_checked'"),
    )
    prior_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    enable_performed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    blocked_dependency_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    blocked_dependency_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    completed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )


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
            length=32,
        ),
        nullable=False,
    )
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    lease_token_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    acquired_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class DevelopmentSourcePromotionLeaseRow(Base):
    """One exact promotion application lease per canonical source."""

    __tablename__ = "development_source_promotion_leases"
    __table_args__ = (
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest)=64 AND "
            "lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="development_source_promotion_lease_digest",
        ),
    )

    development_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_sources.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_workspace_promotion_operations.id", ondelete="RESTRICT"),
        unique=True,
    )
    lease_token_digest: Mapped[str | None] = mapped_column(String(64))
    acquired_at: Mapped[str | None] = mapped_column(String(32))
    lease_expires_at: Mapped[str | None] = mapped_column(String(32))


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
        CheckConstraint(
            "runtime_uid BETWEEN 0 AND 2147483647",
            name="runtime_profile_uid_range",
        ),
        CheckConstraint(
            "runtime_gid BETWEEN 0 AND 2147483647",
            name="runtime_profile_gid_range",
        ),
        CheckConstraint(
            "python_bytecode_policy IN ('interpreter_default', 'suppress')",
            name="runtime_profile_python_bytecode_policy",
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
    runtime_uid: Mapped[int] = mapped_column(Integer, nullable=False)
    runtime_gid: Mapped[int] = mapped_column(Integer, nullable=False)
    python_bytecode_policy: Mapped[PythonBytecodePolicy] = mapped_column(
        Enum(
            PythonBytecodePolicy,
            values_callable=_enum_values,
            native_enum=False,
            length=32,
        ),
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
