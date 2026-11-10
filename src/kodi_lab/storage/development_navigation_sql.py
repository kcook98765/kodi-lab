"""Frozen additive schema for project-bound managed navigation receipts."""

CREATE_TABLE = """
CREATE TABLE mcp_development_navigation_bindings (
receipt_id VARCHAR(36) NOT NULL PRIMARY KEY
REFERENCES mcp_remote_action_receipts(id) ON DELETE RESTRICT,
session_generation INTEGER NOT NULL,
workspace_id VARCHAR(36) NOT NULL,
source_id VARCHAR(36) NOT NULL,
workspace_generation INTEGER NOT NULL,
deployment_id VARCHAR(36) NOT NULL,
addon_id VARCHAR(128) NOT NULL,
addon_version VARCHAR(128) NOT NULL,
tree_digest VARCHAR(71) NOT NULL,
destination_digest VARCHAR(71) NOT NULL,
runtime_revision_id VARCHAR(36) NOT NULL,
runtime_state_revision INTEGER NOT NULL,
container_id VARCHAR(64) NOT NULL,
bridge_generation INTEGER NOT NULL,
window_id INTEGER NOT NULL,
control_id INTEGER NOT NULL,
CONSTRAINT development_navigation_identity CHECK (
    length(workspace_id)=36 AND length(source_id)=36
    AND length(deployment_id)=36 AND length(runtime_revision_id)=36),
CONSTRAINT development_navigation_generation CHECK (
    session_generation>=1 AND workspace_generation>=0
    AND runtime_state_revision>=0 AND bridge_generation>=1),
CONSTRAINT development_navigation_content CHECK (
    length(addon_id) BETWEEN 1 AND 128
    AND length(addon_version) BETWEEN 1 AND 128
    AND length(tree_digest) IN (64,71)
    AND length(destination_digest) IN (64,71) AND length(container_id)=64),
CONSTRAINT development_navigation_menu CHECK (
    window_id IN (10000,10001,10002,10025,10040,10502) AND control_id>=0)
)
"""

TRIGGERS = (
    """CREATE TRIGGER development_navigation_binding_immutable
BEFORE UPDATE ON mcp_development_navigation_bindings
BEGIN SELECT RAISE(ABORT,'development navigation binding is immutable'); END""",
    """CREATE TRIGGER development_navigation_binding_parent
BEFORE INSERT ON mcp_development_navigation_bindings
WHEN NOT EXISTS (SELECT 1 FROM mcp_remote_action_receipts WHERE id=NEW.receipt_id
AND state='pending' AND
action IN ('up','down','left','right','back'))
OR EXISTS (SELECT 1 FROM mcp_production_remote_receipt_bindings
WHERE receipt_id=NEW.receipt_id)
BEGIN SELECT RAISE(ABORT,'development navigation requires original pending input');
END""",
    """CREATE TRIGGER development_navigation_binding_retained
BEFORE DELETE ON mcp_development_navigation_bindings
BEGIN SELECT RAISE(ABORT,'development navigation evidence is retained'); END""",
)

CREATE_INDEX = (
    "CREATE INDEX ix_development_navigation_context "
    "ON mcp_development_navigation_bindings "
    "(container_id,runtime_revision_id,runtime_state_revision)"
)
