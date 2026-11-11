"""Typed owner-project admission independent of human session selection."""

from alembic import op

from kodi_lab.storage.retained_policy_sql import (
    AGENT_PROJECT_MUTATION_TRIGGER,
    MANAGED_START_MUTATION_TRIGGER,
)

revision = "0011_agent_project_versions"
down_revision = "0010_agent_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER retained_mutation_insert")
    op.execute(AGENT_PROJECT_MUTATION_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER retained_mutation_insert")
    op.execute(MANAGED_START_MUTATION_TRIGGER)
