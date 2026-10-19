"""Web-only external-development target registration and trust workflow."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.production_inspection import production_observation_digest
from kodi_lab.application.production_policy import (
    advance_production_policy,
    bind_external_production_transition,
)
from kodi_lab.domain import (
    EXTERNAL_CAPABILITY_VALUES,
    ConnectionState,
    ExternalAdministrativeState,
    ExternalAuthenticationMode,
    ExternalObservationState,
    ExternalProtocolKind,
    ExternalVerificationState,
    HealthStatus,
    ProductionDesignationState,
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
    ExternalProductionPolicyRow,
    HouseholdOnboardingRow,
    McpClientTargetGrantRow,
    McpSecurityEventRow,
    McpTargetPresetAssignmentRow,
    ProductionApprovalTransitionEventRow,
    ProductionMutationControlRow,
    ProductionOperationApprovalRow,
    TargetRow,
)
from kodi_lab.storage.secrets import (
    ExternalCredentialStore,
    ExternalKodiCredential,
    SecretStoreError,
    external_secret_publication,
    queue_external_secret_retirement,
    retire_pending_external_secrets,
)

type ExternalProbeConfiguration = tuple[
    ExternalProtocolKind,
    str,
    int,
    ExternalAuthenticationMode,
    str | None,
    str,
]


def _production_policy(
    session: Session, target_id: UUID | str
) -> ExternalProductionPolicyRow | None:
    # Policy lookup must not flush a dirty native row before its revision binding
    # has been prepared.
    with session.no_autoflush:
        return session.get(ExternalProductionPolicyRow, str(target_id))


class ExternalTargetError(ValueError):
    """Bounded semantic error safe for Web translation."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExternalTargetRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: UUID
    logical_name: str = Field(max_length=120)
    target_class: str = Field(pattern="^external_(development|production)$")
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
    production_policy: ProductionPolicyRead | None = None


class ProductionPolicyRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    designation_state: ProductionDesignationState
    lifecycle_state: str
    confirmed_observation_id: UUID | None
    confirmed_observation_digest: str | None = Field(
        default=None, pattern="^[0-9a-f]{64}$"
    )
    identity_generation: int = Field(ge=1)
    policy_revision: int = Field(ge=1)
    mutations_suspended: bool
    designation_confirmed_by: str | None
    designation_confirmed_at: datetime | None
    designation_invalidated_at: datetime | None


class ProductionMutationControlRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    production_mutations_blocked: bool
    policy_epoch: int = Field(ge=1)
    updated_by: str
    updated_at: datetime


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
        target_type: TargetType = TargetType.EXTERNAL_DEVELOPMENT,
    ) -> None:
        self._session_factory = session_factory
        self._endpoint_policy = endpoint_policy
        self._credential_store = credential_store
        self._verifier = verifier or NativeExternalKodiVerifier()
        self._clock = clock
        if target_type not in {
            TargetType.EXTERNAL_DEVELOPMENT,
            TargetType.EXTERNAL_PRODUCTION,
        }:
            raise ValueError(
                "external target administration requires an external class"
            )
        self._target_type = target_type
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
        _household_operation_id: UUID | None = None,
    ) -> ExternalTargetRead:
        name = self._name(logical_name)
        endpoint = self._validate_endpoint(protocol_kind, host, port)
        capabilities = self._capabilities(configured_capabilities)
        if self._target_type is TargetType.EXTERNAL_PRODUCTION and not capabilities <= {
            "target_inspect",
            "addon_inventory",
            "semantic_remote",
            "screenshot",
        }:
            raise ExternalTargetError("endpoint_not_allowed")
        target_id = uuid4()
        if _household_operation_id is not None:
            with self._session_factory() as session:
                operation = session.get(
                    HouseholdOnboardingRow, str(_household_operation_id)
                )
                if (
                    operation is None
                    or operation.stage != "reserved"
                    or self._target_type is not TargetType.EXTERNAL_PRODUCTION
                ):
                    raise ExternalTargetError("household_registration_invalid")
                target_id = UUID(operation.target_id)

        def validate_registration(session: Session) -> None:
            if _household_operation_id is not None:
                operation = session.get(
                    HouseholdOnboardingRow, str(_household_operation_id)
                )
                if operation is None or operation.stage != "reserved":
                    raise ExternalTargetError("household_registration_invalid")

        now = self._clock()
        write: Callable[[UUID], UUID] | None = None
        if authentication_mode is ExternalAuthenticationMode.USERNAME_PASSWORD:
            write = self._credential_writer(target_id, username, password)
        elif username is not None or password is not None:
            raise ExternalTargetError("secret_unavailable")
        try:
            with external_secret_publication(
                self._session_factory,
                store_kind="kodi",
                target_id=target_id,
                write=write,
                retire=self._credential_store.retire,
                validate=validate_registration,
            ) as (session, secret_reference):
                session.add(
                    TargetRow(
                        id=str(target_id),
                        name=name,
                        target_type=self._target_type,
                        kodi_version=None,
                        availability=TargetAvailability.UNAVAILABLE,
                        health=HealthStatus.WARNING,
                        connection_state=ConnectionState.DISCONNECTED,
                    )
                )
                # The subtype row is created from a scalar target UUID rather
                # than an ORM relationship.  Flush the common target first so
                # SQLite's immediate foreign-key check is satisfied on a real
                # Alembic-created database as well as Base.metadata fixtures.
                session.flush()
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
                        production_policy_revision=(
                            1
                            if self._target_type is TargetType.EXTERNAL_PRODUCTION
                            else None
                        ),
                        created_at=_to_text(now),
                        verified_at=None,
                        deactivated_at=None,
                        last_observed_at=None,
                    )
                )
                session.flush()
                if self._target_type is TargetType.EXTERNAL_PRODUCTION:
                    session.add(
                        ExternalProductionPolicyRow(
                            target_id=str(target_id),
                            designation_state=(
                                ProductionDesignationState.PENDING_NATIVE_IDENTITY
                            ),
                            confirmed_observation_id=None,
                            confirmed_observation_digest=None,
                            identity_generation=1,
                            policy_revision=1,
                            mutations_suspended=True,
                            designation_confirmed_by=None,
                            designation_confirmed_at=None,
                            designation_invalidated_at=None,
                            created_at=_to_text(now),
                            updated_at=_to_text(now),
                        )
                    )
                self._event(
                    session,
                    target_id,
                    (
                        "production_target_registered"
                        if self._target_type is TargetType.EXTERNAL_PRODUCTION
                        else "registered"
                    ),
                    now,
                )
        except SecretStoreError as error:
            raise ExternalTargetError("secret_unavailable") from error
        return self.get(target_id)

    def verify_current_identity(self, target_id: UUID) -> ExternalTargetRead:
        """Fresh bounded read probe without replacing a confirmed observation."""
        before = self.get(target_id)
        if before.verification_state is not ExternalVerificationState.VERIFIED:
            raise ExternalTargetError("needs_reverification")
        with self._session_factory() as session:
            _target, row = self._require_mutable(session, target_id)
            reference = row.secret_reference
        try:
            endpoint = self._validate_endpoint(
                before.protocol_kind, before.host, before.port
            )
            credential = (
                None
                if reference is None
                else self._credential_store.read(target_id, UUID(reference))
            )
            observed = self._verifier.probe(
                endpoint, credential, frozenset(before.configured_capabilities)
            )
        except (ExternalProbeError, SecretStoreError) as error:
            raise ExternalTargetError(
                getattr(error, "code", "secret_unavailable")
            ) from None
        if (
            observed.addresses != before.verified_addresses
            or observed.kodi_name != before.kodi_name
            or observed.kodi_version != before.kodi_version
            or observed.protocol_version != before.protocol_version
            or set(observed.verified_capabilities) != set(before.verified_capabilities)
            or self.get(target_id) != before
        ):
            raise ExternalTargetError("identity_mismatch")
        with self._session_factory.begin() as session:
            self._event(
                session,
                target_id,
                "probe_succeeded",
                self._clock(),
                classification="current_identity_verified",
                observation_id=before.pending_observation_id,
            )
        return before

    def list_targets(
        self, *, include_retired: bool = False
    ) -> list[ExternalTargetRead]:
        with self._session_factory() as session:
            statement = (
                select(ExternalKodiTargetRow, TargetRow)
                .join(TargetRow, TargetRow.id == ExternalKodiTargetRow.target_id)
                .where(TargetRow.target_type == self._target_type)
                .order_by(TargetRow.name, TargetRow.id)
            )
            if not include_retired:
                statement = statement.where(
                    ExternalKodiTargetRow.administrative_state
                    != ExternalAdministrativeState.DEACTIVATED
                )
            rows = session.execute(statement).all()
            return [self._read(session, target, external) for external, target in rows]

    def get(self, target_id: UUID) -> ExternalTargetRead:
        with self._session_factory() as session:
            external = session.get(ExternalKodiTargetRow, str(target_id))
            target = session.get(TargetRow, str(target_id))
            if external is None or target is None:
                raise ExternalTargetError("target_not_found")
            if target.target_type is not self._target_type:
                raise ExternalTargetError("target_not_found")
            return self._read(session, target, external)

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
                self._invalidate_production_policy(
                    session, target_id, now, force_generation=True
                )
                policy = _production_policy(session, target_id)
                if policy is not None:
                    bind_external_production_transition(session, row, policy, now=now)
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
        def validate_rotation(session: Session) -> None:
            _, row = self._require_mutable(session, target_id)
            if row.administrative_state is not ExternalAdministrativeState.DISABLED:
                raise ExternalTargetError("verification_required")

        current = self.get(target_id)
        if current.administrative_state is not ExternalAdministrativeState.DISABLED:
            raise ExternalTargetError("verification_required")
        write: Callable[[UUID], UUID] | None = None
        if authentication_mode is ExternalAuthenticationMode.USERNAME_PASSWORD:
            write = self._credential_writer(target_id, username, password)
        elif username is not None or password is not None:
            raise ExternalTargetError("secret_unavailable")
        now = self._clock()
        try:
            with external_secret_publication(
                self._session_factory,
                store_kind="kodi",
                target_id=target_id,
                write=write,
                retire=self._credential_store.retire,
                validate=validate_rotation,
            ) as (session, new_reference):
                _target, row = self._require_mutable(session, target_id)
                if row.administrative_state is not ExternalAdministrativeState.DISABLED:
                    raise ExternalTargetError("verification_required")
                if row.secret_reference is not None:
                    queue_external_secret_retirement(
                        session,
                        store_kind="kodi",
                        target_id=target_id,
                        reference=UUID(row.secret_reference),
                    )
                row.authentication_mode = authentication_mode
                row.secret_reference = (
                    None if new_reference is None else str(new_reference)
                )
                self._require_reverification(row)
                self._invalidate_production_policy(
                    session, target_id, now, force_generation=True
                )
                policy = _production_policy(session, target_id)
                if policy is not None:
                    bind_external_production_transition(session, row, policy, now=now)
                self._event(session, target_id, "credential_rotated", now)
                self._event(session, target_id, "reverification_required", now)
        except SecretStoreError as error:
            raise ExternalTargetError("secret_unavailable") from error
        retire_pending_external_secrets(
            self._session_factory,
            store_kind="kodi",
            target_id=target_id,
            retire=self._credential_store.retire,
        )
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
                or row.verification_state is ExternalVerificationState.VERIFIED
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
            policy = _production_policy(session, target_id)
            if (
                policy is not None
                and policy.designation_state is not ProductionDesignationState.CONFIRMED
            ):
                policy.designation_state = (
                    ProductionDesignationState.PENDING_CONFIRMATION
                )
            if policy is not None:
                bind_external_production_transition(session, row, policy, now=now)
            self._event(
                session,
                target_id,
                "verification_confirmed",
                now,
                observation_id=observation_id,
            )
            if policy is not None:
                self._event(
                    session,
                    target_id,
                    "production_native_identity_confirmed",
                    now,
                    observation_id=observation_id,
                )
        return self.get(target_id)

    def confirm_production_designation(
        self,
        target_id: UUID,
        observation_id: UUID,
        *,
        administrator_reference: str = "local_administrator",
    ) -> ExternalTargetRead:
        if self._target_type is not TargetType.EXTERNAL_PRODUCTION:
            raise ExternalTargetError("target_not_found")
        now = self._clock()
        with self._session_factory.begin() as session:
            _target, row = self._require_mutable(session, target_id)
            policy = _production_policy(session, target_id)
            observation = session.get(
                ExternalKodiTargetObservationRow, str(observation_id)
            )
            if (
                policy is None
                or row.verification_state is not ExternalVerificationState.VERIFIED
                or row.pending_observation_id != str(observation_id)
                or observation is None
                or observation.target_id != str(target_id)
                or observation.outcome != ExternalObservationState.REACHABLE.value
                or observation.error_code is not None
                or policy.designation_state
                is not ProductionDesignationState.PENDING_CONFIRMATION
            ):
                raise ExternalTargetError("production_designation_required")
            administrator = administrator_reference.strip()
            if not administrator or len(administrator) > 64:
                raise ExternalTargetError("production_designation_required")
            digest = production_observation_digest(row, observation)
            policy.designation_state = ProductionDesignationState.CONFIRMED
            policy.confirmed_observation_id = str(observation_id)
            policy.confirmed_observation_digest = digest
            policy.designation_confirmed_by = administrator
            policy.designation_confirmed_at = _to_text(now)
            policy.designation_invalidated_at = None
            advance_production_policy(policy, now=now)
            self._event(
                session,
                target_id,
                "production_designation_confirmed",
                now,
                classification=f"revision_{policy.policy_revision}",
                observation_id=observation_id,
            )
        return self.get(target_id)

    def mutation_control(self) -> ProductionMutationControlRead:
        with self._session_factory() as session:
            row = session.get(ProductionMutationControlRow, 1)
            if row is None:
                raise ExternalTargetError("production_policy_missing")
            updated = _from_text(row.updated_at)
            assert updated is not None
            return ProductionMutationControlRead(
                production_mutations_blocked=row.production_mutations_blocked,
                policy_epoch=row.policy_epoch,
                updated_by=row.updated_by,
                updated_at=updated,
            )

    def set_mutation_block(
        self,
        *,
        blocked: bool,
        administrator_reference: str = "local_administrator",
    ) -> ProductionMutationControlRead:
        if self._target_type is not TargetType.EXTERNAL_PRODUCTION:
            raise ExternalTargetError("target_not_found")
        now = self._clock()
        administrator = administrator_reference.strip()
        if not administrator or len(administrator) > 64:
            raise ExternalTargetError("production_policy_missing")
        with self._session_factory.begin() as session:
            row = session.get(ProductionMutationControlRow, 1)
            if row is None:
                raise ExternalTargetError("production_policy_missing")
            if row.production_mutations_blocked != blocked:
                row.production_mutations_blocked = blocked
                row.policy_epoch += 1
                row.updated_by = administrator
                row.updated_at = _to_text(now)
                self._cancel_live_approvals(session, now=now)
                session.add(
                    McpSecurityEventRow(
                        id=str(uuid4()),
                        event_kind=(
                            "production_mutations_blocked"
                            if blocked
                            else "production_mutations_unblocked"
                        ),
                        actor="web_admin",
                        client_id=None,
                        credential_id=None,
                        target_id=None,
                        development_source_id=None,
                        workspace_id=None,
                        preset_id=None,
                        occurred_at=_to_text(now),
                    )
                )
        return self.mutation_control()

    def set_target_mutation_suspension(
        self,
        target_id: UUID,
        *,
        suspended: bool,
    ) -> ExternalTargetRead:
        if self._target_type is not TargetType.EXTERNAL_PRODUCTION:
            raise ExternalTargetError("target_not_found")
        now = self._clock()
        with self._session_factory.begin() as session:
            _target, _row = self._require_mutable(session, target_id)
            policy = _production_policy(session, target_id)
            if policy is None:
                raise ExternalTargetError("production_policy_missing")
            if policy.mutations_suspended != suspended:
                policy.mutations_suspended = suspended
                advance_production_policy(policy, now=now)
                self._cancel_live_approvals(session, now=now, target_id=target_id)
                self._event(
                    session,
                    target_id,
                    (
                        "production_mutations_suspended"
                        if suspended
                        else "production_mutations_resumed"
                    ),
                    now,
                    classification=f"revision_{policy.policy_revision}",
                )
        return self.get(target_id)

    def set_enabled(self, target_id: UUID, *, enabled: bool) -> ExternalTargetRead:
        now = self._clock()
        with self._session_factory.begin() as session:
            target, row = self._require_mutable(session, target_id)
            previous_state = row.administrative_state
            if enabled:
                if (
                    row.verification_state
                    is ExternalVerificationState.IDENTITY_MISMATCH
                ):
                    raise ExternalTargetError("identity_mismatch")
                if row.verification_state is not ExternalVerificationState.VERIFIED:
                    raise ExternalTargetError("verification_required")
                policy = _production_policy(session, target_id)
                if target.target_type is TargetType.EXTERNAL_PRODUCTION and (
                    policy is None
                    or policy.designation_state
                    is not ProductionDesignationState.CONFIRMED
                ):
                    raise ExternalTargetError("production_designation_required")
                row.administrative_state = ExternalAdministrativeState.ENABLED
                target.availability = TargetAvailability.AVAILABLE
                self._event(session, target_id, "enabled", now)
            else:
                row.administrative_state = ExternalAdministrativeState.DISABLED
                target.availability = TargetAvailability.UNAVAILABLE
                self._event(session, target_id, "disabled", now)
            if (
                target.target_type is TargetType.EXTERNAL_PRODUCTION
                and row.administrative_state is not previous_state
            ):
                policy = _production_policy(session, target_id)
                if policy is None:
                    raise ExternalTargetError("production_policy_missing")
                bind_external_production_transition(session, row, policy, now=now)
                self._cancel_live_approvals(session, now=now, target_id=target_id)
        return self.get(target_id)

    def deactivate(self, target_id: UUID) -> ExternalTargetRead:
        now = self._clock()
        with self._session_factory.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            target, row = self._require_mutable(
                session, target_id, allow_deactivated=True
            )
            if row.administrative_state is not ExternalAdministrativeState.DEACTIVATED:
                old_reference = (
                    None if row.secret_reference is None else UUID(row.secret_reference)
                )
                if old_reference is not None:
                    queue_external_secret_retirement(
                        session,
                        store_kind="kodi",
                        target_id=target_id,
                        reference=old_reference,
                    )
                row.secret_reference = None
                row.administrative_state = ExternalAdministrativeState.DEACTIVATED
                row.deactivated_at = _to_text(now)
                policy = _production_policy(session, target_id)
                if policy is not None:
                    policy.designation_state = ProductionDesignationState.DEACTIVATED
                    policy.mutations_suspended = True
                    bind_external_production_transition(session, row, policy, now=now)
                    self._cancel_live_approvals(session, now=now, target_id=target_id)
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
        self.retire_deactivated_credentials(target_id)
        return self.get(target_id)

    def retire_deactivated_credentials(self, target_id: UUID) -> None:
        with self._session_factory() as session:
            if session.get(TargetRow, str(target_id)) is not None:
                _, row = self._require_mutable(
                    session, target_id, allow_deactivated=True
                )
                if (
                    row.administrative_state
                    is not ExternalAdministrativeState.DEACTIVATED
                ):
                    raise ExternalTargetError("verification_required")
        retire_pending_external_secrets(
            self._session_factory,
            store_kind="kodi",
            target_id=target_id,
            retire=self._credential_store.retire,
        )

    @staticmethod
    def _cancel_live_approvals(
        session: Session, *, now: datetime, target_id: UUID | None = None
    ) -> None:
        statement = select(ProductionOperationApprovalRow).where(
            ProductionOperationApprovalRow.state.in_(("pending", "approved"))
        )
        if target_id is not None:
            statement = statement.where(
                ProductionOperationApprovalRow.target_id == str(target_id)
            )
        for approval in session.scalars(statement).all():
            previous = approval.state
            approval.state = "cancelled"
            approval.terminal_reason = "mutation_policy_changed"
            session.add(
                ProductionApprovalTransitionEventRow(
                    id=str(uuid4()),
                    approval_id=approval.id,
                    event_kind="cancelled",
                    from_state=previous,
                    to_state="cancelled",
                    terminal_reason="mutation_policy_changed",
                    actor_kind="system",
                    actor_reference="mutation_policy",
                    linked_remote_receipt_id=None,
                    occurred_at=_to_text(now),
                )
            )

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
            production_material_changed = row.pending_observation_id is not None
            row.pending_observation_id = None
            if row.verification_state is ExternalVerificationState.VERIFIED:
                production_material_changed = True
                row.verification_state = ExternalVerificationState.NEEDS_REVERIFICATION
                self._invalidate_production_policy(session, target_id, completed)
                self._event(session, target_id, "reverification_required", completed)
            policy = _production_policy(session, target_id)
            if policy is not None and production_material_changed:
                bind_external_production_transition(session, row, policy, now=completed)
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
                self._invalidate_production_policy(
                    session,
                    target_id,
                    completed,
                    identity_mismatch=True,
                )
            policy = _production_policy(session, target_id)
            if not mismatch and policy is not None:
                if policy.designation_state in {
                    ProductionDesignationState.PENDING_NATIVE_IDENTITY,
                    ProductionDesignationState.INVALIDATED,
                }:
                    policy.designation_state = (
                        ProductionDesignationState.PENDING_NATIVE_IDENTITY
                    )
                self._event(
                    session,
                    target_id,
                    "native_observation_obtained",
                    completed,
                    observation_id=observation_id,
                )
            if policy is not None:
                bind_external_production_transition(session, row, policy, now=completed)
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

    def _invalidate_production_policy(
        self,
        session: Session,
        target_id: UUID,
        now: datetime,
        *,
        identity_mismatch: bool = False,
        force_generation: bool = False,
    ) -> None:
        policy = _production_policy(session, target_id)
        if policy is None:
            return
        already_invalid = (
            policy.designation_state is ProductionDesignationState.INVALIDATED
            and policy.confirmed_observation_id is None
        )
        if already_invalid and not force_generation:
            return
        policy.identity_generation += 1
        policy.designation_state = ProductionDesignationState.INVALIDATED
        policy.confirmed_observation_id = None
        policy.confirmed_observation_digest = None
        policy.designation_confirmed_by = None
        policy.designation_confirmed_at = None
        policy.designation_invalidated_at = _to_text(now)
        policy.mutations_suspended = True
        self._event(
            session,
            target_id,
            "production_designation_invalidated",
            now,
            classification=(
                "identity_mismatch" if identity_mismatch else "configuration_changed"
            ),
        )

    @staticmethod
    def _require_reverification(row: ExternalKodiTargetRow) -> None:
        row.verification_state = ExternalVerificationState.NEEDS_REVERIFICATION
        row.pending_observation_id = None
        row.verified_capabilities_json = "[]"

    def _require_mutable(
        self, session: Session, target_id: UUID, *, allow_deactivated: bool = False
    ) -> tuple[TargetRow, ExternalKodiTargetRow]:
        target = session.get(TargetRow, str(target_id))
        row = session.get(ExternalKodiTargetRow, str(target_id))
        if target is None or row is None:
            raise ExternalTargetError("target_not_found")
        if target.target_type is not self._target_type:
            raise ExternalTargetError("target_not_found")
        if (
            target.target_type is TargetType.EXTERNAL_PRODUCTION
            and _production_policy(session, target_id) is None
        ):
            raise ExternalTargetError("production_policy_missing")
        if (
            row.administrative_state is ExternalAdministrativeState.DEACTIVATED
            and not allow_deactivated
        ):
            raise ExternalTargetError("verification_required")
        return target, row

    def _validate_endpoint(
        self, protocol: ExternalProtocolKind, host: str, port: int
    ) -> ValidatedExternalEndpoint:
        try:
            return self._endpoint_policy.validate(protocol, host, port)
        except ExternalEndpointError as error:
            raise ExternalTargetError("endpoint_not_allowed") from error

    def _credential_writer(
        self, target_id: UUID, username: str | None, password: str | None
    ) -> Callable[[UUID], UUID]:
        if username is None or password is None:
            raise ExternalTargetError("secret_unavailable")
        try:
            credential = ExternalKodiCredential(username=username, password=password)
        except ValueError as error:
            raise ExternalTargetError("secret_unavailable") from error
        return lambda reference: self._credential_store.write(
            target_id,
            credential,
            reference=reference,
        )

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

    def _read(
        self,
        session: Session,
        target: TargetRow,
        row: ExternalKodiTargetRow,
    ) -> ExternalTargetRead:
        created = _from_text(row.created_at)
        assert created is not None
        production_policy = None
        if target.target_type is TargetType.EXTERNAL_PRODUCTION:
            policy = _production_policy(session, row.target_id)
            if policy is None:
                raise ExternalTargetError("production_policy_missing")
            production_policy = self._production_policy_read(row, policy)
        return ExternalTargetRead(
            target_id=UUID(row.target_id),
            logical_name=target.name,
            target_class=target.target_type.value,
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
            production_policy=production_policy,
        )

    @staticmethod
    def _production_policy_read(
        row: ExternalKodiTargetRow, policy: ExternalProductionPolicyRow
    ) -> ProductionPolicyRead:
        if row.administrative_state is ExternalAdministrativeState.DEACTIVATED:
            lifecycle = "deactivated"
        elif row.verification_state is ExternalVerificationState.IDENTITY_MISMATCH:
            lifecycle = "identity_mismatch"
        elif row.verification_state is ExternalVerificationState.NEEDS_REVERIFICATION:
            lifecycle = "needs_reverification"
        elif (
            row.verification_state is ExternalVerificationState.UNVERIFIED
            and row.pending_observation_id is None
        ):
            lifecycle = "registered_disabled_unverified"
        elif row.verification_state is ExternalVerificationState.UNVERIFIED:
            lifecycle = "observed_pending_confirmation"
        elif (
            policy.designation_state is ProductionDesignationState.PENDING_CONFIRMATION
        ):
            lifecycle = "production_designation_pending"
        elif policy.designation_state is ProductionDesignationState.CONFIRMED:
            lifecycle = (
                "enabled_read_only"
                if row.administrative_state is ExternalAdministrativeState.ENABLED
                else "production_designation_confirmed"
            )
        elif policy.designation_state is ProductionDesignationState.INVALIDATED:
            lifecycle = "needs_reverification"
        else:
            lifecycle = "native_identity_confirmed"
        return ProductionPolicyRead(
            designation_state=policy.designation_state,
            lifecycle_state=lifecycle,
            confirmed_observation_id=(
                None
                if policy.confirmed_observation_id is None
                else UUID(policy.confirmed_observation_id)
            ),
            confirmed_observation_digest=policy.confirmed_observation_digest,
            identity_generation=policy.identity_generation,
            policy_revision=policy.policy_revision,
            mutations_suspended=policy.mutations_suspended,
            designation_confirmed_by=policy.designation_confirmed_by,
            designation_confirmed_at=_from_text(policy.designation_confirmed_at),
            designation_invalidated_at=_from_text(policy.designation_invalidated_at),
        )
