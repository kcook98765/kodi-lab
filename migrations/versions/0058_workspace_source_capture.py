"""Durable source captures into existing protected workspaces.

Revision ID: 0058_workspace_source_capture
Revises: 0057_development_capability_amendments
"""

# ruff: noqa: E501

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_workspace_source_capture"
down_revision = "0057_development_capability_amendments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_workspaces", recreate="always") as batch:
        batch.drop_constraint("agent_workspace_state", type_="check")
        batch.alter_column(
            "state",
            existing_type=sa.String(23),
            type_=sa.Enum(
                "creating",
                "capturing",
                "ready",
                "promoted",
                "abandoned",
                "reconciliation_required",
                name="agent_workspace_state",
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=False,
        )
        batch.add_column(sa.Column("creation_baseline_digest", sa.String(71)))
        batch.add_column(sa.Column("active_baseline_artifact_id", sa.String(36)))
        batch.add_column(sa.Column("active_capture_id", sa.String(36)))
        batch.add_column(sa.Column("active_observed_version", sa.String(128)))
    op.execute(
        "UPDATE agent_workspaces SET "
        "creation_baseline_digest=baseline_manifest_digest, "
        "active_baseline_artifact_id=baseline_artifact_id, "
        "active_observed_version=source_observed_version "
        "WHERE baseline_manifest_digest IS NOT NULL"
    )
    op.create_table(
        "agent_workspace_capture_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("prior_generation", sa.Integer(), nullable=False),
        sa.Column("resulting_generation", sa.Integer(), nullable=False),
        sa.Column("prior_baseline_id", sa.String(36), nullable=False),
        sa.Column("prior_baseline_digest", sa.String(71), nullable=False),
        sa.Column("prior_tree_digest", sa.String(71), nullable=False),
        sa.Column("captured_digest", sa.String(71)),
        sa.Column("captured_version", sa.String(128)),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column("completed_at", sa.String(32)),
        sa.Column("failure_code", sa.String(128)),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["agent_workspaces.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["development_source_id"],
            ["development_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('staging','switching','succeeded','failed','reconciliation_required')",
            name="agent_workspace_capture_state_closed",
        ),
        sa.CheckConstraint(
            "prior_generation >= 0 AND resulting_generation = prior_generation + 1",
            name="agent_workspace_capture_generation_step",
        ),
        sa.CheckConstraint(
            "length(prior_baseline_digest)=71 AND length(prior_tree_digest)=71 "
            "AND (captured_digest IS NULL OR length(captured_digest)=71) "
            "AND (captured_version IS NULL OR length(captured_version) BETWEEN 1 AND 128)",
            name="agent_workspace_capture_digests_bounded",
        ),
        sa.CheckConstraint(
            "(state IN ('staging','failed') OR captured_digest IS NOT NULL) AND "
            "(state NOT IN ('succeeded','failed','reconciliation_required') "
            "OR completed_at IS NOT NULL)",
            name="agent_workspace_capture_completion_consistent",
        ),
    )
    op.create_index(
        "ix_agent_workspace_capture_workspace_time",
        "agent_workspace_capture_operations",
        ["workspace_id", "requested_at"],
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_workspace_capture_active "
        "ON agent_workspace_capture_operations(workspace_id) "
        "WHERE state IN ('staging','switching')"
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM agent_workspace_capture_operations)")
    ) or connection.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM agent_workspaces "
            "WHERE state='capturing' OR active_capture_id IS NOT NULL)"
        )
    ):
        raise RuntimeError("refusing to discard workspace source-capture evidence")
    op.execute("DROP INDEX uq_agent_workspace_capture_active")
    op.drop_index(
        "ix_agent_workspace_capture_workspace_time",
        table_name="agent_workspace_capture_operations",
    )
    op.drop_table("agent_workspace_capture_operations")
    with op.batch_alter_table("agent_workspaces", recreate="always") as batch:
        batch.drop_column("active_capture_id")
        batch.drop_column("active_observed_version")
        batch.drop_column("active_baseline_artifact_id")
        batch.drop_column("creation_baseline_digest")
        batch.drop_constraint("agent_workspace_state", type_="check")
        batch.alter_column(
            "state",
            existing_type=sa.String(23),
            type_=sa.Enum(
                "creating",
                "ready",
                "promoted",
                "abandoned",
                "reconciliation_required",
                name="agent_workspace_state",
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=False,
        )
