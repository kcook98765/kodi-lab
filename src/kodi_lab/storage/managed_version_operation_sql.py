"""Immutable intent and child identity, shared by ORM fixtures and Alembic."""

IMMUTABLE_VERSION_OPERATION = """
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
"""
