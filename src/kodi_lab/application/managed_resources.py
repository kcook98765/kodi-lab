"""Managed-only capacity presentation and conservative, ordinary lifecycle idle stop."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.lifecycle_operations import RunningLifecycleOperation
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.operations import (
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.domain import (
    ManagedInstanceLifecycleOperationState,
    RuntimeExecutionProfile,
    TargetType,
)
from kodi_lab.kodi.remote import KodiBrowserRemoteAdapter
from kodi_lab.storage.managed_activity import (
    read_resource_policy,
    record_activity,
    resource_policy,
    timestamp,
)
from kodi_lab.storage.models import (
    ManagedIdleStateRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedInstanceUseRow,
    ManagedStartReservationRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    ManagedRuntimeObservationToken,
    ManagedRuntimeProjectionRepository,
    RuntimeExecutionProfileRepository,
)


class ResourcePolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    max_running: int = Field(ge=1, le=64)
    idle_minutes: int | None = Field(ge=1, le=10080)


class ResourceCapacity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    used: int = Field(ge=0)
    limit: int = Field(ge=1, le=64)
    idle_minutes: int | None
    available: bool


class ManagedResourceProtected(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Broker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...
    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...


class _Playback(Protocol):
    def playback_active(self) -> bool: ...


type PlaybackFactory = Callable[
    [ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile], _Playback
]


class ManagedResourceService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        broker: _Broker,
        lifecycle: ManagedRuntimeLifecycleService,
        *,
        playback_factory: PlaybackFactory = KodiBrowserRemoteAdapter,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions, self._broker, self._lifecycle = sessions, broker, lifecycle
        self._playback_factory, self._clock = playback_factory, clock
        self._reconciliation = ManagedInstanceReconciliationService(sessions)

    @staticmethod
    def require_managed(session: Session, instance_id: UUID) -> ManagedInstanceRow:
        target = session.get(TargetRow, str(instance_id))
        instance = session.get(ManagedInstanceRow, str(instance_id))
        if (
            target is None
            or target.target_type is not TargetType.MANAGED
            or instance is None
        ):
            raise ManagedResourceProtected("managed_target_required")
        return instance

    def configure(self, value: ResourcePolicyInput) -> ResourcePolicyInput:
        with self._sessions.begin() as session:
            policy = resource_policy(session)
            policy.max_running, policy.idle_minutes = (
                value.max_running,
                value.idle_minutes,
            )
        return value

    def capacity(self) -> ResourceCapacity:
        records = self._broker.managed_container_inventory().containers
        with self._sessions.begin() as session:
            policy = read_resource_policy(session)
            used = {
                item.managed_instance_id
                for item in records
                if item.runtime_state is not ContainerRuntimeState.STOPPED
            }
            used.update(
                session.scalars(select(ManagedStartReservationRow.managed_instance_id))
            )
            return ResourceCapacity(
                used=len(used),
                limit=policy.max_running,
                idle_minutes=policy.idle_minutes,
                available=len(used) < policy.max_running,
            )

    def enroll(self, instance_id: UUID, *, enabled: bool) -> None:
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            self.require_managed(session, instance_id)
            state = session.get(ManagedIdleStateRow, str(instance_id))
            if state is not None and state.enrolled == enabled:
                return
            if state is None:
                state = ManagedIdleStateRow(
                    managed_instance_id=str(instance_id), enrolled=enabled
                )
                session.add(state)
            state.enrolled = enabled
            if enabled:
                state.last_activity_at = timestamp(self._clock())
                state.last_stop_attempt_at = None

    def keep_awake(self, instance_id: UUID, *, minutes: int) -> None:
        if type(minutes) is not int or not 0 <= minutes <= 240:
            raise ValueError("keep-awake duration must be zero to 240 minutes")
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            self.require_managed(session, instance_id)
            if minutes:
                lease = session.get(ManagedInstanceMutationLeaseRow, str(instance_id))
                operation = (
                    None
                    if lease is None
                    else session.get(
                        ManagedInstanceLifecycleOperationRow, lease.operation_id
                    )
                )
                if operation is not None and operation.kind.value == "stop":
                    raise ManagedResourceProtected("stop_already_claimed")
                record_activity(session, instance_id, actor="web", now=self._clock())
                session.flush()
            use = session.get(ManagedInstanceUseRow, (str(instance_id), "web"))
            if use is not None:
                use.hold_until = (
                    timestamp(self._clock() + timedelta(minutes=minutes))
                    if minutes
                    else None
                )
                if not minutes:
                    use.released_at = timestamp(self._clock())

    def status(self, instance_id: UUID) -> dict[str, Any]:
        with self._sessions.begin() as session:
            self.require_managed(session, instance_id)
            policy = read_resource_policy(session)
            state = session.get(ManagedIdleStateRow, str(instance_id))
            holds = [
                item.hold_until
                for item in session.scalars(
                    select(ManagedInstanceUseRow).where(
                        ManagedInstanceUseRow.managed_instance_id == str(instance_id),
                        ManagedInstanceUseRow.hold_until > timestamp(self._clock()),
                    )
                )
                if item.hold_until is not None
            ]
            activity = None if state is None else state.last_activity_at
            deadline = None
            if (
                state is not None
                and state.enrolled
                and policy.idle_minutes is not None
                and activity is not None
            ):
                deadline = timestamp(
                    datetime.fromisoformat(activity.replace("Z", "+00:00"))
                    + timedelta(minutes=policy.idle_minutes)
                )
            return {
                "enrolled": state is not None and state.enrolled,
                "idle_minutes": policy.idle_minutes,
                "last_activity_at": activity,
                "idle_deadline": deadline,
                "keep_awake_until": max(holds, default=None),
                "last_stopped_at": None if state is None else state.last_stopped_at,
                "last_stop_reason": None if state is None else state.last_stop_reason,
                "stop_requires_review": state is not None
                and state.last_stop_attempt_at is not None
                and (
                    state.last_stopped_at is None
                    or state.last_stop_attempt_at > state.last_stopped_at
                ),
            }

    def _guard(
        self,
        session: Session,
        instance_id: UUID,
        *,
        reason: str,
        actor: str | None,
        own_operation_id: UUID | None = None,
    ) -> None:
        self.require_managed(session, instance_id)
        lease = session.get(ManagedInstanceMutationLeaseRow, str(instance_id))
        if lease is not None and lease.operation_id != str(own_operation_id):
            raise ManagedResourceProtected("active_operation")
        if ManagedRuntimeProjectionRepository(
            session
        ).has_blocking_runtime_reconciliation(instance_id):
            raise ManagedResourceProtected("operation_requires_reconciliation")
        if self._reconciliation.status(instance_id).required:
            raise ManagedResourceProtected("operation_requires_reconciliation")
        if session.get(ManagedStartReservationRow, str(instance_id)) is not None:
            raise ManagedResourceProtected("start_requires_reconciliation")
        now = self._clock()
        policy = read_resource_policy(session)
        state = session.get(ManagedIdleStateRow, str(instance_id))
        for use in session.scalars(
            select(ManagedInstanceUseRow).where(
                ManagedInstanceUseRow.managed_instance_id == str(instance_id)
            )
        ):
            if use.hold_until is not None and use.hold_until > timestamp(now):
                raise ManagedResourceProtected("keep_awake")
            if (
                reason == "released"
                and use.actor_key != actor
                and (use.released_at is None or use.last_activity_at > use.released_at)
                and datetime.fromisoformat(use.last_activity_at.replace("Z", "+00:00"))
                + timedelta(minutes=policy.idle_minutes or 60)
                > now
            ):
                raise ManagedResourceProtected("other_client_active")
        if reason == "idle":
            if state is None or not state.enrolled or policy.idle_minutes is None:
                raise ManagedResourceProtected("idle_stop_not_enabled")
            if state.last_activity_at is None:
                raise ManagedResourceProtected("activity_unknown")
            if (
                datetime.fromisoformat(state.last_activity_at.replace("Z", "+00:00"))
                + timedelta(minutes=policy.idle_minutes)
                > now
            ):
                raise ManagedResourceProtected("recent_activity")
            if (
                own_operation_id is None
                and state.last_stop_attempt_at is not None
                and state.last_stop_attempt_at >= state.last_activity_at
            ):
                raise ManagedResourceProtected("previous_stop_attempt_requires_review")

    def _playback_evidence(
        self,
        instance_id: UUID,
        *,
        expected_container_id: str | None = None,
    ) -> tuple[ManagedRuntimeObservationToken, str]:
        with self._sessions() as session:
            instance = self.require_managed(session, instance_id)
            token = ManagedRuntimeProjectionRepository(session).observation_token(
                instance_id
            )
            profile = RuntimeExecutionProfileRepository(session).get(
                UUID(instance.runtime_revision_id)
            )
        if token is None or profile is None:
            raise ManagedResourceProtected("playback_unknown")
        records = [
            item
            for item in self._broker.managed_container_inventory().containers
            if item.managed_instance_id == str(instance_id)
        ]
        if (
            len(records) != 1
            or records[0].runtime_state is not ContainerRuntimeState.RUNNING
            or records[0].health_state is not ContainerHealthState.HEALTHY
            or (
                expected_container_id is not None
                and records[0].container_id != expected_container_id
            )
        ):
            raise ManagedResourceProtected("runtime_not_ready")
        runtime = self._broker.observe_managed_kodi_runtime(
            managed_instance_id=str(instance_id),
            expected_container_id=records[0].container_id,
        )
        if (
            runtime.runtime_revision_id != str(token.runtime_revision_id)
            or runtime.process_state is not KodiProcessState.RUNNING
            or runtime.container_id != records[0].container_id
        ):
            raise ManagedResourceProtected("runtime_not_ready")
        try:
            active = self._playback_factory(runtime, profile).playback_active()
        except Exception as error:
            raise ManagedResourceProtected("playback_unknown") from error
        if active is not False:
            raise ManagedResourceProtected("playback_active")
        with self._sessions() as session:
            if (
                ManagedRuntimeProjectionRepository(session).observation_token(
                    instance_id
                )
                != token
            ):
                raise ManagedResourceProtected("runtime_changed")
        return token, runtime.container_id

    def stop_if_eligible(
        self,
        instance_id: UUID,
        *,
        reason: Literal["idle", "released"],
        actor: str | None = None,
        operation_id: UUID | None = None,
        authorize: Callable[[Session], None] | None = None,
        on_claim: Callable[[Session, RunningLifecycleOperation], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> str:
        operation_id = operation_id or uuid4()
        with self._sessions.begin() as session:
            if authorize is not None:
                authorize(session)
            self._guard(session, instance_id, reason=reason, actor=actor)
        original_token, container_id = self._playback_evidence(instance_id)

        def precondition(session: Session) -> None:
            if cancelled is not None and cancelled():
                raise ManagedResourceProtected("service_stopping")
            if authorize is not None:
                authorize(session)
            self._guard(session, instance_id, reason=reason, actor=actor)
            if (
                ManagedRuntimeProjectionRepository(session).observation_token(
                    instance_id
                )
                != original_token
            ):
                raise ManagedResourceProtected("runtime_changed")

        def claimed(session: Session, handle: RunningLifecycleOperation) -> None:
            state = session.get(ManagedIdleStateRow, str(instance_id))
            if state is None:
                state = ManagedIdleStateRow(
                    managed_instance_id=str(instance_id), enrolled=False
                )
                session.add(state)
            state.last_stop_attempt_at = timestamp(self._clock())
            state.last_stop_operation_id = str(operation_id)
            if on_claim is not None:
                on_claim(session, handle)

        dispatch_guard_passed = False

        def before_dispatch() -> None:
            nonlocal dispatch_guard_passed
            with self._sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                if authorize is not None:
                    authorize(session)
                self._guard(
                    session,
                    instance_id,
                    reason=reason,
                    actor=actor,
                    own_operation_id=operation_id,
                )
            self._playback_evidence(instance_id, expected_container_id=container_id)
            with self._sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                if authorize is not None:
                    authorize(session)
                self._guard(
                    session,
                    instance_id,
                    reason=reason,
                    actor=actor,
                    own_operation_id=operation_id,
                )
            if cancelled is not None and cancelled():
                raise ManagedResourceProtected("service_stopping")
            dispatch_guard_passed = True

        try:
            result = self._lifecycle.stop(
                instance_id,
                operation_id=operation_id,
                precondition=precondition,
                on_claim=claimed,
                before_dispatch=before_dispatch,
            )
        except ManagedResourceProtected:
            # A proven predispatch guard refusal may be evaluated again later.
            # Unknown Docker/lifecycle outcomes keep the attempt and normal fence.
            with self._sessions.begin() as session:
                state = session.get(ManagedIdleStateRow, str(instance_id))
                if (
                    not dispatch_guard_passed
                    and state is not None
                    and state.last_stop_operation_id == str(operation_id)
                ):
                    state.last_stop_attempt_at = None
            raise
        with self._sessions.begin() as session:
            operation = session.get(
                ManagedInstanceLifecycleOperationRow, str(operation_id)
            )
            if (
                result.container_state is not ContainerRuntimeState.STOPPED
                or operation is None
                or operation.state
                is not ManagedInstanceLifecycleOperationState.SUCCEEDED
            ):
                raise ManagedResourceProtected("stop_requires_reconciliation")
            state = session.get(ManagedIdleStateRow, str(instance_id))
            assert state is not None
            state.last_stopped_at, state.last_stop_reason = (
                timestamp(self._clock()),
                reason,
            )
        return "stopped"

    def sweep_idle(self, *, cancelled: Callable[[], bool] | None = None) -> int:
        with self._sessions() as session:
            candidates = list(
                session.scalars(
                    select(ManagedIdleStateRow.managed_instance_id).where(
                        ManagedIdleStateRow.enrolled.is_(True)
                    )
                )
            )
        stopped = 0
        for identity in candidates:
            if cancelled is not None and cancelled():
                break
            try:
                self.stop_if_eligible(
                    UUID(identity), reason="idle", cancelled=cancelled
                )
            except Exception:
                # Each instance retains its own operation and attempt evidence.
                continue
            stopped += 1
        return stopped
