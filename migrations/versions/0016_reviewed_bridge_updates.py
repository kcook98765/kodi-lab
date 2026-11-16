"""Owner opt-in and immutable receipts for exact reviewed production updates."""

import sqlalchemy as sa
from alembic import op

revision = "0016_reviewed_bridge_updates"
down_revision = "0015_log_scan_capability"
branch_labels = None
depends_on = None

# Frozen DDL; no dependency on application metadata or future release pins.
_TABLES = (
    "CREATE TABLE production_bridge_update_policies (\n"
    "\ttarget_id VARCHAR(36) NOT NULL, \n"
    "\tenabled BOOLEAN DEFAULT '0' NOT NULL, \n"
    "\trevision INTEGER NOT NULL, \n"
    "\tbinding_digest VARCHAR(64) NOT NULL, \n"
    "\tupdated_by VARCHAR(64) NOT NULL, \n"
    "\tupdated_at VARCHAR(27) NOT NULL, \n"
    "\tPRIMARY KEY (target_id), \n"
    "\tCONSTRAINT bridge_update_policy_valid CHECK (enabled IN (0,1) AND "
    "revision>=1), \n"
    "\tCONSTRAINT bridge_update_policy_binding CHECK (length(binding_digest)=64 "
    "AND binding_digest NOT GLOB '*[^0-9a-f]*'), \n"
    "\tCONSTRAINT bridge_update_policy_actor CHECK (length(updated_at)=27 AND "
    "length(updated_by) BETWEEN 1 AND 64), \n"
    "\tFOREIGN KEY(target_id) REFERENCES external_production_policies "
    "(target_id) ON DELETE RESTRICT\n"
    ")",
    "CREATE TABLE production_bridge_adoptions (\n"
    "\tid VARCHAR(36) NOT NULL, \n"
    "\ttarget_id VARCHAR(36) NOT NULL, \n"
    "\tbridge_id VARCHAR(36) NOT NULL, \n"
    "\tfrom_revision INTEGER NOT NULL, \n"
    "\tto_revision INTEGER NOT NULL, \n"
    "\tfrom_build_id VARCHAR(36) NOT NULL, \n"
    "\tto_build_id VARCHAR(36) NOT NULL, \n"
    "\tauthority_revision INTEGER NOT NULL, \n"
    "\tauthority_build_id VARCHAR(36) NOT NULL, \n"
    "\thttp_acknowledgement_id VARCHAR(36), \n"
    "\tbinding_digest VARCHAR(64) NOT NULL, \n"
    "\tpolicy_revision INTEGER NOT NULL, \n"
    "\tactor VARCHAR(64) NOT NULL, \n"
    "\toccurred_at VARCHAR(27) NOT NULL, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT bridge_adoption_revision_unique UNIQUE (target_id, "
    "to_revision), \n"
    "\tCONSTRAINT bridge_adoption_revisions CHECK (from_revision>=1 AND "
    "to_revision=from_revision+1 AND authority_revision>=1 AND "
    "authority_revision<=from_revision AND policy_revision>=1), \n"
    "\tCONSTRAINT bridge_adoption_binding CHECK (length(binding_digest)=64 AND "
    "binding_digest NOT GLOB '*[^0-9a-f]*'), \n"
    "\tCONSTRAINT bridge_adoption_audit CHECK (length(id)=36 AND "
    "length(bridge_id)=36 AND length(occurred_at)=27 AND "
    "actor='automatic_reviewed_update'), \n"
    "\tFOREIGN KEY(target_id) REFERENCES external_production_policies "
    "(target_id) ON DELETE RESTRICT, \n"
    "\tFOREIGN KEY(from_build_id) REFERENCES reviewed_bridge_builds (id) ON "
    "DELETE RESTRICT, \n"
    "\tFOREIGN KEY(to_build_id) REFERENCES reviewed_bridge_builds (id) ON "
    "DELETE RESTRICT, \n"
    "\tFOREIGN KEY(authority_build_id) REFERENCES reviewed_bridge_builds (id) "
    "ON DELETE RESTRICT, \n"
    "\tFOREIGN KEY(http_acknowledgement_id) REFERENCES "
    "production_bridge_http_acknowledgements (id) ON DELETE RESTRICT\n"
    ")",
)

_TRIGGERS = {
    "trg_bridge_update_policy_no_replace": (
        "BEFORE INSERT ON production_bridge_update_policies "
        "WHEN EXISTS(SELECT 1 FROM production_bridge_update_policies "
        "WHERE target_id=NEW.target_id) "
        "BEGIN SELECT RAISE(ABORT, 'bridge_update_policy_history'); END"
    ),
    "trg_bridge_adoption_no_replace": (
        "BEFORE INSERT ON production_bridge_adoptions "
        "WHEN EXISTS(SELECT 1 FROM production_bridge_adoptions "
        "WHERE id=NEW.id OR (target_id=NEW.target_id AND to_revision=NEW.to_revision)) "
        "BEGIN SELECT RAISE(ABORT, 'bridge_adoption_immutable'); END"
    ),
    "trg_bridge_update_policy_revision": "BEFORE UPDATE ON "
    "production_bridge_update_policies "
    "WHEN NEW.target_id<>OLD.target_id OR "
    "NEW.revision<>OLD.revision+1 BEGIN "
    "SELECT RAISE(ABORT, "
    "'bridge_update_policy_revision'); END",
    "trg_bridge_update_policy_no_delete": "BEFORE DELETE ON "
    "production_bridge_update_policies "
    "BEGIN SELECT RAISE(ABORT, "
    "'bridge_update_policy_history'); END",
    "trg_bridge_adoption_no_update": "BEFORE UPDATE ON "
    "production_bridge_adoptions BEGIN SELECT "
    "RAISE(ABORT, "
    "'bridge_adoption_immutable'); END",
    "trg_bridge_adoption_no_delete": "BEFORE DELETE ON "
    "production_bridge_adoptions BEGIN SELECT "
    "RAISE(ABORT, "
    "'bridge_adoption_immutable'); END",
}


def upgrade() -> None:
    for statement in _TABLES:
        op.execute(statement)
    for name, body in _TRIGGERS.items():
        op.execute("CREATE TRIGGER " + name + " " + body)


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM production_bridge_update_policies) OR "
            "EXISTS(SELECT 1 FROM production_bridge_adoptions)"
        )
    ):
        raise RuntimeError(
            "reviewed Bridge update policy/history exists; refusing lossy downgrade"
        )
    for name in _TRIGGERS:
        op.execute("DROP TRIGGER " + name)
    op.drop_table("production_bridge_adoptions")
    op.drop_table("production_bridge_update_policies")
