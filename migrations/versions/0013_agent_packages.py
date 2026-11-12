"""Owner-scoped original packages, separate from editable source intake."""

import sqlalchemy as sa
from alembic import op

from kodi_lab.storage.agent_package_sql import PACKAGE_IMMUTABLE, PACKAGE_NO_DELETE

revision = "0013_agent_packages"
down_revision = "0012_agent_project_intake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_packages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "client_id",
            sa.String(36),
            sa.ForeignKey("mcp_clients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("archive_sha256", sa.String(64), nullable=False),
        sa.Column("provenance_reference", sa.String(512), nullable=False),
        sa.Column("created_at", sa.String(27), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.UniqueConstraint(
            "client_id", "request_id", name="agent_package_request_once"
        ),
        sa.CheckConstraint(
            "state IN ('reserved','accepted','failed')", name="agent_package_state"
        ),
        sa.CheckConstraint("length(archive_sha256)=64", name="agent_package_sha"),
        sa.CheckConstraint(
            "length(provenance_reference) BETWEEN 1 AND 512",
            name="agent_package_provenance",
        ),
        sa.CheckConstraint("length(created_at)=27", name="agent_package_timestamp"),
    )
    op.execute(PACKAGE_IMMUTABLE)
    op.execute(PACKAGE_NO_DELETE)


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM agent_packages")):
        raise RuntimeError("Agent package evidence exists; preserve owner history")
    op.execute("DROP TRIGGER agent_package_immutable")
    op.execute("DROP TRIGGER agent_package_no_delete")
    op.drop_table("agent_packages")
