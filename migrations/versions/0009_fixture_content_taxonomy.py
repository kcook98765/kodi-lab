"""Extend fixture content taxonomy with the Movies category.

Revision ID: 0009_fixture_content_taxonomy
Revises: 0008_reset_operations
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_fixture_content_taxonomy"
down_revision: str | None = "0008_reset_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("fixture_definitions", recreate="always") as batch_op:
        batch_op.drop_constraint("fixture_content_category", type_="check")
        batch_op.alter_column(
            "content_category",
            existing_type=sa.String(length=5),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "fixture_content_category",
            "content_category IN ('mixed', 'movies')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    movies = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM fixture_definitions WHERE content_category = 'movies'"
        )
    )
    if movies:
        raise RuntimeError(
            "Cannot downgrade fixture taxonomy with populated Movies definitions."
        )
    with op.batch_alter_table("fixture_definitions", recreate="always") as batch_op:
        batch_op.drop_constraint("fixture_content_category", type_="check")
        batch_op.alter_column(
            "content_category",
            existing_type=sa.String(length=16),
            type_=sa.String(length=5),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "fixture_content_category",
            "content_category IN ('mixed')",
        )
