"""Atomic running/starting admission at the broker's managed Docker start boundary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.docker_inventory import (
    ContainerRuntimeState,
    ManagedContainerRecord,
    StartManagedContainerRecord,
)
from kodi_lab.broker.errors import (
    AmbiguousManagedContainerError,
    ContainerIdentityPreconditionError,
    ContainerStateNotReconcilableError,
    DockerInventoryReadError,
    ManagedContainerNotFoundError,
    RuntimeRevisionPreconditionError,
    RuntimeStatePreconditionError,
)
from kodi_lab.config import Settings
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.managed_activity import resource_policy, timestamp
from kodi_lab.storage.models import (
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedStartReservationRow,
)

_SAFE_BEFORE_DISPATCH = (
    AmbiguousManagedContainerError,
    ContainerIdentityPreconditionError,
    ContainerStateNotReconcilableError,
    DockerInventoryReadError,
    ManagedContainerNotFoundError,
    RuntimeRevisionPreconditionError,
    RuntimeStatePreconditionError,
)


class ManagedCapacityError(RuntimeError):
    def __init__(self, code: str, *, used: int = 0, limit: int = 4) -> None:
        self.code, self.used, self.limit = code, used, limit
        super().__init__(code)


class ManagedCapacityAdmission:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        inventory: Callable[[], tuple[ManagedContainerRecord, ...]],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions, self._inventory, self._clock = sessions, inventory, clock

    @classmethod
    def production(
        cls,
        inventory: Callable[[], tuple[ManagedContainerRecord, ...]],
    ) -> ManagedCapacityAdmission:
        return cls(
            create_session_factory(create_database_engine(Settings().database_url)),
            inventory,
        )

    def _claim(
        self,
        instance_id: str,
        container_id: str,
        revision_id: str,
    ) -> str:
        with self._sessions() as session:
            # The trusted inventory snapshot and reservation share one writer
            # transaction across broker processes. A delayed start keeps its slot.
            session.execute(text("BEGIN IMMEDIATE"))
            now = self._clock()
            instance = session.get(ManagedInstanceRow, instance_id)
            lease = session.get(ManagedInstanceMutationLeaseRow, instance_id)
            if (
                instance is None
                or instance.runtime_revision_id != revision_id
                or lease is None
                or lease.lease_token_digest is None
                or lease.lease_expires_at is None
                or datetime.fromisoformat(lease.lease_expires_at.replace("Z", "+00:00"))
                <= now
            ):
                raise ManagedCapacityError("managed_start_authority_unavailable")
            records = self._inventory()
            selected = [
                item for item in records if item.managed_instance_id == instance_id
            ]
            if (
                len(selected) != 1
                or selected[0].container_id != container_id
                or selected[0].runtime_revision_id != revision_id
            ):
                raise ManagedCapacityError("managed_start_identity_unavailable")
            reservations = list(session.scalars(select(ManagedStartReservationRow)))
            policy = resource_policy(session)
            used = {
                item.managed_instance_id
                for item in records
                if item.runtime_state is not ContainerRuntimeState.STOPPED
            } | {item.managed_instance_id for item in reservations}
            if any(item.managed_instance_id == instance_id for item in reservations):
                raise ManagedCapacityError(
                    "managed_start_requires_reconciliation",
                    used=len(used),
                    limit=policy.max_running,
                )
            if instance_id not in used and len(used) >= policy.max_running:
                raise ManagedCapacityError(
                    "managed_capacity_full", used=len(used), limit=policy.max_running
                )
            session.add(
                ManagedStartReservationRow(
                    managed_instance_id=instance_id,
                    container_id=container_id,
                    runtime_revision_id=revision_id,
                    operation_id=lease.operation_id,
                    phase="reserved",
                    created_at=timestamp(now),
                )
            )
            session.commit()
            return lease.operation_id

    def run_start(
        self,
        *,
        instance_id: str,
        container_id: str,
        revision_id: str,
        dispatch: Callable[[], StartManagedContainerRecord],
    ) -> StartManagedContainerRecord:
        try:
            operation_id = self._claim(instance_id, container_id, revision_id)
        except SQLAlchemyError as error:
            raise ManagedCapacityError("managed_capacity_unavailable") from error
        try:
            result = dispatch()
            if (
                result.managed_instance_id != instance_id
                or result.container_id != container_id
                or result.runtime_state is not ContainerRuntimeState.RUNNING
            ):
                raise ManagedCapacityError("managed_start_result_unverified")
        except _SAFE_BEFORE_DISPATCH:
            self._finish(instance_id, operation_id, uncertain=False)
            raise
        except BaseException:
            self._finish(instance_id, operation_id, uncertain=True)
            raise
        else:
            self._finish(instance_id, operation_id, uncertain=False)
            return result

    def _finish(self, instance_id: str, operation_id: str, *, uncertain: bool) -> None:
        with self._sessions.begin() as session:
            row = session.get(ManagedStartReservationRow, instance_id)
            if row is None or row.operation_id != operation_id:
                raise ManagedCapacityError("managed_start_requires_reconciliation")
            if uncertain:
                row.phase = "uncertain"
            else:
                session.delete(row)

    def confirmed_stop(
        self, *, instance_id: str, container_id: str, revision_id: str
    ) -> None:
        """Only the broker's exact, verified stopped postcondition frees a held slot."""
        with self._sessions.begin() as session:
            session.execute(
                delete(ManagedStartReservationRow).where(
                    ManagedStartReservationRow.managed_instance_id == instance_id,
                    ManagedStartReservationRow.container_id == container_id,
                    ManagedStartReservationRow.runtime_revision_id == revision_id,
                )
            )
