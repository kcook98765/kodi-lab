"""Add disposable snapshot creation and narrow mutation coordination.

Revision ID: 0012_disposable_snapshot_creation
Revises: 0011_fixture_medium_taxonomy
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_disposable_snapshot_creation"
down_revision: str | None = "0011_fixture_medium_taxonomy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SNAPSHOT_STATES = "'creating','available','failed','quarantined'"
_VALIDATION_RESULTS = "'pending','passed','failed'"
_OP_STATES = "'queued','running','succeeded','failed','reconciliation_required'"
_OP_PHASES = (
    "'queued','acquiring_leases','resolving_instance','stopping','capturing',"
    "'validating_archive','validating_databases','publishing',"
    "'restoring_running_state','verifying_readiness','succeeded','failed',"
    "'reconciliation_required'"
)
_MUTATION_KINDS = (
    "'reset','snapshot_create','snapshot_restore','lifecycle',"
    "'fixture_assignment','runtime_reassignment'"
)
_HEAVY_KINDS = "'snapshot_create','snapshot_restore'"


def upgrade() -> None:
    op.create_table(
        "snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_managed_instance_id", sa.String(36), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("captured_fixture_definition_id", sa.String(36), nullable=True),
        sa.Column("captured_at", sa.String(32), nullable=False),
        sa.Column("prior_runtime_state", sa.String(7), nullable=True),
        sa.Column("label", sa.String(480), nullable=True),
        sa.Column("manifest_version", sa.Integer(), nullable=False),
        sa.Column("archive_digest", sa.String(71), nullable=True),
        sa.Column("manifest_digest", sa.String(71), nullable=True),
        sa.Column("archive_size", sa.BigInteger(), nullable=True),
        sa.Column("member_count", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(11), nullable=False),
        sa.Column("validation_at", sa.String(32), nullable=True),
        sa.Column("validation_result", sa.String(7), nullable=False),
        sa.CheckConstraint(f"state IN ({_SNAPSHOT_STATES})", name="snapshot_state"),
        sa.CheckConstraint(
            f"validation_result IN ({_VALIDATION_RESULTS})",
            name="snapshot_validation_result",
        ),
        sa.CheckConstraint(
            "prior_runtime_state IN ('running','stopped')",
            name="snapshot_prior_runtime_state",
        ),
        sa.CheckConstraint("manifest_version = 1", name="snapshot_manifest_version_v1"),
        sa.CheckConstraint(
            "label IS NULL OR length(label) BETWEEN 1 AND 480",
            name="snapshot_label_bounded",
        ),
        sa.CheckConstraint(
            "archive_digest IS NULL OR (length(archive_digest) = 71 "
            "AND substr(archive_digest, 1, 7) = 'sha256:' "
            "AND substr(archive_digest, 8) NOT GLOB '*[^0-9a-f]*')",
            name="snapshot_archive_digest_sha256",
        ),
        sa.CheckConstraint(
            "manifest_digest IS NULL OR (length(manifest_digest) = 71 "
            "AND substr(manifest_digest, 1, 7) = 'sha256:' "
            "AND substr(manifest_digest, 8) NOT GLOB '*[^0-9a-f]*')",
            name="snapshot_manifest_digest_sha256",
        ),
        sa.CheckConstraint(
            "archive_size IS NULL OR archive_size >= 0",
            name="snapshot_archive_size_nonnegative",
        ),
        sa.CheckConstraint(
            "member_count IS NULL OR member_count > 0",
            name="snapshot_member_count_positive",
        ),
        sa.CheckConstraint(
            "(state = 'creating' AND validation_result = 'pending' "
            "AND validation_at IS NULL AND archive_digest IS NULL "
            "AND manifest_digest IS NULL AND archive_size IS NULL "
            "AND member_count IS NULL) OR "
            "(state = 'available' AND validation_result = 'passed' "
            "AND validation_at IS NOT NULL AND archive_digest IS NOT NULL "
            "AND manifest_digest IS NOT NULL AND archive_size IS NOT NULL "
            "AND member_count IS NOT NULL AND prior_runtime_state IS NOT NULL) OR "
            "(state IN ('failed', 'quarantined') "
            "AND validation_result = 'failed' AND validation_at IS NOT NULL)",
            name="snapshot_state_fields_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["source_managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_revision_id"], ["runtime_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["captured_fixture_definition_id"],
            ["fixture_definitions.id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_snapshots_instance_captured",
        "snapshots",
        ["source_managed_instance_id", "captured_at"],
    )
    op.create_table(
        "snapshot_create_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("managed_instance_id", sa.String(36), nullable=False),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column("started_at", sa.String(32), nullable=True),
        sa.Column("heartbeat_at", sa.String(32), nullable=True),
        sa.Column("completed_at", sa.String(32), nullable=True),
        sa.Column("state", sa.String(23), nullable=False),
        sa.Column("phase", sa.String(30), nullable=False),
        sa.Column("failure_reason", sa.String(256), nullable=True),
        sa.Column(
            "reconciliation_required", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("lease_token_digest", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.String(32), nullable=True),
        sa.CheckConstraint(
            f"state IN ({_OP_STATES})", name="snapshot_create_operation_state"
        ),
        sa.CheckConstraint(
            f"phase IN ({_OP_PHASES})", name="snapshot_create_operation_phase"
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="snapshot_create_failure_reason_bounded",
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="snapshot_create_lease_digest_sha256",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(["snapshot_id"], ["snapshots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "uq_snapshot_create_active_instance",
        "snapshot_create_operations",
        ["managed_instance_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_snapshot_create_instance_requested",
        "snapshot_create_operations",
        ["managed_instance_id", "requested_at"],
    )
    op.create_table(
        "managed_instance_mutation_leases",
        sa.Column("managed_instance_id", sa.String(36), primary_key=True),
        sa.Column("operation_kind", sa.String(20), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("lease_token_digest", sa.String(64), nullable=True),
        sa.Column("acquired_at", sa.String(32), nullable=True),
        sa.Column("heartbeat_at", sa.String(32), nullable=True),
        sa.Column("lease_expires_at", sa.String(32), nullable=True),
        sa.CheckConstraint(
            f"operation_kind IN ({_MUTATION_KINDS})",
            name="managed_instance_mutation_kind",
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="managed_mutation_lease_digest_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="RESTRICT",
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO managed_instance_mutation_leases "
            "(managed_instance_id, operation_kind, operation_id, "
            "lease_token_digest, acquired_at, heartbeat_at, lease_expires_at) "
            "SELECT managed_instance_id, 'reset', id, lease_token_digest, "
            "started_at, heartbeat_at, lease_expires_at FROM reset_operations "
            "WHERE state IN ('queued', 'running')"
        )
    )
    op.create_table(
        "global_heavy_operation_lease",
        sa.Column("singleton_id", sa.Integer(), primary_key=True),
        sa.Column("operation_kind", sa.String(16), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("lease_token_digest", sa.String(64), nullable=True),
        sa.Column("acquired_at", sa.String(32), nullable=True),
        sa.Column("heartbeat_at", sa.String(32), nullable=True),
        sa.Column("lease_expires_at", sa.String(32), nullable=True),
        sa.CheckConstraint("singleton_id = 1", name="global_heavy_singleton"),
        sa.CheckConstraint(
            f"operation_kind IN ({_HEAVY_KINDS})", name="global_heavy_operation_kind"
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="global_heavy_lease_digest_sha256",
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    for table in (
        "snapshot_create_operations",
        "snapshots",
        "managed_instance_mutation_leases",
        "global_heavy_operation_lease",
    ):
        count = connection.scalar(sa.text(f"SELECT COUNT(*) FROM {table}"))
        if count:
            raise RuntimeError("Cannot downgrade populated Phase 7 snapshot state.")
    op.drop_table("global_heavy_operation_lease")
    op.drop_table("managed_instance_mutation_leases")
    op.drop_index(
        "ix_snapshot_create_instance_requested",
        table_name="snapshot_create_operations",
    )
    op.drop_index(
        "uq_snapshot_create_active_instance",
        table_name="snapshot_create_operations",
    )
    op.drop_table("snapshot_create_operations")
    op.drop_index("ix_snapshots_instance_captured", table_name="snapshots")
    op.drop_table("snapshots")
