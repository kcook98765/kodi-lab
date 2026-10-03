"""Retain exact runtime evidence and separate workspace smoke recovery history.

Revision ID: 0065_workspace_execution_recovery
Revises: 0064_workspace_smoke_exception
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0065_workspace_execution_recovery"
down_revision = "0064_workspace_smoke_exception"
branch_labels = None
depends_on = None

_MUTATION_KINDS = (
    "'reset','snapshot_create','snapshot_restore','lifecycle',"
    "'fixture_assignment','runtime_reassignment','instance_delete',"
    "'development_source_attachment','development_source_deployment',"
    "'development_source_enablement','development_source_smoke_test',"
    "'addon_zip_installation','addon_zip_enablement','addon_zip_smoke_test',"
    "'addon_repository_attachment','addon_repository_installation',"
    "'repository_replacement','agent_workspace_promotion',"
    "'promotion_verification','mcp_workspace_deployment',"
    "'mcp_workspace_execution'"
)
_EXECUTION_KIND = (
    "execution_kind IS NULL OR execution_kind IN "
    "('smoke','kodi_rpc_negative_diagnostic')"
)
_ORIGINAL_IDENTITY = (
    "(original_container_id IS NULL AND "
    "original_runtime_state_revision IS NULL) OR "
    "(original_container_id IS NOT NULL AND "
    "original_runtime_state_revision IS NOT NULL AND "
    "COALESCE(execution_kind,'')='smoke' AND invocation_dispatched=1 AND "
    "length(original_container_id)=64 AND "
    "original_container_id NOT GLOB '*[^0-9a-f]*' AND "
    "original_runtime_state_revision>=0)"
)


def upgrade() -> None:
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch:
        batch.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch.create_check_constraint(
            "managed_instance_mutation_kind",
            f"operation_kind IN ({_MUTATION_KINDS},'workspace_execution_recovery')",
        )
    op.add_column(
        "mcp_workspace_execution_operations",
        sa.Column(
            "execution_kind",
            sa.String(32),
            sa.CheckConstraint(
                _EXECUTION_KIND, name="mcp_workspace_execution_kind_closed"
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "mcp_workspace_execution_operations",
        sa.Column("original_container_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "mcp_workspace_execution_operations",
        sa.Column(
            "original_runtime_state_revision",
            sa.Integer(),
            sa.CheckConstraint(
                _ORIGINAL_IDENTITY,
                name="mcp_workspace_execution_recovery_identity_shape",
            ),
            nullable=True,
        ),
    )
    op.create_table(
        "workspace_execution_recovery_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column(
            "execution_operation_id",
            sa.String(36),
            sa.ForeignKey("mcp_workspace_execution_operations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("owned_runtime_state_revision", sa.Integer(), nullable=False),
        sa.Column("containment_confirmed", sa.Boolean(), nullable=False),
        sa.Column("readiness_confirmed", sa.Boolean(), nullable=False),
        sa.Column("provenance_confirmed", sa.Boolean(), nullable=False),
        sa.Column("requested_at", sa.String(27), nullable=False),
        sa.Column("completed_at", sa.String(27), nullable=True),
        sa.UniqueConstraint(
            "client_id", "request_id", name="uq_workspace_execution_recovery_request"
        ),
        sa.UniqueConstraint(
            "execution_operation_id", name="uq_workspace_execution_recovery_execution"
        ),
        sa.CheckConstraint(
            "length(id)=36 AND length(client_id)=36 AND length(request_id)=36 "
            "AND length(execution_operation_id)=36 AND owned_runtime_state_revision>=0",
            name="workspace_execution_recovery_identity_shape",
        ),
        sa.CheckConstraint(
            "state IN ('running','completed') AND "
            "phase IN ('reserved','stopping','contained','starting',"
            "'verifying','finalized') AND "
            "((state='running' AND outcome IS NULL AND completed_at IS NULL) OR "
            "(state='completed' AND phase='finalized' AND completed_at IS NOT NULL "
            "AND outcome IN ('recovered','contained_but_not_ready',"
            "'still_requires_admin')))",
            name="workspace_execution_recovery_state_closed",
        ),
        sa.CheckConstraint(
            "outcome<>'recovered' OR "
            "(containment_confirmed=1 AND readiness_confirmed=1 "
            "AND provenance_confirmed=1)",
            name="workspace_execution_recovery_proof_required",
        ),
        sa.CheckConstraint(
            "outcome<>'contained_but_not_ready' OR containment_confirmed=1",
            name="workspace_execution_recovery_containment_required",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM workspace_execution_recovery_operations)")
    ):
        raise RuntimeError(
            "persisted workspace execution recovery cannot be downgraded"
        )
    if bind.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM mcp_workspace_execution_operations "
            "WHERE original_container_id IS NOT NULL OR "
            "original_runtime_state_revision IS NOT NULL OR execution_kind IS NOT NULL)"
        )
    ):
        raise RuntimeError("persisted original runtime identity cannot be downgraded")
    op.drop_table("workspace_execution_recovery_operations")
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch:
        batch.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch.create_check_constraint(
            "managed_instance_mutation_kind", f"operation_kind IN ({_MUTATION_KINDS})"
        )
    for column in (
        "original_runtime_state_revision",
        "original_container_id",
        "execution_kind",
    ):
        op.drop_column("mcp_workspace_execution_operations", column)
