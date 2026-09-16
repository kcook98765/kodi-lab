"""Add the confined local-directory development-source catalog.

Revision ID: 0017_development_sources
Revises: 0016_lifecycle_history
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_development_sources"
down_revision: str | Sequence[str] | None = "0016_lifecycle_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "development_sources",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("logical_name", sa.String(length=120), nullable=False),
        sa.Column(
            "source_kind",
            sa.Enum(
                "local_directory",
                name="development_source_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("relative_locator", sa.String(length=1024), nullable=False),
        sa.Column("addon_id", sa.String(length=128), nullable=False),
        sa.Column("observed_name", sa.String(length=256), nullable=False),
        sa.Column("observed_version", sa.String(length=128), nullable=False),
        sa.Column("extension_types_json", sa.Text(), nullable=False),
        sa.Column("registered_at", sa.String(length=32), nullable=False),
        sa.Column("validated_at", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "length(logical_name) BETWEEN 1 AND 120 "
            "AND logical_name = trim(logical_name)",
            name="development_source_logical_name_bounded",
        ),
        sa.CheckConstraint(
            "length(relative_locator) BETWEEN 1 AND 1024 "
            "AND substr(relative_locator, 1, 1) <> '/' "
            "AND instr(relative_locator, '\\') = 0 "
            "AND instr(relative_locator, '//') = 0 "
            "AND relative_locator <> '.' AND relative_locator <> '..' "
            "AND relative_locator NOT LIKE './%' "
            "AND relative_locator NOT LIKE '../%' "
            "AND relative_locator NOT LIKE '%/./%' "
            "AND relative_locator NOT LIKE '%/../%' "
            "AND relative_locator NOT LIKE '%/.' "
            "AND relative_locator NOT LIKE '%/..'",
            name="development_source_relative_locator_safe",
        ),
        sa.CheckConstraint(
            "length(addon_id) BETWEEN 1 AND 128 "
            "AND addon_id = lower(addon_id) "
            "AND addon_id = trim(addon_id) "
            "AND instr(addon_id, '/') = 0 AND instr(addon_id, '\\') = 0 "
            "AND instr(addon_id, '..') = 0",
            name="development_source_addon_id_bounded",
        ),
        sa.CheckConstraint(
            "length(observed_name) BETWEEN 1 AND 256",
            name="development_source_observed_name_bounded",
        ),
        sa.CheckConstraint(
            "length(observed_version) BETWEEN 1 AND 128",
            name="development_source_observed_version_bounded",
        ),
        sa.CheckConstraint(
            "length(extension_types_json) BETWEEN 2 AND 8192",
            name="development_source_extensions_bounded",
        ),
        sa.CheckConstraint(
            "length(registered_at) BETWEEN 20 AND 32 "
            "AND length(validated_at) BETWEEN 20 AND 32",
            name="development_source_timestamps_bounded",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("logical_name", name="uq_development_source_logical_name"),
        sa.UniqueConstraint(
            "relative_locator", name="uq_development_source_relative_locator"
        ),
        sa.UniqueConstraint("addon_id", name="uq_development_source_addon_id"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM development_sources")):
        raise RuntimeError("Cannot downgrade populated development-source catalog.")
    op.drop_table("development_sources")
