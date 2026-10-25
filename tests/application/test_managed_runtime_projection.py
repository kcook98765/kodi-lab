"""Deterministic ordering tests for the mutable managed runtime projection."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.readiness_tasks import (
    ReadinessTaskSnapshot,
    ReadinessTaskState,
)
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.domain import (
    ConnectionState,
    HealthStatus,
    InstanceRuntimeState,
    ManagedInstanceMutationKind,
    TargetAvailability,
)
from kodi_lab.kodi.readiness import JsonRpcState, ReadinessResult, UsabilityState
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceMutationLeaseRepository,
    ManagedRuntimeCurrentProjection,
    ManagedRuntimeObservationToken,
    ManagedRuntimeProjectionRepository,
    ManagedRuntimeProjectionResult,
)
from tests.efficiency import CurrentSchemaTemplate

INSTANCE = UUID("97000000-0000-4000-8000-000000000001")
REVISION = UUID("97000000-0000-4000-8000-000000000002")
DEFINITION = UUID("97000000-0000-4000-8000-000000000003")
EXTERNAL = UUID("97000000-0000-4000-8000-000000000004")
OTHER = UUID("97000000-0000-4000-8000-000000000005")


@pytest.fixture
def factory(
    tmp_path: Path, current_schema: CurrentSchemaTemplate
) -> Iterator[sessionmaker[Session]]:
    database_path = tmp_path / "projection.db"
    current_schema.clone(database_path)
    engine = create_database_engine(f"sqlite:///{database_path}")
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION), slug="kodi-21-3", kodi_version="21.3"
            )
        )
        session.add(
            RuntimeRevisionRow(
                id=str(REVISION),
                runtime_definition_id=str(DEFINITION),
                revision_number=1,
                source_kind="test",
                source_reference="test",
                content_digest="sha256:" + "a" * 64,
                manifest_reference="test",
                manifest_digest="sha256:" + "b" * 64,
                built_at="2026-10-02T00:00:00Z",
                approval_state="approved",
                approved_at="2026-10-02T00:00:00Z",
                requires_trusted_build_confirmation=False,
            )
        )
        session.add(
            TargetRow(
                id=str(INSTANCE),
                name="projection source",
                target_type="managed",
                kodi_version="21.3",
                availability="unavailable",
                health="warning",
                connection_state="disconnected",
            )
        )
        session.flush()
        session.add(
            ManagedInstanceRow(
                target_id=str(INSTANCE),
                instance_type="disposable",
                runtime_revision_id=str(REVISION),
                runtime_state="stopped",
                start_on_host_boot=False,
            )
        )
    yield sessions
    engine.dispose()


class Ready:
    def snapshot(self, _instance: UUID) -> ReadinessTaskSnapshot:
        return ReadinessTaskSnapshot(
            1,
            ReadinessTaskState.USABLE,
            ReadinessResult(
                container_state=ContainerRuntimeState.RUNNING,
                process_state=KodiProcessState.RUNNING,
                jsonrpc_state=JsonRpcState.RESPONSIVE,
                usability_state=UsabilityState.USABLE,
                failure_reason=None,
                last_observed_failure=None,
                identity=None,
                elapsed_seconds=0,
                attempts=1,
            ),
        )


class Idle:
    def snapshot(self, _instance: UUID) -> ReadinessTaskSnapshot:
        return ReadinessTaskSnapshot(0, ReadinessTaskState.IDLE)


class RunningBroker:
    def __init__(self) -> None:
        self.entered = Event()
        self.resume = Event()
        self.resume.set()

    def managed_container_inventory(
        self, *, managed_instance_id: str | None = None
    ) -> ManagedContainerInventoryResult:
        assert managed_instance_id in {None, str(INSTANCE)}
        self.entered.set()
        assert self.resume.wait(5)
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(INSTANCE),
                    container_id="c" * 64,
                    image_id="sha256:" + "d" * 64,
                    runtime_revision_id=str(REVISION),
                    runtime_state=ContainerRuntimeState.RUNNING,
                    health_state=ContainerHealthState.HEALTHY,
                )
            ]
        )

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        return ObserveManagedKodiRuntimeResult(
            managed_instance_id=managed_instance_id,
            runtime_revision_id=str(REVISION),
            container_id=expected_container_id,
            runtime_state=ContainerRuntimeState.RUNNING,
            process_state=KodiProcessState.RUNNING,
            ipv4_address="10.250.0.2",
            jsonrpc_port=8080,
        )


class StoppedBroker(RunningBroker):
    def __init__(self, runtime_revision_id: str) -> None:
        super().__init__()
        self.runtime_revision_id = runtime_revision_id

    def managed_container_inventory(
        self, *, managed_instance_id: str | None = None
    ) -> ManagedContainerInventoryResult:
        inventory = super().managed_container_inventory(
            managed_instance_id=managed_instance_id
        )
        return ManagedContainerInventoryResult(
            containers=[
                inventory.containers[0].model_copy(
                    update={
                        "runtime_state": ContainerRuntimeState.STOPPED,
                        "runtime_revision_id": self.runtime_revision_id,
                    }
                )
            ]
        )

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        raise AssertionError("stopped status must not observe a running Kodi runtime")


def _current(factory: sessionmaker[Session]) -> tuple[int, str, str, str, str]:
    with factory() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE))
        target = session.get(TargetRow, str(INSTANCE))
        assert instance is not None and target is not None
        return (
            instance.runtime_state_revision,
            instance.runtime_state.value,
            target.availability.value,
            target.health.value,
            target.connection_state.value,
        )


def _seed_other(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        session.add(
            TargetRow(
                id=str(OTHER),
                name="protected persistent instance",
                target_type="managed",
                kodi_version="21.3",
                availability="unavailable",
                health="error",
                connection_state="disconnected",
            )
        )
        session.flush()
        session.add(
            ManagedInstanceRow(
                target_id=str(OTHER),
                instance_type="persistent",
                runtime_revision_id=str(REVISION),
                runtime_state="stopped",
                start_on_host_boot=False,
            )
        )


def _other_current(factory: sessionmaker[Session]) -> tuple[int, str, str, str, str]:
    with factory() as session:
        instance = session.get(ManagedInstanceRow, str(OTHER))
        target = session.get(TargetRow, str(OTHER))
        assert instance is not None and target is not None
        return (
            instance.runtime_state_revision,
            instance.runtime_state.value,
            target.availability.value,
            target.health.value,
            target.connection_state.value,
        )


class TwoInstanceBroker(RunningBroker):
    def __init__(self) -> None:
        super().__init__()
        self.observed: list[str] = []

    def managed_container_inventory(
        self, *, managed_instance_id: str | None = None
    ) -> ManagedContainerInventoryResult:
        own = super().managed_container_inventory(
            managed_instance_id=managed_instance_id
        )
        if managed_instance_id is not None:
            return own
        return own.model_copy(
            update={
                "containers": [
                    *own.containers,
                    ManagedContainerInventoryEntry(
                        managed_instance_id=str(OTHER),
                        container_id="e" * 64,
                        image_id="sha256:" + "f" * 64,
                        runtime_revision_id=str(REVISION),
                        runtime_state=ContainerRuntimeState.RUNNING,
                        health_state=ContainerHealthState.HEALTHY,
                    ),
                ]
            }
        )

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        self.observed.append(managed_instance_id)
        return super().observe_managed_kodi_runtime(
            managed_instance_id=managed_instance_id,
            expected_container_id=expected_container_id,
        )


class TrackingReady(Ready):
    def __init__(self) -> None:
        self.snapshots: list[UUID] = []

    def snapshot(self, instance: UUID) -> ReadinessTaskSnapshot:
        self.snapshots.append(instance)
        return super().snapshot(instance)


def test_get_isolates_requested_instance_from_persistent_neighbor(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_other(factory)
    broker = TwoInstanceBroker()
    ready = TrackingReady()
    tokens = ManagedRuntimeProjectionRepository.observation_token
    compare = ManagedRuntimeProjectionRepository.compare_and_set
    token_ids: list[UUID] = []
    compare_ids: list[UUID] = []

    def tracked_token(
        repository: ManagedRuntimeProjectionRepository, instance_id: UUID
    ) -> ManagedRuntimeObservationToken | None:
        token_ids.append(instance_id)
        assert instance_id == INSTANCE
        return tokens(repository, instance_id)

    def tracked_compare(
        repository: ManagedRuntimeProjectionRepository,
        token: ManagedRuntimeObservationToken,
        projection: ManagedRuntimeCurrentProjection,
    ) -> ManagedRuntimeProjectionResult:
        compare_ids.append(token.instance_id)
        assert compare_ids == [INSTANCE]
        return compare(repository, token, projection)

    monkeypatch.setattr(
        ManagedRuntimeProjectionRepository, "observation_token", tracked_token
    )
    monkeypatch.setattr(
        ManagedRuntimeProjectionRepository, "compare_and_set", tracked_compare
    )
    before = _other_current(factory)
    status = ManagedRuntimeStatusService(factory, broker, ready).get(INSTANCE)  # type: ignore[arg-type]
    assert status.managed_instance_id == INSTANCE
    assert status.usability_state.value == "usable"
    assert broker.observed == [str(INSTANCE)]
    assert ready.snapshots == [INSTANCE]
    assert token_ids and set(token_ids) == {INSTANCE}
    assert compare_ids == [INSTANCE]
    assert _current(factory) == (0, "running", "available", "healthy", "connected")
    assert _other_current(factory) == before
    with factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ManagedInstanceLifecycleOperationRow)
            )
            == 0
        )


def test_explicit_list_statuses_observes_collection(
    factory: sessionmaker[Session],
) -> None:
    _seed_other(factory)
    broker = TwoInstanceBroker()
    ready = TrackingReady()
    statuses = ManagedRuntimeStatusService(factory, broker, ready).list_statuses()  # type: ignore[arg-type]
    assert [item.managed_instance_id for item in statuses] == [INSTANCE, OTHER]
    assert broker.observed == [str(INSTANCE), str(OTHER)]
    assert ready.snapshots == [INSTANCE, OTHER]
    assert _current(factory) == (0, "running", "available", "healthy", "connected")
    assert _other_current(factory) == (
        0,
        "running",
        "available",
        "healthy",
        "connected",
    )


@pytest.mark.parametrize(
    "kind",
    [
        ManagedInstanceMutationKind.LIFECYCLE,
        ManagedInstanceMutationKind.RESET,
        ManagedInstanceMutationKind.SNAPSHOT_CREATE,
        ManagedInstanceMutationKind.SNAPSHOT_RESTORE,
        ManagedInstanceMutationKind.INSTANCE_DELETE,
        ManagedInstanceMutationKind.RUNTIME_REASSIGNMENT,
        ManagedInstanceMutationKind.ADDON_ZIP_INSTALLATION,
        ManagedInstanceMutationKind.ADDON_REPOSITORY_INSTALLATION,
        ManagedInstanceMutationKind.REPOSITORY_REPLACEMENT,
        ManagedInstanceMutationKind.PROMOTION_VERIFICATION,
        ManagedInstanceMutationKind.MCP_WORKSPACE_DEPLOYMENT,
    ],
)
def test_delayed_running_status_cannot_overwrite_mutation(
    factory: sessionmaker[Session], kind: ManagedInstanceMutationKind
) -> None:
    broker = RunningBroker()
    broker.resume.clear()
    status = ManagedRuntimeStatusService(factory, broker, Ready())  # type: ignore[arg-type]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(status.get, INSTANCE)
        assert broker.entered.wait(5)
        operation = uuid4()
        with factory.begin() as session:
            ManagedInstanceMutationLeaseRepository(session).reserve(
                INSTANCE, kind=kind, operation_id=operation
            )
        broker.resume.set()
        assert future.result(timeout=5).container_state is ContainerRuntimeState.RUNNING
    assert _current(factory) == (1, "stopped", "unavailable", "warning", "disconnected")
    with factory.begin() as session:
        assert ManagedInstanceMutationLeaseRepository(session).release_for_operation(
            INSTANCE, operation
        )
    assert _current(factory)[0] == 2
    status.get(INSTANCE)
    assert _current(factory) == (2, "running", "available", "healthy", "connected")


def test_observation_during_mutation_stays_stale_after_finalization(
    factory: sessionmaker[Session],
) -> None:
    operation = uuid4()
    with factory.begin() as session:
        leases = ManagedInstanceMutationLeaseRepository(session)
        leases.reserve(
            INSTANCE, kind=ManagedInstanceMutationKind.RESET, operation_id=operation
        )
    with factory() as session:
        token = ManagedRuntimeProjectionRepository(session).observation_token(INSTANCE)
    assert token is not None and token.runtime_state_revision == 1
    running = ManagedRuntimeCurrentProjection(
        InstanceRuntimeState.RUNNING,
        TargetAvailability.AVAILABLE,
        HealthStatus.HEALTHY,
        ConnectionState.CONNECTED,
    )
    with factory.begin() as session:
        assert (
            ManagedRuntimeProjectionRepository(session).compare_and_set(token, running)
            is ManagedRuntimeProjectionResult.STALE
        )
    with factory.begin() as session:
        assert ManagedInstanceMutationLeaseRepository(session).release_for_operation(
            INSTANCE, operation
        )
    with factory.begin() as session:
        assert (
            ManagedRuntimeProjectionRepository(session).compare_and_set(token, running)
            is ManagedRuntimeProjectionResult.STALE
        )
    assert _current(factory) == (2, "stopped", "unavailable", "warning", "disconnected")


def test_restart_and_two_fresh_observations_converge_without_history(
    factory: sessionmaker[Session],
) -> None:
    broker = RunningBroker()
    service = ManagedRuntimeStatusService(factory, broker, Ready())  # type: ignore[arg-type]
    service.get(INSTANCE)
    assert _current(factory) == (0, "running", "available", "healthy", "connected")
    ManagedRuntimeStatusService(factory, broker, Ready()).get(INSTANCE)  # type: ignore[arg-type]
    assert _current(factory)[0] == 0
    with factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ManagedInstanceLifecycleOperationRow)
            )
            == 0
        )


def test_concurrent_status_observations_without_mutation_converge(
    factory: sessionmaker[Session],
) -> None:
    class PairedBroker(RunningBroker):
        def __init__(self) -> None:
            super().__init__()
            self.barrier = Barrier(2)

        def managed_container_inventory(
            self, *, managed_instance_id: str | None = None
        ) -> ManagedContainerInventoryResult:
            self.barrier.wait(timeout=5)
            return super().managed_container_inventory(
                managed_instance_id=managed_instance_id
            )

    broker = PairedBroker()
    service = ManagedRuntimeStatusService(factory, broker, Ready())  # type: ignore[arg-type]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.get, INSTANCE) for _ in range(2)]
        for future in futures:
            assert future.result(timeout=5).managed_instance_id == INSTANCE
    assert _current(factory) == (0, "running", "available", "healthy", "connected")


def test_external_target_has_no_managed_observation_token(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        session.add(
            TargetRow(
                id=str(EXTERNAL),
                name="external",
                target_type="external_development",
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
    with factory() as session:
        assert (
            ManagedRuntimeProjectionRepository(session).observation_token(EXTERNAL)
            is None
        )
        external = session.get(TargetRow, str(EXTERNAL))
        assert external is not None
        assert external.availability is TargetAvailability.AVAILABLE


@pytest.mark.parametrize(
    "kind",
    [
        ManagedInstanceMutationKind.FIXTURE_ASSIGNMENT,
        ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_DEPLOYMENT,
        ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ATTACHMENT,
        ManagedInstanceMutationKind.AGENT_WORKSPACE_PROMOTION,
        ManagedInstanceMutationKind.MCP_WORKSPACE_EXECUTION,
    ],
)
def test_non_runtime_mutations_do_not_advance_revision(
    factory: sessionmaker[Session], kind: ManagedInstanceMutationKind
) -> None:
    operation = uuid4()
    with factory.begin() as session:
        ManagedInstanceMutationLeaseRepository(session).reserve(
            INSTANCE, kind=kind, operation_id=operation
        )
    assert _current(factory)[0] == 0
    with factory.begin() as session:
        assert ManagedInstanceMutationLeaseRepository(session).release_for_operation(
            INSTANCE, operation
        )
    assert _current(factory)[0] == 0


def test_runtime_pin_change_invalidates_prior_observation(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        old = ManagedRuntimeProjectionRepository(session).observation_token(INSTANCE)
    assert old is not None
    operation = uuid4()
    new_revision = uuid4()
    with factory.begin() as session:
        ManagedInstanceMutationLeaseRepository(session).reserve(
            INSTANCE,
            kind=ManagedInstanceMutationKind.RUNTIME_REASSIGNMENT,
            operation_id=operation,
        )
        row = session.get(ManagedInstanceRow, str(INSTANCE))
        assert row is not None
        session.add(
            RuntimeRevisionRow(
                id=str(new_revision),
                runtime_definition_id=str(DEFINITION),
                revision_number=2,
                source_kind="test",
                source_reference="test-next",
                content_digest="sha256:" + "e" * 64,
                manifest_reference="test-next",
                manifest_digest="sha256:" + "f" * 64,
                built_at="2026-10-02T00:00:00Z",
                approval_state="approved",
                approved_at="2026-10-02T00:00:00Z",
                requires_trusted_build_confirmation=False,
            )
        )
        session.flush()
        row.runtime_revision_id = str(new_revision)
    with factory.begin() as session:
        assert ManagedInstanceMutationLeaseRepository(session).release_for_operation(
            INSTANCE, operation
        )
    with factory.begin() as session:
        result = ManagedRuntimeProjectionRepository(session).compare_and_set(
            old,
            ManagedRuntimeCurrentProjection(
                InstanceRuntimeState.RUNNING,
                TargetAvailability.AVAILABLE,
                HealthStatus.HEALTHY,
                ConnectionState.CONNECTED,
            ),
        )
    assert result is ManagedRuntimeProjectionResult.STALE
    assert _current(factory) == (2, "stopped", "unavailable", "warning", "disconnected")


def test_running_container_without_jsonrpc_readiness_is_not_available(
    factory: sessionmaker[Session],
) -> None:
    status = ManagedRuntimeStatusService(
        factory,
        RunningBroker(),
        Idle(),  # type: ignore[arg-type]
    ).get(INSTANCE)
    assert status.container_state is ContainerRuntimeState.RUNNING
    assert _current(factory) == (0, "running", "unavailable", "warning", "disconnected")


def test_runtime_pin_mismatch_cannot_project_usable(
    factory: sessionmaker[Session],
) -> None:
    class WrongPinBroker(RunningBroker):
        def observe_managed_kodi_runtime(
            self, *, managed_instance_id: str, expected_container_id: str
        ) -> ObserveManagedKodiRuntimeResult:
            return (
                super()
                .observe_managed_kodi_runtime(
                    managed_instance_id=managed_instance_id,
                    expected_container_id=expected_container_id,
                )
                .model_copy(update={"runtime_revision_id": str(uuid4())})
            )

    status = ManagedRuntimeStatusService(
        factory,
        WrongPinBroker(),
        Ready(),  # type: ignore[arg-type]
    ).get(INSTANCE)
    assert status.failure_code == "runtime_revision_mismatch"
    assert _current(factory) == (0, "running", "unavailable", "warning", "disconnected")


def test_stopped_matching_pin_keeps_normal_actions(
    factory: sessionmaker[Session],
) -> None:
    status = ManagedRuntimeStatusService(
        factory,
        StoppedBroker(str(REVISION)),
        Idle(),  # type: ignore[arg-type]
    ).get(INSTANCE)
    assert status.container_state is ContainerRuntimeState.STOPPED
    assert status.failure_code is None
    assert status.allowed_actions == ["start", "deprovision"]


def test_stopped_mismatched_pin_uses_existing_failure_without_actions(
    factory: sessionmaker[Session],
) -> None:
    status = ManagedRuntimeStatusService(
        factory,
        StoppedBroker(str(uuid4())),
        Idle(),  # type: ignore[arg-type]
    ).get(INSTANCE)
    assert status.container_state is ContainerRuntimeState.STOPPED
    assert status.failure_code == "runtime_revision_mismatch"
    assert status.allowed_actions == []
    assert status.usability_state.value == "failed"
    assert _current(factory) == (0, "stopped", "unavailable", "warning", "disconnected")


def test_targeted_stopped_mismatch_does_not_project_neighbor(
    factory: sessionmaker[Session],
) -> None:
    _seed_other(factory)
    before = _other_current(factory)
    status = ManagedRuntimeStatusService(
        factory,
        StoppedBroker(str(uuid4())),
        Idle(),  # type: ignore[arg-type]
    ).get(INSTANCE)
    assert status.failure_code == "runtime_revision_mismatch"
    assert _other_current(factory) == before
