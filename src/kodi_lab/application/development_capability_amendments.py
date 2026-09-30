"""Web-only, paired-identity-bound development capability administration."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_bridges import (
    ExternalBridgeService,
    ExternalBridgeServiceError,
)
from kodi_lab.domain import (
    EXTERNAL_CAPABILITY_VALUES,
    ExternalAdministrativeState,
    ExternalAuthenticationMode,
    ExternalBridgePairingState,
    ExternalBridgeReachabilityState,
    ExternalProtocolKind,
    ExternalVerificationState,
    TargetType,
)
from kodi_lab.kodi.bridge import (
    BRIDGE_ADDON_ID,
    BRIDGE_PROTOCOL_VERSION,
    SUPPORTED_V1_BRIDGE_ADDON_VERSION,
)
from kodi_lab.kodi.external import (
    ExternalEndpointError,
    ExternalEndpointPolicy,
    ExternalProbeError,
    NativeExternalKodiVerifier,
)
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalDevelopmentCapabilityAmendmentRow,
    ExternalKodiTargetRow,
    TargetRow,
)
from kodi_lab.storage.secrets import (
    ExternalCredentialStore,
    SecretStoreError,
)


class DevelopmentCapabilityAmendmentError(RuntimeError):
    """Bounded, credential-free Web error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DevelopmentCapabilityAmendmentRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    amendment_id: UUID
    target_id: UUID
    target_classification: str
    bridge_id: UUID
    bridge_version: str
    bridge_protocol: str
    bridge_fingerprint: str
    before_capabilities: tuple[str, ...]
    requested_capabilities: tuple[str, ...]
    resulting_capabilities: tuple[str, ...]
    native_verified_capabilities: tuple[str, ...]
    bridge_verified_capabilities: tuple[str, ...]
    actor: str = Field(min_length=1, max_length=64)
    requested_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class _Snapshot:
    before_json: str
    native_verified_json: str
    verified_addresses_json: str
    target_name: str
    host: str
    port: int
    protocol_kind: ExternalProtocolKind
    authentication_mode: ExternalAuthenticationMode
    secret_reference: str | None
    kodi_name: str
    kodi_version: str
    bridge_id: str
    bridge_secret_reference: str
    bridge_verified_json: str
    endpoint_revision_digest: str
    bridge_version: str
    bridge_protocol: str
    bridge_fingerprint: str


def _json(values: frozenset[str] | set[str] | tuple[str, ...]) -> str:
    return json.dumps(sorted(values), separators=(",", ":"))


def _set(value: str) -> frozenset[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise DevelopmentCapabilityAmendmentError("capabilities_invalid") from error
    if (
        not isinstance(parsed, list)
        or any(not isinstance(item, str) for item in parsed)
        or len(parsed) != len(set(parsed))
        or not set(parsed) <= EXTERNAL_CAPABILITY_VALUES
        or _json(frozenset(parsed)) != value
    ):
        raise DevelopmentCapabilityAmendmentError("capabilities_invalid")
    return frozenset(parsed)


def _time(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


class DevelopmentCapabilityAmendmentService:
    """Verify live native/bridge evidence, then atomically amend and receipt."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        endpoint_policy: ExternalEndpointPolicy,
        credential_store: ExternalCredentialStore,
        bridges: ExternalBridgeService,
        verifier: NativeExternalKodiVerifier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._endpoint_policy = endpoint_policy
        self._credentials = credential_store
        self._bridges = bridges
        self._verifier = verifier or NativeExternalKodiVerifier()
        self._clock = clock

    def list_receipts(
        self, target_id: UUID
    ) -> tuple[DevelopmentCapabilityAmendmentRead, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(ExternalDevelopmentCapabilityAmendmentRow)
                .where(
                    ExternalDevelopmentCapabilityAmendmentRow.target_id
                    == str(target_id)
                )
                .order_by(
                    ExternalDevelopmentCapabilityAmendmentRow.completed_at.desc(),
                    ExternalDevelopmentCapabilityAmendmentRow.id.desc(),
                )
                .limit(100)
            ).all()
            return tuple(self._read(row) for row in reversed(rows))

    def amend(
        self,
        target_id: UUID,
        *,
        expected_capabilities: frozenset[str],
        requested_capabilities: frozenset[str],
        expected_bridge_id: UUID,
        expected_bridge_fingerprint: str,
        actor: str = "local_administrator",
    ) -> DevelopmentCapabilityAmendmentRead:
        if (
            not expected_capabilities
            or not requested_capabilities
            or "target_inspect" not in requested_capabilities
            or not expected_capabilities <= EXTERNAL_CAPABILITY_VALUES
            or not requested_capabilities <= EXTERNAL_CAPABILITY_VALUES
        ):
            raise DevelopmentCapabilityAmendmentError("capabilities_invalid")
        if not actor or len(actor) > 64 or actor != actor.strip():
            raise DevelopmentCapabilityAmendmentError("administrator_invalid")
        before_json = _json(expected_capabilities)
        requested_json = _json(requested_capabilities)
        with self._sessions() as session:
            snapshot = self._snapshot(session, target_id)
            if (
                snapshot.bridge_id != str(expected_bridge_id)
                or snapshot.bridge_fingerprint != expected_bridge_fingerprint
            ):
                raise DevelopmentCapabilityAmendmentError("bridge_identity_stale")
            if snapshot.before_json == requested_json:
                previous = session.scalar(
                    select(ExternalDevelopmentCapabilityAmendmentRow)
                    .where(
                        ExternalDevelopmentCapabilityAmendmentRow.target_id
                        == str(target_id),
                        ExternalDevelopmentCapabilityAmendmentRow.before_json
                        == before_json,
                        ExternalDevelopmentCapabilityAmendmentRow.requested_json
                        == requested_json,
                        ExternalDevelopmentCapabilityAmendmentRow.bridge_id
                        == str(expected_bridge_id),
                        ExternalDevelopmentCapabilityAmendmentRow.bridge_fingerprint
                        == expected_bridge_fingerprint,
                    )
                    .order_by(
                        ExternalDevelopmentCapabilityAmendmentRow.completed_at.desc()
                    )
                    .limit(1)
                )
                if previous is not None:
                    return self._read(previous)
                raise DevelopmentCapabilityAmendmentError("capabilities_unchanged")
            if snapshot.before_json != before_json:
                raise DevelopmentCapabilityAmendmentError("capabilities_stale")

        try:
            endpoint = self._endpoint_policy.validate(
                snapshot.protocol_kind, snapshot.host, snapshot.port
            )
            credential = None
            if (
                snapshot.authentication_mode
                is ExternalAuthenticationMode.USERNAME_PASSWORD
            ):
                if snapshot.secret_reference is None:
                    raise DevelopmentCapabilityAmendmentError(
                        "native_secret_unavailable"
                    )
                credential = self._credentials.read(
                    target_id, UUID(snapshot.secret_reference)
                )
            native = self._verifier.probe(endpoint, credential, requested_capabilities)
        except (ExternalEndpointError, ExternalProbeError, SecretStoreError) as error:
            raise DevelopmentCapabilityAmendmentError(
                getattr(error, "code", "native_verification_failed")
            ) from error
        native_verified = frozenset(native.verified_capabilities)
        if (
            native.addresses != (snapshot.host,)
            or native.kodi_name != snapshot.kodi_name
            or native.kodi_version != snapshot.kodi_version
            or not requested_capabilities.difference(
                {"screenshot", "workspace_deployment"}
            )
            <= native_verified
        ):
            raise DevelopmentCapabilityAmendmentError("native_verification_failed")
        try:
            bridge, bridge_capabilities = (
                self._bridges.observe_development_amendment_identity(target_id)
            )
        except ExternalBridgeServiceError as error:
            raise DevelopmentCapabilityAmendmentError(error.code) from error
        bridge_verified = frozenset(bridge_capabilities)
        if (
            str(bridge.bridge_id) != snapshot.bridge_id
            or bridge.build_fingerprint != snapshot.bridge_fingerprint
            or bridge.addon_id != BRIDGE_ADDON_ID
            or bridge.addon_version != SUPPORTED_V1_BRIDGE_ADDON_VERSION
            or bridge.protocol_version != BRIDGE_PROTOCOL_VERSION
            or not requested_capabilities.intersection(
                {"screenshot", "workspace_deployment"}
            )
            <= bridge_verified
        ):
            raise DevelopmentCapabilityAmendmentError("bridge_identity_stale")

        now = self._clock()
        with self._sessions.begin() as session:
            current = self._snapshot(session, target_id)
            if current != snapshot:
                raise DevelopmentCapabilityAmendmentError("amendment_stale")
            row = session.get(ExternalKodiTargetRow, str(target_id))
            assert row is not None
            receipt = ExternalDevelopmentCapabilityAmendmentRow(
                id=str(uuid4()),
                target_id=str(target_id),
                target_classification=TargetType.EXTERNAL_DEVELOPMENT.value,
                bridge_id=snapshot.bridge_id,
                bridge_addon_id=bridge.addon_id,
                bridge_version=bridge.addon_version,
                bridge_protocol=bridge.protocol_version,
                bridge_fingerprint=bridge.build_fingerprint,
                before_json=before_json,
                requested_json=requested_json,
                resulting_json=requested_json,
                native_verified_json=_json(native_verified),
                bridge_verified_json=_json(bridge_verified),
                actor=actor,
                requested_at=_time(now),
                completed_at=_time(now),
            )
            session.add(receipt)
            session.flush()
            row.configured_capabilities_json = requested_json
            row.verified_capabilities_json = _json(native_verified)
            row.last_observed_at = _time(now)
            session.flush()
            return self._read(receipt)

    @staticmethod
    def _snapshot(session: Session, target_id: UUID) -> _Snapshot:
        target = session.get(TargetRow, str(target_id))
        external = session.get(ExternalKodiTargetRow, str(target_id))
        pairing = session.get(ExternalBridgePairingRow, str(target_id))
        if (
            target is None
            or target.target_type is not TargetType.EXTERNAL_DEVELOPMENT
            or external is None
        ):
            raise DevelopmentCapabilityAmendmentError("target_not_found")
        if (
            external.administrative_state is not ExternalAdministrativeState.ENABLED
            or external.verification_state is not ExternalVerificationState.VERIFIED
            or external.verified_kodi_name is None
            or external.verified_kodi_version is None
            or pairing is None
            or pairing.pairing_state is not ExternalBridgePairingState.PAIRED
            or pairing.reachability_state
            is not ExternalBridgeReachabilityState.REACHABLE
            or pairing.bridge_secret_reference is None
            or pairing.pinned_bridge_id is None
            or pairing.pinned_addon_id != BRIDGE_ADDON_ID
            or pairing.pinned_addon_version != SUPPORTED_V1_BRIDGE_ADDON_VERSION
            or pairing.pinned_protocol_version != BRIDGE_PROTOCOL_VERSION
            or pairing.pinned_build_fingerprint is None
            or pairing.pinned_kodi_version != external.verified_kodi_version
        ):
            raise DevelopmentCapabilityAmendmentError("pairing_unverified")
        _set(external.configured_capabilities_json)
        return _Snapshot(
            before_json=external.configured_capabilities_json,
            native_verified_json=external.verified_capabilities_json,
            verified_addresses_json=external.verified_addresses_json,
            target_name=target.name,
            host=external.host,
            port=external.port,
            protocol_kind=external.protocol_kind,
            authentication_mode=external.authentication_mode,
            secret_reference=external.secret_reference,
            kodi_name=external.verified_kodi_name,
            kodi_version=external.verified_kodi_version,
            bridge_id=pairing.pinned_bridge_id,
            bridge_secret_reference=pairing.bridge_secret_reference,
            bridge_verified_json=pairing.verified_capabilities_json,
            endpoint_revision_digest=pairing.endpoint_revision_digest or "",
            bridge_version=pairing.pinned_addon_version,
            bridge_protocol=pairing.pinned_protocol_version,
            bridge_fingerprint=pairing.pinned_build_fingerprint,
        )

    @staticmethod
    def _read(
        row: ExternalDevelopmentCapabilityAmendmentRow,
    ) -> DevelopmentCapabilityAmendmentRead:
        return DevelopmentCapabilityAmendmentRead(
            amendment_id=UUID(row.id),
            target_id=UUID(row.target_id),
            target_classification=row.target_classification,
            bridge_id=UUID(row.bridge_id),
            bridge_version=row.bridge_version,
            bridge_protocol=row.bridge_protocol,
            bridge_fingerprint=row.bridge_fingerprint,
            before_capabilities=tuple(sorted(_set(row.before_json))),
            requested_capabilities=tuple(sorted(_set(row.requested_json))),
            resulting_capabilities=tuple(sorted(_set(row.resulting_json))),
            native_verified_capabilities=tuple(sorted(_set(row.native_verified_json))),
            bridge_verified_capabilities=tuple(sorted(_set(row.bridge_verified_json))),
            actor=row.actor,
            requested_at=datetime.fromisoformat(
                row.requested_at.replace("Z", "+00:00")
            ),
            completed_at=datetime.fromisoformat(
                row.completed_at.replace("Z", "+00:00")
            ),
        )
