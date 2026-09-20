"""Add MCP development-source and workspace inspection authority.

Revision ID: 0039_mcp_development_inspection
Revises: 0038_mcp_remote_control
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_mcp_development_inspection"
down_revision: str | Sequence[str] | None = "0038_mcp_remote_control"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADDON_DEVELOPMENT_PRESET_ID = "11000000-0000-4000-8000-000000000001"
FULL_LAB_CONTROL_PRESET_ID = "11000000-0000-4000-8000-000000000003"
WORKSPACE_PRESET_IDS = (ADDON_DEVELOPMENT_PRESET_ID, FULL_LAB_CONTROL_PRESET_ID)


def upgrade() -> None:
    bind = op.get_bind()
    existing = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_preset_permissions "
            "WHERE permission = 'workspace.inspect'"
        )
    )
    if existing:
        raise RuntimeError("MCP workspace.inspect preset state already exists.")
    permissions = sa.table(
        "mcp_preset_permissions",
        sa.column("preset_id", sa.String),
        sa.column("permission", sa.String),
    )
    op.bulk_insert(
        permissions,
        [
            {"preset_id": preset_id, "permission": "workspace.inspect"}
            for preset_id in WORKSPACE_PRESET_IDS
        ],
    )

    # This composite key lets SQLite enforce that a binding's source is the
    # workspace's actual durable source rather than merely a caller assertion.
    with op.batch_alter_table("agent_workspaces") as batch:
        batch.create_unique_constraint(
            "uq_agent_workspace_id_source", ["id", "development_source_id"]
        )
    op.create_table(
        "mcp_client_development_source_grants",
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(created_at) = 27",
            name="mcp_source_grant_timestamp_format",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["mcp_clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["development_source_id"],
            ["development_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("client_id", "development_source_id"),
    )
    op.create_index(
        "ix_mcp_source_grant_source",
        "mcp_client_development_source_grants",
        ["development_source_id"],
    )
    op.create_table(
        "mcp_agent_workspace_bindings",
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("binding_origin", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "binding_origin IN ('web_admin','mcp_created')",
            name="mcp_workspace_binding_origin_closed",
        ),
        sa.CheckConstraint(
            "length(created_at) = 27",
            name="mcp_workspace_binding_timestamp_format",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["mcp_clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["development_source_id"],
            ["development_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "development_source_id"],
            ["agent_workspaces.id", "agent_workspaces.development_source_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id"),
    )
    op.create_index(
        "ix_mcp_workspace_binding_client_source",
        "mcp_agent_workspace_bindings",
        ["client_id", "development_source_id", "workspace_id"],
    )
    op.add_column(
        "mcp_security_events",
        sa.Column("development_source_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "mcp_security_events",
        sa.Column("workspace_id", sa.String(36), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    grants = bind.scalar(
        sa.text("SELECT COUNT(*) FROM mcp_client_development_source_grants")
    )
    bindings = bind.scalar(sa.text("SELECT COUNT(*) FROM mcp_agent_workspace_bindings"))
    parameters = {
        f"preset_{index}": value for index, value in enumerate(WORKSPACE_PRESET_IDS)
    }
    placeholders = ", ".join(
        f":preset_{index}" for index in range(len(WORKSPACE_PRESET_IDS))
    )
    assigned_clients = bind.scalar(
        sa.text(
            f"SELECT COUNT(*) FROM mcp_clients WHERE preset_id IN ({placeholders})"
        ),
        parameters,
    )
    if grants or bindings or assigned_clients:
        raise RuntimeError(
            "Cannot downgrade populated MCP development inspection authority."
        )
    with op.batch_alter_table("mcp_security_events") as batch:
        batch.drop_column("workspace_id")
        batch.drop_column("development_source_id")
    op.drop_index(
        "ix_mcp_workspace_binding_client_source",
        table_name="mcp_agent_workspace_bindings",
    )
    op.drop_table("mcp_agent_workspace_bindings")
    op.drop_index(
        "ix_mcp_source_grant_source",
        table_name="mcp_client_development_source_grants",
    )
    op.drop_table("mcp_client_development_source_grants")
    with op.batch_alter_table("agent_workspaces") as batch:
        batch.drop_constraint("uq_agent_workspace_id_source", type_="unique")
    bind.execute(
        sa.text(
            "DELETE FROM mcp_preset_permissions "
            "WHERE permission = 'workspace.inspect' "
            f"AND preset_id IN ({placeholders})"
        ),
        parameters,
    )
