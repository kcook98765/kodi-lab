"""Persist explicit human Reset preparation consent; old requests stay native-only."""

import sqlalchemy as sa
from alembic import op

revision = "0014_human_reset_bridge"
down_revision = "0013_agent_packages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "managed_web_intents",
        sa.Column(
            "prepare_bridge",
            sa.Boolean(),
            sa.CheckConstraint(
                "prepare_bridge IN (0,1) AND "
                "(prepare_bridge=0 OR (action='reset' AND reset_confirmed=1))",
                name="managed_web_bridge_consent",
            ),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM managed_web_intents WHERE prepare_bridge=1")
    ):
        raise RuntimeError("Human Bridge consent exists; refusing lossy downgrade")
    op.drop_column("managed_web_intents", "prepare_bridge")
