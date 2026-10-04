"""Add exact stopped-mismatch reconstruction history and mutation kind.

Revision ID: 0067_managed_runtime_reconstruction
Revises: 0066_historical_workspace_execution_containment
"""

import sqlalchemy as sa
from alembic import op

revision = "0067_managed_runtime_reconstruction"
down_revision = "0066_historical_workspace_execution_containment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch:
        batch.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ("
            "'reset','reconstruction','snapshot_create','snapshot_restore','lifecycle',"
            "'fixture_assignment','runtime_reassignment','instance_delete',"
            "'development_source_attachment','development_source_deployment',"
            "'development_source_enablement','development_source_smoke_test',"
            "'addon_zip_installation','addon_zip_enablement','addon_zip_smoke_test',"
            "'addon_repository_attachment','addon_repository_installation',"
            "'repository_replacement','agent_workspace_promotion',"
            "'promotion_verification','mcp_workspace_deployment',"
            "'mcp_workspace_execution','workspace_execution_recovery')",
        )
    op.create_table(
        "managed_runtime_reconstruction_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("managed_instance_id", sa.String(36), nullable=False),
        sa.Column("old_container_id", sa.String(64), nullable=False),
        sa.Column("old_image_id", sa.String(71), nullable=False),
        sa.Column("old_runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("desired_runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("desired_image_id", sa.String(71), nullable=False),
        sa.Column("confirmed_runtime_state_revision", sa.Integer(), nullable=False),
        sa.Column("owned_runtime_state_revision", sa.Integer(), nullable=False),
        sa.Column("fresh_container_id", sa.String(64), nullable=True),
        sa.Column("old_discarded", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column("started_at", sa.String(32), nullable=True),
        sa.Column("heartbeat_at", sa.String(32), nullable=True),
        sa.Column("completed_at", sa.String(32), nullable=True),
        sa.Column("lease_token_digest", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.String(32), nullable=True),
        sa.Column("failure_reason", sa.String(256), nullable=True),
        sa.CheckConstraint(
            "state IN ('queued','running','succeeded','failed','resolved',"
            "'reconciliation_required')",
            name="runtime_reconstruction_state_closed",
        ),
        sa.CheckConstraint(
            "phase IN ('confirmed','preflight','removing_old','old_absence_proven',"
            "'provisioning','fresh_stopped_verified','applying_fixture','succeeded')",
            name="runtime_reconstruction_phase_closed",
        ),
        sa.CheckConstraint(
            "length(old_container_id)=64 AND old_container_id NOT GLOB '*[^0-9a-f]*' "
            "AND (fresh_container_id IS NULL OR (length(fresh_container_id)=64 "
            "AND fresh_container_id NOT GLOB '*[^0-9a-f]*'))",
            name="runtime_reconstruction_container_shape",
        ),
        sa.CheckConstraint(
            "length(old_image_id)=71 AND old_image_id LIKE 'sha256:%' "
            "AND substr(old_image_id,8) NOT GLOB '*[^0-9a-f]*' "
            "AND length(desired_image_id)=71 "
            "AND desired_image_id LIKE 'sha256:%' "
            "AND substr(desired_image_id,8) NOT GLOB '*[^0-9a-f]*'",
            name="runtime_reconstruction_image_shape",
        ),
        sa.CheckConstraint(
            "old_runtime_revision_id<>desired_runtime_revision_id "
            "AND confirmed_runtime_state_revision>=0 "
            "AND owned_runtime_state_revision>=0",
            name="runtime_reconstruction_mismatch_required",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="runtime_reconstruction_failure_bounded",
        ),
    )
    op.create_index(
        "uq_runtime_reconstruction_active_instance",
        "managed_runtime_reconstruction_operations",
        ["managed_instance_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued','running')"),
    )
    op.create_index(
        "ix_runtime_reconstruction_instance_requested",
        "managed_runtime_reconstruction_operations",
        ["managed_instance_id", "requested_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM managed_runtime_reconstruction_operations)"
        )
    ):
        raise RuntimeError("persisted reconstruction history cannot be downgraded")
    op.drop_table("managed_runtime_reconstruction_operations")
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch:
        batch.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ("
            "'reset','snapshot_create','snapshot_restore','lifecycle',"
            "'fixture_assignment','runtime_reassignment','instance_delete',"
            "'development_source_attachment','development_source_deployment',"
            "'development_source_enablement','development_source_smoke_test',"
            "'addon_zip_installation','addon_zip_enablement','addon_zip_smoke_test',"
            "'addon_repository_attachment','addon_repository_installation',"
            "'repository_replacement','agent_workspace_promotion',"
            "'promotion_verification','mcp_workspace_deployment',"
            "'mcp_workspace_execution','workspace_execution_recovery')",
        )
