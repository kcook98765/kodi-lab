"""Add replay-safe MCP workspace creation and editing permission.

Revision ID: 0040_mcp_workspace_mutation
Revises: 0039_mcp_development_inspection
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_mcp_workspace_mutation"
down_revision: str | Sequence[str] | None = "0039_mcp_development_inspection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADDON_DEVELOPMENT_PRESET_ID = "11000000-0000-4000-8000-000000000001"
FULL_LAB_CONTROL_PRESET_ID = "11000000-0000-4000-8000-000000000003"
EDIT_PRESET_IDS = (ADDON_DEVELOPMENT_PRESET_ID, FULL_LAB_CONTROL_PRESET_ID)


def upgrade() -> None:
    bind = op.get_bind()
    existing = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_preset_permissions "
            "WHERE permission = 'workspace.edit'"
        )
    )
    if existing:
        raise RuntimeError("MCP workspace.edit preset state already exists.")
    permissions = sa.table(
        "mcp_preset_permissions",
        sa.column("preset_id", sa.String),
        sa.column("permission", sa.String),
    )
    op.bulk_insert(
        permissions,
        [
            {"preset_id": preset_id, "permission": "workspace.edit"}
            for preset_id in EDIT_PRESET_IDS
        ],
    )

    op.create_table(
        "mcp_development_requests",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("operation_kind", sa.String(32), nullable=False),
        sa.Column("parameter_fingerprint", sa.String(64), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("creation_operation_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("updated_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(id) = 36 AND length(client_id) = 36 AND "
            "length(request_id) = 36 AND length(development_source_id) = 36 AND "
            "length(workspace_id) = 36 AND length(creation_operation_id) = 36",
            name="mcp_development_request_uuid_lengths",
        ),
        sa.CheckConstraint(
            "operation_kind = 'workspace_create'",
            name="mcp_development_request_operation_closed",
        ),
        sa.CheckConstraint(
            "state = 'allocated'",
            name="mcp_development_request_state_closed",
        ),
        sa.CheckConstraint(
            "length(parameter_fingerprint) = 64 AND "
            "parameter_fingerprint = lower(parameter_fingerprint)",
            name="mcp_development_request_fingerprint_sha256",
        ),
        sa.CheckConstraint(
            "length(created_at) = 27 AND length(updated_at) = 27",
            name="mcp_development_request_timestamp_format",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["mcp_clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["development_source_id"],
            ["development_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["agent_workspaces.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["creation_operation_id"],
            ["agent_workspace_creation_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "client_id",
            "request_id",
            "operation_kind",
            name="uq_mcp_development_request_client_request_kind",
        ),
    )
    op.create_index(
        "ix_mcp_development_request_client_created",
        "mcp_development_requests",
        ["client_id", "created_at"],
    )
    op.create_index(
        "ix_mcp_development_request_created",
        "mcp_development_requests",
        ["created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    requests = bind.scalar(sa.text("SELECT COUNT(*) FROM mcp_development_requests"))
    created_bindings = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_agent_workspace_bindings "
            "WHERE binding_origin = 'mcp_created'"
        )
    )
    parameters = {
        f"preset_{index}": value for index, value in enumerate(EDIT_PRESET_IDS)
    }
    placeholders = ", ".join(
        f":preset_{index}" for index in range(len(EDIT_PRESET_IDS))
    )
    assigned_clients = bind.scalar(
        sa.text(
            f"SELECT COUNT(*) FROM mcp_clients WHERE preset_id IN ({placeholders})"
        ),
        parameters,
    )
    custom_edit = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_preset_permissions p "
            "JOIN mcp_presets s ON s.id = p.preset_id "
            "WHERE p.permission = 'workspace.edit' AND s.builtin = 0"
        )
    )
    if requests or created_bindings or assigned_clients or custom_edit:
        raise RuntimeError("Cannot downgrade populated MCP workspace mutation state.")
    op.drop_index(
        "ix_mcp_development_request_created", table_name="mcp_development_requests"
    )
    op.drop_index(
        "ix_mcp_development_request_client_created",
        table_name="mcp_development_requests",
    )
    op.drop_table("mcp_development_requests")
    bind.execute(
        sa.text(
            "DELETE FROM mcp_preset_permissions "
            "WHERE permission = 'workspace.edit' "
            f"AND preset_id IN ({placeholders})"
        ),
        parameters,
    )
