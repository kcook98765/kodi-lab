"""Durable running-instance choreography for validated uploaded ZIP artifacts."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationAction,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.operations import (
    AddonZipDestinationResult,
    ApplyAddonZipInstallationResult,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.domain import (
    AddonZipArtifact,
    AddonZipExtensionClassification,
    AddonZipInstallationConfidence,
    AddonZipInstallationEvidence,
    AddonZipInstallationOperation,
    AddonZipInstallationOperationPhase,
    AddonZipInstallationOperationState,
    AddonZipInstallationResult,
    GlobalHeavyOperationKind,
    InstanceType,
    ManagedInstance,
    ManagedInstanceMutationKind,
    PythonBytecodePolicy,
    RuntimeExecutionProfile,
)
from kodi_lab.kodi.addons import (
    KodiAddonInventoryAdapter,
    KodiAddonInventoryRecord,
    KodiAddonInventoryResult,
)
from kodi_lab.kodi.readiness import (
    KodiReadinessCoordinator,
    ReadinessResult,
    UsabilityState,
)
from kodi_lab.storage.repositories import (
    AddonZipArtifactRepository,
    AddonZipInstallationEvidenceRepository,
    AddonZipInstallationOperationRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    RuntimeExecutionProfileRepository,
)

INSTALLATION_LEASE_DURATION = timedelta(minutes=15)
TERMINAL_RETENTION_PER_INSTANCE = 20
EXPECTED_KODI_TYPE = "xbmc.python.pluginsource"


class AddonZipInstallationError(RuntimeError):
    code = "addon_zip_installation_failed"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class AddonZipInstallationConflictError(AddonZipInstallationError):
    code = "addon_zip_installation_conflict"


class AddonZipInstallationUnsupportedError(AddonZipInstallationError):
    code = "addon_zip_installation_unsupported"


class AddonZipInstallationLeaseLostError(AddonZipInstallationError):
    code = "addon_zip_installation_lease_lost"


class AddonZipInstallationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    original_managed_instance_id: UUID
    managed_instance_id: UUID | None
    artifact_id: UUID
    addon_id: str = Field(max_length=128)
    addon_version: str = Field(max_length=128)
    state: AddonZipInstallationOperationState
    phase: AddonZipInstallationOperationPhase
    recognition_enabled: bool | None
    stopped_readback_matched: bool
    final_readback_matched: bool
    reconciliation_required: bool
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_reason: str | None = Field(default=None, max_length=256)


class _InstallationBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...

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

    def observe_addon_zip_destination(
        self,
        *,
        managed_instance_id: str,
        artifact_id: str,
        installation_operation_id: str,
        expected_container_id: str,
    ) -> AddonZipDestinationResult: ...

    def apply_addon_zip_installation(
        self,
        *,
        managed_instance_id: str,
        artifact_id: str,
        installation_operation_id: str,
        expected_container_id: str,
    ) -> ApplyAddonZipInstallationResult: ...


class _InventoryAdapter(Protocol):
    def get_inventory(self) -> KodiAddonInventoryResult: ...


class _ReadinessCoordinator(Protocol):
    def wait_until_usable(self, managed_instance_id: str) -> ReadinessResult: ...


type InventoryAdapterFactory = Callable[
    [ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile], _InventoryAdapter
]
type ReadinessCoordinatorFactory = Callable[
    [_InstallationBroker, RuntimeExecutionProfile], _ReadinessCoordinator
]


@dataclass(frozen=True, slots=True)
class AddonZipInstallationLease:
    operation_id: UUID
    instance_id: UUID
    artifact_id: UUID
    token: str

    @property
    def token_digest(self) -> str:
        return hashlib.sha256(self.token.encode()).hexdigest()


class AddonZipInstallationCoordinator:
    """Own one per-instance mutation lease and one global-heavy lease."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        stale_dispatcher: StaleOperationReconciliationDispatcher | None = None,
        reconciliation: ManagedInstanceReconciliationService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._stale = stale_dispatcher or StaleOperationReconciliationDispatcher(
            session_factory, clock=clock
        )
        self._reconciliation = reconciliation or ManagedInstanceReconciliationService(
            session_factory
        )

    def queue(self, instance_id: UUID, artifact_id: UUID) -> AddonZipInstallationStatus:
        self._stale.sweep_instance(instance_id)
        self._stale.sweep_global()
        self._reconciliation.require_allowed(
            instance_id, ReconciliationAction.ADDON_ZIP_INSTALLATION
        )
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            artifact = AddonZipArtifactRepository(session).get(artifact_id)
            active = AddonZipInstallationOperationRepository(
                session
            ).active_for_instance(instance_id)
        if instance is None or artifact is None:
            raise AddonZipInstallationUnsupportedError(
                "Managed instance or accepted ZIP artifact is unavailable."
            )
        if (
            instance.instance_type is not InstanceType.DISPOSABLE
            or artifact.extension_classification
            is not AddonZipExtensionClassification.PYTHON_VIDEO_PLUGIN
        ):
            raise AddonZipInstallationUnsupportedError(
                "Uploaded-ZIP installation supports disposable video-plugin "
                "targets only."
            )
        if active is not None:
            raise AddonZipInstallationConflictError(
                "An uploaded-ZIP installation is already active."
            )
        operation = AddonZipInstallationOperation(
            id=uuid4(),
            original_managed_instance_id=instance_id,
            managed_instance_id=instance_id,
            artifact_id=artifact_id,
            addon_id=artifact.addon_id,
            addon_version=artifact.addon_version,
            runtime_revision_id=instance.runtime_revision_id,
            requested_at=self._clock(),
            state=AddonZipInstallationOperationState.QUEUED,
            phase=AddonZipInstallationOperationPhase.QUEUED,
        )
        try:
            with self._session_factory.begin() as session:
                AddonZipInstallationOperationRepository(session).create(operation)
                ManagedInstanceMutationLeaseRepository(session).reserve(
                    instance_id,
                    kind=ManagedInstanceMutationKind.ADDON_ZIP_INSTALLATION,
                    operation_id=operation.id,
                )
                GlobalHeavyOperationLeaseRepository(session).reserve(
                    kind=GlobalHeavyOperationKind.ADDON_ZIP_INSTALLATION,
                    operation_id=operation.id,
                )
        except IntegrityError as error:
            raise AddonZipInstallationConflictError(
                "Another managed mutation or global-heavy operation is active."
            ) from error
        return self.to_status(operation)

    def acquire(self, operation_id: UUID) -> AddonZipInstallationLease | None:
        with self._session_factory() as session:
            operation = AddonZipInstallationOperationRepository(session).get(
                operation_id
            )
        if (
            operation is None
            or operation.managed_instance_id is None
            or operation.state is not AddonZipInstallationOperationState.QUEUED
        ):
            return None
        lease = AddonZipInstallationLease(
            operation.id,
            operation.managed_instance_id,
            operation.artifact_id,
            secrets.token_urlsafe(32),
        )
        now = self._clock()
        expires = now + INSTALLATION_LEASE_DURATION
        with self._session_factory.begin() as session:
            operation_claimed = AddonZipInstallationOperationRepository(
                session
            ).acquire(
                lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            mutation_claimed = ManagedInstanceMutationLeaseRepository(session).claim(
                lease.instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            heavy_claimed = GlobalHeavyOperationLeaseRepository(session).claim(
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            if not (operation_claimed and mutation_claimed and heavy_claimed):
                raise AddonZipInstallationLeaseLostError(
                    "Uploaded-ZIP installation authority could not be acquired."
                )
        return lease

    def update(
        self,
        lease: AddonZipInstallationLease,
        *,
        phase: AddonZipInstallationOperationPhase | None = None,
        **values: object,
    ) -> None:
        now = self._clock()
        expires = now + INSTALLATION_LEASE_DURATION
        if phase is not None:
            values["phase"] = phase
        values["heartbeat_at"] = now
        values["lease_expires_at"] = expires
        with self._session_factory.begin() as session:
            updated = AddonZipInstallationOperationRepository(session).update_fields(
                lease.operation_id,
                token_digest=lease.token_digest,
                **values,
            )
            mutation = ManagedInstanceMutationLeaseRepository(session).renew(
                lease.instance_id,
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            heavy = GlobalHeavyOperationLeaseRepository(session).renew(
                operation_id=lease.operation_id,
                token_digest=lease.token_digest,
                now=now,
                expires_at=expires,
            )
            if not (updated and mutation and heavy):
                raise AddonZipInstallationLeaseLostError(
                    "Uploaded-ZIP installation authority was lost."
                )

    def finish_success(
        self,
        lease: AddonZipInstallationLease,
        *,
        container_id: str,
        artifact: AddonZipArtifact,
        runtime_revision_id: UUID,
    ) -> None:
        now = self._clock()
        evidence = AddonZipInstallationEvidence(
            managed_instance_id=lease.instance_id,
            original_managed_instance_id=lease.instance_id,
            artifact_id=lease.artifact_id,
            installation_operation_id=lease.operation_id,
            runtime_revision_id=runtime_revision_id,
            trusted_container_id=container_id,
            addon_id=artifact.addon_id,
            addon_version=artifact.addon_version,
            payload_manifest_digest=artifact.payload_manifest_digest,
            verified_at=now,
            confidence=AddonZipInstallationConfidence.VERIFIED_AT_LAST_OPERATION,
            result=AddonZipInstallationResult.INSTALLED_RECOGNIZED_DISABLED,
        )
        with self._session_factory.begin() as session:
            AddonZipInstallationEvidenceRepository(session).upsert(evidence)
            self._finish(
                session,
                lease,
                state=AddonZipInstallationOperationState.SUCCEEDED,
                reason=None,
                now=now,
            )

    def finish_failure(
        self,
        lease: AddonZipInstallationLease,
        *,
        reason: str,
        reconciliation_required: bool,
    ) -> None:
        state = (
            AddonZipInstallationOperationState.RECONCILIATION_REQUIRED
            if reconciliation_required
            else AddonZipInstallationOperationState.FAILED
        )
        with self._session_factory.begin() as session:
            if reconciliation_required:
                AddonZipInstallationEvidenceRepository(session).mark_confidence(
                    lease.instance_id, AddonZipInstallationConfidence.UNKNOWN
                )
            self._finish(
                session,
                lease,
                state=state,
                reason=reason[:256],
                now=self._clock(),
            )

    def _finish(
        self,
        session: Session,
        lease: AddonZipInstallationLease,
        *,
        state: AddonZipInstallationOperationState,
        reason: str | None,
        now: datetime,
    ) -> None:
        if not AddonZipInstallationOperationRepository(session).finish(
            lease.operation_id,
            token_digest=lease.token_digest,
            state=state,
            now=now,
            reason=reason,
        ):
            raise AddonZipInstallationLeaseLostError(
                "Uploaded-ZIP operation finalization could not be proven."
            )
        if not ManagedInstanceMutationLeaseRepository(session).release(
            lease.instance_id,
            operation_id=lease.operation_id,
            token_digest=lease.token_digest,
        ) or not GlobalHeavyOperationLeaseRepository(session).release(
            operation_id=lease.operation_id,
            token_digest=lease.token_digest,
        ):
            raise AddonZipInstallationLeaseLostError(
                "Uploaded-ZIP lease release could not be proven."
            )
        AddonZipInstallationOperationRepository(session).prune_terminal(
            lease.instance_id, retain=TERMINAL_RETENTION_PER_INSTANCE
        )

    def get(self, operation_id: UUID) -> AddonZipInstallationOperation | None:
        with self._session_factory() as session:
            return AddonZipInstallationOperationRepository(session).get(operation_id)

    def queued(self) -> list[AddonZipInstallationOperation]:
        with self._session_factory() as session:
            return AddonZipInstallationOperationRepository(session).list_queued()

    @staticmethod
    def to_status(
        operation: AddonZipInstallationOperation,
    ) -> AddonZipInstallationStatus:
        return AddonZipInstallationStatus(
            operation_id=operation.id,
            original_managed_instance_id=operation.original_managed_instance_id,
            managed_instance_id=operation.managed_instance_id,
            artifact_id=operation.artifact_id,
            addon_id=operation.addon_id,
            addon_version=operation.addon_version,
            state=operation.state,
            phase=operation.phase,
            recognition_enabled=operation.recognition_enabled,
            stopped_readback_matched=operation.stopped_readback_matched,
            final_readback_matched=operation.final_readback_matched,
            reconciliation_required=operation.reconciliation_required,
            requested_at=operation.requested_at,
            started_at=operation.started_at,
            completed_at=operation.completed_at,
            failure_reason=operation.failure_reason,
        )


class AddonZipInstallationService:
    """Run the closed stop/materialize/restart/recognize/readback sequence."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _InstallationBroker,
        coordinator: AddonZipInstallationCoordinator,
        lease: AddonZipInstallationLease,
        *,
        inventory_adapter_factory: InventoryAdapterFactory | None = None,
        readiness_factory: ReadinessCoordinatorFactory = KodiReadinessCoordinator,
        after_application_hook: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._coordinator = coordinator
        self._lease = lease
        self._inventory_factory = inventory_adapter_factory or (
            lambda observation, profile: KodiAddonInventoryAdapter(observation, profile)
        )
        self._readiness_factory = readiness_factory
        self._after_application_hook = after_application_hook

    def install(self) -> None:
        stopped = False
        application_started = False
        container_id: str | None = None
        profile: RuntimeExecutionProfile | None = None
        try:
            instance, artifact, profile = self._load_state()
            observation, container_id = self._require_running_runtime(
                instance.runtime_revision_id
            )
            self._coordinator.update(self._lease, expected_container_id=container_id)
            before = self._inventory(observation, profile)
            if any(item.addon_id == artifact.addon_id for item in before.items):
                raise AddonZipInstallationConflictError(
                    "The add-on ID already exists in live Kodi inventory."
                )
            destination = self._observe_destination(container_id)
            if destination.destination_state != "absent":
                raise AddonZipInstallationConflictError(
                    "The managed add-on destination already exists."
                )
            dependency_baseline = self._dependency_state(artifact, before.items)
            self._coordinator.update(
                self._lease,
                dependency_baseline_json=self._dependency_json(dependency_baseline),
                phase=AddonZipInstallationOperationPhase.STOPPING,
            )
            stopped_result = self._broker.stop_managed_container(
                managed_instance_id=str(self._lease.instance_id),
                expected_container_id=container_id,
                expected_runtime_state=ContainerRuntimeState.RUNNING,
            )
            if (
                stopped_result.container_id != container_id
                or stopped_result.runtime_state is not ContainerRuntimeState.STOPPED
            ):
                raise AddonZipInstallationError(
                    "The exact managed container did not reach stopped state."
                )
            stopped = True
            stopped_destination = self._observe_destination(container_id)
            if stopped_destination.destination_state != "absent":
                raise AddonZipInstallationConflictError(
                    "The add-on destination appeared before application."
                )
            self._coordinator.update(
                self._lease,
                phase=AddonZipInstallationOperationPhase.APPLYING,
                application_started=True,
            )
            application_started = True
            applied = self._apply(container_id)
            if not applied.matched:
                raise AddonZipInstallationError(
                    "Stopped payload readback did not match."
                )
            if self._after_application_hook is not None:
                self._after_application_hook()
            self._coordinator.update(
                self._lease,
                phase=AddonZipInstallationOperationPhase.VERIFY_STOPPED,
                stopped_readback_matched=True,
            )
            stopped_readback = self._observe_destination(container_id)
            if not stopped_readback.matched:
                raise AddonZipInstallationError(
                    "Exact stopped destination verification failed."
                )
            self._coordinator.update(
                self._lease, phase=AddonZipInstallationOperationPhase.RESTARTING
            )
            started = self._broker.start_managed_container(
                managed_instance_id=str(self._lease.instance_id),
                expected_container_id=container_id,
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
            if (
                started.container_id != container_id
                or started.runtime_state is not ContainerRuntimeState.RUNNING
            ):
                raise AddonZipInstallationError(
                    "The exact managed container did not restart."
                )
            stopped = False
            self._coordinator.update(
                self._lease, phase=AddonZipInstallationOperationPhase.READINESS
            )
            readiness = self._readiness_factory(
                self._broker, profile
            ).wait_until_usable(str(self._lease.instance_id))
            if readiness.usability_state is not UsabilityState.USABLE:
                raise AddonZipInstallationError(
                    "The restarted Kodi runtime did not become usable."
                )
            self._coordinator.update(
                self._lease, phase=AddonZipInstallationOperationPhase.RECOGNITION
            )
            after_observation, observed_container = self._require_running_runtime(
                instance.runtime_revision_id
            )
            if observed_container != container_id:
                raise AddonZipInstallationError(
                    "Managed container identity changed after restart."
                )
            after = self._inventory(after_observation, profile)
            recognized = next(
                (item for item in after.items if item.addon_id == artifact.addon_id),
                None,
            )
            if recognized is None:
                raise AddonZipInstallationError(
                    "Kodi did not recognize the installed add-on."
                )
            self._coordinator.update(
                self._lease, recognition_enabled=recognized.enabled
            )
            if (
                recognized.version != artifact.addon_version
                or recognized.kodi_type != EXPECTED_KODI_TYPE
                or recognized.enabled
            ):
                raise AddonZipInstallationError(
                    "Kodi recognition did not match the exact disabled postcondition."
                )
            dependency_post = self._dependency_state(artifact, after.items)
            self._coordinator.update(
                self._lease,
                dependency_post_json=self._dependency_json(dependency_post),
            )
            if dependency_post != dependency_baseline:
                raise AddonZipInstallationError(
                    "A declared dependency state changed during installation."
                )
            self._coordinator.update(
                self._lease, phase=AddonZipInstallationOperationPhase.VERIFY_FINAL
            )
            final_readback = self._observe_destination(container_id)
            if not final_readback.matched:
                raise AddonZipInstallationError(
                    "Final destination readback did not match."
                )
            self._coordinator.update(self._lease, final_readback_matched=True)
            self._coordinator.finish_success(
                self._lease,
                container_id=container_id,
                artifact=artifact,
                runtime_revision_id=instance.runtime_revision_id,
            )
        except Exception as error:
            if application_started:
                self._leave_stopped_if_exact(container_id)
                self._safe_finish_failure(error, reconciliation=True)
            else:
                if stopped:
                    self._restore_prior_running(container_id, profile)
                self._safe_finish_failure(error, reconciliation=False)
            raise

    def _load_state(
        self,
    ) -> tuple[ManagedInstance, AddonZipArtifact, RuntimeExecutionProfile]:
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(self._lease.instance_id)
            artifact = AddonZipArtifactRepository(session).get(self._lease.artifact_id)
            operation = AddonZipInstallationOperationRepository(session).get(
                self._lease.operation_id
            )
            profile = (
                None
                if instance is None
                else RuntimeExecutionProfileRepository(session).get(
                    instance.runtime_revision_id
                )
            )
        if instance is None or artifact is None or profile is None or operation is None:
            raise AddonZipInstallationUnsupportedError(
                "Canonical installation state is unavailable."
            )
        if (
            instance.instance_type is not InstanceType.DISPOSABLE
            or profile.python_bytecode_policy is not PythonBytecodePolicy.SUPPRESS
            or artifact.extension_classification
            is not AddonZipExtensionClassification.PYTHON_VIDEO_PLUGIN
            or operation.managed_instance_id != instance.target_id
            or operation.artifact_id != artifact.id
            or operation.runtime_revision_id != instance.runtime_revision_id
            or operation.addon_id != artifact.addon_id
            or operation.addon_version != artifact.addon_version
        ):
            raise AddonZipInstallationUnsupportedError(
                "The instance runtime or artifact is unsupported."
            )
        return instance, artifact, profile

    def _require_running_runtime(
        self, runtime_revision_id: UUID
    ) -> tuple[ObserveManagedKodiRuntimeResult, str]:
        items = [
            item
            for item in self._broker.managed_container_inventory().containers
            if item.managed_instance_id == str(self._lease.instance_id)
        ]
        if (
            len(items) != 1
            or items[0].runtime_state is not ContainerRuntimeState.RUNNING
            or items[0].health_state is not ContainerHealthState.HEALTHY
        ):
            raise AddonZipInstallationUnsupportedError(
                "Installation requires one healthy running managed container."
            )
        container = items[0]
        observation = self._broker.observe_managed_kodi_runtime(
            managed_instance_id=str(self._lease.instance_id),
            expected_container_id=container.container_id,
        )
        if (
            observation.managed_instance_id != str(self._lease.instance_id)
            or observation.container_id != container.container_id
            or observation.runtime_revision_id != str(runtime_revision_id)
            or observation.runtime_state is not ContainerRuntimeState.RUNNING
            or observation.process_state is not KodiProcessState.RUNNING
        ):
            raise AddonZipInstallationUnsupportedError(
                "The live runtime identity is not exact."
            )
        return observation, container.container_id

    def _inventory(
        self,
        observation: ObserveManagedKodiRuntimeResult,
        profile: RuntimeExecutionProfile,
    ) -> KodiAddonInventoryResult:
        result = self._inventory_factory(observation, profile).get_inventory()
        if result.identity.kodi_release != profile.kodi_release:
            raise AddonZipInstallationUnsupportedError(
                "Kodi inventory identity differs from the runtime profile."
            )
        return result

    def _observe_destination(self, container_id: str) -> AddonZipDestinationResult:
        return self._broker.observe_addon_zip_destination(
            managed_instance_id=str(self._lease.instance_id),
            artifact_id=str(self._lease.artifact_id),
            installation_operation_id=str(self._lease.operation_id),
            expected_container_id=container_id,
        )

    def _apply(self, container_id: str) -> ApplyAddonZipInstallationResult:
        return self._broker.apply_addon_zip_installation(
            managed_instance_id=str(self._lease.instance_id),
            artifact_id=str(self._lease.artifact_id),
            installation_operation_id=str(self._lease.operation_id),
            expected_container_id=container_id,
        )

    @staticmethod
    def _dependency_state(
        artifact: AddonZipArtifact,
        inventory: tuple[KodiAddonInventoryRecord, ...],
    ) -> tuple[tuple[str, bool, bool | None, str | None], ...]:
        by_id = {item.addon_id: item for item in inventory}
        return tuple(
            (
                dependency.dependency_id,
                dependency.dependency_id in by_id,
                None
                if dependency.dependency_id not in by_id
                else by_id[dependency.dependency_id].enabled,
                None
                if dependency.dependency_id not in by_id
                else by_id[dependency.dependency_id].version,
            )
            for dependency in artifact.dependencies
        )

    @staticmethod
    def _dependency_json(
        state: tuple[tuple[str, bool, bool | None, str | None], ...],
    ) -> str:
        value = json.dumps(
            [
                {
                    "id": item[0],
                    "present": item[1],
                    "enabled": item[2],
                    "version": item[3],
                }
                for item in state
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(value.encode()) > 16 * 1024:
            raise AddonZipInstallationUnsupportedError(
                "Dependency observation exceeds the fixed evidence bound."
            )
        return value

    def _restore_prior_running(
        self,
        container_id: str | None,
        profile: RuntimeExecutionProfile | None,
    ) -> None:
        if container_id is None or profile is None:
            return
        with suppress(Exception):
            result = self._broker.start_managed_container(
                managed_instance_id=str(self._lease.instance_id),
                expected_container_id=container_id,
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
            if result.runtime_state is ContainerRuntimeState.RUNNING:
                self._readiness_factory(self._broker, profile).wait_until_usable(
                    str(self._lease.instance_id)
                )

    def _leave_stopped_if_exact(self, container_id: str | None) -> None:
        if container_id is None:
            return
        with suppress(Exception):
            matches = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == str(self._lease.instance_id)
                and item.container_id == container_id
            ]
            if (
                len(matches) == 1
                and matches[0].runtime_state is ContainerRuntimeState.RUNNING
            ):
                self._broker.stop_managed_container(
                    managed_instance_id=str(self._lease.instance_id),
                    expected_container_id=container_id,
                    expected_runtime_state=ContainerRuntimeState.RUNNING,
                )

    def _safe_finish_failure(self, error: Exception, *, reconciliation: bool) -> None:
        reason = (
            error.code
            if isinstance(error, AddonZipInstallationError)
            else "addon_zip_installation_failed"
        )
        with suppress(Exception):
            self._coordinator.finish_failure(
                self._lease,
                reason=reason,
                reconciliation_required=reconciliation,
            )


type AddonZipInstallationServiceFactory = Callable[
    [AddonZipInstallationLease], AddonZipInstallationService
]


class AddonZipInstallationExecutor:
    def __init__(
        self,
        coordinator: AddonZipInstallationCoordinator,
        service_factory: AddonZipInstallationServiceFactory,
    ) -> None:
        self._coordinator = coordinator
        self._service_factory = service_factory
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="addon-zip-install"
        )
        self._guard = Lock()
        self._accepting = True

    def start(self) -> None:
        for operation in self._coordinator.queued():
            self.submit(operation.id)

    def submit(self, operation_id: UUID) -> None:
        with self._guard:
            if not self._accepting:
                raise RuntimeError("uploaded-ZIP installation executor is closed")
            self._pool.submit(self._run, operation_id)

    def _run(self, operation_id: UUID) -> None:
        try:
            lease = self._coordinator.acquire(operation_id)
            if lease is not None:
                self._service_factory(lease).install()
        except Exception:
            return

    def close(self) -> None:
        with self._guard:
            self._accepting = False
        self._pool.shutdown(wait=True, cancel_futures=False)


def invalidate_addon_zip_installation_evidence(
    session: Session, instance_id: UUID
) -> int:
    """Mark retained ZIP provenance stale before destructive runtime mutation."""

    return AddonZipInstallationEvidenceRepository(session).mark_confidence(
        instance_id, AddonZipInstallationConfidence.STALE
    )
