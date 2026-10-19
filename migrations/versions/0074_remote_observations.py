"""Managed post-action UI evidence and exact capture bindings."""

import sqlalchemy as sa
from alembic import op

revision = "0074_remote_observations"
down_revision = "0073_workspace_imports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_remote_observations",
        sa.Column(
            "receipt_id",
            sa.String(36),
            sa.ForeignKey("mcp_remote_action_receipts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ui_status", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.String(27)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("ui_json", sa.Text()),
        sa.Column("runtime_revision_id", sa.String(36)),
        sa.Column("runtime_state_revision", sa.Integer()),
        sa.Column("container_id", sa.String(64)),
        sa.Column("capture_id", sa.String(36)),
        sa.Column("captured_at", sa.String(27)),
        sa.CheckConstraint(
            "ui_status IN ('pending','succeeded','failed')",
            name="mcp_remote_observation_state",
        ),
        sa.CheckConstraint(
            "ui_json IS NULL OR length(ui_json) <= 16384",
            name="mcp_remote_observation_bounded",
        ),
        sa.CheckConstraint(
            "ui_status <> 'succeeded' OR "
            "(ui_json IS NOT NULL AND observed_at IS NOT NULL)",
            name="mcp_remote_observation_complete",
        ),
        sa.CheckConstraint(
            "capture_id IS NULL OR (captured_at IS NOT NULL AND "
            "runtime_revision_id IS NOT NULL AND "
            "runtime_state_revision IS NOT NULL AND container_id IS NOT NULL)",
            name="mcp_remote_observation_capture_bound",
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM mcp_remote_observations")):
        raise RuntimeError("Remote observation evidence cannot be safely downgraded")
    op.drop_table("mcp_remote_observations")
