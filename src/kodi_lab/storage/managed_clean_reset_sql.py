"""Durable clean Reset intent and atomic admission backstops."""

RESET_IMMUTABLE = """
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
"""

RESET_MUTATION_GUARD = """
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
"""

RESET_VIEW_GUARD = """
CREATE TRIGGER clean_reset_view_insert BEFORE INSERT ON novnc_sessions
WHEN EXISTS(SELECT 1 FROM managed_clean_resets r
 WHERE r.target_id=NEW.instance_id AND r.phase NOT IN ('complete','blocked'))
BEGIN SELECT RAISE(ABORT,'clean reset pending'); END
"""
RESET_REMOTE_GUARD = RESET_VIEW_GUARD.replace(
    "clean_reset_view_insert", "clean_reset_remote_insert"
).replace("ON novnc_sessions", "ON managed_remote_guards")

RESET_TRIGGERS = (
    RESET_IMMUTABLE,
    RESET_MUTATION_GUARD,
    RESET_VIEW_GUARD,
    RESET_REMOTE_GUARD,
)
