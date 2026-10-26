"""Typed managed add-on settings-write coordination; no values are persisted."""

import sqlalchemy as sa
from alembic import op

revision = "0086_addon_settings"
down_revision = "0085_session_source_copy_admissions"
branch_labels = None
depends_on = None

_OLD_KINDS = (
    "'reset','snapshot_create','snapshot_restore','lifecycle','fixture_assi"
    "gnment','runtime_reassignment','instance_delete','development_source_a"
    "ttachment','development_source_deployment','development_source_enablem"
    "ent','development_source_smoke_test','addon_zip_installation','addon_z"
    "ip_enablement','addon_zip_smoke_test','addon_repository_attachment','a"
    "ddon_repository_installation','repository_replacement','agent_workspac"
    "e_promotion','promotion_verification','mcp_workspace_deployment','mcp_"
    "workspace_execution','workspace_execution_recovery','mcp_workspace_lau"
    "nch'"
)


def upgrade() -> None:
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch:
        batch.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch.create_check_constraint(
            "managed_instance_mutation_kind",
            f"operation_kind IN ({_OLD_KINDS},'mcp_addon_settings','reconstruction')",
        )
    op.create_table(
        "mcp_addon_settings_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("deployment_id", sa.String(36), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("container_id", sa.String(64), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("addon_version", sa.String(128), nullable=False),
        sa.Column("tree_digest", sa.String(71), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("requested_at", sa.String(27), nullable=False),
        sa.Column("completed_at", sa.String(27), nullable=True),
        sa.Column("results_json", sa.String(8192), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("session_generation", sa.Integer(), nullable=False),
        sa.Column("runtime_state_revision", sa.Integer(), nullable=False),
        sa.Column("bridge_generation", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "client_id", "request_id", name="mcp_settings_owner_request"
        ),
        sa.CheckConstraint(
            "generation>=0 AND session_generation>0 AND runtime_state_revision"
            ">=0 AND bridge_generation>0",
            name="mcp_settings_generation",
        ),
        sa.CheckConstraint(
            "state IN ('pending','dispatching','completed','failed','ambiguous"
            "','contained')",
            name="mcp_settings_state",
        ),
        sa.CheckConstraint(
            "length(container_id)=64 AND container_id NOT GLOB '*[^0-9a-f]*' A"
            "ND length(tree_digest) BETWEEN 64 AND 71 AND length(addon_id) BET"
            "WEEN 1 AND 128 AND length(addon_version) BETWEEN 1 AND 128",
            name="mcp_settings_identity",
        ),
        sa.CheckConstraint(
            "results_json IS NULL OR length(results_json)<=8192",
            name="mcp_settings_result_bound",
        ),
    )
    op.create_index(
        "ix_mcp_settings_target_state",
        "mcp_addon_settings_operations",
        ["target_id", "state"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM mcp_addon_settings_operations")
    ) or op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM managed_instance_mutation_leases WHERE opera"
            "tion_kind IN ('mcp_addon_settings','reconstruction')"
        )
    ):
        raise RuntimeError("Managed settings history must be preserved")
    op.drop_table("mcp_addon_settings_operations")
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch:
        batch.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch.create_check_constraint(
            "managed_instance_mutation_kind", f"operation_kind IN ({_OLD_KINDS})"
        )
