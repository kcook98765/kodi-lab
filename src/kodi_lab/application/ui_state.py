"""Per-target admission and exact managed-runtime UI observation routing."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatusService,
    SemanticUsabilityState,
)
from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import ClientError
from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.kodi.readiness import (
    JsonRpcInvalidResponseError,
    JsonRpcUnavailableError,
    KodiJsonRpcReadinessAdapter,
    _BoundedHttpTransport,
)
from kodi_lab.kodi.ui_state import UiObservation, normalize_collection
from kodi_lab.kodi.ui_state_native import UiObservationError, UiRpc, collect_native
from kodi_lab.storage.repositories import RuntimeExecutionProfileRepository

if TYPE_CHECKING:
    from kodi_lab.application.managed_observation_bridge import (
        ManagedObservationBridgeService,
    )


class ObservationGate:
    """Bounded per-target admission; does not lock navigation or Kodi's GUI."""

    def __init__(self, *, timeout: float = 3.0, limit: int = 1024) -> None:
        self._timeout = timeout
        self._limit = limit
        self._guard = threading.Lock()
        self._entries: dict[UUID, tuple[threading.Lock, int]] = {}

    @contextmanager
    def enter(self, target_id: UUID) -> Iterator[None]:
        with self._guard:
            if target_id not in self._entries:
                if len(self._entries) >= self._limit:
                    raise UiObservationError("rate_limited")
                self._entries[target_id] = (threading.Lock(), 0)
            lock, users = self._entries[target_id]
            self._entries[target_id] = (lock, users + 1)
        acquired = False
        try:
            acquired = lock.acquire(timeout=self._timeout)
            if not acquired:
                raise UiObservationError("rate_limited")
            yield
        finally:
            if acquired:
                lock.release()
            with self._guard:
                _, users = self._entries[target_id]
                if users == 1:
                    del self._entries[target_id]
                else:
                    self._entries[target_id] = (lock, users - 1)


class _Transport(Protocol):
    def post(
        self, *, host: str, port: int, path: str, body: bytes, timeout: float
    ) -> bytes: ...


class ManagedUiObservationService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        broker: BrokerClient,
        status: ManagedRuntimeStatusService,
        reconciliation: ManagedInstanceReconciliationService,
        *,
        transport: _Transport | None = None,
        bridges: ManagedObservationBridgeService | None = None,
    ) -> None:
        self._sessions = sessions
        self._broker = broker
        self._status = status
        self._reconciliation = reconciliation
        self._transport = transport or _BoundedHttpTransport()
        self._bridges = bridges

    def _admit(
        self, target_id: UUID
    ) -> tuple[ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile]:
        snapshot = self._status.get_observation(target_id)
        runtime = snapshot.status
        if (
            runtime.container_state is not ContainerRuntimeState.RUNNING
            or runtime.process_state is not KodiProcessState.RUNNING
            or runtime.usability_state is not SemanticUsabilityState.USABLE
        ):
            raise UiObservationError("target_unreachable")
        if self._reconciliation.status(target_id).blocking:
            raise UiObservationError("reconciliation_required")
        # Reuse this read's exact trusted inputs instead of probing them twice.
        # The final runtime observation below remains a separate fresh request.
        inventory, observed = snapshot.inventory, snapshot.runtime
        matches = (
            [
                item
                for item in inventory.containers
                if item.managed_instance_id == str(target_id)
            ]
            if inventory is not None
            else []
        )
        if (
            len(matches) != 1
            or matches[0].health_state is not ContainerHealthState.HEALTHY
            or observed is None
        ):
            raise UiObservationError("target_unreachable")
        with self._sessions() as session:
            profile = RuntimeExecutionProfileRepository(session).get(
                runtime.runtime_revision_id
            )
        if (
            profile is None
            or observed.runtime_revision_id != str(runtime.runtime_revision_id)
            or observed.container_id != matches[0].container_id
            or observed.managed_instance_id != str(target_id)
            or observed.runtime_state is not ContainerRuntimeState.RUNNING
            or observed.process_state is not KodiProcessState.RUNNING
            or observed.jsonrpc_port != profile.jsonrpc_port
        ):
            raise UiObservationError("identity_mismatch")
        # Match the current endpoint to the finalized runtime profile.
        try:
            identity = KodiJsonRpcReadinessAdapter(
                observed,
                profile,
                transport=self._transport,
            ).get_application_identity()
        except JsonRpcInvalidResponseError as error:
            raise UiObservationError("identity_mismatch") from error
        except JsonRpcUnavailableError as error:
            raise UiObservationError("target_unreachable") from error
        if (
            identity.name != profile.expected_application_name
            or identity.kodi_release != profile.kodi_release
        ):
            raise UiObservationError("identity_mismatch")
        return observed, profile

    def _native_call(
        self,
        observed: ObserveManagedKodiRuntimeResult,
        profile: RuntimeExecutionProfile,
    ) -> UiRpc:
        started = time.monotonic()

        def call(method: str, params: dict[str, Any] | None) -> object:
            request_id = str(uuid4())
            request: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
            }
            if params is not None:
                request["params"] = params
            remaining = 6.0 - (time.monotonic() - started)
            if remaining <= 0:
                raise UiObservationError("ui_observation_failed")
            try:
                body = self._transport.post(
                    host=observed.ipv4_address,
                    port=profile.jsonrpc_port,
                    path=profile.jsonrpc_path,
                    body=json.dumps(request).encode(),
                    timeout=min(remaining, profile.http_timeout_seconds),
                )
                result = json.loads(body)
                if (
                    not isinstance(result, dict)
                    or result.get("jsonrpc") != "2.0"
                    or result.get("id") != request_id
                    or "error" in result
                    or "result" not in result
                ):
                    raise UiObservationError("ui_observation_failed")
                return result["result"]
            except (OSError, ValueError, RuntimeError) as error:
                raise UiObservationError("ui_observation_failed") from error

        return call

    def _verify_current(
        self, target_id: UUID, observed: ObserveManagedKodiRuntimeResult
    ) -> None:
        try:
            current = self._broker.observe_managed_kodi_runtime(
                managed_instance_id=str(target_id),
                expected_container_id=observed.container_id,
            )
        except ClientError as error:
            raise UiObservationError("identity_mismatch") from error
        if (
            current.managed_instance_id != observed.managed_instance_id
            or current.container_id != observed.container_id
            or current.runtime_revision_id != observed.runtime_revision_id
            or current.ipv4_address != observed.ipv4_address
            or current.jsonrpc_port != observed.jsonrpc_port
            or current.runtime_state is not ContainerRuntimeState.RUNNING
            or current.process_state is not KodiProcessState.RUNNING
        ):
            raise UiObservationError("identity_mismatch")

    def inspect(self, target_id: UUID) -> UiObservation:
        observed, profile = self._admit(target_id)
        raw = (
            self._bridges.collect(
                target_id,
                observed,
                profile.kodi_release.canonical,
            )
            if self._bridges
            else None
        )
        result = (
            normalize_collection(raw)
            if raw is not None
            else collect_native(self._native_call(observed, profile))
        )
        self._verify_current(target_id, observed)
        return result
