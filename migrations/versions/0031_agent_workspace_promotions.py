"""Add reviewed three-way agent workspace source promotions.

Revision ID: 0031_agent_workspace_promotions
Revises: 0030_agent_workspace_mutations
"""

# SQLite check expressions are intentionally exact SQL literals.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_agent_workspace_promotions"
down_revision: str | Sequence[str] | None = "0030_agent_workspace_mutations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_workspaces", recreate="always") as batch_op:
        batch_op.drop_constraint("agent_workspace_state", type_="check")
        batch_op.alter_column(
            "state",
            existing_type=sa.String(23),
            type_=sa.Enum(
                "creating",
                "ready",
                "promoted",
                "abandoned",
                "reconciliation_required",
                name="agent_workspace_state",
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=False,
        )
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch_op.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ('reset','snapshot_create','snapshot_restore','lifecycle','fixture_assignment','runtime_reassignment','instance_delete','development_source_attachment','development_source_deployment','development_source_enablement','development_source_smoke_test','addon_zip_installation','addon_zip_enablement','addon_zip_smoke_test','addon_repository_attachment','addon_repository_installation','repository_replacement','agent_workspace_promotion')",
        )
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore','snapshot_delete','instance_delete','development_source_deployment','addon_zip_import','addon_zip_installation','addon_repository_refresh','addon_repository_fetch','addon_repository_installation','repository_replacement','agent_workspace_creation','agent_workspace_promotion')",
        )

    op.create_table(
        "agent_workspace_promotion_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("target_instance_id", sa.String(36), nullable=False),
        sa.Column("workspace_generation", sa.Integer(), nullable=False),
        sa.Column("workspace_digest", sa.String(71), nullable=False),
        sa.Column("baseline_artifact_id", sa.String(36), nullable=False),
        sa.Column("baseline_digest", sa.String(71), nullable=False),
        sa.Column("source_capture_digest", sa.String(71), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("current_version", sa.String(128), nullable=False),
        sa.Column("promoted_name", sa.String(256), nullable=False),
        sa.Column("promoted_version", sa.String(128), nullable=False),
        sa.Column("target_runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("target_container_id", sa.String(64), nullable=False),
        sa.Column("target_deployment_operation_id", sa.String(36), nullable=False),
        sa.Column("target_source_digest", sa.String(71), nullable=False),
        sa.Column("target_destination_digest", sa.String(71), nullable=False),
        sa.Column("changed_file_count", sa.Integer(), nullable=False),
        sa.Column("review_text_bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("reviewable_at", sa.String(32)),
        sa.Column("expires_at", sa.String(32)),
        sa.Column(
            "state",
            sa.Enum(
                "preparing",
                "reviewable",
                "conflicted",
                "invalidated",
                "expired",
                "approved_and_queued",
                "consumed",
                name="agent_workspace_promotion_plan_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("source_operation_id", sa.String(36), unique=True),
        sa.Column("invalidation_reason", sa.String(128)),
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
            ["target_runtime_revision_id"],
            ["runtime_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_deployment_operation_id"],
            ["development_source_deployment_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "workspace_generation >= 0", name="workspace_promotion_generation"
        ),
        sa.CheckConstraint(
            "changed_file_count BETWEEN 1 AND 100 AND review_text_bytes BETWEEN 0 AND 4194304",
            name="workspace_promotion_review_bounds",
        ),
        sa.CheckConstraint(
            "length(workspace_digest)=71 AND length(baseline_digest)=71 AND length(source_capture_digest)=71 AND length(target_source_digest)=71 AND length(target_destination_digest)=71",
            name="workspace_promotion_digest_bounds",
        ),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND length(current_version) BETWEEN 1 AND 128 AND length(promoted_name) BETWEEN 1 AND 256 AND length(promoted_version) BETWEEN 1 AND 128 AND length(target_container_id)=64",
            name="workspace_promotion_identity_bounds",
        ),
        sa.CheckConstraint(
            "invalidation_reason IS NULL OR length(invalidation_reason) BETWEEN 1 AND 128",
            name="workspace_promotion_invalidation_bounded",
        ),
    )
    op.create_index(
        "uq_workspace_promotion_active_workspace",
        "agent_workspace_promotion_plans",
        ["workspace_id"],
        unique=True,
        sqlite_where=sa.text(
            "state IN ('preparing','reviewable','approved_and_queued')"
        ),
    )
    op.create_index(
        "ix_workspace_promotion_source_created",
        "agent_workspace_promotion_plans",
        ["development_source_id", "created_at"],
    )

    op.create_table(
        "agent_workspace_promotion_plan_paths",
        sa.Column("plan_id", sa.String(36), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("relative_path", sa.String(512), nullable=False),
        sa.Column(
            "change_kind",
            sa.Enum(
                "add",
                "modify",
                name="agent_workspace_promotion_change_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("pre_sha256", sa.String(64)),
        sa.Column("pre_size", sa.BigInteger()),
        sa.Column("pre_mode", sa.Integer()),
        sa.Column("post_sha256", sa.String(64), nullable=False),
        sa.Column("post_size", sa.BigInteger(), nullable=False),
        sa.Column("post_mode", sa.Integer(), nullable=False),
        sa.Column("before_text", sa.Text()),
        sa.Column("after_text", sa.Text(), nullable=False),
        sa.Column("conflict_reason", sa.String(64)),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["agent_workspace_promotion_plans.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "ordinal BETWEEN 0 AND 99", name="workspace_promotion_path_ordinal"
        ),
        sa.CheckConstraint(
            "length(relative_path) BETWEEN 1 AND 512",
            name="workspace_promotion_path_bounded",
        ),
        sa.CheckConstraint(
            "length(post_sha256)=64 AND post_sha256 NOT GLOB '*[^0-9a-f]*' AND (pre_sha256 IS NULL OR (length(pre_sha256)=64 AND pre_sha256 NOT GLOB '*[^0-9a-f]*'))",
            name="workspace_promotion_path_hashes",
        ),
        sa.CheckConstraint(
            "(change_kind='add' AND pre_sha256 IS NULL AND pre_size IS NULL AND pre_mode IS NULL AND before_text IS NULL) OR (change_kind='modify' AND pre_sha256 IS NOT NULL AND pre_size IS NOT NULL AND pre_mode IS NOT NULL AND before_text IS NOT NULL)",
            name="workspace_promotion_path_preimage_shape",
        ),
        sa.UniqueConstraint(
            "plan_id", "relative_path", name="uq_workspace_promotion_path"
        ),
    )

    op.create_table(
        "agent_workspace_promotion_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_id", sa.String(36), nullable=False, unique=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("target_instance_id", sa.String(36), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "reconciliation_required",
                name="agent_workspace_promotion_operation_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "phase",
            sa.Enum(
                "queued",
                "final_validation",
                "backup",
                "application_marker",
                "apply",
                "final_capture",
                "evidence_transition",
                "finalize",
                name="agent_workspace_promotion_phase",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column(
            "application_started",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("backup_artifact_id", sa.String(36), unique=True),
        sa.Column(
            "backup_verified", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("started_at", sa.String(32)),
        sa.Column("completed_at", sa.String(32)),
        sa.Column("failure_code", sa.String(128)),
        sa.Column(
            "reconciliation_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("lease_token_digest", sa.String(64)),
        sa.Column("lease_expires_at", sa.String(32)),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["agent_workspace_promotion_plans.id"], ondelete="RESTRICT"
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
        sa.CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128",
            name="workspace_promotion_failure_bounded",
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest)=64 AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="workspace_promotion_lease_digest",
        ),
        sa.CheckConstraint(
            "(state='queued' AND started_at IS NULL AND completed_at IS NULL AND lease_token_digest IS NULL AND lease_expires_at IS NULL) OR (state='running' AND started_at IS NOT NULL AND completed_at IS NULL AND lease_token_digest IS NOT NULL AND lease_expires_at IS NOT NULL) OR (state IN ('succeeded','failed','reconciliation_required') AND completed_at IS NOT NULL AND lease_token_digest IS NULL AND lease_expires_at IS NULL)",
            name="workspace_promotion_operation_state_fields",
        ),
        sa.CheckConstraint(
            "application_started=0 OR (backup_verified=1 AND backup_artifact_id IS NOT NULL)",
            name="workspace_promotion_marker_requires_backup",
        ),
    )
    op.create_index(
        "uq_workspace_promotion_active_source",
        "agent_workspace_promotion_operations",
        ["development_source_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued','running')"),
    )

    op.create_table(
        "agent_workspace_promotion_journal",
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("relative_path", sa.String(512), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "not_attempted",
                "applied",
                "rolled_back",
                "uncertain",
                name="agent_workspace_promotion_journal_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("applied_device", sa.BigInteger()),
        sa.Column("applied_inode", sa.BigInteger()),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["agent_workspace_promotion_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "operation_id", "relative_path", name="uq_workspace_promotion_journal_path"
        ),
        sa.CheckConstraint(
            "(state='applied' AND applied_device IS NOT NULL AND applied_inode IS NOT NULL) OR (state!='applied' AND applied_device IS NULL AND applied_inode IS NULL)",
            name="workspace_promotion_journal_identity_shape",
        ),
    )
    op.create_table(
        "development_source_promotion_leases",
        sa.Column("development_source_id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False, unique=True),
        sa.Column("lease_token_digest", sa.String(64)),
        sa.Column("acquired_at", sa.String(32)),
        sa.Column("lease_expires_at", sa.String(32)),
        sa.ForeignKeyConstraint(
            ["development_source_id"], ["development_sources.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["agent_workspaces.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["agent_workspace_promotion_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest)=64 AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="development_source_promotion_lease_digest",
        ),
    )
    op.create_table(
        "agent_workspace_promotion_handoffs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("operation_id", sa.String(36), nullable=False, unique=True),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("target_instance_id", sa.String(36), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("trusted_container_id", sa.String(64), nullable=False),
        sa.Column("prior_deployment_operation_id", sa.String(36), nullable=False),
        sa.Column("prior_source_digest", sa.String(71), nullable=False),
        sa.Column("prior_destination_digest", sa.String(71), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["agent_workspace_promotion_operations.id"],
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
            ["prior_deployment_operation_id"],
            ["development_source_deployment_operations.id"],
            ondelete="RESTRICT",
        ),
    )


def downgrade() -> None:
    populated = op.get_bind().scalar(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM agent_workspace_promotion_plans) + (SELECT COUNT(*) FROM agent_workspace_promotion_operations) + (SELECT COUNT(*) FROM agent_workspace_promotion_handoffs) + (SELECT COUNT(*) FROM agent_workspaces WHERE state='promoted')"
        )
    )
    if populated:
        raise RuntimeError(
            "Cannot downgrade populated agent workspace promotion state."
        )
    op.drop_table("agent_workspace_promotion_handoffs")
    op.drop_table("development_source_promotion_leases")
    op.drop_table("agent_workspace_promotion_journal")
    op.drop_index(
        "uq_workspace_promotion_active_source",
        table_name="agent_workspace_promotion_operations",
    )
    op.drop_table("agent_workspace_promotion_operations")
    op.drop_table("agent_workspace_promotion_plan_paths")
    op.drop_index(
        "ix_workspace_promotion_source_created",
        table_name="agent_workspace_promotion_plans",
    )
    op.drop_index(
        "uq_workspace_promotion_active_workspace",
        table_name="agent_workspace_promotion_plans",
    )
    op.drop_table("agent_workspace_promotion_plans")
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore','snapshot_delete','instance_delete','development_source_deployment','addon_zip_import','addon_zip_installation','addon_repository_refresh','addon_repository_fetch','addon_repository_installation','repository_replacement','agent_workspace_creation')",
        )
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch_op.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ('reset','snapshot_create','snapshot_restore','lifecycle','fixture_assignment','runtime_reassignment','instance_delete','development_source_attachment','development_source_deployment','development_source_enablement','development_source_smoke_test','addon_zip_installation','addon_zip_enablement','addon_zip_smoke_test','addon_repository_attachment','addon_repository_installation','repository_replacement')",
        )
    with op.batch_alter_table("agent_workspaces", recreate="always") as batch_op:
        batch_op.drop_constraint("agent_workspace_state", type_="check")
        batch_op.alter_column(
            "state",
            existing_type=sa.String(23),
            type_=sa.Enum(
                "creating",
                "ready",
                "abandoned",
                "reconciliation_required",
                name="agent_workspace_state",
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=False,
        )
