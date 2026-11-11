"""Immutable maintenance identity and retained child evidence."""

BRIDGE_PREPARATION_IMMUTABLE = """
CREATE TRIGGER bridge_preparation_immutable BEFORE UPDATE ON managed_bridge_preparations
WHEN NEW.id IS NOT OLD.id OR NEW.request_id IS NOT OLD.request_id
 OR NEW.create_operation_id IS NOT OLD.create_operation_id
 OR NEW.target_id IS NOT OLD.target_id
 OR NEW.runtime_revision_id IS NOT OLD.runtime_revision_id
 OR NEW.container_id IS NOT OLD.container_id OR NEW.image_id IS NOT OLD.image_id
 OR NEW.keep_running IS NOT OLD.keep_running
 OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'Bridge preparation intent is immutable'); END
"""

CREATE_BRIDGE_IMMUTABLE = """
CREATE TRIGGER create_bridge_required_immutable BEFORE UPDATE OF bridge_required
ON create_kodi_operations WHEN NEW.bridge_required IS NOT OLD.bridge_required
BEGIN SELECT RAISE(ABORT,'Create Bridge intent is immutable'); END
"""

BRIDGE_CHILD_IMMUTABLE = """
CREATE TRIGGER bridge_preparation_child_immutable
BEFORE UPDATE ON managed_bridge_preparation_children
BEGIN SELECT RAISE(ABORT,'Bridge preparation child is immutable'); END
"""
