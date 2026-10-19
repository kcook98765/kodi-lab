"""Durable receipts for reviewed registered-source batch imports."""

import sqlalchemy as sa
from alembic import op

revision = "0073_workspace_imports"
down_revision = "0072_kodi_log_capability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_import_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "client_id",
            sa.String(36),
            sa.ForeignKey("mcp_clients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("source_digest", sa.String(71), nullable=False),
        sa.Column("preview_digest", sa.String(64), nullable=False),
        sa.Column("prior_digest", sa.String(71), nullable=False),
        sa.Column("result_digest", sa.String(71)),
        sa.Column("files_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.UniqueConstraint(
            "client_id", "request_id", name="uq_workspace_import_client_request"
        ),
        sa.CheckConstraint(
            "state IN ('applying','succeeded','failed','reconciliation_required')",
            name="workspace_import_state_closed",
        ),
        sa.CheckConstraint(
            "generation >= 0 AND length(files_json) <= 49152",
            name="workspace_import_bounds",
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM workspace_import_operations")
    ):
        raise RuntimeError("Workspace import evidence cannot be safely downgraded")
    op.drop_table("workspace_import_operations")
