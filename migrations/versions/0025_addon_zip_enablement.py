"""Add ZIP-specific enablement operation history.

Revision ID: 0025_addon_zip_enablement
Revises: 0024_addon_zip_installation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_addon_zip_enablement"
down_revision: str | Sequence[str] | None = "0024_addon_zip_installation"
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
            "'addon_zip_installation','addon_zip_enablement')",
        )

    op.create_table(
        "addon_zip_enablement_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("original_managed_instance_id", sa.String(36), nullable=False),
        sa.Column("managed_instance_id", sa.String(36), nullable=True),
        sa.Column("artifact_id", sa.String(36), nullable=False),
        sa.Column("installation_operation_id", sa.String(36), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("addon_version", sa.String(128), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("expected_container_id", sa.String(64), nullable=True),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column("started_at", sa.String(32), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                "reconciliation_required",
                name="addon_zip_enablement_operation_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "phase",
            sa.Enum(
                "preflight",
                "payload_verification",
                "mutation",
                "postcondition",
                "finalize",
                name="addon_zip_enablement_operation_phase",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "result_classification",
            sa.Enum(
                "enabled",
                "already_enabled",
                "dependency_blocked",
                "rejected",
                "failed",
                "reconciliation_required",
                name="addon_zip_enablement_result",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "payload_verification",
            sa.Enum(
                "not_checked",
                "matched",
                "changed",
                "unknown",
                name="addon_zip_payload_verification_outcome",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default="not_checked",
        ),
        sa.Column("prior_enabled", sa.Boolean(), nullable=True),
        sa.Column("result_enabled", sa.Boolean(), nullable=True),
        sa.Column("enable_performed", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("blocked_dependency_id", sa.String(128), nullable=True),
        sa.Column("blocked_dependency_reason", sa.String(64), nullable=True),
        sa.Column("completed_at", sa.String(32), nullable=True),
        sa.Column("failure_reason", sa.String(256), nullable=True),
        sa.Column(
            "reconciliation_required", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="SET NULL",
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
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 128",
            name="addon_zip_enablement_identity_bounded",
        ),
        sa.CheckConstraint(
            "expected_container_id IS NULL OR (length(expected_container_id)=64 AND "
            "expected_container_id NOT GLOB '*[^0-9a-f]*')",
            name="addon_zip_enablement_container_bounded",
        ),
        sa.CheckConstraint(
            "blocked_dependency_id IS NULL OR "
            "length(blocked_dependency_id) BETWEEN 1 AND 128",
            name="addon_zip_enablement_blocked_id_bounded",
        ),
        sa.CheckConstraint(
            "blocked_dependency_reason IS NULL OR "
            "length(blocked_dependency_reason) BETWEEN 1 AND 64",
            name="addon_zip_enablement_blocked_reason_bounded",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="addon_zip_enablement_failure_bounded",
        ),
    )
    op.create_index(
        "uq_addon_zip_enablement_active_instance",
        "addon_zip_enablement_operations",
        ["managed_instance_id"],
        unique=True,
        sqlite_where=sa.text("state='running'"),
    )
    op.create_index(
        "ix_addon_zip_enablement_original_requested",
        "addon_zip_enablement_operations",
        ["original_managed_instance_id", "requested_at"],
    )
    op.create_index(
        "ix_addon_zip_enablement_artifact",
        "addon_zip_enablement_operations",
        ["artifact_id"],
    )


def downgrade() -> None:
    populated = op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM addon_zip_enablement_operations")
    )
    if populated:
        raise RuntimeError("Cannot downgrade populated ZIP enablement history.")
    op.drop_table("addon_zip_enablement_operations")
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
