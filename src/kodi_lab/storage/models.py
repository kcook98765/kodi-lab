"""SQLAlchemy mappings for core targets, instances, and runtime catalog."""

from enum import StrEnum

from sqlalchemy import (
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
    FixtureContentCategory,
    FixtureSizeCategory,
    HealthStatus,
    InstanceRuntimeState,
    InstanceType,
    ProcessObservationKind,
    ReadinessProbeKind,
    ResetOperationPhase,
    ResetOperationState,
    RevisionApprovalState,
    RuntimeKind,
    RuntimeSupportTier,
    RuntimeValidationResult,
    TargetAvailability,
    TargetType,
)
from kodi_lab.storage.metadata import Base


def _enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [item.value for item in enum_type]


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
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    fixture_definition_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("fixture_definitions.id", ondelete="RESTRICT"),
        nullable=True,
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
        ForeignKey("managed_instances.target_id", ondelete="RESTRICT"),
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
