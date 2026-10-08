"""Read-only household presentation over the existing target inventory.

No network adapters, lifecycle services or authorization mutations belong here.
"""

from contextlib import suppress
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_bridges import (
    ExternalBridgeService,
    ExternalBridgeServiceError,
    ExternalBridgeStatus,
)
from kodi_lab.application.external_targets import (
    ExternalTargetAdministrationService,
    ExternalTargetRead,
)
from kodi_lab.application.production_inspection import (
    ProductionInspectionError,
    evaluate_production_bridge_anchor_state,
)
from kodi_lab.application.screenshot_preferences import (
    ScreenshotPreferenceError,
    ScreenshotPreferenceRead,
    ScreenshotPreferenceService,
)
from kodi_lab.domain import TargetType
from kodi_lab.storage.models import HouseholdOnboardingRow, TargetRow


@dataclass(frozen=True)
class KodiView:
    target: ExternalTargetRead
    bridge: ExternalBridgeStatus | None
    status: Literal["connected", "disconnected", "attention", "unknown"]
    status_label: str
    status_note: str
    bridge_label: str
    access_label: str
    remote_label: str
    preference: ScreenshotPreferenceRead | None
    onboarding_stage: str | None
    diagnostic_code: str | None


class KodiViews:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        targets: ExternalTargetAdministrationService,
        bridges: ExternalBridgeService,
        preferences: ScreenshotPreferenceService,
    ) -> None:
        self._sessions = sessions
        self._targets = targets
        self._bridges = bridges
        self._preferences = preferences

    def managed_names(self) -> list[str]:
        with self._sessions() as session:
            return list(
                session.scalars(
                    select(TargetRow.name)
                    .where(TargetRow.target_type == TargetType.MANAGED)
                    .order_by(TargetRow.name, TargetRow.id)
                )
            )

    def list(self) -> list[KodiView]:
        return [self.present(target) for target in self._targets.list_targets()]

    def get(self, target_id: UUID) -> KodiView:
        # The existing service also enforces external_production classification.
        return self.present(self._targets.get(target_id))

    def present(self, target: ExternalTargetRead) -> KodiView:
        bridge = None
        diagnostic_code = None
        try:
            bridge = self._bridges.status(target.target_id)
        except ExternalBridgeServiceError as error:
            diagnostic_code = error.code
        preference = None
        # Missing preference is unknown, never an implicit enable.
        with suppress(ScreenshotPreferenceError):
            preference = self._preferences.read(target.target_id)
        with self._sessions() as session:
            operation = session.scalar(
                select(HouseholdOnboardingRow).where(
                    HouseholdOnboardingRow.target_id == str(target.target_id)
                )
            )
            stage = None if operation is None else operation.stage
            try:
                evaluate_production_bridge_anchor_state(session, target.target_id)
                anchor_verified = True
            except ProductionInspectionError as error:
                anchor_verified = False
                diagnostic_code = error.code

        status, label, note, bridge_label = connection_presentation(
            target, bridge, stage, anchor_verified, diagnostic_code
        )
        remote_configured = "semantic_remote" in target.configured_capabilities
        suspended = (
            target.production_policy is None
            or target.production_policy.mutations_suspended
        )
        return KodiView(
            target=target,
            bridge=bridge,
            status=status,
            status_label=label,
            status_note=note,
            bridge_label=bridge_label,
            access_label=(
                "Read-only with remote control configured"
                if remote_configured and not suspended
                else "Read-only"
            ),
            remote_label=(
                "Configured, currently suspended"
                if remote_configured and suspended
                else "Configured; separate authorization is required"
                if remote_configured
                else "Not configured"
            ),
            preference=preference,
            onboarding_stage=stage,
            diagnostic_code=diagnostic_code,
        )


def connection_presentation(
    target: ExternalTargetRead,
    bridge: ExternalBridgeStatus | None,
    stage: str | None,
    anchor_verified: bool,
    diagnostic_code: str | None,
) -> tuple[Literal["connected", "disconnected", "attention", "unknown"], str, str, str]:
    """Conservative labels; only the existing trust evaluator qualifies Connected."""
    if target.administrative_state == "deactivated" or stage == "abandoned":
        return (
            "disconnected",
            "Disconnected",
            "Administratively disconnected.",
            "Inactive",
        )
    if stage == "blocked":
        return (
            "attention",
            "Needs attention",
            "Setup needs administrator attention.",
            "Setup blocked",
        )
    if stage is not None and stage != "connected":
        return (
            "attention",
            "Needs attention",
            "Setup is incomplete.",
            "Setup incomplete",
        )
    if target.administrative_state == "disabled":
        if target.verification_state == "unverified":
            return (
                "attention",
                "Needs attention",
                "Setup is incomplete.",
                "Not yet verified",
            )
        return (
            "disconnected",
            "Disconnected",
            "Disabled by an administrator.",
            "Inactive",
        )
    policy = target.production_policy
    if (
        target.verification_state in {"identity_mismatch", "needs_reverification"}
        or target.observation_state in {"authentication_failed", "capability_missing"}
        or (policy is not None and policy.designation_state == "invalidated")
        or (
            bridge is not None
            and (
                bridge.pairing_state in {"needs_reverification", "revoked"}
                or bridge.reachability_state
                in {
                    "identity_mismatch",
                    "authentication_failed",
                    "protocol_unsupported",
                }
            )
        )
    ):
        return (
            "attention",
            "Needs attention",
            "Identity, sign-in or trust needs administrator review.",
            "Review required",
        )
    if (
        target.verification_state == "unverified"
        or policy is None
        or policy.designation_state != "confirmed"
        or (
            bridge is not None
            and bridge.pairing_state in {"unpaired", "pairing_pending"}
        )
    ):
        return (
            "attention",
            "Needs attention",
            "Setup is incomplete.",
            "Not yet verified",
        )
    if not anchor_verified and diagnostic_code not in {None, "bridge_not_paired"}:
        return (
            "attention",
            "Needs attention",
            "The recorded Bridge relationship needs verification.",
            "Review required",
        )
    if target.observation_state == "unreachable" or (
        bridge is not None and bridge.reachability_state == "unreachable"
    ):
        return (
            "disconnected",
            "Disconnected",
            "Temporarily unreachable at the last recorded check.",
            "Unreachable",
        )
    if bridge is None or bridge.reachability_state == "unknown":
        return (
            "unknown",
            "Connection unknown",
            "No reliable Bridge connection observation is available.",
            "Unknown",
        )
    if anchor_verified:
        return (
            "connected",
            "Connected",
            "Verified Bridge relationship at the last recorded check.",
            "Verified relationship",
        )
    return (
        "attention",
        "Needs attention",
        "The recorded Bridge relationship needs verification.",
        "Review required",
    )
