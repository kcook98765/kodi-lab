"""Read-only Web wording. These hints never determine operation eligibility."""

from __future__ import annotations

from kodi_lab.application.runtime_status import ManagedRuntimeStatus

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


def managed_next_step(
    instance: ManagedRuntimeStatus, *, blocking: bool, busy: bool
) -> str:
    """Suggest only a review of existing controls; never grant or run an action."""
    if blocking:
        return (
            "Review the retained operation before continuing. Its outcome is "
            "uncertain; do not repeat it."
        )
    if busy:
        return (
            "An operation is in progress. Open the instance to follow its "
            "recorded outcome."
        )
    if instance.failure_code:
        return ERROR_MESSAGES.get(
            instance.failure_code,
            (
                "A status check needs attention. Open the instance and review its "
                "technical details before changing it."
            ),
        )
    if instance.usability_state == "usable":
        return ""
    if instance.container_state == "unknown" or instance.process_state in (
        "unknown",
        "unknown_error",
    ):
        return (
            "Status is uncertain. Review the instance diagnostics before changing it."
        )
    if instance.readiness_state in ("checking",):
        return "Readiness is being checked. Open the instance to follow progress."
    if "provision" in instance.allowed_actions:
        return "Open the instance and review Set up to prepare its stopped runtime."
    if "start" in instance.allowed_actions:
        return "Open the instance and review Start when you are ready to use Kodi."
    return "Open the instance to review its current status and available controls."
