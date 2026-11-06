"""Durable, principal-bound managed version operations; no preset grants."""

import sqlalchemy as sa
from alembic import op

revision = "0003_managed_versions"
down_revision = "0002_managed_setup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE managed_version_operations (
id VARCHAR(36) NOT NULL,
client_id VARCHAR(36) NOT NULL,
request_id VARCHAR(36) NOT NULL,
generation VARCHAR(8) NOT NULL,
target_id VARCHAR(36) NOT NULL,
action VARCHAR(8) NOT NULL,
phase VARCHAR(16) NOT NULL,
code VARCHAR(64) NOT NULL,
runtime_revision_id VARCHAR(36),
image_id VARCHAR(71),
container_id VARCHAR(64),
expected_state_revision INTEGER,
provision_operation_id VARCHAR(36),
lifecycle_operation_id VARCHAR(36),
hold_until VARCHAR(27),
created_at VARCHAR(27) NOT NULL,
updated_at VARCHAR(27) NOT NULL,
PRIMARY KEY (id),
CONSTRAINT managed_version_request UNIQUE (client_id, request_id),
CONSTRAINT managed_version_intent CHECK (generation IN ('matrix', 'nexus',
'omega', 'piers') AND action IN ('start', 'stop')),
CONSTRAINT managed_version_phase CHECK (phase IN ('accepted', 'prepared',
'provisioning', 'starting', 'stopping', 'complete', 'blocked', 'uncertain')),
CONSTRAINT managed_version_binding CHECK (length(id)=36 AND length(client_id)=36
AND length(request_id)=36 AND length(target_id)=36 AND length(code) BETWEEN 1
AND 64 AND length(created_at)=27 AND length(updated_at)=27),
CONSTRAINT managed_version_runtime CHECK ((runtime_revision_id IS NULL AND
image_id IS NULL) OR (runtime_revision_id IS NOT NULL AND image_id IS NOT NULL
AND length(runtime_revision_id)=36 AND length(image_id)=71)),
CONSTRAINT managed_version_container CHECK (container_id IS NULL OR
length(container_id)=64),
CONSTRAINT managed_version_epoch CHECK (expected_state_revision IS NULL OR
expected_state_revision>=0)
)
""")
    op.create_index(
        "ix_managed_version_active",
        "managed_version_operations",
        ["target_id", "phase"],
    )
    # Freeze this migration independently of future application guard changes.
    op.execute("""
CREATE TRIGGER managed_version_operation_immutable
BEFORE UPDATE ON managed_version_operations
WHEN NEW.id IS NOT OLD.id OR NEW.client_id IS NOT OLD.client_id
 OR NEW.request_id IS NOT OLD.request_id OR NEW.generation IS NOT OLD.generation
 OR NEW.target_id IS NOT OLD.target_id OR NEW.action IS NOT OLD.action
 OR NEW.runtime_revision_id IS NOT OLD.runtime_revision_id
 OR NEW.image_id IS NOT OLD.image_id OR NEW.created_at IS NOT OLD.created_at
 OR (OLD.container_id IS NOT NULL AND NEW.container_id IS NOT OLD.container_id)
 OR (OLD.provision_operation_id IS NOT NULL
     AND NEW.provision_operation_id IS NOT OLD.provision_operation_id)
 OR (OLD.lifecycle_operation_id IS NOT NULL
     AND NEW.lifecycle_operation_id IS NOT OLD.lifecycle_operation_id)
 OR (OLD.hold_until IS NOT NULL AND NEW.hold_until IS NOT OLD.hold_until)
BEGIN SELECT RAISE(ABORT, 'managed version operation identity is immutable'); END
""")


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM managed_version_operations")):
        raise RuntimeError("Managed version receipts exist; refusing lossy downgrade")
    op.execute("DROP TRIGGER managed_version_operation_immutable")
    op.drop_table("managed_version_operations")
