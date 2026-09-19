"""Add latest-only managed screenshot metadata.

Revision ID: 0035_managed_screenshots
Revises: 0034_phase9_completion_remediation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_managed_screenshots"
down_revision: str | Sequence[str] | None = "0034_phase9_completion_remediation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "managed_screenshots",
        sa.Column("managed_instance_id", sa.String(36), nullable=False),
        sa.Column("capture_id", sa.String(36), nullable=True),
        sa.Column("captured_at", sa.String(32), nullable=True),
        sa.Column("mime_type", sa.String(16), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("last_attempt_at", sa.String(32), nullable=False),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "mime_type = 'image/png'", name="managed_screenshot_png_only"
        ),
        sa.CheckConstraint(
            "width BETWEEN 1 AND 4096 AND height BETWEEN 1 AND 2160",
            name="managed_screenshot_dimensions_bounded",
        ),
        sa.CheckConstraint(
            "byte_size BETWEEN 1 AND 8388608",
            name="managed_screenshot_size_bounded",
        ),
        sa.CheckConstraint(
            "length(captured_at) BETWEEN 20 AND 32 AND "
            "length(last_attempt_at) BETWEEN 20 AND 32",
            name="managed_screenshot_timestamps_bounded",
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR length(last_error_code) BETWEEN 1 AND 64",
            name="managed_screenshot_error_bounded",
        ),
        sa.CheckConstraint(
            "(capture_id IS NULL AND captured_at IS NULL AND mime_type IS NULL "
            "AND width IS NULL AND height IS NULL AND byte_size IS NULL) OR "
            "(capture_id IS NOT NULL AND captured_at IS NOT NULL "
            "AND mime_type IS NOT NULL AND width IS NOT NULL "
            "AND height IS NOT NULL AND byte_size IS NOT NULL)",
            name="managed_screenshot_current_complete",
        ),
        sa.ForeignKeyConstraint(
            ["managed_instance_id"],
            ["managed_instances.target_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("managed_instance_id"),
        sa.UniqueConstraint("capture_id", name="uq_managed_screenshot_capture"),
    )


def downgrade() -> None:
    count = op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM managed_screenshots"))
    if count:
        raise RuntimeError(
            "Cannot downgrade populated managed screenshot metadata without "
            "orphaning private cache bytes."
        )
    op.drop_table("managed_screenshots")
