"""Add typed managed-instance lifecycle-operation history.

Revision ID: 0016_lifecycle_history
Revises: 0015_instance_deletion
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_lifecycle_history"
down_revision: str | Sequence[str] | None = "0015_instance_deletion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "managed_instance_lifecycle_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("original_managed_instance_id", sa.String(length=36), nullable=False),
        sa.Column("managed_instance_id", sa.String(length=36), nullable=True),
        sa.Column("instance_name", sa.String(length=256), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "provision",
                "start",
                "stop",
                "deprovision",
                name="managed_instance_lifecycle_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("requested_at", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.String(length=32), nullable=False),
        sa.Column("completed_at", sa.String(length=32), nullable=True),
        sa.Column(
            "prior_state",
            sa.Enum(
                "unprovisioned",
                "stopped",
                "running_usable",
                "running_not_ready",
                "transitional",
                "ambiguous",
                name="managed_instance_lifecycle_semantic_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("resulting_state", sa.String(length=20), nullable=True),
        sa.Column(
            "state",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                "reconciliation_required",
                name="managed_instance_lifecycle_operation_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("failure_reason", sa.String(length=256), nullable=True),
        sa.Column(
            "reconciliation_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.CheckConstraint(
            "length(instance_name) BETWEEN 1 AND 256",
            name="lifecycle_operation_instance_name_bounded",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="lifecycle_operation_failure_reason_bounded",
        ),
        sa.CheckConstraint(
            "resulting_state IS NULL OR resulting_state IN "
            "('unprovisioned','stopped','running_usable','running_not_ready',"
            "'transitional','ambiguous')",
            name="lifecycle_operation_resulting_state_closed",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            name="fk_lifecycle_operation_live_instance",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_lifecycle_operation_original_requested",
        "managed_instance_lifecycle_operations",
        ["original_managed_instance_id", "requested_at"],
        unique=False,
    )
    op.create_index(
        "uq_lifecycle_operation_running_instance",
        "managed_instance_lifecycle_operations",
        ["original_managed_instance_id"],
        unique=True,
        sqlite_where=sa.text("state = 'running'"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM managed_instance_lifecycle_operations")
    ):
        raise RuntimeError("Cannot downgrade populated lifecycle operation history.")
    op.drop_index(
        "uq_lifecycle_operation_running_instance",
        table_name="managed_instance_lifecycle_operations",
    )
    op.drop_index(
        "ix_lifecycle_operation_original_requested",
        table_name="managed_instance_lifecycle_operations",
    )
    op.drop_table("managed_instance_lifecycle_operations")
