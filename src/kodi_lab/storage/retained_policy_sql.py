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
