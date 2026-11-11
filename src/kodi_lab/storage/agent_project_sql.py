"""Immutable owner and request identity for agent-created project sources."""

PROJECT_IMMUTABLE = (
    "CREATE TRIGGER agent_project_immutable BEFORE UPDATE ON agent_projects WHEN "
    "NEW.source_id<>OLD.source_id OR NEW.client_id<>OLD.client_id OR "
    "NEW.request_id<>OLD.request_id OR "
    "NEW.parameter_fingerprint<>OLD.parameter_fingerprint "
    "OR NEW.created_at<>OLD.created_at OR "
    "(OLD.state='seeded' AND NEW.state<>'seeded') "
    "BEGIN SELECT RAISE(ABORT,'agent_project_identity_immutable'); END"
)
PROJECT_NO_DELETE = (
    "CREATE TRIGGER agent_project_no_delete BEFORE DELETE ON agent_projects "
    "BEGIN SELECT RAISE(ABORT,'agent_project_history_protected'); END"
)
