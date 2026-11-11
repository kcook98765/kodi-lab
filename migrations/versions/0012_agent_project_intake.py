"""Bounded immutable provenance for agent-owned source package intake."""

import sqlalchemy as sa
from alembic import op

from kodi_lab.storage.agent_project_sql import (
    PROJECT_IMMUTABLE,
    PROJECT_INTAKE_IMMUTABLE,
)

revision = "0012_agent_project_intake"
down_revision = "0011_agent_project_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_projects",
        sa.Column(
            "intake_provenance_json",
            sa.Text(),
            sa.CheckConstraint(
                "intake_provenance_json IS NULL OR "
                "(json_valid(intake_provenance_json) "
                "AND length(intake_provenance_json)<=2048)",
                name="agent_project_provenance_bounded",
            ),
            nullable=True,
        ),
    )
    op.execute("DROP TRIGGER agent_project_immutable")
    op.execute(PROJECT_INTAKE_IMMUTABLE)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM agent_projects "
            "WHERE intake_provenance_json IS NOT NULL"
        )
    ):
        raise RuntimeError(
            "Project package provenance exists; preserve intake evidence"
        )
    op.execute("DROP TRIGGER agent_project_immutable")
    op.drop_column("agent_projects", "intake_provenance_json")
    op.execute(PROJECT_IMMUTABLE)
