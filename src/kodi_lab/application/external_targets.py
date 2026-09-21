"""Web-only external-development target registration and trust workflow."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import (
    EXTERNAL_CAPABILITY_VALUES,
    ConnectionState,
    ExternalAdministrativeState,
    ExternalAuthenticationMode,
    ExternalObservationState,
    ExternalProtocolKind,
    ExternalVerificationState,
    HealthStatus,
    TargetAvailability,
    TargetType,
)
from kodi_lab.kodi.external import (
    ExternalEndpointError,
    ExternalEndpointPolicy,
    ExternalKodiProbeResult,
    ExternalProbeError,
    NativeExternalKodiVerifier,
    ValidatedExternalEndpoint,
)
from kodi_lab.storage.models import (
    ExternalKodiTargetEventRow,
    ExternalKodiTargetObservationRow,
    ExternalKodiTargetRow,
    McpClientTargetGrantRow,
    McpTargetPresetAssignmentRow,
    TargetRow,
)
from kodi_lab.storage.secrets import (
    ExternalCredentialStore,
    ExternalKodiCredential,
    SecretStoreError,
)

type ExternalProbeConfiguration = tuple[
    ExternalProtocolKind,
    str,
    int,
    ExternalAuthenticationMode,
    str | None,
    str,
]


class ExternalTargetError(ValueError):
    """Bounded semantic error safe for Web translation."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExternalTargetRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: UUID
    logical_name: str = Field(max_length=120)
    target_class: str = Field(pattern="^external_development$")
    protocol_kind: ExternalProtocolKind
    host: str = Field(max_length=64)
    port: int = Field(ge=1, le=65_535)
    authentication_mode: ExternalAuthenticationMode
    http_password_warning: bool
    administrative_state: ExternalAdministrativeState
    verification_state: ExternalVerificationState
    observation_state: ExternalObservationState | None
    kodi_name: str | None = Field(default=None, max_length=64)
    kodi_version: str | None = Field(default=None, max_length=64)
    protocol_version: str | None = Field(default=None, max_length=32)
    configured_capabilities: tuple[str, ...]
    verified_capabilities: tuple[str, ...]
    verified_addresses: tuple[str, ...]
    pending_observation_id: UUID | None
    created_at: datetime
    verified_at: datetime | None
    last_observed_at: datetime | None
    deactivated_at: datetime | None


class ExternalTargetEventRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    target_id: UUID
    event_kind: str = Field(max_length=64)
    classification: str | None = Field(default=None, max_length=64)
    observation_id: UUID | None
    occurred_at: datetime


def _to_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _from_text(value: str | None) -> datetime | None:
    return (
        None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _closed_json(values: set[str] | frozenset[str] | tuple[str, ...]) -> str:
    return json.dumps(sorted(values), separators=(",", ":"))


def _parse_tuple(value: str) -> tuple[str, ...]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        raise RuntimeError("stored external target metadata is invalid")
    return tuple(parsed)


class ExternalTargetAdministrationService:
    """Authenticated Web administrator boundary; never an MCP adapter."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        endpoint_policy: ExternalEndpointPolicy,
        credential_store: ExternalCredentialStore,
        verifier: NativeExternalKodiVerifier | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._endpoint_policy = endpoint_policy
        self._credential_store = credential_store
        self._verifier = verifier or NativeExternalKodiVerifier()
        self._clock = clock
        with self._session_factory() as session:
            encrypted = session.scalar(
                select(ExternalKodiTargetRow.target_id)
                .where(ExternalKodiTargetRow.secret_reference.is_not(None))
                .limit(1)
            )
        if encrypted is not None and not credential_store.available:
            raise RuntimeError(
                "encrypted external Kodi credentials exist but the master key "
                "is unavailable"
            )

    def register(
        self,
        *,
        logical_name: str,
        protocol_kind: ExternalProtocolKind,
        host: str,
        port: int,
        authentication_mode: ExternalAuthenticationMode,
        configured_capabilities: set[str],
        username: str | None = None,
        password: str | None = None,
    ) -> ExternalTargetRead:
        name = self._name(logical_name)
        endpoint = self._validate_endpoint(protocol_kind, host, port)
        capabilities = self._capabilities(configured_capabilities)
        target_id = uuid4()
        now = self._clock()
        secret_reference: UUID | None = None
        if authentication_mode is ExternalAuthenticationMode.USERNAME_PASSWORD:
            secret_reference = self._write_credential(target_id, username, password)
        elif username is not None or password is not None:
            raise ExternalTargetError("secret_unavailable")
        try:
            with self._session_factory.begin() as session:
                session.add(
                    TargetRow(
                        id=str(target_id),
                        name=name,
                        target_type=TargetType.EXTERNAL_DEVELOPMENT,
                        kodi_version=None,
                        availability=TargetAvailability.UNAVAILABLE,
                        health=HealthStatus.WARNING,
                        connection_state=ConnectionState.DISCONNECTED,
                    )
                )
                session.add(
                    ExternalKodiTargetRow(
                        target_id=str(target_id),
                        protocol_kind=protocol_kind,
                        host=endpoint.address,
                        port=endpoint.port,
                        authentication_mode=authentication_mode,
                        secret_reference=None
                        if secret_reference is None
                        else str(secret_reference),
                        configured_capabilities_json=_closed_json(capabilities),
                        verified_capabilities_json="[]",
                        administrative_state=ExternalAdministrativeState.DISABLED,
                        verification_state=ExternalVerificationState.UNVERIFIED,
                        observation_state=None,
                        verified_addresses_json="[]",
                        verified_kodi_name=None,
                        verified_kodi_version=None,
                        observed_kodi_name=None,
                        observed_kodi_version=None,
                        observed_protocol_version=None,
                        pending_observation_id=None,
                        created_at=_to_text(now),
                        verified_at=None,
                        deactivated_at=None,
                        last_observed_at=None,
                    )
                )
                self._event(session, target_id, "registered", now)
        except Exception:
            if secret_reference is not None:
                self._credential_store.retire(secret_reference)
            raise
        return self.get(target_id)

    def list_targets(self) -> list[ExternalTargetRead]:
        with self._session_factory() as session:
            rows = session.execute(
                select(ExternalKodiTargetRow, TargetRow)
                .join(TargetRow, TargetRow.id == ExternalKodiTargetRow.target_id)
                .order_by(TargetRow.name, TargetRow.id)
            ).all()
            return [self._read(target, external) for external, target in rows]

    def get(self, target_id: UUID) -> ExternalTargetRead:
        with self._session_factory() as session:
            external = session.get(ExternalKodiTargetRow, str(target_id))
            target = session.get(TargetRow, str(target_id))
            if external is None or target is None:
                raise ExternalTargetError("target_not_found")
            return self._read(target, external)

    def events(self, target_id: UUID) -> list[ExternalTargetEventRead]:
        self.get(target_id)
        with self._session_factory() as session:
            rows = session.scalars(
                select(ExternalKodiTargetEventRow)
                .where(ExternalKodiTargetEventRow.target_id == str(target_id))
                .order_by(
                    ExternalKodiTargetEventRow.occurred_at,
                    ExternalKodiTargetEventRow.id,
                )
                .limit(1001)
            ).all()
        if len(rows) > 1000:
            raise ExternalTargetError("invalid_kodi_response")
        return [
            ExternalTargetEventRead(
                event_id=UUID(row.id),
                target_id=UUID(row.target_id),
                event_kind=row.event_kind,
                classification=row.classification,
                observation_id=None
                if row.observation_id is None
                else UUID(row.observation_id),
                occurred_at=_from_text(row.occurred_at),  # type: ignore[arg-type]
            )
            for row in rows
        ]

    def edit_endpoint(
        self,
        target_id: UUID,
        *,
        protocol_kind: ExternalProtocolKind,
        host: str,
        port: int,
    ) -> ExternalTargetRead:
        endpoint = self._validate_endpoint(protocol_kind, host, port)
        now = self._clock()
        with self._session_factory.begin() as session:
            target, row = self._require_mutable(session, target_id)
            if row.administrative_state is not ExternalAdministrativeState.DISABLED:
                raise ExternalTargetError("verification_required")
            changed = (row.protocol_kind, row.host, row.port) != (
                protocol_kind,
                endpoint.address,
                endpoint.port,
            )
            if changed:
                row.protocol_kind = protocol_kind
                row.host = endpoint.address
                row.port = endpoint.port
                self._require_reverification(row)
                target.availability = TargetAvailability.UNAVAILABLE
                target.connection_state = ConnectionState.DISCONNECTED
                target.health = HealthStatus.WARNING
                self._event(session, target_id, "endpoint_changed", now)
                self._event(session, target_id, "reverification_required", now)
        return self.get(target_id)

    def rotate_credentials(
        self,
        target_id: UUID,
        *,
        authentication_mode: ExternalAuthenticationMode,
        username: str | None = None,
        password: str | None = None,
    ) -> ExternalTargetRead:
        current = self.get(target_id)
        if current.administrative_state is not ExternalAdministrativeState.DISABLED:
            raise ExternalTargetError("verification_required")
        new_reference: UUID | None = None
        if authentication_mode is ExternalAuthenticationMode.USERNAME_PASSWORD:
            new_reference = self._write_credential(target_id, username, password)
        elif username is not None or password is not None:
            raise ExternalTargetError("secret_unavailable")
        old_reference: UUID | None = None
        now = self._clock()
        try:
            with self._session_factory.begin() as session:
                _target, row = self._require_mutable(session, target_id)
                old_reference = (
                    None if row.secret_reference is None else UUID(row.secret_reference)
                )
                row.authentication_mode = authentication_mode
                row.secret_reference = (
                    None if new_reference is None else str(new_reference)
                )
                self._require_reverification(row)
                self._event(session, target_id, "credential_rotated", now)
                self._event(session, target_id, "reverification_required", now)
        except Exception:
            if new_reference is not None:
                self._credential_store.retire(new_reference)
            raise
        if old_reference is not None:
            self._credential_store.retire(old_reference)
        return self.get(target_id)

    def probe(self, target_id: UUID) -> ExternalTargetRead:
        attempted_at = self._clock()
        with self._session_factory.begin() as session:
            _target, row = self._require_mutable(session, target_id)
            if row.administrative_state is not ExternalAdministrativeState.DISABLED:
                raise ExternalTargetError("verification_required")
            self._event(session, target_id, "probe_attempted", attempted_at)
            protocol = row.protocol_kind
            host = row.host
            port = row.port
            configured = frozenset(_parse_tuple(row.configured_capabilities_json))
            auth_mode = row.authentication_mode
            reference = row.secret_reference
            configuration: ExternalProbeConfiguration = (
                protocol,
                host,
                port,
                auth_mode,
                reference,
                row.configured_capabilities_json,
            )
        try:
            endpoint = self._endpoint_policy.validate(protocol, host, port)
            credential = None
            if auth_mode is ExternalAuthenticationMode.USERNAME_PASSWORD:
                if reference is None:
                    raise ExternalProbeError("secret_unavailable")
                try:
                    credential = self._credential_store.read(target_id, UUID(reference))
                except (SecretStoreError, ValueError) as error:
                    raise ExternalProbeError("secret_unavailable") from error
            result = self._verifier.probe(endpoint, credential, configured)
        except ExternalEndpointError as error:
            self._publish_failure(
                target_id,
                attempted_at,
                "endpoint_not_allowed",
                configuration,
            )
            raise ExternalTargetError("endpoint_not_allowed") from error
        except ExternalProbeError as error:
            self._publish_failure(target_id, attempted_at, error.code, configuration)
            raise ExternalTargetError(error.code) from None
        if not self._publish_success(target_id, attempted_at, result, configuration):
            raise ExternalTargetError("needs_reverification")
        return self.get(target_id)

    def confirm(self, target_id: UUID, observation_id: UUID) -> ExternalTargetRead:
        now = self._clock()
        with self._session_factory.begin() as session:
            target, row = self._require_mutable(session, target_id)
            observation = session.get(
                ExternalKodiTargetObservationRow, str(observation_id)
            )
            if (
                observation is None
                or observation.target_id != str(target_id)
                or row.pending_observation_id != str(observation_id)
                or observation.outcome != ExternalObservationState.REACHABLE.value
                or observation.error_code not in {None, "identity_mismatch"}
                or observation.kodi_name is None
                or observation.kodi_version is None
            ):
                raise ExternalTargetError("verification_required")
            row.verification_state = ExternalVerificationState.VERIFIED
            row.verified_addresses_json = observation.addresses_json
            row.verified_kodi_name = observation.kodi_name
            row.verified_kodi_version = observation.kodi_version
            row.verified_capabilities_json = observation.verified_capabilities_json
            row.verified_at = _to_text(now)
            target.kodi_version = observation.kodi_version
            target.health = HealthStatus.HEALTHY
            target.connection_state = ConnectionState.CONNECTED
            self._event(
                session,
                target_id,
                "verification_confirmed",
                now,
                observation_id=observation_id,
            )
        return self.get(target_id)

    def set_enabled(self, target_id: UUID, *, enabled: bool) -> ExternalTargetRead:
        now = self._clock()
        with self._session_factory.begin() as session:
            target, row = self._require_mutable(session, target_id)
            if enabled:
                if (
                    row.verification_state
                    is ExternalVerificationState.IDENTITY_MISMATCH
                ):
                    raise ExternalTargetError("identity_mismatch")
                if row.verification_state is not ExternalVerificationState.VERIFIED:
                    raise ExternalTargetError("verification_required")
                row.administrative_state = ExternalAdministrativeState.ENABLED
                target.availability = TargetAvailability.AVAILABLE
                self._event(session, target_id, "enabled", now)
            else:
                row.administrative_state = ExternalAdministrativeState.DISABLED
                target.availability = TargetAvailability.UNAVAILABLE
                self._event(session, target_id, "disabled", now)
        return self.get(target_id)

    def deactivate(self, target_id: UUID) -> ExternalTargetRead:
        now = self._clock()
        old_reference: UUID | None = None
        with self._session_factory.begin() as session:
            target, row = self._require_mutable(session, target_id)
            old_reference = (
                None if row.secret_reference is None else UUID(row.secret_reference)
            )
            row.secret_reference = None
            row.administrative_state = ExternalAdministrativeState.DEACTIVATED
            row.deactivated_at = _to_text(now)
            target.availability = TargetAvailability.UNAVAILABLE
            target.connection_state = ConnectionState.DISCONNECTED
            target.health = HealthStatus.WARNING
            session.execute(
                delete(McpClientTargetGrantRow).where(
                    McpClientTargetGrantRow.target_id == str(target_id)
                )
            )
            session.execute(
                delete(McpTargetPresetAssignmentRow).where(
                    McpTargetPresetAssignmentRow.target_id == str(target_id)
                )
            )
            self._event(session, target_id, "deactivated", now)
        if old_reference is not None:
            self._credential_store.retire(old_reference)
        return self.get(target_id)

    def _publish_failure(
        self,
        target_id: UUID,
        attempted_at: datetime,
        code: str,
        configuration: ExternalProbeConfiguration,
    ) -> None:
        completed = self._clock()
        outcome = (
            ExternalObservationState.AUTHENTICATION_FAILED
            if code == "authentication_failed"
            else ExternalObservationState.UNREACHABLE
            if code
            in {"target_unreachable", "endpoint_not_allowed", "secret_unavailable"}
            else ExternalObservationState.CAPABILITY_MISSING
        )
        observation_id = uuid4()
        with self._session_factory.begin() as session:
            target, row = self._require_mutable(session, target_id)
            session.add(
                ExternalKodiTargetObservationRow(
                    id=str(observation_id),
                    target_id=str(target_id),
                    outcome=outcome.value,
                    addresses_json="[]",
                    kodi_name=None,
                    kodi_version=None,
                    protocol_version=None,
                    verified_capabilities_json="[]",
                    error_code=code,
                    attempted_at=_to_text(attempted_at),
                    completed_at=_to_text(completed),
                )
            )
            if not self._configuration_matches(row, configuration):
                self._event(
                    session,
                    target_id,
                    "probe_failed",
                    completed,
                    classification=code,
                    observation_id=observation_id,
                )
                return
            row.observation_state = outcome
            row.last_observed_at = _to_text(completed)
            row.pending_observation_id = None
            if row.verification_state is ExternalVerificationState.VERIFIED:
                row.verification_state = ExternalVerificationState.NEEDS_REVERIFICATION
                self._event(session, target_id, "reverification_required", completed)
            target.connection_state = ConnectionState.DISCONNECTED
            target.health = HealthStatus.ERROR
            self._event(
                session,
                target_id,
                "probe_failed",
                completed,
                classification=code,
                observation_id=observation_id,
            )

    def _publish_success(
        self,
        target_id: UUID,
        attempted_at: datetime,
        result: ExternalKodiProbeResult,
        configuration: ExternalProbeConfiguration,
    ) -> bool:
        completed = self._clock()
        observation_id = uuid4()
        addresses_json = _closed_json(result.addresses)
        capabilities_json = _closed_json(result.verified_capabilities)
        with self._session_factory.begin() as session:
            target, row = self._require_mutable(session, target_id)
            mismatch = row.verified_kodi_name is not None and (
                row.verified_kodi_name != result.kodi_name
                or row.verified_kodi_version != result.kodi_version
            )
            session.add(
                ExternalKodiTargetObservationRow(
                    id=str(observation_id),
                    target_id=str(target_id),
                    outcome=ExternalObservationState.REACHABLE.value,
                    addresses_json=addresses_json,
                    kodi_name=result.kodi_name,
                    kodi_version=result.kodi_version,
                    protocol_version=result.protocol_version,
                    verified_capabilities_json=capabilities_json,
                    error_code="identity_mismatch" if mismatch else None,
                    attempted_at=_to_text(attempted_at),
                    completed_at=_to_text(completed),
                )
            )
            if not self._configuration_matches(row, configuration):
                self._event(
                    session,
                    target_id,
                    "probe_succeeded",
                    completed,
                    classification="needs_reverification",
                    observation_id=observation_id,
                )
                return False
            row.observation_state = ExternalObservationState.REACHABLE
            row.observed_kodi_name = result.kodi_name
            row.observed_kodi_version = result.kodi_version
            row.observed_protocol_version = result.protocol_version
            row.verified_capabilities_json = capabilities_json
            row.pending_observation_id = str(observation_id)
            row.last_observed_at = _to_text(completed)
            if mismatch:
                row.verification_state = ExternalVerificationState.IDENTITY_MISMATCH
            target.connection_state = ConnectionState.CONNECTED
            target.health = HealthStatus.WARNING
            self._event(
                session,
                target_id,
                "probe_succeeded",
                completed,
                classification="identity_mismatch" if mismatch else "reachable",
                observation_id=observation_id,
            )
            return True

    @staticmethod
    def _configuration_matches(
        row: ExternalKodiTargetRow, expected: ExternalProbeConfiguration
    ) -> bool:
        return (
            row.protocol_kind,
            row.host,
            row.port,
            row.authentication_mode,
            row.secret_reference,
            row.configured_capabilities_json,
        ) == expected

    @staticmethod
    def _require_reverification(row: ExternalKodiTargetRow) -> None:
        row.verification_state = ExternalVerificationState.NEEDS_REVERIFICATION
        row.pending_observation_id = None
        row.verified_capabilities_json = "[]"

    @staticmethod
    def _require_mutable(
        session: Session, target_id: UUID
    ) -> tuple[TargetRow, ExternalKodiTargetRow]:
        target = session.get(TargetRow, str(target_id))
        row = session.get(ExternalKodiTargetRow, str(target_id))
        if target is None or row is None:
            raise ExternalTargetError("target_not_found")
        if row.administrative_state is ExternalAdministrativeState.DEACTIVATED:
            raise ExternalTargetError("verification_required")
        return target, row

    def _validate_endpoint(
        self, protocol: ExternalProtocolKind, host: str, port: int
    ) -> ValidatedExternalEndpoint:
        try:
            return self._endpoint_policy.validate(protocol, host, port)
        except ExternalEndpointError as error:
            raise ExternalTargetError("endpoint_not_allowed") from error

    def _write_credential(
        self, target_id: UUID, username: str | None, password: str | None
    ) -> UUID:
        if username is None or password is None:
            raise ExternalTargetError("secret_unavailable")
        try:
            return self._credential_store.write(
                target_id, ExternalKodiCredential(username=username, password=password)
            )
        except (SecretStoreError, ValueError) as error:
            raise ExternalTargetError("secret_unavailable") from error

    @staticmethod
    def _name(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 120:
            raise ExternalTargetError("endpoint_not_allowed")
        return normalized

    @staticmethod
    def _capabilities(values: set[str]) -> frozenset[str]:
        normalized = frozenset(values)
        if (
            not normalized
            or len(normalized) > 6
            or not normalized <= EXTERNAL_CAPABILITY_VALUES
        ):
            raise ExternalTargetError("endpoint_not_allowed")
        return normalized

    @staticmethod
    def _event(
        session: Session,
        target_id: UUID,
        kind: str,
        occurred_at: datetime,
        *,
        classification: str | None = None,
        observation_id: UUID | None = None,
    ) -> None:
        session.add(
            ExternalKodiTargetEventRow(
                id=str(uuid4()),
                target_id=str(target_id),
                event_kind=kind,
                classification=classification,
                observation_id=None if observation_id is None else str(observation_id),
                occurred_at=_to_text(occurred_at),
            )
        )

    @staticmethod
    def _read(target: TargetRow, row: ExternalKodiTargetRow) -> ExternalTargetRead:
        created = _from_text(row.created_at)
        assert created is not None
        return ExternalTargetRead(
            target_id=UUID(row.target_id),
            logical_name=target.name,
            target_class="external_development",
            protocol_kind=row.protocol_kind,
            host=row.host,
            port=row.port,
            authentication_mode=row.authentication_mode,
            http_password_warning=(
                row.protocol_kind is ExternalProtocolKind.JSONRPC_HTTP
                and row.authentication_mode
                is ExternalAuthenticationMode.USERNAME_PASSWORD
            ),
            administrative_state=row.administrative_state,
            verification_state=row.verification_state,
            observation_state=row.observation_state,
            kodi_name=row.observed_kodi_name,
            kodi_version=row.observed_kodi_version,
            protocol_version=row.observed_protocol_version,
            configured_capabilities=_parse_tuple(row.configured_capabilities_json),
            verified_capabilities=_parse_tuple(row.verified_capabilities_json),
            verified_addresses=_parse_tuple(row.verified_addresses_json),
            pending_observation_id=None
            if row.pending_observation_id is None
            else UUID(row.pending_observation_id),
            created_at=created,
            verified_at=_from_text(row.verified_at),
            last_observed_at=_from_text(row.last_observed_at),
            deactivated_at=_from_text(row.deactivated_at),
        )
