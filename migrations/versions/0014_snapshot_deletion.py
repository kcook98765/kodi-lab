"""Add explicit snapshot deletion and durable deletion history.

Revision ID: 0014_snapshot_deletion
Revises: 0013_disposable_snapshot_restore
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_snapshot_deletion"
down_revision: str | None = "0013_disposable_snapshot_restore"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OP_STATES = "'queued','running','succeeded','failed','reconciliation_required'"
_DELETE_PHASES = (
    "'queued','acquiring_heavy_lease','validating','deleting_artifact',"
    "'verifying_absence','deleting_metadata','succeeded','failed',"
    "'reconciliation_required'"
)
_NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def _snapshot_state_constraint() -> str:
    return "state IN ('creating','available','failed','quarantined','deleting')"


def _snapshot_fields_constraint() -> str:
    return (
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
        "AND validation_at IS NOT NULL)"
    )


def upgrade() -> None:
    for table in ("snapshot_create_operations", "snapshot_restore_operations"):
        with op.batch_alter_table(
            table, recreate="always", naming_convention=_NAMING
        ) as batch_op:
            batch_op.drop_constraint(
                f"fk_{table}_snapshot_id_snapshots", type_="foreignkey"
            )

    with op.batch_alter_table("snapshots", recreate="always") as batch_op:
        batch_op.drop_constraint("snapshot_state", type_="check")
        batch_op.drop_constraint("snapshot_state_fields_consistent", type_="check")
        batch_op.create_check_constraint("snapshot_state", _snapshot_state_constraint())
        batch_op.create_check_constraint(
            "snapshot_state_fields_consistent", _snapshot_fields_constraint()
        )

    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN "
            "('snapshot_create','snapshot_restore','snapshot_delete')",
        )

    op.create_table(
        "snapshot_delete_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("managed_instance_id", sa.String(36), nullable=False),
        sa.Column("prior_snapshot_state", sa.String(11), nullable=False),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column("started_at", sa.String(32), nullable=True),
        sa.Column("heartbeat_at", sa.String(32), nullable=True),
        sa.Column("completed_at", sa.String(32), nullable=True),
        sa.Column("state", sa.String(23), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("failure_reason", sa.String(256), nullable=True),
        sa.Column(
            "reconciliation_required", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("lease_token_digest", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.String(32), nullable=True),
        sa.CheckConstraint(
            "prior_snapshot_state IN ('available','failed','quarantined')",
            name="snapshot_delete_prior_state",
        ),
        sa.CheckConstraint(
            f"state IN ({_OP_STATES})", name="snapshot_delete_operation_state"
        ),
        sa.CheckConstraint(
            f"phase IN ({_DELETE_PHASES})", name="snapshot_delete_operation_phase"
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="snapshot_delete_failure_reason_bounded",
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="snapshot_delete_lease_digest_sha256",
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
            name="snapshot_delete_state_fields_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "uq_snapshot_delete_active_snapshot",
        "snapshot_delete_operations",
        ["snapshot_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_snapshot_delete_instance_requested",
        "snapshot_delete_operations",
        ["managed_instance_id", "requested_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT COUNT(*) FROM snapshot_delete_operations")):
        raise RuntimeError("Cannot downgrade populated snapshot deletion history.")
    if connection.scalar(
        sa.text("SELECT COUNT(*) FROM snapshots WHERE state='deleting'")
    ):
        raise RuntimeError("Cannot downgrade snapshots with active deletion state.")
    for table in ("snapshot_create_operations", "snapshot_restore_operations"):
        missing = connection.scalar(
            sa.text(
                f"SELECT COUNT(*) FROM {table} o LEFT JOIN snapshots s "
                "ON s.id=o.snapshot_id WHERE s.id IS NULL"
            )
        )
        if missing:
            raise RuntimeError(
                "Cannot downgrade snapshot history with deleted targets."
            )

    op.drop_index(
        "ix_snapshot_delete_instance_requested", table_name="snapshot_delete_operations"
    )
    op.drop_index(
        "uq_snapshot_delete_active_snapshot", table_name="snapshot_delete_operations"
    )
    op.drop_table("snapshot_delete_operations")

    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore')",
        )

    with op.batch_alter_table("snapshots", recreate="always") as batch_op:
        batch_op.drop_constraint("snapshot_state", type_="check")
        batch_op.drop_constraint("snapshot_state_fields_consistent", type_="check")
        batch_op.create_check_constraint(
            "snapshot_state",
            "state IN ('creating','available','failed','quarantined')",
        )
        batch_op.create_check_constraint(
            "snapshot_state_fields_consistent",
            _snapshot_fields_constraint().split(" OR (state = 'deleting'")[0],
        )

    for table in ("snapshot_create_operations", "snapshot_restore_operations"):
        with op.batch_alter_table(
            table, recreate="always", naming_convention=_NAMING
        ) as batch_op:
            batch_op.create_foreign_key(
                f"fk_{table}_snapshot_id_snapshots",
                "snapshots",
                ["snapshot_id"],
                ["id"],
                ondelete="RESTRICT",
            )
