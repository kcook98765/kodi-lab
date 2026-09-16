"""Container-first disposable ManagedInstance deletion execution."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from threading import Lock
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.instance_delete_operations import (
    ELIGIBLE_SNAPSHOT_STATES,
    ManagedInstanceDeleteLease,
    ManagedInstanceDeleteLeaseLostError,
    ManagedInstanceDeleteOperationCoordinator,
    ManagedInstanceDeleteUnsupportedError,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.snapshot_deletions import (
    SnapshotDeleteBroker,
    SnapshotDeletionError,
    delete_snapshot_artifact_core,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.operations import (
    DeprovisionManagedContainerResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    StopManagedContainerResult,
)
from kodi_lab.domain import (
    ManagedInstanceDeleteOperationPhase,
    ManagedInstanceDeletePriorState,
)
from kodi_lab.snapshot.storage import snapshot_artifact_presence
from kodi_lab.storage.repositories import SnapshotRepository


class InstanceDeleteBroker(SnapshotDeleteBroker, Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

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


class ManagedInstanceDeletionError(RuntimeError):
    def __init__(self, safe_reason: str) -> None:
        self.safe_reason = safe_reason[:256]
        super().__init__(safe_reason)


class DisposableManagedInstanceDeletionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: InstanceDeleteBroker,
        readiness: ReadinessTaskRegistry,
        coordinator: ManagedInstanceDeleteOperationCoordinator,
        lease: ManagedInstanceDeleteLease,
        *,
        data_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._readiness = readiness
        self._coordinator = coordinator
        self._lease = lease
        self._data_root = data_root

    def delete(self) -> None:
        self._readiness.cancel(self._lease.managed_instance_id)
        self._coordinator.update_phase(
            self._lease, ManagedInstanceDeleteOperationPhase.RESOLVING_INSTANCE
        )
        container = self._resolve_container()
        if container is None:
            prior_state = ManagedInstanceDeletePriorState.UNPROVISIONED
        elif container.runtime_state is ContainerRuntimeState.RUNNING:
            prior_state = ManagedInstanceDeletePriorState.RUNNING
        elif container.runtime_state is ContainerRuntimeState.STOPPED:
            prior_state = ManagedInstanceDeletePriorState.STOPPED
        else:
            raise ManagedInstanceDeletionError(
                "Managed container is in an unsupported transitional state."
            )
        self._coordinator.set_container_resolution(
            self._lease,
            prior_state=prior_state,
            container_id=None if container is None else container.container_id,
        )

        if (
            container is not None
            and container.runtime_state is ContainerRuntimeState.RUNNING
        ):
            self._coordinator.update_phase(
                self._lease, ManagedInstanceDeleteOperationPhase.STOPPING
            )
            try:
                self._broker.stop_managed_container(
                    managed_instance_id=str(self._lease.managed_instance_id),
                    expected_container_id=container.container_id,
                    expected_runtime_state=ContainerRuntimeState.RUNNING,
                )
            except Exception as error:
                raise ManagedInstanceDeletionError(
                    "Managed container stop could not be proven complete."
                ) from error
            stopped = self._resolve_container()
            if (
                stopped is None
                or stopped.container_id != container.container_id
                or stopped.runtime_state is not ContainerRuntimeState.STOPPED
            ):
                raise ManagedInstanceDeletionError(
                    "Managed container stopped state could not be proven."
                )
            container = stopped

        if container is not None:
            self._coordinator.update_phase(
                self._lease, ManagedInstanceDeleteOperationPhase.DEPROVISIONING
            )
            operation = self._coordinator.get(self._lease.operation_id)
            if operation is None:
                raise ManagedInstanceDeleteLeaseLostError(
                    "Instance deletion operation disappeared."
                )
            try:
                self._broker.deprovision_managed_container(
                    managed_instance_id=str(self._lease.managed_instance_id),
                    expected_container_id=container.container_id,
                    expected_runtime_revision_id=str(operation.runtime_revision_id),
                    expected_runtime_state=ContainerRuntimeState.STOPPED,
                )
            except Exception as error:
                raise ManagedInstanceDeletionError(
                    "Managed container removal could not be proven complete."
                ) from error

        self._coordinator.update_phase(
            self._lease,
            ManagedInstanceDeleteOperationPhase.VERIFYING_CONTAINER_ABSENCE,
        )
        if self._resolve_container() is not None:
            raise ManagedInstanceDeletionError(
                "Complete managed-container absence could not be proven."
            )
        self._readiness.clear(self._lease.managed_instance_id)

        if self._lease.preserve_snapshots:
            self._coordinator.update_phase(
                self._lease, ManagedInstanceDeleteOperationPhase.PRESERVING_SNAPSHOTS
            )
            self._coordinator.update_phase(
                self._lease, ManagedInstanceDeleteOperationPhase.DELETING_METADATA
            )
            self._coordinator.finish_success(self._lease)
            return

        self._delete_snapshots()
        self._coordinator.update_phase(
            self._lease,
            ManagedInstanceDeleteOperationPhase.VERIFYING_SNAPSHOT_ABSENCE,
        )
        self._coordinator.update_phase(
            self._lease, ManagedInstanceDeleteOperationPhase.DELETING_METADATA
        )
        self._coordinator.finish_success(self._lease)

    def _delete_snapshots(self) -> None:
        self._coordinator.update_phase(
            self._lease, ManagedInstanceDeleteOperationPhase.DELETING_SNAPSHOTS
        )
        with self._session_factory() as session:
            snapshots = SnapshotRepository(session).list_for_instance(
                self._lease.managed_instance_id
            )
        operation = self._coordinator.get(self._lease.operation_id)
        if operation is None or len(snapshots) != operation.snapshot_count:
            raise ManagedInstanceDeletionError(
                "Authoritative Snapshot set changed before destruction."
            )
        snapshots.sort(key=lambda item: str(item.id))
        if any(item.state not in ELIGIBLE_SNAPSHOT_STATES for item in snapshots):
            raise ManagedInstanceDeletionError(
                "Active or transitional Snapshot state blocks instance deletion."
            )
        snapshot_ids = [item.id for item in snapshots]
        for snapshot_id in snapshot_ids:
            self._coordinator.update_phase(
                self._lease, ManagedInstanceDeleteOperationPhase.DELETING_SNAPSHOTS
            )
            self._coordinator.begin_snapshot(self._lease, snapshot_id)
            try:
                delete_snapshot_artifact_core(
                    self._broker,
                    data_root=self._data_root,
                    snapshot_id=snapshot_id,
                )
            except SnapshotDeletionError as error:
                raise ManagedInstanceDeletionError(error.safe_reason) from error
            self._coordinator.finish_snapshot(self._lease, snapshot_id)
        for snapshot_id in snapshot_ids:
            if snapshot_artifact_presence(self._data_root, snapshot_id) != "absent":
                raise ManagedInstanceDeletionError(
                    "Snapshot artifact absence could not be proven after deletion."
                )

    def _resolve_container(self) -> ManagedContainerInventoryEntry | None:
        inventory = self._broker.managed_container_inventory()
        matches = [
            item
            for item in inventory.containers
            if item.managed_instance_id == str(self._lease.managed_instance_id)
        ]
        if len(matches) > 1:
            raise ManagedInstanceDeletionError(
                "Managed container identity is ambiguous."
            )
        return None if not matches else matches[0]


type ManagedInstanceDeleteServiceFactory = Callable[
    [ManagedInstanceDeleteLease], DisposableManagedInstanceDeletionService
]


class ManagedInstanceDeleteExecutor:
    def __init__(
        self,
        coordinator: ManagedInstanceDeleteOperationCoordinator,
        service_factory: ManagedInstanceDeleteServiceFactory,
    ) -> None:
        self._coordinator = coordinator
        self._service_factory = service_factory
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="instance-delete"
        )
        self._guard = Lock()
        self._accepting = True

    def start(self) -> None:
        for operation in self._coordinator.queued():
            self.submit(operation.id)

    def submit(self, operation_id: UUID) -> None:
        with self._guard:
            if not self._accepting:
                raise ManagedInstanceDeleteUnsupportedError(
                    "Instance deletion executor is shutting down."
                )
            self._pool.submit(self._execute, operation_id)

    def _execute(self, operation_id: UUID) -> None:
        try:
            lease = self._coordinator.acquire(operation_id)
        except ManagedInstanceDeleteLeaseLostError:
            return
        if lease is None:
            return
        try:
            self._service_factory(lease).delete()
        except ManagedInstanceDeletionError as error:
            with suppress(ManagedInstanceDeleteLeaseLostError):
                self._coordinator.finish_failure(lease, reason=error.safe_reason)
        except Exception:
            with suppress(ManagedInstanceDeleteLeaseLostError):
                self._coordinator.finish_failure(
                    lease, reason="Managed instance deletion could not be completed."
                )

    def close(self) -> None:
        with self._guard:
            self._accepting = False
        self._pool.shutdown(wait=False, cancel_futures=True)
