"""Independent exact-target Web Bridge maintenance receipts."""

import sqlalchemy as sa
from alembic import op

from kodi_lab.storage.bridge_preparation_sql import (
    BRIDGE_CHILD_IMMUTABLE,
    BRIDGE_PREPARATION_IMMUTABLE,
    CREATE_BRIDGE_IMMUTABLE,
)

revision = "0008_managed_bridge_preparation"
down_revision = "0007_production_navigation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive only: retain all original native-only receipt meanings.
    op.execute(
        "ALTER TABLE create_kodi_operations ADD COLUMN bridge_required BOOLEAN "
        "DEFAULT '0' NOT NULL CONSTRAINT create_bridge_required CHECK "
        "(bridge_required IN (0,1))"
    )
    op.create_table(
        "managed_bridge_preparations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False, unique=True),
        sa.Column("create_operation_id", sa.String(36), unique=True),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("container_id", sa.String(64), nullable=False),
        sa.Column("image_id", sa.String(71), nullable=False),
        sa.Column("runtime_state_revision", sa.Integer(), nullable=False),
        sa.Column("keep_running", sa.Boolean(), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("checkpoint", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("updated_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "phase IN ('start','bridge','stop','complete')",
            name="bridge_preparation_phase",
        ),
        sa.CheckConstraint(
            "checkpoint IN ('pending','dispatched','needs_attention','complete')",
            name="bridge_preparation_checkpoint",
        ),
        sa.CheckConstraint(
            "keep_running IN (0,1) AND version>=0 AND attempt>=0 AND "
            "runtime_state_revision>=0",
            name="bridge_preparation_options",
        ),
        sa.CheckConstraint(
            "(phase='complete') = (checkpoint='complete')",
            name="bridge_preparation_completion",
        ),
    )
    op.create_index(
        "uq_bridge_preparation_active",
        "managed_bridge_preparations",
        ["target_id"],
        unique=True,
        sqlite_where=sa.text("checkpoint NOT IN ('complete','needs_attention')"),
    )
    op.create_table(
        "managed_bridge_preparation_children",
        sa.Column(
            "operation_id",
            sa.String(36),
            sa.ForeignKey(
                "managed_instance_lifecycle_operations.id", ondelete="RESTRICT"
            ),
            primary_key=True,
        ),
        sa.Column(
            "preparation_id",
            sa.String(36),
            sa.ForeignKey("managed_bridge_preparations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "preparation_id", "phase", "attempt", name="bridge_preparation_child_once"
        ),
        sa.CheckConstraint(
            "phase IN ('start','bridge','stop') AND attempt>=0",
            name="bridge_preparation_child_phase",
        ),
    )
    op.execute(BRIDGE_PREPARATION_IMMUTABLE)
    op.execute(BRIDGE_CHILD_IMMUTABLE)
    op.execute(CREATE_BRIDGE_IMMUTABLE)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM managed_bridge_preparations")
    ) or op.get_bind().scalar(
        sa.text("SELECT count(*) FROM create_kodi_operations WHERE bridge_required=1")
    ):
        raise RuntimeError(
            "Bridge preparation receipts exist; restore a coordinated backup"
        )
    op.execute("DROP TRIGGER create_bridge_required_immutable")
    op.execute("DROP TRIGGER bridge_preparation_immutable")
    op.execute("DROP TRIGGER bridge_preparation_child_immutable")
    op.drop_table("managed_bridge_preparation_children")
    op.drop_table("managed_bridge_preparations")
    op.drop_column("create_kodi_operations", "bridge_required")
