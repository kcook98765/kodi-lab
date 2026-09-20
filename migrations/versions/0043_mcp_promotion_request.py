"""Add replay-safe MCP promotion request and Web review origin.

Revision ID: 0043_mcp_promotion_request
Revises: 0042_mcp_workspace_deployment_execution
"""

# Closed SQLite constraint literals are intentionally explicit.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_mcp_promotion_request"
down_revision: str | Sequence[str] | None = "0042_mcp_workspace_deployment_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADDON_DEVELOPMENT_PRESET_ID = "11000000-0000-4000-8000-000000000001"
FULL_LAB_CONTROL_PRESET_ID = "11000000-0000-4000-8000-000000000003"
PRESET_IDS = (ADDON_DEVELOPMENT_PRESET_ID, FULL_LAB_CONTROL_PRESET_ID)
PERMISSION = "development.request_promotion"


def upgrade() -> None:
    bind = op.get_bind()
    existing = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_preset_permissions "
            "WHERE permission='development.request_promotion'"
        )
    )
    if existing:
        raise RuntimeError("MCP Slice 5 preset state already exists.")
    permissions = sa.table(
        "mcp_preset_permissions",
        sa.column("preset_id", sa.String),
        sa.column("permission", sa.String),
    )
    op.bulk_insert(
        permissions,
        [
            {"preset_id": preset_id, "permission": PERMISSION}
            for preset_id in PRESET_IDS
        ],
    )

    with op.batch_alter_table("mcp_development_requests", recreate="always") as batch:
        batch.drop_constraint("mcp_development_request_operation_shape", type_="check")
        batch.drop_constraint("mcp_development_request_operation_closed", type_="check")
        batch.create_check_constraint(
            "mcp_development_request_operation_closed",
            "operation_kind IN ('workspace_create','workspace_deploy','workspace_execute','promotion_request')",
        )
        batch.create_check_constraint(
            "mcp_development_request_operation_shape",
            "(operation_kind = 'workspace_create' AND creation_operation_id IS NOT NULL AND operation_id IS NULL) OR (operation_kind IN ('workspace_deploy','workspace_execute','promotion_request') AND creation_operation_id IS NULL AND operation_id IS NOT NULL)",
        )

    with op.batch_alter_table(
        "agent_workspace_promotion_plans", recreate="always"
    ) as batch:
        batch.add_column(sa.Column("originating_mcp_client_id", sa.String(36)))
        batch.add_column(sa.Column("originating_mcp_request_id", sa.String(36)))
        batch.add_column(sa.Column("originating_mcp_deployment_id", sa.String(36)))
        batch.add_column(sa.Column("originating_mcp_execution_id", sa.String(36)))
        batch.create_check_constraint(
            "workspace_promotion_mcp_origin_complete",
            "(originating_mcp_client_id IS NULL AND originating_mcp_request_id IS NULL AND originating_mcp_deployment_id IS NULL AND originating_mcp_execution_id IS NULL) OR (length(originating_mcp_client_id)=36 AND length(originating_mcp_request_id)=36 AND length(originating_mcp_deployment_id)=36 AND length(originating_mcp_execution_id)=36)",
        )
        batch.create_foreign_key(
            "fk_workspace_promotion_mcp_client",
            "mcp_clients",
            ["originating_mcp_client_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_workspace_promotion_mcp_deployment",
            "mcp_workspace_deployment_operations",
            ["originating_mcp_deployment_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_workspace_promotion_mcp_execution",
            "mcp_workspace_execution_operations",
            ["originating_mcp_execution_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "uq_workspace_promotion_mcp_request",
        "agent_workspace_promotion_plans",
        ["originating_mcp_client_id", "originating_mcp_request_id"],
        unique=True,
        sqlite_where=sa.text("originating_mcp_client_id IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    populated = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM agent_workspace_promotion_plans "
            "WHERE originating_mcp_client_id IS NOT NULL"
        )
    )
    requests = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_development_requests "
            "WHERE operation_kind='promotion_request'"
        )
    )
    assigned = bind.scalar(
        sa.text("SELECT COUNT(*) FROM mcp_clients WHERE preset_id IN (:one,:two)"),
        {"one": PRESET_IDS[0], "two": PRESET_IDS[1]},
    )
    custom = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_preset_permissions p "
            "JOIN mcp_presets s ON s.id=p.preset_id WHERE s.builtin=0 "
            "AND p.permission='development.request_promotion'"
        )
    )
    if populated or requests or assigned or custom:
        raise RuntimeError("Cannot downgrade populated MCP Slice 5 state.")

    op.drop_index(
        "uq_workspace_promotion_mcp_request",
        table_name="agent_workspace_promotion_plans",
    )
    with op.batch_alter_table(
        "agent_workspace_promotion_plans", recreate="always"
    ) as batch:
        batch.drop_constraint(
            "fk_workspace_promotion_mcp_execution", type_="foreignkey"
        )
        batch.drop_constraint(
            "fk_workspace_promotion_mcp_deployment", type_="foreignkey"
        )
        batch.drop_constraint("fk_workspace_promotion_mcp_client", type_="foreignkey")
        batch.drop_constraint("workspace_promotion_mcp_origin_complete", type_="check")
        batch.drop_column("originating_mcp_execution_id")
        batch.drop_column("originating_mcp_deployment_id")
        batch.drop_column("originating_mcp_request_id")
        batch.drop_column("originating_mcp_client_id")

    with op.batch_alter_table("mcp_development_requests", recreate="always") as batch:
        batch.drop_constraint("mcp_development_request_operation_shape", type_="check")
        batch.drop_constraint("mcp_development_request_operation_closed", type_="check")
        batch.create_check_constraint(
            "mcp_development_request_operation_closed",
            "operation_kind IN ('workspace_create','workspace_deploy','workspace_execute')",
        )
        batch.create_check_constraint(
            "mcp_development_request_operation_shape",
            "(operation_kind = 'workspace_create' AND creation_operation_id IS NOT NULL AND operation_id IS NULL) OR (operation_kind IN ('workspace_deploy','workspace_execute') AND creation_operation_id IS NULL AND operation_id IS NOT NULL)",
        )
    bind.execute(
        sa.text(
            "DELETE FROM mcp_preset_permissions WHERE preset_id IN (:one,:two) "
            "AND permission='development.request_promotion'"
        ),
        {"one": PRESET_IDS[0], "two": PRESET_IDS[1]},
    )
