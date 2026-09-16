"""Extend fixture size taxonomy with the Medium category.

Revision ID: 0011_fixture_medium_taxonomy
Revises: 0010_fixture_tv_taxonomy
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_fixture_medium_taxonomy"
down_revision: str | None = "0010_fixture_tv_taxonomy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("fixture_definitions", recreate="always") as batch_op:
        batch_op.drop_constraint("fixture_size_category", type_="check")
        batch_op.alter_column(
            "size_category",
            existing_type=sa.String(length=5),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "fixture_size_category",
            "size_category IN ('small', 'medium')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    medium_definitions = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM fixture_definitions WHERE size_category = 'medium'"
        )
    )
    if medium_definitions:
        raise RuntimeError(
            "Cannot downgrade fixture taxonomy with populated Medium definitions."
        )
    with op.batch_alter_table("fixture_definitions", recreate="always") as batch_op:
        batch_op.drop_constraint("fixture_size_category", type_="check")
        batch_op.alter_column(
            "size_category",
            existing_type=sa.String(length=16),
            type_=sa.String(length=5),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "fixture_size_category",
            "size_category IN ('small')",
        )
