"""Narrow Web-admin browser remote for the exact curated Kodi runtimes."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.browser_remote_coordination import (
    BrowserRemoteCoordinationConflict,
    BrowserRemoteCoordinationLost,
    acquire_browser_remote,
    release_browser_remote,
)
from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatusService,
    SemanticUsabilityState,
)
from kodi_lab.application.screenshots import (
    ManagedScreenshotService,
    ScreenshotServiceError,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import ClientError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.kodi.remote import (
    NAVIGATION_ACTIONS,
    REMOTE_RUNTIME_REVISION_IDS,
    BrowserRemoteAction,
    KodiBrowserRemoteAdapter,
    RemoteActionAmbiguousError,
    RemoteKodiRejectedError,
    RemoteNoActivePlayerError,
)
from kodi_lab.storage.repositories import RuntimeExecutionProfileRepository

LOGGER = logging.getLogger("kodi_lab.browser_remote.audit")


class BrowserRemoteError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message[:256]
        super().__init__(self.safe_message)


class ScreenshotRefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: str = Field(max_length=64)
    capture_id: UUID | None = None
    captured_at: datetime | None = None


class BrowserRemoteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invocation_id: UUID
    managed_instance_id: UUID
    action: BrowserRemoteAction
    result: str = Field(max_length=64)
    completed_at: datetime
    screenshot_refresh: ScreenshotRefreshResult


class _Broker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...


class _Adapter(Protocol):
    def dispatch(self, action: BrowserRemoteAction, invocation_id: UUID) -> None: ...


type RemoteAdapterFactory = Callable[
    [ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile], _Adapter
]


class BrowserRemoteService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _Broker,
        status: ManagedRuntimeStatusService,
        reconciliation: ManagedInstanceReconciliationService,
        screenshots: ManagedScreenshotService,
        *,
        adapter_factory: RemoteAdapterFactory = KodiBrowserRemoteAdapter,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = session_factory
        self._broker = broker
        self._status = status
        self._reconciliation = reconciliation
        self._screenshots = screenshots
        self._adapter_factory = adapter_factory
        self._clock = clock

    def available(self, instance_id: UUID) -> bool:
        runtime = self._status.get(instance_id)
        return (
            runtime.runtime_revision_id in REMOTE_RUNTIME_REVISION_IDS
            and runtime.container_state is ContainerRuntimeState.RUNNING
            and runtime.process_state is KodiProcessState.RUNNING
            and runtime.usability_state is SemanticUsabilityState.USABLE
            and not self._reconciliation.status(instance_id).blocking
        )

    def invoke(
        self, instance_id: UUID, action: BrowserRemoteAction
    ) -> BrowserRemoteResult:
        invocation_id = uuid4()
        requested_at = self._clock()
        try:
            return self._invoke(instance_id, action, invocation_id, requested_at)
        except (BrowserRemoteError, ManagedInstanceNotFoundError) as error:
            completed_at = self._clock()
            result_code = (
                error.code
                if isinstance(error, BrowserRemoteError)
                else "managed_instance_not_found"
            )
            LOGGER.warning(
                "browser remote action failed",
                extra={
                    "invocation_id": str(invocation_id),
                    "interface": "web",
                    "actor": "local_admin",
                    "managed_instance_id": str(instance_id),
                    "action": action.value,
                    "requested_at": requested_at.isoformat(),
                    "completed_at": completed_at.isoformat(),
                    "result": result_code,
                    "screenshot_refresh": "not_requested",
                },
            )
            raise

    def _invoke(
        self,
        instance_id: UUID,
        action: BrowserRemoteAction,
        invocation_id: UUID,
        requested_at: datetime,
    ) -> BrowserRemoteResult:
        runtime = self._status.get(instance_id)
        if runtime.runtime_revision_id not in REMOTE_RUNTIME_REVISION_IDS:
            raise BrowserRemoteError(
                "remote_action_unsupported",
                "Browser remote is not supported for this runtime revision.",
            )
        if runtime.container_state is not ContainerRuntimeState.RUNNING:
            raise BrowserRemoteError(
                "visual_instance_not_running", "The managed instance is not running."
            )
        if (
            runtime.process_state is not KodiProcessState.RUNNING
            or runtime.usability_state is not SemanticUsabilityState.USABLE
        ):
            raise BrowserRemoteError(
                "visual_kodi_not_ready", "Kodi is not healthy and usable."
            )
        if self._reconciliation.status(instance_id).blocking:
            raise BrowserRemoteError(
                "remote_action_conflict",
                "Browser remote is blocked pending runtime reconciliation.",
            )
        try:
            lease = acquire_browser_remote(self._sessions, instance_id)
        except BrowserRemoteCoordinationConflict as error:
            raise BrowserRemoteError(
                "remote_action_conflict", "Another managed-instance action is active."
            ) from error

        dispatched = False
        refresh = ScreenshotRefreshResult(result="not_requested")
        try:
            container_id = self._eligible_container(instance_id)
            observation = self._broker.observe_managed_kodi_runtime(
                managed_instance_id=str(instance_id),
                expected_container_id=container_id,
            )
            if (
                observation.runtime_revision_id != str(runtime.runtime_revision_id)
                or observation.container_id != container_id
                or observation.runtime_state is not ContainerRuntimeState.RUNNING
                or observation.process_state is not KodiProcessState.RUNNING
            ):
                raise BrowserRemoteError(
                    "remote_action_conflict",
                    "Managed runtime identity changed before remote dispatch.",
                )
            profile = self._profile(runtime.runtime_revision_id)
            if self._eligible_container(instance_id) != container_id:
                raise BrowserRemoteError(
                    "remote_action_conflict",
                    "Managed runtime identity changed before remote dispatch.",
                )
            adapter = self._adapter_factory(observation, profile)
            adapter.dispatch(action, invocation_id)
            dispatched = True
            if action in NAVIGATION_ACTIONS:
                refresh = self._refresh_screenshot(instance_id)
        except RemoteNoActivePlayerError as error:
            raise BrowserRemoteError(
                "remote_no_active_player", "No active Kodi player is available."
            ) from error
        except RemoteActionAmbiguousError as error:
            raise BrowserRemoteError(
                "remote_action_ambiguous",
                "Kodi may have received the action; it was not retried.",
            ) from error
        except RemoteKodiRejectedError as error:
            raise BrowserRemoteError(
                "remote_kodi_rejected", "Kodi rejected the fixed remote action."
            ) from error
        except ClientError as error:
            code = "remote_action_ambiguous" if dispatched else "remote_action_conflict"
            raise BrowserRemoteError(
                code,
                "Remote completion could not be proven safely."
                if dispatched
                else "Managed runtime identity could not be proven.",
            ) from error
        finally:
            try:
                release_browser_remote(lease)
            except BrowserRemoteCoordinationLost as error:
                if dispatched:
                    raise BrowserRemoteError(
                        "remote_action_ambiguous",
                        "Remote authority changed after dispatch; the action was "
                        "not retried.",
                    ) from error
                raise BrowserRemoteError(
                    "remote_action_conflict", "Remote coordination authority changed."
                ) from error

        completed_at = self._clock()
        result = BrowserRemoteResult(
            invocation_id=invocation_id,
            managed_instance_id=instance_id,
            action=action,
            result="succeeded",
            completed_at=completed_at,
            screenshot_refresh=refresh,
        )
        LOGGER.info(
            "browser remote action completed",
            extra={
                "invocation_id": str(invocation_id),
                "interface": "web",
                "actor": "local_admin",
                "managed_instance_id": str(instance_id),
                "action": action.value,
                "requested_at": requested_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "result": result.result,
                "screenshot_refresh": refresh.result,
            },
        )
        return result

    def _refresh_screenshot(self, instance_id: UUID) -> ScreenshotRefreshResult:
        try:
            captured = self._screenshots.capture(instance_id)
        except ScreenshotServiceError as error:
            return ScreenshotRefreshResult(result=error.code)
        except ManagedInstanceNotFoundError:
            return ScreenshotRefreshResult(result="managed_instance_not_found")
        return ScreenshotRefreshResult(
            result="succeeded",
            capture_id=captured.capture_id,
            captured_at=captured.screenshot.captured_at,
        )

    def _eligible_container(self, instance_id: UUID) -> str:
        inventory = self._broker.managed_container_inventory()
        matches = [
            item
            for item in inventory.containers
            if item.managed_instance_id == str(instance_id)
        ]
        if (
            len(matches) != 1
            or matches[0].runtime_state is not ContainerRuntimeState.RUNNING
            or matches[0].health_state is not ContainerHealthState.HEALTHY
        ):
            raise BrowserRemoteError(
                "remote_action_conflict", "The exact healthy runtime is unavailable."
            )
        return matches[0].container_id

    def _profile(self, revision_id: UUID) -> RuntimeExecutionProfile:
        with self._sessions() as session:
            profile = RuntimeExecutionProfileRepository(session).get(revision_id)
        if profile is None:
            raise BrowserRemoteError(
                "remote_action_unsupported",
                "The trusted runtime profile is unavailable.",
            )
        return profile


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class BrowserRemoteRateLimiter:
    """In-process token bucket: ten actions/second with a burst of twenty."""

    def __init__(
        self,
        *,
        rate: float = 10.0,
        burst: int = 20,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = rate
        self._burst = burst
        self._clock = clock
        self._buckets: dict[tuple[str, UUID], _Bucket] = {}
        self._lock = Lock()

    def require_allowed(self, session_key: str, instance_id: UUID) -> None:
        now = self._clock()
        key = (session_key, instance_id)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(float(self._burst), now)
                self._buckets[key] = bucket
            bucket.tokens = min(
                float(self._burst),
                bucket.tokens + max(0.0, now - bucket.updated_at) * self._rate,
            )
            bucket.updated_at = now
            if bucket.tokens < 1.0:
                raise BrowserRemoteError(
                    "visual_rate_limited", "Browser remote action rate is exceeded."
                )
            bucket.tokens -= 1.0
