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
    op.add_column(
        "mcp_workspace_deployment_operations",
        sa.Column("repository_origin", sa.String(24)),
    )
    op.execute(
        "UPDATE mcp_workspace_deployment_operations "
        "SET repository_origin='planned' WHERE deployment_mode='repository'"
    )
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
            "(initiator_kind='mcp' AND client_id IS NOT NULL AND "
            "((deployment_mode='direct' AND repository_origin IS NULL) OR "
            "(deployment_mode='repository' AND repository_origin IS NOT NULL "
            "AND repository_origin='planned'))) OR "
            "(initiator_kind='web' AND client_id IS NULL "
            "AND deployment_mode='repository' "
            "AND repository_origin IS NOT NULL "
            "AND repository_origin='verified_existing' AND state='succeeded' "
            "AND phase='finalize' AND result_classification IS NOT NULL "
            "AND result_classification='repository_verified' "
            "AND mutation_started=0 AND target_stopped=0 AND sync_started=0 "
            "AND sync_completed=0 AND target_started=0)",
        )
        batch.create_unique_constraint(
            "uq_mcp_workspace_deployment_origin", ["id", "repository_origin"]
        )
    with op.batch_alter_table("repository_workspace_deployments") as batch:
        batch.add_column(
            sa.Column("origin", sa.String(24), nullable=False, server_default="planned")
        )
        batch.add_column(sa.Column("verified_by", sa.String(64)))
        batch.add_column(sa.Column("identity_sha256", sa.String(64)))
        batch.add_column(sa.Column("publication_source_id", sa.String(36)))
        batch.create_check_constraint(
            "repository_workspace_verification_origin_closed",
            "(origin='planned' AND verified_by IS NULL "
            "AND identity_sha256 IS NULL "
            "AND publication_source_id IS NULL) OR "
            "(origin='verified_existing' AND verified_by IS NOT NULL "
            "AND verified_by='local_admin' "
            "AND state='succeeded' AND approved_at IS NULL "
            "AND observed_at IS NOT NULL AND verified_at IS NOT NULL "
            "AND failure_code IS NULL AND identity_sha256 IS NOT NULL "
            "AND length(identity_sha256)=64 "
            "AND identity_sha256 NOT GLOB '*[^0-9a-f]*' "
            "AND publication_source_id IS NOT NULL "
            "AND length(publication_source_id)=36)",
        )
        batch.create_foreign_key(
            "fk_repository_workspace_operation_origin",
            "mcp_workspace_deployment_operations",
            ["operation_id", "origin"],
            ["id", "repository_origin"],
            ondelete="RESTRICT",
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
        batch.drop_constraint(
            "fk_repository_workspace_operation_origin", type_="foreignkey"
        )
        batch.drop_index("uq_repository_existing_target_publication")
        batch.drop_constraint(
            "repository_workspace_verification_origin_closed", type_="check"
        )
        batch.drop_column("verified_by")
        batch.drop_column("identity_sha256")
        batch.drop_column("publication_source_id")
        batch.drop_column("origin")
    with op.batch_alter_table("mcp_workspace_deployment_operations") as batch:
        batch.drop_constraint("uq_mcp_workspace_deployment_origin", type_="unique")
        batch.drop_constraint(
            "mcp_workspace_deployment_initiator_closed", type_="check"
        )
        batch.drop_constraint("mcp_workspace_deployment_uuid_lengths", type_="check")
        batch.alter_column("client_id", existing_type=sa.String(36), nullable=False)
        batch.drop_column("initiator_kind")
        batch.drop_column("repository_origin")
        batch.create_check_constraint(
            "mcp_workspace_deployment_uuid_lengths",
            _UUID_SHAPE.replace(
                "(client_id IS NULL OR length(client_id)=36)", "length(client_id)=36"
            ),
        )
