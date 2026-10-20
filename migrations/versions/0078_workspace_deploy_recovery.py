"""Retain failed deployment phase, runtime binding and verified containment."""

import sqlalchemy as sa
from alembic import op

revision = "0078_workspace_deploy_recovery"
down_revision = "0077_workspace_launch"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("original_container_id", sa.String(64)),
    ("original_runtime_state_revision", sa.Integer()),
    ("failure_phase", sa.String(32)),
    ("failure_cause", sa.String(64)),
    ("recovery_state", sa.String(32)),
    ("recovery_digest", sa.String(71)),
    ("recovery_at", sa.String(27)),
)


def upgrade() -> None:
    for name, kind in _COLUMNS:
        op.add_column(
            "mcp_workspace_deployment_operations", sa.Column(name, kind, nullable=True)
        )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM mcp_workspace_deployment_operations WHERE "
            + " OR ".join(f"{name} IS NOT NULL" for name, _kind in _COLUMNS)
        )
    ):
        raise RuntimeError("Workspace deployment recovery evidence must be preserved")
    with op.batch_alter_table("mcp_workspace_deployment_operations") as batch:
        for name, _kind in reversed(_COLUMNS):
            batch.drop_column(name)
