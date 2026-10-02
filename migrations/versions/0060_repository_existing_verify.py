"""Distinguish Web verification of an existing repository installation.

Revision ID: 0060_repository_existing_verify
Revises: 0059_workspace_validation_digest
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0060_repository_existing_verify"
down_revision = "0059_workspace_validation_digest"
branch_labels = None
depends_on = None

_UUID_SHAPE = (
    "length(id)=36 AND (client_id IS NULL OR length(client_id)=36) "
    "AND length(request_id)=36 AND length(workspace_id)=36 "
    "AND length(development_source_id)=36 AND length(target_id)=36 AND "
    "(runtime_revision_id IS NULL OR length(runtime_revision_id)=36) AND "
    "(bridge_id IS NULL OR length(bridge_id)=36) AND "
    "(bridge_request_id IS NULL OR length(bridge_request_id)=36) AND "
    "(bridge_receipt_id IS NULL OR length(bridge_receipt_id)=36)"
)


def upgrade() -> None:
    with op.batch_alter_table("mcp_workspace_deployment_operations") as batch:
        batch.drop_constraint("mcp_workspace_deployment_uuid_lengths", type_="check")
        batch.alter_column("client_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(
            sa.Column(
                "initiator_kind", sa.String(8), nullable=False, server_default="mcp"
            )
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_uuid_lengths", _UUID_SHAPE
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_initiator_closed",
            "(initiator_kind='mcp' AND client_id IS NOT NULL) OR "
            "(initiator_kind='web' AND client_id IS NULL "
            "AND deployment_mode='repository')",
        )
    with op.batch_alter_table("repository_workspace_deployments") as batch:
        batch.add_column(
            sa.Column("origin", sa.String(24), nullable=False, server_default="planned")
        )
        batch.add_column(sa.Column("verified_by", sa.String(64)))
        batch.create_check_constraint(
            "repository_workspace_verification_origin_closed",
            "(origin='planned' AND verified_by IS NULL) OR "
            "(origin='verified_existing' AND verified_by='local_admin' "
            "AND state='succeeded' AND approved_at IS NULL "
            "AND observed_at IS NOT NULL AND verified_at IS NOT NULL)",
        )
        batch.create_index(
            "uq_repository_existing_target_publication",
            ["target_id", "publication_id"],
            unique=True,
            sqlite_where=sa.text("origin='verified_existing'"),
        )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM repository_workspace_deployments "
            "WHERE origin='verified_existing')"
        )
    ):
        raise RuntimeError("verified existing installation cannot be downgraded")
    with op.batch_alter_table("repository_workspace_deployments") as batch:
        batch.drop_index("uq_repository_existing_target_publication")
        batch.drop_constraint(
            "repository_workspace_verification_origin_closed", type_="check"
        )
        batch.drop_column("verified_by")
        batch.drop_column("origin")
    with op.batch_alter_table("mcp_workspace_deployment_operations") as batch:
        batch.drop_constraint(
            "mcp_workspace_deployment_initiator_closed", type_="check"
        )
        batch.drop_constraint("mcp_workspace_deployment_uuid_lengths", type_="check")
        batch.alter_column("client_id", existing_type=sa.String(36), nullable=False)
        batch.drop_column("initiator_kind")
        batch.create_check_constraint(
            "mcp_workspace_deployment_uuid_lengths",
            _UUID_SHAPE.replace(
                "(client_id IS NULL OR length(client_id)=36)", "length(client_id)=36"
            ),
        )
