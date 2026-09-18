"""Disposable fresh-container snapshot restore orchestration."""

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

from kodi_lab.application.addon_zip_installations import (
    invalidate_addon_zip_installation_evidence,
)
from kodi_lab.application.development_source_deployments import (
    invalidate_development_source_deployments,
)
from kodi_lab.application.snapshot_restore_operations import (
    SnapshotRestoreLease,
    SnapshotRestoreLeaseLostError,
    SnapshotRestoreOperationCoordinator,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.operations import (
    ApplyManagedInstanceSnapshotResult,
    DeprovisionManagedContainerResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    ProvisionManagedContainerResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.domain import (
    InstanceRuntimeState,
    InstanceType,
    RuntimeExecutionProfile,
    SnapshotRestoreOperationPhase,
    SnapshotState,
    SnapshotValidationResult,
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


class SnapshotRestoreError(RuntimeError):
    def __init__(
        self, safe_reason: str, *, reconciliation_required: bool = False
    ) -> None:
        self.safe_reason = safe_reason
        self.reconciliation_required = reconciliation_required
        super().__init__(safe_reason)


class _RestoreBroker(Protocol):
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

    def deprovision_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_revision_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> DeprovisionManagedContainerResult: ...

    def provision_managed_container(
        self, *, managed_instance_id: str
    ) -> ProvisionManagedContainerResult: ...

    def apply_managed_instance_snapshot(
        self,
        *,
        snapshot_id: str,
        managed_instance_id: str,
        expected_container_id: str,
    ) -> ApplyManagedInstanceSnapshotResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...


class _ReadinessWaiter(Protocol):
    def wait_until_usable(self, managed_instance_id: str) -> ReadinessResult: ...


type ReadinessFactory = Callable[
    [_RestoreBroker, RuntimeExecutionProfile], _ReadinessWaiter
]
type SnapshotValidator = Callable[..., SnapshotValidation]


@dataclass(frozen=True, slots=True)
class _RestoreContext:
    manifest: SnapshotCaptureContext
    profile: RuntimeExecutionProfile


@dataclass(frozen=True, slots=True)
class DisposableSnapshotRestoreResult:
    snapshot_id: UUID
    managed_instance_id: UUID
    runtime_revision_id: UUID
    prior_runtime_state: InstanceRuntimeState
    old_container_id: str
    fresh_container_id: str
    final_runtime_state: ContainerRuntimeState
    recognized_databases_checked: int


class DisposableSnapshotRestoreService:
    """Replace one disposable profile through a fresh exact-pin container."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _RestoreBroker,
        coordinator: SnapshotRestoreOperationCoordinator,
        lease: SnapshotRestoreLease,
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

    def restore(self) -> DisposableSnapshotRestoreResult:
        self._coordinator.update_phase(
            self._lease, SnapshotRestoreOperationPhase.VALIDATING_SNAPSHOT
        )
        context = self._load_context()
        validated = self._validate_artifact(context.manifest)
        self._coordinator.update_phase(
            self._lease, SnapshotRestoreOperationPhase.RESOLVING_INSTANCE
        )
        old = self._exact_container()
        if old.runtime_state not in {
            ContainerRuntimeState.RUNNING,
            ContainerRuntimeState.STOPPED,
        }:
            raise SnapshotRestoreError(
                "Snapshot restore requires an exactly running or stopped source."
            )
        prior = InstanceRuntimeState(old.runtime_state.value)
        self._coordinator.record_prior_state(self._lease, prior)
        self._coordinator.record_container_identity(
            self._lease, old_container_id=old.container_id
        )
        stopped_by_operation = False
        old_removed = False
        fresh_id: str | None = None
        structural_restore_complete = False
        try:
            if prior is InstanceRuntimeState.RUNNING:
                self._coordinator.update_phase(
                    self._lease, SnapshotRestoreOperationPhase.STOPPING
                )
                self._broker.stop_managed_container(
                    managed_instance_id=str(self._lease.managed_instance_id),
                    expected_container_id=old.container_id,
                    expected_runtime_state=ContainerRuntimeState.RUNNING,
                )
                self._prove_state(old.container_id, ContainerRuntimeState.STOPPED)
                stopped_by_operation = True

            self._coordinator.update_phase(
                self._lease, SnapshotRestoreOperationPhase.DEPROVISIONING
            )
            with self._session_factory.begin() as session:
                invalidate_development_source_deployments(
                    session, self._lease.managed_instance_id
                )
                invalidate_addon_zip_installation_evidence(
                    session, self._lease.managed_instance_id
                )
            self._broker.deprovision_managed_container(
                managed_instance_id=str(self._lease.managed_instance_id),
                expected_container_id=old.container_id,
                expected_runtime_revision_id=str(context.manifest.runtime_revision_id),
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
            self._prove_absence()
            old_removed = True

            self._coordinator.update_phase(
                self._lease, SnapshotRestoreOperationPhase.PROVISIONING
            )
            provisioned = self._broker.provision_managed_container(
                managed_instance_id=str(self._lease.managed_instance_id)
            )
            if (
                not provisioned.created
                or provisioned.container_id == old.container_id
                or provisioned.runtime_revision_id
                != str(context.manifest.runtime_revision_id)
                or provisioned.runtime_state is not ContainerRuntimeState.STOPPED
            ):
                raise SnapshotRestoreError(
                    "Fresh exact-pin container identity could not be proven.",
                    reconciliation_required=True,
                )
            fresh_id = provisioned.container_id
            self._coordinator.record_container_identity(
                self._lease, fresh_container_id=fresh_id
            )
            self._prove_state(fresh_id, ContainerRuntimeState.STOPPED)

            self._coordinator.update_phase(
                self._lease, SnapshotRestoreOperationPhase.APPLYING_SNAPSHOT
            )
            applied = self._broker.apply_managed_instance_snapshot(
                snapshot_id=str(self._lease.snapshot_id),
                managed_instance_id=str(self._lease.managed_instance_id),
                expected_container_id=fresh_id,
            )
            self._coordinator.update_phase(
                self._lease, SnapshotRestoreOperationPhase.VERIFYING_FILES
            )
            if (
                applied.archive_digest != validated.artifact.archive_digest
                or applied.manifest_digest != validated.artifact.manifest_digest
                or applied.archive_size != validated.artifact.archive_size
                or applied.member_count != validated.artifact.member_count
                or applied.runtime_revision_id
                != str(context.manifest.runtime_revision_id)
            ):
                raise SnapshotRestoreError(
                    "Broker restore proof differs from the validated artifact.",
                    reconciliation_required=True,
                )
            self._coordinator.update_phase(
                self._lease, SnapshotRestoreOperationPhase.VERIFYING_DATABASES
            )
            after = self._validate_artifact(context.manifest)
            if after.artifact != validated.artifact:
                raise SnapshotRestoreError(
                    "Snapshot artifact identity changed during restore.",
                    reconciliation_required=True,
                )
            structural_restore_complete = True

            if prior is InstanceRuntimeState.RUNNING:
                self._coordinator.update_phase(
                    self._lease, SnapshotRestoreOperationPhase.RESTORING_RUNNING_STATE
                )
                self._broker.start_managed_container(
                    managed_instance_id=str(self._lease.managed_instance_id),
                    expected_container_id=fresh_id,
                    expected_runtime_state=ContainerRuntimeState.STOPPED,
                )
                self._prove_state(fresh_id, ContainerRuntimeState.RUNNING)
                self._coordinator.update_phase(
                    self._lease, SnapshotRestoreOperationPhase.WAITING_READINESS
                )
                readiness = self._readiness_factory(
                    self._broker, context.profile
                ).wait_until_usable(str(self._lease.managed_instance_id))
                if readiness.usability_state is not UsabilityState.USABLE:
                    self._stop_failed_running_restore(fresh_id)
                    raise SnapshotRestoreError(
                        "Snapshot bytes restored, but prior running state "
                        "was not recovered.",
                        reconciliation_required=True,
                    )

            self._coordinator.update_phase(
                self._lease, SnapshotRestoreOperationPhase.FINAL_VERIFICATION
            )
            self._verify_pins(context.manifest)
            final_state = (
                ContainerRuntimeState.RUNNING
                if prior is InstanceRuntimeState.RUNNING
                else ContainerRuntimeState.STOPPED
            )
            self._prove_state(fresh_id, final_state)
            return DisposableSnapshotRestoreResult(
                snapshot_id=self._lease.snapshot_id,
                managed_instance_id=self._lease.managed_instance_id,
                runtime_revision_id=context.manifest.runtime_revision_id,
                prior_runtime_state=prior,
                old_container_id=old.container_id,
                fresh_container_id=fresh_id,
                final_runtime_state=final_state,
                recognized_databases_checked=applied.recognized_databases_checked,
            )
        except SnapshotRestoreError as error:
            if not old_removed and stopped_by_operation:
                self._recover_old_running(context.profile, old.container_id, error)
            if old_removed and not error.reconciliation_required:
                raise SnapshotRestoreError(
                    error.safe_reason, reconciliation_required=True
                ) from error
            raise
        except Exception as error:
            if not old_removed and stopped_by_operation:
                wrapped = SnapshotRestoreError(
                    "Snapshot restore could not be completed."
                )
                self._recover_old_running(context.profile, old.container_id, wrapped)
            reason = (
                "Snapshot restore could not be completed after source removal."
                if old_removed
                else "Snapshot restore could not be completed."
            )
            raise SnapshotRestoreError(
                reason,
                reconciliation_required=old_removed or structural_restore_complete,
            ) from error

    def _load_context(self) -> _RestoreContext:
        with self._session_factory() as session:
            snapshot = SnapshotRepository(session).get(self._lease.snapshot_id)
            instance = ManagedInstanceRepository(session).get(
                self._lease.managed_instance_id
            )
            if snapshot is None or instance is None:
                raise SnapshotRestoreError("Snapshot restore state no longer exists.")
            if instance.instance_type is not InstanceType.DISPOSABLE:
                raise SnapshotRestoreError(
                    "Persistent snapshot restore is unsupported."
                )
            if (
                snapshot.source_managed_instance_id != instance.target_id
                or snapshot.runtime_revision_id != instance.runtime_revision_id
                or snapshot.captured_fixture_definition_id
                != instance.fixture_definition_id
                or snapshot.state is not SnapshotState.AVAILABLE
                or snapshot.validation_result is not SnapshotValidationResult.PASSED
                or snapshot.prior_runtime_state is None
            ):
                raise SnapshotRestoreError(
                    "Snapshot source, pin, fixture, or availability is incompatible."
                )
            revision = RuntimeRevisionRepository(session).get(
                instance.runtime_revision_id
            )
            profile = RuntimeExecutionProfileRepository(session).get(
                instance.runtime_revision_id
            )
            if revision is None or profile is None:
                raise SnapshotRestoreError("Pinned runtime state is unavailable.")
            definition = RuntimeDefinitionRepository(session).get(
                revision.runtime_definition_id
            )
            if definition is None:
                raise SnapshotRestoreError("Pinned runtime definition is unavailable.")
            return _RestoreContext(
                manifest=SnapshotCaptureContext(
                    snapshot_id=snapshot.id,
                    source_managed_instance_id=instance.target_id,
                    runtime_revision_id=revision.id,
                    kodi_release=definition.kodi_version,
                    captured_fixture_definition_id=instance.fixture_definition_id,
                    captured_at=snapshot.captured_at,
                    prior_runtime_state=snapshot.prior_runtime_state.value,
                ),
                profile=profile,
            )

    def _validate_artifact(self, context: SnapshotCaptureContext) -> SnapshotValidation:
        try:
            return self._validator(
                data_root=self._data_root,
                context=context,
                owner_gid=self._resolved_owner_gid(),
            )
        except SnapshotStorageError as error:
            raise SnapshotRestoreError(error.safe_message) from error

    def _exact_container(self) -> ManagedContainerInventoryEntry:
        matches = [
            item
            for item in self._broker.managed_container_inventory().containers
            if item.managed_instance_id == str(self._lease.managed_instance_id)
        ]
        if len(matches) != 1:
            raise SnapshotRestoreError(
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
            raise SnapshotRestoreError(
                "Managed container state could not be proven after mutation.",
                reconciliation_required=True,
            )
        return container

    def _prove_absence(self) -> None:
        if any(
            item.managed_instance_id == str(self._lease.managed_instance_id)
            for item in self._broker.managed_container_inventory().containers
        ):
            raise SnapshotRestoreError(
                "Old managed container absence could not be proven.",
                reconciliation_required=True,
            )

    def _verify_pins(self, context: SnapshotCaptureContext) -> None:
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(
                self._lease.managed_instance_id
            )
            if (
                instance is None
                or instance.runtime_revision_id != context.runtime_revision_id
                or instance.fixture_definition_id
                != context.captured_fixture_definition_id
            ):
                raise SnapshotRestoreError(
                    "Runtime pin or fixture assignment changed during restore.",
                    reconciliation_required=True,
                )

    def _recover_old_running(
        self,
        profile: RuntimeExecutionProfile,
        container_id: str,
        original: SnapshotRestoreError,
    ) -> None:
        try:
            self._broker.start_managed_container(
                managed_instance_id=str(self._lease.managed_instance_id),
                expected_container_id=container_id,
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
            self._prove_state(container_id, ContainerRuntimeState.RUNNING)
            readiness = self._readiness_factory(
                self._broker, profile
            ).wait_until_usable(str(self._lease.managed_instance_id))
            if readiness.usability_state is not UsabilityState.USABLE:
                raise RuntimeError("not usable")
        except Exception as recovery_error:
            raise SnapshotRestoreError(
                f"{original.safe_reason} Prior running state was not recovered.",
                reconciliation_required=True,
            ) from recovery_error

    def _stop_failed_running_restore(self, container_id: str) -> None:
        try:
            container = self._exact_container()
            if container.container_id != container_id:
                return
            if container.runtime_state is ContainerRuntimeState.RUNNING:
                self._broker.stop_managed_container(
                    managed_instance_id=str(self._lease.managed_instance_id),
                    expected_container_id=container_id,
                    expected_runtime_state=ContainerRuntimeState.RUNNING,
                )
        except Exception:
            return

    def _resolved_owner_gid(self) -> int:
        if self._owner_gid is not None:
            return self._owner_gid
        try:
            return grp.getgrnam("kodi-lab").gr_gid
        except KeyError as error:
            raise SnapshotRestoreError(
                "Dedicated snapshot storage group is unavailable."
            ) from error


type SnapshotRestoreServiceFactory = Callable[
    [SnapshotRestoreLease], DisposableSnapshotRestoreService
]


class SnapshotRestoreExecutor:
    """Single-worker runner governed by durable restore and shared leases."""

    def __init__(
        self,
        coordinator: SnapshotRestoreOperationCoordinator,
        service_factory: SnapshotRestoreServiceFactory,
    ) -> None:
        self._coordinator = coordinator
        self._service_factory = service_factory
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="restore")
        self._guard = Lock()
        self._accepting = True

    def start(self) -> None:
        for operation in self._coordinator.queued():
            self.submit(operation.id)

    def submit(self, operation_id: UUID) -> None:
        with self._guard:
            if not self._accepting:
                raise SnapshotRestoreError(
                    "Snapshot restore executor is shutting down."
                )
            self._pool.submit(self._execute, operation_id)

    def _execute(self, operation_id: UUID) -> None:
        try:
            lease = self._coordinator.acquire(operation_id)
        except SnapshotRestoreLeaseLostError:
            return
        if lease is None:
            return
        try:
            self._service_factory(lease).restore()
            self._coordinator.finish_success(lease)
        except SnapshotRestoreError as error:
            with suppress(SnapshotRestoreLeaseLostError):
                self._coordinator.finish_failure(
                    lease,
                    reason=error.safe_reason,
                    reconciliation_required=error.reconciliation_required,
                )
        except Exception:
            with suppress(SnapshotRestoreLeaseLostError):
                self._coordinator.finish_failure(
                    lease,
                    reason="Snapshot restore could not be completed.",
                    reconciliation_required=True,
                )

    def close(self) -> None:
        with self._guard:
            self._accepting = False
        self._pool.shutdown(wait=False, cancel_futures=True)
