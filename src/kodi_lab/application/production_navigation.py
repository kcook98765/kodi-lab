"""Server-owned production navigation taxonomy, transport consent, and rates."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.production_inspection import (
    ProductionInspectionError,
    evaluate_production_bridge_anchor_state,
    evaluate_production_inspection_state,
)
from kodi_lab.application.production_policy import advance_production_policy
from kodi_lab.domain import ExternalProtocolKind, TargetType
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.storage.models import (
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
    McpSecurityEventRow,
    ProductionMutationControlRow,
    ProductionNativeControlHttpAcknowledgementRow,
    TargetRow,
)

PRODUCTION_NATIVE_CONTROL_TRANSPORT_POLICY_REVISION = 1


class ProductionActionClass(StrEnum):
    NAVIGATION = "navigation"
    TRANSIENT_CONTROL = "transient_control"
    PROHIBITED = "prohibited"


PRODUCTION_NAVIGATION_ACTIONS = frozenset(
    {
        BrowserRemoteAction.UP,
        BrowserRemoteAction.DOWN,
        BrowserRemoteAction.LEFT,
        BrowserRemoteAction.RIGHT,
        BrowserRemoteAction.BACK,
        BrowserRemoteAction.HOME,
        BrowserRemoteAction.INFO,
    }
)
PRODUCTION_TRANSIENT_CONTROL_ACTIONS = frozenset(
    {
        BrowserRemoteAction.PLAY_PAUSE,
        BrowserRemoteAction.STOP,
        BrowserRemoteAction.PREVIOUS,
        BrowserRemoteAction.NEXT,
        BrowserRemoteAction.VOLUME_UP,
        BrowserRemoteAction.VOLUME_DOWN,
        BrowserRemoteAction.MUTE_TOGGLE,
    }
)
PRODUCTION_PROHIBITED_ACTIONS = frozenset(
    {BrowserRemoteAction.CONTEXT_MENU, BrowserRemoteAction.SELECT}
)


def classify_production_action(action: BrowserRemoteAction) -> ProductionActionClass:
    """Classify every member of the unchanged sixteen-action vocabulary."""

    if action in PRODUCTION_NAVIGATION_ACTIONS:
        return ProductionActionClass.NAVIGATION
    if action in PRODUCTION_TRANSIENT_CONTROL_ACTIONS:
        return ProductionActionClass.TRANSIENT_CONTROL
    if action in PRODUCTION_PROHIBITED_ACTIONS:
        return ProductionActionClass.PROHIBITED
    raise AssertionError("unclassified BrowserRemoteAction")


@dataclass(frozen=True, slots=True)
class ProductionNavigationAuthority:
    native_identity_generation: int
    bridge_trust_revision: int
    target_policy_revision: int
    mutation_control_epoch: int
    inspect_scope_id: UUID
    navigate_scope_id: UUID | None
    client_preset_id: UUID
    target_preset_id: UUID
    generic_grant_created_at: str
    generic_grant_id: UUID = field(default_factory=lambda: UUID(int=0))
    native_http_acknowledgement_id: UUID | None = None


def navigation_binding_digest(session: Session, target_id: UUID) -> str:
    """Bind the opt-in, including native HTTP consent, to current verified peers."""
    native = evaluate_production_inspection_state(
        session, target_id, required_capabilities=frozenset({"target_inspect"})
    )
    bridge = evaluate_production_bridge_anchor_state(session, target_id)
    external = session.get(ExternalKodiTargetRow, str(target_id))
    policy = session.get(ExternalProductionPolicyRow, str(target_id))
    assert external is not None and policy is not None
    return hashlib.sha256(
        json.dumps(
            {
                "observation": policy.confirmed_observation_digest,
                "identity_generation": native.identity_generation,
                "bridge_id": str(bridge.bridge_id),
                "bridge_trust_revision": bridge.bridge_trust_revision,
                "reviewed_build": str(bridge.reviewed_build_id),
                "native_credential": external.secret_reference,
                "transport_policy_revision": (
                    PRODUCTION_NATIVE_CONTROL_TRANSPORT_POLICY_REVISION
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class NavigationPreferenceRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_id: UUID
    enabled: bool
    revision: int
    blocked_reason: str | None = None


class NavigationPreferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: StrictBool
    expected_revision: int = Field(ge=1, strict=True)


def require_production_remote_unblocked(session: Session, target_id: UUID) -> None:
    """Emergency and target vetoes apply to all remote dispatch and replay."""
    policy = session.get(ExternalProductionPolicyRow, str(target_id))
    mutation = session.get(ProductionMutationControlRow, 1)
    if policy is None or mutation is None:
        raise ProductionInspectionError("production_policy_not_confirmed")
    if mutation.production_mutations_blocked:
        raise ProductionInspectionError("production_mutations_blocked")
    if policy.mutations_suspended:
        raise ProductionInspectionError("production_target_mutations_suspended")


def require_navigation_preference(session: Session, target_id: UUID) -> None:
    require_production_remote_unblocked(session, target_id)
    policy = session.get(ExternalProductionPolicyRow, str(target_id))
    if policy is None or not policy.remote_navigation_enabled:
        raise ProductionInspectionError("remote_navigation_disabled")
    if policy.navigation_binding_digest != navigation_binding_digest(
        session, target_id
    ):
        raise ProductionInspectionError("navigation_connection_changed")


class ProductionNavigationPreferenceService:
    """One administrator opt-in on the existing production policy revision."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @staticmethod
    def _policy(session: Session, target_id: UUID) -> ExternalProductionPolicyRow:
        target = session.get(TargetRow, str(target_id))
        policy = session.get(ExternalProductionPolicyRow, str(target_id))
        if (
            target is None
            or target.target_type is not TargetType.EXTERNAL_PRODUCTION
            or policy is None
        ):
            raise ProductionInspectionError("target_not_found")
        return policy

    def read(self, target_id: UUID) -> NavigationPreferenceRead:
        with self._sessions() as session:
            policy = self._policy(session, target_id)
            blocked = None
            try:
                require_production_remote_unblocked(session, target_id)
                if policy.remote_navigation_enabled:
                    require_navigation_preference(session, target_id)
            except ProductionInspectionError as error:
                blocked = error.code
            return NavigationPreferenceRead(
                target_id=target_id,
                enabled=policy.remote_navigation_enabled,
                revision=policy.policy_revision,
                blocked_reason=blocked,
            )

    def update(
        self, target_id: UUID, *, enabled: bool, expected_revision: int
    ) -> NavigationPreferenceRead:
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            policy = self._policy(session, target_id)
            if policy.policy_revision != expected_revision:
                raise ProductionInspectionError("navigation_preference_conflict")
            if enabled:
                require_production_remote_unblocked(session, target_id)
            digest = navigation_binding_digest(session, target_id) if enabled else None
            if (
                policy.remote_navigation_enabled != enabled
                or policy.navigation_binding_digest != digest
            ):
                now = datetime.now(UTC)
                policy.remote_navigation_enabled = enabled
                policy.navigation_binding_digest = digest
                advance_production_policy(policy, now=now)
                session.add(
                    McpSecurityEventRow(
                        id=str(uuid4()),
                        target_id=str(target_id),
                        event_kind="remote_navigation_enabled"
                        if enabled
                        else "remote_navigation_disabled",
                        actor="web_admin",
                        identity_generation=policy.identity_generation,
                        occurred_at=_text(now),
                    )
                )
        return self.read(target_id)


class ProductionNativeControlAcknowledgementRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    acknowledgement_id: UUID
    target_id: UUID
    identity_generation: int = Field(ge=1)
    protocol: str = Field(pattern="^jsonrpc_http$")
    ip_literal: str = Field(min_length=2, max_length=64)
    native_port: int = Field(ge=1, le=65_535)
    endpoint_digest: str = Field(min_length=64, max_length=64)
    policy_revision: int = Field(ge=1)
    transport_policy_revision: int = Field(ge=1)
    acknowledged_by: str = Field(min_length=1, max_length=64)
    acknowledged_at: datetime


def native_control_endpoint_digest(
    external: ExternalKodiTargetRow, policy: ExternalProductionPolicyRow
) -> str:
    """Bind consent to the exact native endpoint and current production policy."""

    return hashlib.sha256(
        json.dumps(
            {
                "configured_capabilities": external.configured_capabilities_json,
                "host": external.host,
                "identity_generation": policy.identity_generation,
                "native_port": external.port,
                "policy_revision": policy.policy_revision,
                "protocol": external.protocol_kind.value,
                "target_id": external.target_id,
                "verified_addresses": external.verified_addresses_json,
                "verified_kodi_name": external.verified_kodi_name,
                "verified_kodi_version": external.verified_kodi_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def current_native_control_acknowledgement(
    session: Session,
    target_id: UUID,
) -> ProductionNativeControlHttpAcknowledgementRow | None:
    external = session.get(ExternalKodiTargetRow, str(target_id))
    policy = session.get(ExternalProductionPolicyRow, str(target_id))
    if external is None or policy is None:
        raise ProductionInspectionError("production_policy_not_confirmed")
    if external.protocol_kind is ExternalProtocolKind.JSONRPC_HTTPS:
        return None
    digest = native_control_endpoint_digest(external, policy)
    return session.scalar(
        select(ProductionNativeControlHttpAcknowledgementRow).where(
            ProductionNativeControlHttpAcknowledgementRow.target_id == str(target_id),
            ProductionNativeControlHttpAcknowledgementRow.identity_generation
            == policy.identity_generation,
            ProductionNativeControlHttpAcknowledgementRow.protocol
            == ExternalProtocolKind.JSONRPC_HTTP.value,
            ProductionNativeControlHttpAcknowledgementRow.ip_literal == external.host,
            ProductionNativeControlHttpAcknowledgementRow.native_port == external.port,
            ProductionNativeControlHttpAcknowledgementRow.endpoint_digest == digest,
            ProductionNativeControlHttpAcknowledgementRow.policy_revision
            == policy.policy_revision,
            ProductionNativeControlHttpAcknowledgementRow.transport_policy_revision
            == PRODUCTION_NATIVE_CONTROL_TRANSPORT_POLICY_REVISION,
        )
    )


class ProductionNativeControlAcknowledgementService:
    """Web-only creation and readback of exact native HTTP control consent."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = session_factory
        self._clock = clock

    def acknowledge(
        self,
        target_id: UUID,
        *,
        administrator_reference: str = "local_administrator",
    ) -> ProductionNativeControlAcknowledgementRead:
        administrator = administrator_reference.strip()
        if not administrator or len(administrator) > 64:
            raise ProductionInspectionError("permission_denied")
        now = self._clock()
        acknowledgement_id = uuid4()
        with self._sessions.begin() as session:
            evaluate_production_inspection_state(
                session,
                target_id,
                required_capabilities=frozenset({"target_inspect", "semantic_remote"}),
            )
            external = session.get(ExternalKodiTargetRow, str(target_id))
            policy = session.get(ExternalProductionPolicyRow, str(target_id))
            assert external is not None and policy is not None
            if external.protocol_kind is not ExternalProtocolKind.JSONRPC_HTTP:
                raise ProductionInspectionError(
                    "transport_acknowledgement_not_required"
                )
            digest = native_control_endpoint_digest(external, policy)
            existing = current_native_control_acknowledgement(session, target_id)
            if existing is None:
                row = ProductionNativeControlHttpAcknowledgementRow(
                    id=str(acknowledgement_id),
                    target_id=str(target_id),
                    identity_generation=policy.identity_generation,
                    protocol=ExternalProtocolKind.JSONRPC_HTTP.value,
                    ip_literal=external.host,
                    native_port=external.port,
                    endpoint_digest=digest,
                    policy_revision=policy.policy_revision,
                    transport_policy_revision=(
                        PRODUCTION_NATIVE_CONTROL_TRANSPORT_POLICY_REVISION
                    ),
                    acknowledged_by=administrator,
                    acknowledged_at=_text(now),
                )
                session.add(row)
                session.flush()
            else:
                row = existing
            return self._read(row)

    def current(
        self, target_id: UUID
    ) -> ProductionNativeControlAcknowledgementRead | None:
        with self._sessions() as session:
            row = current_native_control_acknowledgement(session, target_id)
            return None if row is None else self._read(row)

    @staticmethod
    def _read(
        row: ProductionNativeControlHttpAcknowledgementRow,
    ) -> ProductionNativeControlAcknowledgementRead:
        return ProductionNativeControlAcknowledgementRead(
            acknowledgement_id=UUID(row.id),
            target_id=UUID(row.target_id),
            identity_generation=row.identity_generation,
            protocol=row.protocol,
            ip_literal=row.ip_literal,
            native_port=row.native_port,
            endpoint_digest=row.endpoint_digest,
            policy_revision=row.policy_revision,
            transport_policy_revision=row.transport_policy_revision,
            acknowledged_by=row.acknowledged_by,
            acknowledged_at=datetime.fromisoformat(
                row.acknowledged_at.replace("Z", "+00:00")
            ),
        )


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float
    seen_at: float


class ProductionNavigationRateLimiter:
    """Bounded 2/s, burst-three production navigation admission."""

    def __init__(
        self,
        *,
        maximum_entries: int = 1024,
        idle_seconds: float = 15 * 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maximum_entries < 1 or idle_seconds <= 0:
            raise ValueError("invalid production navigation limiter bounds")
        self._maximum_entries = maximum_entries
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._guard = threading.Lock()
        self._buckets: dict[Hashable, _Bucket] = {}

    def require_allowed(self, client_id: UUID, target_id: UUID) -> None:
        now = self._clock()
        key = (client_id, target_id)
        with self._guard:
            expired = [
                item
                for item, bucket in self._buckets.items()
                if now - bucket.seen_at >= self._idle_seconds
            ]
            for item in expired:
                del self._buckets[item]
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._maximum_entries:
                    raise ProductionInspectionError("rate_limited")
                bucket = _Bucket(tokens=3.0, updated_at=now, seen_at=now)
                self._buckets[key] = bucket
            bucket.tokens = min(
                3.0, bucket.tokens + max(0.0, now - bucket.updated_at) * 2.0
            )
            bucket.updated_at = max(bucket.updated_at, now)
            bucket.seen_at = now
            if bucket.tokens < 1.0:
                raise ProductionInspectionError("rate_limited")
            bucket.tokens -= 1.0


def _text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
