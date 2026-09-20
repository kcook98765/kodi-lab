"""Add durable MCP-client disable timing for workspace retention.

Revision ID: 0041_mcp_workspace_validation_retention
Revises: 0040_mcp_workspace_mutation
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0041_mcp_workspace_validation_retention"
down_revision: str | Sequence[str] | None = "0040_mcp_workspace_mutation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("mcp_clients") as batch:
        batch.add_column(sa.Column("disabled_at", sa.String(27), nullable=True))
    bind = op.get_bind()
    migration_time = (
        datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    bind.execute(
        sa.text(
            "UPDATE mcp_clients SET disabled_at = :migration_time WHERE enabled = 0"
        ),
        {"migration_time": migration_time},
    )
    with op.batch_alter_table("mcp_clients") as batch:
        batch.create_check_constraint(
            "mcp_client_disabled_timestamp_consistent",
            "(enabled = 1 AND disabled_at IS NULL) OR "
            "(enabled = 0 AND disabled_at IS NOT NULL "
            "AND length(disabled_at) = 27)",
        )
    with op.batch_alter_table("mcp_security_events") as batch:
        batch.drop_constraint("mcp_security_event_actor_closed", type_="check")
        batch.create_check_constraint(
            "mcp_security_event_actor_closed",
            "actor IN ('web_admin','system')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    system_events = bind.scalar(
        sa.text("SELECT COUNT(*) FROM mcp_security_events WHERE actor = 'system'")
    )
    disabled = bind.scalar(
        sa.text("SELECT COUNT(*) FROM mcp_clients WHERE enabled = 0")
    )
    if system_events or disabled:
        raise RuntimeError("Cannot downgrade populated MCP workspace retention state.")
    with op.batch_alter_table("mcp_security_events") as batch:
        batch.drop_constraint("mcp_security_event_actor_closed", type_="check")
        batch.create_check_constraint(
            "mcp_security_event_actor_closed", "actor = 'web_admin'"
        )
    with op.batch_alter_table("mcp_clients") as batch:
        batch.drop_constraint("mcp_client_disabled_timestamp_consistent", type_="check")
        batch.drop_column("disabled_at")
