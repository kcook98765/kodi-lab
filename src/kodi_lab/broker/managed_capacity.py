"""Atomic running/starting admission at the broker's managed Docker start boundary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.docker_inventory import (
    ContainerRuntimeState,
    ManagedContainerRecord,
    ProvisionedManagedContainerRecord,
    StartManagedContainerRecord,
)
from kodi_lab.broker.errors import (
    AmbiguousManagedContainerError,
    ContainerIdentityPreconditionError,
    ContainerStateNotReconcilableError,
    DockerInventoryReadError,
    ManagedContainerNotFoundError,
    ManagedNetworkConflictError,
    ManagedNetworkUnavailableError,
    RuntimeImageUnavailableError,
    RuntimeRevisionPreconditionError,
    RuntimeStatePreconditionError,
)
from kodi_lab.config import Settings
from kodi_lab.storage.development_sessions import (
    DevelopmentSessionConflict,
    require_session_admission,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.managed_activity import resource_policy, timestamp
from kodi_lab.storage.models import (
    DevelopmentSessionRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedProvisionReservationRow,
    ManagedStartReservationRow,
)
from kodi_lab.storage.retained_development import retained_inventory, retained_targets

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

    def run_provision(
        self,
        *,
        instance_id: str,
        revision_id: str,
        dispatch: Callable[[], ProvisionedManagedContainerRecord],
    ) -> ProvisionedManagedContainerRecord:
        operation_id = str(uuid4())
        generation: int | None = None
        expected_container: str | None = None
        retained = False
        try:
            with self._sessions() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                instance = session.get(ManagedInstanceRow, instance_id)
                if instance is None or instance.runtime_revision_id != revision_id:
                    raise ManagedCapacityError("managed_provision_identity_unavailable")
                if session.get(ManagedProvisionReservationRow, instance_id) is not None:
                    raise ManagedCapacityError(
                        "managed_provision_requires_reconciliation"
                    )
                records = self._inventory()
                retained = bool(retained_targets(session))
                if retained:
                    expected_container = next(
                        (
                            r.container_id
                            for r in records
                            if r.managed_instance_id == instance_id
                        ),
                        None,
                    )
                try:
                    current = require_session_admission(
                        session, records, target_id=instance_id, revision_id=revision_id
                    )
                except DevelopmentSessionConflict as error:
                    raise ManagedCapacityError(error.code) from error
                if current is not None:
                    if (
                        session.scalar(select(ManagedStartReservationRow).limit(1))
                        is not None
                    ):
                        raise ManagedCapacityError("development_session_start_pending")
                    generation = current.generation
                    expected_container = current.container_id
                    current.phase = "provisioning"
                    current.updated_at = timestamp(self._clock())
                session.add(
                    ManagedProvisionReservationRow(
                        managed_instance_id=instance_id,
                        runtime_revision_id=revision_id,
                        operation_id=operation_id,
                        phase="reserved",
                        session_generation=generation,
                        created_at=timestamp(self._clock()),
                    )
                )
                session.commit()
        except SQLAlchemyError as error:
            raise ManagedCapacityError("managed_capacity_unavailable") from error
        try:
            result = dispatch()
            if (
                result.managed_instance_id != instance_id
                or result.runtime_revision_id != revision_id
            ):
                raise ManagedCapacityError("managed_provision_result_unverified")
            if generation is not None or retained:
                if (expected_container is None and not result.created) or (
                    expected_container is not None
                    and result.container_id != expected_container
                ):
                    raise ManagedCapacityError("managed_provision_result_unverified")
                records = self._inventory()
                with self._sessions() as session:
                    if retained:
                        retained_inventory(session, records)
                selected_records = [
                    r for r in records if r.managed_instance_id == instance_id
                ]
                if (
                    (not retained and len(records) != 1)
                    or (retained and any(r.runtime_state != "stopped" for r in records))
                    or len(selected_records) != 1
                    or selected_records[0].container_id != result.container_id
                    or selected_records[0].runtime_revision_id != revision_id
                    or selected_records[0].image_id != result.image_id
                ):
                    raise ManagedCapacityError("managed_provision_result_unverified")
            self._finish_provision(instance_id, operation_id, result)
            return result
        except (
            ManagedNetworkUnavailableError,
            ManagedNetworkConflictError,
            RuntimeImageUnavailableError,
        ):
            # These provider exceptions are raised before any Docker create.
            self._finish_provision(instance_id, operation_id, None, safe_failure=True)
            raise
        except BaseException:
            self._finish_provision(instance_id, operation_id, None)
            raise

    def _finish_provision(
        self,
        instance_id: str,
        operation_id: str,
        result: ProvisionedManagedContainerRecord | None,
        *,
        safe_failure: bool = False,
    ) -> None:
        with self._sessions.begin() as session:
            row = session.get(ManagedProvisionReservationRow, instance_id)
            if row is None or row.operation_id != operation_id:
                raise ManagedCapacityError("managed_provision_requires_reconciliation")
            current = (
                None
                if row.session_generation is None
                else session.get(DevelopmentSessionRow, row.session_generation)
            )
            if row.session_generation is not None and (
                current is None or current.phase != "provisioning"
            ):
                raise ManagedCapacityError("development_session_requires_review")
            if current is not None:
                current.phase = (
                    "bound"
                    if result is not None
                    or (safe_failure and current.container_id is not None)
                    else "reserved"
                    if safe_failure
                    else "uncertain"
                )
                if result is not None:
                    current.container_id = result.container_id
                current.updated_at = timestamp(self._clock())
            if result is None and not safe_failure:
                row.phase = "uncertain"
            else:
                session.delete(row)

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
            try:
                require_session_admission(
                    session,
                    records,
                    target_id=instance_id,
                    revision_id=revision_id,
                    container_id=container_id,
                    starting=True,
                )
            except DevelopmentSessionConflict as error:
                raise ManagedCapacityError(error.code) from error
            reservations = list(session.scalars(select(ManagedStartReservationRow)))
            policy = resource_policy(session)
            limit = 1 if retained_targets(session) else policy.max_running
            used = {
                item.managed_instance_id
                for item in records
                if item.runtime_state is not ContainerRuntimeState.STOPPED
            } | {item.managed_instance_id for item in reservations}
            if any(item.managed_instance_id == instance_id for item in reservations):
                raise ManagedCapacityError(
                    "managed_start_requires_reconciliation",
                    used=len(used),
                    limit=limit,
                )
            if instance_id not in used and len(used) >= limit:
                raise ManagedCapacityError(
                    "managed_capacity_full", used=len(used), limit=limit
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
