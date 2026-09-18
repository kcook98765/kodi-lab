"""Add content-free agent workspace mutation activity.

Revision ID: 0030_agent_workspace_mutations
Revises: 0029_agent_workspaces
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_agent_workspace_mutations"
down_revision: str | Sequence[str] | None = "0029_agent_workspaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_workspace_mutation_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "create",
                "replace",
                "patch",
                name="agent_workspace_mutation_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("relative_path", sa.String(512), nullable=False),
        sa.Column("occurred_at", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["agent_workspaces.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "generation >= 1", name="agent_workspace_mutation_generation_positive"
        ),
        sa.CheckConstraint(
            "length(relative_path) BETWEEN 1 AND 512",
            name="agent_workspace_mutation_path_bounded",
        ),
        sa.UniqueConstraint(
            "workspace_id", "generation", name="uq_workspace_mutation_generation"
        ),
    )
    op.create_index(
        "ix_agent_workspace_mutation_occurred",
        "agent_workspace_mutation_events",
        ["workspace_id", "occurred_at"],
    )


def downgrade() -> None:
    populated = op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM agent_workspace_mutation_events")
    )
    if populated:
        raise RuntimeError("Cannot downgrade populated workspace mutation history.")
    op.drop_index(
        "ix_agent_workspace_mutation_occurred",
        table_name="agent_workspace_mutation_events",
    )
    op.drop_table("agent_workspace_mutation_events")
