"""Opt-in durable single development session and pre-dispatch provision slots."""

import sqlalchemy as sa
from alembic import op

revision = "0080_development_sessions"
down_revision = "0079_native_zip_installation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "development_sessions",
        sa.Column("generation", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column(
            "runtime_revision_id",
            sa.String(36),
            sa.ForeignKey("runtime_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("agent_workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.String(36),
            sa.ForeignKey("development_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("container_id", sa.String(64)),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("updated_at", sa.String(27), nullable=False),
        sa.UniqueConstraint("request_id", name="development_session_request"),
        sa.CheckConstraint("generation > 0", name="development_session_generation"),
        sa.CheckConstraint(
            "phase IN ('reserved','provisioning','bound','uncertain','closed')",
            name="development_session_phase",
        ),
        sa.CheckConstraint(
            "container_id IS NULL OR length(container_id)=64",
            name="development_session_container",
        ),
        sa.CheckConstraint(
            "phase != 'bound' OR container_id IS NOT NULL",
            name="development_session_bound",
        ),
    )
    op.create_table(
        "development_session_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "active_generation",
            sa.Integer(),
            sa.ForeignKey("development_sessions.generation", ondelete="RESTRICT"),
        ),
        sa.CheckConstraint("id=1", name="development_session_singleton"),
    )
    op.create_table(
        "managed_provision_reservations",
        sa.Column(
            "managed_instance_id",
            sa.String(36),
            sa.ForeignKey("managed_instances.target_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column(
            "session_generation",
            sa.Integer(),
            sa.ForeignKey("development_sessions.generation", ondelete="RESTRICT"),
        ),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.CheckConstraint(
            "phase IN ('reserved','uncertain')", name="managed_provision_phase"
        ),
        sa.CheckConstraint(
            "length(runtime_revision_id)=36 AND length(operation_id)=36",
            name="managed_provision_binding",
        ),
    )


def downgrade() -> None:
    for table in ("development_sessions", "managed_provision_reservations"):
        if op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}")):
            raise RuntimeError(
                "Development session evidence cannot be safely downgraded"
            )
    op.drop_table("managed_provision_reservations")
    op.drop_table("development_session_policy")
    op.drop_table("development_sessions")
