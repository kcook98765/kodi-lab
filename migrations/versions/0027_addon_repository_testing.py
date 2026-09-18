"""Add external HTTPS repository testing and repository artifact lineage.

Revision ID: 0027_addon_repository_testing
Revises: 0026_addon_zip_smoke_test
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0027_addon_repository_testing"
down_revision: str | Sequence[str] | None = "0026_addon_zip_smoke_test"
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
            "'addon_zip_installation','addon_zip_enablement','addon_zip_smoke_test',"
            "'addon_repository_attachment','addon_repository_installation')",
        )
    with op.batch_alter_table(
        "global_heavy_operation_lease", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("global_heavy_operation_kind", type_="check")
        batch_op.create_check_constraint(
            "global_heavy_operation_kind",
            "operation_kind IN ('snapshot_create','snapshot_restore','snapshot_delete',"
            "'instance_delete','development_source_deployment','addon_zip_import',"
            "'addon_zip_installation','addon_repository_refresh',"
            "'addon_repository_fetch','addon_repository_installation')",
        )

    op.create_table(
        "addon_repository_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("logical_name", sa.String(120), nullable=False),
        sa.Column("canonical_index_url", sa.String(2304), nullable=False),
        sa.Column(
            "origin",
            sa.Enum(
                "external_https_repository",
                name="addon_repository_origin",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "length(logical_name) BETWEEN 1 AND 120",
            name="addon_repository_logical_name_bounded",
        ),
        sa.CheckConstraint(
            "length(canonical_index_url) BETWEEN 1 AND 2304",
            name="addon_repository_url_bounded",
        ),
        sa.UniqueConstraint("logical_name", name="uq_addon_repository_logical_name"),
        sa.UniqueConstraint(
            "canonical_index_url", name="uq_addon_repository_canonical_url"
        ),
    )
    op.create_table(
        "managed_instance_addon_repository_sources",
        sa.Column("managed_instance_id", sa.String(36), primary_key=True),
        sa.Column("repository_source_id", sa.String(36), primary_key=True),
        sa.Column("attached_at", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository_source_id"],
            ["addon_repository_sources.id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "addon_repository_attachment_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("original_managed_instance_id", sa.String(36), nullable=False),
        sa.Column("managed_instance_id", sa.String(36)),
        sa.Column("repository_source_id", sa.String(36), nullable=False),
        sa.Column("repository_logical_name", sa.String(120), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "attach",
                "detach",
                name="addon_repository_attachment_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column("completed_at", sa.String(32), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "succeeded",
                "failed",
                name="addon_repository_attachment_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("failure_reason", sa.String(128)),
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
        sa.CheckConstraint(
            "length(repository_logical_name) BETWEEN 1 AND 120",
            name="addon_repository_attachment_name_bounded",
        ),
        sa.CheckConstraint(
            "(state='succeeded' AND failure_reason IS NULL) OR "
            "(state='failed' AND failure_reason IS NOT NULL)",
            name="addon_repository_attachment_state_consistent",
        ),
    )
    op.create_index(
        "ix_addon_repository_attachment_original_requested",
        "addon_repository_attachment_operations",
        ["original_managed_instance_id", "requested_at"],
    )
    op.create_table(
        "addon_repository_metadata_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("repository_source_id", sa.String(36), nullable=False),
        sa.Column("fetched_at", sa.String(32), nullable=False),
        sa.Column("transfer_digest", sa.String(71), nullable=False),
        sa.Column("canonical_metadata_digest", sa.String(71), nullable=False),
        sa.Column("transfer_size", sa.BigInteger(), nullable=False),
        sa.Column("expanded_size", sa.BigInteger(), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_source_id"],
            ["addon_repository_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "transfer_size BETWEEN 1 AND 8388608 AND "
            "expanded_size BETWEEN 1 AND 33554432 AND entry_count BETWEEN 0 AND 2048",
            name="addon_repository_snapshot_aggregates_bounded",
        ),
    )
    op.create_table(
        "addon_repository_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("snapshot_id", sa.String(36), nullable=False),
        sa.Column("repository_source_id", sa.String(36), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column(
            "classification",
            sa.Enum(
                "python_video_plugin",
                "unsupported",
                name="addon_repository_candidate_classification",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("dependencies_json", sa.Text(), nullable=False),
        sa.Column(
            "support",
            sa.Enum(
                "supported",
                "unsupported",
                name="addon_repository_candidate_support",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("unsupported_reason", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["addon_repository_metadata_snapshots.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["repository_source_id"],
            ["addon_repository_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "snapshot_id", "addon_id", "version", name="uq_addon_repository_candidate"
        ),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 AND length(name) BETWEEN 1 AND 256 "
            "AND length(version) BETWEEN 1 AND 128",
            name="addon_repository_candidate_identity_bounded",
        ),
        sa.CheckConstraint(
            "unsupported_reason IS NULL OR length(unsupported_reason) BETWEEN 1 AND 64",
            name="addon_repository_candidate_reason_bounded",
        ),
    )
    _create_operation_table("refresh")
    _create_operation_table("fetch")

    with op.batch_alter_table("addon_zip_artifacts", recreate="always") as batch_op:
        batch_op.drop_constraint("addon_zip_origin", type_="check")
        batch_op.alter_column(
            "origin",
            existing_type=sa.String(12),
            type_=sa.Enum(
                "uploaded_zip",
                "repository_download",
                name="addon_zip_origin",
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column("repository_source_id", sa.String(36)))
        batch_op.add_column(sa.Column("repository_snapshot_id", sa.String(36)))
        batch_op.add_column(sa.Column("repository_candidate_id", sa.String(36)))
        batch_op.add_column(sa.Column("repository_fetched_at", sa.String(32)))
        batch_op.create_foreign_key(
            "fk_addon_zip_repository_source",
            "addon_repository_sources",
            ["repository_source_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_addon_zip_repository_snapshot",
            "addon_repository_metadata_snapshots",
            ["repository_snapshot_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_addon_zip_repository_candidate",
            "addon_repository_candidates",
            ["repository_candidate_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "addon_zip_artifact_repository_lineage",
            "(origin='uploaded_zip' AND repository_source_id IS NULL AND "
            "repository_snapshot_id IS NULL AND repository_candidate_id IS NULL AND "
            "repository_fetched_at IS NULL) OR (origin='repository_download' AND "
            "repository_source_id IS NOT NULL AND repository_snapshot_id IS NOT NULL "
            "AND repository_candidate_id IS NOT NULL AND "
            "repository_fetched_at IS NOT NULL)",
        )
    with op.batch_alter_table(
        "addon_zip_installation_evidence", recreate="always"
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "artifact_origin",
                sa.String(19),
                nullable=False,
                server_default="uploaded_zip",
            )
        )
        batch_op.add_column(sa.Column("repository_source_id", sa.String(36)))
        batch_op.add_column(sa.Column("repository_snapshot_id", sa.String(36)))
        batch_op.add_column(sa.Column("repository_candidate_id", sa.String(36)))
        batch_op.create_foreign_key(
            "fk_addon_zip_evidence_repository_source",
            "addon_repository_sources",
            ["repository_source_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_addon_zip_evidence_repository_snapshot",
            "addon_repository_metadata_snapshots",
            ["repository_snapshot_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_addon_zip_evidence_repository_candidate",
            "addon_repository_candidates",
            ["repository_candidate_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "addon_zip_evidence_origin",
            "artifact_origin IN ('uploaded_zip','repository_download')",
        )
        batch_op.create_check_constraint(
            "addon_zip_evidence_repository_lineage",
            "(artifact_origin='uploaded_zip' AND repository_source_id IS NULL AND "
            "repository_snapshot_id IS NULL AND repository_candidate_id IS NULL) OR "
            "(artifact_origin='repository_download' AND repository_source_id IS NOT "
            "NULL AND repository_snapshot_id IS NOT NULL AND "
            "repository_candidate_id IS NOT NULL)",
        )


def _create_operation_table(kind: str) -> None:
    table = f"addon_repository_{kind}_operations"
    columns: list[Any] = [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("repository_source_id", sa.String(36), nullable=False),
    ]
    if kind == "refresh":
        columns.append(sa.Column("snapshot_id", sa.String(36), nullable=True))
        phases = (
            "queued",
            "resolving",
            "fetching",
            "parsing",
            "publishing",
            "finalize",
        )
    else:
        columns.extend(
            [
                sa.Column("snapshot_id", sa.String(36), nullable=False),
                sa.Column("candidate_id", sa.String(36), nullable=False),
                sa.Column("artifact_id", sa.String(36), nullable=False, unique=True),
            ]
        )
        phases = (
            "queued",
            "resolving",
            "fetching",
            "validating",
            "publishing",
            "finalize",
        )
    columns.extend(
        [
            sa.Column("requested_at", sa.String(32), nullable=False),
            sa.Column(
                "state",
                sa.Enum(
                    "queued",
                    "running",
                    "succeeded",
                    "failed",
                    name=f"addon_repository_{kind}_operation_state",
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=False,
            ),
            sa.Column(
                "phase",
                sa.Enum(
                    *phases,
                    name=f"addon_repository_{kind}_phase",
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=False,
            ),
            sa.Column("started_at", sa.String(32)),
            sa.Column("completed_at", sa.String(32)),
            sa.Column("failure_reason", sa.String(128)),
            sa.Column("lease_token_digest", sa.String(64)),
            sa.Column("lease_expires_at", sa.String(32)),
            sa.ForeignKeyConstraint(
                ["repository_source_id"],
                ["addon_repository_sources.id"],
                ondelete="RESTRICT",
            ),
        ]
    )
    if kind == "refresh":
        columns.append(
            sa.ForeignKeyConstraint(
                ["snapshot_id"],
                ["addon_repository_metadata_snapshots.id"],
                ondelete="RESTRICT",
            )
        )
    else:
        columns.extend(
            [
                sa.ForeignKeyConstraint(
                    ["snapshot_id"],
                    ["addon_repository_metadata_snapshots.id"],
                    ondelete="RESTRICT",
                ),
                sa.ForeignKeyConstraint(
                    ["candidate_id"],
                    ["addon_repository_candidates.id"],
                    ondelete="RESTRICT",
                ),
            ]
        )
    op.create_table(table, *columns)
    op.create_index(
        f"uq_addon_repository_{kind}_active",
        table,
        ["repository_source_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued','running')"),
    )


def downgrade() -> None:
    populated = op.get_bind().scalar(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM addon_repository_sources) + "
            "(SELECT COUNT(*) FROM addon_repository_attachment_operations) + "
            "(SELECT COUNT(*) FROM addon_repository_metadata_snapshots) + "
            "(SELECT COUNT(*) FROM addon_repository_candidates) + "
            "(SELECT COUNT(*) FROM addon_repository_refresh_operations) + "
            "(SELECT COUNT(*) FROM addon_repository_fetch_operations) + "
            "(SELECT COUNT(*) FROM addon_zip_artifacts "
            "WHERE origin='repository_download')"
        )
    )
    if populated:
        raise RuntimeError("Cannot downgrade populated repository state or history.")
    with op.batch_alter_table(
        "addon_zip_installation_evidence", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("addon_zip_evidence_repository_lineage", type_="check")
        batch_op.drop_constraint("addon_zip_evidence_origin", type_="check")
        batch_op.drop_constraint(
            "fk_addon_zip_evidence_repository_candidate", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_addon_zip_evidence_repository_snapshot", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_addon_zip_evidence_repository_source", type_="foreignkey"
        )
        batch_op.drop_column("repository_candidate_id")
        batch_op.drop_column("repository_snapshot_id")
        batch_op.drop_column("repository_source_id")
        batch_op.drop_column("artifact_origin")
    with op.batch_alter_table("addon_zip_artifacts", recreate="always") as batch_op:
        batch_op.drop_constraint("addon_zip_artifact_repository_lineage", type_="check")
        batch_op.drop_constraint(
            "fk_addon_zip_repository_candidate", type_="foreignkey"
        )
        batch_op.drop_constraint("fk_addon_zip_repository_snapshot", type_="foreignkey")
        batch_op.drop_constraint("fk_addon_zip_repository_source", type_="foreignkey")
        batch_op.drop_column("repository_fetched_at")
        batch_op.drop_column("repository_candidate_id")
        batch_op.drop_column("repository_snapshot_id")
        batch_op.drop_column("repository_source_id")
        batch_op.drop_constraint("addon_zip_origin", type_="check")
        batch_op.alter_column(
            "origin",
            existing_type=sa.String(19),
            type_=sa.Enum(
                "uploaded_zip",
                name="addon_zip_origin",
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=False,
        )
    op.drop_table("addon_repository_fetch_operations")
    op.drop_table("addon_repository_refresh_operations")
    op.drop_table("addon_repository_candidates")
    op.drop_table("addon_repository_metadata_snapshots")
    op.drop_table("addon_repository_attachment_operations")
    op.drop_table("managed_instance_addon_repository_sources")
    op.drop_table("addon_repository_sources")
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
