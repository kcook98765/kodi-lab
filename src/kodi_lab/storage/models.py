"""SQLAlchemy mappings for core targets, instances, and runtime catalog."""

from enum import StrEnum

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
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
    AgentWorkspaceBytesState,
    AgentWorkspaceCleanupPhase,
    AgentWorkspaceCleanupState,
    AgentWorkspaceCreationPhase,
    AgentWorkspaceCreationState,
    AgentWorkspaceLifecycleEventKind,
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
    ExternalAdministrativeState,
    ExternalAuthenticationMode,
    ExternalBridgePairingState,
    ExternalBridgeReachabilityState,
    ExternalObservationState,
    ExternalProtocolKind,
    ExternalVerificationState,
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
    ProductionDesignationState,
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
    RuntimeRecipeInstallationState,
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
from kodi_lab.storage.development_capability_sql import (
    DEVELOPMENT_CAPABILITY_TRIGGERS,
    PRODUCTION_SCREENSHOT_TARGET_GUARD,
)
from kodi_lab.storage.metadata import Base


class DevelopmentRepositoryStateRow(Base):
    """One durable binding between the configured origin and selected index."""

    __tablename__ = "development_repository_state"
    __table_args__ = (CheckConstraint("id=1", name="development_repository_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    bootstrap_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    bootstrap_tree_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    current_generation: Mapped[str] = mapped_column(String(64), nullable=False)
    current_index_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pending_generation: Mapped[str | None] = mapped_column(String(64))
    pending_index_sha256: Mapped[str | None] = mapped_column(String(64))
    pending_retirements: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class DevelopmentPublicationRow(Base):
    """Immutable artifact identity with a mutable, closed publication state."""

    __tablename__ = "development_publications"
    __table_args__ = (
        UniqueConstraint(
            "addon_id", "version", name="uq_development_publication_identity"
        ),
        CheckConstraint(
            "channel='development'", name="development_publication_channel"
        ),
        CheckConstraint(
            "addon_type='plugin.video'", name="development_publication_type"
        ),
        CheckConstraint(
            "scope_kind='development_only'", name="development_publication_scope"
        ),
        CheckConstraint(
            "status IN ('prepared','publishing','current',"
            "'superseded','retired','failed')",
            name="development_publication_status",
        ),
        CheckConstraint(
            "source_kind IN ('web_import','workspace')",
            name="development_publication_source_kind",
        ),
        CheckConstraint("zip_bytes > 0", name="development_publication_nonempty"),
        CheckConstraint(
            "(source_kind='web_import' AND workspace_id IS NULL "
            "AND workspace_generation IS NULL) OR "
            "(source_kind='workspace' AND workspace_id IS NOT NULL "
            "AND workspace_generation IS NOT NULL "
            "AND validation_receipt IS NOT NULL "
            "AND source_tree_sha256 IS NOT NULL)",
            name="development_publication_source_binding",
        ),
        Index(
            "uq_development_publication_current",
            "addon_id",
            unique=True,
            sqlite_where=text("status='current'"),
        ),
        Index("ix_development_publication_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    addon_type: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_workspaces.id", ondelete="RESTRICT")
    )
    workspace_generation: Mapped[int | None] = mapped_column(Integer)
    validation_receipt: Mapped[str | None] = mapped_column(String(71))
    source_tree_sha256: Mapped[str | None] = mapped_column(String(71))
    compatible_generations: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    intended_targets: Mapped[str | None] = mapped_column(Text)
    zip_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    tree_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    zip_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    feed_generation: Mapped[str | None] = mapped_column(String(64))
    index_sha256: Mapped[str | None] = mapped_column(String(64))
    approval_identity: Mapped[str | None] = mapped_column(String(128))
    approved_at: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    published_at: Mapped[str | None] = mapped_column(String(32))
    superseded_at: Mapped[str | None] = mapped_column(String(32))
    retired_at: Mapped[str | None] = mapped_column(String(32))
    failure_reason: Mapped[str | None] = mapped_column(String(128))


class DevelopmentPublicationHoldRow(Base):
    """Durable evidence reference that blocks retention removal."""

    __tablename__ = "development_publication_holds"
    __table_args__ = (
        UniqueConstraint(
            "publication_id",
            "reason",
            "reference_id",
            name="uq_development_publication_hold",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    publication_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_publications.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(48), nullable=False)
    reference_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


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
        UniqueConstraint(
            "id", "development_source_id", name="uq_agent_workspace_id_source"
        ),
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
        CheckConstraint(
            "(bytes_state='present' AND cleaned_at IS NULL) OR "
            "(bytes_state='cleaned' AND cleaned_at IS NOT NULL)",
            name="agent_workspace_bytes_state_consistent",
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
    bytes_state: Mapped[AgentWorkspaceBytesState] = mapped_column(
        Enum(
            AgentWorkspaceBytesState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="agent_workspace_bytes_state",
        ),
        nullable=False,
        default=AgentWorkspaceBytesState.PRESENT,
    )
    baseline_artifact_id: Mapped[str] = mapped_column(
        String(36), nullable=False, unique=True
    )
    baseline_manifest_digest: Mapped[str | None] = mapped_column(String(71))
    creation_baseline_digest: Mapped[str | None] = mapped_column(String(71))
    active_baseline_artifact_id: Mapped[str | None] = mapped_column(String(36))
    active_capture_id: Mapped[str | None] = mapped_column(String(36))
    active_observed_version: Mapped[str | None] = mapped_column(String(128))
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_tree_digest: Mapped[str | None] = mapped_column(String(71))
    capture_started_at: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
    promoted_at: Mapped[str | None] = mapped_column(String(32))
    abandoned_at: Mapped[str | None] = mapped_column(String(32))
    cleaned_at: Mapped[str | None] = mapped_column(String(32))


class AgentWorkspaceCaptureOperationRow(Base):
    """One bounded, durable source-to-existing-workspace capture attempt."""

    __tablename__ = "agent_workspace_capture_operations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('staging','switching','succeeded','failed',"
            "'reconciliation_required')",
            name="agent_workspace_capture_state_closed",
        ),
        CheckConstraint(
            "prior_generation >= 0 AND resulting_generation = prior_generation + 1",
            name="agent_workspace_capture_generation_step",
        ),
        CheckConstraint(
            "length(prior_baseline_digest)=71 AND length(prior_tree_digest)=71 "
            "AND (captured_digest IS NULL OR length(captured_digest)=71) "
            "AND (captured_version IS NULL OR "
            "length(captured_version) BETWEEN 1 AND 128)",
            name="agent_workspace_capture_digests_bounded",
        ),
        CheckConstraint(
            "(state IN ('staging','failed') OR captured_digest IS NOT NULL) AND "
            "(state NOT IN ('succeeded','failed','reconciliation_required') "
            "OR completed_at IS NOT NULL)",
            name="agent_workspace_capture_completion_consistent",
        ),
        Index(
            "ix_agent_workspace_capture_workspace_time", "workspace_id", "requested_at"
        ),
        Index(
            "uq_agent_workspace_capture_active",
            "workspace_id",
            unique=True,
            sqlite_where=text("state IN ('staging','switching')"),
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
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    prior_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    prior_baseline_id: Mapped[str] = mapped_column(String(36), nullable=False)
    prior_baseline_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    prior_tree_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    captured_digest: Mapped[str | None] = mapped_column(String(71))
    captured_version: Mapped[str | None] = mapped_column(String(128))
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String(32))
    failure_code: Mapped[str | None] = mapped_column(String(128))


class AgentWorkspaceCleanupOperationRow(Base):
    """Durable exact-UUID byte cleanup history."""

    __tablename__ = "agent_workspace_cleanup_operations"
    __table_args__ = (
        CheckConstraint(
            "(expected_device IS NULL AND expected_inode IS NULL) OR "
            "(expected_device IS NOT NULL AND expected_inode IS NOT NULL)",
            name="agent_workspace_cleanup_identity_shape",
        ),
        CheckConstraint(
            "deletion_started=0 OR expected_device IS NOT NULL",
            name="agent_workspace_cleanup_deletion_identity",
        ),
        CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128",
            name="agent_workspace_cleanup_failure_bounded",
        ),
        CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest)=64 AND "
            "lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="agent_workspace_cleanup_lease_digest",
        ),
        CheckConstraint(
            "(state='queued' AND started_at IS NULL AND completed_at IS NULL AND "
            "lease_token_digest IS NULL AND lease_expires_at IS NULL) OR "
            "(state='running' AND started_at IS NOT NULL AND completed_at IS NULL "
            "AND lease_token_digest IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state IN ('succeeded','failed','reconciliation_required') AND "
            "completed_at IS NOT NULL AND lease_token_digest IS NULL AND "
            "lease_expires_at IS NULL)",
            name="agent_workspace_cleanup_state_fields",
        ),
        Index(
            "uq_agent_workspace_cleanup_active",
            "workspace_id",
            unique=True,
            sqlite_where=text("state IN ('queued','running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_workspaces.id", ondelete="RESTRICT")
    )
    state: Mapped[AgentWorkspaceCleanupState] = mapped_column(
        Enum(
            AgentWorkspaceCleanupState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="agent_workspace_cleanup_state",
        ),
        nullable=False,
    )
    phase: Mapped[AgentWorkspaceCleanupPhase] = mapped_column(
        Enum(
            AgentWorkspaceCleanupPhase,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="agent_workspace_cleanup_phase",
        ),
        nullable=False,
    )
    requested_at: Mapped[str] = mapped_column(String(32), nullable=False)
    deletion_started: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    expected_device: Mapped[int | None] = mapped_column(BigInteger)
    expected_inode: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[str | None] = mapped_column(String(32))
    completed_at: Mapped[str | None] = mapped_column(String(32))
    failure_code: Mapped[str | None] = mapped_column(String(128))
    lease_token_digest: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[str | None] = mapped_column(String(32))


class AgentWorkspaceLifecycleEventRow(Base):
    """Content-free abandonment and cleanup activity."""

    __tablename__ = "agent_workspace_lifecycle_events"
    __table_args__ = (
        CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128",
            name="agent_workspace_lifecycle_failure_bounded",
        ),
        Index("ix_agent_workspace_lifecycle_occurred", "workspace_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_workspaces.id", ondelete="RESTRICT")
    )
    kind: Mapped[AgentWorkspaceLifecycleEventKind] = mapped_column(
        Enum(
            AgentWorkspaceLifecycleEventKind,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="agent_workspace_lifecycle_event_kind",
        ),
        nullable=False,
    )
    occurred_at: Mapped[str] = mapped_column(String(32), nullable=False)
    cleanup_operation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agent_workspace_cleanup_operations.id", ondelete="RESTRICT"),
    )
    failure_code: Mapped[str | None] = mapped_column(String(128))


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
            "(target_container_id IS NULL OR length(target_container_id)=64)",
            name="workspace_promotion_identity_bounds",
        ),
        CheckConstraint(
            "target_class IN ('internal_lab','external_development')",
            name="workspace_promotion_target_class_closed",
        ),
        CheckConstraint(
            "(target_class='internal_lab' AND target_runtime_revision_id IS NOT NULL "
            "AND target_container_id IS NOT NULL AND "
            "target_deployment_operation_id IS NOT NULL AND target_bridge_id IS NULL) "
            "OR (target_class='external_development' AND "
            "target_runtime_revision_id IS NULL AND target_container_id IS NULL AND "
            "target_deployment_operation_id IS NULL AND length(target_bridge_id)=36)",
            name="workspace_promotion_target_shape",
        ),
        CheckConstraint(
            "invalidation_reason IS NULL OR "
            "length(invalidation_reason) BETWEEN 1 AND 128",
            name="workspace_promotion_invalidation_bounded",
        ),
        CheckConstraint(
            "(originating_mcp_client_id IS NULL AND "
            "originating_mcp_request_id IS NULL AND "
            "originating_mcp_deployment_id IS NULL AND "
            "originating_mcp_execution_id IS NULL) OR "
            "(length(originating_mcp_client_id)=36 AND "
            "length(originating_mcp_request_id)=36 AND "
            "length(originating_mcp_deployment_id)=36 AND "
            "length(originating_mcp_execution_id)=36)",
            name="workspace_promotion_mcp_origin_complete",
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
        Index(
            "ix_agent_workspace_promotion_plans_original_target",
            "original_target_instance_id",
        ),
        Index(
            "uq_workspace_promotion_mcp_request",
            "originating_mcp_client_id",
            "originating_mcp_request_id",
            unique=True,
            sqlite_where=text("originating_mcp_client_id IS NOT NULL"),
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
    original_target_instance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_instance_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("managed_instances.target_id", ondelete="RESTRICT"),
    )
    original_target_name: Mapped[str | None] = mapped_column(String(256))
    workspace_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    workspace_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    baseline_artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    baseline_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    source_capture_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_version: Mapped[str] = mapped_column(String(128), nullable=False)
    promoted_name: Mapped[str] = mapped_column(String(256), nullable=False)
    promoted_version: Mapped[str] = mapped_column(String(128), nullable=False)
    target_runtime_revision_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
    )
    target_container_id: Mapped[str | None] = mapped_column(String(64))
    target_deployment_operation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("development_source_deployment_operations.id", ondelete="RESTRICT"),
    )
    target_class: Mapped[str] = mapped_column(
        String(32), nullable=False, default="internal_lab"
    )
    target_bridge_id: Mapped[str | None] = mapped_column(String(36))
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
    originating_mcp_client_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("mcp_clients.id", ondelete="RESTRICT")
    )
    originating_mcp_request_id: Mapped[str | None] = mapped_column(String(36))
    originating_mcp_deployment_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("mcp_workspace_deployment_operations.id", ondelete="RESTRICT"),
    )
    originating_mcp_execution_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("mcp_workspace_execution_operations.id", ondelete="RESTRICT"),
    )


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
        Index(
            "ix_agent_workspace_promotion_operations_original_target",
            "original_target_instance_id",
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
    original_target_instance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_instance_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("managed_instances.target_id", ondelete="RESTRICT"),
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
        Index(
            "ix_agent_workspace_promotion_handoffs_original_target",
            "original_target_instance_id",
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
    original_target_instance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_instance_id: Mapped[str | None] = mapped_column(
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
        Index(
            "ix_promotion_verification_operations_original_target",
            "original_target_instance_id",
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
    original_target_instance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_instance_id: Mapped[str | None] = mapped_column(
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
            "(target_type = 'managed' AND kodi_version IS NOT NULL "
            "AND length(trim(kodi_version)) > 0) OR "
            "(target_type = 'external_development' AND "
            "(kodi_version IS NULL OR length(trim(kodi_version)) > 0)) OR "
            "(target_type = 'external_production' AND "
            "(kodi_version IS NULL OR length(trim(kodi_version)) > 0))",
            name="target_kodi_version_by_type",
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
    kodi_version: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class ExternalKodiTargetRow(Base):
    """One-to-one external-development subtype with separate state dimensions."""

    __tablename__ = "external_kodi_targets"
    __table_args__ = (
        CheckConstraint(
            "length(host) BETWEEN 2 AND 64 AND host = trim(host)",
            name="external_target_host_bounded",
        ),
        CheckConstraint("port BETWEEN 1 AND 65535", name="external_target_port_valid"),
        CheckConstraint(
            "(authentication_mode = 'none' AND secret_reference IS NULL) OR "
            "(authentication_mode = 'username_password' "
            "AND length(secret_reference) = 36)",
            name="external_target_secret_shape",
        ),
        CheckConstraint(
            "length(configured_capabilities_json) BETWEEN 2 AND 512 "
            "AND length(verified_capabilities_json) BETWEEN 2 AND 512",
            name="external_target_capabilities_bounded",
        ),
        CheckConstraint(
            "length(verified_addresses_json) BETWEEN 2 AND 512",
            name="external_target_addresses_bounded",
        ),
        CheckConstraint(
            "length(created_at) = 27 "
            "AND (verified_at IS NULL OR length(verified_at) = 27) "
            "AND (deactivated_at IS NULL OR length(deactivated_at) = 27) "
            "AND (last_observed_at IS NULL OR length(last_observed_at) = 27)",
            name="external_target_timestamps_format",
        ),
        CheckConstraint(
            "pending_observation_id IS NULL OR length(pending_observation_id) = 36",
            name="external_target_pending_observation_shape",
        ),
        CheckConstraint(
            "production_policy_revision IS NULL OR production_policy_revision >= 1",
            name="external_target_production_revision_positive",
        ),
        CheckConstraint(
            "(administrative_state = 'deactivated' AND deactivated_at IS NOT NULL "
            "AND secret_reference IS NULL) OR "
            "(administrative_state <> 'deactivated' AND deactivated_at IS NULL)",
            name="external_target_deactivation_consistent",
        ),
        Index(
            "ix_external_target_admin_verification",
            "administrative_state",
            "verification_state",
        ),
    )

    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("targets.id", ondelete="CASCADE"), primary_key=True
    )
    protocol_kind: Mapped[ExternalProtocolKind] = mapped_column(
        Enum(
            ExternalProtocolKind,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="external_protocol_kind",
        ),
        nullable=False,
    )
    host: Mapped[str] = mapped_column(String(64), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    authentication_mode: Mapped[ExternalAuthenticationMode] = mapped_column(
        Enum(
            ExternalAuthenticationMode,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="external_authentication_mode",
        ),
        nullable=False,
    )
    secret_reference: Mapped[str | None] = mapped_column(String(36))
    configured_capabilities_json: Mapped[str] = mapped_column(
        String(512), nullable=False
    )
    verified_capabilities_json: Mapped[str] = mapped_column(String(512), nullable=False)
    administrative_state: Mapped[ExternalAdministrativeState] = mapped_column(
        Enum(
            ExternalAdministrativeState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="external_administrative_state",
        ),
        nullable=False,
    )
    verification_state: Mapped[ExternalVerificationState] = mapped_column(
        Enum(
            ExternalVerificationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="external_verification_state",
        ),
        nullable=False,
    )
    observation_state: Mapped[ExternalObservationState | None] = mapped_column(
        Enum(
            ExternalObservationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="external_observation_state",
        )
    )
    verified_addresses_json: Mapped[str] = mapped_column(String(512), nullable=False)
    verified_kodi_name: Mapped[str | None] = mapped_column(String(64))
    verified_kodi_version: Mapped[str | None] = mapped_column(String(64))
    observed_kodi_name: Mapped[str | None] = mapped_column(String(64))
    observed_kodi_version: Mapped[str | None] = mapped_column(String(64))
    observed_protocol_version: Mapped[str | None] = mapped_column(String(32))
    pending_observation_id: Mapped[str | None] = mapped_column(
        String(36),
    )
    production_policy_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    verified_at: Mapped[str | None] = mapped_column(String(27))
    deactivated_at: Mapped[str | None] = mapped_column(String(27))
    last_observed_at: Mapped[str | None] = mapped_column(String(27))


class ExternalKodiTargetObservationRow(Base):
    """Bounded durable result of one fixed native read-only probe."""

    __tablename__ = "external_kodi_target_observations"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('reachable','unreachable','authentication_failed',"
            "'capability_missing')",
            name="external_observation_outcome_closed",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code IN ('endpoint_not_allowed',"
            "'target_unreachable','authentication_failed','invalid_kodi_response',"
            "'unsupported_kodi_version','identity_mismatch','secret_unavailable')",
            name="external_observation_error_closed",
        ),
        CheckConstraint(
            "length(addresses_json) BETWEEN 2 AND 512 "
            "AND length(verified_capabilities_json) BETWEEN 2 AND 512",
            name="external_observation_json_bounded",
        ),
        CheckConstraint(
            "length(attempted_at) = 27 AND length(completed_at) = 27",
            name="external_observation_timestamps_format",
        ),
        Index("ix_external_observation_target_time", "target_id", "attempted_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("external_kodi_targets.target_id", ondelete="CASCADE")
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    addresses_json: Mapped[str] = mapped_column(String(512), nullable=False)
    kodi_name: Mapped[str | None] = mapped_column(String(64))
    kodi_version: Mapped[str | None] = mapped_column(String(64))
    protocol_version: Mapped[str | None] = mapped_column(String(32))
    verified_capabilities_json: Mapped[str] = mapped_column(String(512), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    attempted_at: Mapped[str] = mapped_column(String(27), nullable=False)
    completed_at: Mapped[str] = mapped_column(String(27), nullable=False)


class ExternalKodiTargetEventRow(Base):
    """Content-free semantic Web-administration audit event."""

    __tablename__ = "external_kodi_target_events"
    __table_args__ = (
        CheckConstraint(
            "event_kind IN ('registered','endpoint_changed','credential_rotated',"
            "'probe_attempted','probe_succeeded','probe_failed',"
            "'verification_confirmed','reverification_required','enabled',"
            "'disabled','deactivated','production_target_registered',"
            "'native_observation_obtained','production_native_identity_confirmed',"
            "'production_designation_confirmed',"
            "'production_designation_invalidated',"
            "'production_mutations_suspended',"
            "'production_mutations_resumed')",
            name="external_target_event_kind_closed",
        ),
        CheckConstraint(
            "classification IS NULL OR length(classification) BETWEEN 1 AND 64",
            name="external_target_event_classification_bounded",
        ),
        CheckConstraint(
            "length(occurred_at) = 27", name="external_target_event_timestamp_format"
        ),
        Index("ix_external_target_event_time", "target_id", "occurred_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    classification: Mapped[str | None] = mapped_column(String(64))
    observation_id: Mapped[str | None] = mapped_column(String(36))
    occurred_at: Mapped[str] = mapped_column(String(27), nullable=False)


class ExternalDevelopmentCapabilityAmendmentRow(Base):
    """Immutable evidence for an approved development target maximum change."""

    __tablename__ = "external_development_capability_amendments"
    __table_args__ = (
        CheckConstraint(
            "length(id)=36 AND length(target_id)=36 AND length(bridge_id)=36 "
            "AND target_classification='external_development' "
            "AND bridge_addon_id='service.kodi_lab_bridge' "
            "AND length(bridge_version) BETWEEN 1 AND 64 "
            "AND bridge_protocol='1' AND length(bridge_fingerprint)=64",
            name="development_capability_amendment_identity_bounded",
        ),
        CheckConstraint(
            "length(before_json) BETWEEN 2 AND 512 AND "
            "length(requested_json) BETWEEN 2 AND 512 AND "
            "resulting_json=requested_json AND before_json<>requested_json AND "
            "length(native_verified_json) BETWEEN 2 AND 512 AND "
            "length(bridge_verified_json) BETWEEN 2 AND 256",
            name="development_capability_amendment_sets_bounded",
        ),
        CheckConstraint(
            "length(actor) BETWEEN 1 AND 64 AND length(requested_at)=27 "
            "AND length(completed_at)=27 AND completed_at>=requested_at",
            name="development_capability_amendment_audit_bounded",
        ),
        Index(
            "ix_development_capability_amendment_target_time",
            "target_id",
            "completed_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("targets.id", ondelete="RESTRICT"), nullable=False
    )
    target_classification: Mapped[str] = mapped_column(String(32), nullable=False)
    bridge_id: Mapped[str] = mapped_column(String(36), nullable=False)
    bridge_addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    bridge_version: Mapped[str] = mapped_column(String(64), nullable=False)
    bridge_protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    bridge_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    before_json: Mapped[str] = mapped_column(String(512), nullable=False)
    requested_json: Mapped[str] = mapped_column(String(512), nullable=False)
    resulting_json: Mapped[str] = mapped_column(String(512), nullable=False)
    native_verified_json: Mapped[str] = mapped_column(String(512), nullable=False)
    bridge_verified_json: Mapped[str] = mapped_column(String(256), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_at: Mapped[str] = mapped_column(String(27), nullable=False)
    completed_at: Mapped[str] = mapped_column(String(27), nullable=False)


class ExternalProductionPolicyRow(Base):
    """Mandatory one-to-one policy state for an external-production target."""

    __tablename__ = "external_production_policies"
    __table_args__ = (
        CheckConstraint(
            "identity_generation >= 1 AND policy_revision >= 1",
            name="external_production_generations_positive",
        ),
        CheckConstraint(
            "confirmed_observation_digest IS NULL OR "
            "(length(confirmed_observation_digest)=64 AND "
            "confirmed_observation_digest NOT GLOB '*[^0-9a-f]*')",
            name="external_production_observation_digest_sha256",
        ),
        CheckConstraint(
            "designation_state<>'confirmed' OR "
            "(confirmed_observation_id IS NOT NULL AND "
            "confirmed_observation_digest IS NOT NULL AND "
            "designation_confirmed_by IS NOT NULL AND "
            "designation_confirmed_at IS NOT NULL)",
            name="external_production_confirmation_complete",
        ),
        CheckConstraint(
            "length(created_at)=27 AND length(updated_at)=27 AND "
            "(designation_confirmed_at IS NULL OR "
            "length(designation_confirmed_at)=27) AND "
            "(designation_invalidated_at IS NULL OR "
            "length(designation_invalidated_at)=27)",
            name="external_production_timestamps_format",
        ),
        CheckConstraint(
            "designation_confirmed_by IS NULL OR "
            "length(designation_confirmed_by) BETWEEN 1 AND 64",
            name="external_production_administrator_bounded",
        ),
        Index("ix_external_production_designation", "designation_state"),
    )

    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("external_kodi_targets.target_id", ondelete="CASCADE"),
        primary_key=True,
    )
    designation_state: Mapped[ProductionDesignationState] = mapped_column(
        Enum(
            ProductionDesignationState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="production_designation_state",
        ),
        nullable=False,
    )
    confirmed_observation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("external_kodi_target_observations.id", ondelete="RESTRICT"),
    )
    confirmed_observation_digest: Mapped[str | None] = mapped_column(String(64))
    identity_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    __mapper_args__: dict[str, object] = {  # noqa: RUF012
        "version_id_col": policy_revision,
        "version_id_generator": False,
    }
    mutations_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False)
    designation_confirmed_by: Mapped[str | None] = mapped_column(String(64))
    designation_confirmed_at: Mapped[str | None] = mapped_column(String(27))
    designation_invalidated_at: Mapped[str | None] = mapped_column(String(27))
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(27), nullable=False)


class ExternalProductionPolicyRevisionGuardRow(Base):
    """Permanent high-water mark preventing policy revision recreation."""

    __tablename__ = "external_production_policy_revision_guards"
    __table_args__ = (
        CheckConstraint(
            "maximum_policy_revision >= 1",
            name="external_production_policy_revision_guard_positive",
        ),
    )

    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("targets.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    maximum_policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)


class ProductionCapabilityAmendmentRow(Base):
    """Immutable Web-admin audit for the screenshot-only production maximum."""

    __tablename__ = "production_capability_amendments"
    __table_args__ = (
        CheckConstraint(
            "length(id)=36 AND operation IN ('add','remove') "
            "AND capability='screenshot' AND "
            "((operation='add' AND "
            "event_kind='production_capability_screenshot_added') OR "
            "(operation='remove' AND "
            "event_kind='production_capability_screenshot_removed'))",
            name="production_capability_amendment_semantics_closed",
        ),
        CheckConstraint(
            "length(before_configured_json) BETWEEN 2 AND 512 AND "
            "length(after_configured_json) BETWEEN 2 AND 512",
            name="production_capability_amendment_sets_bounded",
        ),
        CheckConstraint(
            "resulting_policy_revision>=1 AND length(actor) BETWEEN 1 AND 64 "
            "AND length(occurred_at)=27",
            name="production_capability_amendment_audit_bounded",
        ),
        UniqueConstraint(
            "target_id",
            "resulting_policy_revision",
            name="uq_production_capability_amendment_revision",
        ),
        Index(
            "ix_production_capability_amendment_target_time",
            "target_id",
            "occurred_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("targets.id", ondelete="RESTRICT"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(8), nullable=False)
    capability: Mapped[str] = mapped_column(String(32), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    before_configured_json: Mapped[str] = mapped_column(String(512), nullable=False)
    after_configured_json: Mapped[str] = mapped_column(String(512), nullable=False)
    resulting_policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[str] = mapped_column(String(27), nullable=False)


class ProductionMutationControlRow(Base):
    """Singleton deny-only global production mutation control."""

    __tablename__ = "production_mutation_control"
    __table_args__ = (
        CheckConstraint("id=1", name="production_mutation_control_singleton"),
        CheckConstraint("policy_epoch >= 1", name="production_policy_epoch_positive"),
        CheckConstraint(
            "length(updated_by) BETWEEN 1 AND 64 AND length(updated_at)=27",
            name="production_mutation_control_update_bounded",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    production_mutations_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False)
    policy_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(27), nullable=False)


class McpProductionTargetScopeRow(Base):
    """Immutable generation-bound production inspection authority."""

    __tablename__ = "mcp_production_target_scopes"
    __table_args__ = (
        CheckConstraint(
            "length(id)=36 AND identity_generation>=1",
            name="mcp_production_scope_identity_valid",
        ),
        CheckConstraint(
            "scope IN ('inspect','visual','navigate','request_transient_control')",
            name="mcp_production_scope_value_closed",
        ),
        CheckConstraint(
            "(scope='inspect' AND bridge_trust_revision IS NULL) OR "
            "(scope IN ('visual','navigate','request_transient_control') "
            "AND bridge_trust_revision>=1)",
            name="mcp_production_scope_bridge_binding",
        ),
        CheckConstraint(
            "state IN ('active','revoked')",
            name="mcp_production_scope_state_closed",
        ),
        CheckConstraint(
            "(state='active' AND revoked_at IS NULL AND revoked_by IS NULL) OR "
            "(state='revoked' AND revoked_at IS NOT NULL AND revoked_by IS NOT NULL)",
            name="mcp_production_scope_revocation_consistent",
        ),
        CheckConstraint(
            "length(created_at)=27 AND "
            "(revoked_at IS NULL OR "
            "(length(revoked_at)=27 AND revoked_at>=created_at))",
            name="mcp_production_scope_timestamps_valid",
        ),
        CheckConstraint(
            "length(created_by) BETWEEN 1 AND 64 AND "
            "(revoked_by IS NULL OR length(revoked_by) BETWEEN 1 AND 64)",
            name="mcp_production_scope_administrators_bounded",
        ),
        Index("ix_mcp_production_scope_client", "client_id", "state"),
        Index(
            "ix_mcp_production_scope_target_generation",
            "target_id",
            "identity_generation",
            "state",
        ),
        Index(
            "uq_mcp_production_scope_active",
            "client_id",
            "target_id",
            "identity_generation",
            "scope",
            unique=True,
            sqlite_where=text("state='active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mcp_clients.id", ondelete="RESTRICT"), nullable=False
    )
    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("external_production_policies.target_id", ondelete="RESTRICT"),
        nullable=False,
    )
    identity_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    bridge_trust_revision: Mapped[int | None] = mapped_column(Integer)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    revoked_at: Mapped[str | None] = mapped_column(String(27))
    revoked_by: Mapped[str | None] = mapped_column(String(64))


class ExternalBridgePairingRow(Base):
    """Pinned optional bridge identity and protected-secret references."""

    __tablename__ = "external_bridge_pairings"
    __table_args__ = (
        CheckConstraint(
            "bridge_port IS NULL OR bridge_port BETWEEN 1 AND 65535",
            name="external_bridge_port_valid",
        ),
        CheckConstraint(
            "pairing_token_reference IS NULL OR length(pairing_token_reference)=36",
            name="external_bridge_pairing_token_reference_shape",
        ),
        CheckConstraint(
            "bridge_secret_reference IS NULL OR length(bridge_secret_reference)=36",
            name="external_bridge_secret_reference_shape",
        ),
        CheckConstraint(
            "pairing_token_expires_at IS NULL OR length(pairing_token_expires_at)=27",
            name="external_bridge_token_expiry_shape",
        ),
        CheckConstraint(
            "length(observed_capabilities_json) BETWEEN 2 AND 256 AND "
            "length(verified_capabilities_json) BETWEEN 2 AND 256",
            name="external_bridge_capabilities_bounded",
        ),
        CheckConstraint(
            "observed_capabilities_json IN "
            "('[]','[\"screenshot\"]','[\"workspace_deployment\"]',"
            '\'["screenshot","workspace_deployment"]\') AND '
            "verified_capabilities_json IN "
            "('[]','[\"screenshot\"]','[\"workspace_deployment\"]',"
            '\'["screenshot","workspace_deployment"]\')',
            name="external_bridge_capabilities_closed",
        ),
        CheckConstraint(
            "(pairing_token_reference IS NULL) = (pairing_token_expires_at IS NULL)",
            name="external_bridge_pairing_token_complete",
        ),
        CheckConstraint(
            "pairing_token_reference IS NULL OR pairing_state='unpaired'",
            name="external_bridge_token_only_unpaired",
        ),
        CheckConstraint(
            "pairing_state<>'revoked' OR "
            "(pairing_token_reference IS NULL AND "
            "bridge_secret_reference IS NULL AND "
            "verified_capabilities_json='[]')",
            name="external_bridge_revocation_retires_authority",
        ),
        CheckConstraint(
            "observed_build_fingerprint IS NULL OR "
            "(length(observed_build_fingerprint)=64 AND "
            "observed_build_fingerprint NOT GLOB '*[^0-9a-f]*')",
            name="external_bridge_observed_fingerprint_sha256",
        ),
        CheckConstraint(
            "pinned_build_fingerprint IS NULL OR "
            "(length(pinned_build_fingerprint)=64 AND "
            "pinned_build_fingerprint NOT GLOB '*[^0-9a-f]*')",
            name="external_bridge_pinned_fingerprint_sha256",
        ),
        CheckConstraint(
            "endpoint_revision_digest IS NULL OR "
            "(length(endpoint_revision_digest)=64 AND "
            "endpoint_revision_digest NOT GLOB '*[^0-9a-f]*')",
            name="external_bridge_endpoint_revision_sha256",
        ),
        CheckConstraint(
            "(pairing_state='pairing_pending' AND "
            "bridge_secret_reference IS NOT NULL AND "
            "observed_bridge_id IS NOT NULL) OR "
            "pairing_state<>'pairing_pending'",
            name="external_bridge_pending_complete",
        ),
        CheckConstraint(
            "(pairing_state IN ('paired','needs_reverification') AND "
            "bridge_secret_reference IS NOT NULL AND "
            "pinned_bridge_id IS NOT NULL) OR "
            "pairing_state NOT IN ('paired','needs_reverification')",
            name="external_bridge_active_identity_complete",
        ),
        CheckConstraint(
            "(production_identity_generation IS NULL AND "
            "reviewed_build_id IS NULL AND production_confirmed_by IS NULL AND "
            "production_confirmed_at IS NULL) OR "
            "(production_identity_generation>=1 AND "
            "production_bridge_trust_revision>=1 AND reviewed_build_id IS NOT NULL "
            "AND production_confirmed_by IS NOT NULL AND "
            "length(production_confirmed_by) BETWEEN 1 AND 64 AND "
            "length(production_confirmed_at)=27)",
            name="external_bridge_production_binding_complete",
        ),
        CheckConstraint(
            "production_bridge_trust_revision IS NULL OR "
            "production_bridge_trust_revision>=1",
            name="external_bridge_production_revision_positive",
        ),
        Index(
            "ix_external_bridge_pairing_state",
            "pairing_state",
            "reachability_state",
        ),
    )

    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("external_kodi_targets.target_id", ondelete="CASCADE"),
        primary_key=True,
    )
    bridge_port: Mapped[int | None] = mapped_column(Integer)
    pairing_state: Mapped[ExternalBridgePairingState] = mapped_column(
        Enum(
            ExternalBridgePairingState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="external_bridge_pairing_state",
        ),
        nullable=False,
    )
    reachability_state: Mapped[ExternalBridgeReachabilityState] = mapped_column(
        Enum(
            ExternalBridgeReachabilityState,
            values_callable=_enum_values,
            native_enum=False,
            create_constraint=True,
            name="external_bridge_reachability_state",
        ),
        nullable=False,
    )
    pairing_token_reference: Mapped[str | None] = mapped_column(String(36))
    bridge_secret_reference: Mapped[str | None] = mapped_column(String(36))
    pairing_token_expires_at: Mapped[str | None] = mapped_column(String(27))
    observed_bridge_id: Mapped[str | None] = mapped_column(String(36))
    observed_protocol_version: Mapped[str | None] = mapped_column(String(16))
    observed_addon_id: Mapped[str | None] = mapped_column(String(128))
    observed_addon_version: Mapped[str | None] = mapped_column(String(64))
    observed_kodi_version: Mapped[str | None] = mapped_column(String(64))
    observed_build_fingerprint: Mapped[str | None] = mapped_column(String(64))
    observed_capabilities_json: Mapped[str] = mapped_column(String(256), nullable=False)
    pinned_bridge_id: Mapped[str | None] = mapped_column(String(36))
    pinned_protocol_version: Mapped[str | None] = mapped_column(String(16))
    pinned_addon_id: Mapped[str | None] = mapped_column(String(128))
    pinned_addon_version: Mapped[str | None] = mapped_column(String(64))
    pinned_kodi_version: Mapped[str | None] = mapped_column(String(64))
    pinned_build_fingerprint: Mapped[str | None] = mapped_column(String(64))
    verified_capabilities_json: Mapped[str] = mapped_column(String(256), nullable=False)
    endpoint_revision_digest: Mapped[str | None] = mapped_column(String(64))
    token_generated_at: Mapped[str | None] = mapped_column(String(27))
    pairing_attempted_at: Mapped[str | None] = mapped_column(String(27))
    paired_at: Mapped[str | None] = mapped_column(String(27))
    verified_at: Mapped[str | None] = mapped_column(String(27))
    revoked_at: Mapped[str | None] = mapped_column(String(27))
    production_identity_generation: Mapped[int | None] = mapped_column(Integer)
    production_bridge_trust_revision: Mapped[int | None] = mapped_column(Integer)
    reviewed_build_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("reviewed_bridge_builds.id", ondelete="RESTRICT")
    )
    production_confirmed_by: Mapped[str | None] = mapped_column(String(64))
    production_confirmed_at: Mapped[str | None] = mapped_column(String(27))


class ReviewedBridgeBuildRow(Base):
    """Administrator-reviewed immutable production bridge provenance."""

    __tablename__ = "reviewed_bridge_builds"
    __table_args__ = (
        CheckConstraint("length(id)=36", name="reviewed_bridge_build_id_shape"),
        CheckConstraint(
            "length(semantic_fingerprint)=64 AND "
            "semantic_fingerprint NOT GLOB '*[^0-9a-f]*' AND "
            "length(zip_sha256)=64 AND zip_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="reviewed_bridge_build_digests_sha256",
        ),
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 64 AND "
            "length(protocol_version) BETWEEN 1 AND 16",
            name="reviewed_bridge_build_identity_bounded",
        ),
        CheckConstraint(
            "capabilities_json IN "
            '(\'["screenshot"]\',\'["screenshot","workspace_deployment"]\') '
            "AND length(supported_kodi_versions_json) BETWEEN 5 AND 256",
            name="reviewed_bridge_build_sets_bounded",
        ),
        CheckConstraint(
            "length(source_reference) BETWEEN 1 AND 512 AND "
            "length(reviewed_by) BETWEEN 1 AND 64 AND length(reviewed_at)=27",
            name="reviewed_bridge_build_provenance_bounded",
        ),
        CheckConstraint(
            "state IN ('active','retired') AND "
            "((state='active' AND retired_by IS NULL AND retired_at IS NULL) OR "
            "(state='retired' AND retired_by IS NOT NULL AND retired_at IS NOT NULL))",
            name="reviewed_bridge_build_state_consistent",
        ),
        UniqueConstraint(
            "semantic_fingerprint",
            "addon_id",
            "addon_version",
            "protocol_version",
            name="uq_reviewed_bridge_semantic_identity",
        ),
        UniqueConstraint("zip_sha256", name="uq_reviewed_bridge_zip_sha256"),
        Index("ix_reviewed_bridge_build_state", "state", "semantic_fingerprint"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    semantic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    zip_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    addon_version: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(16), nullable=False)
    capabilities_json: Mapped[str] = mapped_column(String(256), nullable=False)
    supported_kodi_versions_json: Mapped[str] = mapped_column(
        String(256), nullable=False
    )
    source_reference: Mapped[str] = mapped_column(String(512), nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_at: Mapped[str] = mapped_column(String(27), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    retired_by: Mapped[str | None] = mapped_column(String(64))
    retired_at: Mapped[str | None] = mapped_column(String(27))


class ProductionBridgeHttpAcknowledgementRow(Base):
    """Immutable exact private-HTTP production bridge risk acknowledgement."""

    __tablename__ = "production_bridge_http_acknowledgements"
    __table_args__ = (
        CheckConstraint(
            "length(id)=36 AND identity_generation>=1 AND "
            "bridge_trust_revision>=1 AND transport_policy_revision>=1",
            name="production_bridge_ack_generations_positive",
        ),
        CheckConstraint(
            "protocol='jsonrpc_http' AND length(ip_literal) BETWEEN 2 AND 64 "
            "AND native_port BETWEEN 1 AND 65535 AND bridge_port BETWEEN 1 AND 65535",
            name="production_bridge_ack_endpoint_valid",
        ),
        CheckConstraint(
            "length(endpoint_digest)=64 AND endpoint_digest NOT GLOB '*[^0-9a-f]*'",
            name="production_bridge_ack_digest_sha256",
        ),
        CheckConstraint(
            "length(acknowledged_by) BETWEEN 1 AND 64 AND length(acknowledged_at)=27",
            name="production_bridge_ack_administrator_bounded",
        ),
        UniqueConstraint(
            "target_id",
            "identity_generation",
            "bridge_trust_revision",
            "endpoint_digest",
            "transport_policy_revision",
            name="uq_production_bridge_http_ack_binding",
        ),
        Index(
            "ix_production_bridge_http_ack_target",
            "target_id",
            "identity_generation",
            "bridge_trust_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("external_production_policies.target_id", ondelete="RESTRICT"),
        nullable=False,
    )
    identity_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    bridge_trust_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    ip_literal: Mapped[str] = mapped_column(String(64), nullable=False)
    native_port: Mapped[int] = mapped_column(Integer, nullable=False)
    bridge_port: Mapped[int] = mapped_column(Integer, nullable=False)
    endpoint_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    transport_policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    acknowledged_by: Mapped[str] = mapped_column(String(64), nullable=False)
    acknowledged_at: Mapped[str] = mapped_column(String(27), nullable=False)


class ProductionNativeControlHttpAcknowledgementRow(Base):
    """Immutable exact native-HTTP production-control risk acknowledgement."""

    __tablename__ = "production_native_control_http_acknowledgements"
    __table_args__ = (
        CheckConstraint(
            "length(id)=36 AND identity_generation>=1 AND policy_revision>=1 "
            "AND transport_policy_revision>=1",
            name="production_native_control_ack_generations_positive",
        ),
        CheckConstraint(
            "protocol='jsonrpc_http' AND length(ip_literal) BETWEEN 2 AND 64 "
            "AND native_port BETWEEN 1 AND 65535",
            name="production_native_control_ack_endpoint_valid",
        ),
        CheckConstraint(
            "length(endpoint_digest)=64 AND endpoint_digest NOT GLOB '*[^0-9a-f]*'",
            name="production_native_control_ack_digest_sha256",
        ),
        CheckConstraint(
            "length(acknowledged_by) BETWEEN 1 AND 64 AND length(acknowledged_at)=27",
            name="production_native_control_ack_administrator_bounded",
        ),
        UniqueConstraint(
            "target_id",
            "identity_generation",
            "policy_revision",
            "endpoint_digest",
            "transport_policy_revision",
            name="uq_production_native_control_http_ack_binding",
        ),
        Index(
            "ix_production_native_control_http_ack_target",
            "target_id",
            "identity_generation",
            "policy_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("external_production_policies.target_id", ondelete="RESTRICT"),
        nullable=False,
    )
    identity_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    ip_literal: Mapped[str] = mapped_column(String(64), nullable=False)
    native_port: Mapped[int] = mapped_column(Integer, nullable=False)
    endpoint_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    transport_policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    acknowledged_by: Mapped[str] = mapped_column(String(64), nullable=False)
    acknowledged_at: Mapped[str] = mapped_column(String(27), nullable=False)


class ExternalBridgeRequestReceiptRow(Base):
    """Bounded durable identity for one authenticated bridge request."""

    __tablename__ = "external_bridge_request_receipts"
    __table_args__ = (
        CheckConstraint(
            "operation IN "
            "('bridge_identity','bridge_capabilities','capture_screenshot')",
            name="external_bridge_receipt_operation_closed",
        ),
        CheckConstraint(
            "state IN ('pending','completed','ambiguous','rejected')",
            name="external_bridge_receipt_state_closed",
        ),
        CheckConstraint("length(nonce)=43", name="external_bridge_nonce_shape"),
        CheckConstraint(
            "response_digest IS NULL OR "
            "(length(response_digest)=64 AND "
            "response_digest NOT GLOB '*[^0-9a-f]*')",
            name="external_bridge_response_digest_sha256",
        ),
        CheckConstraint(
            "byte_size IS NULL OR byte_size BETWEEN 1 AND 8388608",
            name="external_bridge_receipt_size_bounded",
        ),
        CheckConstraint(
            "(width IS NULL AND height IS NULL) OR "
            "(width BETWEEN 1 AND 4096 AND height BETWEEN 1 AND 2160)",
            name="external_bridge_receipt_dimensions_bounded",
        ),
        CheckConstraint(
            "length(created_at)=27 AND length(expires_at)=27 AND "
            "(completed_at IS NULL OR length(completed_at)=27)",
            name="external_bridge_receipt_timestamps_format",
        ),
        CheckConstraint(
            "(state='pending' AND completed_at IS NULL AND "
            "response_digest IS NULL AND byte_size IS NULL AND "
            "width IS NULL AND height IS NULL) OR "
            "(state='completed' AND completed_at IS NOT NULL AND "
            "response_digest IS NOT NULL AND byte_size IS NOT NULL AND "
            "width IS NOT NULL AND height IS NOT NULL) OR "
            "(state IN ('ambiguous','rejected') AND completed_at IS NOT NULL)",
            name="external_bridge_receipt_terminal_coherent",
        ),
        UniqueConstraint("target_id", "nonce", name="uq_external_bridge_nonce"),
        Index(
            "ix_external_bridge_receipt_expiry",
            "expires_at",
            "target_id",
            "request_id",
        ),
    )

    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("external_bridge_pairings.target_id", ondelete="CASCADE"),
        primary_key=True,
    )
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(43), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    response_digest: Mapped[str | None] = mapped_column(String(64))
    byte_size: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String(27))
    expires_at: Mapped[str] = mapped_column(String(27), nullable=False)


class McpProductionScreenshotReceiptRow(Base):
    """Durable production screenshot replay identity; pixels are never stored."""

    __tablename__ = "mcp_production_screenshot_receipts"
    __table_args__ = (
        CheckConstraint(
            "length(client_id)=36 AND length(request_id)=36 AND "
            "length(target_id)=36 AND length(bridge_request_id)=36",
            name="production_screenshot_receipt_uuid_lengths",
        ),
        CheckConstraint(
            "length(parameter_digest)=64 AND "
            "parameter_digest NOT GLOB '*[^0-9a-f]*' AND "
            "identity_generation>=1 AND bridge_trust_revision>=1",
            name="production_screenshot_receipt_binding_valid",
        ),
        CheckConstraint(
            "state IN ('pending','completed','ambiguous','rejected')",
            name="production_screenshot_receipt_state_closed",
        ),
        CheckConstraint(
            "response_digest IS NULL OR (length(response_digest)=64 AND "
            "response_digest NOT GLOB '*[^0-9a-f]*')",
            name="production_screenshot_receipt_digest_sha256",
        ),
        CheckConstraint(
            "byte_size IS NULL OR byte_size BETWEEN 1 AND 8388608",
            name="production_screenshot_receipt_size_bounded",
        ),
        CheckConstraint(
            "(width IS NULL AND height IS NULL) OR "
            "(width BETWEEN 1 AND 4096 AND height BETWEEN 1 AND 2160)",
            name="production_screenshot_receipt_dimensions_bounded",
        ),
        CheckConstraint(
            "length(created_at)=27 AND length(expires_at)=27 AND "
            "(completed_at IS NULL OR length(completed_at)=27)",
            name="production_screenshot_receipt_timestamps_format",
        ),
        CheckConstraint(
            "(state='pending' AND completed_at IS NULL AND response_digest IS NULL "
            "AND byte_size IS NULL AND width IS NULL AND height IS NULL) OR "
            "(state='completed' AND completed_at IS NOT NULL AND "
            "response_digest IS NOT NULL AND byte_size IS NOT NULL AND "
            "width IS NOT NULL AND height IS NOT NULL) OR "
            "(state IN ('ambiguous','rejected') AND completed_at IS NOT NULL)",
            name="production_screenshot_receipt_terminal_coherent",
        ),
        UniqueConstraint(
            "target_id",
            "bridge_request_id",
            name="uq_production_screenshot_bridge_request",
        ),
        Index(
            "ix_production_screenshot_receipt_capacity",
            "expires_at",
            "state",
            "target_id",
        ),
    )

    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mcp_clients.id", ondelete="RESTRICT"), primary_key=True
    )
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("external_production_policies.target_id", ondelete="RESTRICT"),
        nullable=False,
    )
    parameter_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    bridge_trust_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    bridge_request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    response_digest: Mapped[str | None] = mapped_column(String(64))
    byte_size: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String(27))
    expires_at: Mapped[str] = mapped_column(String(27), nullable=False)


class ExternalDevelopmentSourceBaselineRow(Base):
    """Web-established exact source baseline for one external target."""

    __tablename__ = "external_development_source_baselines"
    __table_args__ = (
        CheckConstraint(
            "length(target_id)=36 AND length(development_source_id)=36 AND "
            "length(bridge_id)=36",
            name="external_source_baseline_uuid_lengths",
        ),
        CheckConstraint(
            "state IN ('current','stale','unknown')",
            name="external_source_baseline_state_closed",
        ),
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 128",
            name="external_source_baseline_identity_bounded",
        ),
        CheckConstraint(
            "length(manifest_digest)=71 AND substr(manifest_digest,1,7)='sha256:'",
            name="external_source_baseline_digest_valid",
        ),
        CheckConstraint(
            "file_count BETWEEN 1 AND 25000 AND total_bytes BETWEEN 1 AND 1073741824",
            name="external_source_baseline_counts_bounded",
        ),
        CheckConstraint(
            "length(established_at)=27 AND length(checked_at)=27 AND "
            "(invalidated_at IS NULL OR length(invalidated_at)=27)",
            name="external_source_baseline_timestamps_format",
        ),
        CheckConstraint(
            "(state='current' AND invalidated_at IS NULL) OR "
            "(state<>'current' AND invalidated_at IS NOT NULL)",
            name="external_source_baseline_state_time_consistent",
        ),
        Index("ix_external_source_baseline_state", "target_id", "state"),
    )

    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("external_bridge_pairings.target_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    development_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_sources.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    bridge_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    addon_version: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    established_at: Mapped[str] = mapped_column(String(27), nullable=False)
    checked_at: Mapped[str] = mapped_column(String(27), nullable=False)
    invalidated_at: Mapped[str | None] = mapped_column(String(27))


class ExternalBridgeDeploymentReceiptRow(Base):
    """Lab-side durable correlation for one bridge deployment receipt."""

    __tablename__ = "external_bridge_deployment_receipts"
    __table_args__ = (
        CheckConstraint(
            "length(operation_id)=36 AND length(target_id)=36 AND "
            "length(bridge_id)=36 AND length(bridge_request_id)=36 AND "
            "(bridge_receipt_id IS NULL OR length(bridge_receipt_id)=36)",
            name="external_bridge_deployment_receipt_uuid_lengths",
        ),
        CheckConstraint(
            "state IN ('pending','succeeded','failed','ambiguous')",
            name="external_bridge_deployment_receipt_state_closed",
        ),
        CheckConstraint(
            "length(expected_manifest_digest)=71 AND "
            "substr(expected_manifest_digest,1,7)='sha256:'",
            name="external_bridge_deployment_receipt_digest_valid",
        ),
        CheckConstraint(
            "classification IS NULL OR length(classification) BETWEEN 1 AND 64",
            name="external_bridge_deployment_receipt_classification_bounded",
        ),
        CheckConstraint(
            "file_count BETWEEN 1 AND 25000 AND total_bytes BETWEEN 1 AND 1073741824",
            name="external_bridge_deployment_receipt_counts_bounded",
        ),
        CheckConstraint(
            "length(created_at)=27 AND "
            "(completed_at IS NULL OR length(completed_at)=27) AND "
            "(reconciled_at IS NULL OR length(reconciled_at)=27)",
            name="external_bridge_deployment_receipt_timestamps_format",
        ),
        CheckConstraint(
            "(state='pending' AND completed_at IS NULL AND "
            "bridge_receipt_id IS NULL) OR "
            "(state IN ('succeeded','failed') AND completed_at IS NOT NULL AND "
            "bridge_receipt_id IS NOT NULL) OR state='ambiguous'",
            name="external_bridge_deployment_receipt_terminal_shape",
        ),
        UniqueConstraint(
            "target_id",
            "bridge_request_id",
            name="uq_external_bridge_deployment_request",
        ),
        Index(
            "ix_external_bridge_deployment_receipt_state",
            "target_id",
            "state",
            "created_at",
        ),
    )

    operation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("mcp_workspace_deployment_operations.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("external_bridge_pairings.target_id", ondelete="RESTRICT"),
        nullable=False,
    )
    bridge_id: Mapped[str] = mapped_column(String(36), nullable=False)
    bridge_request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    bridge_receipt_id: Mapped[str | None] = mapped_column(String(36))
    expected_manifest_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    classification: Mapped[str | None] = mapped_column(String(64))
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String(27))
    reconciled_at: Mapped[str | None] = mapped_column(String(27))


class ExternalBridgeEventRow(Base):
    """Content-free semantic bridge administration and capture audit."""

    __tablename__ = "external_bridge_events"
    __table_args__ = (
        CheckConstraint(
            "event_kind IN ('pairing_token_generated','pairing_attempted',"
            "'bridge_paired','pairing_failed','bridge_revoked',"
            "'bridge_reverified','bridge_reverification_required',"
            "'screenshot_requested','screenshot_completed','screenshot_failed',"
            "'baseline_established','baseline_invalidated','deployment_requested',"
            "'deployment_completed','deployment_failed','deployment_reconciled',"
            "'execution_completed','execution_failed','production_build_reviewed',"
            "'production_build_retired','production_transport_acknowledged',"
            "'production_bridge_confirmed',"
            "'production_bridge_revision_advanced')",
            name="external_bridge_event_kind_closed",
        ),
        CheckConstraint(
            "classification IS NULL OR length(classification) BETWEEN 1 AND 64",
            name="external_bridge_event_classification_bounded",
        ),
        CheckConstraint(
            "content_digest IS NULL OR "
            "(length(content_digest)=64 AND "
            "content_digest NOT GLOB '*[^0-9a-f]*')",
            name="external_bridge_event_digest_sha256",
        ),
        CheckConstraint(
            "length(occurred_at)=27", name="external_bridge_event_timestamp_format"
        ),
        Index("ix_external_bridge_event_time", "target_id", "occurred_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("external_bridge_pairings.target_id", ondelete="CASCADE"),
        nullable=False,
    )
    bridge_id: Mapped[str | None] = mapped_column(String(36))
    request_id: Mapped[str | None] = mapped_column(String(36))
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    classification: Mapped[str | None] = mapped_column(String(64))
    byte_size: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    content_digest: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[str] = mapped_column(String(27), nullable=False)


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


class ManagedScreenshotRow(Base):
    """Latest-only private screenshot metadata for one live instance."""

    __tablename__ = "managed_screenshots"
    __table_args__ = (
        UniqueConstraint("capture_id", name="uq_managed_screenshot_capture"),
        CheckConstraint("mime_type = 'image/png'", name="managed_screenshot_png_only"),
        CheckConstraint(
            "width BETWEEN 1 AND 4096 AND height BETWEEN 1 AND 2160",
            name="managed_screenshot_dimensions_bounded",
        ),
        CheckConstraint(
            "byte_size BETWEEN 1 AND 8388608",
            name="managed_screenshot_size_bounded",
        ),
        CheckConstraint(
            "length(captured_at) BETWEEN 20 AND 32 AND "
            "length(last_attempt_at) BETWEEN 20 AND 32",
            name="managed_screenshot_timestamps_bounded",
        ),
        CheckConstraint(
            "last_error_code IS NULL OR length(last_error_code) BETWEEN 1 AND 64",
            name="managed_screenshot_error_bounded",
        ),
        CheckConstraint(
            "(capture_id IS NULL AND captured_at IS NULL AND mime_type IS NULL "
            "AND width IS NULL AND height IS NULL AND byte_size IS NULL) OR "
            "(capture_id IS NOT NULL AND captured_at IS NOT NULL "
            "AND mime_type IS NOT NULL AND width IS NOT NULL "
            "AND height IS NOT NULL AND byte_size IS NOT NULL)",
            name="managed_screenshot_current_complete",
        ),
    )

    managed_instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("managed_instances.target_id", ondelete="CASCADE"),
        primary_key=True,
    )
    capture_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    captured_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_attempt_at: Mapped[str] = mapped_column(String(32), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


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


class RuntimeRecipeInstallationRow(Base):
    """One durable attempt to materialize a reviewed recipe as a local image."""

    __tablename__ = "runtime_recipe_installations"
    __table_args__ = (
        Index(
            "uq_runtime_recipe_install_active_generation",
            "generation",
            unique=True,
            sqlite_where=text(
                "state IN ('pending','acquiring','assembling','validating')"
            ),
        ),
        Index("ix_runtime_recipe_install_revision", "runtime_revision_id"),
        CheckConstraint("recipe_revision > 0", name="runtime_recipe_revision_positive"),
        CheckConstraint(
            "length(recipe_digest)=71 AND substr(recipe_digest,1,7)='sha256:'",
            name="runtime_recipe_digest_shape",
        ),
        CheckConstraint(
            "state IN ('pending','acquiring','assembling',"
            "'validating','ready','failed')",
            name="runtime_recipe_install_state_closed",
        ),
        CheckConstraint(
            "(state='ready' AND completed_at IS NOT NULL AND image_id IS NOT NULL "
            "AND runtime_revision_id IS NOT NULL AND attestation_json IS NOT NULL "
            "AND validation_json IS NOT NULL AND failure_code IS NULL) OR "
            "(state='failed' AND completed_at IS NOT NULL AND failure_code IS NOT NULL "
            "AND runtime_revision_id IS NULL) OR "
            "(state IN ('pending','acquiring','assembling','validating') "
            "AND completed_at IS NULL AND runtime_revision_id IS NULL "
            "AND failure_code IS NULL)",
            name="runtime_recipe_install_terminal_consistent",
        ),
        CheckConstraint(
            "attestation_json IS NULL OR length(attestation_json) BETWEEN 1 AND 65536",
            name="runtime_recipe_attestation_bounded",
        ),
        CheckConstraint(
            "validation_json IS NULL OR length(validation_json) BETWEEN 1 AND 8192",
            name="runtime_recipe_validation_bounded",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    recipe_id: Mapped[str] = mapped_column(String(64), nullable=False)
    recipe_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    recipe_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    generation: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[RuntimeRecipeInstallationState] = mapped_column(
        Enum(
            RuntimeRecipeInstallationState,
            values_callable=_enum_values,
            native_enum=False,
        ),
        nullable=False,
    )
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(27), nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String(27), nullable=True)
    image_id: Mapped[str | None] = mapped_column(String(71), nullable=True)
    runtime_revision_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    attestation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(48), nullable=True)


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


class McpPresetRow(Base):
    """Built-in or custom immutable-permission MCP authority preset."""

    __tablename__ = "mcp_presets"
    __table_args__ = (
        CheckConstraint(
            "length(display_name) BETWEEN 1 AND 120 "
            "AND display_name = trim(display_name)",
            name="mcp_preset_display_name_bounded",
        ),
        UniqueConstraint("display_name", name="uq_mcp_preset_display_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    builtin: Mapped[bool] = mapped_column(Boolean, nullable=False)
    assignable: Mapped[bool] = mapped_column(Boolean, nullable=False)


class McpPresetPermissionRow(Base):
    """Open-ended permission name assigned to one MCP preset."""

    __tablename__ = "mcp_preset_permissions"
    __table_args__ = (
        CheckConstraint(
            "length(permission) BETWEEN 1 AND 128 "
            "AND permission = lower(permission) "
            "AND permission = trim(permission)",
            name="mcp_preset_permission_bounded",
        ),
    )

    preset_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("mcp_presets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission: Mapped[str] = mapped_column(String(128), primary_key=True)


class McpClientRow(Base):
    """Durable MCP caller identity; protocol clientInfo is never authority."""

    __tablename__ = "mcp_clients"
    __table_args__ = (
        CheckConstraint(
            "length(display_name) BETWEEN 1 AND 120 "
            "AND display_name = trim(display_name)",
            name="mcp_client_display_name_bounded",
        ),
        CheckConstraint(
            "length(created_at) = 27 "
            "AND (last_used_at IS NULL OR length(last_used_at) = 27)",
            name="mcp_client_timestamps_format",
        ),
        CheckConstraint(
            "(enabled = 1 AND disabled_at IS NULL) OR "
            "(enabled = 0 AND disabled_at IS NOT NULL "
            "AND length(disabled_at) = 27)",
            name="mcp_client_disabled_timestamp_consistent",
        ),
        CheckConstraint(
            "authority_revision >= 1", name="mcp_client_authority_revision_positive"
        ),
        Index("ix_mcp_client_preset", "preset_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    preset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mcp_presets.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    last_used_at: Mapped[str | None] = mapped_column(String(27), nullable=True)
    disabled_at: Mapped[str | None] = mapped_column(String(27), nullable=True)
    authority_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class McpCredentialRow(Base):
    """Verifier-only bearer credential belonging to one MCP client."""

    __tablename__ = "mcp_credentials"
    __table_args__ = (
        CheckConstraint(
            "length(token_digest) = 64 AND token_digest NOT GLOB '*[^0-9a-f]*'",
            name="mcp_credential_digest_sha256",
        ),
        CheckConstraint(
            "length(created_at) = 27 "
            "AND (expires_at IS NULL OR length(expires_at) = 27) "
            "AND (revoked_at IS NULL OR length(revoked_at) = 27) "
            "AND (last_used_at IS NULL OR length(last_used_at) = 27)",
            name="mcp_credential_timestamps_format",
        ),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name="mcp_credential_expiry_after_creation",
        ),
        UniqueConstraint("token_digest", name="uq_mcp_credential_token_digest"),
        Index("ix_mcp_credential_client", "client_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mcp_clients.id", ondelete="CASCADE"), nullable=False
    )
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    expires_at: Mapped[str | None] = mapped_column(String(27), nullable=True)
    revoked_at: Mapped[str | None] = mapped_column(String(27), nullable=True)
    last_used_at: Mapped[str | None] = mapped_column(String(27), nullable=True)


class McpClientTargetGrantRow(Base):
    """Exact client-to-target grant; application policy remains class-specific."""

    __tablename__ = "mcp_client_target_grants"
    __table_args__ = (
        CheckConstraint(
            "length(created_at) = 27", name="mcp_client_target_grant_timestamp_format"
        ),
        CheckConstraint(
            "length(grant_id) = 36", name="mcp_client_target_grant_id_shape"
        ),
        UniqueConstraint("grant_id", name="uq_mcp_client_target_grant_id"),
        Index("ix_mcp_grant_target", "target_id"),
    )

    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mcp_clients.id", ondelete="CASCADE"), primary_key=True
    )
    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("targets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    grant_id: Mapped[str] = mapped_column(String(36), nullable=False)


class McpClientTargetGrantIdentityRow(Base):
    """Permanent one-use identity ledger for generic client-target grants."""

    __tablename__ = "mcp_client_target_grant_identities"
    __table_args__ = (
        CheckConstraint(
            "length(grant_id)=36 AND length(client_id)=36 AND length(target_id)=36 "
            "AND length(created_at)=27",
            name="mcp_client_target_grant_identity_shapes",
        ),
    )

    grant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)


class McpClientDevelopmentSourceGrantRow(Base):
    """Exact MCP client-to-DevelopmentSource grant; never a wildcard."""

    __tablename__ = "mcp_client_development_source_grants"
    __table_args__ = (
        CheckConstraint(
            "length(created_at) = 27", name="mcp_source_grant_timestamp_format"
        ),
        Index("ix_mcp_source_grant_source", "development_source_id"),
    )

    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mcp_clients.id", ondelete="CASCADE"), primary_key=True
    )
    development_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_sources.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)


class McpAgentWorkspaceBindingRow(Base):
    """One durable MCP owner binding for an exact protected workspace."""

    __tablename__ = "mcp_agent_workspace_bindings"
    __table_args__ = (
        CheckConstraint(
            "binding_origin IN ('web_admin','mcp_created')",
            name="mcp_workspace_binding_origin_closed",
        ),
        CheckConstraint(
            "length(created_at) = 27",
            name="mcp_workspace_binding_timestamp_format",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "development_source_id"],
            ["agent_workspaces.id", "agent_workspaces.development_source_id"],
            ondelete="RESTRICT",
        ),
        Index(
            "ix_mcp_workspace_binding_client_source",
            "client_id",
            "development_source_id",
            "workspace_id",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mcp_clients.id", ondelete="CASCADE"), nullable=False
    )
    development_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    binding_origin: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)


class McpDevelopmentRequestRow(Base):
    """Bounded durable replay identity for Phase 12 development mutations."""

    __tablename__ = "mcp_development_requests"
    __table_args__ = (
        CheckConstraint(
            "length(id) = 36 AND length(client_id) = 36 AND "
            "length(request_id) = 36 AND length(development_source_id) = 36 AND "
            "length(workspace_id) = 36 AND "
            "(creation_operation_id IS NULL OR length(creation_operation_id) = 36) "
            "AND (operation_id IS NULL OR length(operation_id) = 36)",
            name="mcp_development_request_uuid_lengths",
        ),
        CheckConstraint(
            "operation_kind IN "
            "('workspace_create','workspace_deploy','workspace_execute',"
            "'promotion_request')",
            name="mcp_development_request_operation_closed",
        ),
        CheckConstraint(
            "(operation_kind = 'workspace_create' AND "
            "creation_operation_id IS NOT NULL AND operation_id IS NULL) OR "
            "(operation_kind IN "
            "('workspace_deploy','workspace_execute','promotion_request') AND "
            "creation_operation_id IS NULL AND operation_id IS NOT NULL)",
            name="mcp_development_request_operation_shape",
        ),
        CheckConstraint(
            "state = 'allocated'",
            name="mcp_development_request_state_closed",
        ),
        CheckConstraint(
            "length(parameter_fingerprint) = 64 AND "
            "parameter_fingerprint = lower(parameter_fingerprint)",
            name="mcp_development_request_fingerprint_sha256",
        ),
        CheckConstraint(
            "length(created_at) = 27 AND length(updated_at) = 27",
            name="mcp_development_request_timestamp_format",
        ),
        UniqueConstraint(
            "client_id",
            "request_id",
            "operation_kind",
            name="uq_mcp_development_request_client_request_kind",
        ),
        Index("ix_mcp_development_request_client_created", "client_id", "created_at"),
        Index("ix_mcp_development_request_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mcp_clients.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    parameter_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    development_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    creation_operation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agent_workspace_creation_operations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(27), nullable=False)


class McpWorkspaceValidationRow(Base):
    """Latest successful closed structural validation for one workspace."""

    __tablename__ = "mcp_workspace_validations"
    __table_args__ = (
        CheckConstraint(
            "length(id) = 36 AND length(workspace_id) = 36 AND "
            "length(development_source_id) = 36",
            name="mcp_workspace_validation_uuid_lengths",
        ),
        CheckConstraint(
            "generation >= 0", name="mcp_workspace_validation_generation_nonnegative"
        ),
        CheckConstraint(
            "classification = 'passed'",
            name="mcp_workspace_validation_classification_closed",
        ),
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 128 AND "
            "length(extension_types_json) BETWEEN 2 AND 4096",
            name="mcp_workspace_validation_identity_bounded",
        ),
        CheckConstraint(
            "length(validated_at) = 27",
            name="mcp_workspace_validation_timestamp_format",
        ),
        CheckConstraint(
            "tree_digest IS NULL OR (length(tree_digest)=71 AND "
            "substr(tree_digest,1,7)='sha256:')",
            name="mcp_workspace_validation_tree_digest_valid",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    development_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    tree_digest: Mapped[str | None] = mapped_column(String(71))
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    addon_version: Mapped[str] = mapped_column(String(128), nullable=False)
    extension_types_json: Mapped[str] = mapped_column(Text, nullable=False)
    validated_at: Mapped[str] = mapped_column(String(27), nullable=False)


class McpWorkspaceDeploymentOperationRow(Base):
    """Path-free exact-generation workspace deployment evidence."""

    __tablename__ = "mcp_workspace_deployment_operations"
    __table_args__ = (
        CheckConstraint(
            "length(id)=36 AND (client_id IS NULL OR length(client_id)=36) "
            "AND length(request_id)=36 "
            "AND length(workspace_id)=36 AND length(development_source_id)=36 "
            "AND length(target_id)=36 AND "
            "(runtime_revision_id IS NULL OR length(runtime_revision_id)=36) AND "
            "(bridge_id IS NULL OR length(bridge_id)=36) AND "
            "(bridge_request_id IS NULL OR length(bridge_request_id)=36) AND "
            "(bridge_receipt_id IS NULL OR length(bridge_receipt_id)=36)",
            name="mcp_workspace_deployment_uuid_lengths",
        ),
        CheckConstraint(
            "target_class IN ('internal_lab','external_development')",
            name="mcp_workspace_deployment_target_class_closed",
        ),
        CheckConstraint(
            "(target_class='internal_lab' AND deployment_mode='direct' "
            "AND runtime_revision_id IS NOT NULL AND bridge_id IS NULL "
            "AND bridge_request_id IS NULL AND bridge_receipt_id IS NULL) OR "
            "(target_class='external_development' AND runtime_revision_id IS NULL "
            "AND bridge_id IS NOT NULL AND ((deployment_mode='direct' "
            "AND bridge_request_id IS NOT NULL) OR (deployment_mode='repository' "
            "AND bridge_request_id IS NULL AND bridge_receipt_id IS NULL)))",
            name="mcp_workspace_deployment_target_shape",
        ),
        CheckConstraint(
            "deployment_mode IN ('direct','repository')",
            name="mcp_workspace_deployment_mode_closed",
        ),
        CheckConstraint(
            "(initiator_kind='mcp' AND client_id IS NOT NULL) OR "
            "(initiator_kind='web' AND client_id IS NULL "
            "AND deployment_mode='repository')",
            name="mcp_workspace_deployment_initiator_closed",
        ),
        CheckConstraint(
            "generation >= 0", name="mcp_workspace_deployment_generation_nonnegative"
        ),
        CheckConstraint(
            "state IN ('pending','running','succeeded','failed',"
            "'ambiguous','reconciliation_required')",
            name="mcp_workspace_deployment_state_closed",
        ),
        CheckConstraint(
            "phase IN ('claimed','preflight','stop','sync','transfer','replace',"
            "'receipt','start','readiness','inventory','finalize','package',"
            "'publication_approval','awaiting_kodi_update','verify_native',"
            "'verify_tree')",
            name="mcp_workspace_deployment_phase_closed",
        ),
        CheckConstraint(
            "(file_count IS NULL AND total_bytes IS NULL) OR "
            "(file_count BETWEEN 1 AND 25000 AND "
            "total_bytes BETWEEN 1 AND 1073741824)",
            name="mcp_workspace_deployment_counts_bounded",
        ),
        CheckConstraint(
            "inventory_classification IN ('not_checked','matched','failed','unknown')",
            name="mcp_workspace_deployment_inventory_closed",
        ),
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 128",
            name="mcp_workspace_deployment_identity_bounded",
        ),
        CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128",
            name="mcp_workspace_deployment_failure_bounded",
        ),
        CheckConstraint(
            "(source_manifest_digest IS NULL OR "
            "(length(source_manifest_digest)=71 AND "
            "substr(source_manifest_digest,1,7)='sha256:')) AND "
            "(verified_destination_digest IS NULL OR "
            "(length(verified_destination_digest)=71 AND "
            "substr(verified_destination_digest,1,7)='sha256:'))",
            name="mcp_workspace_deployment_digests_valid",
        ),
        CheckConstraint(
            "length(requested_at)=27 AND "
            "(started_at IS NULL OR length(started_at)=27) AND "
            "(completed_at IS NULL OR length(completed_at)=27)",
            name="mcp_workspace_deployment_timestamps_format",
        ),
        CheckConstraint(
            "(state='pending' AND started_at IS NULL AND completed_at IS NULL) OR "
            "(state='running' AND started_at IS NOT NULL AND completed_at IS NULL) OR "
            "(state IN ('succeeded','failed','ambiguous','reconciliation_required') "
            "AND started_at IS NOT NULL AND completed_at IS NOT NULL)",
            name="mcp_workspace_deployment_state_timestamps",
        ),
        CheckConstraint(
            "(sync_started=0 OR mutation_started=1) AND "
            "(sync_completed=0 OR sync_started=1) AND "
            "(target_started=0 OR sync_completed=1)",
            name="mcp_workspace_deployment_markers_ordered",
        ),
        CheckConstraint(
            "state<>'succeeded' OR (inventory_classification='matched' "
            "AND addon_enabled IS NOT NULL AND result_classification IS NOT NULL "
            "AND source_manifest_digest IS NOT NULL "
            "AND verified_destination_digest IS NOT NULL AND failure_code IS NULL "
            "AND ((deployment_mode='direct' AND sync_completed=1 "
            "AND target_started=1 AND (target_class='internal_lab' OR "
            "(bridge_receipt_id IS NOT NULL AND file_count IS NOT NULL "
            "AND total_bytes IS NOT NULL))) OR "
            "(deployment_mode='repository' AND target_class='external_development' "
            "AND addon_enabled=1 AND file_count IS NOT NULL "
            "AND total_bytes IS NOT NULL AND source_manifest_digest="
            "verified_destination_digest)))",
            name="mcp_workspace_deployment_success_complete",
        ),
        UniqueConstraint(
            "client_id", "request_id", name="uq_mcp_workspace_deployment_request"
        ),
        Index(
            "ix_mcp_workspace_deployment_workspace_requested",
            "workspace_id",
            "requested_at",
        ),
        Index(
            "ix_mcp_workspace_deployment_target_requested",
            "target_id",
            "requested_at",
        ),
        Index(
            "uq_mcp_workspace_deployment_active_client",
            "client_id",
            unique=True,
            sqlite_where=text("state IN ('pending','running')"),
        ),
        Index(
            "uq_mcp_workspace_deployment_active_workspace",
            "workspace_id",
            unique=True,
            sqlite_where=text("state IN ('pending','running')"),
        ),
        Index(
            "uq_mcp_workspace_deployment_active_target",
            "target_id",
            unique=True,
            sqlite_where=text("state IN ('pending','running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    initiator_kind: Mapped[str] = mapped_column(
        String(8), nullable=False, default="mcp", server_default="mcp"
    )
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    development_source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    runtime_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    target_class: Mapped[str] = mapped_column(
        String(32), nullable=False, default="internal_lab"
    )
    deployment_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="direct", server_default="direct"
    )
    bridge_id: Mapped[str | None] = mapped_column(String(36))
    bridge_request_id: Mapped[str | None] = mapped_column(String(36))
    bridge_receipt_id: Mapped[str | None] = mapped_column(String(36))
    file_count: Mapped[int | None] = mapped_column(Integer)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    addon_version: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    mutation_started: Mapped[bool] = mapped_column(Boolean, nullable=False)
    target_stopped: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sync_started: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sync_completed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    target_started: Mapped[bool] = mapped_column(Boolean, nullable=False)
    inventory_classification: Mapped[str] = mapped_column(String(16), nullable=False)
    addon_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result_classification: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_manifest_digest: Mapped[str | None] = mapped_column(String(71))
    verified_destination_digest: Mapped[str | None] = mapped_column(String(71))
    failure_code: Mapped[str | None] = mapped_column(String(128))
    requested_at: Mapped[str] = mapped_column(String(27), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(27))
    completed_at: Mapped[str | None] = mapped_column(String(27))


class ExternalDevelopmentRepositoryReadinessRow(Base):
    """Operator-confirmed target reachability plus observed repository identity."""

    __tablename__ = "external_development_repository_readiness"
    __table_args__ = (
        CheckConstraint(
            "length(target_id)=36 AND length(bridge_id)=36",
            name="external_dev_repository_uuid_shape",
        ),
        CheckConstraint(
            "length(observed_manifest_digest)=71 AND "
            "substr(observed_manifest_digest,1,7)='sha256:'",
            name="external_dev_repository_manifest_shape",
        ),
        CheckConstraint(
            "manual_updates_confirmed=1",
            name="external_dev_repository_manual_updates",
        ),
    )

    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("external_kodi_targets.target_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    bootstrap_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    native_kodi_version: Mapped[str] = mapped_column(String(64), nullable=False)
    bridge_id: Mapped[str] = mapped_column(String(36), nullable=False)
    repository_version: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_manifest_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    manual_updates_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    confirmed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[str] = mapped_column(String(27), nullable=False)


class RepositoryWorkspaceValidationSnapshotRow(Base):
    """Immutable receipt snapshot because normal validation stores only latest."""

    __tablename__ = "repository_workspace_validation_snapshots"
    __table_args__ = (
        CheckConstraint(
            "generation>=0 AND length(source_tree_digest)=71 AND "
            "substr(source_tree_digest,1,7)='sha256:'",
            name="repository_workspace_validation_shape",
        ),
        CheckConstraint(
            "length(id)=36 AND length(workspace_id)=36 AND "
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 64 AND "
            "length(extension_types_json) BETWEEN 2 AND 4096 AND "
            "length(validated_at)=27",
            name="repository_workspace_validation_bounds",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    addon_version: Mapped[str] = mapped_column(String(64), nullable=False)
    extension_types_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_tree_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    validated_at: Mapped[str] = mapped_column(String(27), nullable=False)


class RepositoryWorkspaceDeploymentRow(Base):
    """One reviewed publication and later installed-tree observation."""

    __tablename__ = "repository_workspace_deployments"
    __table_args__ = (
        CheckConstraint(
            "workspace_generation>=0",
            name="repository_workspace_generation_nonnegative",
        ),
        CheckConstraint(
            "state IN ('awaiting_approval','publication_approved',"
            "'awaiting_kodi_update','observed_identity','verified_tree',"
            "'succeeded','failed')",
            name="repository_workspace_state_closed",
        ),
        CheckConstraint(
            "repository_id='repository.kodi-lab-dev'",
            name="repository_workspace_origin_closed",
        ),
        CheckConstraint(
            "length(expected_manifest_digest)=71 AND "
            "substr(expected_manifest_digest,1,7)='sha256:'",
            name="repository_workspace_manifest_shape",
        ),
        CheckConstraint(
            "file_count BETWEEN 1 AND 25000 AND total_bytes BETWEEN 1 AND 1073741824",
            name="repository_workspace_bounds",
        ),
        CheckConstraint(
            "(origin='planned' AND verified_by IS NULL) OR "
            "(origin='verified_existing' AND verified_by='local_admin' "
            "AND state='succeeded' AND approved_at IS NULL "
            "AND observed_at IS NOT NULL AND verified_at IS NOT NULL)",
            name="repository_workspace_verification_origin_closed",
        ),
        Index(
            "uq_repository_existing_target_publication",
            "target_id",
            "publication_id",
            unique=True,
            sqlite_where=text("origin='verified_existing'"),
        ),
        Index("ix_repository_workspace_target_state", "target_id", "state"),
    )

    operation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("mcp_workspace_deployment_operations.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    publication_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("development_publications.id", ondelete="RESTRICT"),
        nullable=False,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
        nullable=False,
    )
    workspace_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("external_kodi_targets.target_id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_kodi_version: Mapped[str] = mapped_column(String(64), nullable=False)
    bridge_id: Mapped[str] = mapped_column(String(36), nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    addon_version: Mapped[str] = mapped_column(String(64), nullable=False)
    zip_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    package_tree_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_manifest_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    validation_receipt: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("repository_workspace_validation_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    repository_id: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    bootstrap_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    installed_before_version: Mapped[str | None] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(
        String(24), nullable=False, default="planned", server_default="planned"
    )
    verified_by: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    approved_at: Mapped[str | None] = mapped_column(String(27))
    published_at: Mapped[str | None] = mapped_column(String(27))
    observed_at: Mapped[str | None] = mapped_column(String(27))
    verified_at: Mapped[str | None] = mapped_column(String(27))
    failure_code: Mapped[str | None] = mapped_column(String(128))


class McpWorkspaceExecutionOperationRow(Base):
    """At-most-once fixed workspace smoke execution evidence."""

    __tablename__ = "mcp_workspace_execution_operations"
    __table_args__ = (
        CheckConstraint(
            "length(id)=36 AND length(client_id)=36 AND length(request_id)=36 "
            "AND length(workspace_id)=36 AND length(development_source_id)=36 "
            "AND length(target_id)=36 AND length(deployment_id)=36",
            name="mcp_workspace_execution_uuid_lengths",
        ),
        CheckConstraint(
            "generation >= 0", name="mcp_workspace_execution_generation_nonnegative"
        ),
        CheckConstraint(
            "state IN ('pending','running','succeeded','failed',"
            "'ambiguous','reconciliation_required')",
            name="mcp_workspace_execution_state_closed",
        ),
        CheckConstraint(
            "phase IN ('claimed','preflight','verify_pre','invoke','verify_post',"
            "'finalize')",
            name="mcp_workspace_execution_phase_closed",
        ),
        CheckConstraint(
            "execution_classification IN ('not_attempted','directory_result',"
            "'jsonrpc_error','timeout','ambiguous')",
            name="mcp_workspace_execution_classification_closed",
        ),
        CheckConstraint(
            "item_count IS NULL OR item_count BETWEEN 0 AND 1",
            name="mcp_workspace_execution_item_count_bounded",
        ),
        CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 128",
            name="mcp_workspace_execution_identity_bounded",
        ),
        CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128",
            name="mcp_workspace_execution_failure_bounded",
        ),
        CheckConstraint(
            "length(requested_at)=27 AND "
            "(started_at IS NULL OR length(started_at)=27) AND "
            "(completed_at IS NULL OR length(completed_at)=27)",
            name="mcp_workspace_execution_timestamps_format",
        ),
        CheckConstraint(
            "(state='pending' AND started_at IS NULL AND completed_at IS NULL) OR "
            "(state='running' AND started_at IS NOT NULL AND completed_at IS NULL) OR "
            "(state IN ('succeeded','failed','ambiguous','reconciliation_required') "
            "AND started_at IS NOT NULL AND completed_at IS NOT NULL)",
            name="mcp_workspace_execution_state_timestamps",
        ),
        CheckConstraint(
            "state<>'succeeded' OR (invocation_dispatched=1 AND "
            "execution_classification='directory_result' AND "
            "result_classification IS NOT NULL AND failure_code IS NULL)",
            name="mcp_workspace_execution_success_complete",
        ),
        UniqueConstraint(
            "client_id", "request_id", name="uq_mcp_workspace_execution_request"
        ),
        Index(
            "ix_mcp_workspace_execution_workspace_requested",
            "workspace_id",
            "requested_at",
        ),
        Index(
            "uq_mcp_workspace_execution_active_target",
            "target_id",
            unique=True,
            sqlite_where=text("state IN ('pending','running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    development_source_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(36), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    addon_id: Mapped[str] = mapped_column(String(128), nullable=False)
    addon_version: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    invocation_dispatched: Mapped[bool] = mapped_column(Boolean, nullable=False)
    execution_classification: Mapped[str] = mapped_column(String(32), nullable=False)
    item_count: Mapped[int | None] = mapped_column(Integer)
    result_classification: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(128))
    requested_at: Mapped[str] = mapped_column(String(27), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(27))
    completed_at: Mapped[str | None] = mapped_column(String(27))


class McpTargetPresetAssignmentRow(Base):
    """Current target-side policy assignment on the common target identity."""

    __tablename__ = "mcp_target_preset_assignments"
    __table_args__ = (
        CheckConstraint(
            "length(assigned_at) = 27",
            name="mcp_target_preset_assignment_timestamp_format",
        ),
        Index("ix_mcp_target_assignment_preset", "preset_id"),
    )

    target_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("targets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    preset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mcp_presets.id", ondelete="RESTRICT"), nullable=False
    )
    assigned_at: Mapped[str] = mapped_column(String(27), nullable=False)


class McpSecurityEventRow(Base):
    """Content-free durable MCP security-administration event."""

    __tablename__ = "mcp_security_events"
    __table_args__ = (
        CheckConstraint(
            "length(event_kind) BETWEEN 1 AND 64 "
            "AND event_kind = lower(event_kind) "
            "AND event_kind = trim(event_kind)",
            name="mcp_security_event_kind_bounded",
        ),
        CheckConstraint(
            "length(occurred_at) = 27", name="mcp_security_event_timestamp_format"
        ),
        CheckConstraint(
            "actor IN ('web_admin','system')",
            name="mcp_security_event_actor_closed",
        ),
        Index("ix_mcp_security_event_occurred", "occurred_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(16), nullable=False)
    client_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    credential_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    development_source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    preset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    production_scope_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    identity_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[str] = mapped_column(String(27), nullable=False)


class McpRemoteActionReceiptRow(Base):
    """Durable client-scoped replay barrier for one semantic remote action."""

    __tablename__ = "mcp_remote_action_receipts"
    __table_args__ = (
        CheckConstraint(
            "length(client_id) = 36 AND length(credential_id) = 36 "
            "AND length(target_id) = 36 AND length(request_id) = 36",
            name="mcp_remote_receipt_uuid_lengths",
        ),
        CheckConstraint(
            "action IN ('up','down','left','right','select','back','home',"
            "'context_menu','info','play_pause','stop','previous','next',"
            "'volume_up','volume_down','mute_toggle')",
            name="mcp_remote_receipt_action_closed",
        ),
        CheckConstraint(
            "state IN ('pending','completed','ambiguous')",
            name="mcp_remote_receipt_state_closed",
        ),
        CheckConstraint(
            "length(created_at) = 27 AND "
            "(completed_at IS NULL OR length(completed_at) = 27)",
            name="mcp_remote_receipt_timestamp_format",
        ),
        CheckConstraint(
            "(state = 'pending' AND final_result IS NULL AND completed_at IS NULL "
            "AND screenshot_refresh_result IS NULL) OR "
            "(state IN ('completed','ambiguous') AND final_result IS NOT NULL "
            "AND completed_at IS NOT NULL AND screenshot_refresh_result IS NOT NULL)",
            name="mcp_remote_receipt_terminal_consistent",
        ),
        CheckConstraint(
            "final_result IS NULL OR (length(final_result) BETWEEN 1 AND 64 "
            "AND final_result = lower(final_result) "
            "AND final_result = trim(final_result))",
            name="mcp_remote_receipt_result_bounded",
        ),
        CheckConstraint(
            "screenshot_refresh_result IS NULL OR "
            "(length(screenshot_refresh_result) BETWEEN 1 AND 64 "
            "AND screenshot_refresh_result = lower(screenshot_refresh_result) "
            "AND screenshot_refresh_result = trim(screenshot_refresh_result))",
            name="mcp_remote_receipt_refresh_bounded",
        ),
        CheckConstraint(
            "(screenshot_capture_id IS NULL AND screenshot_captured_at IS NULL) OR "
            "(length(screenshot_capture_id) = 36 "
            "AND length(screenshot_captured_at) = 27)",
            name="mcp_remote_receipt_refresh_metadata_paired",
        ),
        UniqueConstraint(
            "client_id", "request_id", name="uq_mcp_remote_receipt_client_request"
        ),
        Index("ix_mcp_remote_receipt_client_created", "client_id", "created_at"),
        Index("ix_mcp_remote_receipt_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(36), nullable=False)
    credential_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    final_result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String(27), nullable=True)
    screenshot_refresh_result: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    screenshot_capture_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    screenshot_captured_at: Mapped[str | None] = mapped_column(
        String(27), nullable=True
    )


class McpProductionRemoteReceiptBindingRow(Base):
    """Immutable production authority snapshot for one base remote receipt."""

    __tablename__ = "mcp_production_remote_receipt_bindings"
    __table_args__ = (
        CheckConstraint(
            "length(receipt_id)=36 AND "
            "operation_class IN ('navigation','transient_control')",
            name="mcp_production_remote_binding_identity_valid",
        ),
        CheckConstraint(
            "native_identity_generation>=1 AND bridge_trust_revision>=1 "
            "AND target_policy_revision>=1 AND mutation_control_epoch>=1",
            name="mcp_production_remote_binding_revisions_positive",
        ),
        CheckConstraint(
            "length(inspect_scope_id)=36 "
            "AND length(client_preset_id)=36 AND length(target_preset_id)=36 "
            "AND length(generic_grant_id)=36 "
            "AND ((operation_class='navigation' AND length(navigate_scope_id)=36 "
            "AND transient_scope_id IS NULL AND approval_id IS NULL) OR "
            "(operation_class='transient_control' AND navigate_scope_id IS NULL "
            "AND length(transient_scope_id)=36 AND length(approval_id)=36))",
            name="mcp_production_remote_binding_authority_shapes",
        ),
        CheckConstraint(
            "length(generic_grant_created_at)=27 AND "
            "(native_http_acknowledgement_id IS NULL OR "
            "length(native_http_acknowledgement_id)=36)",
            name="mcp_production_remote_binding_optional_shapes",
        ),
        Index(
            "ix_mcp_production_remote_binding_scopes",
            "inspect_scope_id",
            "navigate_scope_id",
            "transient_scope_id",
        ),
        Index(
            "uq_mcp_production_remote_binding_approval",
            "approval_id",
            unique=True,
            sqlite_where=text("approval_id IS NOT NULL"),
        ),
    )

    receipt_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("mcp_remote_action_receipts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    operation_class: Mapped[str] = mapped_column(String(16), nullable=False)
    native_identity_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    bridge_trust_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    target_policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    mutation_control_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    inspect_scope_id: Mapped[str] = mapped_column(String(36), nullable=False)
    navigate_scope_id: Mapped[str | None] = mapped_column(String(36))
    transient_scope_id: Mapped[str | None] = mapped_column(String(36))
    approval_id: Mapped[str | None] = mapped_column(String(36))
    client_preset_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_preset_id: Mapped[str] = mapped_column(String(36), nullable=False)
    generic_grant_created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    generic_grant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    native_http_acknowledgement_id: Mapped[str | None] = mapped_column(String(36))


class ProductionOperationApprovalRow(Base):
    """One durable, exact, one-shot production transient-control approval."""

    __tablename__ = "production_operation_approvals"
    __table_args__ = (
        CheckConstraint(
            "length(id)=36 AND length(client_id)=36 AND length(credential_id)=36 "
            "AND length(target_id)=36 AND length(request_id)=36",
            name="production_approval_primary_uuid_shapes",
        ),
        CheckConstraint(
            "tool='remote_action' AND action IN "
            "('play_pause','stop','previous','next','volume_up','volume_down',"
            "'mute_toggle')",
            name="production_approval_semantics_closed",
        ),
        CheckConstraint(
            "length(payload_sha256)=64 AND "
            "payload_sha256 NOT GLOB '*[^0-9a-f]*' AND canonicalization_revision>=1",
            name="production_approval_payload_digest_valid",
        ),
        CheckConstraint(
            "native_identity_generation>=1 AND bridge_trust_revision>=1 "
            "AND target_policy_revision>=1 AND mutation_control_epoch>=1 "
            "AND client_authority_revision>=1",
            name="production_approval_revisions_positive",
        ),
        CheckConstraint(
            "length(generic_grant_id)=36 AND length(client_preset_id)=36 "
            "AND length(target_preset_id)=36 AND length(inspect_scope_id)=36 "
            "AND length(transient_scope_id)=36 AND length(reviewed_build_id)=36",
            name="production_approval_authority_uuid_shapes",
        ),
        CheckConstraint(
            "native_http_acknowledgement_id IS NULL OR "
            "length(native_http_acknowledgement_id)=36",
            name="production_approval_ack_shape",
        ),
        CheckConstraint(
            "state IN ('pending','approved','denied','cancelled','expired','consumed')",
            name="production_approval_state_closed",
        ),
        CheckConstraint(
            "terminal_reason IS NULL OR terminal_reason IN "
            "('administrator_denied','administrator_cancelled','authority_changed',"
            "'mutation_policy_changed','capability_lost','expired')",
            name="production_approval_terminal_reason_closed",
        ),
        CheckConstraint(
            "length(created_at)=27 AND length(pending_expires_at)=27 "
            "AND pending_expires_at>created_at "
            "AND (approved_at IS NULL OR length(approved_at)=27) "
            "AND (approved_dispatch_expires_at IS NULL OR "
            "length(approved_dispatch_expires_at)=27) "
            "AND (consumed_at IS NULL OR length(consumed_at)=27)",
            name="production_approval_timestamps_valid",
        ),
        CheckConstraint(
            "julianday(pending_expires_at)-julianday(created_at)<=900.001/86400.0 "
            "AND (approved_dispatch_expires_at IS NULL OR "
            "julianday(approved_dispatch_expires_at)-julianday(approved_at)"
            "<=300.001/86400.0)",
            name="production_approval_ttl_bounded",
        ),
        CheckConstraint(
            "(state='pending' AND terminal_reason IS NULL AND approved_at IS NULL "
            "AND approved_dispatch_expires_at IS NULL "
            "AND approving_administrator IS NULL "
            "AND consumed_at IS NULL AND linked_remote_receipt_id IS NULL) OR "
            "(state='approved' AND terminal_reason IS NULL AND approved_at IS NOT NULL "
            "AND approved_dispatch_expires_at IS NOT NULL "
            "AND approving_administrator IS NOT NULL AND consumed_at IS NULL "
            "AND linked_remote_receipt_id IS NULL) OR "
            "(state IN ('denied','cancelled','expired') "
            "AND terminal_reason IS NOT NULL "
            "AND consumed_at IS NULL AND linked_remote_receipt_id IS NULL) OR "
            "(state='consumed' AND terminal_reason IS NULL AND approved_at IS NOT NULL "
            "AND approved_dispatch_expires_at IS NOT NULL "
            "AND approving_administrator IS NOT NULL AND consumed_at IS NOT NULL "
            "AND length(linked_remote_receipt_id)=36)",
            name="production_approval_state_shape",
        ),
        CheckConstraint(
            "approving_administrator IS NULL OR "
            "length(approving_administrator) BETWEEN 1 AND 64",
            name="production_approval_administrator_bounded",
        ),
        UniqueConstraint(
            "client_id", "request_id", name="uq_production_approval_client_request"
        ),
        Index(
            "uq_production_approval_linked_receipt",
            "linked_remote_receipt_id",
            unique=True,
            sqlite_where=text("linked_remote_receipt_id IS NOT NULL"),
        ),
        Index("ix_production_approval_queue", "state", "pending_expires_at"),
        Index("ix_production_approval_client_history", "client_id", "created_at", "id"),
        Index("ix_production_approval_target_state", "target_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(36), nullable=False)
    credential_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tool: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    canonicalization_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    native_identity_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    bridge_trust_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    target_policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    mutation_control_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    client_authority_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    generic_grant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    client_preset_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_preset_id: Mapped[str] = mapped_column(String(36), nullable=False)
    inspect_scope_id: Mapped[str] = mapped_column(String(36), nullable=False)
    transient_scope_id: Mapped[str] = mapped_column(String(36), nullable=False)
    native_http_acknowledgement_id: Mapped[str | None] = mapped_column(String(36))
    reviewed_build_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    terminal_reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(27), nullable=False)
    pending_expires_at: Mapped[str] = mapped_column(String(27), nullable=False)
    approved_at: Mapped[str | None] = mapped_column(String(27))
    approved_dispatch_expires_at: Mapped[str | None] = mapped_column(String(27))
    approving_administrator: Mapped[str | None] = mapped_column(String(64))
    consumed_at: Mapped[str | None] = mapped_column(String(27))
    linked_remote_receipt_id: Mapped[str | None] = mapped_column(String(36))


class ProductionApprovalTransitionEventRow(Base):
    """Append-only bounded semantic approval transition audit."""

    __tablename__ = "production_approval_transition_events"
    __table_args__ = (
        CheckConstraint(
            "length(id)=36 AND length(approval_id)=36",
            name="production_approval_event_uuid_shapes",
        ),
        CheckConstraint(
            "from_state IS NULL OR from_state IN ('pending','approved')",
            name="production_approval_event_from_state_closed",
        ),
        CheckConstraint(
            "to_state IN "
            "('pending','approved','denied','cancelled','expired','consumed')",
            name="production_approval_event_to_state_closed",
        ),
        CheckConstraint(
            "event_kind IN ('requested','approved','denied','cancelled','expired',"
            "'consumed')",
            name="production_approval_event_kind_closed",
        ),
        CheckConstraint(
            "actor_kind IN ('mcp_client','web_admin','system') AND "
            "length(actor_reference) BETWEEN 1 AND 64 AND length(occurred_at)=27",
            name="production_approval_event_actor_bounded",
        ),
        CheckConstraint(
            "terminal_reason IS NULL OR terminal_reason IN "
            "('administrator_denied','administrator_cancelled','authority_changed',"
            "'mutation_policy_changed','capability_lost','expired')",
            name="production_approval_event_reason_closed",
        ),
        Index(
            "ix_production_approval_event_approval_time",
            "approval_id",
            "occurred_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    approval_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(16))
    to_state: Mapped[str] = mapped_column(String(16), nullable=False)
    terminal_reason: Mapped[str | None] = mapped_column(String(32))
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_reference: Mapped[str] = mapped_column(String(64), nullable=False)
    linked_remote_receipt_id: Mapped[str | None] = mapped_column(String(36))
    occurred_at: Mapped[str] = mapped_column(String(27), nullable=False)


class ProductionTransientDispatchAuditRow(Base):
    """Bounded semantic audit for one consumed transient approval."""

    __tablename__ = "production_transient_dispatch_audits"
    __table_args__ = (
        CheckConstraint(
            "length(receipt_id)=36 AND length(approval_id)=36 "
            "AND length(client_id)=36 AND length(credential_id)=36 "
            "AND length(target_id)=36 AND length(request_id)=36",
            name="production_transient_audit_uuid_shapes",
        ),
        CheckConstraint(
            "action IN ('play_pause','stop','previous','next','volume_up',"
            "'volume_down','mute_toggle')",
            name="production_transient_audit_action_closed",
        ),
        CheckConstraint(
            "length(payload_sha256)=64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*' "
            "AND canonicalization_revision>=1",
            name="production_transient_audit_digest_valid",
        ),
        CheckConstraint(
            "state IN ('pending','completed','ambiguous') AND "
            "dispatch_classification IN ('not_dispatched','acknowledged','ambiguous')",
            name="production_transient_audit_state_closed",
        ),
        CheckConstraint(
            "length(outcome) BETWEEN 1 AND 64 AND outcome=lower(outcome) "
            "AND outcome=trim(outcome)",
            name="production_transient_audit_outcome_bounded",
        ),
        CheckConstraint(
            "length(requested_at)=27 AND "
            "(completed_at IS NULL OR length(completed_at)=27)",
            name="production_transient_audit_timestamps_valid",
        ),
        Index("ix_production_transient_audit_target_time", "target_id", "requested_at"),
        UniqueConstraint("approval_id", name="uq_production_transient_audit_approval"),
    )

    receipt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    approval_id: Mapped[str] = mapped_column(String(36), nullable=False)
    client_id: Mapped[str] = mapped_column(String(36), nullable=False)
    credential_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    canonicalization_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    dispatch_classification: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_at: Mapped[str] = mapped_column(String(27), nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String(27))


class ProductionApprovalCreationRateRow(Base):
    __tablename__ = "production_approval_creation_rates"

    client_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    last_created_at: Mapped[str] = mapped_column(String(27), nullable=False)


class ProductionTransientDispatchRateRow(Base):
    __tablename__ = "production_transient_dispatch_rates"

    client_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    last_dispatched_at: Mapped[str] = mapped_column(String(27), nullable=False)


class McpProductionNavigationAuditRow(Base):
    """Bounded semantic audit for one production navigation receipt."""

    __tablename__ = "mcp_production_navigation_audits"
    __table_args__ = (
        CheckConstraint(
            "length(receipt_id)=36 AND length(client_id)=36 "
            "AND length(credential_id)=36 AND length(target_id)=36 "
            "AND length(request_id)=36",
            name="mcp_production_navigation_audit_uuid_shapes",
        ),
        CheckConstraint(
            "operation_class='navigation' AND action IN "
            "('up','down','left','right','back','home','info')",
            name="mcp_production_navigation_audit_semantics_closed",
        ),
        CheckConstraint(
            "native_identity_generation>=1 AND bridge_trust_revision>=1 "
            "AND target_policy_revision>=1 AND mutation_control_epoch>=1 "
            "AND length(inspect_scope_id)=36 AND length(navigate_scope_id)=36",
            name="mcp_production_navigation_audit_authority_valid",
        ),
        CheckConstraint(
            "state IN ('pending','completed','ambiguous') AND "
            "dispatch_classification IN "
            "('not_dispatched','acknowledged','ambiguous')",
            name="mcp_production_navigation_audit_outcome_closed",
        ),
        CheckConstraint(
            "length(outcome) BETWEEN 1 AND 64 AND outcome=lower(outcome) "
            "AND outcome=trim(outcome)",
            name="mcp_production_navigation_audit_outcome_bounded",
        ),
        CheckConstraint(
            "length(requested_at)=27 AND "
            "(completed_at IS NULL OR length(completed_at)=27) AND "
            "((state='pending' AND completed_at IS NULL) OR "
            "(state IN ('completed','ambiguous') AND completed_at IS NOT NULL))",
            name="mcp_production_navigation_audit_timestamps_valid",
        ),
        Index(
            "ix_mcp_production_navigation_audit_target_time",
            "target_id",
            "requested_at",
        ),
    )

    receipt_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("mcp_remote_action_receipts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    client_id: Mapped[str] = mapped_column(String(36), nullable=False)
    credential_id: Mapped[str] = mapped_column(String(36), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_class: Mapped[str] = mapped_column(String(16), nullable=False)
    native_identity_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    bridge_trust_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    target_policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    mutation_control_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    inspect_scope_id: Mapped[str] = mapped_column(String(36), nullable=False)
    navigate_scope_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    dispatch_classification: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_at: Mapped[str] = mapped_column(String(27), nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String(27))


_EXTERNAL_PRODUCTION_MATERIAL_UPDATE = (
    "NEW.administrative_state<>OLD.administrative_state OR "
    "NEW.protocol_kind<>OLD.protocol_kind OR NEW.host<>OLD.host OR "
    "NEW.port<>OLD.port OR NEW.authentication_mode<>OLD.authentication_mode OR "
    "NEW.secret_reference IS NOT OLD.secret_reference OR "
    "NEW.configured_capabilities_json<>OLD.configured_capabilities_json OR "
    "NEW.verified_capabilities_json<>OLD.verified_capabilities_json OR "
    "NEW.verification_state<>OLD.verification_state OR "
    "NEW.verified_addresses_json<>OLD.verified_addresses_json OR "
    "NEW.verified_kodi_name IS NOT OLD.verified_kodi_name OR "
    "NEW.verified_kodi_version IS NOT OLD.verified_kodi_version OR "
    "NEW.observed_kodi_name IS NOT OLD.observed_kodi_name OR "
    "NEW.observed_kodi_version IS NOT OLD.observed_kodi_version OR "
    "NEW.observed_protocol_version IS NOT OLD.observed_protocol_version OR "
    "NEW.pending_observation_id IS NOT OLD.pending_observation_id"
)


_SLICE4B_CREATE_ALL_TRIGGERS = (
    "CREATE TRIGGER trg_mcp_client_target_grant_identity_once "
    "AFTER INSERT ON mcp_client_target_grants BEGIN "
    "INSERT INTO mcp_client_target_grant_identities "
    "(grant_id,client_id,target_id,created_at) VALUES "
    "(NEW.grant_id,NEW.client_id,NEW.target_id,NEW.created_at); END",
    "CREATE TRIGGER trg_mcp_client_target_grant_identity_history_immutable "
    "BEFORE UPDATE ON mcp_client_target_grant_identities BEGIN "
    "SELECT RAISE(ABORT, 'mcp_client_target_grant_identity_reused'); END",
    "CREATE TRIGGER trg_mcp_client_target_grant_identity_history_no_delete "
    "BEFORE DELETE ON mcp_client_target_grant_identities BEGIN "
    "SELECT RAISE(ABORT, 'mcp_client_target_grant_identity_reused'); END",
    "CREATE TRIGGER trg_production_mutation_epoch_no_delete BEFORE DELETE ON "
    "production_mutation_control BEGIN SELECT RAISE(ABORT, "
    "'production_mutation_epoch_invalid'); END",
    "CREATE TRIGGER trg_mcp_credential_approval_no_delete BEFORE DELETE ON "
    "mcp_credentials WHEN EXISTS(SELECT 1 FROM production_operation_approvals "
    "WHERE credential_id=OLD.id) BEGIN SELECT RAISE(ABORT, "
    "'production_approval_authority_protected'); END",
    "CREATE TRIGGER trg_mcp_client_approval_no_delete BEFORE DELETE ON mcp_clients "
    "WHEN EXISTS(SELECT 1 FROM production_operation_approvals WHERE client_id=OLD.id) "
    "BEGIN SELECT RAISE(ABORT, 'production_approval_authority_protected'); END",
    "CREATE TRIGGER trg_production_policy_approval_no_delete BEFORE DELETE ON "
    "external_production_policies WHEN EXISTS(SELECT 1 FROM "
    "production_operation_approvals WHERE target_id=OLD.target_id) BEGIN "
    "SELECT RAISE(ABORT, 'production_approval_authority_protected'); END",
    "CREATE TRIGGER trg_external_production_policy_reinsert_guard BEFORE INSERT ON "
    "external_production_policies WHEN EXISTS(SELECT 1 FROM "
    "external_production_policy_revision_guards WHERE target_id=NEW.target_id) "
    "BEGIN SELECT RAISE(ABORT, "
    "'production_policy_revision_not_monotonic'); END",
    "CREATE TRIGGER trg_external_production_policy_revision_monotonic BEFORE UPDATE "
    "OF policy_revision ON external_production_policies WHEN "
    "NEW.policy_revision<OLD.policy_revision BEGIN SELECT RAISE(ABORT, "
    "'production_policy_revision_not_monotonic'); END",
    "CREATE TRIGGER trg_external_production_policy_material_revision BEFORE UPDATE "
    "ON external_production_policies WHEN (NEW.designation_state<>"
    "OLD.designation_state OR NEW.confirmed_observation_id IS NOT "
    "OLD.confirmed_observation_id OR NEW.confirmed_observation_digest IS NOT "
    "OLD.confirmed_observation_digest OR NEW.identity_generation<>"
    "OLD.identity_generation OR NEW.mutations_suspended<>OLD.mutations_suspended OR "
    "NEW.updated_at<>OLD.updated_at) AND NEW.policy_revision<>OLD.policy_revision+1 "
    "BEGIN SELECT RAISE(ABORT, 'production_policy_revision_required'); END",
    "CREATE TRIGGER trg_external_production_policy_guard_insert AFTER INSERT ON "
    "external_production_policies BEGIN INSERT INTO "
    "external_production_policy_revision_guards "
    "(target_id,maximum_policy_revision) VALUES "
    "(NEW.target_id,NEW.policy_revision); END",
    "CREATE TRIGGER trg_external_production_policy_guard_advance AFTER UPDATE OF "
    "policy_revision ON external_production_policies WHEN "
    "NEW.policy_revision>OLD.policy_revision BEGIN UPDATE "
    "external_production_policy_revision_guards SET maximum_policy_revision="
    "NEW.policy_revision WHERE target_id=NEW.target_id; END",
    "CREATE TRIGGER trg_external_production_policy_guard_reinsert BEFORE INSERT ON "
    "external_production_policy_revision_guards WHEN EXISTS(SELECT 1 FROM "
    "external_production_policy_revision_guards WHERE target_id=NEW.target_id) "
    "BEGIN SELECT RAISE(ABORT, "
    "'production_policy_revision_not_monotonic'); END",
    "CREATE TRIGGER trg_external_production_policy_guard_immutable BEFORE UPDATE ON "
    "external_production_policy_revision_guards WHEN NEW.target_id<>OLD.target_id OR "
    "NEW.maximum_policy_revision<=OLD.maximum_policy_revision BEGIN SELECT "
    "RAISE(ABORT, 'production_policy_revision_not_monotonic'); END",
    "CREATE TRIGGER trg_external_production_policy_guard_no_delete BEFORE DELETE ON "
    "external_production_policy_revision_guards BEGIN SELECT RAISE(ABORT, "
    "'production_policy_revision_not_monotonic'); END",
    "CREATE TRIGGER trg_external_production_revision_binding_insert BEFORE INSERT ON "
    "external_kodi_targets WHEN (COALESCE((SELECT target_type FROM targets WHERE "
    "id=NEW.target_id),'')='external_production') <> "
    "(NEW.production_policy_revision IS NOT NULL) BEGIN SELECT RAISE(ABORT, "
    "'production_policy_revision_required'); END",
    "CREATE TRIGGER trg_external_production_revision_binding_reinsert BEFORE INSERT "
    "ON external_kodi_targets WHEN EXISTS(SELECT 1 FROM "
    "external_production_policy_revision_guards WHERE target_id=NEW.target_id) "
    "BEGIN SELECT RAISE(ABORT, "
    "'production_policy_revision_not_monotonic'); END",
    f"CREATE TRIGGER trg_external_production_revision_binding_material BEFORE UPDATE "
    f"ON external_kodi_targets WHEN COALESCE((SELECT target_type FROM targets WHERE "
    f"id=NEW.target_id),'')='external_production' AND "
    f"({_EXTERNAL_PRODUCTION_MATERIAL_UPDATE}) "
    f"AND (NEW.production_policy_revision IS NULL OR "
    f"NEW.production_policy_revision<=COALESCE(OLD.production_policy_revision,0) OR "
    f"NEW.production_policy_revision<>COALESCE((SELECT policy_revision FROM "
    f"external_production_policies WHERE target_id=NEW.target_id),0)) BEGIN SELECT "
    f"RAISE(ABORT, 'production_policy_revision_required'); END",
    f"CREATE TRIGGER trg_external_production_revision_binding_immutable BEFORE UPDATE "
    f"ON external_kodi_targets WHEN (NEW.production_policy_revision IS NOT "
    f"OLD.production_policy_revision AND NOT ({_EXTERNAL_PRODUCTION_MATERIAL_UPDATE})) "
    f"OR (COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>"
    f"'external_production' AND NEW.production_policy_revision IS NOT NULL) BEGIN "
    f"SELECT RAISE(ABORT, 'production_policy_revision_required'); END",
    "CREATE TRIGGER trg_production_target_preset_revision_insert AFTER INSERT ON "
    "mcp_target_preset_assignments WHEN COALESCE((SELECT target_type FROM targets "
    "WHERE id=NEW.target_id),'')='external_production' BEGIN UPDATE "
    "external_production_policies SET policy_revision=policy_revision+1 WHERE "
    "target_id=NEW.target_id; END",
    "CREATE TRIGGER trg_production_target_preset_revision_update AFTER UPDATE OF "
    "preset_id ON mcp_target_preset_assignments WHEN NEW.preset_id<>OLD.preset_id AND "
    "COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')="
    "'external_production' BEGIN UPDATE external_production_policies SET "
    "policy_revision=policy_revision+1 WHERE target_id=NEW.target_id; END",
    "CREATE TRIGGER trg_production_target_preset_revision_delete AFTER DELETE ON "
    "mcp_target_preset_assignments WHEN COALESCE((SELECT target_type FROM targets "
    "WHERE id=OLD.target_id),'')='external_production' AND NOT "
    "(COALESCE((SELECT administrative_state FROM external_kodi_targets WHERE "
    "target_id=OLD.target_id),'')='deactivated' AND COALESCE((SELECT "
    "designation_state FROM external_production_policies WHERE "
    "target_id=OLD.target_id),'')='deactivated') BEGIN UPDATE "
    "external_production_policies SET policy_revision=policy_revision+1 WHERE "
    "target_id=OLD.target_id; END",
)

for _trigger_sql in _SLICE4B_CREATE_ALL_TRIGGERS:
    event.listen(
        Base.metadata,
        "after_create",
        DDL(_trigger_sql).execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
    )


_CAPABILITY_AMENDMENT_CREATE_TRIGGERS = (
    "CREATE TRIGGER trg_production_capability_amendment_insert BEFORE INSERT ON "
    "production_capability_amendments WHEN "
    "COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>"
    "'external_production' OR NOT json_valid(NEW.before_configured_json) OR NOT "
    "json_valid(NEW.after_configured_json) OR json_type(NEW.before_configured_json)"
    "<>'array' OR json_type(NEW.after_configured_json)<>'array' OR EXISTS(SELECT 1 "
    "FROM json_each(NEW.before_configured_json) WHERE type<>'text' OR value NOT IN "
    "('target_inspect','addon_inventory','semantic_remote','screenshot')) OR "
    "EXISTS(SELECT 1 FROM json_each(NEW.after_configured_json) WHERE type<>'text' "
    "OR value NOT IN ('target_inspect','addon_inventory','semantic_remote',"
    "'screenshot')) OR (SELECT count(*) FROM json_each(NEW.before_configured_json))"
    "<>(SELECT count(DISTINCT value) FROM json_each(NEW.before_configured_json)) OR "
    "(SELECT count(*) FROM json_each(NEW.after_configured_json))<>(SELECT "
    "count(DISTINCT value) FROM json_each(NEW.after_configured_json)) OR "
    "COALESCE((SELECT configured_capabilities_json FROM external_kodi_targets WHERE "
    "target_id=NEW.target_id),'')<>NEW.before_configured_json OR COALESCE((SELECT "
    "policy_revision FROM external_production_policies WHERE target_id=NEW.target_id),"
    "0)<>NEW.resulting_policy_revision OR NOT (((NEW.operation='add') AND NOT "
    "EXISTS(SELECT 1 FROM json_each(NEW.before_configured_json) WHERE "
    "value='screenshot') AND EXISTS(SELECT 1 FROM "
    "json_each(NEW.after_configured_json) WHERE value='screenshot') AND "
    "json_array_length(NEW.after_configured_json)=json_array_length("
    "NEW.before_configured_json)+1 AND NOT EXISTS(SELECT 1 FROM "
    "json_each(NEW.before_configured_json) b WHERE NOT EXISTS(SELECT 1 FROM "
    "json_each(NEW.after_configured_json) a WHERE a.value=b.value)) AND NOT "
    "EXISTS(SELECT 1 FROM json_each(NEW.after_configured_json) a WHERE "
    "a.value<>'screenshot' AND NOT EXISTS(SELECT 1 FROM "
    "json_each(NEW.before_configured_json) b WHERE b.value=a.value))) OR "
    "((NEW.operation='remove') AND EXISTS(SELECT 1 FROM "
    "json_each(NEW.before_configured_json) WHERE value='screenshot') AND NOT "
    "EXISTS(SELECT 1 FROM json_each(NEW.after_configured_json) WHERE "
    "value='screenshot') AND json_array_length(NEW.after_configured_json)+1="
    "json_array_length(NEW.before_configured_json) AND NOT EXISTS(SELECT 1 FROM "
    "json_each(NEW.after_configured_json) a WHERE NOT EXISTS(SELECT 1 FROM "
    "json_each(NEW.before_configured_json) b WHERE b.value=a.value)) AND NOT "
    "EXISTS(SELECT 1 FROM json_each(NEW.before_configured_json) b WHERE "
    "b.value<>'screenshot' AND NOT EXISTS(SELECT 1 FROM "
    "json_each(NEW.after_configured_json) a WHERE a.value=b.value)))) BEGIN SELECT "
    "RAISE(ABORT, 'production_capability_amendment_invalid'); END",
    "CREATE TRIGGER trg_production_capability_amendment_immutable BEFORE UPDATE ON "
    "production_capability_amendments BEGIN SELECT RAISE(ABORT, "
    "'production_capability_amendment_immutable'); END",
    "CREATE TRIGGER trg_production_capability_amendment_no_delete BEFORE DELETE ON "
    "production_capability_amendments BEGIN SELECT RAISE(ABORT, "
    "'production_capability_amendment_immutable'); END",
    "CREATE TRIGGER trg_production_configured_capability_amendment_required "
    "BEFORE UPDATE OF configured_capabilities_json ON external_kodi_targets WHEN "
    "NEW.configured_capabilities_json<>OLD.configured_capabilities_json AND "
    "COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')="
    "'external_production' AND NOT EXISTS(SELECT 1 FROM "
    "production_capability_amendments a WHERE a.target_id=NEW.target_id AND "
    "a.before_configured_json=OLD.configured_capabilities_json AND "
    "a.after_configured_json=NEW.configured_capabilities_json AND "
    "a.resulting_policy_revision=NEW.production_policy_revision) BEGIN SELECT "
    "RAISE(ABORT, 'production_capability_amendment_required'); END",
    "CREATE TRIGGER trg_production_screenshot_removal_safe BEFORE UPDATE OF "
    "configured_capabilities_json ON external_kodi_targets WHEN "
    + PRODUCTION_SCREENSHOT_TARGET_GUARD
    + "EXISTS(SELECT 1 FROM json_each(OLD.configured_capabilities_json) WHERE "
    "value='screenshot') AND NOT EXISTS(SELECT 1 FROM "
    "json_each(NEW.configured_capabilities_json) WHERE value='screenshot') AND "
    "(NOT EXISTS(SELECT 1 FROM production_capability_amendments a WHERE "
    "a.target_id=NEW.target_id AND a.operation='remove' AND "
    "a.before_configured_json=OLD.configured_capabilities_json AND "
    "a.after_configured_json=NEW.configured_capabilities_json AND "
    "a.resulting_policy_revision=NEW.production_policy_revision) OR EXISTS(SELECT 1 "
    "FROM external_bridge_pairings p WHERE p.target_id=NEW.target_id AND "
    "(p.pairing_state<>'revoked' OR p.pairing_token_reference IS NOT NULL OR "
    "p.bridge_secret_reference IS NOT NULL OR p.verified_capabilities_json<>'[]' OR "
    "p.production_bridge_trust_revision IS NULL OR "
    "p.production_identity_generation IS NOT NULL OR p.reviewed_build_id IS NOT "
    "NULL OR p.production_confirmed_by IS NOT NULL OR p.production_confirmed_at "
    "IS NOT NULL)) OR EXISTS(SELECT 1 FROM mcp_production_target_scopes s WHERE "
    "s.target_id=NEW.target_id AND s.scope IN "
    "('visual','navigate','request_transient_control') AND s.state='active') OR "
    "EXISTS(SELECT 1 FROM production_operation_approvals a WHERE "
    "a.target_id=NEW.target_id AND a.state IN ('pending','approved'))) BEGIN SELECT "
    "RAISE(ABORT, 'production_screenshot_removal_unsafe'); END",
)

for _trigger_sql in _CAPABILITY_AMENDMENT_CREATE_TRIGGERS:
    event.listen(
        Base.metadata,
        "after_create",
        DDL(_trigger_sql).execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
    )

for _trigger_sql in DEVELOPMENT_CAPABILITY_TRIGGERS:
    event.listen(
        Base.metadata,
        "after_create",
        DDL(_trigger_sql).execute_if(dialect="sqlite"),  # type: ignore[no-untyped-call]
    )
