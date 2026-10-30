"""Stored prerequisites only: no probe, repair, secret read or readiness claim."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.managed_bridge_trust import (
    ManagedBridgeTrustError,
    require_approved_identity,
)
from kodi_lab.broker.managed_bridge_trust import approved_bridge_build
from kodi_lab.domain import TargetType
from kodi_lab.kodi.bridge import (
    BridgeIdentity,
    ExternalBridgeClient,
    ExternalBridgeError,
)
from kodi_lab.storage.models import ManagedBridgeTrustRow, ManagedInstanceRow, TargetRow
from kodi_lab.storage.repositories import RuntimeExecutionProfileRepository


class CapabilityPrerequisitesUnavailable(RuntimeError):
    pass


class CapabilityPrerequisite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    capability: Literal[
        "ui_inspection",
        "log_read",
        "manifest_launch",
        "screenshot",
        "native_remote",
        "direct_workspace_deployment",
    ]
    bridge_prerequisite: Literal["present", "blocked", "unknown", "not_required"]
    reason: Literal[
        "native_fallback_requires_observation",
        "bridge_recovery_required",
        "bridge_identity_invalid",
        "bridge_runtime_mismatch",
        "bridge_not_paired",
        "stored_identity_requires_observation",
        "log_capability_missing",
        "launch_upgrade_required",
        "bridge_independent",
    ]
    live_readiness: Literal["unknown"] = "unknown"


class ManagedCapabilityPrerequisites(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_revision: Literal[1] = 1
    assessment_scope: Literal["stored_prerequisites"] = "stored_prerequisites"
    target_id: UUID
    runtime_revision_id: UUID
    kodi_release: str | None
    checked_at: datetime
    trust_state: Literal["no_record", "prepared", "active", "invalidated", "revoked"]
    trust_generation: int | None
    candidate_addon_version: str | None
    last_authenticated_addon_version: str | None
    last_authenticated_at: datetime | None
    last_authenticated_container_id: str | None
    current_container_binding: Literal["not_observed"] = "not_observed"
    native_readiness: Literal["unknown"] = "unknown"
    prompt_state: Literal["unknown"] = "unknown"
    checks: tuple[CapabilityPrerequisite, ...]
    next_action: Literal[
        "inspect_native", "review_bridge", "resume_reviewed_bridge", "review_history"
    ]
    action_authority: Literal["web_administrator"] = "web_administrator"
    action_target_state: Literal["running", "any"]
    mutation_admission_required: Literal[True] = True


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except ValueError:
        return None


class ManagedCapabilityPrerequisiteService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def inspect(
        self, target_id: UUID, *, read_session: Session | None = None
    ) -> ManagedCapabilityPrerequisites:
        with (
            self._sessions() if read_session is None else nullcontext(read_session)
        ) as session:
            target = session.get(TargetRow, str(target_id))
            instance = session.get(ManagedInstanceRow, str(target_id))
            if (
                target is None
                or target.target_type is not TargetType.MANAGED
                or instance is None
            ):
                raise CapabilityPrerequisitesUnavailable("managed_target_unavailable")
            profile = RuntimeExecutionProfileRepository(session).get(
                UUID(instance.runtime_revision_id)
            )
            row = session.get(ManagedBridgeTrustRow, str(target_id))
            release = None if profile is None else profile.kodi_release.canonical
            candidate = (
                approved_bridge_build(release)["addon_version"]
                if release in {"19.4", "20.5", "21.3", "22.0-rc.1"}
                else None
            )
            checks: list[CapabilityPrerequisite] = []
            identity: BridgeIdentity | None = None
            common: Literal[
                "bridge_identity_invalid",
                "bridge_runtime_mismatch",
                "bridge_recovery_required",
            ] = (
                "bridge_runtime_mismatch"
                if row and row.runtime_revision_id != instance.runtime_revision_id
                else "bridge_recovery_required"
            )
            if row is not None and row.state == "active":
                common = "bridge_identity_invalid"
                try:
                    if row.runtime_revision_id != instance.runtime_revision_id:
                        common = "bridge_runtime_mismatch"
                    elif (
                        release is not None
                        and row.identity_json
                        and row.secret_reference
                        and row.bridge_id
                    ):
                        parsed = ExternalBridgeClient._identity(
                            json.loads(row.identity_json)
                        )
                        require_approved_identity(parsed, release, established=True)
                        if (
                            str(parsed.bridge_id) == row.bridge_id
                            and _timestamp(row.established_at) is not None
                        ):
                            identity = parsed
                except (
                    ValueError,
                    TypeError,
                    KeyError,
                    ExternalBridgeError,
                    ManagedBridgeTrustError,
                ):
                    identity = None

            def check(capability: str, state: str, reason: str) -> None:
                checks.append(
                    CapabilityPrerequisite.model_validate(
                        dict(
                            capability=capability,
                            bridge_prerequisite=state,
                            reason=reason,
                        )
                    )
                )

            if row is None:
                check(
                    "ui_inspection", "unknown", "native_fallback_requires_observation"
                )
                check("log_read", "blocked", "bridge_not_paired")
                check("manifest_launch", "blocked", "launch_upgrade_required")
            elif identity is None:
                # Existing non-active/drifted trust denies native fallback.
                for capability in ("ui_inspection", "log_read", "manifest_launch"):
                    check(capability, "blocked", common)
            else:
                check(
                    "ui_inspection", "present", "stored_identity_requires_observation"
                )
                log_present = "log_read" in identity.capabilities
                check(
                    "log_read",
                    "present" if log_present else "blocked",
                    "stored_identity_requires_observation"
                    if log_present
                    else "log_capability_missing",
                )
                try:
                    assert release is not None
                    require_approved_identity(identity, release)
                    launch_present = identity.addon_version == candidate
                except ManagedBridgeTrustError:
                    launch_present = False
                check(
                    "manifest_launch",
                    "present" if launch_present else "blocked",
                    "stored_identity_requires_observation"
                    if launch_present
                    else "launch_upgrade_required",
                )
            for capability in (
                "screenshot",
                "native_remote",
                "direct_workspace_deployment",
            ):
                check(capability, "not_required", "bridge_independent")
            runtime_matches = (
                row is not None
                and row.runtime_revision_id == instance.runtime_revision_id
            )
            resume = bool(
                row
                and row.state == "prepared"
                and row.reviewed_installation
                and runtime_matches
                and candidate is not None
            )
            action = (
                "resume_reviewed_bridge"
                if resume
                else "inspect_native"
                if identity is not None
                else "review_history"
                if row and row.state == "active"
                else "review_bridge"
            )
            return ManagedCapabilityPrerequisites(
                target_id=target_id,
                runtime_revision_id=UUID(instance.runtime_revision_id),
                kodi_release=release,
                checked_at=datetime.now(UTC),
                trust_state="no_record"
                if row is None
                else cast(
                    Literal["prepared", "active", "invalidated", "revoked"], row.state
                ),
                trust_generation=None if row is None else row.generation,
                candidate_addon_version=candidate,
                last_authenticated_addon_version=None
                if identity is None
                else identity.addon_version,
                last_authenticated_at=None
                if identity is None or row is None
                else _timestamp(row.established_at),
                last_authenticated_container_id=None
                if identity is None or row is None
                else row.container_id,
                checks=tuple(checks),
                next_action=cast(
                    Literal[
                        "inspect_native",
                        "review_bridge",
                        "resume_reviewed_bridge",
                        "review_history",
                    ],
                    action,
                ),
                action_target_state="running" if resume else "any",
            )
