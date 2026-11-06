"""Immutable human Web confirmations over existing managed lifecycle receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0005_managed_web"
down_revision = "0004_clean_reset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE managed_web_intents (
 request_id VARCHAR(36) PRIMARY KEY NOT NULL,
 generation VARCHAR(8) NOT NULL,
 action VARCHAR(8) NOT NULL,
 snapshot_sha256 VARCHAR(64) NOT NULL,
 reset_confirmed BOOLEAN NOT NULL,
 created_at VARCHAR(27) NOT NULL,
 CONSTRAINT managed_web_action CHECK(action IN ('start','stop','reset')),
 CONSTRAINT managed_web_version CHECK(generation IN ('matrix','nexus','omega','piers')),
 CONSTRAINT managed_web_binding CHECK(length(request_id)=36
 AND length(snapshot_sha256)=64 AND length(created_at)=27),
 CONSTRAINT managed_web_confirmation CHECK(reset_confirmed IN (0,1)
 AND (action!='reset' OR reset_confirmed=1))
)
""")
    op.execute("""
CREATE TRIGGER managed_web_intent_immutable BEFORE UPDATE ON managed_web_intents
BEGIN SELECT RAISE(ABORT,'managed Web intent is immutable'); END
""")


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM managed_web_intents")):
        raise RuntimeError("Managed Web confirmations exist; refusing lossy downgrade")
    op.drop_table("managed_web_intents")
