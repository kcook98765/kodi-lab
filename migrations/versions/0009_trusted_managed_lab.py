"""Independent managed role v1; preserve every existing preset and grant."""

import sqlalchemy as sa
from alembic import op

revision = "0009_trusted_managed_lab"
down_revision = "0008_managed_bridge_preparation"
branch_labels = None
depends_on = None

OLD_TRIGGER = (
    "CREATE TRIGGER trg_mcp_client_authority_revision BEFORE UPDATE ON mcp_clients "
    "WHEN (((NEW.enabled<>OLD.enabled OR NEW.preset_id<>OLD.preset_id) AND "
    "NEW.authority_revision<=OLD.authority_revision) OR "
    "((NEW.enabled=OLD.enabled AND NEW.preset_id=OLD.preset_id) AND "
    "NEW.authority_revision<>OLD.authority_revision)) BEGIN SELECT "
    "RAISE(ABORT, 'mcp_client_authority_revision_invalid'); END"
)
NEW_TRIGGER = (
    "CREATE TRIGGER trg_mcp_client_authority_revision BEFORE UPDATE ON mcp_clients "
    "WHEN (((NEW.enabled<>OLD.enabled OR NEW.preset_id<>OLD.preset_id OR "
    "NEW.managed_lab_role_version IS NOT OLD.managed_lab_role_version) AND "
    "NEW.authority_revision<=OLD.authority_revision) OR "
    "((NEW.enabled=OLD.enabled AND NEW.preset_id=OLD.preset_id AND "
    "NEW.managed_lab_role_version IS OLD.managed_lab_role_version) AND "
    "NEW.authority_revision<>OLD.authority_revision)) BEGIN SELECT "
    "RAISE(ABORT, 'mcp_client_authority_revision_invalid'); END"
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE mcp_clients ADD COLUMN managed_lab_role_version INTEGER "
        "CONSTRAINT managed_lab_role_version CHECK (managed_lab_role_version = 1)"
    )
    op.execute("DROP TRIGGER trg_mcp_client_authority_revision")
    op.execute(NEW_TRIGGER)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM mcp_clients "
            "WHERE managed_lab_role_version IS NOT NULL"
        )
    ) or op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM mcp_security_events WHERE event_kind IN "
            "('managed_lab_role_granted','managed_lab_role_revoked')"
        )
    ):
        raise RuntimeError("Managed Lab role is in use; preserve its authority history")
    op.execute("DROP TRIGGER trg_mcp_client_authority_revision")
    op.drop_column("mcp_clients", "managed_lab_role_version")
    op.execute(OLD_TRIGGER)
