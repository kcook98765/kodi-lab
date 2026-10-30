"""Truthful managed verification receipts without native mutation markers."""

import sqlalchemy as sa
from alembic import op

revision = "0090_verified_noop_deployment"
down_revision = "0089_retained_development"
branch_labels = None
depends_on = None

_TABLE = "mcp_workspace_deployment_operations"
_OLD_SUCCESS = (
    "state<>'succeeded' OR (inventory_classification='matched' AND "
    "addon_enabled IS NOT NULL AND result_classification IS NOT NULL "
    "AND source_manifest_digest IS NOT NULL AND "
    "verified_destination_digest IS NOT NULL AND failure_code IS NULL "
    "AND ((deployment_mode='direct' AND sync_completed=1 AND "
    "target_started=1 AND (target_class='internal_lab' OR "
    "(bridge_receipt_id IS NOT NULL AND file_count IS NOT NULL AND "
    "total_bytes IS NOT NULL))) OR (deployment_mode='repository' AND "
    "target_class='external_development' AND addon_enabled=1 AND "
    "file_count IS NOT NULL AND total_bytes IS NOT NULL AND "
    "source_manifest_digest=verified_destination_digest)))"
)
_NEW_SUCCESS = (
    "state<>'succeeded' OR (inventory_classification='matched' AND "
    "addon_enabled IS NOT NULL AND result_classification IS NOT NULL "
    "AND source_manifest_digest IS NOT NULL AND "
    "verified_destination_digest IS NOT NULL AND failure_code IS NULL "
    "AND ((target_class='internal_lab' AND deployment_mode='direct' "
    "AND result_classification='verified_existing' AND "
    "managed_verification_json IS NOT NULL AND mutation_started=0 AND "
    "target_stopped=0 AND sync_started=0 AND sync_completed=0 AND "
    "target_started=0 AND addon_enabled=1) OR "
    "(deployment_mode='direct' AND sync_completed=1 AND "
    "target_started=1 AND (target_class='internal_lab' OR "
    "(bridge_receipt_id IS NOT NULL AND file_count IS NOT NULL AND "
    "total_bytes IS NOT NULL))) OR (deployment_mode='repository' AND "
    "target_class='external_development' AND addon_enabled=1 AND "
    "file_count IS NOT NULL AND total_bytes IS NOT NULL AND "
    "source_manifest_digest=verified_destination_digest)))"
)
_VERIFICATION = (
    "(managed_verification_json IS NULL OR "
    "(target_class='internal_lab' AND "
    "length(managed_verification_json) BETWEEN 2 AND 8192 AND "
    "json_valid(managed_verification_json))) AND "
    "(COALESCE(result_classification,'')<>'verified_existing' OR "
    "(managed_verification_json IS NOT NULL AND mutation_started=0 AND"
    " target_stopped=0 AND sync_started=0 AND sync_completed=0 AND "
    "target_started=0))"
)


def upgrade() -> None:
    with op.batch_alter_table(_TABLE, recreate="always") as batch:
        batch.add_column(
            sa.Column("managed_verification_json", sa.Text(), nullable=True)
        )
        batch.drop_constraint(
            "mcp_workspace_deployment_success_complete", type_="check"
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_success_complete", _NEW_SUCCESS
        )
        batch.create_check_constraint(
            "mcp_workspace_managed_verification", _VERIFICATION
        )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM "
            + _TABLE
            + " WHERE managed_verification_json IS NOT NULL"
        )
    ):
        raise RuntimeError("Managed verification history cannot be discarded")
    with op.batch_alter_table(_TABLE, recreate="always") as batch:
        batch.drop_constraint("mcp_workspace_managed_verification", type_="check")
        batch.drop_constraint(
            "mcp_workspace_deployment_success_complete", type_="check"
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_success_complete", _OLD_SUCCESS
        )
        batch.drop_column("managed_verification_json")
