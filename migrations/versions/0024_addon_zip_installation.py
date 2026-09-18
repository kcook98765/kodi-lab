"""Add immutable uploaded-ZIP artifacts and installation operations.

Revision ID: 0024_addon_zip_installation
Revises: 0023_dev_source_smoke_test
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_addon_zip_installation"
down_revision: str | Sequence[str] | None = "0023_dev_source_smoke_test"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch_op.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ('reset','snapshot_create','snapshot_restore',"
            "'lifecycle','fixture_assignment','runtime_reassignment','instance_delete',"
            "'development_source_attachment','development_source_deployment',"
            "'development_source_enablement','development_source_smoke_test',"
            "'addon_zip_installation')",
        )
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore','snapshot_delete',"
            "'instance_delete','development_source_deployment','addon_zip_import',"
            "'addon_zip_installation')",
        )

    op.create_table(
        "addon_zip_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("logical_label", sa.String(120), nullable=False),
        sa.Column("sha256_digest", sa.String(71), nullable=False),
        sa.Column("compressed_size", sa.BigInteger(), nullable=False),
        sa.Column("total_uncompressed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("addon_name", sa.String(256), nullable=False),
        sa.Column("addon_version", sa.String(128), nullable=False),
        sa.Column(
            "extension_classification",
            sa.Enum(
                "python_video_plugin",
                name="addon_zip_extension_classification",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("dependencies_json", sa.Text(), nullable=False),
        sa.Column("payload_manifest_digest", sa.String(71), nullable=False),
        sa.Column("imported_at", sa.String(32), nullable=False),
        sa.Column(
            "origin",
            sa.Enum(
                "uploaded_zip",
                name="addon_zip_origin",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "validation_state",
            sa.Enum(
                "accepted",
                name="addon_zip_validation_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(logical_label) BETWEEN 1 AND 120",
            name="addon_zip_artifact_label_bounded",
        ),
        sa.CheckConstraint(
            "length(sha256_digest)=71 AND substr(sha256_digest,1,7)='sha256:'",
            name="addon_zip_artifact_digest_bounded",
        ),
        sa.CheckConstraint(
            "length(payload_manifest_digest)=71 AND "
            "substr(payload_manifest_digest,1,7)='sha256:'",
            name="addon_zip_payload_digest_bounded",
        ),
        sa.CheckConstraint(
            "compressed_size BETWEEN 1 AND 268435456 AND "
            "total_uncompressed_bytes BETWEEN 0 AND 1073741824 AND "
            "member_count BETWEEN 1 AND 50000",
            name="addon_zip_artifact_aggregates_bounded",
        ),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_name) BETWEEN 1 AND 256 AND "
            "length(addon_version) BETWEEN 1 AND 128",
            name="addon_zip_artifact_identity_bounded",
        ),
    )
    op.create_table(
        "addon_zip_import_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("artifact_id", sa.String(36), nullable=False, unique=True),
        sa.Column("logical_label", sa.String(120), nullable=False),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                name="addon_zip_import_operation_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "phase",
            sa.Enum(
                "uploaded",
                "validating",
                "publishing",
                "finalize",
                name="addon_zip_import_operation_phase",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.String(32), nullable=True),
        sa.Column("completed_at", sa.String(32), nullable=True),
        sa.Column("failure_reason", sa.String(256), nullable=True),
        sa.Column("lease_token_digest", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.String(32), nullable=True),
        sa.CheckConstraint(
            "length(logical_label) BETWEEN 1 AND 120",
            name="addon_zip_import_label_bounded",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="addon_zip_import_failure_bounded",
        ),
    )
    op.create_index(
        "uq_addon_zip_import_active",
        "addon_zip_import_operations",
        ["state"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued','running')"),
    )
    op.create_table(
        "addon_zip_installation_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("original_managed_instance_id", sa.String(36), nullable=False),
        sa.Column("managed_instance_id", sa.String(36), nullable=True),
        sa.Column("artifact_id", sa.String(36), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("addon_version", sa.String(128), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "reconciliation_required",
                name="addon_zip_installation_operation_state",
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
                "applying",
                "verify_stopped",
                "restarting",
                "readiness",
                "recognition",
                "verify_final",
                "finalize",
                name="addon_zip_installation_operation_phase",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("expected_container_id", sa.String(64), nullable=True),
        sa.Column(
            "application_started", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column(
            "stopped_readback_matched", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column(
            "final_readback_matched", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("recognition_enabled", sa.Boolean(), nullable=True),
        sa.Column("dependency_baseline_json", sa.Text(), nullable=True),
        sa.Column("dependency_post_json", sa.Text(), nullable=True),
        sa.Column("started_at", sa.String(32), nullable=True),
        sa.Column("heartbeat_at", sa.String(32), nullable=True),
        sa.Column("completed_at", sa.String(32), nullable=True),
        sa.Column("failure_reason", sa.String(256), nullable=True),
        sa.Column(
            "reconciliation_required", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("lease_token_digest", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.String(32), nullable=True),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["addon_zip_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["runtime_revision_id"], ["runtime_revisions.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 128",
            name="addon_zip_installation_identity_bounded",
        ),
        sa.CheckConstraint(
            "expected_container_id IS NULL OR length(expected_container_id)=64",
            name="addon_zip_installation_container_bounded",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="addon_zip_installation_failure_bounded",
        ),
        sa.CheckConstraint(
            "dependency_baseline_json IS NULL OR "
            "length(dependency_baseline_json)<=16384",
            name="addon_zip_installation_dependency_baseline_bounded",
        ),
        sa.CheckConstraint(
            "dependency_post_json IS NULL OR length(dependency_post_json)<=16384",
            name="addon_zip_installation_dependency_post_bounded",
        ),
    )
    op.create_index(
        "uq_addon_zip_installation_active_instance",
        "addon_zip_installation_operations",
        ["managed_instance_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued','running')"),
    )
    op.create_index(
        "ix_addon_zip_installation_original_requested",
        "addon_zip_installation_operations",
        ["original_managed_instance_id", "requested_at"],
    )
    op.create_table(
        "addon_zip_installation_evidence",
        sa.Column("managed_instance_id", sa.String(36), primary_key=True),
        sa.Column("original_managed_instance_id", sa.String(36), nullable=False),
        sa.Column("artifact_id", sa.String(36), nullable=False),
        sa.Column("installation_operation_id", sa.String(36), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("trusted_container_id", sa.String(64), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False, primary_key=True),
        sa.Column("addon_version", sa.String(128), nullable=False),
        sa.Column("payload_manifest_digest", sa.String(71), nullable=False),
        sa.Column("verified_at", sa.String(32), nullable=False),
        sa.Column(
            "confidence",
            sa.Enum(
                "verified_at_last_operation",
                "stale",
                "unknown",
                name="addon_zip_installation_confidence",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "result",
            sa.Enum(
                "installed_recognized_disabled",
                name="addon_zip_installation_result",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"], ["managed_instances.target_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["addon_zip_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["installation_operation_id"],
            ["addon_zip_installation_operations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_revision_id"], ["runtime_revisions.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "length(trusted_container_id)=64 AND "
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 128",
            name="addon_zip_evidence_identity_bounded",
        ),
        sa.CheckConstraint(
            "length(payload_manifest_digest)=71 AND "
            "substr(payload_manifest_digest,1,7)='sha256:'",
            name="addon_zip_evidence_digest_bounded",
        ),
    )


def downgrade() -> None:
    populated = op.get_bind().scalar(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM addon_zip_artifacts) + "
            "(SELECT COUNT(*) FROM addon_zip_import_operations) + "
            "(SELECT COUNT(*) FROM addon_zip_installation_operations) + "
            "(SELECT COUNT(*) FROM addon_zip_installation_evidence)"
        )
    )
    if populated:
        raise RuntimeError("Cannot downgrade populated uploaded-ZIP state or history.")
    op.drop_table("addon_zip_installation_evidence")
    op.drop_table("addon_zip_installation_operations")
    op.drop_table("addon_zip_import_operations")
    op.drop_table("addon_zip_artifacts")
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore',"
            "'snapshot_delete','instance_delete','development_source_deployment')",
        )
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch_op.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ('reset','snapshot_create','snapshot_restore',"
            "'lifecycle','fixture_assignment','runtime_reassignment','instance_delete',"
            "'development_source_attachment','development_source_deployment',"
            "'development_source_enablement','development_source_smoke_test')",
        )
