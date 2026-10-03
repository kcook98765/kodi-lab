"""Persist bounded correlated addon exceptions on workspace smoke failures.

Revision ID: 0064_workspace_smoke_exception
Revises: 0063_managed_runtime_projection
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0064_workspace_smoke_exception"
down_revision = "0063_managed_runtime_projection"
branch_labels = None
depends_on = None

_TABLE = "mcp_workspace_execution_operations"
_SHAPE = (
    "(addon_exception_status IS NULL AND addon_exception_class IS NULL "
    "AND addon_exception_file IS NULL AND addon_exception_line IS NULL "
    "AND addon_exception_detail IS NULL) OR "
    "(addon_exception_status IN ('no_match','matched','rejected','unavailable') "
    "AND state='failed' AND COALESCE(failure_code,'')='execution_failed' "
    "AND execution_classification='jsonrpc_error' "
    "AND COALESCE(result_classification,'')='smoke_failed' "
    "AND kodi_error_code IS NOT NULL "
    "AND ((addon_exception_status='matched' "
    "AND addon_exception_class IS NOT NULL AND addon_exception_file IS NOT NULL "
    "AND addon_exception_line IS NOT NULL "
    "AND length(CAST(addon_exception_class AS BLOB)) BETWEEN 1 AND 64 "
    "AND instr(addon_exception_class,char(0))=0 "
    "AND addon_exception_class NOT GLOB '*[^A-Za-z0-9_]*' "
    "AND substr(addon_exception_class,1,1) GLOB '[A-Za-z_]' "
    "AND length(CAST(addon_exception_file AS BLOB)) BETWEEN 1 AND 256 "
    "AND instr(addon_exception_file,char(0))=0 "
    "AND addon_exception_file NOT GLOB '*[^A-Za-z0-9_./-]*' "
    "AND substr(addon_exception_file,1,1)<>'/' "
    "AND instr(addon_exception_file,'..')=0 "
    "AND instr(addon_exception_file,'//')=0 "
    "AND addon_exception_file NOT LIKE './%' "
    "AND addon_exception_file NOT LIKE '%/./%' "
    "AND substr(addon_exception_file,-3)='.py' "
    "AND addon_exception_line BETWEEN 1 AND 1000000 "
    "AND (addon_exception_detail IS NULL OR "
    "(length(CAST(addon_exception_detail AS BLOB)) BETWEEN 1 AND 128 "
    "AND instr(addon_exception_detail,char(0))=0))) OR "
    "(addon_exception_status<>'matched' "
    "AND addon_exception_class IS NULL AND addon_exception_file IS NULL "
    "AND addon_exception_line IS NULL AND addon_exception_detail IS NULL)))"
)


def upgrade() -> None:
    # Keep table identity: deployment and request history refer to this table.
    for name, kind in (
        ("addon_exception_status", sa.String(16)),
        ("addon_exception_class", sa.String(64)),
        ("addon_exception_file", sa.String(256)),
        ("addon_exception_line", sa.Integer()),
    ):
        op.add_column(_TABLE, sa.Column(name, kind, nullable=True))
    op.add_column(
        _TABLE,
        sa.Column(
            "addon_exception_detail",
            sa.String(128),
            sa.CheckConstraint(
                _SHAPE, name="mcp_workspace_execution_addon_exception_shape"
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            f"SELECT EXISTS(SELECT 1 FROM {_TABLE} WHERE "
            "addon_exception_status IS NOT NULL OR "
            "addon_exception_class IS NOT NULL OR "
            "addon_exception_file IS NOT NULL OR "
            "addon_exception_line IS NOT NULL OR "
            "addon_exception_detail IS NOT NULL)"
        )
    ):
        raise RuntimeError("persisted addon exception diagnostics cannot be downgraded")
    for name in (
        "addon_exception_detail",
        "addon_exception_line",
        "addon_exception_file",
        "addon_exception_class",
        "addon_exception_status",
    ):
        op.drop_column(_TABLE, name)
