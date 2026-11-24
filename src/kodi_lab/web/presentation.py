"""Read-only Web wording. These hints never determine operation eligibility."""

from __future__ import annotations

STATE_LABELS = {
    "unprovisioned": "Not set up yet",
    "provisioned": "Set up",
    "unknown": "Status unknown",
    "unknown_error": "Could not check status",
    "missing": "Not present",
    "created": "Created, not started",
    "running": "Running",
    "exited": "Stopped",
    "stopped": "Stopped",
    "not_running": "Not running",
    "not_checked": "Not checked yet",
    "not_usable": "Not ready to use",
    "usable": "Ready to use",
    "unreachable": "Could not connect",
    "available": "Responding",
    "ready": "Ready",
    "idle": "No readiness check running",
    "queued": "Waiting to begin",
    "starting": "Starting up",
    "checking": "Checking readiness",
    "pending": "Waiting for a check",
    "failed": "Needs attention",
    "cancelled": "Check cancelled",
    "reconciliation_required": "Review required before continuing",
    "transitional": "Changing state; wait for a confirmed outcome",
    "responsive": "Responding",
    "unavailable": "Could not connect",
    "protocol_error": "Unexpected response; review diagnostics",
    "timed_out": "Readiness check timed out",
}

ERROR_MESSAGES = {
    "broker_unavailable": (
        "Kodi Lab cannot reach its runtime service. Review diagnostics "
        "before changing this instance."
    ),
    "runtime_revision_mismatch": (
        "The runtime no longer matches this instance's recorded version. "
        "Review reconstruction before changing it."
    ),
    "reconciliation_required": (
        "An earlier action has an uncertain outcome. Review its retained "
        "operation; do not repeat it."
    ),
    "deployment_ambiguous": (
        "The deployment outcome is uncertain. Inspect the original "
        "operation before attempting another deployment."
    ),
    "operation_in_progress": (
        "Another operation is in progress. Wait for its recorded outcome."
    ),
    "managed_capacity_full": (
        "The running-instance limit has been reached. Review managed "
        "resources before starting another instance."
    ),
    "screenshots_disabled": (
        "Screen capture is disabled by policy. Review the existing screenshot settings."
    ),
}

ERROR_MESSAGES.update(
    runtime_service_unavailable=ERROR_MESSAGES["broker_unavailable"],
    broker_runtime_dispatch_unconfirmed=ERROR_MESSAGES["reconciliation_required"],
    lifecycle_operation_conflict=ERROR_MESSAGES["operation_in_progress"],
    lifecycle_conflict=(
        "The current instance state does not allow that action. "
        "Refresh and review its available controls."
    ),
)


def state_label(value: str) -> str:
    """Unknown vocabulary stays visibly uncertain and retains the exact code."""
    return STATE_LABELS.get(value, f"Unrecognized state ({value})")
