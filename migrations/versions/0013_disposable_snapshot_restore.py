"""Add durable disposable snapshot restore operations.

Revision ID: 0013_disposable_snapshot_restore
Revises: 0012_disposable_snapshot_creation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_disposable_snapshot_restore"
down_revision: str | None = "0012_disposable_snapshot_creation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES = "'queued','running','succeeded','failed','reconciliation_required'"
_PHASES = (
    "'queued','acquiring_leases','validating_snapshot','resolving_instance',"
    "'stopping','deprovisioning','provisioning','applying_snapshot',"
    "'verifying_files','verifying_databases','restoring_running_state',"
    "'waiting_readiness','final_verification','succeeded','failed',"
    "'reconciliation_required'"
)


def upgrade() -> None:
    op.create_table(
        "snapshot_restore_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("managed_instance_id", sa.String(36), nullable=False),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column("prior_source_runtime_state", sa.String(7), nullable=True),
        sa.Column("old_container_id", sa.String(64), nullable=True),
        sa.Column("fresh_container_id", sa.String(64), nullable=True),
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
            f"state IN ({_STATES})", name="snapshot_restore_operation_state"
        ),
        sa.CheckConstraint(
            f"phase IN ({_PHASES})", name="snapshot_restore_operation_phase"
        ),
        sa.CheckConstraint(
            "prior_source_runtime_state IS NULL OR "
            "prior_source_runtime_state IN ('running','stopped')",
            name="snapshot_restore_prior_runtime_state",
        ),
        sa.CheckConstraint(
            "old_container_id IS NULL OR (length(old_container_id) = 64 "
            "AND old_container_id NOT GLOB '*[^0-9a-f]*')",
            name="snapshot_restore_old_container_id",
        ),
        sa.CheckConstraint(
            "fresh_container_id IS NULL OR (length(fresh_container_id) = 64 "
            "AND fresh_container_id NOT GLOB '*[^0-9a-f]*')",
            name="snapshot_restore_fresh_container_id",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="snapshot_restore_failure_reason_bounded",
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="snapshot_restore_lease_digest_sha256",
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
            name="snapshot_restore_state_fields_consistent",
        ),
        sa.ForeignKeyConstraint(["snapshot_id"], ["snapshots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "uq_snapshot_restore_active_instance",
        "snapshot_restore_operations",
        ["managed_instance_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_snapshot_restore_instance_requested",
        "snapshot_restore_operations",
        ["managed_instance_id", "requested_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT COUNT(*) FROM snapshot_restore_operations")):
        raise RuntimeError("Cannot downgrade populated snapshot restore state.")
    op.drop_index(
        "ix_snapshot_restore_instance_requested",
        table_name="snapshot_restore_operations",
    )
    op.drop_index(
        "uq_snapshot_restore_active_instance",
        table_name="snapshot_restore_operations",
    )
    op.drop_table("snapshot_restore_operations")
