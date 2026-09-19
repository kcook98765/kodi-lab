"""Exact Kodi 21.3 view-only live display sessions."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event, Lock
from typing import Protocol
from uuid import UUID, uuid4

from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatus,
    ManagedRuntimeStatusService,
    SemanticUsabilityState,
)
from kodi_lab.application.screenshots import (
    EphemeralPngFrame,
    ManagedScreenshotService,
    ScreenshotServiceError,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import ClientError
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.kodi.screenshots import ScreenshotArtifactError

LOGGER = logging.getLogger("kodi_lab.live_display.audit")
DISPLAY_SESSION_LIFETIME_SECONDS = 15 * 60.0


class LiveDisplayError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message[:256]
        super().__init__(self.safe_message)


class LiveDisplayFrameSkipped(RuntimeError):
    """Expected transient capture contention; no frame was generated or queued."""


class _Broker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...


@dataclass(slots=True)
class LiveDisplaySession:
    session_id: UUID
    instance_id: UUID
    runtime_revision_id: UUID
    container_id: str
    started_at: datetime
    started_monotonic: float
    cancelled: Event = field(default_factory=Event)
    frame_count: int = 0
    aggregate_bytes: int = 0
    established: bool = False
    closed: bool = False


class LiveDisplaySessionRegistry:
    """Process-local one-per-instance and two-global display registry."""

    def __init__(self, *, global_limit: int = 2) -> None:
        self._global_limit = global_limit
        self._lock = Lock()
        self._active: dict[UUID, LiveDisplaySession] = {}

    def acquire(self, session: LiveDisplaySession) -> None:
        with self._lock:
            if session.instance_id in self._active:
                raise LiveDisplayError(
                    "display_conflict",
                    "A live display is already active for this instance.",
                )
            if len(self._active) >= self._global_limit:
                raise LiveDisplayError(
                    "display_rate_limited",
                    "The global live-display capacity is currently reached.",
                )
            self._active[session.instance_id] = session

    def release(self, session: LiveDisplaySession) -> None:
        with self._lock:
            if self._active.get(session.instance_id) is session:
                del self._active[session.instance_id]

    def cancel(self, instance_id: UUID) -> bool:
        with self._lock:
            session = self._active.get(instance_id)
            if session is None:
                return False
            session.cancelled.set()
            return True

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def session_id(self, instance_id: UUID) -> UUID | None:
        """Return only the non-credential correlation UUID for internal evidence."""

        with self._lock:
            session = self._active.get(instance_id)
            return None if session is None else session.session_id


LIVE_DISPLAY_REGISTRY = LiveDisplaySessionRegistry()


def cancel_active_live_display(instance_id: UUID) -> bool:
    """Signal cancellation without waiting; lifecycle authority always proceeds."""

    return LIVE_DISPLAY_REGISTRY.cancel(instance_id)


class ManagedLiveDisplayService:
    def __init__(
        self,
        broker: _Broker,
        status: ManagedRuntimeStatusService,
        reconciliation: ManagedInstanceReconciliationService,
        screenshots: ManagedScreenshotService,
        *,
        registry: LiveDisplaySessionRegistry = LIVE_DISPLAY_REGISTRY,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._broker = broker
        self._status = status
        self._reconciliation = reconciliation
        self._screenshots = screenshots
        self._registry = registry
        self._clock = clock
        self._monotonic = monotonic

    def begin(self, instance_id: UUID) -> LiveDisplaySession:
        runtime = self._status.get(instance_id)
        visual = self._screenshots.get_status(instance_id)
        if not visual.display_supported:
            raise LiveDisplayError(
                "display_unsupported",
                "Live display is not supported for this runtime revision.",
            )
        self._require_available(instance_id, runtime)
        container_id = self._eligible_container(instance_id)
        session = LiveDisplaySession(
            session_id=uuid4(),
            instance_id=instance_id,
            runtime_revision_id=runtime.runtime_revision_id,
            container_id=container_id,
            started_at=self._clock(),
            started_monotonic=self._monotonic(),
        )
        self._registry.acquire(session)
        return session

    def mark_established(self, session: LiveDisplaySession) -> None:
        session.established = True
        LOGGER.info(
            "managed live display started",
            extra={
                "display_session_id": str(session.session_id),
                "managed_instance_id": str(session.instance_id),
                "interface": "web",
                "actor": "local_admin",
                "started_at": session.started_at.isoformat(),
                "result": "started",
            },
        )

    def active_session_id(self, instance_id: UUID) -> UUID | None:
        return self._registry.session_id(instance_id)

    def capture(self, session: LiveDisplaySession) -> EphemeralPngFrame:
        self._validate_session(session)
        try:
            frame = self._screenshots.capture_ephemeral(
                session.instance_id,
                expected_runtime_revision_id=session.runtime_revision_id,
                expected_container_id=session.container_id,
            )
        except ScreenshotServiceError as error:
            if error.code in {
                "screenshot_capture_in_progress",
                "visual_rate_limited",
            }:
                raise LiveDisplayFrameSkipped from error
            if error.code in {
                "visual_instance_not_running",
                "visual_kodi_not_ready",
                "screenshot_unsupported",
            }:
                mapping = {
                    "visual_instance_not_running": "display_instance_not_running",
                    "visual_kodi_not_ready": "display_kodi_not_ready",
                    "screenshot_unsupported": "display_unsupported",
                }
                raise LiveDisplayError(
                    mapping[error.code], "Live display is no longer available."
                ) from error
            raise LiveDisplayError(
                "display_backend_failed", "The live-display backend failed safely."
            ) from error
        except (ClientError, OSError, ScreenshotArtifactError) as error:
            raise LiveDisplayError(
                "display_backend_failed", "The live-display backend failed safely."
            ) from error
        self._validate_session(session)
        return frame

    def record_delivery(self, session: LiveDisplaySession, byte_count: int) -> None:
        session.frame_count += 1
        session.aggregate_bytes += byte_count

    def end(self, session: LiveDisplaySession, reason: str) -> None:
        if session.closed:
            return
        session.closed = True
        self._registry.release(session)
        ended_at = self._clock()
        LOGGER.info(
            "managed live display ended",
            extra={
                "display_session_id": str(session.session_id),
                "managed_instance_id": str(session.instance_id),
                "interface": "web",
                "actor": "local_admin",
                "started_at": session.started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "result": reason,
                "established": session.established,
                "frame_count": session.frame_count,
                "aggregate_byte_count": session.aggregate_bytes,
            },
        )

    def _validate_session(self, session: LiveDisplaySession) -> None:
        if session.cancelled.is_set():
            raise LiveDisplayError(
                "display_unavailable", "Live display was cancelled by a runtime change."
            )
        if (
            self._monotonic() - session.started_monotonic
            >= DISPLAY_SESSION_LIFETIME_SECONDS
        ):
            raise LiveDisplayError(
                "display_session_expired", "The live-display session has expired."
            )
        try:
            runtime = self._status.get(session.instance_id)
        except ManagedInstanceNotFoundError as error:
            raise LiveDisplayError(
                "display_unavailable", "The managed instance is no longer available."
            ) from error
        if runtime.runtime_revision_id != session.runtime_revision_id:
            raise LiveDisplayError(
                "display_unavailable", "Managed runtime identity changed."
            )
        try:
            visual = self._screenshots.get_status(session.instance_id)
        except ManagedInstanceNotFoundError as error:
            raise LiveDisplayError(
                "display_unavailable", "The managed instance is no longer available."
            ) from error
        if not visual.display_supported:
            raise LiveDisplayError(
                "display_unsupported", "Live display support changed."
            )
        self._require_available(session.instance_id, runtime)
        if self._eligible_container(session.instance_id) != session.container_id:
            raise LiveDisplayError(
                "display_unavailable", "Managed runtime identity changed."
            )

    def _require_available(
        self, instance_id: UUID, runtime: ManagedRuntimeStatus
    ) -> None:
        if runtime.container_state is not ContainerRuntimeState.RUNNING:
            raise LiveDisplayError(
                "display_instance_not_running", "The managed instance is not running."
            )
        if (
            runtime.process_state is not KodiProcessState.RUNNING
            or runtime.usability_state is not SemanticUsabilityState.USABLE
        ):
            raise LiveDisplayError(
                "display_kodi_not_ready", "Kodi is not healthy and usable."
            )
        if self._reconciliation.status(instance_id).blocking:
            raise LiveDisplayError(
                "display_unavailable",
                "Live display is blocked pending runtime reconciliation.",
            )

    def _eligible_container(self, instance_id: UUID) -> str:
        try:
            inventory = self._broker.managed_container_inventory()
        except ClientError as error:
            raise LiveDisplayError(
                "display_unavailable", "Managed runtime inventory is unavailable."
            ) from error
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
            raise LiveDisplayError(
                "display_unavailable", "The exact healthy runtime is unavailable."
            )
        return matches[0].container_id
