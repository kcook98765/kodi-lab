"""Durable clean Reset intents, immutable identity and mutation fences."""

import sqlalchemy as sa
from alembic import op

revision = "0004_clean_reset"
down_revision = "0003_managed_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE managed_clean_resets (
 id VARCHAR(36) PRIMARY KEY NOT NULL,
 client_id VARCHAR(36) NOT NULL,
 request_id VARCHAR(36) NOT NULL,
 generation VARCHAR(8) NOT NULL,
 target_id VARCHAR(36) NOT NULL,
 restart BOOLEAN NOT NULL,
 runtime_revision_id VARCHAR(36) NOT NULL,
 image_id VARCHAR(71) NOT NULL,
 old_container_id VARCHAR(64),
 new_container_id VARCHAR(64),
 selected_generation INTEGER,
 expected_state_revision INTEGER NOT NULL,
 phase VARCHAR(16) NOT NULL,
 step VARCHAR(12),
 code VARCHAR(64) NOT NULL,
 stop_id VARCHAR(36) NOT NULL,
 remove_id VARCHAR(36) NOT NULL,
 provision_id VARCHAR(36) NOT NULL,
 start_id VARCHAR(36) NOT NULL,
 remove_dispatched_at VARCHAR(27),
 invalidated_at VARCHAR(27),
 created_at VARCHAR(27) NOT NULL,
 updated_at VARCHAR(27) NOT NULL,
 CONSTRAINT clean_reset_request UNIQUE(client_id,request_id),
 CONSTRAINT clean_reset_version CHECK(generation IN ('matrix','nexus','omega','piers')),
 CONSTRAINT clean_reset_restart CHECK(restart IN (0,1)),
 CONSTRAINT clean_reset_phase CHECK(phase IN ('accepted','stopping','stopped',
 'removing','retired','provisioning','prepared','starting','complete','blocked','uncertain')),
 CONSTRAINT clean_reset_step CHECK(step IS NULL OR
 step IN ('stop','remove','provision','start')),
 CONSTRAINT clean_reset_identity CHECK(length(id)=36 AND length(client_id)=36
 AND length(request_id)=36 AND length(target_id)=36 AND length(runtime_revision_id)=36
 AND length(image_id)=71 AND length(stop_id)=36 AND length(remove_id)=36
 AND length(provision_id)=36 AND length(start_id)=36 AND expected_state_revision>=0),
 CONSTRAINT clean_reset_containers CHECK((old_container_id IS NULL
 OR length(old_container_id)=64)
 AND (new_container_id IS NULL OR (length(new_container_id)=64
 AND (old_container_id IS NULL OR new_container_id!=old_container_id))))
)
""")
    op.execute("""CREATE INDEX ix_clean_reset_target
 ON managed_clean_resets(target_id,phase)
""")
    op.execute("""CREATE UNIQUE INDEX uq_clean_reset_active
 ON managed_clean_resets(target_id) WHERE phase NOT IN ('complete','blocked')
""")
    op.execute("""
CREATE TRIGGER clean_reset_immutable BEFORE UPDATE ON managed_clean_resets
WHEN NEW.id IS NOT OLD.id OR NEW.client_id IS NOT OLD.client_id
 OR NEW.request_id IS NOT OLD.request_id OR NEW.generation IS NOT OLD.generation
 OR NEW.target_id IS NOT OLD.target_id OR NEW.restart IS NOT OLD.restart
 OR NEW.runtime_revision_id IS NOT OLD.runtime_revision_id
 OR NEW.image_id IS NOT OLD.image_id
 OR NEW.old_container_id IS NOT OLD.old_container_id
 OR NEW.selected_generation IS NOT OLD.selected_generation
 OR NEW.stop_id IS NOT OLD.stop_id OR NEW.remove_id IS NOT OLD.remove_id
 OR NEW.provision_id IS NOT OLD.provision_id OR NEW.start_id IS NOT OLD.start_id
 OR NEW.created_at IS NOT OLD.created_at
 OR (OLD.new_container_id IS NOT NULL
 AND NEW.new_container_id IS NOT OLD.new_container_id)
 OR (OLD.remove_dispatched_at IS NOT NULL
 AND NEW.remove_dispatched_at IS NOT OLD.remove_dispatched_at)
 OR (OLD.invalidated_at IS NOT NULL AND NEW.invalidated_at IS NOT OLD.invalidated_at)
BEGIN SELECT RAISE(ABORT,'clean reset intent is immutable'); END
""")
    op.execute("""
CREATE TRIGGER clean_reset_mutation_insert BEFORE INSERT
ON managed_instance_mutation_leases
WHEN EXISTS(SELECT 1 FROM managed_clean_resets r
 WHERE r.target_id=NEW.managed_instance_id
 AND r.phase NOT IN ('complete','blocked'))
AND NOT EXISTS(SELECT 1 FROM managed_clean_resets r
 JOIN managed_instance_lifecycle_operations o ON o.id=NEW.operation_id
 WHERE r.target_id=NEW.managed_instance_id AND NEW.operation_kind='lifecycle'
 AND o.managed_instance_id=r.target_id
 AND o.original_managed_instance_id=r.target_id
 AND o.state='running' AND (
 (r.phase IN ('accepted','stopping') AND o.kind='stop' AND o.id=r.stop_id) OR
 (r.phase IN ('stopped','removing') AND o.kind='deprovision'
 AND o.id=r.remove_id) OR
 (r.phase IN ('retired','provisioning') AND o.kind='provision'
 AND o.id=r.provision_id) OR
 (r.phase IN ('prepared','starting') AND o.kind='start' AND o.id=r.start_id)))
BEGIN SELECT RAISE(ABORT,'clean reset pending'); END
""")
    op.execute("""
CREATE TRIGGER clean_reset_view_insert BEFORE INSERT ON novnc_sessions
WHEN EXISTS(SELECT 1 FROM managed_clean_resets r
 WHERE r.target_id=NEW.instance_id AND r.phase NOT IN ('complete','blocked'))
BEGIN SELECT RAISE(ABORT,'clean reset pending'); END
""")
    op.execute("""
CREATE TRIGGER clean_reset_remote_insert BEFORE INSERT ON managed_remote_guards
WHEN EXISTS(SELECT 1 FROM managed_clean_resets r
 WHERE r.target_id=NEW.instance_id AND r.phase NOT IN ('complete','blocked'))
BEGIN SELECT RAISE(ABORT,'clean reset pending'); END
""")


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM managed_clean_resets")):
        raise RuntimeError("Clean Reset receipts exist; refusing lossy downgrade")
    op.execute("DROP TRIGGER clean_reset_immutable")
    op.execute("DROP TRIGGER clean_reset_mutation_insert")
    op.execute("DROP TRIGGER clean_reset_view_insert")
    op.execute("DROP TRIGGER clean_reset_remote_insert")
    op.drop_table("managed_clean_resets")
