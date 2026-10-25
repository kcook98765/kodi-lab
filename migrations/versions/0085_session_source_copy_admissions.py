"""Retain guided source-copy request scope beyond bounded operation detail."""

import sqlalchemy as sa
from alembic import op

revision = "0085_session_source_copy_admissions"
down_revision = "0084_session_replacements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_source_copy_admissions",
        sa.Column("request_id", sa.String(36), primary_key=True),
        # Historical identity intentionally survives Source detail pruning.
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column(
            "session_generation",
            sa.Integer(),
            sa.ForeignKey("development_sessions.generation", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("runtime_revision_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("container_id", sa.String(64), nullable=False),
        sa.Column("requested_at", sa.String(27), nullable=False),
        sa.UniqueConstraint("operation_id", name="session_source_copy_operation"),
        sa.CheckConstraint(
            "length(request_id)=36 AND length(operation_id)=36 AND "
            "length(target_id)=36 AND length(runtime_revision_id)=36 AND "
            "length(workspace_id)=36 AND length(source_id)=36",
            name="session_source_copy_identity",
        ),
        sa.CheckConstraint(
            "session_generation>0", name="session_source_copy_generation"
        ),
        sa.CheckConstraint(
            "length(container_id)=64 AND container_id NOT GLOB '*[^0-9a-f]*'",
            name="session_source_copy_container",
        ),
        sa.CheckConstraint(
            "length(requested_at)=27", name="session_source_copy_timestamp"
        ),
    )
    op.create_index(
        "ix_session_source_copy_generation",
        "session_source_copy_admissions",
        ["session_generation"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM session_source_copy_admissions")
    ):
        raise RuntimeError("Source-copy admission history cannot be safely downgraded")
    op.drop_table("session_source_copy_admissions")
