"""Remediate Phase 9 completion blockers.

Revision ID: 0034_phase9_completion_remediation
Revises: 0033_agent_workspace_cleanup
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_phase9_completion_remediation"
down_revision: str | Sequence[str] | None = "0033_agent_workspace_cleanup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PHASE9_TARGET_TABLES = (
    "agent_workspace_promotion_plans",
    "agent_workspace_promotion_operations",
    "agent_workspace_promotion_handoffs",
    "promotion_verification_operations",
)


def upgrade() -> None:
    for table in _PHASE9_TARGET_TABLES:
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.add_column(sa.Column("original_target_instance_id", sa.String(36)))
            batch.alter_column(
                "target_instance_id",
                existing_type=sa.String(36),
                nullable=True,
            )
        op.execute(
            sa.text(
                f"UPDATE {table} SET original_target_instance_id = target_instance_id"
            )
        )
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.alter_column(
                "original_target_instance_id",
                existing_type=sa.String(36),
                nullable=False,
            )
        op.create_index(
            f"ix_{table}_original_target",
            table,
            ["original_target_instance_id"],
        )

    with op.batch_alter_table(
        "agent_workspace_promotion_plans", recreate="always"
    ) as batch:
        batch.add_column(sa.Column("original_target_name", sa.String(256)))
    op.execute(
        sa.text(
            "UPDATE agent_workspace_promotion_plans "
            "SET original_target_name = ("
            "SELECT targets.name FROM targets "
            "WHERE targets.id = agent_workspace_promotion_plans."
            "original_target_instance_id)"
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    for table in _PHASE9_TARGET_TABLES:
        detached = connection.scalar(
            sa.text(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE target_instance_id IS NULL OR "
                "target_instance_id != original_target_instance_id"
            )
        )
        if detached:
            raise RuntimeError(
                "Cannot downgrade detached Phase 9 target history without "
                "losing retained identity semantics."
            )

    with op.batch_alter_table(
        "agent_workspace_promotion_plans", recreate="always"
    ) as batch:
        batch.drop_column("original_target_name")

    for table in reversed(_PHASE9_TARGET_TABLES):
        op.drop_index(f"ix_{table}_original_target", table_name=table)
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.alter_column(
                "target_instance_id",
                existing_type=sa.String(36),
                nullable=False,
            )
            batch.drop_column("original_target_instance_id")
