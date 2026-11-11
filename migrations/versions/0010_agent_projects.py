"""Owner-scoped private project source intake over existing workspaces."""

import sqlalchemy as sa
from alembic import op

from kodi_lab.storage.agent_project_sql import PROJECT_IMMUTABLE, PROJECT_NO_DELETE

revision = "0010_agent_projects"
down_revision = "0009_trusted_managed_lab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_projects",
        sa.Column(
            "source_id",
            sa.String(36),
            sa.ForeignKey("development_sources.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "client_id",
            sa.String(36),
            sa.ForeignKey("mcp_clients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("parameter_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.UniqueConstraint(
            "client_id", "request_id", name="agent_project_request_once"
        ),
        sa.CheckConstraint(
            "state IN ('reserved','seeded')", name="agent_project_state"
        ),
        sa.CheckConstraint(
            "length(parameter_fingerprint)=64", name="agent_project_fingerprint"
        ),
        sa.CheckConstraint("length(created_at)=27", name="agent_project_timestamp"),
    )
    op.execute(PROJECT_IMMUTABLE)
    op.execute(PROJECT_NO_DELETE)


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM agent_projects")):
        raise RuntimeError(
            "Agent project history exists; preserve its source and owner"
        )
    op.execute("DROP TRIGGER agent_project_immutable")
    op.execute("DROP TRIGGER agent_project_no_delete")
    op.drop_table("agent_projects")
