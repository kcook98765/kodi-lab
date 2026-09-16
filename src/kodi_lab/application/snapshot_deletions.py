"""Explicit artifact-first Snapshot deletion orchestration."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from threading import Lock
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.snapshot_delete_operations import (
    SnapshotDeleteLease,
    SnapshotDeleteLeaseLostError,
    SnapshotDeleteOperationCoordinator,
)
from kodi_lab.domain import SnapshotDeleteOperationPhase, SnapshotState
from kodi_lab.snapshot.storage import snapshot_artifact_presence
from kodi_lab.storage.repositories import SnapshotRepository


class SnapshotDeleteBrokerResult(Protocol):
    snapshot_id: str
    artifact_absent: bool


class SnapshotDeleteBroker(Protocol):
    def delete_managed_snapshot_artifact(
        self, *, snapshot_id: str
    ) -> SnapshotDeleteBrokerResult: ...


class SnapshotDeletionError(RuntimeError):
    def __init__(
        self, safe_reason: str, *, reconciliation_required: bool = True
    ) -> None:
        self.safe_reason = safe_reason[:256]
        self.reconciliation_required = reconciliation_required
        super().__init__(safe_reason)


class DisposableSnapshotDeletionService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: SnapshotDeleteBroker,
        coordinator: SnapshotDeleteOperationCoordinator,
        lease: SnapshotDeleteLease,
        *,
        data_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._coordinator = coordinator
        self._lease = lease
        self._data_root = data_root

    def delete(self) -> None:
        self._coordinator.update_phase(
            self._lease, SnapshotDeleteOperationPhase.VALIDATING
        )
        with self._session_factory() as session:
            snapshot = SnapshotRepository(session).get(self._lease.snapshot_id)
        if (
            snapshot is None
            or snapshot.source_managed_instance_id != self._lease.managed_instance_id
            or snapshot.state is not SnapshotState.DELETING
        ):
            raise SnapshotDeletionError(
                "Snapshot deletion state is unavailable or inconsistent."
            )

        self._coordinator.update_phase(
            self._lease, SnapshotDeleteOperationPhase.DELETING_ARTIFACT
        )
        try:
            result = self._broker.delete_managed_snapshot_artifact(
                snapshot_id=str(self._lease.snapshot_id)
            )
        except Exception as error:
            raise SnapshotDeletionError(
                "Snapshot artifact deletion could not be proven complete."
            ) from error
        if str(result.snapshot_id) != str(self._lease.snapshot_id) or not (
            result.artifact_absent
        ):
            raise SnapshotDeletionError(
                "Snapshot artifact deletion returned inconsistent identity."
            )

        self._coordinator.update_phase(
            self._lease, SnapshotDeleteOperationPhase.VERIFYING_ABSENCE
        )
        if (
            snapshot_artifact_presence(self._data_root, self._lease.snapshot_id)
            != "absent"
        ):
            raise SnapshotDeletionError(
                "Snapshot artifact absence could not be independently proven."
            )
        self._coordinator.update_phase(
            self._lease, SnapshotDeleteOperationPhase.DELETING_METADATA
        )


type SnapshotDeleteServiceFactory = Callable[
    [SnapshotDeleteLease], DisposableSnapshotDeletionService
]


class SnapshotDeleteExecutor:
    def __init__(
        self,
        coordinator: SnapshotDeleteOperationCoordinator,
        service_factory: SnapshotDeleteServiceFactory,
    ) -> None:
        self._coordinator = coordinator
        self._service_factory = service_factory
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete")
        self._guard = Lock()
        self._accepting = True

    def start(self) -> None:
        for operation in self._coordinator.queued():
            self.submit(operation.id)

    def submit(self, operation_id: UUID) -> None:
        with self._guard:
            if not self._accepting:
                raise SnapshotDeletionError(
                    "Snapshot deletion executor is shutting down."
                )
            self._pool.submit(self._execute, operation_id)

    def _execute(self, operation_id: UUID) -> None:
        try:
            lease = self._coordinator.acquire(operation_id)
        except SnapshotDeleteLeaseLostError:
            return
        if lease is None:
            return
        try:
            self._service_factory(lease).delete()
            self._coordinator.finish_success(lease)
        except SnapshotDeletionError as error:
            with suppress(SnapshotDeleteLeaseLostError):
                self._coordinator.finish_failure(
                    lease,
                    reason=error.safe_reason,
                    reconciliation_required=error.reconciliation_required,
                )
        except Exception:
            with suppress(SnapshotDeleteLeaseLostError):
                self._coordinator.finish_failure(
                    lease,
                    reason="Snapshot deletion could not be completed.",
                    reconciliation_required=True,
                )

    def close(self) -> None:
        with self._guard:
            self._accepting = False
        self._pool.shutdown(wait=False, cancel_futures=True)
