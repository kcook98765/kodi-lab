"""Retain the exact tree digest of the latest workspace validation.

Revision ID: 0059_workspace_validation_digest
Revises: 0058_workspace_source_capture
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0059_workspace_validation_digest"
down_revision = "0058_workspace_source_capture"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mcp_workspace_validations") as batch:
        batch.add_column(sa.Column("tree_digest", sa.String(71)))
        batch.create_check_constraint(
            "mcp_workspace_validation_tree_digest_valid",
            "tree_digest IS NULL OR (length(tree_digest)=71 AND "
            "substr(tree_digest,1,7)='sha256:')",
        )
    op.execute(
        "UPDATE mcp_workspace_validations SET tree_digest=("
        "SELECT w.current_tree_digest FROM agent_workspaces w "
        "WHERE w.id=mcp_workspace_validations.workspace_id "
        "AND w.generation=mcp_workspace_validations.generation) "
        "WHERE EXISTS(SELECT 1 FROM agent_workspaces w "
        "WHERE w.id=mcp_workspace_validations.workspace_id "
        "AND w.generation=mcp_workspace_validations.generation "
        "AND w.current_tree_digest IS NOT NULL)"
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM mcp_workspace_validations v "
            "JOIN agent_workspaces w ON w.id=v.workspace_id "
            "WHERE v.tree_digest IS NOT NULL AND "
            "(v.generation<>w.generation OR v.tree_digest<>w.current_tree_digest))"
        )
    ):
        raise RuntimeError("refusing to discard historical validation tree digest")
    with op.batch_alter_table("mcp_workspace_validations") as batch:
        batch.drop_constraint(
            "mcp_workspace_validation_tree_digest_valid", type_="check"
        )
        batch.drop_column("tree_digest")
