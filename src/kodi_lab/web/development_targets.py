"""External Development presentation only; no production trust or authorization."""

from dataclasses import dataclass

from kodi_lab.application.external_bridges import ExternalBridgeStatus
from kodi_lab.application.external_targets import ExternalTargetRead

_CAPABILITIES = {
    "target_inspect": "Kodi health inspection",
    "addon_inventory": "Installed add-on inspection",
    "semantic_remote": "Kodi navigation",
    "screenshot": "Screenshot capture",
    "workspace_deployment": "Add-on transfer",
    "fixed_execution": "Add-on test execution",
    "ui_observation": "Screen information",
    "log_read": "Diagnostic log reading",
}


def capability_names(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_CAPABILITIES.get(value, value.replace("_", " ")) for value in values)


@dataclass(frozen=True)
class DevelopmentTargetView:
    status: str
    label: str
    note: str
    native_label: str
    observation_label: str
    pairing_label: str
    reachability_label: str
    warnings: tuple[str, ...]
    next_step: str
    advanced_open: bool
    reconnect_available: bool
    reconnect_note: str
    native_capabilities: tuple[str, ...]
    bridge_capabilities: tuple[str, ...]
    transfer_note: str
    reconnect_stage: str | None
    cleanup_pending: bool


def development_target_view(
    target: ExternalTargetRead,
    bridge: ExternalBridgeStatus,
    *,
    reconnect_stage: str | None = None,
    cleanup_pending: bool = False,
) -> DevelopmentTargetView:
    if target.target_class != "external_development":
        raise ValueError(
            "External Development presentation requires its stored category"
        )
    admin = target.administrative_state
    native = target.verification_state
    pairing = bridge.pairing_state
    reachability = bridge.reachability_state
    native_only = not {"screenshot", "workspace_deployment"}.intersection(
        target.configured_capabilities
    )
    warnings: list[str] = []
    if native == "identity_mismatch":
        warnings.append("Kodi identity does not match the recorded connection.")
    elif native == "needs_reverification":
        warnings.append("Kodi identity needs verification again.")
    if target.pending_observation_id is not None and native != "verified":
        warnings.append(
            "A recorded Kodi identity is waiting for confirmation. Review it in "
            "Advanced options before confirming."
        )
    if target.observation_state == "authentication_failed":
        warnings.append("Kodi sign-in failed. Review credentials and reconnect.")
    elif target.observation_state == "capability_missing":
        warnings.append(
            "Kodi did not verify the required support. Review its settings."
        )
    if reachability == "identity_mismatch":
        warnings.append("Bridge identity does not match the recorded connection.")
    elif reachability == "authentication_failed":
        warnings.append("Bridge authentication failed. Review its saved pairing.")
    elif reachability == "protocol_unsupported":
        warnings.append(
            "This Bridge version could not be verified. Review compatibility."
        )
    if pairing == "needs_reverification":
        warnings.append("Bridge verification needs review before use.")
    if pairing == "pairing_pending" or bridge.pairing_token_pending:
        warnings.append(
            "Pairing is unfinished. Review the saved identity before confirming; "
            "do not submit another code to recover an uncertain response."
        )
    if reconnect_stage:
        warnings.append(
            "A saved reconnect is unfinished. Open Reconnect Kodi to review or "
            "recover the existing request before starting another pairing attempt."
        )
    if cleanup_pending:
        if admin == "deactivated":
            warnings.append(
                "Credential cleanup is unfinished. Review Advanced options and retry "
                "the existing connection retirement."
            )
        else:
            warnings.append(
                "Credential cleanup is unfinished. Ask an administrator to review "
                "the saved cleanup obligation before changing this connection."
            )

    reconnect_note = ""
    if admin == "deactivated":
        reconnect_note = "this registration is retired"
    elif admin != "enabled":
        reconnect_note = "confirm Kodi identity and enable this registration first"
    elif native != "verified":
        reconnect_note = "review and confirm Kodi identity in Advanced options first"
    elif native_only and pairing in {"unpaired", "revoked"}:
        reconnect_note = "this registration's maximum permits native inspection only"
    elif (
        pairing == "pairing_pending" or bridge.pairing_token_pending
    ) and not reconnect_stage:
        reconnect_note = (
            "resolve the unfinished Bridge pairing in Advanced options first"
        )
    reconnect_available = not reconnect_note
    if admin == "deactivated":
        status, label, note = (
            "retired",
            "Retired connection",
            "This Lab registration is retired. Kodi and its files are preserved.",
        )
        next_step = (
            "Connect Kodi again to create a new registration. Advanced options "
            "retain audit history and credential-cleanup retry."
        )
    elif warnings:
        status, label, note = (
            "attention",
            "Needs attention",
            "Recorded identity or setup needs review.",
        )
        next_step = (
            "Review the warning and open Advanced options for the existing "
            "verification or recovery controls."
        )
    elif native != "verified":
        status, label, note = (
            "attention",
            "Kodi not verified",
            "Kodi identity has not been confirmed.",
        )
        next_step = (
            "Check and confirm Kodi identity in Advanced options, then enable the "
            "connection. Disable an enabled registration before checking its identity."
        )
    elif admin == "disabled":
        status, label, note = (
            "disabled",
            "Connection disabled",
            "This connection was disabled by an administrator.",
        )
        next_step = (
            "Review the recorded identity and enable this registration in "
            "Advanced options to continue."
        )
    elif native_only and pairing in {"unpaired", "revoked"}:
        status, label, note = (
            "native_only",
            "Native inspection only",
            "Bridge pairing is outside this connection's configured maximum.",
        )
        next_step = (
            "Keep this connection for native inspection. If Bridge features are "
            "needed, retire this connection in Advanced options, then connect it "
            "again with screenshot or add-on transfer in its initial maximum. "
            "The retired connection's history is preserved."
        )
    elif pairing in {"unpaired", "revoked"}:
        status, label, note = (
            "attention",
            "Bridge not paired",
            "Kodi identity is verified; Bridge pairing is still needed.",
        )
        next_step = (
            "Pair the Bridge using the current four-character code in Advanced "
            "options, or use the existing connection guide."
        )
    elif target.observation_state == "unreachable" or reachability == "unreachable":
        status, label, note = (
            "unavailable",
            "Connection unavailable",
            "Kodi or Bridge was unreachable at the last recorded check.",
        )
        next_step = (
            "Check that Kodi and Bridge are running on the other device and "
            "connected to your network, then review Reconnect Kodi."
        )
    elif target.observation_state != "reachable" or reachability != "reachable":
        status, label, note = (
            "unknown",
            "Connection not confirmed",
            "No reliable successful connection check is recorded for Kodi and Bridge.",
        )
        next_step = "Use Reconnect Kodi to review the existing connection."
    else:
        status, label, note = (
            "verified",
            "Connection verified",
            "Kodi identity and Bridge checks were successful at the last "
            "recorded verification.",
        )
        next_step = (
            "Review agent permissions and development prerequisites before "
            "testing an add-on."
        )

    if "workspace_deployment" not in bridge.verified_capabilities:
        transfer_note = "Add-on transfer support has not been verified for this Bridge."
    elif "workspace_deployment" not in target.configured_capabilities:
        transfer_note = (
            "Bridge add-on transfer support is recorded, but it is outside this "
            "connection's configured maximum. Review Advanced options."
        )
    else:
        transfer_note = (
            "Bridge add-on transfer support is recorded. This does not establish "
            "deployment readiness or agent authority."
        )
    return DevelopmentTargetView(
        status=status,
        label=label,
        note=note,
        native_label={
            "unverified": "Not verified",
            "verified": "Verified identity",
            "needs_reverification": "Verification required again",
            "identity_mismatch": "Identity mismatch",
        }[native],
        observation_label={
            None: "Not checked",
            "reachable": "Reachable at last check",
            "unreachable": "Unreachable at last check",
            "authentication_failed": "Sign-in failed",
            "capability_missing": "Required support not verified",
        }[target.observation_state],
        pairing_label={
            "unpaired": "Not paired",
            "pairing_pending": "Pairing unfinished",
            "paired": "Paired",
            "needs_reverification": "Verification required again",
            "revoked": "Pairing revoked",
        }[pairing],
        reachability_label={
            "unknown": "Not checked",
            "reachable": "Reachable at last check",
            "unreachable": "Unreachable at last check",
            "authentication_failed": "Authentication failed",
            "identity_mismatch": "Identity mismatch",
            "protocol_unsupported": "Unsupported Bridge protocol",
        }[reachability],
        warnings=tuple(warnings),
        next_step=next_step,
        advanced_open=bool(warnings) or status == "attention" or admin == "deactivated",
        reconnect_available=reconnect_available,
        reconnect_note=reconnect_note,
        native_capabilities=capability_names(target.verified_capabilities),
        bridge_capabilities=capability_names(bridge.verified_capabilities),
        transfer_note=transfer_note,
        reconnect_stage=reconnect_stage,
        cleanup_pending=cleanup_pending,
    )
