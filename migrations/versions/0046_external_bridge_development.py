"""Add bridge-backed external workspace deployment evidence.

Revision ID: 0046_external_bridge_development
Revises: 0045_external_bridge_pairing
"""

# Closed SQLite constraints are intentionally explicit and auditable.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_external_bridge_development"
down_revision: str | Sequence[str] | None = "0045_external_bridge_pairing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("external_bridge_pairings", recreate="always") as batch:
        batch.drop_constraint("external_bridge_capabilities_closed", type_="check")
        batch.create_check_constraint(
            "external_bridge_capabilities_closed",
            "observed_capabilities_json IN ('[]','[\"screenshot\"]','[\"workspace_deployment\"]','[\"screenshot\",\"workspace_deployment\"]') AND verified_capabilities_json IN ('[]','[\"screenshot\"]','[\"workspace_deployment\"]','[\"screenshot\",\"workspace_deployment\"]')",
        )

    with op.batch_alter_table("external_bridge_events", recreate="always") as batch:
        batch.drop_constraint("external_bridge_event_kind_closed", type_="check")
        batch.create_check_constraint(
            "external_bridge_event_kind_closed",
            "event_kind IN ('pairing_token_generated','pairing_attempted','bridge_paired','pairing_failed','bridge_revoked','bridge_reverified','bridge_reverification_required','screenshot_requested','screenshot_completed','screenshot_failed','baseline_established','baseline_invalidated','deployment_requested','deployment_completed','deployment_failed','deployment_reconciled','execution_completed','execution_failed')",
        )

    with op.batch_alter_table(
        "mcp_workspace_deployment_operations", recreate="always"
    ) as batch:
        batch.drop_constraint("mcp_workspace_deployment_uuid_lengths", type_="check")
        batch.drop_constraint("mcp_workspace_deployment_phase_closed", type_="check")
        batch.drop_constraint(
            "mcp_workspace_deployment_success_complete", type_="check"
        )
        batch.alter_column(
            "runtime_revision_id", existing_type=sa.String(36), nullable=True
        )
        batch.add_column(
            sa.Column(
                "target_class",
                sa.String(32),
                nullable=False,
                server_default="internal_lab",
            )
        )
        batch.add_column(sa.Column("bridge_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("bridge_request_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("bridge_receipt_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("file_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("total_bytes", sa.BigInteger(), nullable=True))
        batch.create_check_constraint(
            "mcp_workspace_deployment_uuid_lengths",
            "length(id)=36 AND length(client_id)=36 AND length(request_id)=36 AND length(workspace_id)=36 AND length(development_source_id)=36 AND length(target_id)=36 AND (runtime_revision_id IS NULL OR length(runtime_revision_id)=36) AND (bridge_id IS NULL OR length(bridge_id)=36) AND (bridge_request_id IS NULL OR length(bridge_request_id)=36) AND (bridge_receipt_id IS NULL OR length(bridge_receipt_id)=36)",
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_target_class_closed",
            "target_class IN ('internal_lab','external_development')",
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_target_shape",
            "(target_class='internal_lab' AND runtime_revision_id IS NOT NULL AND bridge_id IS NULL AND bridge_request_id IS NULL AND bridge_receipt_id IS NULL) OR (target_class='external_development' AND runtime_revision_id IS NULL AND bridge_id IS NOT NULL AND bridge_request_id IS NOT NULL)",
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_phase_closed",
            "phase IN ('claimed','preflight','stop','sync','transfer','replace','receipt','start','readiness','inventory','finalize')",
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_counts_bounded",
            "(file_count IS NULL AND total_bytes IS NULL) OR (file_count BETWEEN 1 AND 25000 AND total_bytes BETWEEN 1 AND 1073741824)",
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_success_complete",
            "state<>'succeeded' OR (sync_completed=1 AND target_started=1 AND inventory_classification='matched' AND addon_enabled IS NOT NULL AND result_classification IS NOT NULL AND source_manifest_digest IS NOT NULL AND verified_destination_digest IS NOT NULL AND failure_code IS NULL AND (target_class='internal_lab' OR (bridge_receipt_id IS NOT NULL AND file_count IS NOT NULL AND total_bytes IS NOT NULL)))",
        )

    with op.batch_alter_table(
        "agent_workspace_promotion_plans", recreate="always"
    ) as batch:
        batch.drop_constraint("workspace_promotion_identity_bounds", type_="check")
        batch.alter_column(
            "target_runtime_revision_id", existing_type=sa.String(36), nullable=True
        )
        batch.alter_column(
            "target_container_id", existing_type=sa.String(64), nullable=True
        )
        batch.alter_column(
            "target_deployment_operation_id",
            existing_type=sa.String(36),
            nullable=True,
        )
        batch.add_column(
            sa.Column(
                "target_class",
                sa.String(32),
                nullable=False,
                server_default="internal_lab",
            )
        )
        batch.add_column(sa.Column("target_bridge_id", sa.String(36), nullable=True))
        batch.create_check_constraint(
            "workspace_promotion_identity_bounds",
            "length(addon_id) BETWEEN 1 AND 128 AND length(current_version) BETWEEN 1 AND 128 AND length(promoted_name) BETWEEN 1 AND 256 AND length(promoted_version) BETWEEN 1 AND 128 AND (target_container_id IS NULL OR length(target_container_id)=64)",
        )
        batch.create_check_constraint(
            "workspace_promotion_target_class_closed",
            "target_class IN ('internal_lab','external_development')",
        )
        batch.create_check_constraint(
            "workspace_promotion_target_shape",
            "(target_class='internal_lab' AND target_runtime_revision_id IS NOT NULL AND target_container_id IS NOT NULL AND target_deployment_operation_id IS NOT NULL AND target_bridge_id IS NULL) OR (target_class='external_development' AND target_runtime_revision_id IS NULL AND target_container_id IS NULL AND target_deployment_operation_id IS NULL AND length(target_bridge_id)=36)",
        )

    op.create_table(
        "external_development_source_baselines",
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("bridge_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("addon_version", sa.String(128), nullable=False),
        sa.Column("manifest_digest", sa.String(71), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("established_at", sa.String(27), nullable=False),
        sa.Column("checked_at", sa.String(27), nullable=False),
        sa.Column("invalidated_at", sa.String(27)),
        sa.CheckConstraint(
            "length(target_id)=36 AND length(development_source_id)=36 AND length(bridge_id)=36",
            name="external_source_baseline_uuid_lengths",
        ),
        sa.CheckConstraint(
            "state IN ('current','stale','unknown')",
            name="external_source_baseline_state_closed",
        ),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND length(addon_version) BETWEEN 1 AND 128",
            name="external_source_baseline_identity_bounded",
        ),
        sa.CheckConstraint(
            "length(manifest_digest)=71 AND substr(manifest_digest,1,7)='sha256:'",
            name="external_source_baseline_digest_valid",
        ),
        sa.CheckConstraint(
            "file_count BETWEEN 1 AND 25000 AND total_bytes BETWEEN 1 AND 1073741824",
            name="external_source_baseline_counts_bounded",
        ),
        sa.CheckConstraint(
            "length(established_at)=27 AND length(checked_at)=27 AND (invalidated_at IS NULL OR length(invalidated_at)=27)",
            name="external_source_baseline_timestamps_format",
        ),
        sa.CheckConstraint(
            "(state='current' AND invalidated_at IS NULL) OR (state<>'current' AND invalidated_at IS NOT NULL)",
            name="external_source_baseline_state_time_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["external_bridge_pairings.target_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["development_source_id"], ["development_sources.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("target_id", "development_source_id"),
    )
    op.create_index(
        "ix_external_source_baseline_state",
        "external_development_source_baselines",
        ["target_id", "state"],
    )

    op.create_table(
        "external_bridge_deployment_receipts",
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("bridge_id", sa.String(36), nullable=False),
        sa.Column("bridge_request_id", sa.String(36), nullable=False),
        sa.Column("bridge_receipt_id", sa.String(36)),
        sa.Column("expected_manifest_digest", sa.String(71), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("classification", sa.String(64)),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("completed_at", sa.String(27)),
        sa.Column("reconciled_at", sa.String(27)),
        sa.CheckConstraint(
            "length(operation_id)=36 AND length(target_id)=36 AND length(bridge_id)=36 AND length(bridge_request_id)=36 AND (bridge_receipt_id IS NULL OR length(bridge_receipt_id)=36)",
            name="external_bridge_deployment_receipt_uuid_lengths",
        ),
        sa.CheckConstraint(
            "state IN ('pending','succeeded','failed','ambiguous')",
            name="external_bridge_deployment_receipt_state_closed",
        ),
        sa.CheckConstraint(
            "length(expected_manifest_digest)=71 AND substr(expected_manifest_digest,1,7)='sha256:'",
            name="external_bridge_deployment_receipt_digest_valid",
        ),
        sa.CheckConstraint(
            "classification IS NULL OR length(classification) BETWEEN 1 AND 64",
            name="external_bridge_deployment_receipt_classification_bounded",
        ),
        sa.CheckConstraint(
            "file_count BETWEEN 1 AND 25000 AND total_bytes BETWEEN 1 AND 1073741824",
            name="external_bridge_deployment_receipt_counts_bounded",
        ),
        sa.CheckConstraint(
            "length(created_at)=27 AND (completed_at IS NULL OR length(completed_at)=27) AND (reconciled_at IS NULL OR length(reconciled_at)=27)",
            name="external_bridge_deployment_receipt_timestamps_format",
        ),
        sa.CheckConstraint(
            "(state='pending' AND completed_at IS NULL AND bridge_receipt_id IS NULL) OR (state IN ('succeeded','failed') AND completed_at IS NOT NULL AND bridge_receipt_id IS NOT NULL) OR state='ambiguous'",
            name="external_bridge_deployment_receipt_terminal_shape",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["mcp_workspace_deployment_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"], ["external_bridge_pairings.target_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("operation_id"),
        sa.UniqueConstraint(
            "target_id",
            "bridge_request_id",
            name="uq_external_bridge_deployment_request",
        ),
    )
    op.create_index(
        "ix_external_bridge_deployment_receipt_state",
        "external_bridge_deployment_receipts",
        ["target_id", "state", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    external_operations = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_workspace_deployment_operations "
            "WHERE target_class='external_development'"
        )
    )
    external_plans = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM agent_workspace_promotion_plans "
            "WHERE target_class='external_development'"
        )
    )
    populated = sum(
        int(bind.scalar(sa.text(f"SELECT COUNT(*) FROM {table}")) or 0)
        for table in (
            "external_development_source_baselines",
            "external_bridge_deployment_receipts",
        )
    )
    if external_operations or external_plans or populated:
        raise RuntimeError("Cannot downgrade populated external development state.")

    op.drop_index(
        "ix_external_bridge_deployment_receipt_state",
        table_name="external_bridge_deployment_receipts",
    )
    op.drop_table("external_bridge_deployment_receipts")
    op.drop_index(
        "ix_external_source_baseline_state",
        table_name="external_development_source_baselines",
    )
    op.drop_table("external_development_source_baselines")

    with op.batch_alter_table(
        "mcp_workspace_deployment_operations", recreate="always"
    ) as batch:
        batch.drop_constraint(
            "mcp_workspace_deployment_success_complete", type_="check"
        )
        batch.drop_constraint("mcp_workspace_deployment_counts_bounded", type_="check")
        batch.drop_constraint("mcp_workspace_deployment_phase_closed", type_="check")
        batch.drop_constraint("mcp_workspace_deployment_target_shape", type_="check")
        batch.drop_constraint(
            "mcp_workspace_deployment_target_class_closed", type_="check"
        )
        batch.drop_constraint("mcp_workspace_deployment_uuid_lengths", type_="check")
        batch.drop_column("total_bytes")
        batch.drop_column("file_count")
        batch.drop_column("bridge_receipt_id")
        batch.drop_column("bridge_request_id")
        batch.drop_column("bridge_id")
        batch.drop_column("target_class")
        batch.alter_column(
            "runtime_revision_id", existing_type=sa.String(36), nullable=False
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_uuid_lengths",
            "length(id)=36 AND length(client_id)=36 AND length(request_id)=36 AND length(workspace_id)=36 AND length(development_source_id)=36 AND length(target_id)=36 AND length(runtime_revision_id)=36",
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_phase_closed",
            "phase IN ('claimed','preflight','stop','sync','start','readiness','inventory','finalize')",
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_success_complete",
            "state<>'succeeded' OR (sync_completed=1 AND target_started=1 AND inventory_classification='matched' AND addon_enabled IS NOT NULL AND result_classification IS NOT NULL AND source_manifest_digest IS NOT NULL AND verified_destination_digest IS NOT NULL AND failure_code IS NULL)",
        )

    with op.batch_alter_table(
        "agent_workspace_promotion_plans", recreate="always"
    ) as batch:
        batch.drop_constraint("workspace_promotion_target_shape", type_="check")
        batch.drop_constraint("workspace_promotion_target_class_closed", type_="check")
        batch.drop_constraint("workspace_promotion_identity_bounds", type_="check")
        batch.drop_column("target_bridge_id")
        batch.drop_column("target_class")
        batch.alter_column(
            "target_deployment_operation_id",
            existing_type=sa.String(36),
            nullable=False,
        )
        batch.alter_column(
            "target_container_id", existing_type=sa.String(64), nullable=False
        )
        batch.alter_column(
            "target_runtime_revision_id", existing_type=sa.String(36), nullable=False
        )
        batch.create_check_constraint(
            "workspace_promotion_identity_bounds",
            "length(addon_id) BETWEEN 1 AND 128 AND length(current_version) BETWEEN 1 AND 128 AND length(promoted_name) BETWEEN 1 AND 256 AND length(promoted_version) BETWEEN 1 AND 128 AND length(target_container_id)=64",
        )

    with op.batch_alter_table("external_bridge_events", recreate="always") as batch:
        batch.drop_constraint("external_bridge_event_kind_closed", type_="check")
        batch.create_check_constraint(
            "external_bridge_event_kind_closed",
            "event_kind IN ('pairing_token_generated','pairing_attempted','bridge_paired','pairing_failed','bridge_revoked','bridge_reverified','bridge_reverification_required','screenshot_requested','screenshot_completed','screenshot_failed')",
        )
    with op.batch_alter_table("external_bridge_pairings", recreate="always") as batch:
        batch.drop_constraint("external_bridge_capabilities_closed", type_="check")
        batch.create_check_constraint(
            "external_bridge_capabilities_closed",
            "observed_capabilities_json IN ('[]','[\"screenshot\"]') AND verified_capabilities_json IN ('[]','[\"screenshot\"]')",
        )
