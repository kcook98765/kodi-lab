"""Add exact one-shot repository replacement approval and execution.

Revision ID: 0028_repository_replacement_override
Revises: 0027_addon_repository_testing
"""

# SQLite check expressions are intentionally exact SQL literals.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_repository_replacement_override"
down_revision: str | Sequence[str] | None = "0027_addon_repository_testing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch_op.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ('reset','snapshot_create','snapshot_restore','lifecycle',"
            "'fixture_assignment','runtime_reassignment','instance_delete',"
            "'development_source_attachment','development_source_deployment',"
            "'development_source_enablement','development_source_smoke_test',"
            "'addon_zip_installation','addon_zip_enablement','addon_zip_smoke_test',"
            "'addon_repository_attachment','addon_repository_installation',"
            "'repository_replacement')",
        )
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore','snapshot_delete',"
            "'instance_delete','development_source_deployment','addon_zip_import',"
            "'addon_zip_installation','addon_repository_refresh','addon_repository_fetch',"
            "'addon_repository_installation','repository_replacement')",
        )

    op.create_table(
        "repository_replacement_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("original_managed_instance_id", sa.String(36), nullable=False),
        sa.Column("managed_instance_id", sa.String(36)),
        sa.Column("repository_source_id", sa.String(36), nullable=False),
        sa.Column("repository_attachment_at", sa.String(32), nullable=False),
        sa.Column("repository_snapshot_id", sa.String(36), nullable=False),
        sa.Column("repository_candidate_id", sa.String(36), nullable=False),
        sa.Column("incoming_artifact_id", sa.String(36), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column(
            "incumbent_class",
            sa.Enum(
                "attached_unmaterialized",
                "verified_copied",
                name="repository_replacement_incumbent_class",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("development_source_attachment_at", sa.String(32), nullable=False),
        sa.Column("development_deployment_operation_id", sa.String(36)),
        sa.Column("development_source_manifest_digest", sa.String(71)),
        sa.Column("development_destination_digest", sa.String(71)),
        sa.Column("incumbent_version", sa.String(128)),
        sa.Column("incumbent_type", sa.String(128)),
        sa.Column("incumbent_enabled", sa.Boolean()),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("expected_container_id", sa.String(64), nullable=False),
        sa.Column("qualifying_snapshot_id", sa.String(36)),
        sa.Column("incoming_version", sa.String(128), nullable=False),
        sa.Column("incoming_payload_manifest_digest", sa.String(71), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.String(32), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "approved",
                "expired",
                "invalidated",
                "consumed",
                "cancelled",
                name="repository_replacement_plan_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("consumed_execution_operation_id", sa.String(36)),
        sa.Column(
            "terminal_outcome",
            sa.Enum(
                "succeeded",
                "failed",
                "reconciliation_required",
                name="repository_replacement_terminal_outcome",
                native_enum=False,
                create_constraint=True,
            ),
        ),
        sa.Column("invalidation_reason", sa.String(128)),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["repository_source_id"],
            ["addon_repository_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["repository_snapshot_id"],
            ["addon_repository_metadata_snapshots.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["repository_candidate_id"],
            ["addon_repository_candidates.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["incoming_artifact_id"], ["addon_zip_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["development_source_id"], ["development_sources.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["development_deployment_operation_id"],
            ["development_source_deployment_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_revision_id"], ["runtime_revisions.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND length(incoming_version) BETWEEN 1 AND 128",
            name="repository_replacement_plan_identity_bounded",
        ),
        sa.CheckConstraint(
            "length(expected_container_id)=64",
            name="repository_replacement_plan_container_bounded",
        ),
        sa.CheckConstraint(
            "length(incoming_payload_manifest_digest)=71 AND substr(incoming_payload_manifest_digest,1,7)='sha256:'",
            name="repository_replacement_plan_incoming_digest",
        ),
        sa.CheckConstraint(
            "invalidation_reason IS NULL OR length(invalidation_reason) BETWEEN 1 AND 128",
            name="repository_replacement_plan_reason_bounded",
        ),
        sa.CheckConstraint(
            "(incumbent_class='attached_unmaterialized' AND development_deployment_operation_id IS NULL AND development_source_manifest_digest IS NULL AND development_destination_digest IS NULL AND incumbent_version IS NULL AND incumbent_type IS NULL AND incumbent_enabled IS NULL AND qualifying_snapshot_id IS NULL) OR (incumbent_class='verified_copied' AND development_deployment_operation_id IS NOT NULL AND development_source_manifest_digest IS NOT NULL AND development_destination_digest IS NOT NULL AND incumbent_version IS NOT NULL AND incumbent_type IS NOT NULL AND incumbent_enabled IS NOT NULL AND qualifying_snapshot_id IS NOT NULL)",
            name="repository_replacement_plan_incumbent_consistent",
        ),
    )
    op.create_index(
        "ix_repository_replacement_plan_instance_created",
        "repository_replacement_plans",
        ["original_managed_instance_id", "created_at"],
    )
    op.create_index(
        "uq_repository_replacement_plan_active_instance",
        "repository_replacement_plans",
        ["managed_instance_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('pending','approved')"),
    )

    op.create_table(
        "repository_replacement_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_id", sa.String(36), nullable=False, unique=True),
        sa.Column("original_managed_instance_id", sa.String(36), nullable=False),
        sa.Column("managed_instance_id", sa.String(36)),
        sa.Column("incoming_artifact_id", sa.String(36), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("incoming_version", sa.String(128), nullable=False),
        sa.Column(
            "incumbent_class",
            sa.Enum(
                "attached_unmaterialized",
                "verified_copied",
                name="repository_replacement_operation_incumbent_class",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("expected_container_id", sa.String(64), nullable=False),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "reconciliation_required",
                name="repository_replacement_operation_state",
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
                "stopping",
                "revalidate_stopped",
                "applying",
                "verify_stopped",
                "restarting",
                "readiness",
                "recognition",
                "verify_final",
                "finalize",
                name="repository_replacement_operation_phase",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "application_started",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "stopped_readback_matched",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "final_readback_matched",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("recognition_enabled", sa.Boolean()),
        sa.Column("dependency_baseline_json", sa.Text()),
        sa.Column("dependency_post_json", sa.Text()),
        sa.Column("started_at", sa.String(32)),
        sa.Column("heartbeat_at", sa.String(32)),
        sa.Column("completed_at", sa.String(32)),
        sa.Column("failure_reason", sa.String(256)),
        sa.Column(
            "reconciliation_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("lease_token_digest", sa.String(64)),
        sa.Column("lease_expires_at", sa.String(32)),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["repository_replacement_plans.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["incoming_artifact_id"], ["addon_zip_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["development_source_id"], ["development_sources.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND length(incoming_version) BETWEEN 1 AND 128",
            name="repository_replacement_operation_identity_bounded",
        ),
        sa.CheckConstraint(
            "length(expected_container_id)=64",
            name="repository_replacement_operation_container_bounded",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="repository_replacement_operation_reason_bounded",
        ),
        sa.CheckConstraint(
            "(state='queued' AND started_at IS NULL AND completed_at IS NULL AND lease_token_digest IS NULL AND lease_expires_at IS NULL AND application_started=0) OR (state='running' AND started_at IS NOT NULL AND completed_at IS NULL AND lease_token_digest IS NOT NULL AND lease_expires_at IS NOT NULL) OR (state IN ('succeeded','failed','reconciliation_required') AND completed_at IS NOT NULL AND lease_token_digest IS NULL AND lease_expires_at IS NULL)",
            name="repository_replacement_operation_state_consistent",
        ),
        sa.CheckConstraint(
            "(state='succeeded' AND application_started=1 AND stopped_readback_matched=1 AND final_readback_matched=1 AND recognition_enabled IS NOT NULL AND dependency_baseline_json IS NOT NULL AND dependency_post_json IS NOT NULL AND reconciliation_required=0) OR (state='failed' AND application_started=0 AND reconciliation_required=0) OR (state='reconciliation_required' AND application_started=1 AND reconciliation_required=1) OR state IN ('queued','running')",
            name="repository_replacement_operation_result_consistent",
        ),
    )
    op.create_index(
        "uq_repository_replacement_operation_active_instance",
        "repository_replacement_operations",
        ["managed_instance_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued','running')"),
    )
    op.create_index(
        "ix_repository_replacement_operation_original_requested",
        "repository_replacement_operations",
        ["original_managed_instance_id", "requested_at"],
    )

    with op.batch_alter_table(
        "addon_zip_installation_evidence", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("addon_zip_installation_result", type_="check")
        batch_op.alter_column(
            "result",
            existing_type=sa.String(29),
            type_=sa.Enum(
                "installed_recognized_disabled",
                "repository_replacement_recognized",
                name="addon_zip_installation_result",
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "installation_operation_id", existing_type=sa.String(36), nullable=True
        )
        batch_op.add_column(sa.Column("replacement_operation_id", sa.String(36)))
        batch_op.create_foreign_key(
            "fk_addon_zip_evidence_replacement_operation",
            "repository_replacement_operations",
            ["replacement_operation_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "addon_zip_evidence_producer_consistent",
            "(installation_operation_id IS NOT NULL AND replacement_operation_id IS NULL AND result='installed_recognized_disabled') OR (installation_operation_id IS NULL AND replacement_operation_id IS NOT NULL AND result='repository_replacement_recognized')",
        )


def downgrade() -> None:
    populated = op.get_bind().scalar(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM repository_replacement_plans) + (SELECT COUNT(*) FROM repository_replacement_operations) + (SELECT COUNT(*) FROM addon_zip_installation_evidence WHERE replacement_operation_id IS NOT NULL)"
        )
    )
    if populated:
        raise RuntimeError(
            "Cannot downgrade populated repository replacement state or history."
        )
    with op.batch_alter_table(
        "addon_zip_installation_evidence", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint(
            "addon_zip_evidence_producer_consistent", type_="check"
        )
        batch_op.drop_constraint(
            "fk_addon_zip_evidence_replacement_operation", type_="foreignkey"
        )
        batch_op.drop_column("replacement_operation_id")
        batch_op.alter_column(
            "installation_operation_id", existing_type=sa.String(36), nullable=False
        )
        batch_op.drop_constraint("addon_zip_installation_result", type_="check")
        batch_op.alter_column(
            "result",
            existing_type=sa.String(33),
            type_=sa.Enum(
                "installed_recognized_disabled",
                name="addon_zip_installation_result",
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=False,
        )
    op.drop_table("repository_replacement_operations")
    op.drop_table("repository_replacement_plans")
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore','snapshot_delete','instance_delete','development_source_deployment','addon_zip_import','addon_zip_installation','addon_repository_refresh','addon_repository_fetch','addon_repository_installation')",
        )
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch_op.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ('reset','snapshot_create','snapshot_restore','lifecycle','fixture_assignment','runtime_reassignment','instance_delete','development_source_attachment','development_source_deployment','development_source_enablement','development_source_smoke_test','addon_zip_installation','addon_zip_enablement','addon_zip_smoke_test','addon_repository_attachment','addon_repository_installation')",
        )
