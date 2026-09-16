"""Add disposable instance deletion and detached snapshot history.

Revision ID: 0015_instance_deletion
Revises: 0014_snapshot_deletion
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_instance_deletion"
down_revision: str | None = "0014_snapshot_deletion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
_HISTORY_TABLES = (
    "reset_operations",
    "snapshot_create_operations",
    "snapshot_restore_operations",
    "snapshot_delete_operations",
)
_HISTORY_FKS = {
    "reset_operations": "fk_reset_operation_managed_instance",
    "snapshot_create_operations": (
        "fk_snapshot_create_operations_managed_instance_id_managed_instances"
    ),
    "snapshot_restore_operations": (
        "fk_snapshot_restore_operations_managed_instance_id_managed_instances"
    ),
    "snapshot_delete_operations": (
        "fk_snapshot_delete_operations_managed_instance_id_managed_instances"
    ),
}
_OP_STATES = "'queued','running','succeeded','failed','reconciliation_required'"
_OP_PHASES = (
    "'queued','acquiring_leases','resolving_instance','stopping','deprovisioning',"
    "'verifying_container_absence','preserving_snapshots','deleting_snapshots',"
    "'verifying_snapshot_absence','deleting_metadata','succeeded','failed',"
    "'reconciliation_required'"
)


def upgrade() -> None:
    connection = op.get_bind()
    oversized = connection.execute(
        sa.text(
            "SELECT target_id, length(notes) FROM managed_instances "
            "WHERE notes IS NOT NULL AND length(notes) > 4096 LIMIT 1"
        )
    ).first()
    if oversized is not None:
        raise RuntimeError(
            "Cannot upgrade: ManagedInstance notes exceed 4096 Unicode code points."
        )

    with op.batch_alter_table("managed_instances", recreate="always") as batch_op:
        batch_op.alter_column(
            "notes", existing_type=sa.Text(), type_=sa.String(4096), nullable=True
        )
        batch_op.create_check_constraint(
            "managed_instance_notes_bounded",
            "notes IS NULL OR length(notes) <= 4096",
        )

    op.add_column(
        "snapshots",
        sa.Column("original_source_managed_instance_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "snapshots", sa.Column("source_binding", sa.String(14), nullable=True)
    )
    op.add_column(
        "snapshots", sa.Column("source_instance_name", sa.String(256), nullable=True)
    )
    op.add_column(
        "snapshots", sa.Column("source_deleted_at", sa.String(32), nullable=True)
    )
    op.execute(
        sa.text(
            "UPDATE snapshots SET original_source_managed_instance_id = "
            "source_managed_instance_id, source_binding = 'attached'"
        )
    )
    with op.batch_alter_table("snapshots", recreate="always") as batch_op:
        batch_op.alter_column(
            "source_managed_instance_id",
            existing_type=sa.String(36),
            nullable=True,
        )
        batch_op.alter_column(
            "original_source_managed_instance_id",
            existing_type=sa.String(36),
            nullable=False,
        )
        batch_op.alter_column(
            "source_binding", existing_type=sa.String(14), nullable=False
        )
        batch_op.create_check_constraint(
            "snapshot_source_binding",
            "source_binding IN ('attached','deleted_source')",
        )
        batch_op.create_check_constraint(
            "snapshot_source_binding_consistent",
            "(source_binding = 'attached' AND source_managed_instance_id IS NOT NULL "
            "AND original_source_managed_instance_id = source_managed_instance_id "
            "AND source_instance_name IS NULL AND source_deleted_at IS NULL) OR "
            "(source_binding = 'deleted_source' AND source_managed_instance_id IS NULL "
            "AND source_instance_name IS NOT NULL AND length(source_instance_name) > 0 "
            "AND source_deleted_at IS NOT NULL)",
        )

    for table in _HISTORY_TABLES:
        with op.batch_alter_table(
            table, recreate="always", naming_convention=_NAMING
        ) as batch_op:
            batch_op.drop_constraint(
                _HISTORY_FKS[table],
                type_="foreignkey",
            )

    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch_op.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ('reset','snapshot_create','snapshot_restore',"
            "'lifecycle','fixture_assignment','runtime_reassignment','instance_delete')",
        )

    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore','snapshot_delete',"
            "'instance_delete')",
        )

    op.create_table(
        "managed_instance_delete_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("original_managed_instance_id", sa.String(36), nullable=False),
        sa.Column("source_instance_name", sa.String(256), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("fixture_definition_id", sa.String(36), nullable=True),
        sa.Column("preserve_snapshots", sa.Boolean(), nullable=False),
        sa.Column("snapshot_count", sa.Integer(), nullable=False),
        sa.Column("aggregate_snapshot_bytes", sa.BigInteger(), nullable=False),
        sa.Column("current_snapshot_id", sa.String(36), nullable=True),
        sa.Column("completed_snapshot_count", sa.Integer(), nullable=False),
        sa.Column("expected_container_id", sa.String(64), nullable=True),
        sa.Column("observed_container_id", sa.String(64), nullable=True),
        sa.Column("prior_state", sa.String(13), nullable=True),
        sa.Column("notes_at_deletion", sa.String(4096), nullable=True),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column("started_at", sa.String(32), nullable=True),
        sa.Column("heartbeat_at", sa.String(32), nullable=True),
        sa.Column("completed_at", sa.String(32), nullable=True),
        sa.Column("state", sa.String(23), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("failure_reason", sa.String(256), nullable=True),
        sa.Column(
            "reconciliation_required", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("lease_token_digest", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.String(32), nullable=True),
        sa.CheckConstraint(
            f"state IN ({_OP_STATES})", name="instance_delete_operation_state"
        ),
        sa.CheckConstraint(
            f"phase IN ({_OP_PHASES})", name="instance_delete_operation_phase"
        ),
        sa.CheckConstraint(
            "prior_state IS NULL OR prior_state IN "
            "('unprovisioned','stopped','running')",
            name="instance_delete_prior_state",
        ),
        sa.CheckConstraint(
            "length(source_instance_name) BETWEEN 1 AND 256",
            name="instance_delete_source_name_bounded",
        ),
        sa.CheckConstraint(
            "snapshot_count >= 0 AND completed_snapshot_count "
            "BETWEEN 0 AND snapshot_count",
            name="instance_delete_snapshot_progress_valid",
        ),
        sa.CheckConstraint(
            "aggregate_snapshot_bytes >= 0",
            name="instance_delete_snapshot_bytes_nonnegative",
        ),
        sa.CheckConstraint(
            "notes_at_deletion IS NULL OR length(notes_at_deletion) <= 4096",
            name="instance_delete_notes_bounded",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="instance_delete_failure_reason_bounded",
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="instance_delete_lease_digest_sha256",
        ),
        sa.CheckConstraint(
            "expected_container_id IS NULL OR (length(expected_container_id) = 64 "
            "AND expected_container_id NOT GLOB '*[^0-9a-f]*')",
            name="instance_delete_expected_container_id",
        ),
        sa.CheckConstraint(
            "observed_container_id IS NULL OR (length(observed_container_id) = 64 "
            "AND observed_container_id NOT GLOB '*[^0-9a-f]*')",
            name="instance_delete_observed_container_id",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(
            ["runtime_revision_id"], ["runtime_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["fixture_definition_id"],
            ["fixture_definitions.id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "uq_instance_delete_active_instance",
        "managed_instance_delete_operations",
        ["original_managed_instance_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_instance_delete_original_requested",
        "managed_instance_delete_operations",
        ["original_managed_instance_id", "requested_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT COUNT(*) FROM managed_instance_delete_operations")
    ):
        raise RuntimeError("Cannot downgrade populated instance deletion history.")
    if connection.scalar(
        sa.text("SELECT COUNT(*) FROM snapshots WHERE source_binding='deleted_source'")
    ):
        raise RuntimeError("Cannot downgrade detached snapshots.")
    for table in _HISTORY_TABLES:
        missing = connection.scalar(
            sa.text(
                f"SELECT COUNT(*) FROM {table} h LEFT JOIN managed_instances m "
                "ON m.target_id=h.managed_instance_id WHERE m.target_id IS NULL"
            )
        )
        if missing:
            raise RuntimeError(
                "Cannot downgrade operation history without a live ManagedInstance."
            )

    op.drop_index(
        "ix_instance_delete_original_requested",
        table_name="managed_instance_delete_operations",
    )
    op.drop_index(
        "uq_instance_delete_active_instance",
        table_name="managed_instance_delete_operations",
    )
    op.drop_table("managed_instance_delete_operations")

    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN "
            "('snapshot_create','snapshot_restore','snapshot_delete')",
        )
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch_op.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ('reset','snapshot_create','snapshot_restore',"
            "'lifecycle','fixture_assignment','runtime_reassignment')",
        )

    for table in _HISTORY_TABLES:
        with op.batch_alter_table(
            table, recreate="always", naming_convention=_NAMING
        ) as batch_op:
            batch_op.create_foreign_key(
                _HISTORY_FKS[table],
                "managed_instances",
                ["managed_instance_id"],
                ["target_id"],
                ondelete="RESTRICT",
            )

    with op.batch_alter_table("snapshots", recreate="always") as batch_op:
        batch_op.drop_constraint("snapshot_source_binding_consistent", type_="check")
        batch_op.drop_constraint("snapshot_source_binding", type_="check")
        batch_op.alter_column(
            "source_managed_instance_id",
            existing_type=sa.String(36),
            nullable=False,
        )
        batch_op.drop_column("source_deleted_at")
        batch_op.drop_column("source_instance_name")
        batch_op.drop_column("source_binding")
        batch_op.drop_column("original_source_managed_instance_id")

    with op.batch_alter_table("managed_instances", recreate="always") as batch_op:
        batch_op.drop_constraint("managed_instance_notes_bounded", type_="check")
        batch_op.alter_column(
            "notes", existing_type=sa.String(4096), type_=sa.Text(), nullable=True
        )
