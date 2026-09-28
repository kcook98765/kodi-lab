"""Durable reviewed binary-package recipe installations.

Revision ID: 0054_runtime_recipe_installations
Revises: 0053_production_screenshot_capability_amendment
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054_runtime_recipe_installations"
down_revision = "0053_production_screenshot_capability_amendment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_recipe_installations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("recipe_id", sa.String(64), nullable=False),
        sa.Column("recipe_revision", sa.Integer(), nullable=False),
        sa.Column("recipe_digest", sa.String(71), nullable=False),
        sa.Column("generation", sa.String(32), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "pending",
                "acquiring",
                "assembling",
                "validating",
                "ready",
                "failed",
                name="runtimerecipeinstallationstate",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("updated_at", sa.String(27), nullable=False),
        sa.Column("completed_at", sa.String(27), nullable=True),
        sa.Column("image_id", sa.String(71), nullable=True),
        sa.Column(
            "runtime_revision_id",
            sa.String(36),
            sa.ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("attestation_json", sa.Text(), nullable=True),
        sa.Column("validation_json", sa.Text(), nullable=True),
        sa.Column("failure_code", sa.String(48), nullable=True),
        sa.CheckConstraint(
            "recipe_revision > 0", name="runtime_recipe_revision_positive"
        ),
        sa.CheckConstraint(
            "length(recipe_digest)=71 AND substr(recipe_digest,1,7)='sha256:'",
            name="runtime_recipe_digest_shape",
        ),
        sa.CheckConstraint(
            "state IN ('pending','acquiring','assembling',"
            "'validating','ready','failed')",
            name="runtime_recipe_install_state_closed",
        ),
        sa.CheckConstraint(
            "(state='ready' AND completed_at IS NOT NULL AND image_id IS NOT NULL "
            "AND runtime_revision_id IS NOT NULL AND attestation_json IS NOT NULL "
            "AND validation_json IS NOT NULL AND failure_code IS NULL) OR "
            "(state='failed' AND completed_at IS NOT NULL AND failure_code IS NOT NULL "
            "AND runtime_revision_id IS NULL) OR "
            "(state IN ('pending','acquiring','assembling','validating') "
            "AND completed_at IS NULL AND runtime_revision_id IS NULL "
            "AND failure_code IS NULL)",
            name="runtime_recipe_install_terminal_consistent",
        ),
        sa.CheckConstraint(
            "attestation_json IS NULL OR length(attestation_json) BETWEEN 1 AND 65536",
            name="runtime_recipe_attestation_bounded",
        ),
        sa.CheckConstraint(
            "validation_json IS NULL OR length(validation_json) BETWEEN 1 AND 8192",
            name="runtime_recipe_validation_bounded",
        ),
    )
    op.create_index(
        "uq_runtime_recipe_install_active_generation",
        "runtime_recipe_installations",
        ["generation"],
        unique=True,
        sqlite_where=sa.text(
            "state IN ('pending','acquiring','assembling','validating')"
        ),
    )
    op.create_index(
        "ix_runtime_recipe_install_revision",
        "runtime_recipe_installations",
        ["runtime_revision_id"],
    )
    op.execute(
        "CREATE TRIGGER trg_runtime_recipe_install_terminal_immutable BEFORE UPDATE "
        "ON runtime_recipe_installations WHEN OLD.state IN ('ready','failed') "
        "BEGIN SELECT RAISE(ABORT, 'runtime_recipe_install_terminal_immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER trg_runtime_recipe_install_no_delete BEFORE DELETE "
        "ON runtime_recipe_installations BEGIN SELECT "
        "RAISE(ABORT, 'runtime_recipe_install_no_delete'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_runtime_recipe_install_no_delete")
    op.execute("DROP TRIGGER trg_runtime_recipe_install_terminal_immutable")
    op.drop_index(
        "ix_runtime_recipe_install_revision", table_name="runtime_recipe_installations"
    )
    op.drop_index(
        "uq_runtime_recipe_install_active_generation",
        table_name="runtime_recipe_installations",
    )
    op.drop_table("runtime_recipe_installations")
