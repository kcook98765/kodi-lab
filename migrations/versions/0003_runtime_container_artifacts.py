"""Add immutable runtime container artifact mappings.

Revision ID: 0003_runtime_container_artifacts
Revises: 0002_runtime_catalog
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_runtime_container_artifacts"
down_revision: str | Sequence[str] | None = "0002_runtime_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_container_artifacts",
        sa.Column("runtime_revision_id", sa.String(length=36), nullable=False),
        sa.Column("image_id", sa.String(length=71), nullable=False),
        sa.CheckConstraint(
            "length(image_id) = 71 AND substr(image_id, 1, 7) = 'sha256:' "
            "AND substr(image_id, 8) NOT GLOB '*[^0-9a-f]*'",
            name="runtime_container_artifact_image_id_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_revision_id"],
            ["runtime_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("runtime_revision_id"),
    )


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM runtime_container_artifacts")
    )
    if count:
        raise RuntimeError(
            "Cannot downgrade a populated runtime-container artifact mapping. "
            "Remove the development database only if this immutable mapping is "
            "intentionally being discarded."
        )
    op.drop_table("runtime_container_artifacts")
