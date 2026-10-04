"""Disposable snapshot-create orchestration and asynchronous execution."""

from __future__ import annotations

import grp
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.snapshot_operations import (
    SnapshotCreateLease,
    SnapshotCreateLeaseLostError,
    SnapshotCreateOperationCoordinator,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.errors import (
    ClientProtocolError,
    ClientTransportError,
    RemoteBrokerError,
)
from kodi_lab.broker.operations import (
    CaptureManagedInstanceSnapshotResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.domain import (
    InstanceRuntimeState,
    InstanceType,
    RuntimeExecutionProfile,
    SnapshotCreateOperationPhase,
    SnapshotState,
)
from kodi_lab.kodi.readiness import (
    KodiReadinessCoordinator,
    ReadinessResult,
    UsabilityState,
)
from kodi_lab.snapshot.storage import SnapshotCaptureContext, SnapshotStorageError
from kodi_lab.snapshot.validation import SnapshotValidation, validate_published_snapshot
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
    SnapshotRepository,
)


class SnapshotCreateError(RuntimeError):
    """A create-specific safe failure and reconciliation classification."""

    def __init__(
        self,
        safe_reason: str,
        *,
        reconciliation_required: bool = False,
        quarantine_artifact: bool = False,
    ) -> None:
        self.safe_reason = safe_reason
        self.reconciliation_required = reconciliation_required
        self.quarantine_artifact = quarantine_artifact
        super().__init__(safe_reason)


class _SnapshotBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def stop_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StopManagedContainerResult: ...

    def start_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StartManagedContainerResult: ...

    def capture_managed_instance_snapshot(
        self,
        *,
        snapshot_id: str,
        managed_instance_id: str,
        expected_container_id: str,
    ) -> CaptureManagedInstanceSnapshotResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...


class _ReadinessWaiter(Protocol):
    def wait_until_usable(self, managed_instance_id: str) -> ReadinessResult: ...


type ReadinessFactory = Callable[
    [_SnapshotBroker, RuntimeExecutionProfile], _ReadinessWaiter
]
type SnapshotValidator = Callable[..., SnapshotValidation]


@dataclass(frozen=True, slots=True)
class DisposableSnapshotCreateResult:
    snapshot_id: UUID
    managed_instance_id: UUID
    runtime_revision_id: UUID
    prior_runtime_state: InstanceRuntimeState
    final_runtime_state: ContainerRuntimeState
    archive_size: int
    member_count: int
    recognized_databases_checked: int


@dataclass(frozen=True, slots=True)
class _CaptureContext:
    manifest: SnapshotCaptureContext
    profile: RuntimeExecutionProfile


class DisposableSnapshotCreationService:
    """Stop if needed, capture, validate, publish availability, and restore state."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _SnapshotBroker,
        coordinator: SnapshotCreateOperationCoordinator,
        lease: SnapshotCreateLease,
        *,
        data_root: Path,
        owner_gid: int | None = None,
        readiness_factory: ReadinessFactory = KodiReadinessCoordinator,
        validator: SnapshotValidator = validate_published_snapshot,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._coordinator = coordinator
        self._lease = lease
        self._data_root = data_root
        self._owner_gid = owner_gid
        self._readiness_factory = readiness_factory
        self._validator = validator

    def create(self) -> DisposableSnapshotCreateResult:
        self._coordinator.update_phase(
            self._lease, SnapshotCreateOperationPhase.RESOLVING_INSTANCE
        )
        context = self._load_context()
        container = self._exact_container()
        if container.runtime_state not in {
            ContainerRuntimeState.RUNNING,
            ContainerRuntimeState.STOPPED,
        }:
            raise SnapshotCreateError(
                "Managed container state is transitional or unknown."
            )
        prior_state = InstanceRuntimeState(container.runtime_state.value)
        self._record_prior_state(prior_state)
        context = self._load_context()
        prior_running = prior_state is InstanceRuntimeState.RUNNING
        stopped_by_operation = False
        restored = not prior_running
        validated: SnapshotValidation | None = None
        try:
            if prior_running:
                self._coordinator.update_phase(
                    self._lease, SnapshotCreateOperationPhase.STOPPING
                )
                self._broker.stop_managed_container(
                    managed_instance_id=str(self._lease.managed_instance_id),
                    expected_container_id=container.container_id,
                    expected_runtime_state=ContainerRuntimeState.RUNNING,
                )
                container = self._prove_state(
                    container.container_id, ContainerRuntimeState.STOPPED
                )
                stopped_by_operation = True
            self._coordinator.update_phase(
                self._lease, SnapshotCreateOperationPhase.CAPTURING
            )
            try:
                broker_result = self._broker.capture_managed_instance_snapshot(
                    snapshot_id=str(self._lease.snapshot_id),
                    managed_instance_id=str(self._lease.managed_instance_id),
                    expected_container_id=container.container_id,
                )
            except RemoteBrokerError as error:
                if error.code == "runtime_revision_precondition_failed":
                    raise SnapshotCreateError(
                        "runtime_revision_precondition_failed: "
                        "The managed runtime revision differs from trusted state."
                    ) from error
                raise
            self._coordinator.update_phase(
                self._lease, SnapshotCreateOperationPhase.VALIDATING_ARCHIVE
            )
            try:
                validated = self._validator(
                    data_root=self._data_root,
                    context=context.manifest,
                    owner_gid=self._resolved_owner_gid(),
                    database_validation_started=lambda: self._coordinator.update_phase(
                        self._lease,
                        SnapshotCreateOperationPhase.VALIDATING_DATABASES,
                    ),
                )
            except SnapshotStorageError as error:
                raise SnapshotCreateError(
                    error.safe_message, quarantine_artifact=True
                ) from error
            if (
                broker_result.archive_digest != validated.artifact.archive_digest
                or broker_result.manifest_digest != validated.artifact.manifest_digest
                or broker_result.archive_size != validated.artifact.archive_size
                or broker_result.member_count != validated.artifact.member_count
                or broker_result.runtime_revision_id
                != str(context.manifest.runtime_revision_id)
            ):
                raise SnapshotCreateError(
                    "Broker and independent snapshot validation identities differ.",
                    quarantine_artifact=True,
                )
            self._coordinator.update_phase(
                self._lease, SnapshotCreateOperationPhase.PUBLISHING
            )
            self._mark_available(validated)
            if prior_running:
                self._restore_running(context.profile, container.container_id)
                restored = True
            self._verify_pins_unchanged(context.manifest)
            final = self._prove_state(
                container.container_id,
                (
                    ContainerRuntimeState.RUNNING
                    if prior_running
                    else ContainerRuntimeState.STOPPED
                ),
            )
            return DisposableSnapshotCreateResult(
                snapshot_id=self._lease.snapshot_id,
                managed_instance_id=self._lease.managed_instance_id,
                runtime_revision_id=context.manifest.runtime_revision_id,
                prior_runtime_state=prior_state,
                final_runtime_state=final.runtime_state,
                archive_size=validated.artifact.archive_size,
                member_count=validated.artifact.member_count,
                recognized_databases_checked=(validated.recognized_databases_checked),
            )
        except SnapshotCreateError as original:
            if (
                not original.reconciliation_required
                and prior_running
                and stopped_by_operation
                and not restored
            ):
                try:
                    self._restore_running(context.profile, container.container_id)
                except Exception as recovery_error:
                    raise SnapshotCreateError(
                        "broker_runtime_dispatch_unconfirmed"
                        if isinstance(
                            recovery_error,
                            (ClientTransportError, ClientProtocolError),
                        )
                        else "Snapshot creation failed and prior running state "
                        "was not restored.",
                        reconciliation_required=True,
                        quarantine_artifact=original.quarantine_artifact,
                    ) from recovery_error
            raise
        except Exception as error:
            if isinstance(error, (ClientTransportError, ClientProtocolError)):
                raise SnapshotCreateError(
                    "broker_runtime_dispatch_unconfirmed",
                    reconciliation_required=True,
                ) from error
            if prior_running and stopped_by_operation and not restored:
                try:
                    self._restore_running(context.profile, container.container_id)
                except Exception as recovery_error:
                    raise SnapshotCreateError(
                        "broker_runtime_dispatch_unconfirmed"
                        if isinstance(
                            recovery_error,
                            (ClientTransportError, ClientProtocolError),
                        )
                        else "Snapshot creation failed and prior running state "
                        "was not restored.",
                        reconciliation_required=True,
                    ) from recovery_error
            raise SnapshotCreateError(
                "Snapshot creation could not be completed."
            ) from error

    def _load_context(self) -> _CaptureContext:
        with self._session_factory() as session:
            snapshots = SnapshotRepository(session)
            snapshot = snapshots.get(self._lease.snapshot_id)
            instance = ManagedInstanceRepository(session).get(
                self._lease.managed_instance_id
            )
            if snapshot is None or instance is None:
                raise ManagedInstanceNotFoundError(self._lease.managed_instance_id)
            if instance.instance_type is not InstanceType.DISPOSABLE:
                raise SnapshotCreateError(
                    "Persistent snapshot capture is not supported."
                )
            if (
                snapshot.source_managed_instance_id != instance.target_id
                or snapshot.runtime_revision_id != instance.runtime_revision_id
                or snapshot.captured_fixture_definition_id
                != instance.fixture_definition_id
                or snapshot.state is not SnapshotState.CREATING
            ):
                raise SnapshotCreateError(
                    "Snapshot source identity differs from trusted current state."
                )
            revision = RuntimeRevisionRepository(session).get(
                instance.runtime_revision_id
            )
            profile = RuntimeExecutionProfileRepository(session).get(
                instance.runtime_revision_id
            )
            if revision is None or profile is None:
                raise SnapshotCreateError("Pinned runtime state is unavailable.")
            definition = RuntimeDefinitionRepository(session).get(
                revision.runtime_definition_id
            )
            if definition is None:
                raise SnapshotCreateError("Pinned runtime definition is unavailable.")
            if snapshot.prior_runtime_state is None:
                prior = InstanceRuntimeState.STOPPED
            else:
                prior = snapshot.prior_runtime_state
            return _CaptureContext(
                manifest=SnapshotCaptureContext(
                    snapshot_id=snapshot.id,
                    source_managed_instance_id=instance.target_id,
                    runtime_revision_id=revision.id,
                    kodi_release=definition.kodi_version,
                    captured_fixture_definition_id=instance.fixture_definition_id,
                    captured_at=snapshot.captured_at,
                    prior_runtime_state=prior.value,
                ),
                profile=profile,
            )

    def _record_prior_state(self, state: InstanceRuntimeState) -> None:
        with self._session_factory.begin() as session:
            if not SnapshotRepository(session).set_prior_runtime_state(
                self._lease.snapshot_id, state
            ):
                raise SnapshotCreateError(
                    "Snapshot prior runtime state could not be recorded."
                )

    def _exact_container(self) -> ManagedContainerInventoryEntry:
        inventory = self._broker.managed_container_inventory()
        matches = [
            item
            for item in inventory.containers
            if item.managed_instance_id == str(self._lease.managed_instance_id)
        ]
        if len(matches) != 1:
            raise SnapshotCreateError(
                "Snapshot source must have exactly one owned container."
            )
        return matches[0]

    def _prove_state(
        self, container_id: str, state: ContainerRuntimeState
    ) -> ManagedContainerInventoryEntry:
        container = self._exact_container()
        if (
            container.container_id != container_id
            or container.runtime_state is not state
        ):
            raise SnapshotCreateError(
                "Managed container state could not be proven after mutation.",
                reconciliation_required=True,
            )
        return container

    def _restore_running(
        self, profile: RuntimeExecutionProfile, container_id: str
    ) -> None:
        self._coordinator.update_phase(
            self._lease, SnapshotCreateOperationPhase.RESTORING_RUNNING_STATE
        )
        self._broker.start_managed_container(
            managed_instance_id=str(self._lease.managed_instance_id),
            expected_container_id=container_id,
            expected_runtime_state=ContainerRuntimeState.STOPPED,
        )
        self._prove_state(container_id, ContainerRuntimeState.RUNNING)
        self._coordinator.update_phase(
            self._lease, SnapshotCreateOperationPhase.VERIFYING_READINESS
        )
        readiness = self._readiness_factory(self._broker, profile).wait_until_usable(
            str(self._lease.managed_instance_id)
        )
        if readiness.usability_state is not UsabilityState.USABLE:
            raise SnapshotCreateError(
                "Kodi readiness failed while restoring prior running state.",
                reconciliation_required=True,
            )

    def _mark_available(self, validated: SnapshotValidation) -> None:
        artifact = validated.artifact
        with self._session_factory.begin() as session:
            if not SnapshotRepository(session).mark_available(
                self._lease.snapshot_id,
                archive_digest=artifact.archive_digest,
                manifest_digest=artifact.manifest_digest,
                archive_size=artifact.archive_size,
                member_count=artifact.member_count,
                validated_at=self._coordinator.now(),
            ):
                raise SnapshotCreateError(
                    "Snapshot availability could not be published safely.",
                    quarantine_artifact=True,
                )

    def _verify_pins_unchanged(self, expected: SnapshotCaptureContext) -> None:
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(
                self._lease.managed_instance_id
            )
            if (
                instance is None
                or instance.runtime_revision_id != expected.runtime_revision_id
                or instance.fixture_definition_id
                != expected.captured_fixture_definition_id
            ):
                raise SnapshotCreateError(
                    "Runtime or fixture identity changed during snapshot capture.",
                    reconciliation_required=True,
                )

    def _resolved_owner_gid(self) -> int:
        if self._owner_gid is not None:
            return self._owner_gid
        try:
            return grp.getgrnam("kodi-lab").gr_gid
        except KeyError as error:
            raise SnapshotCreateError(
                "Dedicated snapshot storage group is unavailable."
            ) from error


type SnapshotServiceFactory = Callable[
    [SnapshotCreateLease], DisposableSnapshotCreationService
]


class SnapshotCreateExecutor:
    """In-process runner whose authority is the durable typed lease set."""

    def __init__(
        self,
        coordinator: SnapshotCreateOperationCoordinator,
        service_factory: SnapshotServiceFactory,
    ) -> None:
        self._coordinator = coordinator
        self._service_factory = service_factory
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="snapshot")
        self._guard = Lock()
        self._accepting = True

    def start(self) -> None:
        for operation in self._coordinator.queued():
            self.submit(operation.id)

    def submit(self, operation_id: UUID) -> None:
        with self._guard:
            if not self._accepting:
                raise SnapshotCreateError("Snapshot executor is shutting down.")
            self._pool.submit(self._execute, operation_id)

    def _execute(self, operation_id: UUID) -> None:
        try:
            lease = self._coordinator.acquire(operation_id)
        except SnapshotCreateLeaseLostError:
            return
        if lease is None:
            return
        try:
            self._service_factory(lease).create()
            self._coordinator.finish_success(lease)
        except SnapshotCreateError as error:
            self._mark_invalid_if_creating(lease, error.quarantine_artifact)
            with suppress(SnapshotCreateLeaseLostError):
                self._coordinator.finish_failure(
                    lease,
                    reason=error.safe_reason,
                    reconciliation_required=error.reconciliation_required,
                )
        except Exception:
            self._mark_invalid_if_creating(lease, False)
            with suppress(SnapshotCreateLeaseLostError):
                self._coordinator.finish_failure(
                    lease,
                    reason="Snapshot creation could not be completed.",
                    reconciliation_required=False,
                )

    def _mark_invalid_if_creating(
        self, lease: SnapshotCreateLease, quarantined: bool
    ) -> None:
        self._coordinator.mark_snapshot_invalid(
            lease.snapshot_id, quarantined=quarantined
        )

    def close(self) -> None:
        with self._guard:
            self._accepting = False
        self._pool.shutdown(wait=False, cancel_futures=True)
