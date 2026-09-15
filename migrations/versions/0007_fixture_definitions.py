"""Add immutable fixture definitions and managed-instance assignments.

Revision ID: 0007_fixture_definitions
Revises: 0006_runtime_matrix_contract
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_fixture_definitions"
down_revision: str | None = "0006_runtime_matrix_contract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fixture_definitions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("fixture_version", sa.Integer(), nullable=False),
        sa.Column("size_category", sa.String(length=5), nullable=False),
        sa.Column("content_category", sa.String(length=5), nullable=False),
        sa.Column("content_digest", sa.String(length=71), nullable=False),
        sa.Column("compatible_kodi_version", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=False),
        sa.CheckConstraint("fixture_version > 0", name="fixture_version_positive"),
        sa.CheckConstraint("size_category IN ('small')", name="fixture_size_category"),
        sa.CheckConstraint(
            "content_category IN ('mixed')", name="fixture_content_category"
        ),
        sa.CheckConstraint(
            "length(content_digest) = 71 "
            "AND substr(content_digest, 1, 7) = 'sha256:' "
            "AND substr(content_digest, 8) NOT GLOB '*[^0-9a-f]*'",
            name="fixture_content_digest_sha256",
        ),
        sa.CheckConstraint(
            "length(trim(compatible_kodi_version)) BETWEEN 1 AND 32",
            name="fixture_compatible_release_bounded",
        ),
        sa.CheckConstraint(
            "length(trim(description)) BETWEEN 1 AND 512",
            name="fixture_description_bounded",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_fixture_definitions_slug"),
    )
    with op.batch_alter_table("managed_instances", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column("fixture_definition_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_managed_instance_fixture_definition",
            "fixture_definitions",
            ["fixture_definition_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    connection = op.get_bind()
    assigned = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM managed_instances "
            "WHERE fixture_definition_id IS NOT NULL"
        )
    )
    definitions = connection.scalar(sa.text("SELECT COUNT(*) FROM fixture_definitions"))
    if assigned or definitions:
        raise RuntimeError(
            "Cannot downgrade populated fixture definitions or assignments."
        )
    with op.batch_alter_table("managed_instances", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "fk_managed_instance_fixture_definition", type_="foreignkey"
        )
        batch_op.drop_column("fixture_definition_id")
    op.drop_table("fixture_definitions")
