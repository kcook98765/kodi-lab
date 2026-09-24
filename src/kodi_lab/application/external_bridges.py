"""Web-only bridge pairing and bounded external screenshot service."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.production_inspection import (
    ProductionInspectionError,
    evaluate_production_inspection_state,
)
from kodi_lab.development.deployment import DevelopmentTreeManifest
from kodi_lab.domain import (
    ExternalAdministrativeState,
    ExternalBridgePairingState,
    ExternalBridgeReachabilityState,
    ExternalVerificationState,
    TargetType,
)
from kodi_lab.kodi.bridge import (
    BRIDGE_ADDON_ID,
    BRIDGE_PROTOCOL_VERSION,
    DEPLOYMENT_CHUNK_BYTES,
    SCREENSHOT_ERROR_CODES,
    BridgeAddonTree,
    BridgeDeploymentReceipt,
    BridgeIdentity,
    BridgeScreenshotResult,
    ExternalBridgeClient,
    ExternalBridgeError,
    b64url,
)
from kodi_lab.kodi.external import (
    ExternalEndpointError,
    ExternalEndpointPolicy,
    ValidatedExternalEndpoint,
)
from kodi_lab.screenshot_diagnostics import (
    bind_screenshot_bridge_revision,
    emit_screenshot_stage,
)
from kodi_lab.storage.models import (
    ExternalBridgeEventRow,
    ExternalBridgePairingRow,
    ExternalBridgeRequestReceiptRow,
    ExternalKodiTargetRow,
    ExternalProductionPolicyRow,
    ProductionBridgeHttpAcknowledgementRow,
    ReviewedBridgeBuildRow,
    TargetRow,
)
from kodi_lab.storage.secrets import ExternalBridgeSecretStore, SecretStoreError

MAX_DEPLOYMENT_DURATION_SECONDS = 2 * 60 * 60
MAX_DEPLOYMENT_TRANSFER_BYTES_PER_SECOND = 16 * 1024 * 1024
PAIRING_TOKEN_TTL = timedelta(minutes=10)
BRIDGE_RECEIPT_TTL = timedelta(hours=24)
PRODUCTION_PIXEL_CACHE_TTL = timedelta(seconds=120)
MAX_RECEIPTS_PER_TARGET = 256
MAX_RECEIPTS_GLOBAL = 4096
PRODUCTION_TRANSPORT_POLICY_REVISION = 1


class ExternalBridgeServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ExternalBridgeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: UUID
    bridge_port: int | None = Field(default=None, ge=1, le=65_535)
    pairing_state: ExternalBridgePairingState
    reachability_state: ExternalBridgeReachabilityState
    pairing_token_pending: bool
    pairing_token_expires_at: datetime | None
    observed_bridge_id: UUID | None
    observed_protocol_version: str | None = Field(max_length=16)
    observed_addon_id: str | None = Field(max_length=128)
    observed_addon_version: str | None = Field(max_length=64)
    observed_kodi_version: str | None = Field(max_length=64)
    observed_build_fingerprint: str | None = Field(max_length=64)
    observed_capabilities: tuple[str, ...]
    pinned_bridge_id: UUID | None
    protocol_version: str | None = Field(default=None, max_length=16)
    addon_id: str | None = Field(default=None, max_length=128)
    addon_version: str | None = Field(default=None, max_length=64)
    kodi_version: str | None = Field(default=None, max_length=64)
    build_fingerprint: str | None = Field(default=None, max_length=64)
    verified_capabilities: tuple[str, ...]
    paired_at: datetime | None
    verified_at: datetime | None
    revoked_at: datetime | None
    http_screenshot_cleartext_warning: bool
    production_identity_generation: int | None = None
    production_bridge_trust_revision: int | None = None
    reviewed_build_id: UUID | None = None
    production_confirmed_at: datetime | None = None


class ReviewedBridgeBuildRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    build_id: UUID
    semantic_fingerprint: str = Field(min_length=64, max_length=64)
    zip_sha256: str = Field(min_length=64, max_length=64)
    addon_id: str = Field(min_length=1, max_length=128)
    addon_version: str = Field(min_length=1, max_length=64)
    protocol_version: str = Field(min_length=1, max_length=16)
    capabilities: tuple[str, ...]
    supported_kodi_versions: tuple[str, ...]
    source_reference: str = Field(min_length=1, max_length=512)
    reviewed_by: str = Field(min_length=1, max_length=64)
    reviewed_at: datetime
    state: str
    retired_by: str | None = None
    retired_at: datetime | None = None


class ProductionHttpAcknowledgementRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    acknowledgement_id: UUID
    target_id: UUID
    identity_generation: int
    bridge_trust_revision: int
    endpoint_digest: str
    transport_policy_revision: int
    acknowledged_by: str
    acknowledged_at: datetime


class PairingTokenResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: UUID
    token: str = Field(min_length=43, max_length=43)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ExternalScreenshotCapture:
    capture_id: UUID
    target_id: UUID
    bridge_id: UUID
    captured_at: datetime
    width: int
    height: int
    byte_size: int
    digest: str
    png: bytes


@dataclass(frozen=True, slots=True)
class _Snapshot:
    target_id: UUID
    endpoint: ValidatedExternalEndpoint
    bridge_port: int
    bridge_id: UUID
    secret_reference: UUID
    kodi_version: str
    build_fingerprint: str
    endpoint_digest: str
    addon_id: str = BRIDGE_ADDON_ID
    addon_version: str = ""
    protocol_version: str = BRIDGE_PROTOCOL_VERSION
    target_type: TargetType = TargetType.EXTERNAL_DEVELOPMENT
    identity_generation: int | None = None
    bridge_trust_revision: int | None = None


@dataclass(frozen=True, slots=True)
class _PairingSnapshot:
    endpoint: ValidatedExternalEndpoint
    bridge_port: int
    token_reference: UUID
    kodi_version: str
    target_type: TargetType = TargetType.EXTERNAL_DEVELOPMENT
    identity_generation: int | None = None
    bridge_trust_revision: int | None = None


class _BoundedGate:
    def __init__(
        self,
        *,
        global_limit: int,
        target_limit: int = 1024,
        rejection_code: str = "bridge_replay_rejected",
    ) -> None:
        self._global = threading.BoundedSemaphore(global_limit)
        self._guard = threading.Lock()
        self._targets: dict[UUID, threading.Lock] = {}
        self._target_limit = target_limit
        self._rejection_code = rejection_code

    @contextmanager
    def enter(self, target_id: UUID) -> Iterator[None]:
        with self._guard:
            lock = self._targets.get(target_id)
            if lock is None:
                if len(self._targets) >= self._target_limit:
                    raise ExternalBridgeServiceError(self._rejection_code)
                lock = threading.Lock()
                self._targets[target_id] = lock
        if not self._global.acquire(blocking=False):
            raise ExternalBridgeServiceError(self._rejection_code)
        if not lock.acquire(blocking=False):
            self._global.release()
            raise ExternalBridgeServiceError(self._rejection_code)
        try:
            yield
        finally:
            lock.release()
            self._global.release()


class ExternalBridgeService:
    """No MCP administration: Web pairs; MCP can only consume verified capture."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        endpoint_policy: ExternalEndpointPolicy,
        secret_store: ExternalBridgeSecretStore,
        client: ExternalBridgeClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = session_factory
        self._endpoint_policy = endpoint_policy
        self._secrets = secret_store
        self._client = client or ExternalBridgeClient(clock=clock)
        self._clock = clock
        self._pairing_gate = _BoundedGate(global_limit=2)
        self._screenshot_gate = _BoundedGate(global_limit=4)
        self._production_screenshot_gate = _BoundedGate(
            global_limit=1, rejection_code="rate_limited"
        )
        self._deployment_gate = _BoundedGate(global_limit=2)
        self._production_pixel_cache: dict[
            tuple[UUID, UUID], ExternalScreenshotCapture
        ] = {}
        self._production_pixel_cache_lock = threading.Lock()
        with self._sessions() as session:
            protected = session.scalar(
                select(ExternalBridgePairingRow.target_id)
                .where(
                    (ExternalBridgePairingRow.pairing_token_reference.is_not(None))
                    | (ExternalBridgePairingRow.bridge_secret_reference.is_not(None))
                )
                .limit(1)
            )
        if protected is not None and not self._secrets.available:
            raise RuntimeError(
                "protected external bridge secrets exist but the master key "
                "is unavailable"
            )

    def status(self, target_id: UUID) -> ExternalBridgeStatus:
        with self._sessions() as session:
            target = session.get(TargetRow, str(target_id))
            external = session.get(ExternalKodiTargetRow, str(target_id))
            row = session.get(ExternalBridgePairingRow, str(target_id))
        if (
            target is None
            or external is None
            or target.target_type
            not in {TargetType.EXTERNAL_DEVELOPMENT, TargetType.EXTERNAL_PRODUCTION}
        ):
            raise ExternalBridgeServiceError("target_not_found")
        if row is None:
            return ExternalBridgeStatus(
                target_id=target_id,
                pairing_state=ExternalBridgePairingState.UNPAIRED,
                reachability_state=ExternalBridgeReachabilityState.UNKNOWN,
                pairing_token_pending=False,
                pairing_token_expires_at=None,
                observed_bridge_id=None,
                observed_protocol_version=None,
                observed_addon_id=None,
                observed_addon_version=None,
                observed_kodi_version=None,
                observed_build_fingerprint=None,
                observed_capabilities=(),
                pinned_bridge_id=None,
                protocol_version=None,
                addon_id=None,
                addon_version=None,
                kodi_version=None,
                build_fingerprint=None,
                verified_capabilities=(),
                paired_at=None,
                verified_at=None,
                revoked_at=None,
                http_screenshot_cleartext_warning=(
                    external.protocol_kind.value == "jsonrpc_http"
                ),
                production_identity_generation=None,
                production_bridge_trust_revision=None,
                reviewed_build_id=None,
                production_confirmed_at=None,
            )
        return self._project(target_id, external, row)

    def register_reviewed_build(
        self,
        *,
        semantic_fingerprint: str,
        zip_sha256: str,
        addon_id: str,
        addon_version: str,
        protocol_version: str,
        capabilities: tuple[str, ...],
        supported_kodi_versions: tuple[str, ...],
        source_reference: str,
        administrator_reference: str = "local_administrator",
    ) -> ReviewedBridgeBuildRead:
        """Register explicit reviewed provenance; no build is seeded implicitly."""

        administrator = self._administrator(administrator_reference)
        fingerprint = self._sha256(semantic_fingerprint)
        archive = self._sha256(zip_sha256)
        capability_set = tuple(sorted(set(capabilities)))
        kodi_versions = tuple(sorted(set(supported_kodi_versions)))
        if (
            capability_set
            not in {("screenshot",), ("screenshot", "workspace_deployment")}
            or not kodi_versions
            or any(not value or len(value) > 32 for value in kodi_versions)
            or not addon_id
            or len(addon_id) > 128
            or not addon_version
            or len(addon_version) > 64
            or not protocol_version
            or len(protocol_version) > 16
            or not source_reference.strip()
            or len(source_reference.strip()) > 512
        ):
            raise ExternalBridgeServiceError("bridge_build_not_reviewed")
        build_id = uuid4()
        now = self._clock()
        try:
            with self._sessions.begin() as session:
                session.add(
                    ReviewedBridgeBuildRow(
                        id=str(build_id),
                        semantic_fingerprint=fingerprint,
                        zip_sha256=archive,
                        addon_id=addon_id,
                        addon_version=addon_version,
                        protocol_version=protocol_version,
                        capabilities_json=self._closed(capability_set),
                        supported_kodi_versions_json=json.dumps(
                            kodi_versions, separators=(",", ":")
                        ),
                        source_reference=source_reference.strip(),
                        reviewed_by=administrator,
                        reviewed_at=self._text(now),
                        state="active",
                        retired_by=None,
                        retired_at=None,
                    )
                )
        except IntegrityError as error:
            raise ExternalBridgeServiceError("bridge_build_not_reviewed") from error
        return self.get_reviewed_build(build_id)

    def list_reviewed_builds(self) -> list[ReviewedBridgeBuildRead]:
        with self._sessions() as session:
            rows = session.scalars(
                select(ReviewedBridgeBuildRow)
                .order_by(ReviewedBridgeBuildRow.reviewed_at, ReviewedBridgeBuildRow.id)
                .limit(1001)
            ).all()
            if len(rows) > 1000:
                raise ExternalBridgeServiceError("bridge_build_not_reviewed")
            return [self._reviewed_build_read(row) for row in rows]

    def get_reviewed_build(self, build_id: UUID) -> ReviewedBridgeBuildRead:
        with self._sessions() as session:
            row = session.get(ReviewedBridgeBuildRow, str(build_id))
            if row is None:
                raise ExternalBridgeServiceError("bridge_build_not_reviewed")
            return self._reviewed_build_read(row)

    def retire_reviewed_build(
        self,
        build_id: UUID,
        *,
        administrator_reference: str = "local_administrator",
    ) -> ReviewedBridgeBuildRead:
        administrator = self._administrator(administrator_reference)
        now = self._clock()
        with self._sessions.begin() as session:
            row = session.get(ReviewedBridgeBuildRow, str(build_id))
            if row is None:
                raise ExternalBridgeServiceError("bridge_build_not_reviewed")
            if row.state == "active":
                row.state = "retired"
                row.retired_by = administrator
                row.retired_at = self._text(now)
                affected = session.scalars(
                    select(ExternalBridgePairingRow).where(
                        ExternalBridgePairingRow.reviewed_build_id == str(build_id),
                        ExternalBridgePairingRow.pairing_state
                        == ExternalBridgePairingState.PAIRED,
                    )
                ).all()
                for pairing in affected:
                    pairing.production_bridge_trust_revision = (
                        pairing.production_bridge_trust_revision or 0
                    ) + 1
                    pairing.pairing_state = (
                        ExternalBridgePairingState.NEEDS_REVERIFICATION
                    )
                    pairing.verified_capabilities_json = "[]"
                    self._event(
                        session,
                        UUID(pairing.target_id),
                        "production_bridge_revision_advanced",
                        now,
                        classification="reviewed_build_retired",
                    )
        return self.get_reviewed_build(build_id)

    def acknowledge_production_http_risk(
        self,
        target_id: UUID,
        *,
        bridge_port: int,
        administrator_reference: str = "local_administrator",
    ) -> ProductionHttpAcknowledgementRead:
        administrator = self._administrator(administrator_reference)
        with self._sessions() as session:
            external = self._require_native_verified(session, target_id)
            target = session.get(TargetRow, str(target_id))
            policy = session.get(ExternalProductionPolicyRow, str(target_id))
            pairing = session.get(ExternalBridgePairingRow, str(target_id))
            if (
                target is None
                or target.target_type is not TargetType.EXTERNAL_PRODUCTION
                or policy is None
                or external.protocol_kind.value != "jsonrpc_http"
            ):
                raise ExternalBridgeServiceError("transport_acknowledgement_required")
            self._validate_bridge_endpoint(external, bridge_port)
            revision = self._prospective_production_revision(pairing)
            endpoint_digest = self._endpoint_digest(external, bridge_port)
            identity_generation = policy.identity_generation
        acknowledgement_id = uuid4()
        now = self._clock()
        try:
            with self._sessions.begin() as session:
                session.add(
                    ProductionBridgeHttpAcknowledgementRow(
                        id=str(acknowledgement_id),
                        target_id=str(target_id),
                        identity_generation=identity_generation,
                        bridge_trust_revision=revision,
                        protocol="jsonrpc_http",
                        ip_literal=external.host,
                        native_port=external.port,
                        bridge_port=bridge_port,
                        endpoint_digest=endpoint_digest,
                        transport_policy_revision=PRODUCTION_TRANSPORT_POLICY_REVISION,
                        acknowledged_by=administrator,
                        acknowledged_at=self._text(now),
                    )
                )
        except IntegrityError as error:
            with self._sessions() as session:
                existing = session.scalar(
                    select(ProductionBridgeHttpAcknowledgementRow).where(
                        ProductionBridgeHttpAcknowledgementRow.target_id
                        == str(target_id),
                        ProductionBridgeHttpAcknowledgementRow.identity_generation
                        == identity_generation,
                        ProductionBridgeHttpAcknowledgementRow.bridge_trust_revision
                        == revision,
                        ProductionBridgeHttpAcknowledgementRow.endpoint_digest
                        == endpoint_digest,
                        ProductionBridgeHttpAcknowledgementRow.transport_policy_revision
                        == PRODUCTION_TRANSPORT_POLICY_REVISION,
                    )
                )
                if existing is None:
                    raise ExternalBridgeServiceError(
                        "transport_acknowledgement_required"
                    ) from error
                acknowledgement_id = UUID(existing.id)
                administrator = existing.acknowledged_by
                now = self._datetime(existing.acknowledged_at)
        return ProductionHttpAcknowledgementRead(
            acknowledgement_id=acknowledgement_id,
            target_id=target_id,
            identity_generation=identity_generation,
            bridge_trust_revision=revision,
            endpoint_digest=endpoint_digest,
            transport_policy_revision=PRODUCTION_TRANSPORT_POLICY_REVISION,
            acknowledged_by=administrator,
            acknowledged_at=now,
        )

    def generate_pairing_token(
        self, target_id: UUID, *, bridge_port: int
    ) -> PairingTokenResult:
        token = os.urandom(32)
        now = self._clock()
        expires = now + PAIRING_TOKEN_TTL
        old_references: list[UUID] = []
        with self._sessions() as session:
            external = self._require_native_verified(session, target_id)
            self._validate_bridge_endpoint(external, bridge_port)
            target = session.get(TargetRow, str(target_id))
            pairing = session.get(ExternalBridgePairingRow, str(target_id))
            if (
                target is not None
                and target.target_type is TargetType.EXTERNAL_PRODUCTION
            ):
                policy = session.get(ExternalProductionPolicyRow, str(target_id))
                assert policy is not None
                revision = self._prospective_production_revision(pairing)
                self._require_http_acknowledgement(
                    session,
                    external,
                    target_id,
                    bridge_port,
                    policy.identity_generation,
                    revision,
                )
        try:
            reference = self._secrets.write(target_id, "pairing_token", token)
        except SecretStoreError as error:
            raise ExternalBridgeServiceError("bridge_authentication_failed") from error
        try:
            with self._sessions.begin() as session:
                external = self._require_native_verified(session, target_id)
                self._validate_bridge_endpoint(external, bridge_port)
                target = session.get(TargetRow, str(target_id))
                existing = session.get(ExternalBridgePairingRow, str(target_id))
                if (
                    target is not None
                    and target.target_type is TargetType.EXTERNAL_PRODUCTION
                ):
                    policy = session.get(ExternalProductionPolicyRow, str(target_id))
                    assert policy is not None
                    production_revision = self._prospective_production_revision(
                        existing
                    )
                    self._require_http_acknowledgement(
                        session,
                        external,
                        target_id,
                        bridge_port,
                        policy.identity_generation,
                        production_revision,
                    )
                row = session.get(ExternalBridgePairingRow, str(target_id))
                if row is None:
                    row = ExternalBridgePairingRow(
                        target_id=str(target_id),
                        pairing_state=ExternalBridgePairingState.UNPAIRED,
                        reachability_state=ExternalBridgeReachabilityState.UNKNOWN,
                        observed_capabilities_json="[]",
                        verified_capabilities_json="[]",
                    )
                    session.add(row)
                    # The semantic event references this lazily created parent
                    # by scalar target UUID.  Publish the parent first so a
                    # real migrated SQLite database can enforce its immediate
                    # foreign-key constraint deterministically.
                    session.flush()
                for value in (
                    row.pairing_token_reference,
                    row.bridge_secret_reference,
                ):
                    if value is not None:
                        old_references.append(UUID(value))
                self._clear_identity(row)
                if (
                    target is not None
                    and target.target_type is TargetType.EXTERNAL_PRODUCTION
                ):
                    # Preserve the monotonic revision while the new possession
                    # ceremony is pending; old scopes stay permanently stale.
                    row.production_identity_generation = None
                    row.reviewed_build_id = None
                    row.production_confirmed_by = None
                    row.production_confirmed_at = None
                    row.production_bridge_trust_revision = production_revision
                row.bridge_port = bridge_port
                row.pairing_state = ExternalBridgePairingState.UNPAIRED
                row.reachability_state = ExternalBridgeReachabilityState.UNKNOWN
                row.pairing_token_reference = str(reference)
                row.bridge_secret_reference = None
                row.pairing_token_expires_at = self._text(expires)
                row.token_generated_at = self._text(now)
                row.revoked_at = None
                self._event(session, target_id, "pairing_token_generated", now)
        except Exception:
            self._secrets.retire(reference)
            raise
        for old in old_references:
            self._secrets.retire(old)
        return PairingTokenResult(
            target_id=target_id, token=b64url(token), expires_at=expires
        )

    def attempt_pairing(self, target_id: UUID) -> ExternalBridgeStatus:
        with self._pairing_gate.enter(target_id):
            snapshot = self._pairing_snapshot(target_id)
            now = self._clock()
            with self._sessions.begin() as session:
                row = session.get(ExternalBridgePairingRow, str(target_id))
                assert row is not None
                row.pairing_attempted_at = self._text(now)
                self._event(session, target_id, "pairing_attempted", now)
            try:
                token = self._secrets.read(
                    target_id, snapshot.token_reference, "pairing_token"
                )
                pairing = self._client.pair(
                    snapshot.endpoint,
                    bridge_port=snapshot.bridge_port,
                    target_id=target_id,
                    token=token,
                )
                self._require_native_identity(pairing.identity, snapshot.kodi_version)
                authenticated_identity = self._client.identity(
                    snapshot.endpoint,
                    bridge_port=snapshot.bridge_port,
                    target_id=target_id,
                    bridge_id=pairing.identity.bridge_id,
                    secret=pairing.secret,
                )
                capabilities = self._client.capabilities(
                    snapshot.endpoint,
                    bridge_port=snapshot.bridge_port,
                    target_id=target_id,
                    bridge_id=pairing.identity.bridge_id,
                    secret=pairing.secret,
                )
                if authenticated_identity != pairing.identity or not set(
                    capabilities
                ).intersection({"screenshot", "workspace_deployment"}):
                    raise ExternalBridgeServiceError("bridge_identity_mismatch")
                if snapshot.target_type is TargetType.EXTERNAL_PRODUCTION:
                    if "screenshot" not in capabilities:
                        raise ExternalBridgeServiceError(
                            "bridge_capability_unavailable"
                        )
                    with self._sessions() as session:
                        self._require_reviewed_identity(
                            session, authenticated_identity, capabilities
                        )
            except (
                ExternalBridgeError,
                ExternalBridgeServiceError,
                SecretStoreError,
            ) as error:
                code = getattr(error, "code", "bridge_authentication_failed")
                self._pairing_failed(target_id, code)
                raise ExternalBridgeServiceError(code) from error
            try:
                secret_reference = self._secrets.write(
                    target_id, "request_authentication", pairing.secret
                )
            except SecretStoreError as error:
                self._pairing_failed(target_id, "bridge_authentication_failed")
                raise ExternalBridgeServiceError(
                    "bridge_authentication_failed"
                ) from error
            try:
                with self._sessions.begin() as session:
                    external = self._require_native_verified(session, target_id)
                    row = session.get(ExternalBridgePairingRow, str(target_id))
                    if (
                        row is None
                        or row.pairing_token_reference != str(snapshot.token_reference)
                        or row.bridge_port != snapshot.bridge_port
                    ):
                        raise ExternalBridgeServiceError("bridge_replay_rejected")
                    row.pairing_state = ExternalBridgePairingState.PAIRING_PENDING
                    row.reachability_state = ExternalBridgeReachabilityState.REACHABLE
                    row.pairing_token_reference = None
                    row.pairing_token_expires_at = None
                    row.bridge_secret_reference = str(secret_reference)
                    self._set_observed(row, authenticated_identity, capabilities)
                    row.endpoint_revision_digest = self._endpoint_digest(
                        external, row.bridge_port
                    )
                    if snapshot.target_type is TargetType.EXTERNAL_PRODUCTION:
                        row.production_bridge_trust_revision = (
                            snapshot.bridge_trust_revision
                        )
            except Exception:
                self._secrets.retire(secret_reference)
                raise
            self._secrets.retire(snapshot.token_reference)
        return self.status(target_id)

    def confirm_pairing(
        self,
        target_id: UUID,
        *,
        administrator_reference: str = "local_administrator",
    ) -> ExternalBridgeStatus:
        now = self._clock()
        administrator = self._administrator(administrator_reference)
        with self._sessions.begin() as session:
            external = self._require_native_verified(session, target_id)
            target = session.get(TargetRow, str(target_id))
            row = session.get(ExternalBridgePairingRow, str(target_id))
            if (
                row is None
                or row.pairing_state is not ExternalBridgePairingState.PAIRING_PENDING
                or row.observed_bridge_id is None
                or row.observed_protocol_version is None
                or row.observed_addon_id is None
                or row.observed_addon_version is None
                or row.observed_kodi_version is None
                or row.observed_build_fingerprint is None
                or row.bridge_secret_reference is None
                or row.bridge_port is None
            ):
                raise ExternalBridgeServiceError("bridge_not_paired")
            assert external.verified_kodi_version is not None
            if not self._kodi_versions_compatible(
                row.observed_kodi_version, external.verified_kodi_version
            ):
                raise ExternalBridgeServiceError("bridge_identity_mismatch")
            reviewed: ReviewedBridgeBuildRow | None = None
            policy: ExternalProductionPolicyRow | None = None
            if (
                target is not None
                and target.target_type is TargetType.EXTERNAL_PRODUCTION
            ):
                policy = session.get(ExternalProductionPolicyRow, str(target_id))
                assert policy is not None
                reviewed = self._require_reviewed_values(
                    session,
                    fingerprint=row.observed_build_fingerprint,
                    addon_id=row.observed_addon_id,
                    addon_version=row.observed_addon_version,
                    protocol_version=row.observed_protocol_version,
                    kodi_version=row.observed_kodi_version,
                    capabilities=self._parse_closed(row.observed_capabilities_json),
                )
                revision = row.production_bridge_trust_revision
                if revision is None:
                    revision = 1
                self._require_http_acknowledgement(
                    session,
                    external,
                    target_id,
                    row.bridge_port,
                    policy.identity_generation,
                    revision,
                )
            row.pinned_bridge_id = row.observed_bridge_id
            row.pinned_protocol_version = row.observed_protocol_version
            row.pinned_addon_id = row.observed_addon_id
            row.pinned_addon_version = row.observed_addon_version
            row.pinned_kodi_version = row.observed_kodi_version
            row.pinned_build_fingerprint = row.observed_build_fingerprint
            row.verified_capabilities_json = row.observed_capabilities_json
            row.pairing_state = ExternalBridgePairingState.PAIRED
            row.reachability_state = ExternalBridgeReachabilityState.REACHABLE
            row.paired_at = self._text(now)
            row.verified_at = self._text(now)
            bridge_port = row.bridge_port
            assert bridge_port is not None
            row.endpoint_revision_digest = self._endpoint_digest(external, bridge_port)
            if reviewed is not None and policy is not None:
                row.verified_capabilities_json = self._closed(("screenshot",))
                row.production_identity_generation = policy.identity_generation
                row.production_bridge_trust_revision = revision
                row.reviewed_build_id = reviewed.id
                row.production_confirmed_by = administrator
                row.production_confirmed_at = self._text(now)
            self._event(
                session,
                target_id,
                "bridge_paired",
                now,
                bridge_id=UUID(row.pinned_bridge_id),
            )
            if reviewed is not None:
                self._event(
                    session,
                    target_id,
                    "production_bridge_confirmed",
                    now,
                    bridge_id=UUID(row.pinned_bridge_id),
                    classification="reviewed_build_confirmed",
                )
        return self.status(target_id)

    def revoke(self, target_id: UUID) -> ExternalBridgeStatus:
        now = self._clock()
        references: list[UUID] = []
        with self._sessions.begin() as session:
            self._require_external(session, target_id)
            row = session.get(ExternalBridgePairingRow, str(target_id))
            if row is None:
                raise ExternalBridgeServiceError("bridge_not_paired")
            if (
                row.pairing_state is ExternalBridgePairingState.REVOKED
                and row.pairing_token_reference is None
                and row.bridge_secret_reference is None
                and row.production_identity_generation is None
                and row.reviewed_build_id is None
            ):
                return self._project(
                    target_id, self._require_external(session, target_id), row
                )
            for value in (
                row.pairing_token_reference,
                row.bridge_secret_reference,
            ):
                if value is not None:
                    references.append(UUID(value))
            bridge_id = (
                None if row.pinned_bridge_id is None else UUID(row.pinned_bridge_id)
            )
            row.pairing_state = ExternalBridgePairingState.REVOKED
            row.reachability_state = ExternalBridgeReachabilityState.UNKNOWN
            row.pairing_token_reference = None
            row.bridge_secret_reference = None
            row.pairing_token_expires_at = None
            row.verified_capabilities_json = "[]"
            row.revoked_at = self._text(now)
            if row.production_bridge_trust_revision is not None:
                row.production_bridge_trust_revision += 1
                row.production_identity_generation = None
                row.reviewed_build_id = None
                row.production_confirmed_by = None
                row.production_confirmed_at = None
            self._event(
                session,
                target_id,
                "bridge_revoked",
                now,
                bridge_id=bridge_id,
            )
        for reference in references:
            self._secrets.retire(reference)
        return self.status(target_id)

    def invalidate_production_native_identity(self, target_id: UUID) -> None:
        """Delete Lab bridge authority after native generation/config drift."""

        references: list[UUID] = []
        now = self._clock()
        with self._sessions.begin() as session:
            target = session.get(TargetRow, str(target_id))
            row = session.get(ExternalBridgePairingRow, str(target_id))
            if (
                target is None
                or target.target_type is not TargetType.EXTERNAL_PRODUCTION
                or row is None
            ):
                return
            if (
                row.pairing_state is ExternalBridgePairingState.REVOKED
                and row.pairing_token_reference is None
                and row.bridge_secret_reference is None
                and row.production_identity_generation is None
                and row.reviewed_build_id is None
            ):
                return
            for value in (
                row.pairing_token_reference,
                row.bridge_secret_reference,
            ):
                if value is not None:
                    references.append(UUID(value))
            row.pairing_state = ExternalBridgePairingState.REVOKED
            row.reachability_state = ExternalBridgeReachabilityState.UNKNOWN
            row.pairing_token_reference = None
            row.pairing_token_expires_at = None
            row.bridge_secret_reference = None
            row.verified_capabilities_json = "[]"
            row.production_bridge_trust_revision = (
                row.production_bridge_trust_revision or 0
            ) + 1
            row.production_identity_generation = None
            row.reviewed_build_id = None
            row.production_confirmed_by = None
            row.production_confirmed_at = None
            row.revoked_at = self._text(now)
        for reference in references:
            self._secrets.retire(reference)

    def mark_needs_reverification(self, target_id: UUID) -> ExternalBridgeStatus:
        now = self._clock()
        with self._sessions.begin() as session:
            self._require_external(session, target_id)
            row = session.get(ExternalBridgePairingRow, str(target_id))
            if row is None or row.pairing_state not in {
                ExternalBridgePairingState.PAIRED,
                ExternalBridgePairingState.NEEDS_REVERIFICATION,
            }:
                raise ExternalBridgeServiceError("bridge_not_paired")
            advance_revision = row.pairing_state is ExternalBridgePairingState.PAIRED
            row.pairing_state = ExternalBridgePairingState.NEEDS_REVERIFICATION
            row.reachability_state = ExternalBridgeReachabilityState.UNKNOWN
            row.verified_capabilities_json = "[]"
            if advance_revision and row.production_bridge_trust_revision is not None:
                row.production_bridge_trust_revision += 1
            self._event(session, target_id, "bridge_reverification_required", now)
        return self.status(target_id)

    def reverify(self, target_id: UUID) -> ExternalBridgeStatus:
        with self._pairing_gate.enter(target_id):
            snapshot = self._active_snapshot(target_id, allow_needs_reverification=True)
            identity, capabilities = self._observe_authenticated(snapshot)
            if (
                snapshot.target_type is TargetType.EXTERNAL_PRODUCTION
                and identity.bridge_id != snapshot.bridge_id
            ):
                self.revoke(target_id)
                raise ExternalBridgeServiceError("bridge_identity_mismatch")
            try:
                self._require_pinned_identity(snapshot, identity)
            except ExternalBridgeServiceError:
                if snapshot.target_type is TargetType.EXTERNAL_PRODUCTION:
                    self._stage_production_reverification(
                        target_id, identity, capabilities
                    )
                    return self.status(target_id)
                self._invalidate_identity(target_id, "bridge_identity_mismatch")
                raise
            if not set(capabilities).intersection(
                {"screenshot", "workspace_deployment"}
            ):
                self._invalidate_identity(target_id, "bridge_capability_unavailable")
                raise ExternalBridgeServiceError("bridge_capability_unavailable")
            if (
                snapshot.target_type is TargetType.EXTERNAL_PRODUCTION
                and "screenshot" not in capabilities
            ):
                self._invalidate_identity(target_id, "bridge_capability_unavailable")
                raise ExternalBridgeServiceError("bridge_capability_unavailable")
            now = self._clock()
            with self._sessions.begin() as session:
                external = self._require_native_verified(session, target_id)
                row = session.get(ExternalBridgePairingRow, str(target_id))
                if row is None or row.pinned_bridge_id != str(identity.bridge_id):
                    raise ExternalBridgeServiceError("bridge_identity_mismatch")
                if snapshot.target_type is TargetType.EXTERNAL_PRODUCTION:
                    reviewed = self._require_reviewed_identity(
                        session,
                        identity,
                        capabilities,
                        allow_capability_expansion=True,
                    )
                    policy = session.get(ExternalProductionPolicyRow, str(target_id))
                    assert policy is not None
                    assert row.bridge_port is not None
                    assert row.production_bridge_trust_revision is not None
                    self._require_http_acknowledgement(
                        session,
                        external,
                        target_id,
                        row.bridge_port,
                        policy.identity_generation,
                        row.production_bridge_trust_revision,
                    )
                    row.reviewed_build_id = reviewed.id
                    row.production_identity_generation = policy.identity_generation
                    row.production_confirmed_by = "local_administrator"
                    row.production_confirmed_at = self._text(now)
                row.pairing_state = ExternalBridgePairingState.PAIRED
                row.reachability_state = ExternalBridgeReachabilityState.REACHABLE
                row.verified_capabilities_json = self._closed(
                    ("screenshot",)
                    if snapshot.target_type is TargetType.EXTERNAL_PRODUCTION
                    else capabilities
                )
                bridge_port = row.bridge_port
                assert bridge_port is not None
                row.endpoint_revision_digest = self._endpoint_digest(
                    external, bridge_port
                )
                row.verified_at = self._text(now)
                self._event(
                    session,
                    target_id,
                    "bridge_reverified",
                    now,
                    bridge_id=identity.bridge_id,
                )
        return self.status(target_id)

    def screenshot_available(self, target_id: UUID) -> bool:
        try:
            self._active_snapshot(target_id, required_capability="screenshot")
            return True
        except ExternalBridgeServiceError:
            return False

    def deployment_available(self, target_id: UUID) -> bool:
        with self._sessions() as session:
            target = session.get(TargetRow, str(target_id))
            if (
                target is not None
                and target.target_type is TargetType.EXTERNAL_PRODUCTION
            ):
                return False
        try:
            self._active_snapshot(target_id, required_capability="workspace_deployment")
            return True
        except ExternalBridgeServiceError:
            return False

    def authenticate_production_identity(self, target_id: UUID) -> None:
        """Fresh fixed read-only bridge identity proof; never a command channel."""

        snapshot = self._active_snapshot(
            target_id,
            allow_transient_unreachable=True,
            require_production_http_acknowledgement=False,
        )
        if snapshot.target_type is not TargetType.EXTERNAL_PRODUCTION:
            raise ExternalBridgeServiceError("bridge_not_paired")
        try:
            identity = self._client.identity(
                snapshot.endpoint,
                bridge_port=snapshot.bridge_port,
                target_id=target_id,
                bridge_id=snapshot.bridge_id,
                secret=self._read_secret(snapshot),
            )
        except ExternalBridgeError as error:
            self._publish_reachability(target_id, error.code)
            raise ExternalBridgeServiceError(error.code) from error
        try:
            self._require_pinned_identity(snapshot, identity)
        except ExternalBridgeServiceError:
            self._invalidate_identity(target_id, "bridge_identity_mismatch")
            raise
        self._publish_authenticated_reachable(snapshot)

    def inspect_addon(self, target_id: UUID, addon_id: str) -> BridgeAddonTree:
        with self._deployment_gate.enter(target_id):
            snapshot = self._active_snapshot(
                target_id, required_capability="workspace_deployment"
            )
            identity, capabilities = self._observe_authenticated(snapshot)
            self._require_pinned_identity(snapshot, identity)
            if "workspace_deployment" not in capabilities:
                raise ExternalBridgeServiceError("bridge_capability_unavailable")
            try:
                return self._client.inspect_addon(
                    snapshot.endpoint,
                    bridge_port=snapshot.bridge_port,
                    target_id=target_id,
                    bridge_id=snapshot.bridge_id,
                    secret=self._read_secret(snapshot),
                    addon_id=addon_id,
                )
            except ExternalBridgeError as error:
                self._publish_reachability(target_id, error.code)
                raise ExternalBridgeServiceError(error.code) from error

    def deploy_workspace(
        self,
        target_id: UUID,
        *,
        operation_id: UUID,
        request_id: UUID,
        addon_id: str,
        addon_version: str,
        generation: int,
        manifest: DevelopmentTreeManifest,
        stage_root: Path,
        before_commit: Callable[[], None] | None = None,
    ) -> BridgeDeploymentReceipt:
        started = time.monotonic()
        transferred = 0

        def require_within_duration() -> None:
            if time.monotonic() - started > MAX_DEPLOYMENT_DURATION_SECONDS:
                raise ExternalBridgeServiceError("transfer_invalid")

        with self._deployment_gate.enter(target_id):
            snapshot = self._active_snapshot(
                target_id, required_capability="workspace_deployment"
            )
            identity, capabilities = self._observe_authenticated(snapshot)
            self._require_pinned_identity(snapshot, identity)
            if "workspace_deployment" not in capabilities:
                raise ExternalBridgeServiceError("bridge_capability_unavailable")
            secret = self._read_secret(snapshot)
            entries = [entry.canonical() for entry in manifest.entries]
            file_count = sum(entry.entry_type == "file" for entry in manifest.entries)
            try:

                def begin() -> BridgeDeploymentReceipt:
                    require_within_duration()
                    return self._client.deployment_begin(
                        snapshot.endpoint,
                        bridge_port=snapshot.bridge_port,
                        target_id=target_id,
                        bridge_id=snapshot.bridge_id,
                        secret=secret,
                        deployment_id=operation_id,
                        request_id=request_id,
                        addon_id=addon_id,
                        addon_version=addon_version,
                        generation=generation,
                        manifest_digest=manifest.digest,
                        entries=entries,
                        file_count=file_count,
                        total_bytes=manifest.included_bytes,
                    )

                try:
                    receipt = begin()
                except ExternalBridgeError:
                    receipt = begin()
                if receipt.state in {"succeeded", "failed", "ambiguous"}:
                    return receipt
                for entry in manifest.entries:
                    if entry.entry_type != "file" or entry.sha256 is None:
                        continue
                    path = stage_root.joinpath(*entry.path.split("/"))
                    offset = 0
                    with path.open("rb") as stream:
                        while chunk := stream.read(DEPLOYMENT_CHUNK_BYTES):
                            transferred += len(chunk)
                            expected_elapsed = (
                                transferred / MAX_DEPLOYMENT_TRANSFER_BYTES_PER_SECOND
                            )
                            remaining = expected_elapsed - (time.monotonic() - started)
                            if remaining > 0:
                                time.sleep(remaining)
                            require_within_duration()
                            receipt = self._transfer_chunk_with_retry(
                                snapshot,
                                secret,
                                operation_id,
                                entry.path,
                                entry.sha256,
                                offset,
                                chunk,
                            )
                            offset += len(chunk)
                if before_commit is not None:
                    require_within_duration()
                    before_commit()
                require_within_duration()
                return self._commit_or_reconcile(
                    snapshot, secret, operation_id, manifest.digest
                )
            except ExternalBridgeServiceError:
                raise
            except (ExternalBridgeError, OSError) as error:
                code = getattr(error, "code", "transfer_invalid")
                self._publish_reachability(target_id, code)
                raise ExternalBridgeServiceError(code) from error

    def _transfer_chunk_with_retry(
        self,
        snapshot: _Snapshot,
        secret: bytes,
        operation_id: UUID,
        path: str,
        file_digest: str,
        offset: int,
        content: bytes,
    ) -> BridgeDeploymentReceipt:
        def transfer() -> BridgeDeploymentReceipt:
            return self._client.deployment_chunk(
                snapshot.endpoint,
                bridge_port=snapshot.bridge_port,
                target_id=snapshot.target_id,
                bridge_id=snapshot.bridge_id,
                secret=secret,
                deployment_id=operation_id,
                path=path,
                file_digest=file_digest,
                offset=offset,
                content=content,
            )

        try:
            return transfer()
        except ExternalBridgeError:
            return transfer()

    def deployment_receipt(
        self, target_id: UUID, operation_id: UUID
    ) -> BridgeDeploymentReceipt:
        with self._deployment_gate.enter(target_id):
            snapshot = self._active_snapshot(
                target_id, required_capability="workspace_deployment"
            )
            try:
                receipt = self._client.deployment_receipt(
                    snapshot.endpoint,
                    bridge_port=snapshot.bridge_port,
                    target_id=target_id,
                    bridge_id=snapshot.bridge_id,
                    secret=self._read_secret(snapshot),
                    deployment_id=operation_id,
                )
                return receipt
            except ExternalBridgeError as error:
                self._publish_reachability(target_id, error.code)
                raise ExternalBridgeServiceError(error.code) from error

    def _commit_or_reconcile(
        self,
        snapshot: _Snapshot,
        secret: bytes,
        operation_id: UUID,
        manifest_digest: str,
    ) -> BridgeDeploymentReceipt:
        try:
            return self._client.deployment_commit(
                snapshot.endpoint,
                bridge_port=snapshot.bridge_port,
                target_id=snapshot.target_id,
                bridge_id=snapshot.bridge_id,
                secret=secret,
                deployment_id=operation_id,
                manifest_digest=manifest_digest,
            )
        except ExternalBridgeError:
            try:
                receipt = self._client.deployment_receipt(
                    snapshot.endpoint,
                    bridge_port=snapshot.bridge_port,
                    target_id=snapshot.target_id,
                    bridge_id=snapshot.bridge_id,
                    secret=secret,
                    deployment_id=operation_id,
                )
                if receipt.state not in {"succeeded", "failed", "ambiguous"}:
                    raise ExternalBridgeServiceError("deployment_ambiguous")
                return receipt
            except ExternalBridgeError as error:
                raise ExternalBridgeServiceError("deployment_ambiguous") from error

    def capture_screenshot(
        self, target_id: UUID, *, request_id: UUID | None = None
    ) -> ExternalScreenshotCapture:
        with self._sessions() as session:
            target = session.get(TargetRow, str(target_id))
        production = bool(
            target is not None and target.target_type is TargetType.EXTERNAL_PRODUCTION
        )
        if production and request_id is None:
            raise ExternalBridgeServiceError("request_id_required")
        request_id = request_id or uuid4()
        if production:
            # Resolve exact replay before any new-dispatch admission.
            replay = self._production_cached_replay(target_id, request_id)
            if replay is not None:
                self._active_snapshot(target_id, required_capability="screenshot")
                return replay
        gate = self._production_screenshot_gate if production else self._screenshot_gate
        with gate.enter(target_id):
            snapshot = self._active_snapshot(
                target_id, required_capability="screenshot"
            )
            if production and snapshot.bridge_trust_revision is not None:
                bind_screenshot_bridge_revision(snapshot.bridge_trust_revision)
            existing = self._bridge_receipt(target_id, request_id)
            if existing is not None and existing.state == "completed":
                raise ExternalBridgeServiceError("screenshot_replay_unavailable")
            if existing is not None and existing.state != "pending":
                raise ExternalBridgeServiceError("screenshot_replay_unavailable")
            requested_at = (
                self._clock()
                if existing is None
                else self._datetime(existing.created_at)
            )
            self._audit_screenshot(
                target_id,
                snapshot.bridge_id,
                request_id,
                "screenshot_requested",
                requested_at,
            )
            try:
                identity, capabilities = self._observe_authenticated(snapshot)
            except ExternalBridgeServiceError as error:
                self._fail_receipt(target_id, request_id, error.code, ambiguous=False)
                raise
            try:
                self._require_pinned_identity(snapshot, identity)
            except ExternalBridgeServiceError as error:
                if (
                    snapshot.target_type is TargetType.EXTERNAL_PRODUCTION
                    and identity.bridge_id != snapshot.bridge_id
                ):
                    self.invalidate_production_native_identity(target_id)
                else:
                    self._invalidate_identity(target_id, "bridge_identity_mismatch")
                self._fail_receipt(target_id, request_id, error.code, ambiguous=False)
                raise
            if snapshot.target_type is TargetType.EXTERNAL_PRODUCTION:
                self._record_production_capability_observation(
                    target_id, identity, capabilities
                )
            if "screenshot" not in capabilities:
                self._invalidate_identity(target_id, "bridge_capability_unavailable")
                self._fail_receipt(
                    target_id,
                    request_id,
                    "bridge_capability_unavailable",
                    ambiguous=False,
                )
                raise ExternalBridgeServiceError("bridge_capability_unavailable")
            nonce = b64url(os.urandom(32)) if existing is None else existing.nonce
            if existing is None:
                try:
                    self._claim_receipt(target_id, request_id, nonce, requested_at)
                    emit_screenshot_stage(
                        "receipt_persistence",
                        "entered",
                        "bridge_receipt_pending",
                        receipt_kind="bridge",
                        receipt_state="pending",
                    )
                except ExternalBridgeServiceError as error:
                    self._fail_receipt(
                        target_id, request_id, error.code, ambiguous=False
                    )
                    raise
            try:
                result = self._client.capture_screenshot(
                    snapshot.endpoint,
                    bridge_port=snapshot.bridge_port,
                    target_id=target_id,
                    bridge_id=snapshot.bridge_id,
                    secret=self._read_secret(snapshot),
                    request_id=request_id,
                    nonce=nonce,
                    requested_at=requested_at,
                )
            except ExternalBridgeError as first:
                if first.code == "bridge_unreachable" and not production:
                    try:
                        result = self._client.capture_screenshot(
                            snapshot.endpoint,
                            bridge_port=snapshot.bridge_port,
                            target_id=target_id,
                            bridge_id=snapshot.bridge_id,
                            secret=self._read_secret(snapshot),
                            request_id=request_id,
                            nonce=nonce,
                            requested_at=requested_at,
                        )
                    except ExternalBridgeError as second:
                        self._publish_screenshot_failure_reachability(
                            target_id, second.code
                        )
                        self._fail_receipt(
                            target_id,
                            request_id,
                            second.code,
                            ambiguous=second.code
                            in {"bridge_unreachable", "bridge_replay_rejected"},
                        )
                        raise ExternalBridgeServiceError(second.code) from second
                else:
                    self._publish_screenshot_failure_reachability(target_id, first.code)
                    self._fail_receipt(
                        target_id,
                        request_id,
                        first.code,
                        ambiguous=first.code
                        in {"bridge_unreachable", "bridge_replay_rejected"},
                    )
                    raise ExternalBridgeServiceError(first.code) from first
            self._complete_receipt(target_id, result)
            emit_screenshot_stage(
                "receipt_persistence",
                "passed",
                "bridge_receipt_completed",
                receipt_kind="bridge",
                receipt_state="completed",
            )
            capture = ExternalScreenshotCapture(
                capture_id=request_id,
                target_id=target_id,
                bridge_id=snapshot.bridge_id,
                captured_at=result.captured_at,
                width=result.width,
                height=result.height,
                byte_size=len(result.png),
                digest=result.digest,
                png=result.png,
            )
            if production:
                with self._production_pixel_cache_lock:
                    if len(self._production_pixel_cache) >= 4:
                        # Only completed response bytes are displaced; durable
                        # replay identity remains and therefore cannot recapture.
                        oldest = next(iter(self._production_pixel_cache))
                        del self._production_pixel_cache[oldest]
                    self._production_pixel_cache[(target_id, request_id)] = capture
                reachability = self._screenshot_reachability_value(target_id)
                emit_screenshot_stage(
                    "reachability_classification",
                    "passed",
                    "screenshot_success_reachable",
                    reachability_before=reachability,
                    reachability_after=reachability,
                    classification_source="authenticated_screenshot_success",
                )
            return capture

    def _bridge_receipt(
        self, target_id: UUID, request_id: UUID
    ) -> ExternalBridgeRequestReceiptRow | None:
        with self._sessions() as session:
            row = session.get(
                ExternalBridgeRequestReceiptRow,
                {"target_id": str(target_id), "request_id": str(request_id)},
            )
            if row is None:
                return None
            session.expunge(row)
            return row

    def _production_cached_replay(
        self, target_id: UUID, request_id: UUID
    ) -> ExternalScreenshotCapture | None:
        with self._production_pixel_cache_lock:
            cached = self._production_pixel_cache.get((target_id, request_id))
            if (
                cached is not None
                and self._clock() - cached.captured_at > PRODUCTION_PIXEL_CACHE_TTL
            ):
                del self._production_pixel_cache[(target_id, request_id)]
                cached = None
        if cached is not None:
            return cached
        receipt = self._bridge_receipt(target_id, request_id)
        if receipt is not None and receipt.state == "completed":
            raise ExternalBridgeServiceError("screenshot_replay_unavailable")
        return None

    def discard_production_pixels(self, target_id: UUID, request_id: UUID) -> None:
        """Drop only volatile response bytes; durable replay identity remains."""

        with self._production_pixel_cache_lock:
            self._production_pixel_cache.pop((target_id, request_id), None)

    def _pairing_snapshot(self, target_id: UUID) -> _PairingSnapshot:
        expired_reference: UUID | None = None
        snapshot: _PairingSnapshot | None = None
        with self._sessions() as session:
            external = self._require_native_verified(session, target_id)
            target = session.get(TargetRow, str(target_id))
            policy = session.get(ExternalProductionPolicyRow, str(target_id))
            row = session.get(ExternalBridgePairingRow, str(target_id))
            if (
                row is None
                or row.bridge_port is None
                or row.pairing_token_reference is None
                or row.pairing_token_expires_at is None
            ):
                raise ExternalBridgeServiceError("bridge_not_paired")
            expiry = self._datetime(row.pairing_token_expires_at)
            if self._clock() >= expiry:
                expired_reference = UUID(row.pairing_token_reference)
            else:
                endpoint = self._validate_bridge_endpoint(external, row.bridge_port)
                if external.verified_kodi_version is None:
                    raise ExternalBridgeServiceError("bridge_identity_mismatch")
                snapshot = _PairingSnapshot(
                    endpoint=endpoint,
                    bridge_port=row.bridge_port,
                    token_reference=UUID(row.pairing_token_reference),
                    kodi_version=external.verified_kodi_version,
                    target_type=(
                        TargetType.EXTERNAL_DEVELOPMENT
                        if target is None
                        else target.target_type
                    ),
                    identity_generation=(
                        None if policy is None else policy.identity_generation
                    ),
                    bridge_trust_revision=(
                        None
                        if target is None
                        or target.target_type is TargetType.EXTERNAL_DEVELOPMENT
                        else self._prospective_production_revision(row)
                    ),
                )
        if expired_reference is not None:
            with self._sessions.begin() as session:
                row = session.get(ExternalBridgePairingRow, str(target_id))
                if row is not None and row.pairing_token_reference == str(
                    expired_reference
                ):
                    row.pairing_token_reference = None
                    row.pairing_token_expires_at = None
            self._secrets.retire(expired_reference)
            raise ExternalBridgeServiceError("bridge_authentication_failed")
        assert snapshot is not None
        return snapshot

    def _active_snapshot(
        self,
        target_id: UUID,
        *,
        allow_needs_reverification: bool = False,
        allow_transient_unreachable: bool = False,
        required_capability: str | None = None,
        require_production_http_acknowledgement: bool = True,
    ) -> _Snapshot:
        with self._sessions() as session:
            external = self._require_native_verified(session, target_id)
            target = session.get(TargetRow, str(target_id))
            policy = session.get(ExternalProductionPolicyRow, str(target_id))
            if (
                required_capability is not None
                and required_capability
                not in self._parse_closed(external.configured_capabilities_json)
            ):
                raise ExternalBridgeServiceError("bridge_capability_unavailable")
            row = session.get(ExternalBridgePairingRow, str(target_id))
            allowed = {ExternalBridgePairingState.PAIRED}
            if allow_needs_reverification:
                allowed.add(ExternalBridgePairingState.NEEDS_REVERIFICATION)
            if (
                row is None
                or row.pairing_state not in allowed
                or row.bridge_port is None
                or row.pinned_bridge_id is None
                or row.bridge_secret_reference is None
                or row.pinned_kodi_version is None
                or row.pinned_build_fingerprint is None
                or (
                    not allow_needs_reverification
                    and row.reachability_state
                    is not ExternalBridgeReachabilityState.REACHABLE
                    and not (
                        allow_transient_unreachable
                        and row.reachability_state
                        is ExternalBridgeReachabilityState.UNREACHABLE
                    )
                )
                or (
                    not allow_needs_reverification
                    and required_capability is not None
                    and required_capability
                    not in self._parse_closed(row.verified_capabilities_json)
                )
            ):
                raise ExternalBridgeServiceError("bridge_not_paired")
            endpoint = self._validate_bridge_endpoint(external, row.bridge_port)
            digest = self._endpoint_digest(external, row.bridge_port)
            if row.endpoint_revision_digest != digest:
                raise ExternalBridgeServiceError("bridge_identity_mismatch")
            assert external.verified_kodi_version is not None
            if not self._kodi_versions_compatible(
                row.pinned_kodi_version, external.verified_kodi_version
            ):
                raise ExternalBridgeServiceError("bridge_identity_mismatch")
            if (
                target is not None
                and target.target_type is TargetType.EXTERNAL_PRODUCTION
            ):
                if policy is None or row.production_bridge_trust_revision is None:
                    raise ExternalBridgeServiceError("bridge_reverification_required")
                if not allow_needs_reverification:
                    if (
                        row.production_identity_generation != policy.identity_generation
                        or row.reviewed_build_id is None
                        or row.production_confirmed_at is None
                    ):
                        raise ExternalBridgeServiceError(
                            "bridge_reverification_required"
                        )
                    reviewed = session.get(
                        ReviewedBridgeBuildRow, row.reviewed_build_id
                    )
                    if reviewed is None or reviewed.state != "active":
                        raise ExternalBridgeServiceError("bridge_build_not_reviewed")
                if require_production_http_acknowledgement:
                    self._require_http_acknowledgement(
                        session,
                        external,
                        target_id,
                        row.bridge_port,
                        policy.identity_generation,
                        row.production_bridge_trust_revision,
                    )
            return _Snapshot(
                target_id=target_id,
                endpoint=endpoint,
                bridge_port=row.bridge_port,
                bridge_id=UUID(row.pinned_bridge_id),
                secret_reference=UUID(row.bridge_secret_reference),
                kodi_version=row.pinned_kodi_version,
                build_fingerprint=row.pinned_build_fingerprint,
                endpoint_digest=digest,
                addon_id=row.pinned_addon_id or "",
                addon_version=row.pinned_addon_version or "",
                protocol_version=row.pinned_protocol_version or "",
                target_type=(
                    TargetType.EXTERNAL_DEVELOPMENT
                    if target is None
                    else target.target_type
                ),
                identity_generation=(
                    None if policy is None else policy.identity_generation
                ),
                bridge_trust_revision=row.production_bridge_trust_revision,
            )

    def _observe_authenticated(
        self, snapshot: _Snapshot
    ) -> tuple[BridgeIdentity, tuple[str, ...]]:
        secret = self._read_secret(snapshot)
        try:
            identity = self._client.identity(
                snapshot.endpoint,
                bridge_port=snapshot.bridge_port,
                target_id=snapshot.target_id,
                bridge_id=snapshot.bridge_id,
                secret=secret,
            )
            capabilities = self._client.capabilities(
                snapshot.endpoint,
                bridge_port=snapshot.bridge_port,
                target_id=snapshot.target_id,
                bridge_id=snapshot.bridge_id,
                secret=secret,
            )
        except ExternalBridgeError as error:
            self._publish_reachability(snapshot.target_id, error.code)
            raise ExternalBridgeServiceError(error.code) from error
        return identity, capabilities

    def _read_secret(self, snapshot: _Snapshot) -> bytes:
        try:
            return self._secrets.read(
                snapshot.target_id,
                snapshot.secret_reference,
                "request_authentication",
            )
        except SecretStoreError as error:
            raise ExternalBridgeServiceError("bridge_authentication_failed") from error

    @staticmethod
    def _require_native_identity(identity: BridgeIdentity, kodi_version: str) -> None:
        if not ExternalBridgeService._kodi_versions_compatible(
            identity.kodi_version, kodi_version
        ):
            raise ExternalBridgeServiceError("bridge_identity_mismatch")
        if identity.protocol_version != BRIDGE_PROTOCOL_VERSION:
            raise ExternalBridgeServiceError("bridge_protocol_unsupported")
        if identity.addon_id != BRIDGE_ADDON_ID:
            raise ExternalBridgeServiceError("bridge_identity_mismatch")

    @staticmethod
    def _require_pinned_identity(snapshot: _Snapshot, identity: BridgeIdentity) -> None:
        if (
            identity.bridge_id != snapshot.bridge_id
            or identity.kodi_version != snapshot.kodi_version
            or identity.build_fingerprint != snapshot.build_fingerprint
            or identity.protocol_version != snapshot.protocol_version
            or (
                snapshot.target_type is TargetType.EXTERNAL_PRODUCTION
                and (
                    identity.addon_id != snapshot.addon_id
                    or identity.addon_version != snapshot.addon_version
                )
            )
        ):
            raise ExternalBridgeServiceError("bridge_identity_mismatch")

    @staticmethod
    def _kodi_versions_compatible(bridge_version: str, native_version: str) -> bool:
        return bridge_version == native_version or (
            bridge_version == "22.0" and native_version.startswith("22.0-")
        )

    def _claim_receipt(
        self, target_id: UUID, request_id: UUID, nonce: str, now: datetime
    ) -> None:
        expiry = now + BRIDGE_RECEIPT_TTL
        with self._sessions.begin() as session:
            session.execute(
                delete(ExternalBridgeRequestReceiptRow).where(
                    ExternalBridgeRequestReceiptRow.expires_at < self._text(now),
                    ExternalBridgeRequestReceiptRow.state.in_(
                        ("completed", "ambiguous", "rejected")
                    ),
                )
            )
            target_count = session.scalar(
                select(func.count())
                .select_from(ExternalBridgeRequestReceiptRow)
                .where(ExternalBridgeRequestReceiptRow.target_id == str(target_id))
            )
            global_count = session.scalar(
                select(func.count()).select_from(ExternalBridgeRequestReceiptRow)
            )
            if (
                target_count is None
                or global_count is None
                or target_count >= MAX_RECEIPTS_PER_TARGET
                or global_count >= MAX_RECEIPTS_GLOBAL
            ):
                raise ExternalBridgeServiceError("bridge_replay_rejected")
            session.add(
                ExternalBridgeRequestReceiptRow(
                    target_id=str(target_id),
                    request_id=str(request_id),
                    nonce=nonce,
                    operation="capture_screenshot",
                    state="pending",
                    created_at=self._text(now),
                    expires_at=self._text(expiry),
                )
            )

    def _complete_receipt(
        self, target_id: UUID, result: BridgeScreenshotResult
    ) -> None:
        now = self._clock()
        with self._sessions.begin() as session:
            row = session.get(
                ExternalBridgeRequestReceiptRow,
                {"target_id": str(target_id), "request_id": str(result.request_id)},
            )
            if row is None or row.state != "pending":
                raise ExternalBridgeServiceError("bridge_replay_rejected")
            row.state = "completed"
            row.response_digest = result.digest
            row.byte_size = len(result.png)
            row.width = result.width
            row.height = result.height
            row.completed_at = self._text(now)
            self._event(
                session,
                target_id,
                "screenshot_completed",
                now,
                bridge_id=result.bridge_id,
                request_id=result.request_id,
                byte_size=len(result.png),
                width=result.width,
                height=result.height,
                content_digest=result.digest,
            )

    def _fail_receipt(
        self, target_id: UUID, request_id: UUID, code: str, *, ambiguous: bool
    ) -> None:
        now = self._clock()
        receipt_state = "absent"
        with self._sessions.begin() as session:
            row = session.get(
                ExternalBridgeRequestReceiptRow,
                {"target_id": str(target_id), "request_id": str(request_id)},
            )
            if row is not None and row.state == "pending":
                row.state = "ambiguous" if ambiguous else "rejected"
                row.completed_at = self._text(now)
                receipt_state = row.state
            self._event(
                session,
                target_id,
                "screenshot_failed",
                now,
                request_id=request_id,
                classification=code,
            )
        if receipt_state != "absent":
            emit_screenshot_stage(
                "receipt_persistence",
                "passed",
                "bridge_receipt_failed",
                receipt_kind="bridge",
                receipt_state=receipt_state,
            )

    def _audit_screenshot(
        self,
        target_id: UUID,
        bridge_id: UUID,
        request_id: UUID,
        kind: str,
        now: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            self._event(
                session,
                target_id,
                kind,
                now,
                bridge_id=bridge_id,
                request_id=request_id,
            )

    def _pairing_failed(self, target_id: UUID, code: str) -> None:
        now = self._clock()
        token_reference: UUID | None = None
        with self._sessions.begin() as session:
            row = session.get(ExternalBridgePairingRow, str(target_id))
            target = session.get(TargetRow, str(target_id))
            if row is not None:
                if (
                    target is not None
                    and target.target_type is TargetType.EXTERNAL_PRODUCTION
                    and row.pairing_token_reference is not None
                ):
                    token_reference = UUID(row.pairing_token_reference)
                    row.pairing_token_reference = None
                    row.pairing_token_expires_at = None
                row.reachability_state = self._reachability(code)
                self._event(
                    session,
                    target_id,
                    "pairing_failed",
                    now,
                    classification=code,
                )
        if token_reference is not None:
            self._secrets.retire(token_reference)

    def _publish_reachability(self, target_id: UUID, code: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(ExternalBridgePairingRow, str(target_id))
            if row is not None:
                row.reachability_state = self._reachability(code)

    def _publish_screenshot_failure_reachability(
        self, target_id: UUID, code: str
    ) -> None:
        before = self._screenshot_reachability_value(target_id)
        if code not in SCREENSHOT_ERROR_CODES:
            self._publish_reachability(target_id, code)
            source = "transport_or_trust_failure"
        else:
            source = "semantic_screenshot_failure"
        after = self._screenshot_reachability_value(target_id)
        emit_screenshot_stage(
            "reachability_classification",
            "passed",
            code,
            reachability_before=before,
            reachability_after=after,
            classification_source=source,
        )

    def _screenshot_reachability_value(self, target_id: UUID) -> str:
        with self._sessions() as session:
            row = session.get(ExternalBridgePairingRow, str(target_id))
            return "absent" if row is None else row.reachability_state.value

    def _publish_authenticated_reachable(self, snapshot: _Snapshot) -> None:
        """Restore reachability only for the same still-paired trusted identity."""

        with self._sessions.begin() as session:
            row = session.get(ExternalBridgePairingRow, str(snapshot.target_id))
            if (
                row is None
                or row.pairing_state is not ExternalBridgePairingState.PAIRED
                or row.bridge_port != snapshot.bridge_port
                or row.pinned_bridge_id != str(snapshot.bridge_id)
                or row.bridge_secret_reference != str(snapshot.secret_reference)
                or row.endpoint_revision_digest != snapshot.endpoint_digest
                or row.production_identity_generation != snapshot.identity_generation
                or row.production_bridge_trust_revision
                != snapshot.bridge_trust_revision
            ):
                raise ExternalBridgeServiceError("bridge_reverification_required")
            row.reachability_state = ExternalBridgeReachabilityState.REACHABLE

    def _invalidate_identity(self, target_id: UUID, classification: str) -> None:
        now = self._clock()
        with self._sessions.begin() as session:
            row = session.get(ExternalBridgePairingRow, str(target_id))
            if row is not None:
                advance_revision = (
                    row.pairing_state is ExternalBridgePairingState.PAIRED
                )
                row.pairing_state = ExternalBridgePairingState.NEEDS_REVERIFICATION
                row.reachability_state = (
                    ExternalBridgeReachabilityState.IDENTITY_MISMATCH
                )
                row.verified_capabilities_json = "[]"
                if (
                    advance_revision
                    and row.production_bridge_trust_revision is not None
                ):
                    row.production_bridge_trust_revision += 1
                self._event(
                    session,
                    target_id,
                    "bridge_reverification_required",
                    now,
                    classification=classification,
                )

    @staticmethod
    def _reachability(code: str) -> ExternalBridgeReachabilityState:
        if code == "bridge_authentication_failed":
            return ExternalBridgeReachabilityState.AUTHENTICATION_FAILED
        if code == "bridge_identity_mismatch":
            return ExternalBridgeReachabilityState.IDENTITY_MISMATCH
        if code == "bridge_protocol_unsupported":
            return ExternalBridgeReachabilityState.PROTOCOL_UNSUPPORTED
        return ExternalBridgeReachabilityState.UNREACHABLE

    @staticmethod
    def _require_external(session: Session, target_id: UUID) -> ExternalKodiTargetRow:
        target = session.get(TargetRow, str(target_id))
        external = session.get(ExternalKodiTargetRow, str(target_id))
        if (
            target is None
            or external is None
            or target.target_type
            not in {TargetType.EXTERNAL_DEVELOPMENT, TargetType.EXTERNAL_PRODUCTION}
            or external.administrative_state is ExternalAdministrativeState.DEACTIVATED
        ):
            raise ExternalBridgeServiceError("target_not_found")
        return external

    @classmethod
    def _require_native_verified(
        cls, session: Session, target_id: UUID
    ) -> ExternalKodiTargetRow:
        external = cls._require_external(session, target_id)
        target = session.get(TargetRow, str(target_id))
        if (
            external.administrative_state is not ExternalAdministrativeState.ENABLED
            or external.verification_state is not ExternalVerificationState.VERIFIED
            or external.verified_kodi_version is None
            or cls._parse_closed(external.verified_addresses_json) != {external.host}
        ):
            raise ExternalBridgeServiceError("needs_reverification")
        configured = cls._parse_closed(external.configured_capabilities_json)
        if not configured.intersection({"screenshot", "workspace_deployment"}):
            raise ExternalBridgeServiceError("bridge_capability_unavailable")
        if target is not None and target.target_type is TargetType.EXTERNAL_PRODUCTION:
            try:
                evaluate_production_inspection_state(
                    session,
                    target_id,
                    required_capabilities=frozenset({"target_inspect"}),
                )
            except ProductionInspectionError as error:
                raise ExternalBridgeServiceError(error.code) from error
            if "screenshot" not in configured:
                raise ExternalBridgeServiceError("bridge_capability_unavailable")
        return external

    def _validate_bridge_endpoint(
        self, external: ExternalKodiTargetRow, bridge_port: int
    ) -> ValidatedExternalEndpoint:
        try:
            return self._endpoint_policy.validate(
                external.protocol_kind, external.host, bridge_port
            )
        except ExternalEndpointError as error:
            raise ExternalBridgeServiceError("bridge_unreachable") from error

    @staticmethod
    def _endpoint_digest(external: ExternalKodiTargetRow, bridge_port: int) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "bridge_port": bridge_port,
                    "host": external.host,
                    "native_port": external.port,
                    "protocol": external.protocol_kind.value,
                    "verified_addresses": external.verified_addresses_json,
                    "verified_kodi_name": external.verified_kodi_name,
                    "verified_kodi_version": external.verified_kodi_version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    @staticmethod
    def _set_observed(
        row: ExternalBridgePairingRow,
        identity: BridgeIdentity,
        capabilities: tuple[str, ...],
    ) -> None:
        row.observed_bridge_id = str(identity.bridge_id)
        row.observed_protocol_version = identity.protocol_version
        row.observed_addon_id = identity.addon_id
        row.observed_addon_version = identity.addon_version
        row.observed_kodi_version = identity.kodi_version
        row.observed_build_fingerprint = identity.build_fingerprint
        row.observed_capabilities_json = ExternalBridgeService._closed(capabilities)

    @staticmethod
    def _clear_identity(row: ExternalBridgePairingRow) -> None:
        row.observed_bridge_id = None
        row.observed_protocol_version = None
        row.observed_addon_id = None
        row.observed_addon_version = None
        row.observed_kodi_version = None
        row.observed_build_fingerprint = None
        row.observed_capabilities_json = "[]"
        row.pinned_bridge_id = None
        row.pinned_protocol_version = None
        row.pinned_addon_id = None
        row.pinned_addon_version = None
        row.pinned_kodi_version = None
        row.pinned_build_fingerprint = None
        row.verified_capabilities_json = "[]"
        row.endpoint_revision_digest = None
        row.paired_at = None
        row.verified_at = None

    @staticmethod
    def _closed(values: tuple[str, ...]) -> str:
        return json.dumps(sorted(values), separators=(",", ":"))

    @staticmethod
    def _parse_closed(value: str) -> set[str]:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ExternalBridgeServiceError("needs_reverification") from error
        if (
            not isinstance(parsed, list)
            or any(not isinstance(item, str) for item in parsed)
            or len(parsed) != len(set(parsed))
        ):
            raise ExternalBridgeServiceError("needs_reverification")
        return set(parsed)

    @staticmethod
    def _text(value: datetime) -> str:
        return (
            value.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _datetime(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @classmethod
    def _project(
        cls,
        target_id: UUID,
        external: ExternalKodiTargetRow,
        row: ExternalBridgePairingRow,
    ) -> ExternalBridgeStatus:
        return ExternalBridgeStatus(
            target_id=target_id,
            bridge_port=row.bridge_port,
            pairing_state=row.pairing_state,
            reachability_state=row.reachability_state,
            pairing_token_pending=row.pairing_token_reference is not None,
            pairing_token_expires_at=(
                None
                if row.pairing_token_expires_at is None
                else cls._datetime(row.pairing_token_expires_at)
            ),
            observed_bridge_id=(
                None if row.observed_bridge_id is None else UUID(row.observed_bridge_id)
            ),
            observed_protocol_version=row.observed_protocol_version,
            observed_addon_id=row.observed_addon_id,
            observed_addon_version=row.observed_addon_version,
            observed_kodi_version=row.observed_kodi_version,
            observed_build_fingerprint=row.observed_build_fingerprint,
            observed_capabilities=tuple(
                sorted(cls._parse_closed(row.observed_capabilities_json))
            ),
            pinned_bridge_id=(
                None if row.pinned_bridge_id is None else UUID(row.pinned_bridge_id)
            ),
            protocol_version=row.pinned_protocol_version
            or row.observed_protocol_version,
            addon_id=row.pinned_addon_id or row.observed_addon_id,
            addon_version=row.pinned_addon_version or row.observed_addon_version,
            kodi_version=row.pinned_kodi_version or row.observed_kodi_version,
            build_fingerprint=row.pinned_build_fingerprint
            or row.observed_build_fingerprint,
            verified_capabilities=tuple(
                sorted(cls._parse_closed(row.verified_capabilities_json))
            ),
            paired_at=None if row.paired_at is None else cls._datetime(row.paired_at),
            verified_at=(
                None if row.verified_at is None else cls._datetime(row.verified_at)
            ),
            revoked_at=(
                None if row.revoked_at is None else cls._datetime(row.revoked_at)
            ),
            http_screenshot_cleartext_warning=(
                external.protocol_kind.value == "jsonrpc_http"
            ),
            production_identity_generation=row.production_identity_generation,
            production_bridge_trust_revision=(row.production_bridge_trust_revision),
            reviewed_build_id=(
                None if row.reviewed_build_id is None else UUID(row.reviewed_build_id)
            ),
            production_confirmed_at=(
                None
                if row.production_confirmed_at is None
                else cls._datetime(row.production_confirmed_at)
            ),
        )

    @staticmethod
    def _administrator(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 64:
            raise ExternalBridgeServiceError("permission_denied")
        return normalized

    @staticmethod
    def _sha256(value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ExternalBridgeServiceError("bridge_build_not_reviewed")
        return normalized

    @classmethod
    def _reviewed_build_read(
        cls, row: ReviewedBridgeBuildRow
    ) -> ReviewedBridgeBuildRead:
        return ReviewedBridgeBuildRead(
            build_id=UUID(row.id),
            semantic_fingerprint=row.semantic_fingerprint,
            zip_sha256=row.zip_sha256,
            addon_id=row.addon_id,
            addon_version=row.addon_version,
            protocol_version=row.protocol_version,
            capabilities=tuple(sorted(cls._parse_closed(row.capabilities_json))),
            supported_kodi_versions=tuple(
                sorted(cls._parse_closed(row.supported_kodi_versions_json))
            ),
            source_reference=row.source_reference,
            reviewed_by=row.reviewed_by,
            reviewed_at=cls._datetime(row.reviewed_at),
            state=row.state,
            retired_by=row.retired_by,
            retired_at=(
                None if row.retired_at is None else cls._datetime(row.retired_at)
            ),
        )

    @staticmethod
    def _prospective_production_revision(
        row: ExternalBridgePairingRow | None,
    ) -> int:
        if row is None or row.production_bridge_trust_revision is None:
            return 1
        if row.pairing_token_reference is not None or (
            row.bridge_secret_reference is not None
            and row.pairing_state
            in {
                ExternalBridgePairingState.NEEDS_REVERIFICATION,
                ExternalBridgePairingState.PAIRING_PENDING,
            }
        ):
            return row.production_bridge_trust_revision
        return row.production_bridge_trust_revision + 1

    @classmethod
    def _require_http_acknowledgement(
        cls,
        session: Session,
        external: ExternalKodiTargetRow,
        target_id: UUID,
        bridge_port: int,
        identity_generation: int,
        bridge_revision: int,
    ) -> None:
        if external.protocol_kind.value == "jsonrpc_https":
            return
        endpoint_digest = cls._endpoint_digest(external, bridge_port)
        acknowledgement = session.scalar(
            select(ProductionBridgeHttpAcknowledgementRow).where(
                ProductionBridgeHttpAcknowledgementRow.target_id == str(target_id),
                ProductionBridgeHttpAcknowledgementRow.identity_generation
                == identity_generation,
                ProductionBridgeHttpAcknowledgementRow.bridge_trust_revision
                == bridge_revision,
                ProductionBridgeHttpAcknowledgementRow.protocol == "jsonrpc_http",
                ProductionBridgeHttpAcknowledgementRow.ip_literal == external.host,
                ProductionBridgeHttpAcknowledgementRow.native_port == external.port,
                ProductionBridgeHttpAcknowledgementRow.bridge_port == bridge_port,
                ProductionBridgeHttpAcknowledgementRow.endpoint_digest
                == endpoint_digest,
                ProductionBridgeHttpAcknowledgementRow.transport_policy_revision
                == PRODUCTION_TRANSPORT_POLICY_REVISION,
            )
        )
        if acknowledgement is None:
            raise ExternalBridgeServiceError("transport_acknowledgement_required")

    @classmethod
    def _require_reviewed_identity(
        cls,
        session: Session,
        identity: BridgeIdentity,
        capabilities: tuple[str, ...],
        *,
        allow_capability_expansion: bool = False,
    ) -> ReviewedBridgeBuildRow:
        return cls._require_reviewed_values(
            session,
            fingerprint=identity.build_fingerprint,
            addon_id=identity.addon_id,
            addon_version=identity.addon_version,
            protocol_version=identity.protocol_version,
            kodi_version=identity.kodi_version,
            capabilities=set(capabilities),
            allow_capability_expansion=allow_capability_expansion,
        )

    @classmethod
    def _require_reviewed_values(
        cls,
        session: Session,
        *,
        fingerprint: str,
        addon_id: str,
        addon_version: str,
        protocol_version: str,
        kodi_version: str,
        capabilities: set[str],
        allow_capability_expansion: bool = False,
    ) -> ReviewedBridgeBuildRow:
        rows = session.scalars(
            select(ReviewedBridgeBuildRow).where(
                ReviewedBridgeBuildRow.semantic_fingerprint == fingerprint,
                ReviewedBridgeBuildRow.addon_id == addon_id,
                ReviewedBridgeBuildRow.addon_version == addon_version,
                ReviewedBridgeBuildRow.protocol_version == protocol_version,
                ReviewedBridgeBuildRow.state == "active",
            )
        ).all()
        for row in rows:
            reviewed_capabilities = set(cls._parse_closed(row.capabilities_json))
            capability_match = reviewed_capabilities == capabilities or (
                allow_capability_expansion
                and "screenshot" in reviewed_capabilities
                and reviewed_capabilities < capabilities
            )
            if (
                kodi_version in cls._parse_closed(row.supported_kodi_versions_json)
                and capability_match
            ):
                return row
        raise ExternalBridgeServiceError("bridge_build_not_reviewed")

    def _stage_production_reverification(
        self,
        target_id: UUID,
        identity: BridgeIdentity,
        capabilities: tuple[str, ...],
    ) -> None:
        now = self._clock()
        reviewed: ReviewedBridgeBuildRow | None = None
        with self._sessions.begin() as session:
            try:
                reviewed = self._require_reviewed_identity(
                    session, identity, capabilities
                )
            except ExternalBridgeServiceError as error:
                if error.code != "bridge_build_not_reviewed":
                    raise
            row = session.get(ExternalBridgePairingRow, str(target_id))
            if row is None or row.pinned_bridge_id != str(identity.bridge_id):
                raise ExternalBridgeServiceError("bridge_identity_mismatch")
            observation_unchanged = (
                row.observed_bridge_id == str(identity.bridge_id)
                and row.observed_protocol_version == identity.protocol_version
                and row.observed_addon_id == identity.addon_id
                and row.observed_addon_version == identity.addon_version
                and row.observed_kodi_version == identity.kodi_version
                and row.observed_build_fingerprint == identity.build_fingerprint
                and self._parse_closed(row.observed_capabilities_json)
                == set(capabilities)
            )
            advance_revision = not (
                row.pairing_state is ExternalBridgePairingState.NEEDS_REVERIFICATION
                and observation_unchanged
            )
            self._set_observed(row, identity, capabilities)
            row.pairing_state = (
                ExternalBridgePairingState.PAIRING_PENDING
                if reviewed is not None
                else ExternalBridgePairingState.NEEDS_REVERIFICATION
            )
            row.reachability_state = ExternalBridgeReachabilityState.REACHABLE
            if advance_revision:
                row.production_bridge_trust_revision = (
                    row.production_bridge_trust_revision or 0
                ) + 1
            row.production_identity_generation = None
            row.reviewed_build_id = None
            row.production_confirmed_by = None
            row.production_confirmed_at = None
            row.verified_capabilities_json = "[]"
            self._event(
                session,
                target_id,
                "production_bridge_revision_advanced",
                now,
                bridge_id=identity.bridge_id,
                classification=(
                    "unreviewed_build"
                    if reviewed is None
                    else f"reviewed:{reviewed.id}"[:64]
                ),
            )
        if reviewed is None:
            raise ExternalBridgeServiceError("bridge_build_not_reviewed")

    def _record_production_capability_observation(
        self,
        target_id: UUID,
        identity: BridgeIdentity,
        capabilities: tuple[str, ...],
    ) -> None:
        with self._sessions.begin() as session:
            row = session.get(ExternalBridgePairingRow, str(target_id))
            if row is None or row.pinned_bridge_id != str(identity.bridge_id):
                return
            row.observed_capabilities_json = self._closed(capabilities)

    @staticmethod
    def _event(
        session: Session,
        target_id: UUID,
        kind: str,
        at: datetime,
        *,
        bridge_id: UUID | None = None,
        request_id: UUID | None = None,
        classification: str | None = None,
        byte_size: int | None = None,
        width: int | None = None,
        height: int | None = None,
        content_digest: str | None = None,
    ) -> None:
        session.add(
            ExternalBridgeEventRow(
                id=str(uuid4()),
                target_id=str(target_id),
                bridge_id=None if bridge_id is None else str(bridge_id),
                request_id=None if request_id is None else str(request_id),
                event_kind=kind,
                classification=classification,
                byte_size=byte_size,
                width=width,
                height=height,
                content_digest=content_digest,
                occurred_at=ExternalBridgeService._text(at),
            )
        )
