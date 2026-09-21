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
from sqlalchemy.orm import Session, sessionmaker

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
from kodi_lab.storage.models import (
    ExternalBridgeEventRow,
    ExternalBridgePairingRow,
    ExternalBridgeRequestReceiptRow,
    ExternalKodiTargetRow,
    TargetRow,
)
from kodi_lab.storage.secrets import ExternalBridgeSecretStore, SecretStoreError

MAX_DEPLOYMENT_DURATION_SECONDS = 2 * 60 * 60
MAX_DEPLOYMENT_TRANSFER_BYTES_PER_SECOND = 16 * 1024 * 1024
PAIRING_TOKEN_TTL = timedelta(minutes=10)
BRIDGE_RECEIPT_TTL = timedelta(hours=24)
MAX_RECEIPTS_PER_TARGET = 256
MAX_RECEIPTS_GLOBAL = 4096


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


@dataclass(frozen=True, slots=True)
class _PairingSnapshot:
    endpoint: ValidatedExternalEndpoint
    bridge_port: int
    token_reference: UUID
    kodi_version: str


class _BoundedGate:
    def __init__(self, *, global_limit: int, target_limit: int = 1024) -> None:
        self._global = threading.BoundedSemaphore(global_limit)
        self._guard = threading.Lock()
        self._targets: dict[UUID, threading.Lock] = {}
        self._target_limit = target_limit

    @contextmanager
    def enter(self, target_id: UUID) -> Iterator[None]:
        with self._guard:
            lock = self._targets.get(target_id)
            if lock is None:
                if len(self._targets) >= self._target_limit:
                    raise ExternalBridgeServiceError("bridge_replay_rejected")
                lock = threading.Lock()
                self._targets[target_id] = lock
        if not self._global.acquire(blocking=False):
            raise ExternalBridgeServiceError("bridge_replay_rejected")
        if not lock.acquire(blocking=False):
            self._global.release()
            raise ExternalBridgeServiceError("bridge_replay_rejected")
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
        self._deployment_gate = _BoundedGate(global_limit=2)
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
            or target.target_type is not TargetType.EXTERNAL_DEVELOPMENT
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
            )
        return self._project(target_id, external, row)

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
        try:
            reference = self._secrets.write(target_id, "pairing_token", token)
        except SecretStoreError as error:
            raise ExternalBridgeServiceError("bridge_authentication_failed") from error
        try:
            with self._sessions.begin() as session:
                external = self._require_native_verified(session, target_id)
                self._validate_bridge_endpoint(external, bridge_port)
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
            except Exception:
                self._secrets.retire(secret_reference)
                raise
            self._secrets.retire(snapshot.token_reference)
        return self.status(target_id)

    def confirm_pairing(self, target_id: UUID) -> ExternalBridgeStatus:
        now = self._clock()
        with self._sessions.begin() as session:
            external = self._require_native_verified(session, target_id)
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
            self._event(
                session,
                target_id,
                "bridge_paired",
                now,
                bridge_id=UUID(row.pinned_bridge_id),
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
            row.pairing_state = ExternalBridgePairingState.NEEDS_REVERIFICATION
            row.reachability_state = ExternalBridgeReachabilityState.UNKNOWN
            row.verified_capabilities_json = "[]"
            self._event(session, target_id, "bridge_reverification_required", now)
        return self.status(target_id)

    def reverify(self, target_id: UUID) -> ExternalBridgeStatus:
        with self._pairing_gate.enter(target_id):
            snapshot = self._active_snapshot(target_id, allow_needs_reverification=True)
            identity, capabilities = self._observe_authenticated(snapshot)
            try:
                self._require_pinned_identity(snapshot, identity)
            except ExternalBridgeServiceError:
                self._invalidate_identity(target_id, "bridge_identity_mismatch")
                raise
            if not set(capabilities).intersection(
                {"screenshot", "workspace_deployment"}
            ):
                self._invalidate_identity(target_id, "bridge_capability_unavailable")
                raise ExternalBridgeServiceError("bridge_capability_unavailable")
            now = self._clock()
            with self._sessions.begin() as session:
                external = self._require_native_verified(session, target_id)
                row = session.get(ExternalBridgePairingRow, str(target_id))
                if row is None or row.pinned_bridge_id != str(identity.bridge_id):
                    raise ExternalBridgeServiceError("bridge_identity_mismatch")
                row.pairing_state = ExternalBridgePairingState.PAIRED
                row.reachability_state = ExternalBridgeReachabilityState.REACHABLE
                row.verified_capabilities_json = self._closed(capabilities)
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
        try:
            self._active_snapshot(target_id, required_capability="workspace_deployment")
            return True
        except ExternalBridgeServiceError:
            return False

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

    def capture_screenshot(self, target_id: UUID) -> ExternalScreenshotCapture:
        with self._screenshot_gate.enter(target_id):
            snapshot = self._active_snapshot(
                target_id, required_capability="screenshot"
            )
            request_id = uuid4()
            requested_at = self._clock()
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
                self._invalidate_identity(target_id, "bridge_identity_mismatch")
                self._fail_receipt(target_id, request_id, error.code, ambiguous=False)
                raise
            if "screenshot" not in capabilities:
                self._invalidate_identity(target_id, "bridge_capability_unavailable")
                self._fail_receipt(
                    target_id,
                    request_id,
                    "bridge_capability_unavailable",
                    ambiguous=False,
                )
                raise ExternalBridgeServiceError("bridge_capability_unavailable")
            nonce = b64url(os.urandom(32))
            try:
                self._claim_receipt(target_id, request_id, nonce, requested_at)
            except ExternalBridgeServiceError as error:
                self._fail_receipt(target_id, request_id, error.code, ambiguous=False)
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
                if first.code == "bridge_unreachable":
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
                        self._publish_reachability(target_id, second.code)
                        self._fail_receipt(
                            target_id,
                            request_id,
                            second.code,
                            ambiguous=second.code
                            in {"bridge_unreachable", "bridge_replay_rejected"},
                        )
                        raise ExternalBridgeServiceError(second.code) from second
                else:
                    self._publish_reachability(target_id, first.code)
                    self._fail_receipt(
                        target_id, request_id, first.code, ambiguous=False
                    )
                    raise ExternalBridgeServiceError(first.code) from first
            self._complete_receipt(target_id, result)
            return ExternalScreenshotCapture(
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

    def _pairing_snapshot(self, target_id: UUID) -> _PairingSnapshot:
        with self._sessions() as session:
            external = self._require_native_verified(session, target_id)
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
                raise ExternalBridgeServiceError("bridge_authentication_failed")
            endpoint = self._validate_bridge_endpoint(external, row.bridge_port)
            if external.verified_kodi_version is None:
                raise ExternalBridgeServiceError("bridge_identity_mismatch")
            return _PairingSnapshot(
                endpoint=endpoint,
                bridge_port=row.bridge_port,
                token_reference=UUID(row.pairing_token_reference),
                kodi_version=external.verified_kodi_version,
            )

    def _active_snapshot(
        self,
        target_id: UUID,
        *,
        allow_needs_reverification: bool = False,
        required_capability: str | None = None,
    ) -> _Snapshot:
        with self._sessions() as session:
            external = self._require_native_verified(session, target_id)
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
            return _Snapshot(
                target_id=target_id,
                endpoint=endpoint,
                bridge_port=row.bridge_port,
                bridge_id=UUID(row.pinned_bridge_id),
                secret_reference=UUID(row.bridge_secret_reference),
                kodi_version=row.pinned_kodi_version,
                build_fingerprint=row.pinned_build_fingerprint,
                endpoint_digest=digest,
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
            or identity.protocol_version != BRIDGE_PROTOCOL_VERSION
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
        with self._sessions.begin() as session:
            row = session.get(
                ExternalBridgeRequestReceiptRow,
                {"target_id": str(target_id), "request_id": str(request_id)},
            )
            if row is not None and row.state == "pending":
                row.state = "ambiguous" if ambiguous else "rejected"
                row.completed_at = self._text(now)
            self._event(
                session,
                target_id,
                "screenshot_failed",
                now,
                request_id=request_id,
                classification=code,
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
        with self._sessions.begin() as session:
            row = session.get(ExternalBridgePairingRow, str(target_id))
            if row is not None:
                row.reachability_state = self._reachability(code)
                self._event(
                    session,
                    target_id,
                    "pairing_failed",
                    now,
                    classification=code,
                )

    def _publish_reachability(self, target_id: UUID, code: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(ExternalBridgePairingRow, str(target_id))
            if row is not None:
                row.reachability_state = self._reachability(code)

    def _invalidate_identity(self, target_id: UUID, classification: str) -> None:
        now = self._clock()
        with self._sessions.begin() as session:
            row = session.get(ExternalBridgePairingRow, str(target_id))
            if row is not None:
                row.pairing_state = ExternalBridgePairingState.NEEDS_REVERIFICATION
                row.reachability_state = (
                    ExternalBridgeReachabilityState.IDENTITY_MISMATCH
                )
                row.verified_capabilities_json = "[]"
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
            or target.target_type is not TargetType.EXTERNAL_DEVELOPMENT
            or external.administrative_state is ExternalAdministrativeState.DEACTIVATED
        ):
            raise ExternalBridgeServiceError("target_not_found")
        return external

    @classmethod
    def _require_native_verified(
        cls, session: Session, target_id: UUID
    ) -> ExternalKodiTargetRow:
        external = cls._require_external(session, target_id)
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
        )

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
