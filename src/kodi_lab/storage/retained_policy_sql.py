"""Atomic retained-selection backstop, shared by ORM fixtures and Alembic."""

# This backstop shares the actual SQLite lease-insert writer boundary, including
# services whose SQLAlchemy transaction began with a read. No stale read can
# admit mutation after a concurrent selection. Containment/explicit lifecycle
# provision/stop/deprovision retain their separately checked routes.
RETAINED_MUTATION_TRIGGER = (
    "CREATE TRIGGER retained_mutation_insert BEFORE INSERT ON "
    "managed_instance_mutation_leases "
    "WHEN EXISTS(SELECT 1 FROM development_session_policy WHERE id=1 AND "
    "retain_profiles=1) "
    "AND NEW.operation_kind != 'reconstruction' "
    "AND NOT (NEW.operation_kind='lifecycle' AND EXISTS(SELECT 1 FROM "
    "managed_instance_lifecycle_operations WHERE id=NEW.operation_id "
    "AND managed_instance_id=NEW.managed_instance_id AND state='running' "
    "AND kind IN ('provision','stop','deprovision'))) "
    "AND NOT EXISTS(SELECT 1 FROM development_session_policy p "
    "JOIN development_sessions s ON s.generation=p.active_generation "
    "JOIN managed_instances i ON i.target_id=s.target_id "
    "WHERE p.id=1 AND s.phase='bound' AND s.container_id IS NOT NULL "
    "AND s.target_id=NEW.managed_instance_id AND "
    "i.runtime_revision_id=s.runtime_revision_id) "
    "BEGIN SELECT RAISE(ABORT,'retained selection required'); END"
)

# Keep 0089's historical trigger independent of the additive 0091 table.
# This exception is only an issuance gate; the broker must additionally verify
# the request's one-use proof, live claimed lease and fresh exact inventory.
MANAGED_START_MUTATION_TRIGGER = RETAINED_MUTATION_TRIGGER.replace(
    "AND NOT EXISTS(SELECT 1 FROM development_session_policy p ",
    "AND NOT (NEW.operation_kind='lifecycle' AND EXISTS(SELECT 1 FROM "
    "managed_start_authorizations a "
    "JOIN managed_instance_lifecycle_operations o ON o.id=a.operation_id "
    "JOIN managed_instances i ON i.target_id=a.target_id "
    "JOIN runtime_container_artifacts r ON r.runtime_revision_id=a.runtime_revision_id "
    "WHERE a.operation_id=NEW.operation_id AND a.target_id=NEW.managed_instance_id "
    "AND o.managed_instance_id=a.target_id "
    "AND o.original_managed_instance_id=a.target_id "
    "AND o.kind='start' AND o.state='running' AND o.reconciliation_required=0 "
    "AND i.runtime_revision_id=a.runtime_revision_id AND r.image_id=a.image_id "
    "AND a.runtime_state_revision=i.runtime_state_revision+1 "
    "AND a.consumed_at IS NULL)) "
    "AND NOT EXISTS(SELECT 1 FROM development_session_policy p ",
)

# A private project's existing typed parent receipt replaces human selection.
# This is only the lease issuance backstop: broker/native, credential and
# active-use checks remain mandatory at dispatch.
_PROJECT_PARENTS = (
    ("mcp_workspace_deployment", "mcp_workspace_deployment_operations", "", "o"),
    (
        "mcp_workspace_execution",
        "mcp_workspace_execution_operations",
        "JOIN mcp_workspace_deployment_operations d ON d.id=o.deployment_id "
        "AND d.client_id=o.client_id AND d.workspace_id=o.workspace_id "
        "AND d.target_id=o.target_id "
        "AND d.development_source_id=o.development_source_id ",
        "d",
    ),
    ("mcp_workspace_launch", "mcp_workspace_launches", "", "o"),
)
_PROJECT_EXCEPTIONS = "".join(
    "AND NOT (NEW.operation_kind='"
    + kind
    + "' AND EXISTS(SELECT 1 FROM "
    + table
    + " o "
    + extra_join
    + "JOIN agent_projects p ON p.source_id=o.development_source_id "
    "AND p.client_id=o.client_id JOIN mcp_clients c ON c.id=p.client_id "
    "JOIN mcp_agent_workspace_bindings b ON b.workspace_id=o.workspace_id "
    "AND b.client_id=p.client_id AND b.development_source_id=p.source_id "
    "JOIN agent_workspaces w ON w.id=b.workspace_id "
    "JOIN mcp_client_development_source_grants g ON g.client_id=p.client_id "
    "AND g.development_source_id=p.source_id "
    "JOIN managed_instances i ON i.target_id=o.target_id "
    "JOIN targets t ON t.id=i.target_id "
    "JOIN runtime_revisions r ON r.id=i.runtime_revision_id "
    "WHERE o.id=NEW.operation_id AND o.target_id=NEW.managed_instance_id "
    "AND " + revision_owner + ".runtime_revision_id=i.runtime_revision_id "
    "AND o.generation=w.generation AND w.development_source_id=p.source_id "
    "AND w.state='ready' AND w.bytes_state='present' "
    "AND p.state='seeded' AND c.enabled=1 AND c.managed_lab_role_version=1 "
    "AND t.target_type='managed' AND i.instance_type='disposable' "
    "AND r.approval_state='approved' AND o.state='pending')) "
    for kind, table, extra_join, revision_owner in _PROJECT_PARENTS
)
AGENT_PROJECT_MUTATION_TRIGGER = MANAGED_START_MUTATION_TRIGGER.replace(
    "AND NOT EXISTS(SELECT 1 FROM development_session_policy p ",
    _PROJECT_EXCEPTIONS + "AND NOT EXISTS(SELECT 1 FROM development_session_policy p ",
)
