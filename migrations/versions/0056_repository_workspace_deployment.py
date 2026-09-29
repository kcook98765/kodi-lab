"""Durable repository-mode workspace deployment and target opt-in.

Revision ID: 0056_repository_workspace_deployment
Revises: 0055_development_repository_publications
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0056_repository_workspace_deployment"
down_revision = "0055_development_repository_publications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mcp_workspace_deployment_operations") as batch:
        batch.add_column(
            sa.Column(
                "deployment_mode",
                sa.String(16),
                nullable=False,
                server_default="direct",
            )
        )
        for name in (
            "mcp_workspace_deployment_target_shape",
            "mcp_workspace_deployment_phase_closed",
            "mcp_workspace_deployment_success_complete",
        ):
            batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(
            "mcp_workspace_deployment_mode_closed",
            "deployment_mode IN ('direct','repository')",
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_target_shape",
            "(target_class='internal_lab' AND deployment_mode='direct' "
            "AND runtime_revision_id IS NOT NULL AND bridge_id IS NULL "
            "AND bridge_request_id IS NULL AND bridge_receipt_id IS NULL) OR "
            "(target_class='external_development' AND runtime_revision_id IS NULL "
            "AND bridge_id IS NOT NULL AND ((deployment_mode='direct' "
            "AND bridge_request_id IS NOT NULL) OR (deployment_mode='repository' "
            "AND bridge_request_id IS NULL AND bridge_receipt_id IS NULL)))",
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_phase_closed",
            "phase IN ('claimed','preflight','stop','sync','transfer','replace',"
            "'receipt','start','readiness','inventory','finalize','package',"
            "'publication_approval','awaiting_kodi_update','verify_native',"
            "'verify_tree')",
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_success_complete",
            "state<>'succeeded' OR (inventory_classification='matched' "
            "AND addon_enabled IS NOT NULL AND result_classification IS NOT NULL "
            "AND source_manifest_digest IS NOT NULL "
            "AND verified_destination_digest IS NOT NULL AND failure_code IS NULL "
            "AND ((deployment_mode='direct' AND sync_completed=1 "
            "AND target_started=1 AND (target_class='internal_lab' OR "
            "(bridge_receipt_id IS NOT NULL AND file_count IS NOT NULL "
            "AND total_bytes IS NOT NULL))) OR "
            "(deployment_mode='repository' AND target_class='external_development' "
            "AND addon_enabled=1 AND file_count IS NOT NULL "
            "AND total_bytes IS NOT NULL AND source_manifest_digest="
            "verified_destination_digest)))",
        )

    op.create_table(
        "external_development_repository_readiness",
        sa.Column(
            "target_id",
            sa.String(36),
            sa.ForeignKey("external_kodi_targets.target_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("bootstrap_sha256", sa.String(64), nullable=False),
        sa.Column("native_kodi_version", sa.String(64), nullable=False),
        sa.Column("bridge_id", sa.String(36), nullable=False),
        sa.Column("repository_version", sa.String(64), nullable=False),
        sa.Column("observed_manifest_digest", sa.String(71), nullable=False),
        sa.Column("manual_updates_confirmed", sa.Boolean(), nullable=False),
        sa.Column("confirmed_by", sa.String(64), nullable=False),
        sa.Column("confirmed_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(target_id)=36 AND length(bridge_id)=36",
            name="external_dev_repository_uuid_shape",
        ),
        sa.CheckConstraint(
            "length(observed_manifest_digest)=71 AND "
            "substr(observed_manifest_digest,1,7)='sha256:'",
            name="external_dev_repository_manifest_shape",
        ),
        sa.CheckConstraint(
            "manual_updates_confirmed=1",
            name="external_dev_repository_manual_updates",
        ),
    )
    op.create_table(
        "repository_workspace_validation_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("addon_version", sa.String(64), nullable=False),
        sa.Column("extension_types_json", sa.Text(), nullable=False),
        sa.Column("source_tree_digest", sa.String(71), nullable=False),
        sa.Column("validated_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "generation>=0 AND length(source_tree_digest)=71 AND "
            "substr(source_tree_digest,1,7)='sha256:'",
            name="repository_workspace_validation_shape",
        ),
        sa.CheckConstraint(
            "length(id)=36 AND length(workspace_id)=36 AND "
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 64 AND "
            "length(extension_types_json) BETWEEN 2 AND 4096 AND "
            "length(validated_at)=27",
            name="repository_workspace_validation_bounds",
        ),
    )
    op.create_table(
        "repository_workspace_deployments",
        sa.Column(
            "operation_id",
            sa.String(36),
            sa.ForeignKey(
                "mcp_workspace_deployment_operations.id", ondelete="RESTRICT"
            ),
            primary_key=True,
        ),
        sa.Column(
            "publication_id",
            sa.String(36),
            sa.ForeignKey("development_publications.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("workspace_generation", sa.Integer(), nullable=False),
        sa.Column(
            "target_id",
            sa.String(36),
            sa.ForeignKey("external_kodi_targets.target_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("target_kodi_version", sa.String(64), nullable=False),
        sa.Column("bridge_id", sa.String(36), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("addon_version", sa.String(64), nullable=False),
        sa.Column("zip_sha256", sa.String(64), nullable=False),
        sa.Column("package_tree_sha256", sa.String(64), nullable=False),
        sa.Column("expected_manifest_digest", sa.String(71), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "validation_receipt",
            sa.String(36),
            sa.ForeignKey(
                "repository_workspace_validation_snapshots.id", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        sa.Column("repository_id", sa.String(64), nullable=False),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("bootstrap_sha256", sa.String(64), nullable=False),
        sa.Column("installed_before_version", sa.String(64)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("approved_at", sa.String(27)),
        sa.Column("published_at", sa.String(27)),
        sa.Column("observed_at", sa.String(27)),
        sa.Column("verified_at", sa.String(27)),
        sa.Column("failure_code", sa.String(128)),
        sa.CheckConstraint(
            "workspace_generation>=0",
            name="repository_workspace_generation_nonnegative",
        ),
        sa.CheckConstraint(
            "state IN ('awaiting_approval','publication_approved',"
            "'awaiting_kodi_update','observed_identity','verified_tree',"
            "'succeeded','failed')",
            name="repository_workspace_state_closed",
        ),
        sa.CheckConstraint(
            "repository_id='repository.kodi-lab-dev'",
            name="repository_workspace_origin_closed",
        ),
        sa.CheckConstraint(
            "length(expected_manifest_digest)=71 AND "
            "substr(expected_manifest_digest,1,7)='sha256:'",
            name="repository_workspace_manifest_shape",
        ),
        sa.CheckConstraint(
            "file_count BETWEEN 1 AND 25000 AND total_bytes BETWEEN 1 AND 1073741824",
            name="repository_workspace_bounds",
        ),
    )
    op.create_index(
        "ix_repository_workspace_target_state",
        "repository_workspace_deployments",
        ["target_id", "state"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if (
        bind.scalar(sa.text("SELECT count(*) FROM repository_workspace_deployments"))
        or bind.scalar(
            sa.text("SELECT count(*) FROM external_development_repository_readiness")
        )
        or bind.scalar(
            sa.text("SELECT count(*) FROM repository_workspace_validation_snapshots")
        )
    ):
        raise RuntimeError(
            "populated repository workspace deployment cannot be downgraded"
        )
    op.drop_index(
        "ix_repository_workspace_target_state",
        table_name="repository_workspace_deployments",
    )
    op.drop_table("repository_workspace_deployments")
    op.drop_table("repository_workspace_validation_snapshots")
    op.drop_table("external_development_repository_readiness")
    with op.batch_alter_table("mcp_workspace_deployment_operations") as batch:
        for name in (
            "mcp_workspace_deployment_mode_closed",
            "mcp_workspace_deployment_target_shape",
            "mcp_workspace_deployment_phase_closed",
            "mcp_workspace_deployment_success_complete",
        ):
            batch.drop_constraint(name, type_="check")
        batch.drop_column("deployment_mode")
        batch.create_check_constraint(
            "mcp_workspace_deployment_target_shape",
            "(target_class='internal_lab' AND runtime_revision_id IS NOT NULL "
            "AND bridge_id IS NULL AND bridge_request_id IS NULL AND "
            "bridge_receipt_id IS NULL) OR (target_class='external_development' "
            "AND runtime_revision_id IS NULL AND bridge_id IS NOT NULL "
            "AND bridge_request_id IS NOT NULL)",
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_phase_closed",
            "phase IN ('claimed','preflight','stop','sync','transfer','replace',"
            "'receipt','start','readiness','inventory','finalize')",
        )
        batch.create_check_constraint(
            "mcp_workspace_deployment_success_complete",
            "state<>'succeeded' OR (sync_completed=1 AND target_started=1 "
            "AND inventory_classification='matched' AND addon_enabled IS NOT NULL "
            "AND result_classification IS NOT NULL "
            "AND source_manifest_digest IS NOT NULL "
            "AND verified_destination_digest IS NOT NULL AND failure_code IS NULL "
            "AND (target_class='internal_lab' OR (bridge_receipt_id IS NOT NULL "
            "AND file_count IS NOT NULL AND total_bytes IS NOT NULL)))",
        )
