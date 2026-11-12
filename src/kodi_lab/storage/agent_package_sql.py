"""Retain exact owner/request identity for immutable package intake."""

PACKAGE_IMMUTABLE = (
    "CREATE TRIGGER agent_package_immutable BEFORE UPDATE ON agent_packages WHEN "
    "NEW.id<>OLD.id OR NEW.client_id<>OLD.client_id OR "
    "NEW.request_id<>OLD.request_id OR NEW.archive_sha256<>OLD.archive_sha256 OR "
    "NEW.provenance_reference<>OLD.provenance_reference OR "
    "NEW.created_at<>OLD.created_at OR "
    "(OLD.state<>'reserved' AND NEW.state<>OLD.state) "
    "BEGIN SELECT RAISE(ABORT,'agent_package_identity_immutable'); END"
)
PACKAGE_NO_DELETE = (
    "CREATE TRIGGER agent_package_no_delete BEFORE DELETE ON agent_packages "
    "BEGIN SELECT RAISE(ABORT,'agent_package_history_protected'); END"
)
