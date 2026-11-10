"""Retained project bindings for opt-in managed navigation; no grant changes."""

from alembic import op
from sqlalchemy import text

from kodi_lab.storage.development_navigation_sql import (
    CREATE_INDEX,
    CREATE_TABLE,
    TRIGGERS,
)

revision = "0008_development_navigation"
down_revision = "0007_production_navigation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(CREATE_TABLE)
    op.execute(CREATE_INDEX)
    for trigger in TRIGGERS:
        op.execute(trigger)


def downgrade() -> None:
    if op.get_bind().scalar(
        text("SELECT EXISTS(SELECT 1 FROM mcp_development_navigation_bindings)")
    ):
        raise RuntimeError("development navigation receipt history prevents downgrade")
    for name in ("immutable", "parent", "retained"):
        op.execute("DROP TRIGGER development_navigation_binding_" + name)
    op.drop_table("mcp_development_navigation_bindings")
