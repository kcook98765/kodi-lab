"""Durable experimental noVNC ownership and cross-process input arbitration."""

import sqlalchemy as sa
from alembic import op

revision = "0087_novnc_control"
down_revision = "0086_addon_settings"
branch_labels = None
depends_on = None

NOVNC_INPUT_TRIGGERS = (
    "CREATE TRIGGER novnc_control_insert BEFORE INSERT ON novnc_sessions "
    "WHEN NEW.mode='interactive' AND (EXISTS(SELECT 1 FROM "
    "managed_instance_mutation_leases WHERE managed_instance_id=NEW.instance_id) "
    "OR EXISTS(SELECT 1 FROM managed_remote_guards WHERE instance_id=NEW.instance_id)) "
    "BEGIN SELECT RAISE(ABORT,'managed input conflict'); END",
    "CREATE TRIGGER novnc_control_update BEFORE UPDATE OF mode ON novnc_sessions "
    "WHEN NEW.mode='interactive' AND (EXISTS(SELECT 1 FROM "
    "managed_instance_mutation_leases WHERE managed_instance_id=NEW.instance_id) "
    "OR EXISTS(SELECT 1 FROM managed_remote_guards WHERE instance_id=NEW.instance_id)) "
    "BEGIN SELECT RAISE(ABORT,'managed input conflict'); END",
    "CREATE TRIGGER novnc_mutation_insert BEFORE INSERT ON "
    "managed_instance_mutation_leases "
    "WHEN (EXISTS(SELECT 1 FROM novnc_sessions WHERE "
    "instance_id=NEW.managed_instance_id AND mode='interactive') "
    "OR EXISTS(SELECT 1 FROM managed_remote_guards WHERE "
    "instance_id=NEW.managed_instance_id)) "
    "AND NOT (NEW.operation_kind='lifecycle' AND EXISTS(SELECT 1 FROM "
    "managed_instance_lifecycle_operations WHERE id=NEW.operation_id "
    "AND managed_instance_id=NEW.managed_instance_id AND kind='stop' "
    "AND state='running')) "
    "BEGIN SELECT RAISE(ABORT,'managed input conflict'); END",
    "CREATE TRIGGER novnc_mutation_update BEFORE UPDATE OF "
    "managed_instance_id ON managed_instance_mutation_leases "
    "WHEN (EXISTS(SELECT 1 FROM novnc_sessions WHERE "
    "instance_id=NEW.managed_instance_id AND mode='interactive') "
    "OR EXISTS(SELECT 1 FROM managed_remote_guards WHERE "
    "instance_id=NEW.managed_instance_id)) "
    "AND NOT (NEW.operation_kind='lifecycle' AND EXISTS(SELECT 1 FROM "
    "managed_instance_lifecycle_operations WHERE id=NEW.operation_id "
    "AND managed_instance_id=NEW.managed_instance_id AND kind='stop' "
    "AND state='running')) "
    "BEGIN SELECT RAISE(ABORT,'managed input conflict'); END",
    "CREATE TRIGGER novnc_remote_insert BEFORE INSERT ON managed_remote_guards "
    "WHEN EXISTS(SELECT 1 FROM novnc_sessions WHERE "
    "instance_id=NEW.instance_id AND mode='interactive') OR EXISTS(SELECT 1 FROM "
    "managed_instance_mutation_leases WHERE managed_instance_id=NEW.instance_id) "
    "BEGIN SELECT RAISE(ABORT,'managed input conflict'); END",
    "CREATE TRIGGER novnc_remote_update BEFORE UPDATE OF instance_id "
    "ON managed_remote_guards "
    "WHEN EXISTS(SELECT 1 FROM novnc_sessions WHERE "
    "instance_id=NEW.instance_id AND mode='interactive') OR EXISTS(SELECT 1 FROM "
    "managed_instance_mutation_leases WHERE managed_instance_id=NEW.instance_id) "
    "BEGIN SELECT RAISE(ABORT,'managed input conflict'); END",
)


def upgrade() -> None:
    op.create_table(
        "novnc_sessions",
        sa.Column(
            "instance_id",
            sa.String(36),
            sa.ForeignKey("managed_instances.target_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("attempt", sa.String(36), nullable=False, unique=True),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("identity_json", sa.String(4096), nullable=False),
        sa.Column("expires_at", sa.String(27), nullable=False),
        sa.Column("absolute_expires_at", sa.String(27), nullable=False),
        sa.Column("broker_id", sa.String(36)),
        sa.Column("exec_id", sa.String(64)),
        sa.CheckConstraint("mode IN ('view-only','interactive')", name="novnc_mode"),
        sa.CheckConstraint(
            "state IN ('pending','active','revoking','blocked')", name="novnc_state"
        ),
        sa.CheckConstraint(
            "length(owner)=64 AND length(identity_json)<=4096",
            name="novnc_identity_bound",
        ),
        sa.CheckConstraint("expires_at<=absolute_expires_at", name="novnc_deadline"),
    )
    op.create_table(
        "managed_remote_guards",
        sa.Column(
            "instance_id",
            sa.String(36),
            sa.ForeignKey("managed_instances.target_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("invocation_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column("container_id", sa.String(64)),
        sa.Column("process_id", sa.String(36), nullable=False),
    )
    for statement in NOVNC_INPUT_TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM novnc_sessions")
    ) or op.get_bind().scalar(sa.text("SELECT count(*) FROM managed_remote_guards")):
        raise RuntimeError("Input ownership must be reconciled before downgrade")
    for statement in NOVNC_INPUT_TRIGGERS:
        op.execute("DROP TRIGGER " + statement.split()[2])
    op.drop_table("managed_remote_guards")
    op.drop_table("novnc_sessions")
