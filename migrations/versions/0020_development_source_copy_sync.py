"""Add stopped-only development-source copy/sync deployment state.

Revision ID: 0020_dev_source_copy_sync
Revises: 0019_runtime_execution_ownership
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_dev_source_copy_sync"
down_revision: str | Sequence[str] | None = "0019_runtime_execution_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch_op.alter_column(
            "operation_kind",
            existing_type=sa.String(length=32),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ('reset','snapshot_create','snapshot_restore',"
            "'lifecycle','fixture_assignment','runtime_reassignment','instance_delete',"
            "'development_source_attachment','development_source_deployment')",
        )
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.alter_column(
            "operation_kind",
            existing_type=sa.String(length=16),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore','snapshot_delete',"
            "'instance_delete','development_source_deployment')",
        )

    op.create_table(
        "development_source_deployments",
        sa.Column("managed_instance_id", sa.String(36), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column(
            "mode",
            sa.Enum(
                "copy",
                name="development_source_deployment_mode",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("last_successful_operation_id", sa.String(36), nullable=False),
        sa.Column("source_manifest_digest", sa.String(71), nullable=False),
        sa.Column("verified_destination_digest", sa.String(71), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("trusted_container_id", sa.String(64), nullable=False),
        sa.Column("observed_source_name", sa.String(256), nullable=False),
        sa.Column("observed_source_version", sa.String(128), nullable=False),
        sa.Column("verified_at", sa.String(32), nullable=False),
        sa.Column(
            "confidence",
            sa.Enum(
                "verified_at_last_operation",
                "stale",
                "unknown",
                name="development_source_deployment_confidence",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128",
            name="development_source_deployment_addon_id_bounded",
        ),
        sa.CheckConstraint(
            "length(source_manifest_digest) = 71 AND "
            "substr(source_manifest_digest, 1, 7) = 'sha256:' AND "
            "substr(source_manifest_digest, 8) NOT GLOB '*[^0-9a-f]*'",
            name="development_source_deployment_source_digest",
        ),
        sa.CheckConstraint(
            "length(verified_destination_digest) = 71 AND "
            "substr(verified_destination_digest, 1, 7) = 'sha256:' AND "
            "substr(verified_destination_digest, 8) NOT GLOB '*[^0-9a-f]*'",
            name="development_source_deployment_destination_digest",
        ),
        sa.CheckConstraint(
            "length(trusted_container_id) = 64 AND "
            "trusted_container_id NOT GLOB '*[^0-9a-f]*'",
            name="development_source_deployment_container_id",
        ),
        sa.CheckConstraint(
            "length(observed_source_name) BETWEEN 1 AND 256 AND "
            "length(observed_source_version) BETWEEN 1 AND 128",
            name="development_source_deployment_observation_bounded",
        ),
        sa.CheckConstraint(
            "length(verified_at) BETWEEN 20 AND 32",
            name="development_source_deployment_verified_at_bounded",
        ),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["development_source_id"], ["development_sources.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["runtime_revision_id"], ["runtime_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["last_successful_operation_id"],
            ["development_source_deployment_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("managed_instance_id", "development_source_id"),
    )
    op.create_index(
        "ix_development_source_deployment_source",
        "development_source_deployments",
        ["development_source_id"],
    )

    op.create_table(
        "development_source_deployment_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("original_managed_instance_id", sa.String(36), nullable=False),
        sa.Column("managed_instance_id", sa.String(36), nullable=True),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("source_logical_name", sa.String(120), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column(
            "request_kind",
            sa.Enum(
                "copy_sync",
                name="development_source_deployment_request_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "reconciliation_required",
                name="development_source_deployment_operation_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "phase",
            sa.Enum(
                "acquire",
                "resolve",
                "stage",
                "inspect_destination",
                "apply",
                "verify",
                "finalize",
                name="development_source_deployment_operation_phase",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            nullable=False,
        ),
        sa.Column(
            "result_classification",
            sa.Enum(
                "initial_deployment",
                "synchronized",
                "unchanged",
                name="development_source_deployment_result",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("new_count", sa.Integer(), nullable=True),
        sa.Column("changed_count", sa.Integer(), nullable=True),
        sa.Column("unchanged_count", sa.Integer(), nullable=True),
        sa.Column("destination_only_count", sa.Integer(), nullable=True),
        sa.Column("transferred_bytes", sa.BigInteger(), nullable=True),
        sa.Column("expected_container_id", sa.String(64), nullable=True),
        sa.Column("started_at", sa.String(32), nullable=True),
        sa.Column("heartbeat_at", sa.String(32), nullable=True),
        sa.Column("completed_at", sa.String(32), nullable=True),
        sa.Column("failure_reason", sa.String(256), nullable=True),
        sa.Column("reconciliation_required", sa.Boolean(), nullable=False),
        sa.Column("lease_token_digest", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.String(32), nullable=True),
        sa.CheckConstraint(
            "length(source_logical_name) BETWEEN 1 AND 120 AND "
            "length(addon_id) BETWEEN 1 AND 128",
            name="development_source_deployment_history_identity_bounded",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="development_source_deployment_failure_reason_bounded",
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="development_source_deployment_lease_digest",
        ),
        sa.CheckConstraint(
            "expected_container_id IS NULL OR (length(expected_container_id) = 64 "
            "AND expected_container_id NOT GLOB '*[^0-9a-f]*')",
            name="development_source_deployment_expected_container",
        ),
        sa.CheckConstraint(
            "(new_count IS NULL OR new_count >= 0) AND "
            "(changed_count IS NULL OR changed_count >= 0) AND "
            "(unchanged_count IS NULL OR unchanged_count >= 0) AND "
            "(destination_only_count IS NULL OR destination_only_count >= 0) AND "
            "(transferred_bytes IS NULL OR transferred_bytes >= 0)",
            name="development_source_deployment_aggregates_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["development_source_id"], ["development_sources.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "uq_development_source_deployment_active_instance",
        "development_source_deployment_operations",
        ["managed_instance_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_development_source_deployment_original_requested",
        "development_source_deployment_operations",
        ["original_managed_instance_id", "requested_at"],
    )
    op.create_index(
        "ix_development_source_deployment_operation_source",
        "development_source_deployment_operations",
        ["development_source_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    for table in (
        "development_source_deployments",
        "development_source_deployment_operations",
    ):
        if connection.scalar(sa.text(f"SELECT COUNT(*) FROM {table}")):
            raise RuntimeError(
                "Cannot downgrade populated development-source deployment data."
            )
    if connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM managed_instance_mutation_leases "
            "WHERE operation_kind = 'development_source_deployment'"
        )
    ) or connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM global_heavy_operation_lease "
            "WHERE operation_kind = 'development_source_deployment'"
        )
    ):
        raise RuntimeError("Cannot downgrade active deployment authority.")
    op.drop_index(
        "ix_development_source_deployment_operation_source",
        table_name="development_source_deployment_operations",
    )
    op.drop_index(
        "ix_development_source_deployment_original_requested",
        table_name="development_source_deployment_operations",
    )
    op.drop_index(
        "uq_development_source_deployment_active_instance",
        table_name="development_source_deployment_operations",
    )
    op.drop_table("development_source_deployment_operations")
    op.drop_index(
        "ix_development_source_deployment_source",
        table_name="development_source_deployments",
    )
    op.drop_table("development_source_deployments")
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.alter_column(
            "operation_kind",
            existing_type=sa.String(length=32),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore',"
            "'snapshot_delete','instance_delete')",
        )
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch_op.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ('reset','snapshot_create','snapshot_restore',"
            "'lifecycle','fixture_assignment','runtime_reassignment','instance_delete',"
            "'development_source_attachment')",
        )
