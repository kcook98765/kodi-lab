"""Add ZIP-specific smoke-test operation history.

Revision ID: 0026_addon_zip_smoke_test
Revises: 0025_addon_zip_enablement
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_addon_zip_smoke_test"
down_revision: str | Sequence[str] | None = "0025_addon_zip_enablement"
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
            "'addon_zip_installation','addon_zip_enablement','addon_zip_smoke_test')",
        )

    op.create_table(
        "addon_zip_smoke_test_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("original_managed_instance_id", sa.String(36), nullable=False),
        sa.Column("managed_instance_id", sa.String(36), nullable=True),
        sa.Column("artifact_id", sa.String(36), nullable=False),
        sa.Column("installation_operation_id", sa.String(36), nullable=False),
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
                name="addon_zip_smoke_test_operation_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "phase",
            sa.Enum(
                "acquire",
                "validate",
                "verify_pre_execution",
                "invoke",
                "verify_post_execution",
                "finalize",
                name="addon_zip_smoke_test_operation_phase",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "overall_result",
            sa.Enum(
                "passed",
                "execution_failed",
                "timeout",
                "dependency_blocked",
                "installed_tree_changed",
                "unsupported_addon_type",
                "reconciliation_required",
                name="addon_zip_smoke_test_overall_result",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "execution_result",
            sa.Enum(
                "not_attempted",
                "directory_result",
                "jsonrpc_error",
                "timeout",
                "ambiguous",
                name="addon_zip_smoke_test_execution_result",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default="not_attempted",
        ),
        sa.Column(
            "post_execution_provenance",
            sa.Enum(
                "not_checked",
                "matched",
                "changed",
                "unknown",
                name="addon_zip_smoke_test_post_provenance",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default="not_checked",
        ),
        sa.Column(
            "invocation_dispatched", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("invocation_duration_ms", sa.Integer(), nullable=True),
        sa.Column("returned_item_count", sa.Integer(), nullable=True),
        sa.Column("blocked_dependency_id", sa.String(128), nullable=True),
        sa.Column("blocked_dependency_reason", sa.String(64), nullable=True),
        sa.Column("expected_container_id", sa.String(64), nullable=True),
        sa.Column("started_at", sa.String(32), nullable=True),
        sa.Column("completed_at", sa.String(32), nullable=True),
        sa.Column("failure_reason", sa.String(256), nullable=True),
        sa.Column(
            "reconciliation_required", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("lease_token_digest", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.String(32), nullable=True),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND "
            "length(addon_version) BETWEEN 1 AND 128",
            name="addon_zip_smoke_test_identity_bounded",
        ),
        sa.CheckConstraint(
            "invocation_duration_ms IS NULL OR "
            "invocation_duration_ms BETWEEN 0 AND 90000",
            name="addon_zip_smoke_test_duration_bounded",
        ),
        sa.CheckConstraint(
            "returned_item_count IS NULL OR returned_item_count BETWEEN 0 AND 1",
            name="addon_zip_smoke_test_item_count_bounded",
        ),
        sa.CheckConstraint(
            "blocked_dependency_id IS NULL OR "
            "length(blocked_dependency_id) BETWEEN 1 AND 128",
            name="addon_zip_smoke_test_blocked_id_bounded",
        ),
        sa.CheckConstraint(
            "blocked_dependency_reason IS NULL OR "
            "length(blocked_dependency_reason) BETWEEN 1 AND 64",
            name="addon_zip_smoke_test_blocked_reason_bounded",
        ),
        sa.CheckConstraint(
            "expected_container_id IS NULL OR (length(expected_container_id) = 64 "
            "AND expected_container_id NOT GLOB '*[^0-9a-f]*')",
            name="addon_zip_smoke_test_expected_container",
        ),
        sa.CheckConstraint(
            "lease_token_digest IS NULL OR (length(lease_token_digest) = 64 "
            "AND lease_token_digest NOT GLOB '*[^0-9a-f]*')",
            name="addon_zip_smoke_test_lease_digest",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="addon_zip_smoke_test_failure_bounded",
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
    )
    op.create_index(
        "uq_addon_zip_smoke_test_active_instance",
        "addon_zip_smoke_test_operations",
        ["managed_instance_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued','running')"),
    )
    op.create_index(
        "ix_addon_zip_smoke_test_original_requested",
        "addon_zip_smoke_test_operations",
        ["original_managed_instance_id", "requested_at"],
    )
    op.create_index(
        "ix_addon_zip_smoke_test_artifact",
        "addon_zip_smoke_test_operations",
        ["artifact_id"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM addon_zip_smoke_test_operations")
    ):
        raise RuntimeError("Cannot downgrade populated ZIP smoke-test history.")
    op.drop_table("addon_zip_smoke_test_operations")
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
