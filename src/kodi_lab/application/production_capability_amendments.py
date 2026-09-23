"""Web-only screenshot amendment for the production capability maximum."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.production_inspection import (
    ProductionInspectionError,
    evaluate_production_inspection_state,
)
from kodi_lab.domain import (
    ExternalAdministrativeState,
    ExternalBridgePairingState,
    ExternalBridgeReachabilityState,
    ExternalVerificationState,
    ProductionDesignationState,
    TargetType,
)
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
    McpProductionTargetScopeRow,
    ProductionApprovalTransitionEventRow,
    ProductionCapabilityAmendmentRow,
    ProductionOperationApprovalRow,
    TargetRow,
)
from kodi_lab.storage.secrets import ExternalBridgeSecretStore, SecretStoreError

PRODUCTION_CONFIGURED_CAPABILITIES = frozenset(
    {"target_inspect", "addon_inventory", "semantic_remote", "screenshot"}
)


class ProductionCapabilityAmendmentError(RuntimeError):
    """Bounded Web-safe capability amendment failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProductionCapabilityAmendmentRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    amendment_id: UUID
    target_id: UUID
    operation: Literal["add", "remove"]
    capability: Literal["screenshot"]
    event_kind: Literal[
        "production_capability_screenshot_added",
        "production_capability_screenshot_removed",
    ]
    before_configured_capabilities: tuple[str, ...]
    after_configured_capabilities: tuple[str, ...]
    native_verified_capabilities: tuple[str, ...]
    identity_generation: int = Field(ge=1)
    policy_revision: int = Field(ge=1)
    production_policy_revision_binding: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=64)
    occurred_at: datetime


def _text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _closed_json(values: frozenset[str]) -> str:
    return json.dumps(sorted(values), separators=(",", ":"))


def _closed_set(value: str) -> frozenset[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ProductionCapabilityAmendmentError(
            "configured_capabilities_invalid"
        ) from error
    if (
        not isinstance(parsed, list)
        or any(not isinstance(item, str) for item in parsed)
        or len(parsed) != len(set(parsed))
    ):
        raise ProductionCapabilityAmendmentError("configured_capabilities_invalid")
    result = frozenset(parsed)
    if not result <= PRODUCTION_CONFIGURED_CAPABILITIES:
        raise ProductionCapabilityAmendmentError("configured_capabilities_invalid")
    return result


class ProductionCapabilityAmendmentService:
    """Narrow transactional amendment of only production `screenshot` maximum."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        bridge_secrets: ExternalBridgeSecretStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._bridge_secrets = bridge_secrets
        self._clock = clock

    def amend_production_capability_maximum(
        self,
        target_id: UUID,
        *,
        operation: Literal["add", "remove"],
        capability: Literal["screenshot"],
        expected_configured_capabilities: frozenset[str],
        expected_policy_revision: int,
        administrator_reference: str = "local_administrator",
    ) -> ProductionCapabilityAmendmentRead:
        if operation not in {"add", "remove"} or capability != "screenshot":
            raise ProductionCapabilityAmendmentError("capability_amendment_unsupported")
        if (
            not expected_configured_capabilities
            or not expected_configured_capabilities
            <= PRODUCTION_CONFIGURED_CAPABILITIES
        ):
            raise ProductionCapabilityAmendmentError("configured_capabilities_invalid")
        if expected_policy_revision < 1:
            raise ProductionCapabilityAmendmentError("policy_revision_stale")
        actor = administrator_reference.strip()
        if not actor or len(actor) > 64:
            raise ProductionCapabilityAmendmentError("capability_confirmation_required")

        now = self._clock()
        before_json = _closed_json(expected_configured_capabilities)
        if operation == "add":
            if "screenshot" in expected_configured_capabilities:
                raise ProductionCapabilityAmendmentError(
                    "capability_already_configured"
                )
            after = expected_configured_capabilities | {"screenshot"}
        else:
            if "screenshot" not in expected_configured_capabilities:
                raise ProductionCapabilityAmendmentError("capability_not_configured")
            after = expected_configured_capabilities - {"screenshot"}
        after = frozenset(after)
        after_json = _closed_json(after)
        amendment_id = uuid4()
        retired_references: tuple[UUID, ...] = ()
        verified: frozenset[str]
        generation: int
        resulting_revision = expected_policy_revision + 1

        with self._sessions.begin() as session:
            target = session.get(TargetRow, str(target_id))
            external = session.get(ExternalKodiTargetRow, str(target_id))
            policy = session.get(ExternalProductionPolicyRow, str(target_id))
            if (
                target is None
                or target.target_type is not TargetType.EXTERNAL_PRODUCTION
                or external is None
                or policy is None
            ):
                raise ProductionCapabilityAmendmentError("target_not_found")
            if (
                external.administrative_state is not ExternalAdministrativeState.ENABLED
                or external.verification_state is not ExternalVerificationState.VERIFIED
                or policy.designation_state is not ProductionDesignationState.CONFIRMED
            ):
                raise ProductionCapabilityAmendmentError(
                    "production_lifecycle_not_confirmed"
                )
            try:
                evaluate_production_inspection_state(
                    session,
                    target_id,
                    required_capabilities=frozenset({"target_inspect"}),
                )
            except ProductionInspectionError as error:
                raise ProductionCapabilityAmendmentError(
                    "production_lifecycle_not_confirmed"
                ) from error
            configured = _closed_set(external.configured_capabilities_json)
            verified = _closed_set(external.verified_capabilities_json)
            if configured != expected_configured_capabilities:
                raise ProductionCapabilityAmendmentError(
                    "configured_capabilities_stale"
                )
            if policy.policy_revision != expected_policy_revision:
                raise ProductionCapabilityAmendmentError("policy_revision_stale")
            generation = policy.identity_generation
            previous_native_revision = external.production_policy_revision
            if previous_native_revision is None:
                raise ProductionCapabilityAmendmentError(
                    "production_lifecycle_not_confirmed"
                )

            policy_result = cast(
                CursorResult[Any],
                session.execute(
                    update(ExternalProductionPolicyRow)
                    .where(
                        ExternalProductionPolicyRow.target_id == str(target_id),
                        ExternalProductionPolicyRow.policy_revision
                        == expected_policy_revision,
                    )
                    .values(
                        policy_revision=resulting_revision,
                        updated_at=_text(now),
                    )
                    .execution_options(synchronize_session=False)
                ),
            )
            if policy_result.rowcount != 1:
                raise ProductionCapabilityAmendmentError("policy_revision_stale")

            if operation == "remove":
                retired_references = self._tombstone_removed_screenshot(
                    session, target_id=target_id, now=now, actor=actor
                )

            event_kind: Literal[
                "production_capability_screenshot_added",
                "production_capability_screenshot_removed",
            ] = (
                "production_capability_screenshot_added"
                if operation == "add"
                else "production_capability_screenshot_removed"
            )
            self._write_audit(
                session,
                amendment_id=amendment_id,
                target_id=target_id,
                operation=operation,
                event_kind=event_kind,
                before_json=before_json,
                after_json=after_json,
                resulting_revision=resulting_revision,
                actor=actor,
                now=now,
            )
            session.flush()

            external_result = cast(
                CursorResult[Any],
                session.execute(
                    update(ExternalKodiTargetRow)
                    .where(
                        ExternalKodiTargetRow.target_id == str(target_id),
                        ExternalKodiTargetRow.configured_capabilities_json
                        == before_json,
                        ExternalKodiTargetRow.production_policy_revision
                        == previous_native_revision,
                    )
                    .values(
                        configured_capabilities_json=after_json,
                        production_policy_revision=resulting_revision,
                    )
                    .execution_options(synchronize_session=False)
                ),
            )
            if external_result.rowcount != 1:
                raise ProductionCapabilityAmendmentError(
                    "configured_capabilities_stale"
                )

        retirement_error = False
        for reference in retired_references:
            try:
                self._bridge_secrets.retire(reference)
            except SecretStoreError:
                # The committed database tombstone removed every authority-bearing
                # reference.  An undeletable orphan can therefore never authenticate.
                retirement_error = True
        if retirement_error:
            raise ProductionCapabilityAmendmentError("bridge_secret_retirement_failed")

        return ProductionCapabilityAmendmentRead(
            amendment_id=amendment_id,
            target_id=target_id,
            operation=operation,
            capability="screenshot",
            event_kind=event_kind,
            before_configured_capabilities=tuple(
                sorted(expected_configured_capabilities)
            ),
            after_configured_capabilities=tuple(sorted(after)),
            native_verified_capabilities=tuple(sorted(verified)),
            identity_generation=generation,
            policy_revision=resulting_revision,
            production_policy_revision_binding=resulting_revision,
            actor=actor,
            occurred_at=now,
        )

    @staticmethod
    def _write_audit(
        session: Session,
        *,
        amendment_id: UUID,
        target_id: UUID,
        operation: str,
        event_kind: str,
        before_json: str,
        after_json: str,
        resulting_revision: int,
        actor: str,
        now: datetime,
    ) -> None:
        session.add(
            ProductionCapabilityAmendmentRow(
                id=str(amendment_id),
                target_id=str(target_id),
                operation=operation,
                capability="screenshot",
                event_kind=event_kind,
                before_configured_json=before_json,
                after_configured_json=after_json,
                resulting_policy_revision=resulting_revision,
                actor=actor,
                occurred_at=_text(now),
            )
        )

    @staticmethod
    def _tombstone_removed_screenshot(
        session: Session, *, target_id: UUID, now: datetime, actor: str
    ) -> tuple[UUID, ...]:
        timestamp = _text(now)
        references: list[UUID] = []
        pairing = session.get(ExternalBridgePairingRow, str(target_id))
        if pairing is None:
            pairing = ExternalBridgePairingRow(
                target_id=str(target_id),
                pairing_state=ExternalBridgePairingState.REVOKED,
                reachability_state=ExternalBridgeReachabilityState.UNKNOWN,
                observed_capabilities_json="[]",
                verified_capabilities_json="[]",
                production_bridge_trust_revision=1,
                revoked_at=timestamp,
            )
            session.add(pairing)
        else:
            for value in (
                pairing.pairing_token_reference,
                pairing.bridge_secret_reference,
            ):
                if value is not None:
                    references.append(UUID(value))
            pairing.pairing_state = ExternalBridgePairingState.REVOKED
            pairing.reachability_state = ExternalBridgeReachabilityState.UNKNOWN
            pairing.pairing_token_reference = None
            pairing.pairing_token_expires_at = None
            pairing.bridge_secret_reference = None
            pairing.verified_capabilities_json = "[]"
            pairing.production_bridge_trust_revision = (
                pairing.production_bridge_trust_revision or 0
            ) + 1
            pairing.production_identity_generation = None
            pairing.reviewed_build_id = None
            pairing.production_confirmed_by = None
            pairing.production_confirmed_at = None
            pairing.revoked_at = timestamp

        scopes = session.scalars(
            select(McpProductionTargetScopeRow).where(
                McpProductionTargetScopeRow.target_id == str(target_id),
                McpProductionTargetScopeRow.scope.in_(
                    ("visual", "navigate", "request_transient_control")
                ),
                McpProductionTargetScopeRow.state == "active",
            )
        ).all()
        for scope in scopes:
            scope.state = "revoked"
            scope.revoked_at = timestamp
            scope.revoked_by = actor

        approvals = session.scalars(
            select(ProductionOperationApprovalRow).where(
                ProductionOperationApprovalRow.target_id == str(target_id),
                ProductionOperationApprovalRow.state.in_(("pending", "approved")),
            )
        ).all()
        for approval in approvals:
            previous = approval.state
            approval.state = "cancelled"
            approval.terminal_reason = "capability_lost"
            session.add(
                ProductionApprovalTransitionEventRow(
                    id=str(uuid4()),
                    approval_id=approval.id,
                    event_kind="cancelled",
                    from_state=previous,
                    to_state="cancelled",
                    terminal_reason="capability_lost",
                    actor_kind="system",
                    actor_reference="screenshot_removed",
                    linked_remote_receipt_id=None,
                    occurred_at=timestamp,
                )
            )
        return tuple(references)
