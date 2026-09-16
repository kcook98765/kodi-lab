"""Trusted application coordination for Web lifecycle actions."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.mutations import ManagedInstanceMutationCoordinator
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.reset_operations import ResetOperationCoordinator
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatus,
    ManagedRuntimeStatusService,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.operations import (
    DeprovisionManagedContainerResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    ProvisionManagedContainerResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.domain import ManagedInstanceMutationKind, RuntimeExecutionProfile
from kodi_lab.kodi.readiness import KodiReadinessCoordinator, ReadinessResult
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeExecutionProfileRepository,
)


class LifecycleConflictError(RuntimeError):
    """Trusted current state does not permit the requested semantic action."""


class _ReadinessCoordinator(Protocol):
    def wait_until_usable(
        self, managed_instance_id: str, *, cancelled: Callable[[], bool] | None = None
    ) -> ReadinessResult: ...


type ReadinessCoordinatorFactory = Callable[
    ["_LifecycleBroker", RuntimeExecutionProfile], _ReadinessCoordinator
]


class _LifecycleBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...

    def provision_managed_container(
        self, *, managed_instance_id: str
    ) -> ProvisionManagedContainerResult: ...

    def start_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StartManagedContainerResult: ...

    def stop_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StopManagedContainerResult: ...

    def deprovision_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_revision_id: str,
        expected_runtime_state: Literal[ContainerRuntimeState.STOPPED],
    ) -> DeprovisionManagedContainerResult: ...


class ManagedRuntimeLifecycleService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _LifecycleBroker,
        readiness: ReadinessTaskRegistry,
        status: ManagedRuntimeStatusService,
        coordinator_factory: ReadinessCoordinatorFactory = KodiReadinessCoordinator,
        reset_operations: ResetOperationCoordinator | None = None,
        mutation_coordinator: ManagedInstanceMutationCoordinator | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._readiness = readiness
        self._status = status
        self._coordinator_factory = coordinator_factory
        self._reset_operations = reset_operations
        self._mutations = mutation_coordinator or ManagedInstanceMutationCoordinator(
            session_factory
        )

    def provision(self, instance_id: UUID) -> ManagedRuntimeStatus:
        self._require_no_reset(instance_id)
        with self._mutations.hold(instance_id, ManagedInstanceMutationKind.LIFECYCLE):
            self._load_instance(instance_id)
            self._broker.provision_managed_container(
                managed_instance_id=str(instance_id)
            )
            self._readiness.clear(instance_id)
            return self._status.get(instance_id)

    def start(self, instance_id: UUID) -> ManagedRuntimeStatus:
        self._require_no_reset(instance_id)
        with self._mutations.hold(instance_id, ManagedInstanceMutationKind.LIFECYCLE):
            profile = self._load_instance(instance_id)
            container = self._require_container(instance_id)
            self._broker.start_managed_container(
                managed_instance_id=str(instance_id),
                expected_container_id=container.container_id,
                expected_runtime_state=container.runtime_state,
            )

            def run(cancel_event: Event) -> ReadinessResult:
                coordinator = self._coordinator_factory(self._broker, profile)
                return coordinator.wait_until_usable(
                    str(instance_id), cancelled=cancel_event.is_set
                )

            self._readiness.start(instance_id, run)
            return self._status.get(instance_id)

    def stop(self, instance_id: UUID) -> ManagedRuntimeStatus:
        self._require_no_reset(instance_id)
        with self._mutations.hold(instance_id, ManagedInstanceMutationKind.LIFECYCLE):
            self._load_instance(instance_id)
            container = self._require_container(instance_id)
            self._readiness.cancel(instance_id)
            self._broker.stop_managed_container(
                managed_instance_id=str(instance_id),
                expected_container_id=container.container_id,
                expected_runtime_state=container.runtime_state,
            )
            return self._status.get(instance_id)

    def deprovision(self, instance_id: UUID) -> ManagedRuntimeStatus:
        self._require_no_reset(instance_id)
        with self._mutations.hold(instance_id, ManagedInstanceMutationKind.LIFECYCLE):
            self._load_instance(instance_id)
            container = self._require_container(instance_id)
            if container.runtime_state is not ContainerRuntimeState.STOPPED:
                raise LifecycleConflictError("managed runtime must be stopped")
            self._readiness.clear(instance_id)
            self._broker.deprovision_managed_container(
                managed_instance_id=str(instance_id),
                expected_container_id=container.container_id,
                expected_runtime_revision_id=str(self._revision_id(instance_id)),
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
            return self._status.get(instance_id)

    def _load_instance(self, instance_id: UUID) -> RuntimeExecutionProfile:
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            if instance is None:
                raise ManagedInstanceNotFoundError(instance_id)
            profile = RuntimeExecutionProfileRepository(session).get(
                instance.runtime_revision_id
            )
            if profile is None:
                raise LifecycleConflictError("runtime execution profile is unavailable")
            return profile

    def _revision_id(self, instance_id: UUID) -> UUID:
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            if instance is None:
                raise ManagedInstanceNotFoundError(instance_id)
            return instance.runtime_revision_id

    def _require_container(self, instance_id: UUID) -> ManagedContainerInventoryEntry:
        inventory = self._broker.managed_container_inventory()
        match = next(
            (
                item
                for item in inventory.containers
                if item.managed_instance_id == str(instance_id)
            ),
            None,
        )
        if match is None:
            raise LifecycleConflictError("managed runtime is not provisioned")
        return match

    def _require_no_reset(self, instance_id: UUID) -> None:
        if self._reset_operations is not None:
            self._reset_operations.require_no_active(instance_id)
