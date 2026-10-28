"""Explicit managed-only view-only live preview with ephemeral authorization."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event, Lock
from typing import Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatus,
    ManagedRuntimeStatusService,
    SemanticUsabilityState,
)
from kodi_lab.application.screenshots import (
    EphemeralJpegFrame,
    ManagedScreenshotService,
    ScreenshotIdentityChangedError,
    ScreenshotServiceError,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import ClientError
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.domain import InstanceType, ManagedInstanceMutationKind
from kodi_lab.kodi.screenshots import ScreenshotArtifactError
from kodi_lab.storage.development_sessions import active_session
from kodi_lab.storage.models import AgentWorkspaceRow, ManagedInstanceRow
from kodi_lab.storage.repositories import (
    ManagedInstanceMutationLeaseRepository,
    ManagedRuntimeProjectionRepository,
)

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
    owner: str = ""
    policy_id: UUID | None = None
    authority: tuple[object, ...] = ()
    target_fps: int = 1
    period_seconds: float = 1.0
    worker_pending: bool = False
    worker_lock: Lock = field(default_factory=Lock)
    skipped_frames: int = 0
    capture_ms: float = 0.0
    encode_ms: float = 0.0
    send_ms: float = 0.0
    decode_ms: float = 0.0
    recent_worker_seconds: float = 0.0
    previous_delivery: dict[str, float] | None = None
    novnc_mode: Literal["view-only", "interactive"] | None = None


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

    def cancel(
        self,
        instance_id: UUID,
        *,
        policy_id: UUID | None = None,
        owner: str | None = None,
    ) -> bool:
        with self._lock:
            session = self._active.get(instance_id)
            if (
                session is None
                or (policy_id is not None and session.policy_id != policy_id)
                or (
                    session is not None and owner is not None and session.owner != owner
                )
            ):
                return False
            session.cancelled.set()
            return True

    def commit_probe(
        self, probe: LiveDisplaySession, commit: Callable[[], None]
    ) -> None:
        with self._lock:
            if (
                self._active.get(probe.instance_id) is not probe
                or probe.cancelled.is_set()
            ):
                raise LiveDisplayError(
                    "display_policy_denied", "Preview probe was cancelled."
                )
            commit()

    def cancel_for_mutation(
        self,
        instance_id: UUID,
        kind: ManagedInstanceMutationKind,
        db: Session,
        operation_id: UUID,
    ) -> bool:
        with self._lock:
            session = self._active.get(instance_id)
            if session is None:
                return False
            if session.novnc_mode == "view-only" and kind in {
                ManagedInstanceMutationKind.MCP_WORKSPACE_LAUNCH,
                ManagedInstanceMutationKind.MCP_WORKSPACE_EXECUTION,
            }:
                from kodi_lab.application.novnc import CatalogViewAuthority
                from kodi_lab.broker.novnc import ViewBinding

                current = active_session(db)
                instance = db.get(ManagedInstanceRow, str(instance_id))
                lease = ManagedInstanceMutationLeaseRepository(db).current(instance_id)
                if (
                    current is not None
                    and instance is not None
                    and lease is not None
                    and lease.operation_id == str(operation_id)
                    and current.target_id == str(instance_id)
                    and current.phase == "bound"
                    and current.container_id == session.container_id
                    and CatalogViewAuthority._operation_context(
                        db,
                        lease,
                        current,
                        instance,
                        ViewBinding(session.container_id, "", "", session.authority),
                    )
                ):
                    workspace = db.get(AgentWorkspaceRow, current.workspace_id)
                    if workspace is not None and session.authority == (
                        instance.runtime_revision_id,
                        instance.runtime_state_revision,
                        current.generation,
                        current.workspace_id,
                        current.source_id,
                        workspace.generation,
                    ):
                        # Reserve and claim publish atomically. Renewal requires
                        # the claimed owner; this transaction checks exact scope.
                        return False
            session.cancelled.set()
            return True

    def cancel_owner(self, owner: str) -> None:
        with self._lock:
            for session in self._active.values():
                if session.owner == owner:
                    session.cancelled.set()

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


def cancel_live_display_for_mutation(
    instance_id: UUID,
    kind: ManagedInstanceMutationKind,
    db: Session,
    operation_id: UUID,
) -> bool:
    return LIVE_DISPLAY_REGISTRY.cancel_for_mutation(
        instance_id, kind, db, operation_id
    )


@dataclass(slots=True)
class PreviewGrant:
    id: UUID
    instance_id: UUID
    owner: str
    authority: tuple[object, ...]
    target_fps: int
    expires: float
    enabled: bool = False
    connected: bool = False
    cancelled: Event = field(default_factory=Event)


class ManagedPreviewPolicy:
    """Default-disabled process-local authorization, distinct from external policy.

    One explicit Start authorizes a native probe and, only after that succeeds,
    one exact Web-session connection. Restart and any revocation erase authority.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._grants: dict[UUID, PreviewGrant] = {}
        # Bounded single-use attempt tombstones preserve Stop-before-Start.
        # Never evict a cancellation to make room for a delayed request.
        self._attempts: set[tuple[str, UUID, UUID]] = set()
        self._closed = False

    def prepare(
        self,
        instance_id: UUID,
        owner: str,
        authority: tuple[object, ...],
        target_fps: int,
        now: float,
        *,
        attempt_id: UUID | None = None,
        cancellation: Event | None = None,
    ) -> PreviewGrant:
        if target_fps not in {1, 3}:
            raise LiveDisplayError("display_policy_denied", "Unsupported preview rate.")
        with self._lock:
            attempt_id = attempt_id or uuid4()
            key = (owner, instance_id, attempt_id)
            if (
                self._closed
                or (owner, UUID(int=0), UUID(int=0)) in self._attempts
                or key in self._attempts
                or len(self._attempts) >= 4096
            ):
                raise LiveDisplayError(
                    "display_policy_denied",
                    "Preview attempt is retired or capacity is exhausted.",
                )
            self._attempts.add(key)
            self._grants = {
                key: value for key, value in self._grants.items() if value.expires > now
            }
            if instance_id in self._grants:
                raise LiveDisplayError(
                    "display_conflict", "Stop the existing preview authorization first."
                )
            if len(self._grants) >= 2:
                raise LiveDisplayError(
                    "display_rate_limited", "Preview authorization capacity reached."
                )
            grant = PreviewGrant(
                attempt_id,
                instance_id,
                owner,
                authority,
                target_fps,
                now + 15.0,
                cancelled=cancellation or Event(),
            )
            self._grants[instance_id] = grant
            return grant

    def commit(self, grant: PreviewGrant, now: float) -> None:
        with self._lock:
            if (
                self._grants.get(grant.instance_id) is not grant
                or grant.expires <= now
                or grant.cancelled.is_set()
            ):
                raise LiveDisplayError(
                    "display_policy_denied", "Preview authorization was revoked."
                )
            grant.enabled = True

    def require(
        self,
        instance_id: UUID,
        owner: str,
        authority: tuple[object, ...],
        now: float,
        *,
        policy_id: UUID | None = None,
    ) -> PreviewGrant:
        with self._lock:
            grant = self._grants.get(instance_id)
            if (
                self._closed
                or grant is None
                or not grant.enabled
                or grant.cancelled.is_set()
                or grant.expires <= now
                or grant.owner != owner
                or grant.authority != authority
                or (policy_id is not None and grant.id != policy_id)
            ):
                raise LiveDisplayError(
                    "display_policy_denied",
                    "Live preview is disabled or its exact authorization changed.",
                )
            return grant

    def connect(self, grant: PreviewGrant, now: float) -> None:
        with self._lock:
            if (
                self._grants.get(grant.instance_id) is not grant
                or not grant.enabled
                or grant.cancelled.is_set()
                or grant.connected
                or grant.expires <= now
            ):
                raise LiveDisplayError(
                    "display_policy_denied", "Preview authorization is unavailable."
                )
            grant.connected = True
            grant.expires = now + DISPLAY_SESSION_LIFETIME_SECONDS

    def revoke(self, instance_id: UUID, *, policy_id: UUID | None = None) -> None:
        with self._lock:
            grant = self._grants.get(instance_id)
            if grant is not None and (policy_id is None or grant.id == policy_id):
                del self._grants[instance_id]

    def cancel_attempt(self, instance_id: UUID, owner: str, attempt_id: UUID) -> bool:
        with self._lock:
            key = (owner, instance_id, attempt_id)
            if key not in self._attempts:
                if len(self._attempts) >= 4096:
                    raise LiveDisplayError(
                        "display_policy_denied",
                        "Preview attempt capacity is exhausted.",
                    )
                self._attempts.add(key)
            grant = self._grants.get(instance_id)
            if grant is not None and grant.owner == owner and grant.id == attempt_id:
                del self._grants[instance_id]
                return True
            return False

    def retire_owner(self, owner: str) -> tuple[tuple[UUID, UUID], ...]:
        with self._lock:
            marker = (owner, UUID(int=0), UUID(int=0))
            if len(self._attempts) >= 4096 and marker not in self._attempts:
                self._closed = True
            else:
                self._attempts.add(marker)
            ids = tuple(
                (key, value.id)
                for key, value in self._grants.items()
                if value.owner == owner
            )
            for key, _revision in ids:
                del self._grants[key]
            return ids


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
        self._policy = ManagedPreviewPolicy()
        self._sessions = screenshots._session_factory

    def _authority(
        self, instance_id: UUID, revision_id: UUID, container_id: str
    ) -> tuple[object, ...]:
        with self._sessions() as db:
            instance = db.get(ManagedInstanceRow, str(instance_id))
            token = ManagedRuntimeProjectionRepository(db).observation_token(
                instance_id
            )
            if (
                instance is None
                or instance.instance_type is not InstanceType.DISPOSABLE
                or instance.runtime_revision_id != str(revision_id)
                or token is None
                or token.runtime_revision_id != revision_id
            ):
                raise LiveDisplayError(
                    "display_unavailable", "Managed runtime identity changed."
                )
            if (
                ManagedInstanceMutationLeaseRepository(db).current(instance_id)
                is not None
            ):
                raise LiveDisplayError(
                    "display_unavailable", "Managed runtime is busy."
                )
            current = active_session(db)
            selection: tuple[object, ...] = (None,)
            if current is not None:
                if (
                    current.phase != "bound"
                    or current.target_id != str(instance_id)
                    or current.runtime_revision_id != str(revision_id)
                    or current.container_id != container_id
                ):
                    raise LiveDisplayError(
                        "display_unavailable", "Development session identity changed."
                    )
                selection = (
                    current.generation,
                    current.workspace_id,
                    current.source_id,
                    current.container_id,
                )
            return (
                str(revision_id),
                container_id,
                token.runtime_state_revision,
                *selection,
            )

    def enable(
        self,
        instance_id: UUID,
        owner: str,
        *,
        target_fps: int = 1,
        attempt_id: UUID | None = None,
        authorize: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        runtime = self._status.get(instance_id)
        visual = self._screenshots.get_status(instance_id, include_cache=False)
        if not visual.display_supported:
            raise LiveDisplayError(
                "display_unsupported",
                "This exact runtime is not reviewed for preview qualification.",
            )
        self._require_available(instance_id, runtime)
        container = self._eligible_container(instance_id)
        authority = self._authority(instance_id, runtime.runtime_revision_id, container)
        attempt_id = attempt_id or uuid4()
        probe = LiveDisplaySession(
            session_id=uuid4(),
            instance_id=instance_id,
            runtime_revision_id=runtime.runtime_revision_id,
            container_id=container,
            started_at=self._clock(),
            started_monotonic=self._monotonic(),
            owner=owner,
            policy_id=attempt_id,
            authority=authority,
        )
        # Reserve actual probe work independently of policy expiry/revocation.
        # Only this synchronous worker's finally releases its capacity.
        if self._registry.session_id(instance_id) is not None:
            raise LiveDisplayError(
                "display_conflict", "A preview or its capture cleanup remains active."
            )
        self._registry.acquire(probe)
        grant: PreviewGrant | None = None
        try:
            grant = self._policy.prepare(
                instance_id,
                owner,
                authority,
                target_fps,
                self._monotonic(),
                attempt_id=attempt_id,
                cancellation=probe.cancelled,
            )
            if probe.cancelled.is_set():
                raise LiveDisplayError(
                    "display_policy_denied", "Preview was cancelled."
                )
            if authorize is not None and not authorize():
                raise LiveDisplayError(
                    "display_session_expired", "The Web session expired."
                )
            # Explicit Start qualifies this ACTUAL native revision/container with
            # one discarded JPEG probe before enabling streaming authority.
            frame = self._screenshots.capture_preview(
                instance_id,
                expected_runtime_revision_id=runtime.runtime_revision_id,
                expected_container_id=container,
            )
            if (
                frame.container_id != container
                or frame.runtime_revision_id != runtime.runtime_revision_id
                or self._authority(instance_id, runtime.runtime_revision_id, container)
                != authority
            ):
                raise LiveDisplayError(
                    "display_unavailable", "Native preview identity changed."
                )
            self._require_available(instance_id, self._status.get(instance_id))
            if authorize is not None and not authorize():
                raise LiveDisplayError(
                    "display_session_expired", "The Web session expired."
                )
            self._registry.commit_probe(
                probe, lambda: self._policy.commit(grant, self._monotonic())
            )
        except BaseException:
            if grant is not None:
                self._policy.revoke(instance_id, policy_id=grant.id)
            raise
        finally:
            self._registry.release(probe)
        assert grant is not None
        return {
            "enabled": True,
            "qualification": "native_jpeg_verified",
            "runtime_revision_id": str(runtime.runtime_revision_id),
            "container_id": container,
            "width": frame.width,
            "height": frame.height,
            "target_fps": target_fps,
            "policy_revision": str(grant.id),
        }

    def disable(self, instance_id: UUID, *, policy_id: UUID | None = None) -> None:
        self._policy.revoke(instance_id, policy_id=policy_id)
        self._registry.cancel(instance_id, policy_id=policy_id)

    def disable_attempt(self, instance_id: UUID, owner: str, attempt_id: UUID) -> None:
        self._policy.cancel_attempt(instance_id, owner, attempt_id)
        # UUID correlation prevents an old tab/request from stopping a new grant.
        self._registry.cancel(instance_id, policy_id=attempt_id, owner=owner)

    def retire_owner(self, owner: str) -> None:
        self._policy.retire_owner(owner)
        # Also retire probes admitted before their policy preparation began.
        self._registry.cancel_owner(owner)

    def begin(self, instance_id: UUID, owner: str = "") -> LiveDisplaySession:
        runtime = self._status.get(instance_id)
        visual = self._screenshots.get_status(instance_id, include_cache=False)
        if not visual.display_supported:
            raise LiveDisplayError(
                "display_unsupported",
                "Live display is not supported for this runtime revision.",
            )
        self._require_available(instance_id, runtime)
        container_id = self._eligible_container(instance_id)
        authority = self._authority(
            instance_id, runtime.runtime_revision_id, container_id
        )
        grant = self._policy.require(instance_id, owner, authority, self._monotonic())
        session = LiveDisplaySession(
            session_id=uuid4(),
            instance_id=instance_id,
            runtime_revision_id=runtime.runtime_revision_id,
            container_id=container_id,
            started_at=self._clock(),
            started_monotonic=self._monotonic(),
            owner=owner,
            authority=authority,
            policy_id=grant.id,
            target_fps=grant.target_fps,
        )
        self._registry.acquire(session)
        try:
            self._policy.connect(grant, self._monotonic())
        except BaseException:
            self._registry.release(session)
            raise
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

    def capture(self, session: LiveDisplaySession) -> EphemeralJpegFrame:
        # Screenshot capture already checks current eligibility, exact broker
        # identities and post-read container identity. Keep the cheap revocable
        # authority barriers here; full delivery validation still runs after
        # metadata and immediately before pixel submission.
        self.check_current(session)
        try:
            frame = self._screenshots.capture_preview(
                session.instance_id,
                expected_runtime_revision_id=session.runtime_revision_id,
                expected_container_id=session.container_id,
            )
        except ScreenshotIdentityChangedError as error:
            raise LiveDisplayError(
                "display_unavailable", "Managed runtime identity changed."
            ) from error
        except ScreenshotServiceError as error:
            if error.code in {
                "screenshot_capture_in_progress",
                "visual_rate_limited",
            }:
                raise LiveDisplayFrameSkipped from error
            if error.code in {
                "display_unavailable",
                "visual_instance_not_running",
                "visual_kodi_not_ready",
                "screenshot_unsupported",
            }:
                mapping = {
                    "display_unavailable": "display_unavailable",
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
        self.check_current(session)
        return frame

    def record_delivery(self, session: LiveDisplaySession, byte_count: int) -> None:
        session.frame_count += 1
        session.aggregate_bytes += byte_count

    def reserve_worker(self, session: LiveDisplaySession) -> None:
        with session.worker_lock:
            if session.closed or session.worker_pending:
                raise LiveDisplayError(
                    "display_conflict", "A capture is already outstanding."
                )
            session.worker_pending = True

    def retire_worker(self, session: LiveDisplaySession) -> None:
        with session.worker_lock:
            session.worker_pending = False
            if session.closed:
                self._registry.release(session)

    def check_current(self, session: LiveDisplaySession) -> None:
        """Fast authorization/identity barrier during waits and before delivery."""
        if session.closed or session.cancelled.is_set():
            raise LiveDisplayError("display_unavailable", "Preview was cancelled.")
        if (
            self._monotonic() - session.started_monotonic
            >= DISPLAY_SESSION_LIFETIME_SECONDS
        ):
            raise LiveDisplayError("display_session_expired", "Preview expired.")
        authority = self._authority(
            session.instance_id, session.runtime_revision_id, session.container_id
        )
        if authority != session.authority:
            raise LiveDisplayError("display_unavailable", "Preview identity changed.")
        self._policy.require(
            session.instance_id,
            session.owner,
            authority,
            self._monotonic(),
            policy_id=session.policy_id,
        )

    def end(self, session: LiveDisplaySession, reason: str) -> None:
        with session.worker_lock:
            if session.closed:
                return
            session.closed = True
            session.cancelled.set()
            self._policy.revoke(session.instance_id, policy_id=session.policy_id)
            if not session.worker_pending:
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
                "skipped_frames": session.skipped_frames,
                "capture_ms": round(session.capture_ms, 2),
                "encode_ms": round(session.encode_ms, 2),
                "send_ms": round(session.send_ms, 2),
                "decode_ms": round(session.decode_ms, 2),
            },
        )

    def validate_delivery(self, session: LiveDisplaySession) -> None:
        """Fresh bounded eligibility/inventory recheck, without retained PNG I/O."""
        self._validate_session(session)

    def _validate_session(self, session: LiveDisplaySession) -> None:
        self.check_current(session)
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
            visual = self._screenshots.get_status(
                session.instance_id, include_cache=False
            )
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
