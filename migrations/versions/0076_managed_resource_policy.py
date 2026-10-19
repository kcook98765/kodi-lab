"""Managed capacity, conservative idle enrollment and owned agent lifecycle receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0076_managed_resource_policy"
down_revision = "0075_external_reconnect"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_resource_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("max_running", sa.Integer(), nullable=False),
        sa.Column("idle_minutes", sa.Integer()),
        sa.CheckConstraint("id=1", name="managed_resource_policy_singleton"),
        sa.CheckConstraint("max_running BETWEEN 1 AND 64", name="managed_resource_cap"),
        sa.CheckConstraint(
            "idle_minutes IS NULL OR idle_minutes BETWEEN 1 AND 10080",
            name="managed_resource_idle_minutes",
        ),
    )
    op.execute("INSERT INTO managed_resource_policy VALUES (1,4,60)")
    op.create_table(
        "managed_idle_states",
        sa.Column(
            "managed_instance_id",
            sa.String(36),
            sa.ForeignKey("managed_instances.target_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("enrolled", sa.Boolean(), nullable=False),
        sa.Column("last_activity_at", sa.String(27)),
        sa.Column("last_stop_attempt_at", sa.String(27)),
        sa.Column("last_stop_operation_id", sa.String(36)),
        sa.Column("last_stopped_at", sa.String(27)),
        sa.Column("last_stop_reason", sa.String(16)),
        sa.CheckConstraint("enrolled IN (0,1)", name="managed_idle_enrolled"),
        sa.CheckConstraint(
            "last_stop_reason IS NULL OR last_stop_reason IN ('idle','released')",
            name="managed_idle_stop_reason",
        ),
    )
    op.execute(
        "INSERT INTO managed_idle_states(managed_instance_id,enrolled) "
        "SELECT target_id,0 FROM managed_instances"
    )
    op.create_table(
        "managed_instance_uses",
        sa.Column(
            "managed_instance_id",
            sa.String(36),
            sa.ForeignKey("managed_instances.target_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("actor_key", sa.String(40), primary_key=True),
        sa.Column("last_activity_at", sa.String(27), nullable=False),
        sa.Column("hold_until", sa.String(27)),
        sa.Column("released_at", sa.String(27)),
        sa.CheckConstraint(
            "actor_key='web' OR (length(actor_key)=40 "
            "AND substr(actor_key,1,4)='mcp:')",
            name="managed_use_actor",
        ),
    )
    op.create_table(
        "managed_start_reservations",
        sa.Column(
            "managed_instance_id",
            sa.String(36),
            sa.ForeignKey("managed_instances.target_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("container_id", sa.String(64), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "phase IN ('reserved','uncertain')", name="managed_start_phase"
        ),
        sa.CheckConstraint(
            "length(container_id)=64 AND length(runtime_revision_id)=36 "
            "AND length(operation_id)=36",
            name="managed_start_binding",
        ),
    )
    op.create_table(
        "managed_agent_lifecycle_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "client_id",
            sa.String(36),
            sa.ForeignKey("mcp_clients.id", ondelete="SET NULL"),
        ),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("original_instance_id", sa.String(36), nullable=False),
        sa.Column(
            "managed_instance_id",
            sa.String(36),
            sa.ForeignKey("managed_instances.target_id", ondelete="SET NULL"),
        ),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("hold_minutes", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("lifecycle_operation_id", sa.String(36)),
        sa.Column("runtime_revision_id", sa.String(36)),
        sa.Column("container_id", sa.String(64)),
        sa.Column("expected_state_revision", sa.Integer()),
        sa.Column("result_json", sa.Text()),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("updated_at", sa.String(27), nullable=False),
        sa.UniqueConstraint(
            "client_id", "request_id", name="managed_agent_request_owner"
        ),
        sa.CheckConstraint(
            "action IN ('ensure_ready','release')", name="managed_agent_action"
        ),
        sa.CheckConstraint(
            "state IN ('pending','dispatched','ready','not_ready','released',"
            "'retained','failed','ambiguous')",
            name="managed_agent_state",
        ),
        sa.CheckConstraint(
            "length(original_instance_id)=36 AND length(request_id)=36 "
            "AND length(id)=36 AND hold_minutes BETWEEN 0 AND 240",
            name="managed_agent_binding",
        ),
        sa.CheckConstraint(
            "result_json IS NULL OR length(result_json)<=8192",
            name="managed_agent_result_bound",
        ),
        sa.CheckConstraint(
            "(runtime_revision_id IS NULL AND container_id IS NULL "
            "AND expected_state_revision IS NULL) OR "
            "(runtime_revision_id IS NOT NULL AND length(runtime_revision_id)=36 "
            "AND container_id IS NOT NULL AND length(container_id)=64 "
            "AND expected_state_revision IS NOT NULL AND expected_state_revision>=0)",
            name="managed_agent_runtime_binding",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if (
        any(
            bind.scalar(sa.text(f"SELECT count(*) FROM {table}"))
            for table in (
                "managed_agent_lifecycle_requests",
                "managed_start_reservations",
                "managed_instance_uses",
            )
        )
        or bind.scalar(
            sa.text(
                "SELECT count(*) FROM managed_idle_states WHERE enrolled<>0 "
                "OR last_activity_at IS NOT NULL OR last_stop_attempt_at IS NOT NULL "
                "OR last_stop_operation_id IS NOT NULL "
                "OR last_stopped_at IS NOT NULL OR last_stop_reason IS NOT NULL"
            )
        )
        or bind.scalar(
            sa.text(
                "SELECT count(*) FROM managed_resource_policy "
                "WHERE max_running<>4 OR idle_minutes IS NULL OR idle_minutes<>60"
            )
        )
    ):
        raise RuntimeError(
            "Managed resource policy or lifecycle evidence cannot be safely downgraded"
        )
    for table in (
        "managed_agent_lifecycle_requests",
        "managed_start_reservations",
        "managed_instance_uses",
        "managed_idle_states",
        "managed_resource_policy",
    ):
        op.drop_table(table)
