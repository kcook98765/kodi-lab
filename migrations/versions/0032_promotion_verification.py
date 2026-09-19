"""Add promotion-bound selected-target verification.

Revision ID: 0032_promotion_verification
Revises: 0031_agent_workspace_promotions
"""

# SQLite constraint expressions are intentionally exact SQL literals.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_promotion_verification"
down_revision: str | Sequence[str] | None = "0031_agent_workspace_promotions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table(
        "agent_workspace_promotion_plans", recreate="always"
    ) as batch:
        batch.add_column(sa.Column("expected_enabled", sa.Boolean()))
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch:
        batch.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ('reset','snapshot_create','snapshot_restore','lifecycle','fixture_assignment','runtime_reassignment','instance_delete','development_source_attachment','development_source_deployment','development_source_enablement','development_source_smoke_test','addon_zip_installation','addon_zip_enablement','addon_zip_smoke_test','addon_repository_attachment','addon_repository_installation','repository_replacement','agent_workspace_promotion','promotion_verification')",
        )
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch:
        batch.drop_constraint("global_heavy_operation_kind", type_="check")
        batch.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore','snapshot_delete','instance_delete','development_source_deployment','addon_zip_import','addon_zip_installation','addon_repository_refresh','addon_repository_fetch','addon_repository_installation','repository_replacement','agent_workspace_creation','agent_workspace_promotion','promotion_verification')",
        )
    with op.batch_alter_table(
        "agent_workspace_promotion_handoffs", recreate="always"
    ) as batch:
        batch.add_column(sa.Column("expected_addon_id", sa.String(128)))
        batch.add_column(sa.Column("expected_name", sa.String(256)))
        batch.add_column(sa.Column("expected_version", sa.String(128)))
        batch.add_column(sa.Column("expected_enabled", sa.Boolean()))
        batch.add_column(sa.Column("verification_operation_id", sa.String(36)))
        batch.add_column(sa.Column("consumed_at", sa.String(32)))
        batch.create_unique_constraint(
            "uq_promotion_handoff_verification", ["verification_operation_id"]
        )
        batch.create_check_constraint(
            "promotion_handoff_verification_shape",
            "(verification_operation_id IS NULL AND consumed_at IS NULL) OR (verification_operation_id IS NOT NULL AND expected_addon_id IS NOT NULL AND expected_name IS NOT NULL AND expected_version IS NOT NULL AND expected_enabled IS NOT NULL)",
        )

    op.create_table(
        "promotion_verification_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_promotion_id", sa.String(36), nullable=False, unique=True),
        sa.Column("handoff_id", sa.String(36), nullable=False, unique=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("target_instance_id", sa.String(36), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column(
            "deployment_operation_id", sa.String(36), nullable=False, unique=True
        ),
        sa.Column(
            "state",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "incomplete",
                "reconciliation_required",
                name="promotion_verification_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "phase",
            sa.Enum(
                "queued",
                "preflight",
                "log_cursor",
                "stop",
                "sync",
                "start",
                "readiness",
                "inventory",
                "log_window",
                "finalize",
                name="promotion_verification_phase",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("expected_name", sa.String(256), nullable=False),
        sa.Column("expected_version", sa.String(128), nullable=False),
        sa.Column("expected_enabled", sa.Boolean(), nullable=False),
        sa.Column("sync_result", sa.String(23), nullable=False),
        sa.Column("reload_result", sa.String(23), nullable=False),
        sa.Column("inventory_result", sa.String(23), nullable=False),
        sa.Column("log_result", sa.String(18), nullable=False),
        sa.Column("current_version", sa.String(128)),
        sa.Column("current_enabled", sa.Boolean()),
        sa.Column(
            "target_error_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "unrelated_warning_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "stop_completed", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "sync_started", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "sync_completed", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "start_dispatched",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "handoff_consumed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column("started_at", sa.String(32)),
        sa.Column("completed_at", sa.String(32)),
        sa.Column("failure_code", sa.String(128)),
        sa.Column("lease_token_digest", sa.String(64)),
        sa.Column("lease_expires_at", sa.String(32)),
        sa.ForeignKeyConstraint(
            ["source_promotion_id"],
            ["agent_workspace_promotion_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["handoff_id"],
            ["agent_workspace_promotion_handoffs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["agent_workspaces.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["development_source_id"], ["development_sources.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["target_instance_id"], ["managed_instances.target_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["runtime_revision_id"], ["runtime_revisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["deployment_operation_id"],
            ["development_source_deployment_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "sync_result IN ('pending','succeeded','failed','incomplete','reconciliation_required') AND reload_result IN ('pending','succeeded','failed','incomplete','reconciliation_required') AND inventory_result IN ('pending','succeeded','failed','incomplete','reconciliation_required')",
            name="promotion_verification_stage_results",
        ),
        sa.CheckConstraint(
            "log_result IN ('pending','clean','target_errors','unrelated_warnings','incomplete')",
            name="promotion_verification_log_result",
        ),
        sa.CheckConstraint(
            "target_error_count BETWEEN 0 AND 1000000 AND unrelated_warning_count BETWEEN 0 AND 1000000",
            name="promotion_verification_log_bounds",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128",
            name="promotion_verification_failure_bounded",
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest)=64 AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="promotion_verification_lease_digest",
        ),
        sa.CheckConstraint(
            "(state='queued' AND started_at IS NULL AND completed_at IS NULL AND lease_token_digest IS NULL AND lease_expires_at IS NULL) OR (state='running' AND started_at IS NOT NULL AND completed_at IS NULL AND lease_token_digest IS NOT NULL AND lease_expires_at IS NOT NULL) OR (state IN ('succeeded','failed','incomplete','reconciliation_required') AND completed_at IS NOT NULL AND lease_token_digest IS NULL AND lease_expires_at IS NULL)",
            name="promotion_verification_state_fields",
        ),
        sa.CheckConstraint(
            "sync_completed=0 OR sync_started=1",
            name="promotion_verification_sync_order",
        ),
        sa.CheckConstraint(
            "handoff_consumed=sync_completed",
            name="promotion_verification_consumption_order",
        ),
        sa.CheckConstraint(
            "start_dispatched=0 OR sync_completed=1",
            name="promotion_verification_start_order",
        ),
    )
    op.create_table(
        "promotion_verification_log_evidence",
        sa.Column("verification_operation_id", sa.String(36), primary_key=True),
        sa.Column(
            "target_error_snippets_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.ForeignKeyConstraint(
            ["verification_operation_id"],
            ["promotion_verification_operations.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "length(target_error_snippets_json) <= 12288",
            name="promotion_verification_private_log_bounds",
        ),
    )


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM promotion_verification_operations")
    )
    if count:
        raise RuntimeError("Cannot downgrade populated promotion verification state.")
    op.drop_table("promotion_verification_log_evidence")
    op.drop_table("promotion_verification_operations")
    with op.batch_alter_table(
        "agent_workspace_promotion_plans", recreate="always"
    ) as batch:
        batch.drop_column("expected_enabled")
    with op.batch_alter_table(
        "agent_workspace_promotion_handoffs", recreate="always"
    ) as batch:
        batch.drop_constraint("promotion_handoff_verification_shape", type_="check")
        batch.drop_constraint("uq_promotion_handoff_verification", type_="unique")
        for column in (
            "consumed_at",
            "verification_operation_id",
            "expected_enabled",
            "expected_version",
            "expected_name",
            "expected_addon_id",
        ):
            batch.drop_column(column)
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch:
        batch.drop_constraint("global_heavy_operation_kind", type_="check")
        batch.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore','snapshot_delete','instance_delete','development_source_deployment','addon_zip_import','addon_zip_installation','addon_repository_refresh','addon_repository_fetch','addon_repository_installation','repository_replacement','agent_workspace_creation','agent_workspace_promotion')",
        )
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch:
        batch.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ('reset','snapshot_create','snapshot_restore','lifecycle','fixture_assignment','runtime_reassignment','instance_delete','development_source_attachment','development_source_deployment','development_source_enablement','development_source_smoke_test','addon_zip_installation','addon_zip_enablement','addon_zip_smoke_test','addon_repository_attachment','addon_repository_installation','repository_replacement','agent_workspace_promotion')",
        )
