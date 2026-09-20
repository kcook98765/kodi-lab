"""Add replay-safe MCP semantic remote control.

Revision ID: 0038_mcp_remote_control
Revises: 0037_mcp_observation_permissions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_mcp_remote_control"
down_revision: str | Sequence[str] | None = "0037_mcp_observation_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADDON_DEVELOPMENT_PRESET_ID = "11000000-0000-4000-8000-000000000001"
FULL_LAB_CONTROL_PRESET_ID = "11000000-0000-4000-8000-000000000003"
CONTROL_PRESET_IDS = (ADDON_DEVELOPMENT_PRESET_ID, FULL_LAB_CONTROL_PRESET_ID)


def upgrade() -> None:
    bind = op.get_bind()
    existing = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_preset_permissions "
            "WHERE permission = 'target.control'"
        )
    )
    if existing:
        raise RuntimeError("MCP target.control preset state already exists.")
    permissions = sa.table(
        "mcp_preset_permissions",
        sa.column("preset_id", sa.String),
        sa.column("permission", sa.String),
    )
    op.bulk_insert(
        permissions,
        [
            {"preset_id": preset_id, "permission": "target.control"}
            for preset_id in CONTROL_PRESET_IDS
        ],
    )
    op.create_table(
        "mcp_remote_action_receipts",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("credential_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("final_result", sa.String(64), nullable=True),
        sa.Column("completed_at", sa.String(27), nullable=True),
        sa.Column("screenshot_refresh_result", sa.String(64), nullable=True),
        sa.Column("screenshot_capture_id", sa.String(36), nullable=True),
        sa.Column("screenshot_captured_at", sa.String(27), nullable=True),
        sa.CheckConstraint(
            "length(client_id) = 36 AND length(credential_id) = 36 "
            "AND length(target_id) = 36 AND length(request_id) = 36",
            name="mcp_remote_receipt_uuid_lengths",
        ),
        sa.CheckConstraint(
            "action IN ('up','down','left','right','select','back','home',"
            "'context_menu','info','play_pause','stop','previous','next',"
            "'volume_up','volume_down','mute_toggle')",
            name="mcp_remote_receipt_action_closed",
        ),
        sa.CheckConstraint(
            "state IN ('pending','completed','ambiguous')",
            name="mcp_remote_receipt_state_closed",
        ),
        sa.CheckConstraint(
            "length(created_at) = 27 AND "
            "(completed_at IS NULL OR length(completed_at) = 27)",
            name="mcp_remote_receipt_timestamp_format",
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND final_result IS NULL AND completed_at IS NULL "
            "AND screenshot_refresh_result IS NULL) OR "
            "(state IN ('completed','ambiguous') AND final_result IS NOT NULL "
            "AND completed_at IS NOT NULL AND screenshot_refresh_result IS NOT NULL)",
            name="mcp_remote_receipt_terminal_consistent",
        ),
        sa.CheckConstraint(
            "final_result IS NULL OR (length(final_result) BETWEEN 1 AND 64 "
            "AND final_result = lower(final_result) "
            "AND final_result = trim(final_result))",
            name="mcp_remote_receipt_result_bounded",
        ),
        sa.CheckConstraint(
            "screenshot_refresh_result IS NULL OR "
            "(length(screenshot_refresh_result) BETWEEN 1 AND 64 "
            "AND screenshot_refresh_result = lower(screenshot_refresh_result) "
            "AND screenshot_refresh_result = trim(screenshot_refresh_result))",
            name="mcp_remote_receipt_refresh_bounded",
        ),
        sa.CheckConstraint(
            "(screenshot_capture_id IS NULL AND screenshot_captured_at IS NULL) OR "
            "(length(screenshot_capture_id) = 36 "
            "AND length(screenshot_captured_at) = 27)",
            name="mcp_remote_receipt_refresh_metadata_paired",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "client_id", "request_id", name="uq_mcp_remote_receipt_client_request"
        ),
    )
    op.create_index(
        "ix_mcp_remote_receipt_client_created",
        "mcp_remote_action_receipts",
        ["client_id", "created_at"],
    )
    op.create_index(
        "ix_mcp_remote_receipt_created",
        "mcp_remote_action_receipts",
        ["created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    receipt_count = bind.scalar(
        sa.text("SELECT COUNT(*) FROM mcp_remote_action_receipts")
    )
    parameters = {
        f"preset_{index}": value for index, value in enumerate(CONTROL_PRESET_IDS)
    }
    placeholders = ", ".join(
        f":preset_{index}" for index in range(len(CONTROL_PRESET_IDS))
    )
    assigned_clients = bind.scalar(
        sa.text(
            f"SELECT COUNT(*) FROM mcp_clients WHERE preset_id IN ({placeholders})"
        ),
        parameters,
    )
    assigned_targets = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_target_preset_assignments "
            f"WHERE preset_id IN ({placeholders})"
        ),
        parameters,
    )
    if receipt_count or assigned_clients or assigned_targets:
        raise RuntimeError(
            "Cannot downgrade populated MCP remote-control authority or receipts."
        )
    op.drop_index(
        "ix_mcp_remote_receipt_created", table_name="mcp_remote_action_receipts"
    )
    op.drop_index(
        "ix_mcp_remote_receipt_client_created",
        table_name="mcp_remote_action_receipts",
    )
    op.drop_table("mcp_remote_action_receipts")
    bind.execute(
        sa.text(
            "DELETE FROM mcp_preset_permissions "
            "WHERE permission = 'target.control' "
            f"AND preset_id IN ({placeholders})"
        ),
        parameters,
    )
