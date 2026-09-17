"""Add durable per-instance development-source attachments and typed history.

Revision ID: 0018_development_source_attach
Revises: 0017_development_sources
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_development_source_attach"
down_revision: str | Sequence[str] | None = "0017_development_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch_op.alter_column(
            "operation_kind",
            existing_type=sa.String(length=20),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ('reset','snapshot_create','snapshot_restore',"
            "'lifecycle','fixture_assignment','runtime_reassignment','instance_delete',"
            "'development_source_attachment')",
        )

    op.create_table(
        "managed_instance_development_sources",
        sa.Column("managed_instance_id", sa.String(length=36), nullable=False),
        sa.Column("development_source_id", sa.String(length=36), nullable=False),
        sa.Column("attached_at", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "length(attached_at) BETWEEN 20 AND 32",
            name="development_source_attachment_timestamp_bounded",
        ),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["development_source_id"],
            ["development_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("managed_instance_id", "development_source_id"),
        sa.UniqueConstraint(
            "managed_instance_id",
            "development_source_id",
            name="uq_managed_instance_development_source",
        ),
    )
    op.create_index(
        "ix_managed_instance_development_source_source",
        "managed_instance_development_sources",
        ["development_source_id"],
        unique=False,
    )

    op.create_table(
        "development_source_attachment_operations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("original_managed_instance_id", sa.String(length=36), nullable=False),
        sa.Column("managed_instance_id", sa.String(length=36), nullable=True),
        sa.Column("development_source_id", sa.String(length=36), nullable=False),
        sa.Column("source_logical_name", sa.String(length=120), nullable=False),
        sa.Column("addon_id", sa.String(length=128), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "attach",
                "detach",
                name="development_source_attachment_operation_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("requested_at", sa.String(length=32), nullable=False),
        sa.Column("completed_at", sa.String(length=32), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "succeeded",
                "failed",
                name="development_source_attachment_operation_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("failure_reason", sa.String(length=256), nullable=True),
        sa.CheckConstraint(
            "length(source_logical_name) BETWEEN 1 AND 120",
            name="source_attachment_history_name_bounded",
        ),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128",
            name="source_attachment_history_addon_id_bounded",
        ),
        sa.CheckConstraint(
            "length(requested_at) BETWEEN 20 AND 32 "
            "AND length(completed_at) BETWEEN 20 AND 32",
            name="source_attachment_history_timestamps_bounded",
        ),
        sa.CheckConstraint(
            "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 256",
            name="source_attachment_history_reason_bounded",
        ),
        sa.CheckConstraint(
            "(state = 'succeeded' AND failure_reason IS NULL) OR "
            "(state = 'failed' AND failure_reason IS NOT NULL)",
            name="source_attachment_history_state_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["development_source_id"],
            ["development_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_source_attachment_history_original_requested",
        "development_source_attachment_operations",
        ["original_managed_instance_id", "requested_at"],
        unique=False,
    )
    op.create_index(
        "ix_source_attachment_history_source",
        "development_source_attachment_operations",
        ["development_source_id"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    for table in (
        "managed_instance_development_sources",
        "development_source_attachment_operations",
    ):
        if connection.scalar(sa.text(f"SELECT COUNT(*) FROM {table}")):
            raise RuntimeError(
                "Cannot downgrade populated development-source attachment data."
            )
    if connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM managed_instance_mutation_leases "
            "WHERE operation_kind = 'development_source_attachment'"
        )
    ):
        raise RuntimeError(
            "Cannot downgrade active development-source attachment authority."
        )

    op.drop_index(
        "ix_source_attachment_history_source",
        table_name="development_source_attachment_operations",
    )
    op.drop_index(
        "ix_source_attachment_history_original_requested",
        table_name="development_source_attachment_operations",
    )
    op.drop_table("development_source_attachment_operations")
    op.drop_index(
        "ix_managed_instance_development_source_source",
        table_name="managed_instance_development_sources",
    )
    op.drop_table("managed_instance_development_sources")

    with op.batch_alter_table(
        "managed_instance_mutation_leases", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint("managed_instance_mutation_kind", type_="check")
        batch_op.alter_column(
            "operation_kind",
            existing_type=sa.String(length=32),
            type_=sa.String(length=20),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "managed_instance_mutation_kind",
            "operation_kind IN ('reset','snapshot_create','snapshot_restore',"
            "'lifecycle','fixture_assignment','runtime_reassignment','instance_delete')",
        )
