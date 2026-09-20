"""Add the MCP read-only visual-observation permission.

Revision ID: 0037_mcp_observation_permissions
Revises: 0036_mcp_core_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_mcp_observation_permissions"
down_revision: str | Sequence[str] | None = "0036_mcp_core_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADDON_DEVELOPMENT_PRESET_ID = "11000000-0000-4000-8000-000000000001"
READ_ONLY_INSPECTION_PRESET_ID = "11000000-0000-4000-8000-000000000002"
FULL_LAB_CONTROL_PRESET_ID = "11000000-0000-4000-8000-000000000003"
VISUAL_PRESET_IDS = (
    ADDON_DEVELOPMENT_PRESET_ID,
    READ_ONLY_INSPECTION_PRESET_ID,
    FULL_LAB_CONTROL_PRESET_ID,
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_preset_permissions "
            "WHERE permission = 'target.visual'"
        )
    )
    if existing:
        raise RuntimeError("MCP target.visual preset state already exists.")
    permissions = sa.table(
        "mcp_preset_permissions",
        sa.column("preset_id", sa.String),
        sa.column("permission", sa.String),
    )
    op.bulk_insert(
        permissions,
        [
            {"preset_id": preset_id, "permission": "target.visual"}
            for preset_id in VISUAL_PRESET_IDS
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    parameters = {
        f"preset_{index}": value for index, value in enumerate(VISUAL_PRESET_IDS)
    }
    placeholders = ", ".join(
        f":preset_{index}" for index in range(len(VISUAL_PRESET_IDS))
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
    if assigned_clients or assigned_targets:
        raise RuntimeError(
            "Cannot downgrade populated MCP observation authority without "
            "removing target.visual access from assigned clients or targets."
        )
    bind.execute(
        sa.text(
            "DELETE FROM mcp_preset_permissions "
            f"WHERE permission = 'target.visual' AND preset_id IN ({placeholders})"
        ),
        parameters,
    )
