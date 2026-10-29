"""Owned ensure-ready/release requests over the ordinary managed lifecycle."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.lifecycle_operations import RunningLifecycleOperation
from kodi_lab.application.managed_resources import (
    ManagedResourceProtected,
    ManagedResourceService,
    ResourceCapacity,
)
from kodi_lab.application.mcp_workspace_development import BoundedIntervalRateLimiter
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatusService,
    ProvisionState,
    SemanticUsabilityState,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.domain import ManagedInstanceLifecycleOperationState
from kodi_lab.storage.managed_activity import record_activity, timestamp
from kodi_lab.storage.models import (
    ManagedAgentLifecycleRequestRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceUseRow,
    ManagedStartReservationRow,
)
from kodi_lab.storage.repositories import ManagedRuntimeProjectionRepository

type AuthorizeLifecycle = Callable[[Session], None]
type ManagedReceiptState = Literal[
    "pending",
    "dispatched",
    "ready",
    "not_ready",
    "released",
    "retained",
    "failed",
    "ambiguous",
]


class ManagedAgentLifecycleError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ManagedAgentLifecycleResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = 1
    request_id: UUID
    operation_id: UUID
    target_id: UUID
    action: Literal["ensure_ready", "release"]
    receipt_state: ManagedReceiptState
    code: str = Field(max_length=64)
    replayed: bool
    lifecycle_operation_id: UUID | None
    ready: bool
    current_state: ContainerRuntimeState
    readiness: SemanticUsabilityState
    hold_until: datetime | None
    capacity: ResourceCapacity | None


class ManagedAgentLifecycleService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        resources: ManagedResourceService,
        lifecycle: ManagedRuntimeLifecycleService,
        status: ManagedRuntimeStatusService,
        *,
        lock_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        rate_limiter: BoundedIntervalRateLimiter | None = None,
    ) -> None:
        self._sessions, self._resources, self._lifecycle, self._status = (
            sessions,
            resources,
            lifecycle,
            status,
        )
        self._locks, self._clock = lock_root, clock
        self._rate = rate_limiter or BoundedIntervalRateLimiter(5.0)

    @contextmanager
    def _lock(self, target_id: UUID) -> Iterator[None]:
        self._locks.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            self._locks / f"{target_id}.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ManagedAgentLifecycleError("target_busy") from error
            yield
        finally:
            os.close(descriptor)

    def ensure_ready(
        self,
        *,
        client_id: UUID,
        request_id: UUID,
        target_id: UUID,
        authorize: AuthorizeLifecycle,
        hold_minutes: int = 15,
    ) -> ManagedAgentLifecycleResult:
        if type(hold_minutes) is not int or not 0 <= hold_minutes <= 240:
            raise ManagedAgentLifecycleError("hold_duration_invalid")
        return self._invoke(
            client_id,
            request_id,
            target_id,
            action="ensure_ready",
            hold_minutes=hold_minutes,
            authorize=authorize,
        )

    def release(
        self,
        *,
        client_id: UUID,
        request_id: UUID,
        target_id: UUID,
        authorize: AuthorizeLifecycle,
    ) -> ManagedAgentLifecycleResult:
        return self._invoke(
            client_id,
            request_id,
            target_id,
            action="release",
            hold_minutes=0,
            authorize=authorize,
        )

    def _invoke(
        self,
        client_id: UUID,
        request_id: UUID,
        target_id: UUID,
        *,
        action: Literal["ensure_ready", "release"],
        hold_minutes: int,
        authorize: AuthorizeLifecycle,
    ) -> ManagedAgentLifecycleResult:
        with self._sessions() as session:
            authorize(session)
            self._resources.require_managed(session, target_id)
        with self._lock(target_id):
            row, replayed = self._claim(
                client_id, request_id, target_id, action, hold_minutes, authorize
            )
            if row.state == "pending":
                try:
                    if action == "ensure_ready":
                        self._ensure(row, authorize)
                    else:
                        self._release(row, authorize)
                except ManagedAgentLifecycleError:
                    raise
                except ManagedResourceProtected as error:
                    self._finish(
                        row.id,
                        state="retained" if action == "release" else "failed",
                        code=error.code,
                    )
                except Exception as error:
                    with self._sessions() as session:
                        latest = session.get(ManagedAgentLifecycleRequestRow, row.id)
                        child = (
                            None
                            if latest is None or latest.lifecycle_operation_id is None
                            else session.get(
                                ManagedInstanceLifecycleOperationRow,
                                latest.lifecycle_operation_id,
                            )
                        )
                    code = getattr(error, "code", None)
                    if code in {
                        "managed_capacity_full",
                        "managed_start_authority_unavailable",
                        "managed_start_identity_unavailable",
                        "managed_capacity_unavailable",
                        "managed_start_requires_reconciliation",
                    }:
                        self._finish(row.id, state="failed", code=code)
                    elif (
                        child is not None
                        and child.state is ManagedInstanceLifecycleOperationState.FAILED
                    ):
                        self._finish(row.id, state="failed", code="lifecycle_failed")
                    elif (
                        latest is not None and latest.lifecycle_operation_id is not None
                    ):
                        self._finish(
                            row.id,
                            state="ambiguous",
                            code="operation_requires_reconciliation",
                        )
                    else:
                        # No child claim was committed, so there is no start/stop
                        # dispatch to retry. Current authority is checked again below.
                        self._finish(
                            row.id, state="failed", code="lifecycle_unavailable"
                        )
            return self._result(row.id, client_id, authorize, replayed=replayed)

    def _claim(
        self,
        client_id: UUID,
        request_id: UUID,
        target_id: UUID,
        action: str,
        hold_minutes: int,
        authorize: AuthorizeLifecycle,
    ) -> tuple[ManagedAgentLifecycleRequestRow, bool]:
        now = self._clock()
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            authorize(session)
            existing = session.scalar(
                select(ManagedAgentLifecycleRequestRow).where(
                    ManagedAgentLifecycleRequestRow.client_id == str(client_id),
                    ManagedAgentLifecycleRequestRow.request_id == str(request_id),
                )
            )
            if existing is not None:
                if (
                    existing.original_instance_id != str(target_id)
                    or existing.action != action
                    or existing.hold_minutes != hold_minutes
                ):
                    raise ManagedAgentLifecycleError("request_conflict")
                session.expunge(existing)
                return existing, True
            if action == "ensure_ready":
                self._retained_admission(session, target_id)
            # Only terminal, unreferenced request receipts expire. Pending and
            # uncertain operations remain available for explicit reconciliation.
            session.execute(
                delete(ManagedAgentLifecycleRequestRow).where(
                    ManagedAgentLifecycleRequestRow.updated_at
                    < timestamp(now - timedelta(days=30)),
                    ManagedAgentLifecycleRequestRow.state.in_(
                        ("ready", "not_ready", "released", "retained", "failed")
                    ),
                )
            )
            total = (
                session.scalar(
                    select(func.count()).select_from(ManagedAgentLifecycleRequestRow)
                )
                or 0
            )
            owned = (
                session.scalar(
                    select(func.count())
                    .select_from(ManagedAgentLifecycleRequestRow)
                    .where(ManagedAgentLifecycleRequestRow.client_id == str(client_id))
                )
                or 0
            )
            if total >= 32768 or owned >= 1024:
                raise ManagedAgentLifecycleError("lifecycle_receipt_quota_exceeded")
            if not self._rate.allow(f"{client_id}:{target_id}:{action}"):
                raise ManagedAgentLifecycleError("rate_limited")
            row = ManagedAgentLifecycleRequestRow(
                id=str(uuid4()),
                client_id=str(client_id),
                request_id=str(request_id),
                original_instance_id=str(target_id),
                managed_instance_id=str(target_id),
                action=action,
                hold_minutes=hold_minutes,
                state="pending",
                created_at=timestamp(now),
                updated_at=timestamp(now),
            )
            session.add(row)
            session.flush()
            session.expunge(row)
            return row, False

    def _retained_admission(
        self,
        session: Session,
        target_id: UUID,
        *,
        observed_container_id: str | None = None,
    ) -> None:
        from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
        from kodi_lab.storage.retained_development import (
            require_retained_container,
            retained_targets,
        )

        if not retained_targets(session):
            return
        try:
            require_retained_container(
                session,
                self._resources._broker.managed_container_inventory().containers,
                str(target_id),
                observed_container_id=observed_container_id,
            )
        except DevelopmentSessionConflict as error:
            raise ManagedAgentLifecycleError(error.code) from error

    def _pending(
        self,
        session: Session,
        row: ManagedAgentLifecycleRequestRow,
        authorize: AuthorizeLifecycle,
        *,
        observed_container_id: str | None = None,
    ) -> ManagedAgentLifecycleRequestRow:
        authorize(session)
        current = session.get(ManagedAgentLifecycleRequestRow, row.id)
        if (
            current is None
            or current.state != "pending"
            or current.client_id != row.client_id
            or current.original_instance_id != row.original_instance_id
        ):
            raise ManagedAgentLifecycleError("request_conflict")
        instance_id = UUID(row.original_instance_id)
        self._resources.require_managed(session, instance_id)
        self._retained_admission(
            session, instance_id, observed_container_id=observed_container_id
        )
        if (
            session.get(ManagedInstanceMutationLeaseRow, row.original_instance_id)
            is not None
        ):
            raise ManagedResourceProtected("active_operation")
        if (
            session.get(ManagedStartReservationRow, row.original_instance_id)
            is not None
        ):
            raise ManagedResourceProtected("start_requires_reconciliation")
        if self._resources._reconciliation.status(instance_id).required:
            raise ManagedResourceProtected("operation_requires_reconciliation")
        return current

    def _hold(self, session: Session, row: ManagedAgentLifecycleRequestRow) -> None:
        instance_id = UUID(row.original_instance_id)
        actor = f"mcp:{row.client_id}"
        record_activity(session, instance_id, actor=actor, now=self._clock())
        session.flush()
        use = session.get(ManagedInstanceUseRow, (row.original_instance_id, actor))
        assert use is not None
        if row.hold_minutes:
            until = timestamp(self._clock() + timedelta(minutes=row.hold_minutes))
            use.hold_until = max(use.hold_until or until, until)

    def _ensure(
        self, row: ManagedAgentLifecycleRequestRow, authorize: AuthorizeLifecycle
    ) -> None:
        instance_id = UUID(row.original_instance_id)
        with self._sessions() as session:
            self._pending(session, row, authorize)
            original = ManagedRuntimeProjectionRepository(session).observation_token(
                instance_id
            )
        observation = self._status.get_observation(instance_id)
        status = observation.status
        records = (
            []
            if observation.inventory is None
            else [
                item
                for item in observation.inventory.containers
                if item.managed_instance_id == row.original_instance_id
            ]
        )
        if (
            original is None
            or status.provision_state is not ProvisionState.PROVISIONED
            or len(records) != 1
        ):
            raise ManagedResourceProtected("provision_required")
        container_id = records[0].container_id
        if status.container_state is ContainerRuntimeState.RUNNING:
            with self._sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                current = self._pending(
                    session, row, authorize, observed_container_id=container_id
                )
                if (
                    ManagedRuntimeProjectionRepository(session).observation_token(
                        instance_id
                    )
                    != original
                ):
                    raise ManagedResourceProtected("runtime_changed")
                current.runtime_revision_id, current.container_id = (
                    str(original.runtime_revision_id),
                    container_id,
                )
                current.expected_state_revision = original.runtime_state_revision
                current.state = (
                    "ready"
                    if status.usability_state is SemanticUsabilityState.USABLE
                    else "not_ready"
                )
                current.result_json = json.dumps({"code": "running"})
                current.updated_at = timestamp(self._clock())
                self._hold(session, current)
            return
        if status.container_state is not ContainerRuntimeState.STOPPED:
            raise ManagedResourceProtected("runtime_not_ready")

        def precondition(session: Session) -> None:
            self._pending(session, row, authorize)
            if (
                ManagedRuntimeProjectionRepository(session).observation_token(
                    instance_id
                )
                != original
            ):
                raise ManagedResourceProtected("runtime_changed")

        def claimed(session: Session, handle: RunningLifecycleOperation) -> None:
            current = session.get(ManagedAgentLifecycleRequestRow, row.id)
            assert current is not None and current.state == "pending"
            current.lifecycle_operation_id, current.state = (
                str(handle.operation_id),
                "dispatched",
            )
            current.runtime_revision_id, current.container_id = (
                str(original.runtime_revision_id),
                container_id,
            )
            current.expected_state_revision = handle.runtime_state_revision + 1
            current.updated_at = timestamp(self._clock())
            current.result_json = json.dumps({"code": "start_requested"})
            self._hold(session, current)

        def before_dispatch() -> None:
            with self._sessions() as session:
                authorize(session)
            current_records = [
                item
                for item in (
                    self._resources._broker.managed_container_inventory().containers
                )
                if item.managed_instance_id == row.original_instance_id
            ]
            if (
                len(current_records) != 1
                or current_records[0].container_id != container_id
            ):
                raise ManagedResourceProtected("runtime_changed")

        self._lifecycle.start(
            instance_id,
            operation_id=UUID(row.id),
            precondition=precondition,
            on_claim=claimed,
            before_dispatch=before_dispatch,
        )

    def _release(
        self, row: ManagedAgentLifecycleRequestRow, authorize: AuthorizeLifecycle
    ) -> None:
        instance_id = UUID(row.original_instance_id)
        with self._sessions.begin() as session:
            authorize(session)
            self._resources.require_managed(session, instance_id)
            use = session.get(
                ManagedInstanceUseRow,
                (row.original_instance_id, f"mcp:{row.client_id}"),
            )
            if use is not None:
                use.hold_until, use.released_at = None, timestamp(self._clock())
        status = self._status.get(instance_id)
        if (
            status.container_state is ContainerRuntimeState.STOPPED
            or status.provision_state is ProvisionState.UNPROVISIONED
        ):
            self._finish(row.id, state="released", code="already_stopped")
            return

        def claimed(session: Session, handle: RunningLifecycleOperation) -> None:
            authorize(session)
            current = session.get(ManagedAgentLifecycleRequestRow, row.id)
            if current is None or current.state != "pending":
                raise ManagedAgentLifecycleError("request_conflict")
            current.lifecycle_operation_id, current.state = (
                str(handle.operation_id),
                "dispatched",
            )
            current.updated_at = timestamp(self._clock())
            current.result_json = json.dumps({"code": "stop_requested"})

        self._resources.stop_if_eligible(
            instance_id,
            reason="released",
            actor=f"mcp:{row.client_id}",
            operation_id=UUID(row.id),
            authorize=authorize,
            on_claim=claimed,
        )
        self._finish(row.id, state="released", code="stopped")

    def _finish(self, identity: str, *, state: str, code: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(ManagedAgentLifecycleRequestRow, identity)
            assert row is not None
            row.state, row.result_json = state, json.dumps({"code": code})
            row.updated_at = timestamp(self._clock())

    def _result(
        self,
        identity: str,
        client_id: UUID,
        authorize: AuthorizeLifecycle,
        *,
        replayed: bool,
    ) -> ManagedAgentLifecycleResult:
        with self._sessions.begin() as session:
            authorize(session)
            row = session.get(ManagedAgentLifecycleRequestRow, identity)
            assert row is not None and row.client_id == str(client_id)
            child = (
                None
                if row.lifecycle_operation_id is None
                else session.get(
                    ManagedInstanceLifecycleOperationRow, row.lifecycle_operation_id
                )
            )
            if row.state == "dispatched":
                if (
                    child is None
                    or child.state
                    is ManagedInstanceLifecycleOperationState.RECONCILIATION_REQUIRED
                ):
                    row.state, row.result_json = (
                        "ambiguous",
                        json.dumps({"code": "operation_requires_reconciliation"}),
                    )
                elif child.state is ManagedInstanceLifecycleOperationState.FAILED:
                    row.state, row.result_json = (
                        "failed",
                        json.dumps({"code": "lifecycle_failed"}),
                    )
                elif child.state is ManagedInstanceLifecycleOperationState.SUCCEEDED:
                    row.state = (
                        "not_ready" if row.action == "ensure_ready" else "released"
                    )
                    row.result_json = json.dumps(
                        {
                            "code": "start_accepted"
                            if row.action == "ensure_ready"
                            else "stopped"
                        }
                    )
                else:
                    lease = session.get(
                        ManagedInstanceMutationLeaseRow, row.original_instance_id
                    )
                    if (
                        lease is None
                        or lease.operation_id != row.lifecycle_operation_id
                        or lease.lease_expires_at is None
                        or datetime.fromisoformat(
                            lease.lease_expires_at.replace("Z", "+00:00")
                        )
                        <= self._clock()
                    ):
                        row.state, row.result_json = (
                            "ambiguous",
                            json.dumps({"code": "operation_requires_reconciliation"}),
                        )
            session.flush()
            session.expunge(row)
        try:
            observation = self._status.get_observation(UUID(row.original_instance_id))
            current = observation.status
            with self._sessions() as session:
                if row.action == "ensure_ready":
                    self._retained_admission(
                        session,
                        UUID(row.original_instance_id),
                        observed_container_id=row.container_id,
                    )
                token = ManagedRuntimeProjectionRepository(session).observation_token(
                    UUID(row.original_instance_id)
                )
            records = (
                []
                if observation.inventory is None
                else observation.inventory.containers
            )
            binding_matches = (
                token is not None
                and token.runtime_state_revision == row.expected_state_revision
                and str(token.runtime_revision_id) == row.runtime_revision_id
                and any(
                    item.managed_instance_id == row.original_instance_id
                    and item.container_id == row.container_id
                    for item in records
                )
            )
            ready = (
                row.action == "ensure_ready"
                and row.state in {"ready", "not_ready"}
                and binding_matches
                and current.usability_state is SemanticUsabilityState.USABLE
                and current.container_state is ContainerRuntimeState.RUNNING
            )
            current_state, readiness = (
                current.container_state,
                current.usability_state,
            )
        except Exception:
            ready, binding_matches, current_state, readiness = (
                False,
                False,
                ContainerRuntimeState.UNKNOWN,
                SemanticUsabilityState.NOT_USABLE,
            )
        try:
            capacity = self._resources.capacity()
        except Exception:
            capacity = None
        with self._sessions.begin() as session:
            authorize(session)
            use = session.get(
                ManagedInstanceUseRow, (row.original_instance_id, f"mcp:{client_id}")
            )
            hold = None if use is None else use.hold_until
            if ready and row.state == "not_ready":
                durable = session.get(ManagedAgentLifecycleRequestRow, identity)
                assert durable is not None
                durable.state = row.state = "ready"
        code = (
            "operation_in_progress"
            if row.result_json is None
            else str(json.loads(row.result_json)["code"])
        )
        if (
            row.action == "ensure_ready"
            and row.state in {"ready", "not_ready"}
            and not binding_matches
        ):
            code = "runtime_changed_since_request"
        return ManagedAgentLifecycleResult(
            request_id=UUID(row.request_id),
            operation_id=UUID(row.id),
            target_id=UUID(row.original_instance_id),
            action="ensure_ready" if row.action == "ensure_ready" else "release",
            receipt_state=cast(ManagedReceiptState, row.state),
            code=code,
            replayed=replayed,
            lifecycle_operation_id=None
            if row.lifecycle_operation_id is None
            else UUID(row.lifecycle_operation_id),
            ready=ready,
            current_state=current_state,
            readiness=readiness,
            hold_until=None
            if hold is None
            else datetime.fromisoformat(hold.replace("Z", "+00:00")),
            capacity=capacity,
        )
