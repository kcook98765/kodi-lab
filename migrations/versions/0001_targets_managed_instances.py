"""Create targets and managed instances.

Revision ID: 0001_targets_instances
Revises:
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_targets_instances"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "targets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "target_type",
            sa.Enum(
                "managed",
                name="target_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("kodi_version", sa.Text(), nullable=False),
        sa.Column(
            "availability",
            sa.Enum(
                "available",
                "unavailable",
                name="target_availability",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "health",
            sa.Enum(
                "healthy",
                "warning",
                "error",
                name="health_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "connection_state",
            sa.Enum(
                "connected",
                "disconnected",
                name="connection_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(kodi_version)) > 0",
            name="target_kodi_version_not_blank",
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="target_name_not_blank"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "managed_instances",
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column(
            "instance_type",
            sa.Enum(
                "disposable",
                "persistent",
                name="instance_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("runtime_revision", sa.Text(), nullable=False),
        sa.Column(
            "runtime_state",
            sa.Enum(
                "running",
                "stopped",
                name="instance_runtime_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "start_on_host_boot",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "length(trim(runtime_revision)) > 0",
            name="managed_instance_runtime_revision_not_blank",
        ),
        sa.ForeignKeyConstraint(["target_id"], ["targets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("target_id"),
    )


def downgrade() -> None:
    op.drop_table("managed_instances")
    op.drop_table("targets")
