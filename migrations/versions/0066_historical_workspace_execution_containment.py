"""Record later administrator-confirmed containment of historical workspace ambiguity.

Revision ID: 0066_historical_workspace_execution_containment
Revises: 0065_workspace_execution_recovery
"""

import sqlalchemy as sa
from alembic import op

revision = "0066_historical_workspace_execution_containment"
down_revision = "0065_workspace_execution_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "historical_workspace_execution_containment_resolutions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False, unique=True),
        sa.Column(
            "execution_operation_id",
            sa.String(36),
            sa.ForeignKey("mcp_workspace_execution_operations.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "managed_instance_id",
            sa.String(36),
            sa.ForeignKey("managed_instances.target_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("administrator_key", sa.String(64), nullable=False),
        sa.Column(
            "stop_operation_id",
            sa.String(36),
            sa.ForeignKey(
                "managed_instance_lifecycle_operations.id", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        sa.Column("observed_runtime_state_revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("proof", sa.String(32), nullable=False),
        sa.Column("requested_at", sa.String(27), nullable=False),
        sa.Column("completed_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "length(id)=36 AND length(request_id)=36 AND "
            "length(execution_operation_id)=36 AND length(managed_instance_id)=36 "
            "AND length(stop_operation_id)=36 AND observed_runtime_state_revision>=0",
            name="historical_containment_identity_shape",
        ),
        sa.CheckConstraint(
            "administrator_key='local-admin' AND state='completed' AND "
            "proof='broker_stopped' AND length(requested_at)=27 AND "
            "length(completed_at)=27",
            name="historical_containment_completed_proof",
        ),
    )
    for sql in _TRIGGERS:
        op.execute(sql)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM "
            "historical_workspace_execution_containment_resolutions)"
        )
    ):
        raise RuntimeError("persisted historical containment cannot be downgraded")
    op.drop_table("historical_workspace_execution_containment_resolutions")


_TRIGGERS = (
    "CREATE TRIGGER trg_historical_containment_eligible BEFORE INSERT ON "
    "historical_workspace_execution_containment_resolutions WHEN NOT EXISTS "
    "(SELECT 1 FROM mcp_workspace_execution_operations e WHERE "
    "e.id=NEW.execution_operation_id AND e.target_id=NEW.managed_instance_id "
    "AND e.state='ambiguous' AND e.invocation_dispatched=1 AND "
    "e.execution_classification='timeout' AND "
    "e.failure_code='execution_ambiguous' AND e.execution_kind IS NULL AND "
    "e.original_container_id IS NULL AND "
    "e.original_runtime_state_revision IS NULL) OR EXISTS "
    "(SELECT 1 FROM workspace_execution_recovery_operations r WHERE "
    "r.execution_operation_id=NEW.execution_operation_id AND "
    "r.state='completed' AND r.outcome='recovered') BEGIN "
    "SELECT RAISE(ABORT,'historical_containment_ineligible'); END",
    "CREATE TRIGGER trg_historical_containment_immutable BEFORE UPDATE ON "
    "historical_workspace_execution_containment_resolutions BEGIN "
    "SELECT RAISE(ABORT,'historical_containment_immutable'); END",
    "CREATE TRIGGER trg_historical_containment_no_delete BEFORE DELETE ON "
    "historical_workspace_execution_containment_resolutions BEGIN "
    "SELECT RAISE(ABORT,'historical_containment_immutable'); END",
)
