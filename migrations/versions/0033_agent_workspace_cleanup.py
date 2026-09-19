"""Add explicit agent workspace abandonment and durable byte cleanup.

Revision ID: 0033_agent_workspace_cleanup
Revises: 0032_promotion_verification
"""

# SQLite constraint expressions are intentionally exact SQL literals.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_agent_workspace_cleanup"
down_revision: str | Sequence[str] | None = "0032_promotion_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_workspaces", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "bytes_state",
                sa.Enum(
                    "present",
                    "cleaned",
                    name="agent_workspace_bytes_state",
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=False,
                server_default="present",
            )
        )
        batch.add_column(sa.Column("promoted_at", sa.String(32)))
        batch.add_column(sa.Column("abandoned_at", sa.String(32)))
        batch.add_column(sa.Column("cleaned_at", sa.String(32)))
        batch.create_check_constraint(
            "agent_workspace_bytes_state_consistent",
            "(bytes_state='present' AND cleaned_at IS NULL) OR (bytes_state='cleaned' AND cleaned_at IS NOT NULL)",
        )
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch:
        batch.drop_constraint("global_heavy_operation_kind", type_="check")
        batch.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore','snapshot_delete','instance_delete','development_source_deployment','addon_zip_import','addon_zip_installation','addon_repository_refresh','addon_repository_fetch','addon_repository_installation','repository_replacement','agent_workspace_creation','agent_workspace_promotion','promotion_verification','agent_workspace_cleanup')",
        )
    op.create_table(
        "agent_workspace_cleanup_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "reconciliation_required",
                name="agent_workspace_cleanup_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "phase",
            sa.Enum(
                "queued",
                "validating",
                "deleting",
                "verifying",
                "finalize",
                name="agent_workspace_cleanup_phase",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column(
            "deletion_started",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("expected_device", sa.BigInteger()),
        sa.Column("expected_inode", sa.BigInteger()),
        sa.Column("started_at", sa.String(32)),
        sa.Column("completed_at", sa.String(32)),
        sa.Column("failure_code", sa.String(128)),
        sa.Column("lease_token_digest", sa.String(64)),
        sa.Column("lease_expires_at", sa.String(32)),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["agent_workspaces.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "(expected_device IS NULL AND expected_inode IS NULL) OR (expected_device IS NOT NULL AND expected_inode IS NOT NULL)",
            name="agent_workspace_cleanup_identity_shape",
        ),
        sa.CheckConstraint(
            "deletion_started=0 OR expected_device IS NOT NULL",
            name="agent_workspace_cleanup_deletion_identity",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128",
            name="agent_workspace_cleanup_failure_bounded",
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest)=64 AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="agent_workspace_cleanup_lease_digest",
        ),
        sa.CheckConstraint(
            "(state='queued' AND started_at IS NULL AND completed_at IS NULL AND lease_token_digest IS NULL AND lease_expires_at IS NULL) OR (state='running' AND started_at IS NOT NULL AND completed_at IS NULL AND lease_token_digest IS NOT NULL AND lease_expires_at IS NOT NULL) OR (state IN ('succeeded','failed','reconciliation_required') AND completed_at IS NOT NULL AND lease_token_digest IS NULL AND lease_expires_at IS NULL)",
            name="agent_workspace_cleanup_state_fields",
        ),
    )
    op.create_index(
        "uq_agent_workspace_cleanup_active",
        "agent_workspace_cleanup_operations",
        ["workspace_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued','running')"),
    )
    op.create_table(
        "agent_workspace_lifecycle_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "abandoned",
                "cleanup_queued",
                "cleanup_succeeded",
                "cleanup_failed",
                "cleanup_reconciliation_required",
                name="agent_workspace_lifecycle_event_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.String(32), nullable=False),
        sa.Column("cleanup_operation_id", sa.String(36)),
        sa.Column("failure_code", sa.String(128)),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["agent_workspaces.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["cleanup_operation_id"],
            ["agent_workspace_cleanup_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128",
            name="agent_workspace_lifecycle_failure_bounded",
        ),
    )
    op.create_index(
        "ix_agent_workspace_lifecycle_occurred",
        "agent_workspace_lifecycle_events",
        ["workspace_id", "occurred_at"],
    )


def downgrade() -> None:
    populated = op.get_bind().scalar(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM agent_workspace_cleanup_operations) + (SELECT COUNT(*) FROM agent_workspace_lifecycle_events) + (SELECT COUNT(*) FROM agent_workspaces WHERE bytes_state='cleaned' OR promoted_at IS NOT NULL OR abandoned_at IS NOT NULL)"
        )
    )
    if populated:
        raise RuntimeError("Cannot downgrade populated agent workspace cleanup state.")
    op.drop_index(
        "ix_agent_workspace_lifecycle_occurred",
        table_name="agent_workspace_lifecycle_events",
    )
    op.drop_table("agent_workspace_lifecycle_events")
    op.drop_index(
        "uq_agent_workspace_cleanup_active",
        table_name="agent_workspace_cleanup_operations",
    )
    op.drop_table("agent_workspace_cleanup_operations")
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch:
        batch.drop_constraint("global_heavy_operation_kind", type_="check")
        batch.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore','snapshot_delete','instance_delete','development_source_deployment','addon_zip_import','addon_zip_installation','addon_repository_refresh','addon_repository_fetch','addon_repository_installation','repository_replacement','agent_workspace_creation','agent_workspace_promotion','promotion_verification')",
        )
    with op.batch_alter_table("agent_workspaces", recreate="always") as batch:
        batch.drop_constraint("agent_workspace_bytes_state_consistent", type_="check")
        batch.drop_constraint("agent_workspace_bytes_state", type_="check")
        for column in ("cleaned_at", "abandoned_at", "promoted_at", "bytes_state"):
            batch.drop_column(column)
