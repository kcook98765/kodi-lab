"""Add the authenticated MCP core authority model.

Revision ID: 0036_mcp_core_foundation
Revises: 0035_managed_screenshots
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_mcp_core_foundation"
down_revision: str | Sequence[str] | None = "0035_managed_screenshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADDON_DEVELOPMENT_PRESET_ID = "11000000-0000-4000-8000-000000000001"
READ_ONLY_INSPECTION_PRESET_ID = "11000000-0000-4000-8000-000000000002"
FULL_LAB_CONTROL_PRESET_ID = "11000000-0000-4000-8000-000000000003"
PRODUCTION_REMOTE_KODI_PRESET_ID = "11000000-0000-4000-8000-000000000004"


def upgrade() -> None:
    op.create_table(
        "mcp_presets",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("builtin", sa.Boolean(), nullable=False),
        sa.Column("assignable", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "length(display_name) BETWEEN 1 AND 120 "
            "AND display_name = trim(display_name)",
            name="mcp_preset_display_name_bounded",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("display_name", name="uq_mcp_preset_display_name"),
    )
    op.create_table(
        "mcp_preset_permissions",
        sa.Column("preset_id", sa.String(36), nullable=False),
        sa.Column("permission", sa.String(128), nullable=False),
        sa.CheckConstraint(
            "length(permission) BETWEEN 1 AND 128 "
            "AND permission = lower(permission) "
            "AND permission = trim(permission)",
            name="mcp_preset_permission_bounded",
        ),
        sa.ForeignKeyConstraint(["preset_id"], ["mcp_presets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("preset_id", "permission"),
    )
    op.create_table(
        "mcp_clients",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("preset_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("last_used_at", sa.String(27), nullable=True),
        sa.CheckConstraint(
            "length(display_name) BETWEEN 1 AND 120 "
            "AND display_name = trim(display_name)",
            name="mcp_client_display_name_bounded",
        ),
        sa.CheckConstraint(
            "length(created_at) = 27 "
            "AND (last_used_at IS NULL OR length(last_used_at) = 27)",
            name="mcp_client_timestamps_format",
        ),
        sa.ForeignKeyConstraint(["preset_id"], ["mcp_presets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mcp_client_preset", "mcp_clients", ["preset_id"])
    op.create_table(
        "mcp_credentials",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("expires_at", sa.String(27), nullable=True),
        sa.Column("revoked_at", sa.String(27), nullable=True),
        sa.Column("last_used_at", sa.String(27), nullable=True),
        sa.CheckConstraint(
            "length(token_digest) = 64 AND token_digest NOT GLOB '*[^0-9a-f]*'",
            name="mcp_credential_digest_sha256",
        ),
        sa.CheckConstraint(
            "length(created_at) = 27 "
            "AND (expires_at IS NULL OR length(expires_at) = 27) "
            "AND (revoked_at IS NULL OR length(revoked_at) = 27) "
            "AND (last_used_at IS NULL OR length(last_used_at) = 27)",
            name="mcp_credential_timestamps_format",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name="mcp_credential_expiry_after_creation",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["mcp_clients.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_mcp_credential_token_digest"),
    )
    op.create_index("ix_mcp_credential_client", "mcp_credentials", ["client_id"])
    op.create_table(
        "mcp_client_target_grants",
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(created_at) = 27",
            name="mcp_client_target_grant_timestamp_format",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["mcp_clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["target_id"], ["managed_instances.target_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("client_id", "target_id"),
    )
    op.create_index("ix_mcp_grant_target", "mcp_client_target_grants", ["target_id"])
    op.create_table(
        "mcp_target_preset_assignments",
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("preset_id", sa.String(36), nullable=False),
        sa.Column("assigned_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(assigned_at) = 27",
            name="mcp_target_preset_assignment_timestamp_format",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["managed_instances.target_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["preset_id"], ["mcp_presets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("target_id"),
    )
    op.create_index(
        "ix_mcp_target_assignment_preset",
        "mcp_target_preset_assignments",
        ["preset_id"],
    )
    op.create_table(
        "mcp_security_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("event_kind", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("client_id", sa.String(36), nullable=True),
        sa.Column("credential_id", sa.String(36), nullable=True),
        sa.Column("target_id", sa.String(36), nullable=True),
        sa.Column("preset_id", sa.String(36), nullable=True),
        sa.Column("occurred_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(event_kind) BETWEEN 1 AND 64 "
            "AND event_kind = lower(event_kind) "
            "AND event_kind = trim(event_kind)",
            name="mcp_security_event_kind_bounded",
        ),
        sa.CheckConstraint(
            "length(occurred_at) = 27", name="mcp_security_event_timestamp_format"
        ),
        sa.CheckConstraint(
            "actor = 'web_admin'", name="mcp_security_event_actor_closed"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mcp_security_event_occurred",
        "mcp_security_events",
        ["occurred_at", "id"],
    )

    presets = sa.table(
        "mcp_presets",
        sa.column("id", sa.String),
        sa.column("display_name", sa.String),
        sa.column("builtin", sa.Boolean),
        sa.column("assignable", sa.Boolean),
    )
    op.bulk_insert(
        presets,
        [
            {
                "id": ADDON_DEVELOPMENT_PRESET_ID,
                "display_name": "Add-on Development",
                "builtin": True,
                "assignable": True,
            },
            {
                "id": READ_ONLY_INSPECTION_PRESET_ID,
                "display_name": "Read-only Inspection",
                "builtin": True,
                "assignable": True,
            },
            {
                "id": FULL_LAB_CONTROL_PRESET_ID,
                "display_name": "Full Lab Control",
                "builtin": True,
                "assignable": True,
            },
            {
                "id": PRODUCTION_REMOTE_KODI_PRESET_ID,
                "display_name": "Production Remote Kodi",
                "builtin": True,
                "assignable": False,
            },
        ],
    )
    permissions = sa.table(
        "mcp_preset_permissions",
        sa.column("preset_id", sa.String),
        sa.column("permission", sa.String),
    )
    op.bulk_insert(
        permissions,
        [
            {"preset_id": ADDON_DEVELOPMENT_PRESET_ID, "permission": "target.inspect"},
            {
                "preset_id": READ_ONLY_INSPECTION_PRESET_ID,
                "permission": "target.inspect",
            },
            {"preset_id": FULL_LAB_CONTROL_PRESET_ID, "permission": "target.inspect"},
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    protected_tables = (
        "mcp_clients",
        "mcp_credentials",
        "mcp_client_target_grants",
        "mcp_target_preset_assignments",
        "mcp_security_events",
    )
    for table_name in protected_tables:
        count = bind.scalar(sa.text(f"SELECT COUNT(*) FROM {table_name}"))
        if count:
            raise RuntimeError(
                "Cannot downgrade populated MCP foundation metadata without "
                "discarding authorization or audit state."
            )
    op.drop_index("ix_mcp_security_event_occurred", table_name="mcp_security_events")
    op.drop_table("mcp_security_events")
    op.drop_index(
        "ix_mcp_target_assignment_preset",
        table_name="mcp_target_preset_assignments",
    )
    op.drop_table("mcp_target_preset_assignments")
    op.drop_index("ix_mcp_grant_target", table_name="mcp_client_target_grants")
    op.drop_table("mcp_client_target_grants")
    op.drop_index("ix_mcp_credential_client", table_name="mcp_credentials")
    op.drop_table("mcp_credentials")
    op.drop_index("ix_mcp_client_preset", table_name="mcp_clients")
    op.drop_table("mcp_clients")
    op.drop_table("mcp_preset_permissions")
    op.drop_table("mcp_presets")
