"""Retain exact administrator review and one-shot source-copy approval."""

import sqlalchemy as sa
from alembic import op

revision = "0018_source_addition_reviews"
down_revision = "0017_system_snapshot_capability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_addition_reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("container_id", sa.String(64), nullable=False),
        sa.Column("proof_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.String(32), nullable=False),
        sa.Column("approved_at", sa.String(32)),
        sa.Column("operation_id", sa.String(36), unique=True),
        sa.Column("dispatched_at", sa.String(32)),
        sa.CheckConstraint(
            "length(proof_json) BETWEEN 1 AND 65536",
            name="source_addition_review_proof_bounded",
        ),
        sa.CheckConstraint(
            "(operation_id IS NULL AND approved_at IS NULL "
            "AND dispatched_at IS NULL) OR (operation_id IS NOT NULL "
            "AND approved_at IS NOT NULL)",
            name="source_addition_review_approval",
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM source_addition_reviews)")
    ):
        raise RuntimeError(
            "Source addition review history exists; refusing lossy downgrade"
        )
    op.drop_table("source_addition_reviews")
