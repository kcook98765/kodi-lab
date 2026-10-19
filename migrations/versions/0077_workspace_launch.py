"""Separate managed GUI launch intent and typed coordination."""

import sqlalchemy as sa
from alembic import op

revision = "0077_workspace_launch"
down_revision = "0076_managed_resource_policy"
branch_labels = None
depends_on = None

_OLD_KINDS = (
    "'reset','snapshot_create','snapshot_restore','lifecycle',"
    "'fixture_assignment','runtime_reassignment','instance_delete',"
    "'development_source_attachment','development_source_deployment',"
    "'development_source_enablement','development_source_smoke_test',"
    "'addon_zip_installation','addon_zip_enablement','addon_zip_smoke_test',"
    "'addon_repository_attachment','addon_repository_installation',"
    "'repository_replacement','agent_workspace_promotion',"
    "'promotion_verification','mcp_workspace_deployment',"
    "'mcp_workspace_execution','workspace_execution_recovery'"
)


def upgrade() -> None:
    op.add_column(
        "managed_bridge_trust",
        sa.Column(
            "reviewed_installation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch:
        batch.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch.create_check_constraint(
            "managed_instance_mutation_kind",
            f"operation_kind IN ({_OLD_KINDS},'mcp_workspace_launch')",
        )
    op.create_table(
        "mcp_workspace_launches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("deployment_id", sa.String(36), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("bridge_generation", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("runtime_state_revision", sa.Integer(), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("addon_version", sa.String(128), nullable=False),
        sa.Column("tree_digest", sa.String(71), nullable=False),
        sa.Column("container_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("requested_at", sa.String(27), nullable=False),
        sa.Column("started_at", sa.String(27), nullable=True),
        sa.Column("completed_at", sa.String(27), nullable=True),
        sa.UniqueConstraint("client_id", "request_id", name="mcp_launch_owner_request"),
        sa.CheckConstraint(
            "generation>=0 AND ordinal BETWEEN 0 AND 63 AND runtime_state_revision>=0",
            name="mcp_launch_generation",
        ),
        sa.CheckConstraint(
            "state IN ('pending','dispatching','accepted','failed',"
            "'ambiguous','contained')",
            name="mcp_launch_state",
        ),
        sa.CheckConstraint(
            "length(container_id)=64 AND length(tree_digest) BETWEEN 64 AND 71 "
            "AND length(addon_id) BETWEEN 1 AND 128 "
            "AND length(addon_version) BETWEEN 1 AND 128",
            name="mcp_launch_identity",
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM managed_bridge_trust WHERE reviewed_installation<>0"
        )
    ):
        raise RuntimeError("Reviewed managed Bridge provenance must be preserved")
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM mcp_workspace_launches")
    ) or op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM managed_instance_mutation_leases "
            "WHERE operation_kind='mcp_workspace_launch'"
        )
    ):
        raise RuntimeError("Managed launch history must be preserved")
    op.drop_table("mcp_workspace_launches")
    op.drop_column("managed_bridge_trust", "reviewed_installation")
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch:
        batch.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch.create_check_constraint(
            "managed_instance_mutation_kind", f"operation_kind IN ({_OLD_KINDS})"
        )
