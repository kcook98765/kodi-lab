"""One semantic MCP target-adapter boundary for managed and external Kodi."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_inventory import (
    AddonInventoryQueryStatus,
    KodiAddonInventoryItem,
    KodiAddonProvenance,
    KodiAddonProvenanceClassification,
    KodiAddonProvenanceEvidenceStatus,
    ManagedInstanceAddonInventory,
    ManagedInstanceAddonInventoryService,
)
from kodi_lab.application.browser_remote import (
    BrowserRemoteError,
    BrowserRemoteResult,
    BrowserRemoteService,
    RemoteActorContext,
    ScreenshotRefreshResult,
)
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.domain import (
    EXTERNAL_CAPABILITY_VALUES,
    ExternalAdministrativeState,
    ExternalAuthenticationMode,
    ExternalVerificationState,
    TargetType,
)
from kodi_lab.kodi.addons import (
    KodiAddonInventoryLimitExceededError,
    KodiAddonInventoryMalformedDataError,
    KodiAddonInventoryUnsupportedResponseError,
)
from kodi_lab.kodi.external import (
    ExternalEndpointError,
    ExternalEndpointPolicy,
    ExternalProbeError,
    NativeExternalKodiAdapter,
    ValidatedExternalEndpoint,
)
from kodi_lab.kodi.remote import (
    BrowserRemoteAction,
    RemoteActionAmbiguousError,
    RemoteKodiRejectedError,
    RemoteNoActivePlayerError,
)
from kodi_lab.mcp.authorization import TargetTechnicalAuthority
from kodi_lab.mcp.registry import TargetClass
from kodi_lab.storage.models import (
    ExternalKodiTargetRow,
    ManagedInstanceRow,
    TargetRow,
)
from kodi_lab.storage.secrets import (
    ExternalCredentialStore,
    ExternalKodiCredential,
    SecretStoreError,
)


class KodiTargetAdapterError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class KodiTargetSummaryData:
    target_id: UUID
    display_name: str
    kodi_version: str
    target_class: str
    runtime_profile: str
    provision_state: str
    runtime_state: str
    process_state: str
    jsonrpc_state: str
    usability_state: str
    readiness_state: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ExternalSnapshot:
    target_id: UUID
    logical_name: str
    endpoint: ValidatedExternalEndpoint
    credential: ExternalKodiCredential | None
    kodi_name: str
    kodi_version: str
    capabilities: frozenset[str]


class _ExternalOperationGate:
    """Bound concurrent native work and serialize every exact external target."""

    def __init__(self, *, global_limit: int = 8, target_limit: int = 1024) -> None:
        self._global = threading.BoundedSemaphore(global_limit)
        self._target_limit = target_limit
        self._guard = threading.Lock()
        self._targets: dict[UUID, threading.Lock] = {}

    @contextmanager
    def enter(self, target_id: UUID) -> Iterator[None]:
        with self._guard:
            lock = self._targets.get(target_id)
            if lock is None:
                if len(self._targets) >= self._target_limit:
                    raise KodiTargetAdapterError("rate_limited")
                lock = threading.Lock()
                self._targets[target_id] = lock
        if not self._global.acquire(blocking=False):
            raise KodiTargetAdapterError("rate_limited")
        acquired = lock.acquire(blocking=False)
        if not acquired:
            self._global.release()
            raise KodiTargetAdapterError("rate_limited")
        try:
            yield
        finally:
            lock.release()
            self._global.release()


class _ManagedAuthority(Protocol):
    def resolve(self, target_id: UUID) -> TargetTechnicalAuthority: ...


class KodiTargetAdapterService:
    """Resolve target class once below MCP authorization and handlers."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        managed_status: ManagedRuntimeStatusService,
        managed_inventory: ManagedInstanceAddonInventoryService,
        managed_remote: BrowserRemoteService,
        managed_authority: _ManagedAuthority,
        endpoint_policy: ExternalEndpointPolicy,
        credential_store: ExternalCredentialStore,
        external_transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = session_factory
        self._managed_status = managed_status
        self._managed_inventory = managed_inventory
        self._managed_remote = managed_remote
        self._managed_authority = managed_authority
        self._endpoint_policy = endpoint_policy
        self._credential_store = credential_store
        self._external_transport = external_transport
        self._now = now
        self._gate = _ExternalOperationGate()

    def resolve(self, target_id: UUID) -> TargetTechnicalAuthority:
        target_type = self._target_type(target_id)
        if target_type is TargetType.MANAGED:
            return self._managed_authority.resolve(target_id)
        snapshot = self._external_snapshot(target_id, load_secret=False)
        states = {"reconciled"}
        if snapshot.capabilities:
            states.update({"running", "ready"})
        return TargetTechnicalAuthority(
            target_class=TargetClass.EXTERNAL_DEVELOPMENT,
            capabilities=snapshot.capabilities,
            current_states=frozenset(states),
        )

    def summary(self, target_id: UUID, *, fresh: bool) -> KodiTargetSummaryData:
        if self._target_type(target_id) is TargetType.MANAGED:
            status = self._managed_status.get(target_id)
            with self._sessions() as session:
                instance = session.get(ManagedInstanceRow, str(target_id))
            if instance is None:
                raise KodiTargetAdapterError("target_not_found_or_not_authorized")
            return self._managed_summary(
                status, f"managed_{instance.instance_type.value}"
            )
        snapshot = self._external_snapshot(target_id, load_secret=fresh)
        if fresh:
            with self._gate.enter(target_id):
                self._require_identity(snapshot, self._external_adapter(snapshot))
        return self._external_summary(snapshot)

    def summaries(self, target_ids: list[UUID]) -> tuple[KodiTargetSummaryData, ...]:
        if not target_ids:
            return ()
        with self._sessions() as session:
            target_types = {
                UUID(row.id): row.target_type
                for row in session.scalars(
                    select(TargetRow).where(
                        TargetRow.id.in_([str(target_id) for target_id in target_ids])
                    )
                ).all()
            }
            profiles = {
                UUID(row.target_id): f"managed_{row.instance_type.value}"
                for row in session.scalars(
                    select(ManagedInstanceRow).where(
                        ManagedInstanceRow.target_id.in_(
                            [str(target_id) for target_id in target_ids]
                        )
                    )
                ).all()
            }
        statuses = (
            {
                item.managed_instance_id: item
                for item in self._managed_status.list_statuses()
            }
            if TargetType.MANAGED in target_types.values()
            else {}
        )
        results: list[KodiTargetSummaryData] = []
        for target_id in target_ids:
            target_type = target_types.get(target_id)
            if target_type is TargetType.MANAGED:
                try:
                    results.append(
                        self._managed_summary(statuses[target_id], profiles[target_id])
                    )
                except KeyError as error:
                    raise KodiTargetAdapterError(
                        "target_not_found_or_not_authorized"
                    ) from error
            elif target_type is TargetType.EXTERNAL_DEVELOPMENT:
                results.append(
                    self._external_summary(
                        self._external_snapshot(target_id, load_secret=False)
                    )
                )
            else:
                raise KodiTargetAdapterError("target_not_found_or_not_authorized")
        return tuple(results)

    def addon_inventory(self, target_id: UUID) -> ManagedInstanceAddonInventory:
        if self._target_type(target_id) is TargetType.MANAGED:
            return self._managed_inventory.get(target_id)
        snapshot = self._external_snapshot(
            target_id, required_capability="addon_inventory", load_secret=True
        )
        try:
            with self._gate.enter(target_id):
                result = self._external_adapter(snapshot).get_inventory()
            self._require_identity_result(
                snapshot, result.identity.name, result.identity.kodi_release.canonical
            )
        except KodiTargetAdapterError:
            raise
        except ExternalProbeError as error:
            raise KodiTargetAdapterError(error.code) from error
        except KodiAddonInventoryLimitExceededError as error:
            raise KodiTargetAdapterError("response_too_large") from error
        except (
            KodiAddonInventoryMalformedDataError,
            KodiAddonInventoryUnsupportedResponseError,
        ) as error:
            raise KodiTargetAdapterError("inventory_unavailable") from error
        unknown = KodiAddonProvenance(
            classification=KodiAddonProvenanceClassification.UNKNOWN,
            evidence_status=KodiAddonProvenanceEvidenceStatus.UNKNOWN,
        )
        return ManagedInstanceAddonInventory(
            instance_id=target_id,
            observed_at=self._now(),
            kodi_release=result.identity.kodi_release.canonical,
            query_status=AddonInventoryQueryStatus.AVAILABLE,
            complete=True,
            items=[
                KodiAddonInventoryItem(
                    addon_id=item.addon_id,
                    name=item.name,
                    version=item.version,
                    enabled=item.enabled,
                    kodi_type=item.kodi_type,
                    provenance=unknown,
                )
                for item in result.items
            ],
        )

    def invoke(
        self,
        target_id: UUID,
        action: BrowserRemoteAction,
        *,
        actor: RemoteActorContext | None = None,
        invocation_id: UUID | None = None,
        before_dispatch: Callable[[], None] | None = None,
    ) -> BrowserRemoteResult:
        if self._target_type(target_id) is TargetType.MANAGED:
            self._before_dispatch(before_dispatch)
            return self._managed_remote.invoke(
                target_id,
                action,
                actor=actor,
                invocation_id=invocation_id,
            )
        if invocation_id is None:
            raise BrowserRemoteError(
                "remote_action_conflict", "Missing invocation identity."
            )
        snapshot = self._external_snapshot(
            target_id, required_capability="semantic_remote", load_secret=True
        )
        try:
            with self._gate.enter(target_id):
                adapter = self._external_adapter(snapshot)
                self._require_identity(snapshot, adapter)
                self._before_dispatch(before_dispatch)
                adapter.dispatch(action, invocation_id)
        except KodiTargetAdapterError as error:
            raise BrowserRemoteError(
                error.code, "External target is unavailable."
            ) from error
        except ExternalProbeError as error:
            raise BrowserRemoteError(
                error.code, "External target is unavailable."
            ) from error
        except RemoteActionAmbiguousError as error:
            raise BrowserRemoteError(
                "remote_action_ambiguous",
                "Kodi may have received the action; it was not retried.",
            ) from error
        except RemoteNoActivePlayerError as error:
            raise BrowserRemoteError(
                "remote_no_active_player", "No active Kodi player is available."
            ) from error
        except RemoteKodiRejectedError as error:
            raise BrowserRemoteError(
                "remote_kodi_rejected", "Kodi rejected the fixed remote action."
            ) from error
        return BrowserRemoteResult(
            invocation_id=invocation_id,
            managed_instance_id=target_id,
            action=action,
            result="succeeded",
            completed_at=self._now(),
            screenshot_refresh=ScreenshotRefreshResult(result="not_requested"),
        )

    @staticmethod
    def _before_dispatch(callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception as error:
            code = getattr(error, "code", "target_not_found_or_not_authorized")
            if code not in {
                "authentication_failed",
                "capability_unavailable",
                "identity_mismatch",
                "needs_reverification",
                "permission_denied",
                "target_not_found_or_not_authorized",
                "target_unreachable",
            }:
                code = "target_not_found_or_not_authorized"
            raise BrowserRemoteError(code, "Remote authority changed.") from error

    def _target_type(self, target_id: UUID) -> TargetType:
        with self._sessions() as session:
            row = session.get(TargetRow, str(target_id))
        if row is None:
            raise KodiTargetAdapterError("target_not_found_or_not_authorized")
        return row.target_type

    def _external_snapshot(
        self,
        target_id: UUID,
        *,
        required_capability: str = "target_inspect",
        load_secret: bool,
    ) -> _ExternalSnapshot:
        with self._sessions() as session:
            target = session.get(TargetRow, str(target_id))
            row = session.get(ExternalKodiTargetRow, str(target_id))
            if (
                target is None
                or row is None
                or target.target_type is not TargetType.EXTERNAL_DEVELOPMENT
            ):
                raise KodiTargetAdapterError("target_not_found_or_not_authorized")
            if row.administrative_state is not ExternalAdministrativeState.ENABLED:
                raise KodiTargetAdapterError("target_not_found_or_not_authorized")
            if row.verification_state is ExternalVerificationState.IDENTITY_MISMATCH:
                raise KodiTargetAdapterError("identity_mismatch")
            if row.verification_state is ExternalVerificationState.NEEDS_REVERIFICATION:
                raise KodiTargetAdapterError("needs_reverification")
            if row.verification_state is not ExternalVerificationState.VERIFIED:
                raise KodiTargetAdapterError("target_not_found_or_not_authorized")
            configured = self._closed_json_strings(
                row.configured_capabilities_json, allowed=EXTERNAL_CAPABILITY_VALUES
            )
            verified = self._closed_json_strings(
                row.verified_capabilities_json, allowed=EXTERNAL_CAPABILITY_VALUES
            )
            capabilities = configured.intersection(verified)
            if required_capability not in capabilities:
                raise KodiTargetAdapterError("capability_unavailable")
            addresses = tuple(self._closed_json_strings(row.verified_addresses_json))
            if (
                addresses != (row.host,)
                or row.verified_kodi_name is None
                or row.verified_kodi_version is None
            ):
                raise KodiTargetAdapterError("needs_reverification")
            try:
                endpoint = self._endpoint_policy.validate(
                    row.protocol_kind, row.host, row.port
                )
            except ExternalEndpointError as error:
                raise KodiTargetAdapterError("needs_reverification") from error
            authentication_mode = row.authentication_mode
            reference = row.secret_reference
            logical_name = target.name
            kodi_name = row.verified_kodi_name
            kodi_version = row.verified_kodi_version
        credential = None
        if (
            load_secret
            and authentication_mode is ExternalAuthenticationMode.USERNAME_PASSWORD
        ):
            if reference is None:
                raise KodiTargetAdapterError("authentication_failed")
            try:
                credential = self._credential_store.read(target_id, UUID(reference))
            except (SecretStoreError, ValueError) as error:
                raise KodiTargetAdapterError("authentication_failed") from error
        return _ExternalSnapshot(
            target_id=target_id,
            logical_name=logical_name,
            endpoint=endpoint,
            credential=credential,
            kodi_name=kodi_name,
            kodi_version=kodi_version,
            capabilities=capabilities,
        )

    @staticmethod
    def _closed_json_strings(
        value: str, *, allowed: frozenset[str] | None = None
    ) -> frozenset[str]:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise KodiTargetAdapterError("needs_reverification") from error
        if (
            not isinstance(parsed, list)
            or any(not isinstance(item, str) for item in parsed)
            or len(parsed) != len(set(parsed))
        ):
            raise KodiTargetAdapterError("needs_reverification")
        result = frozenset(parsed)
        if allowed is not None and not result <= allowed:
            raise KodiTargetAdapterError("needs_reverification")
        return result

    def _external_adapter(
        self, snapshot: _ExternalSnapshot
    ) -> NativeExternalKodiAdapter:
        return NativeExternalKodiAdapter(
            snapshot.endpoint,
            snapshot.credential,
            transport=self._external_transport,
        )

    def _require_identity(
        self, snapshot: _ExternalSnapshot, adapter: NativeExternalKodiAdapter
    ) -> None:
        try:
            identity = adapter.get_identity()
        except ExternalProbeError as error:
            raise KodiTargetAdapterError(error.code) from error
        self._require_identity_result(
            snapshot, identity.name, identity.kodi_release.canonical
        )

    @staticmethod
    def _require_identity_result(
        snapshot: _ExternalSnapshot, name: str, version: str
    ) -> None:
        if name != snapshot.kodi_name or version != snapshot.kodi_version:
            raise KodiTargetAdapterError("identity_mismatch")

    @staticmethod
    def _external_summary(snapshot: _ExternalSnapshot) -> KodiTargetSummaryData:
        tools = ["target_status"]
        if "addon_inventory" in snapshot.capabilities:
            tools.append("addon_list")
        if "semantic_remote" in snapshot.capabilities:
            tools.append("remote_action")
        return KodiTargetSummaryData(
            target_id=snapshot.target_id,
            display_name=snapshot.logical_name,
            kodi_version=snapshot.kodi_version,
            target_class=TargetClass.EXTERNAL_DEVELOPMENT.value,
            runtime_profile="external_native",
            provision_state="enabled",
            runtime_state="external",
            process_state="running",
            jsonrpc_state="responsive",
            usability_state="usable",
            readiness_state="ready",
            capabilities=tuple(tools),
        )

    @staticmethod
    def _managed_summary(status: object, profile: str) -> KodiTargetSummaryData:
        return KodiTargetSummaryData(
            target_id=status.managed_instance_id,  # type: ignore[attr-defined]
            display_name=status.name,  # type: ignore[attr-defined]
            kodi_version=status.kodi_version,  # type: ignore[attr-defined]
            target_class=TargetClass.INTERNAL_LAB.value,
            runtime_profile=profile,
            provision_state=status.provision_state.value,  # type: ignore[attr-defined]
            runtime_state=status.container_state.value,  # type: ignore[attr-defined]
            process_state=status.process_state.value,  # type: ignore[attr-defined]
            jsonrpc_state=status.jsonrpc_state.value,  # type: ignore[attr-defined]
            usability_state=status.usability_state.value,  # type: ignore[attr-defined]
            readiness_state=status.readiness_state.value,  # type: ignore[attr-defined]
            capabilities=("target_status",),
        )
