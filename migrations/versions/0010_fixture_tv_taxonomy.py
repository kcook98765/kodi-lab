"""Extend fixture content taxonomy with the TV category.

Revision ID: 0010_fixture_tv_taxonomy
Revises: 0009_fixture_content_taxonomy
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_fixture_tv_taxonomy"
down_revision: str | None = "0009_fixture_content_taxonomy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("fixture_definitions", recreate="always") as batch_op:
        batch_op.drop_constraint("fixture_content_category", type_="check")
        batch_op.create_check_constraint(
            "fixture_content_category",
            "content_category IN ('mixed', 'movies', 'tv')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    tv_definitions = connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM fixture_definitions WHERE content_category = 'tv'"
        )
    )
    if tv_definitions:
        raise RuntimeError(
            "Cannot downgrade fixture taxonomy with populated TV definitions."
        )
    with op.batch_alter_table("fixture_definitions", recreate="always") as batch_op:
        batch_op.drop_constraint("fixture_content_category", type_="check")
        batch_op.create_check_constraint(
            "fixture_content_category",
            "content_category IN ('mixed', 'movies')",
        )
