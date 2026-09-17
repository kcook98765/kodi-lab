"""Bounded destructive reset orchestration for disposable managed instances."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_deployments import (
    invalidate_development_source_deployments,
)
from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.reset_activity import (
    ResetActivityCapacityError,
    ResetActivityConflictError,
    ResetActivityRegistry,
    ResetActivitySnapshot,
    ResetActivityState,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.operations import (
    ApplyAssignedFixtureResult,
    DeprovisionManagedContainerResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    ProvisionManagedContainerResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.domain import (
    FixtureDefinition,
    InstanceType,
    ManagedInstance,
    RuntimeExecutionProfile,
)
from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.fixtures.semantics import FixtureSemanticSpecification
from kodi_lab.kodi.fixtures import (
    FixtureLibraryState,
    SmallMixedFixtureKodiAdapter,
)
from kodi_lab.kodi.readiness import (
    KodiReadinessCoordinator,
    ReadinessResult,
    UsabilityState,
)
from kodi_lab.storage.repositories import (
    FixtureDefinitionRepository,
    ManagedInstanceRepository,
    RuntimeExecutionProfileRepository,
)


class ResetPhase(StrEnum):
    PREFLIGHT = "preflight"
    STOP = "stop"
    DEPROVISION = "deprovision"
    PROVISION = "provision"
    FIXTURE_APPLY = "fixture_apply"
    FIXTURE_START = "fixture_start"
    READINESS = "readiness"
    FIXTURE_INITIALIZATION = "fixture_initialization"
    FINAL_STOP = "final_stop"
    FINAL_VERIFY = "final_verify"


class ResetError(RuntimeError):
    code = "reset_failed"

    def __init__(self, phase: ResetPhase, safe_reason: str) -> None:
        self.phase = phase
        self.safe_reason = safe_reason
        super().__init__(f"{self.code}: {safe_reason}")


class PersistentResetUnsupportedError(ResetError):
    code = "persistent_reset_unsupported"


class ResetInProgressError(ResetError):
    code = "reset_in_progress"


class ResetStateConflictError(ResetError):
    code = "reset_state_conflict"


class ResetStopFailedError(ResetError):
    code = "reset_stop_failed"


class ResetDeprovisionFailedError(ResetError):
    code = "reset_deprovision_failed"


class ResetProvisionFailedError(ResetError):
    code = "reset_provision_failed"


class FixtureApplyFailedError(ResetError):
    code = "fixture_apply_failed"


class FixtureInitializationFailedError(ResetError):
    code = "fixture_initialization_failed"


class ResetReconciliationRequiredError(ResetError):
    code = "reset_reconciliation_required"


class FixtureInitializationResult(StrEnum):
    NOT_ASSIGNED = "not_assigned"
    INITIALIZED = "initialized"


@dataclass(frozen=True, slots=True)
class DisposableResetResult:
    managed_instance_id: UUID
    runtime_revision_id: UUID
    outcome: str
    final_provision_state: str
    final_container_state: ContainerRuntimeState
    fixture_slug: str | None
    fixture_version: int | None
    fixture_initialization: FixtureInitializationResult
    fixture_state: FixtureLibraryState | None


class _ResetBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

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
        expected_runtime_state: ContainerRuntimeState,
    ) -> DeprovisionManagedContainerResult: ...

    def apply_assigned_fixture(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ApplyAssignedFixtureResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...


class _ReadinessWaiter(Protocol):
    def wait_until_usable(self, managed_instance_id: str) -> ReadinessResult: ...


class _FixtureAdapter(Protocol):
    def scan_assigned_fixture_library(self) -> None: ...

    def wait_for_fixture_state(self) -> FixtureLibraryState: ...


class ResetActivityObserver(Protocol):
    def begin(self, instance_id: UUID) -> ResetActivitySnapshot: ...

    def transition(
        self, instance_id: UUID, generation: int, state: ResetActivityState
    ) -> ResetActivitySnapshot: ...

    def succeed(self, instance_id: UUID, generation: int) -> ResetActivitySnapshot: ...

    def fail(
        self,
        instance_id: UUID,
        generation: int,
        *,
        failure_phase: str,
        failure_reason: str,
        reconciliation_required: bool,
    ) -> ResetActivitySnapshot: ...


type FixtureAdapterFactory = Callable[
    [
        ObserveManagedKodiRuntimeResult,
        RuntimeExecutionProfile,
        FixtureSemanticSpecification,
    ],
    _FixtureAdapter,
]


class PerInstanceResetLocks:
    """Process-local keyed exclusion; not a distributed coordination claim."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._locks: dict[UUID, Lock] = {}

    def acquire(self, instance_id: UUID) -> Lock | None:
        with self._guard:
            lock = self._locks.setdefault(instance_id, Lock())
        return lock if lock.acquire(blocking=False) else None


class DisposableInstanceResetService:
    """Recreate one exact pin and optionally initialize its assigned fixture."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _ResetBroker,
        *,
        locks: PerInstanceResetLocks | None = None,
        activity: ResetActivityObserver | None = None,
        readiness_factory: Callable[
            [_ResetBroker, RuntimeExecutionProfile], _ReadinessWaiter
        ] = KodiReadinessCoordinator,
        fixture_adapter_factory: FixtureAdapterFactory = SmallMixedFixtureKodiAdapter,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._locks = locks or PerInstanceResetLocks()
        self._activity = activity or ResetActivityRegistry()
        self._readiness_factory = readiness_factory
        self._fixture_adapter_factory = fixture_adapter_factory

    def reset(self, managed_instance_id: UUID) -> DisposableResetResult:
        lock = self._locks.acquire(managed_instance_id)
        if lock is None:
            raise ResetInProgressError(
                ResetPhase.PREFLIGHT, "another reset is already in progress"
            )
        try:
            try:
                activity = self._activity.begin(managed_instance_id)
            except (ResetActivityConflictError, ResetActivityCapacityError) as error:
                raise ResetInProgressError(
                    ResetPhase.PREFLIGHT, "another reset is already in progress"
                ) from error
            try:
                result = self._reset_locked(managed_instance_id, activity.generation)
            except ResetError as error:
                self._activity.fail(
                    managed_instance_id,
                    activity.generation,
                    failure_phase=error.phase.value,
                    failure_reason=error.safe_reason,
                    reconciliation_required=isinstance(
                        error, ResetReconciliationRequiredError
                    ),
                )
                raise
            except Exception:
                self._activity.fail(
                    managed_instance_id,
                    activity.generation,
                    failure_phase=ResetPhase.PREFLIGHT.value,
                    failure_reason="Reset could not be completed.",
                    reconciliation_required=False,
                )
                raise
            self._activity.succeed(managed_instance_id, activity.generation)
            return result
        finally:
            lock.release()

    @property
    def activity_registry(self) -> ResetActivityObserver:
        return self._activity

    def _reset_locked(
        self, instance_id: UUID, activity_generation: int
    ) -> DisposableResetResult:
        instance, profile, fixture = self._load_context(instance_id)
        if instance.instance_type is not InstanceType.DISPOSABLE:
            raise PersistentResetUnsupportedError(
                ResetPhase.PREFLIGHT, "persistent managed instances cannot be reset"
            )
        pinned_revision = instance.runtime_revision_id
        packaged_fixture = None
        if fixture is not None:
            try:
                packaged_fixture = load_packaged_fixture(fixture.slug)
            except Exception as error:
                raise ResetStateConflictError(
                    ResetPhase.PREFLIGHT,
                    "assigned fixture package is unavailable or invalid",
                ) from error
            if packaged_fixture.definition != fixture:
                raise ResetStateConflictError(
                    ResetPhase.PREFLIGHT,
                    "assigned fixture differs from its trusted package",
                )
        container = self._find_container(instance_id)
        if container is not None:
            if container.runtime_state is ContainerRuntimeState.RUNNING:
                try:
                    self._activity.transition(
                        instance_id,
                        activity_generation,
                        ResetActivityState.STOPPING,
                    )
                    self._broker.stop_managed_container(
                        managed_instance_id=str(instance_id),
                        expected_container_id=container.container_id,
                        expected_runtime_state=container.runtime_state,
                    )
                    container = self._require_stopped_container(instance_id)
                except Exception as error:
                    raise ResetStopFailedError(
                        ResetPhase.STOP,
                        "existing container could not be safely stopped",
                    ) from error
            elif container.runtime_state is not ContainerRuntimeState.STOPPED:
                raise ResetStateConflictError(
                    ResetPhase.PREFLIGHT,
                    "container state is transitional or unknown",
                )
            try:
                self._activity.transition(
                    instance_id,
                    activity_generation,
                    ResetActivityState.DEPROVISIONING,
                )
                with self._session_factory.begin() as session:
                    invalidate_development_source_deployments(session, instance_id)
                self._broker.deprovision_managed_container(
                    managed_instance_id=str(instance_id),
                    expected_container_id=container.container_id,
                    expected_runtime_revision_id=str(pinned_revision),
                    expected_runtime_state=ContainerRuntimeState.STOPPED,
                )
            except Exception as error:
                raise ResetDeprovisionFailedError(
                    ResetPhase.DEPROVISION,
                    "existing container could not be safely deprovisioned",
                ) from error
            try:
                if self._find_container(instance_id) is not None:
                    raise RuntimeError("container remains")
            except Exception as error:
                raise ResetDeprovisionFailedError(
                    ResetPhase.DEPROVISION,
                    "container absence could not be established",
                ) from error
        try:
            self._activity.transition(
                instance_id,
                activity_generation,
                ResetActivityState.PROVISIONING,
            )
            provisioned = self._broker.provision_managed_container(
                managed_instance_id=str(instance_id)
            )
        except Exception as error:
            raise ResetProvisionFailedError(
                ResetPhase.PROVISION,
                "the exact pinned runtime could not be provisioned",
            ) from error
        if (
            provisioned.runtime_revision_id != str(pinned_revision)
            or provisioned.runtime_state is not ContainerRuntimeState.STOPPED
        ):
            raise ResetReconciliationRequiredError(
                ResetPhase.PROVISION,
                "provisioning returned an unexpected revision or state",
            )

        fixture_state: FixtureLibraryState | None = None
        if fixture is not None:
            try:
                self._activity.transition(
                    instance_id,
                    activity_generation,
                    ResetActivityState.APPLYING_FIXTURE,
                )
                applied = self._broker.apply_assigned_fixture(
                    managed_instance_id=str(instance_id),
                    expected_container_id=provisioned.container_id,
                )
            except Exception as error:
                try:
                    self._require_stopped_container(instance_id)
                except Exception as cleanup_error:
                    raise ResetReconciliationRequiredError(
                        ResetPhase.FIXTURE_APPLY,
                        "fixture application failed and stopped state is unproven",
                    ) from cleanup_error
                raise FixtureApplyFailedError(
                    ResetPhase.FIXTURE_APPLY,
                    "the trusted assigned fixture archive could not be applied",
                ) from error
            if (
                applied.runtime_revision_id != str(pinned_revision)
                or applied.fixture_slug != fixture.slug
                or applied.fixture_version != fixture.fixture_version
                or applied.content_digest != fixture.content_digest
            ):
                raise ResetReconciliationRequiredError(
                    ResetPhase.FIXTURE_APPLY,
                    "fixture application returned inconsistent trusted identity",
                )
            assert packaged_fixture is not None
            fixture_state = self._initialize_fixture(
                instance_id,
                provisioned.container_id,
                profile,
                packaged_fixture.semantics,
                activity_generation,
            )

        self._activity.transition(
            instance_id,
            activity_generation,
            ResetActivityState.FINAL_VERIFYING,
        )
        final_container = self._require_stopped_container(instance_id)
        final_instance, _final_profile, final_fixture = self._load_context(instance_id)
        if (
            final_instance.target_id != instance.target_id
            or final_instance.runtime_revision_id != pinned_revision
            or final_instance.fixture_definition_id != instance.fixture_definition_id
            or final_fixture != fixture
        ):
            raise ResetReconciliationRequiredError(
                ResetPhase.FINAL_VERIFY,
                "managed-instance identity, pin, or fixture assignment changed",
            )
        return DisposableResetResult(
            managed_instance_id=instance_id,
            runtime_revision_id=pinned_revision,
            outcome="succeeded",
            final_provision_state="provisioned",
            final_container_state=final_container.runtime_state,
            fixture_slug=None if fixture is None else fixture.slug,
            fixture_version=None if fixture is None else fixture.fixture_version,
            fixture_initialization=(
                FixtureInitializationResult.NOT_ASSIGNED
                if fixture is None
                else FixtureInitializationResult.INITIALIZED
            ),
            fixture_state=fixture_state,
        )

    def _initialize_fixture(
        self,
        instance_id: UUID,
        container_id: str,
        profile: RuntimeExecutionProfile,
        semantics: FixtureSemanticSpecification,
        activity_generation: int,
    ) -> FixtureLibraryState:
        try:
            self._activity.transition(
                instance_id,
                activity_generation,
                ResetActivityState.STARTING_FOR_FIXTURE,
            )
            self._broker.start_managed_container(
                managed_instance_id=str(instance_id),
                expected_container_id=container_id,
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
        except Exception as error:
            self._raise_after_cleanup(
                instance_id,
                activity_generation,
                ResetPhase.FIXTURE_START,
                "fixture initialization container start failed",
                error,
            )
        try:
            self._activity.transition(
                instance_id,
                activity_generation,
                ResetActivityState.WAITING_READINESS,
            )
            readiness = self._readiness_factory(
                self._broker, profile
            ).wait_until_usable(str(instance_id))
            if readiness.usability_state is not UsabilityState.USABLE:
                raise RuntimeError("Kodi readiness did not reach usable state")
        except Exception as error:
            self._raise_after_cleanup(
                instance_id,
                activity_generation,
                ResetPhase.READINESS,
                "fixture initialization readiness failed",
                error,
            )
        try:
            running = self._require_running_container(instance_id)
            observation = self._broker.observe_managed_kodi_runtime(
                managed_instance_id=str(instance_id),
                expected_container_id=running.container_id,
            )
            adapter = self._fixture_adapter_factory(observation, profile, semantics)
            self._activity.transition(
                instance_id,
                activity_generation,
                ResetActivityState.SCANNING_FIXTURE,
            )
            adapter.scan_assigned_fixture_library()
            self._activity.transition(
                instance_id,
                activity_generation,
                ResetActivityState.VERIFYING_FIXTURE,
            )
            state = adapter.wait_for_fixture_state()
            if not state.is_baseline:
                raise RuntimeError("fixture adapter returned a non-baseline state")
        except Exception as error:
            self._raise_after_cleanup(
                instance_id,
                activity_generation,
                ResetPhase.FIXTURE_INITIALIZATION,
                "fixture scan or semantic verification failed",
                error,
            )
        try:
            self._activity.transition(
                instance_id,
                activity_generation,
                ResetActivityState.FINAL_STOPPING,
            )
            running = self._require_running_container(instance_id)
            self._broker.stop_managed_container(
                managed_instance_id=str(instance_id),
                expected_container_id=running.container_id,
                expected_runtime_state=ContainerRuntimeState.RUNNING,
            )
            self._require_stopped_container(instance_id)
        except Exception as error:
            raise ResetReconciliationRequiredError(
                ResetPhase.FINAL_STOP,
                "fixture baseline was created but a stopped state was not established",
            ) from error
        return state

    def _raise_after_cleanup(
        self,
        instance_id: UUID,
        activity_generation: int,
        phase: ResetPhase,
        message: str,
        cause: Exception,
    ) -> None:
        try:
            container = self._find_container(instance_id)
            if container is None:
                raise RuntimeError("container is absent")
            if container.runtime_state is ContainerRuntimeState.RUNNING:
                self._activity.transition(
                    instance_id,
                    activity_generation,
                    ResetActivityState.FINAL_STOPPING,
                )
                self._broker.stop_managed_container(
                    managed_instance_id=str(instance_id),
                    expected_container_id=container.container_id,
                    expected_runtime_state=ContainerRuntimeState.RUNNING,
                )
            self._require_stopped_container(instance_id)
        except Exception as cleanup_error:
            raise ResetReconciliationRequiredError(
                phase, "fixture failure cleanup could not establish stopped state"
            ) from cleanup_error
        raise FixtureInitializationFailedError(phase, message) from cause

    def _load_context(
        self, instance_id: UUID
    ) -> tuple[ManagedInstance, RuntimeExecutionProfile, FixtureDefinition | None]:
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            if instance is None:
                raise ManagedInstanceNotFoundError(instance_id)
            profile = RuntimeExecutionProfileRepository(session).get(
                instance.runtime_revision_id
            )
            if profile is None:
                raise ResetStateConflictError(
                    ResetPhase.PREFLIGHT,
                    "the exact pinned runtime has no execution profile",
                )
            fixture = (
                None
                if instance.fixture_definition_id is None
                else FixtureDefinitionRepository(session).get(
                    instance.fixture_definition_id
                )
            )
            if instance.fixture_definition_id is not None and fixture is None:
                raise ResetStateConflictError(
                    ResetPhase.PREFLIGHT,
                    "the assigned fixture definition is unavailable",
                )
            if (
                fixture is not None
                and fixture.compatible_kodi_release != profile.kodi_release
            ):
                raise ResetStateConflictError(
                    ResetPhase.PREFLIGHT,
                    "the assigned fixture is incompatible with the exact pin",
                )
            return instance, profile, fixture

    def _find_container(
        self, instance_id: UUID
    ) -> ManagedContainerInventoryEntry | None:
        try:
            inventory = self._broker.managed_container_inventory()
        except Exception as error:
            raise ResetStateConflictError(
                ResetPhase.PREFLIGHT,
                "managed-container ownership could not be established",
            ) from error
        matches = [
            item
            for item in inventory.containers
            if item.managed_instance_id == str(instance_id)
        ]
        if len(matches) > 1:
            raise ResetStateConflictError(
                ResetPhase.PREFLIGHT,
                "managed-container ownership is ambiguous",
            )
        return None if not matches else matches[0]

    def _require_stopped_container(
        self, instance_id: UUID
    ) -> ManagedContainerInventoryEntry:
        container = self._find_container(instance_id)
        if (
            container is None
            or container.runtime_state is not ContainerRuntimeState.STOPPED
        ):
            raise ResetStateConflictError(
                ResetPhase.FINAL_VERIFY, "managed container is not stopped"
            )
        return container

    def _require_running_container(
        self, instance_id: UUID
    ) -> ManagedContainerInventoryEntry:
        container = self._find_container(instance_id)
        if (
            container is None
            or container.runtime_state is not ContainerRuntimeState.RUNNING
        ):
            raise ResetStateConflictError(
                ResetPhase.READINESS, "managed container is not running"
            )
        return container
