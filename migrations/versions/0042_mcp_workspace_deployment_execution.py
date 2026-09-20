"""Add exact-generation MCP workspace deployment and fixed execution.

Revision ID: 0042_mcp_workspace_deployment_execution
Revises: 0041_mcp_workspace_validation_retention
"""

# Closed SQLite constraint literals are intentionally explicit.
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_mcp_workspace_deployment_execution"
down_revision: str | Sequence[str] | None = "0041_mcp_workspace_validation_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADDON_DEVELOPMENT_PRESET_ID = "11000000-0000-4000-8000-000000000001"
FULL_LAB_CONTROL_PRESET_ID = "11000000-0000-4000-8000-000000000003"
PRESET_IDS = (ADDON_DEVELOPMENT_PRESET_ID, FULL_LAB_CONTROL_PRESET_ID)
PERMISSIONS = ("workspace.deploy", "development.execute")

_MUTATION_KINDS = "operation_kind IN ('reset','snapshot_create','snapshot_restore','lifecycle','fixture_assignment','runtime_reassignment','instance_delete','development_source_attachment','development_source_deployment','development_source_enablement','development_source_smoke_test','addon_zip_installation','addon_zip_enablement','addon_zip_smoke_test','addon_repository_attachment','addon_repository_installation','repository_replacement','agent_workspace_promotion','promotion_verification','mcp_workspace_deployment','mcp_workspace_execution')"
_MUTATION_KINDS_OLD = "operation_kind IN ('reset','snapshot_create','snapshot_restore','lifecycle','fixture_assignment','runtime_reassignment','instance_delete','development_source_attachment','development_source_deployment','development_source_enablement','development_source_smoke_test','addon_zip_installation','addon_zip_enablement','addon_zip_smoke_test','addon_repository_attachment','addon_repository_installation','repository_replacement','agent_workspace_promotion','promotion_verification')"
_HEAVY_KINDS = "operation_kind IN ('snapshot_create','snapshot_restore','snapshot_delete','instance_delete','development_source_deployment','addon_zip_import','addon_zip_installation','addon_repository_refresh','addon_repository_fetch','addon_repository_installation','repository_replacement','agent_workspace_creation','agent_workspace_promotion','promotion_verification','agent_workspace_cleanup','mcp_workspace_deployment')"
_HEAVY_KINDS_OLD = "operation_kind IN ('snapshot_create','snapshot_restore','snapshot_delete','instance_delete','development_source_deployment','addon_zip_import','addon_zip_installation','addon_repository_refresh','addon_repository_fetch','addon_repository_installation','repository_replacement','agent_workspace_creation','agent_workspace_promotion','promotion_verification','agent_workspace_cleanup')"


def upgrade() -> None:
    bind = op.get_bind()
    existing = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_preset_permissions "
            "WHERE permission IN ('workspace.deploy','development.execute')"
        )
    )
    if existing:
        raise RuntimeError("MCP Slice 4 preset state already exists.")
    permissions = sa.table(
        "mcp_preset_permissions",
        sa.column("preset_id", sa.String),
        sa.column("permission", sa.String),
    )
    op.bulk_insert(
        permissions,
        [
            {"preset_id": preset_id, "permission": permission}
            for preset_id in PRESET_IDS
            for permission in PERMISSIONS
        ],
    )

    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch:
        batch.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch.create_check_constraint("managed_instance_mutation_kind", _MUTATION_KINDS)
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch:
        batch.drop_constraint("global_heavy_operation_kind", type_="check")
        batch.create_check_constraint("global_heavy_operation_kind", _HEAVY_KINDS)

    with op.batch_alter_table("mcp_development_requests", recreate="always") as batch:
        batch.drop_constraint("mcp_development_request_uuid_lengths", type_="check")
        batch.drop_constraint("mcp_development_request_operation_closed", type_="check")
        batch.alter_column(
            "creation_operation_id", existing_type=sa.String(36), nullable=True
        )
        batch.add_column(sa.Column("operation_id", sa.String(36), nullable=True))
        batch.create_check_constraint(
            "mcp_development_request_uuid_lengths",
            "length(id) = 36 AND length(client_id) = 36 AND length(request_id) = 36 AND length(development_source_id) = 36 AND length(workspace_id) = 36 AND (creation_operation_id IS NULL OR length(creation_operation_id) = 36) AND (operation_id IS NULL OR length(operation_id) = 36)",
        )
        batch.create_check_constraint(
            "mcp_development_request_operation_closed",
            "operation_kind IN ('workspace_create','workspace_deploy','workspace_execute')",
        )
        batch.create_check_constraint(
            "mcp_development_request_operation_shape",
            "(operation_kind = 'workspace_create' AND creation_operation_id IS NOT NULL AND operation_id IS NULL) OR (operation_kind IN ('workspace_deploy','workspace_execute') AND creation_operation_id IS NULL AND operation_id IS NOT NULL)",
        )

    op.create_table(
        "mcp_workspace_validations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), nullable=False, unique=True),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("classification", sa.String(16), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("addon_version", sa.String(128), nullable=False),
        sa.Column("extension_types_json", sa.Text(), nullable=False),
        sa.Column("validated_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(id) = 36 AND length(workspace_id) = 36 AND length(development_source_id) = 36",
            name="mcp_workspace_validation_uuid_lengths",
        ),
        sa.CheckConstraint(
            "generation >= 0", name="mcp_workspace_validation_generation_nonnegative"
        ),
        sa.CheckConstraint(
            "classification = 'passed'",
            name="mcp_workspace_validation_classification_closed",
        ),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND length(addon_version) BETWEEN 1 AND 128 AND length(extension_types_json) BETWEEN 2 AND 4096",
            name="mcp_workspace_validation_identity_bounded",
        ),
        sa.CheckConstraint(
            "length(validated_at) = 27",
            name="mcp_workspace_validation_timestamp_format",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["agent_workspaces.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["development_source_id"], ["development_sources.id"], ondelete="RESTRICT"
        ),
    )

    op.create_table(
        "mcp_workspace_deployment_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("addon_version", sa.String(128), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("mutation_started", sa.Boolean(), nullable=False),
        sa.Column("target_stopped", sa.Boolean(), nullable=False),
        sa.Column("sync_started", sa.Boolean(), nullable=False),
        sa.Column("sync_completed", sa.Boolean(), nullable=False),
        sa.Column("target_started", sa.Boolean(), nullable=False),
        sa.Column("inventory_classification", sa.String(16), nullable=False),
        sa.Column("addon_enabled", sa.Boolean(), nullable=True),
        sa.Column("result_classification", sa.String(64), nullable=True),
        sa.Column("source_manifest_digest", sa.String(71), nullable=True),
        sa.Column("verified_destination_digest", sa.String(71), nullable=True),
        sa.Column("failure_code", sa.String(128), nullable=True),
        sa.Column("requested_at", sa.String(27), nullable=False),
        sa.Column("started_at", sa.String(27), nullable=True),
        sa.Column("completed_at", sa.String(27), nullable=True),
        sa.CheckConstraint(
            "length(id)=36 AND length(client_id)=36 AND length(request_id)=36 "
            "AND length(workspace_id)=36 AND length(development_source_id)=36 "
            "AND length(target_id)=36 AND length(runtime_revision_id)=36",
            name="mcp_workspace_deployment_uuid_lengths",
        ),
        sa.CheckConstraint(
            "generation >= 0", name="mcp_workspace_deployment_generation_nonnegative"
        ),
        sa.CheckConstraint(
            "state IN ('pending','running','succeeded','failed','ambiguous','reconciliation_required')",
            name="mcp_workspace_deployment_state_closed",
        ),
        sa.CheckConstraint(
            "phase IN ('claimed','preflight','stop','sync','start','readiness','inventory','finalize')",
            name="mcp_workspace_deployment_phase_closed",
        ),
        sa.CheckConstraint(
            "inventory_classification IN ('not_checked','matched','failed','unknown')",
            name="mcp_workspace_deployment_inventory_closed",
        ),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND length(addon_version) BETWEEN 1 AND 128",
            name="mcp_workspace_deployment_identity_bounded",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128",
            name="mcp_workspace_deployment_failure_bounded",
        ),
        sa.CheckConstraint(
            "(source_manifest_digest IS NULL OR (length(source_manifest_digest)=71 AND substr(source_manifest_digest,1,7)='sha256:')) AND (verified_destination_digest IS NULL OR (length(verified_destination_digest)=71 AND substr(verified_destination_digest,1,7)='sha256:'))",
            name="mcp_workspace_deployment_digests_valid",
        ),
        sa.CheckConstraint(
            "length(requested_at)=27 AND (started_at IS NULL OR length(started_at)=27) AND (completed_at IS NULL OR length(completed_at)=27)",
            name="mcp_workspace_deployment_timestamps_format",
        ),
        sa.CheckConstraint(
            "(state='pending' AND started_at IS NULL AND completed_at IS NULL) OR (state='running' AND started_at IS NOT NULL AND completed_at IS NULL) OR (state IN ('succeeded','failed','ambiguous','reconciliation_required') AND started_at IS NOT NULL AND completed_at IS NOT NULL)",
            name="mcp_workspace_deployment_state_timestamps",
        ),
        sa.CheckConstraint(
            "(sync_started=0 OR mutation_started=1) AND (sync_completed=0 OR sync_started=1) AND (target_started=0 OR sync_completed=1)",
            name="mcp_workspace_deployment_markers_ordered",
        ),
        sa.CheckConstraint(
            "state<>'succeeded' OR (sync_completed=1 AND target_started=1 AND inventory_classification='matched' AND addon_enabled IS NOT NULL AND result_classification IS NOT NULL AND source_manifest_digest IS NOT NULL AND verified_destination_digest IS NOT NULL AND failure_code IS NULL)",
            name="mcp_workspace_deployment_success_complete",
        ),
        sa.UniqueConstraint(
            "client_id", "request_id", name="uq_mcp_workspace_deployment_request"
        ),
    )
    op.create_index(
        "ix_mcp_workspace_deployment_workspace_requested",
        "mcp_workspace_deployment_operations",
        ["workspace_id", "requested_at"],
    )
    op.create_index(
        "ix_mcp_workspace_deployment_target_requested",
        "mcp_workspace_deployment_operations",
        ["target_id", "requested_at"],
    )
    for suffix, column in (
        ("client", "client_id"),
        ("workspace", "workspace_id"),
        ("target", "target_id"),
    ):
        op.create_index(
            f"uq_mcp_workspace_deployment_active_{suffix}",
            "mcp_workspace_deployment_operations",
            [column],
            unique=True,
            sqlite_where=sa.text("state IN ('pending','running')"),
        )

    op.create_table(
        "mcp_workspace_execution_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("client_id", sa.String(36), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("development_source_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("deployment_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("addon_version", sa.String(128), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("invocation_dispatched", sa.Boolean(), nullable=False),
        sa.Column("execution_classification", sa.String(32), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=True),
        sa.Column("result_classification", sa.String(64), nullable=True),
        sa.Column("failure_code", sa.String(128), nullable=True),
        sa.Column("requested_at", sa.String(27), nullable=False),
        sa.Column("started_at", sa.String(27), nullable=True),
        sa.Column("completed_at", sa.String(27), nullable=True),
        sa.CheckConstraint(
            "length(id)=36 AND length(client_id)=36 AND length(request_id)=36 AND length(workspace_id)=36 AND length(development_source_id)=36 AND length(target_id)=36 AND length(deployment_id)=36",
            name="mcp_workspace_execution_uuid_lengths",
        ),
        sa.CheckConstraint(
            "generation >= 0", name="mcp_workspace_execution_generation_nonnegative"
        ),
        sa.CheckConstraint(
            "state IN ('pending','running','succeeded','failed','ambiguous','reconciliation_required')",
            name="mcp_workspace_execution_state_closed",
        ),
        sa.CheckConstraint(
            "phase IN ('claimed','preflight','verify_pre','invoke','verify_post','finalize')",
            name="mcp_workspace_execution_phase_closed",
        ),
        sa.CheckConstraint(
            "execution_classification IN ('not_attempted','directory_result','jsonrpc_error','timeout','ambiguous')",
            name="mcp_workspace_execution_classification_closed",
        ),
        sa.CheckConstraint(
            "item_count IS NULL OR item_count BETWEEN 0 AND 1",
            name="mcp_workspace_execution_item_count_bounded",
        ),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND length(addon_version) BETWEEN 1 AND 128",
            name="mcp_workspace_execution_identity_bounded",
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 128",
            name="mcp_workspace_execution_failure_bounded",
        ),
        sa.CheckConstraint(
            "length(requested_at)=27 AND (started_at IS NULL OR length(started_at)=27) AND (completed_at IS NULL OR length(completed_at)=27)",
            name="mcp_workspace_execution_timestamps_format",
        ),
        sa.CheckConstraint(
            "(state='pending' AND started_at IS NULL AND completed_at IS NULL) OR (state='running' AND started_at IS NOT NULL AND completed_at IS NULL) OR (state IN ('succeeded','failed','ambiguous','reconciliation_required') AND started_at IS NOT NULL AND completed_at IS NOT NULL)",
            name="mcp_workspace_execution_state_timestamps",
        ),
        sa.CheckConstraint(
            "state<>'succeeded' OR (invocation_dispatched=1 AND execution_classification='directory_result' AND result_classification IS NOT NULL AND failure_code IS NULL)",
            name="mcp_workspace_execution_success_complete",
        ),
        sa.UniqueConstraint(
            "client_id", "request_id", name="uq_mcp_workspace_execution_request"
        ),
    )
    op.create_index(
        "ix_mcp_workspace_execution_workspace_requested",
        "mcp_workspace_execution_operations",
        ["workspace_id", "requested_at"],
    )
    op.create_index(
        "uq_mcp_workspace_execution_active_target",
        "mcp_workspace_execution_operations",
        ["target_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('pending','running')"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    populated = sum(
        int(bind.scalar(sa.text(f"SELECT COUNT(*) FROM {table}")) or 0)
        for table in (
            "mcp_workspace_validations",
            "mcp_workspace_deployment_operations",
            "mcp_workspace_execution_operations",
        )
    )
    slice_requests = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_development_requests "
            "WHERE operation_kind IN ('workspace_deploy','workspace_execute')"
        )
    )
    assigned = bind.scalar(
        sa.text("SELECT COUNT(*) FROM mcp_clients WHERE preset_id IN (:one,:two)"),
        {"one": PRESET_IDS[0], "two": PRESET_IDS[1]},
    )
    custom = bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM mcp_preset_permissions p "
            "JOIN mcp_presets s ON s.id=p.preset_id WHERE s.builtin=0 "
            "AND p.permission IN ('workspace.deploy','development.execute')"
        )
    )
    if populated or slice_requests or assigned or custom:
        raise RuntimeError("Cannot downgrade populated MCP Slice 4 state.")

    op.drop_index(
        "uq_mcp_workspace_execution_active_target",
        table_name="mcp_workspace_execution_operations",
    )
    op.drop_index(
        "ix_mcp_workspace_execution_workspace_requested",
        table_name="mcp_workspace_execution_operations",
    )
    op.drop_table("mcp_workspace_execution_operations")
    for suffix in ("target", "workspace", "client"):
        op.drop_index(
            f"uq_mcp_workspace_deployment_active_{suffix}",
            table_name="mcp_workspace_deployment_operations",
        )
    op.drop_index(
        "ix_mcp_workspace_deployment_target_requested",
        table_name="mcp_workspace_deployment_operations",
    )
    op.drop_index(
        "ix_mcp_workspace_deployment_workspace_requested",
        table_name="mcp_workspace_deployment_operations",
    )
    op.drop_table("mcp_workspace_deployment_operations")
    op.drop_table("mcp_workspace_validations")

    with op.batch_alter_table("mcp_development_requests", recreate="always") as batch:
        batch.drop_constraint("mcp_development_request_operation_shape", type_="check")
        batch.drop_constraint("mcp_development_request_operation_closed", type_="check")
        batch.drop_constraint("mcp_development_request_uuid_lengths", type_="check")
        batch.drop_column("operation_id")
        batch.alter_column(
            "creation_operation_id", existing_type=sa.String(36), nullable=False
        )
        batch.create_check_constraint(
            "mcp_development_request_operation_closed",
            "operation_kind = 'workspace_create'",
        )
        batch.create_check_constraint(
            "mcp_development_request_uuid_lengths",
            "length(id) = 36 AND length(client_id) = 36 AND length(request_id) = 36 AND length(development_source_id) = 36 AND length(workspace_id) = 36 AND length(creation_operation_id) = 36",
        )
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch:
        batch.drop_constraint("global_heavy_operation_kind", type_="check")
        batch.create_check_constraint("global_heavy_operation_kind", _HEAVY_KINDS_OLD)
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch:
        batch.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch.create_check_constraint(
            "managed_instance_mutation_kind", _MUTATION_KINDS_OLD
        )
    bind.execute(
        sa.text(
            "DELETE FROM mcp_preset_permissions WHERE "
            "preset_id IN (:one,:two) AND "
            "permission IN ('workspace.deploy','development.execute')"
        ),
        {"one": PRESET_IDS[0], "two": PRESET_IDS[1]},
    )
