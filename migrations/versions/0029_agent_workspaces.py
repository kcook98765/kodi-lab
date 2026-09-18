"""Add protected agent workspaces and durable creation operations.

Revision ID: 0029_agent_workspaces
Revises: 0028_repository_replacement_override
"""

# SQLite check expressions are intentionally exact SQL literals.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_agent_workspaces"
down_revision: str | Sequence[str] | None = "0028_repository_replacement_override"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _heavy_constraint(*, include_workspace: bool) -> str:
    values = [
        "snapshot_create",
        "snapshot_restore",
        "snapshot_delete",
        "instance_delete",
        "development_source_deployment",
        "addon_zip_import",
        "addon_zip_installation",
        "addon_repository_refresh",
        "addon_repository_fetch",
        "addon_repository_installation",
        "repository_replacement",
    ]
    if include_workspace:
        values.append("agent_workspace_creation")
    return "operation_kind IN (" + ",".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.create_check_constraint(
            "global_heavy_operation_kind", _heavy_constraint(include_workspace=True)
        )

    op.create_table(
        "agent_workspaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("source_addon_id", sa.String(128), nullable=False),
        sa.Column("source_logical_name", sa.String(120), nullable=False),
        sa.Column("source_observed_name", sa.String(256), nullable=False),
        sa.Column("source_observed_version", sa.String(128), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "creating",
                "ready",
                "abandoned",
                "reconciliation_required",
                name="agent_workspace_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("baseline_artifact_id", sa.String(36), nullable=False, unique=True),
        sa.Column("baseline_manifest_digest", sa.String(71)),
        sa.Column(
            "generation", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("current_tree_digest", sa.String(71)),
        sa.Column("capture_started_at", sa.String(32), nullable=False),
        sa.Column("captured_at", sa.String(32)),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(
            ["development_source_id"], ["development_sources.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "generation >= 0", name="agent_workspace_generation_nonnegative"
        ),
        sa.CheckConstraint(
            "length(source_addon_id) BETWEEN 1 AND 128 AND length(source_logical_name) BETWEEN 1 AND 120 AND length(source_observed_name) BETWEEN 1 AND 256 AND length(source_observed_version) BETWEEN 1 AND 128",
            name="agent_workspace_source_snapshot_bounded",
        ),
        sa.CheckConstraint(
            "(baseline_manifest_digest IS NULL OR (length(baseline_manifest_digest)=71 AND substr(baseline_manifest_digest,1,7)='sha256:')) AND (current_tree_digest IS NULL OR (length(current_tree_digest)=71 AND substr(current_tree_digest,1,7)='sha256:'))",
            name="agent_workspace_digests_valid",
        ),
        sa.CheckConstraint(
            "state <> 'ready' OR (baseline_manifest_digest IS NOT NULL AND current_tree_digest IS NOT NULL AND captured_at IS NOT NULL)",
            name="agent_workspace_ready_complete",
        ),
    )
    op.create_index(
        "ix_agent_workspace_source_created",
        "agent_workspaces",
        ["development_source_id", "created_at"],
    )
    op.create_index("ix_agent_workspace_state", "agent_workspaces", ["state"])

    op.create_table(
        "agent_workspace_creation_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False, unique=True),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column("started_at", sa.String(32)),
        sa.Column("completed_at", sa.String(32)),
        sa.Column(
            "state",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "reconciliation_required",
                name="agent_workspace_creation_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "phase",
            sa.Enum(
                "queued",
                "capturing",
                "verifying_source",
                "publishing_baseline",
                "creating_tree",
                "verifying_tree",
                "finalize",
                name="agent_workspace_creation_phase",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("failure_code", sa.String(128)),
        sa.Column("lease_token_digest", sa.String(64)),
        sa.Column("lease_expires_at", sa.String(32)),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["agent_workspaces.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["development_source_id"], ["development_sources.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128",
            name="agent_workspace_creation_failure_bounded",
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest)=64 AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="agent_workspace_creation_lease_digest",
        ),
        sa.CheckConstraint(
            "(state IN ('queued','running') AND completed_at IS NULL AND failure_code IS NULL) OR (state='succeeded' AND completed_at IS NOT NULL AND failure_code IS NULL) OR (state IN ('failed','reconciliation_required') AND completed_at IS NOT NULL AND failure_code IS NOT NULL)",
            name="agent_workspace_creation_terminal_consistent",
        ),
    )
    op.create_index(
        "ix_agent_workspace_creation_source_requested",
        "agent_workspace_creation_operations",
        ["development_source_id", "requested_at"],
    )


def downgrade() -> None:
    populated = op.get_bind().scalar(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM agent_workspaces) + "
            "(SELECT COUNT(*) FROM agent_workspace_creation_operations)"
        )
    )
    if populated:
        raise RuntimeError(
            "Cannot downgrade populated agent workspace state or history."
        )
    op.drop_table("agent_workspace_creation_operations")
    op.drop_index("ix_agent_workspace_state", table_name="agent_workspaces")
    op.drop_index("ix_agent_workspace_source_created", table_name="agent_workspaces")
    op.drop_table("agent_workspaces")
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.create_check_constraint(
            "global_heavy_operation_kind", _heavy_constraint(include_workspace=False)
        )
