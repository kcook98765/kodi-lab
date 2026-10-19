"""Durable guided reconnect bound to an existing external registration."""

import sqlalchemy as sa
from alembic import op

revision = "0075_external_reconnect"
down_revision = "0074_remote_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_reconnect_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "target_id",
            sa.String(36),
            sa.ForeignKey("external_kodi_targets.target_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("target_class", sa.String(32), nullable=False),
        sa.Column("bridge_port", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("native_binding", sa.String(64), nullable=False),
        sa.Column("disclosure_token", sa.String(64)),
        sa.Column("expected_bridge_revision", sa.Integer(), nullable=False),
        sa.Column("initial_bridge_reference", sa.String(36)),
        sa.Column("owned_bridge_reference", sa.String(36)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("updated_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "stage IN ('native_verified','requires_revoke','bootstrap_started',"
            "'paired','connected','blocked','cancelled')",
            name="external_reconnect_stage",
        ),
        sa.CheckConstraint(
            "bridge_port BETWEEN 1 AND 65535 AND length(native_binding)=64",
            name="external_reconnect_binding",
        ),
        sa.CheckConstraint(
            "target_class IN ('external_development','external_production')",
            name="external_reconnect_class",
        ),
    )
    op.create_index(
        "uq_external_reconnect_active_target",
        "external_reconnect_operations",
        ["target_id"],
        unique=True,
        sqlite_where=sa.text("stage NOT IN ('connected','cancelled')"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM external_reconnect_operations")
    ):
        raise RuntimeError("External reconnect history cannot be safely downgraded")
    op.drop_index(
        "uq_external_reconnect_active_target", "external_reconnect_operations"
    )
    op.drop_table("external_reconnect_operations")
