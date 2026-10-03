"""Retain bounded Kodi errors on the existing workspace execution operation.

Revision ID: 0062_workspace_kodi_error
Revises: 0061_reset_phase_v5
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0062_workspace_kodi_error"
down_revision = "0061_reset_phase_v5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ADD COLUMN retains table identity for existing promotion foreign keys.
    op.add_column(
        "mcp_workspace_execution_operations",
        sa.Column(
            "kodi_error_code",
            sa.Integer(),
            sa.CheckConstraint(
                "kodi_error_code IS NULL OR "
                "(state='failed' AND execution_classification='jsonrpc_error' "
                "AND failure_code='execution_failed')",
                name="mcp_workspace_execution_kodi_error_failure_only",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "mcp_workspace_execution_operations",
        sa.Column(
            "kodi_error_message",
            sa.String(192),
            sa.CheckConstraint(
                "kodi_error_message IS NULL OR "
                "(kodi_error_code IS NOT NULL AND "
                "length(kodi_error_message) BETWEEN 1 AND 192)",
                name="mcp_workspace_execution_kodi_message_bounded",
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM mcp_workspace_execution_operations "
            "WHERE kodi_error_code IS NOT NULL OR kodi_error_message IS NOT NULL)"
        )
    ):
        raise RuntimeError("persisted Kodi execution diagnostics cannot be downgraded")
    op.drop_column("mcp_workspace_execution_operations", "kodi_error_message")
    op.drop_column("mcp_workspace_execution_operations", "kodi_error_code")
