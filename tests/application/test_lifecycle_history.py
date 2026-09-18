from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.lifecycle_operations import (
    ManagedInstanceLifecycleOperationCoordinator,
)
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
)
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.domain import (
    ManagedInstanceLifecycleKind,
    ManagedInstanceLifecycleOperation,
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceSemanticState,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceLifecycleOperationRepository,
    TargetRepository,
)

INSTANCE_ID = UUID("96000000-0000-4000-8000-000000000001")
REVISION_ID = UUID("96000000-0000-4000-8000-000000000002")
DEFINITION_ID = UUID("96000000-0000-4000-8000-000000000003")


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 16, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class StoppedBroker:
    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(INSTANCE_ID),
                    container_id="a" * 64,
                    image_id="sha256:" + "b" * 64,
                    runtime_state=ContainerRuntimeState.STOPPED,
                    health_state=ContainerHealthState.NOT_CONFIGURED,
                )
            ]
        )

    def observe_managed_kodi_runtime(
        self, **_kwargs: str
    ) -> ObserveManagedKodiRuntimeResult:
        raise AssertionError("stopped state must not invoke runtime observation")


class TransitionalBroker(StoppedBroker):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(INSTANCE_ID),
                    container_id="a" * 64,
                    image_id="sha256:" + "b" * 64,
                    runtime_state=ContainerRuntimeState.TRANSITIONAL,
                    health_state=ContainerHealthState.UNKNOWN,
                )
            ]
        )


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    with result.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="kodi-21-3", kodi_version="21.3"
            )
        )
        session.add(
            RuntimeRevisionRow(
                id=str(REVISION_ID),
                runtime_definition_id=str(DEFINITION_ID),
                revision_number=1,
                source_kind="test",
                source_reference="test",
                content_digest="sha256:" + "c" * 64,
                manifest_reference="test",
                manifest_digest="sha256:" + "d" * 64,
                built_at="2026-09-16T00:00:00.000000Z",
                approval_state="approved",
                approved_at="2026-09-16T00:00:00.000000Z",
                requires_trusted_build_confirmation=False,
            )
        )
        session.add(
            TargetRow(
                id=str(INSTANCE_ID),
                name="lifecycle source",
                target_type="managed",
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.flush()
        session.add(
            ManagedInstanceRow(
                target_id=str(INSTANCE_ID),
                instance_type="disposable",
                runtime_revision_id=str(REVISION_ID),
                runtime_state="stopped",
                start_on_host_boot=False,
                notes=None,
            )
        )
    yield result
    engine.dispose()


def test_begin_and_terminalize_bind_history_to_exact_mutation_lease(
    factory: sessionmaker[Session],
) -> None:
    clock = Clock()
    coordinator = ManagedInstanceLifecycleOperationCoordinator(factory, clock=clock)
    handle = coordinator.begin(
        INSTANCE_ID,
        instance_name="lifecycle source",
        kind=ManagedInstanceLifecycleKind.STOP,
        prior_state=ManagedInstanceSemanticState.RUNNING_NOT_READY,
    )
    with factory() as session:
        running = ManagedInstanceLifecycleOperationRepository(session).get(
            handle.operation_id
        )
        lease = session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID))
        assert running is not None and running.active
        assert lease is not None and lease.operation_id == str(handle.operation_id)

    clock.value += timedelta(seconds=1)
    terminal = coordinator.terminalize(
        handle,
        state=ManagedInstanceLifecycleOperationState.SUCCEEDED,
        resulting_state=ManagedInstanceSemanticState.STOPPED,
    )
    assert terminal.prior_state is ManagedInstanceSemanticState.RUNNING_NOT_READY
    assert terminal.resulting_state is ManagedInstanceSemanticState.STOPPED
    with factory() as session:
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None


@pytest.mark.parametrize(
    ("kind", "prior"),
    [
        (
            ManagedInstanceLifecycleKind.PROVISION,
            ManagedInstanceSemanticState.UNPROVISIONED,
        ),
        (ManagedInstanceLifecycleKind.START, ManagedInstanceSemanticState.STOPPED),
        (
            ManagedInstanceLifecycleKind.STOP,
            ManagedInstanceSemanticState.RUNNING_NOT_READY,
        ),
        (
            ManagedInstanceLifecycleKind.DEPROVISION,
            ManagedInstanceSemanticState.STOPPED,
        ),
    ],
)
def test_stale_dispatcher_terminalizes_without_resume_and_stable_state_does_not_block(
    factory: sessionmaker[Session],
    kind: ManagedInstanceLifecycleKind,
    prior: ManagedInstanceSemanticState,
) -> None:
    clock = Clock()
    coordinator = ManagedInstanceLifecycleOperationCoordinator(factory, clock=clock)
    handle = coordinator.begin(
        INSTANCE_ID,
        instance_name="lifecycle source",
        kind=kind,
        prior_state=prior,
    )
    clock.value += timedelta(minutes=10, seconds=1)
    broker = StoppedBroker()
    swept = StaleOperationReconciliationDispatcher(
        factory, clock=clock, broker=broker
    ).sweep_instance(INSTANCE_ID)
    assert swept.terminalized_operations == [handle.operation_id]
    assert swept.released_reservations == 1
    with factory() as session:
        operation = ManagedInstanceLifecycleOperationRepository(session).get(
            handle.operation_id
        )
        assert operation is not None
        assert (
            operation.state
            is ManagedInstanceLifecycleOperationState.RECONCILIATION_REQUIRED
        )
        assert operation.resulting_state is ManagedInstanceSemanticState.STOPPED
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None
    status = ManagedInstanceReconciliationService(factory, broker=broker).status(
        INSTANCE_ID
    )
    assert status.required
    assert not status.blocking
    assert status.observed_state == "stopped"
    assert status.blocked_actions == []


def test_stale_lifecycle_with_transitional_observation_blocks_all_mutations(
    factory: sessionmaker[Session],
) -> None:
    clock = Clock()
    handle = ManagedInstanceLifecycleOperationCoordinator(factory, clock=clock).begin(
        INSTANCE_ID,
        instance_name="lifecycle source",
        kind=ManagedInstanceLifecycleKind.START,
        prior_state=ManagedInstanceSemanticState.STOPPED,
    )
    clock.value += timedelta(minutes=10, seconds=1)
    broker = TransitionalBroker()
    StaleOperationReconciliationDispatcher(
        factory, clock=clock, broker=broker
    ).sweep_instance(INSTANCE_ID)
    status = ManagedInstanceReconciliationService(factory, broker=broker).status(
        INSTANCE_ID
    )
    assert status.operation_uuid == handle.operation_id
    assert status.required and status.blocking
    assert status.observed_state == "transitional"
    assert {item.value for item in status.blocked_actions} == {
        "reset",
        "snapshot_create",
        "snapshot_restore",
        "instance_delete",
        "provision",
        "start",
        "stop",
        "deprovision",
        "fixture_assignment",
        "runtime_reassignment",
        "development_source_attachment",
        "development_source_deployment",
        "development_source_enablement",
        "development_source_smoke_test",
        "addon_zip_installation",
        "addon_zip_enablement",
        "addon_zip_smoke_test",
        "addon_repository_attachment",
        "addon_repository_installation",
        "repository_replacement",
    }


def test_retention_keeps_latest_twenty_ordinary_and_every_unresolved_row(
    factory: sessionmaker[Session],
) -> None:
    start = datetime(2026, 9, 16, tzinfo=UTC)
    with factory.begin() as session:
        repository = ManagedInstanceLifecycleOperationRepository(session)
        for index in range(22):
            at = start + timedelta(seconds=index)
            repository.create(
                ManagedInstanceLifecycleOperation(
                    id=uuid4(),
                    original_managed_instance_id=INSTANCE_ID,
                    managed_instance_id=INSTANCE_ID,
                    instance_name="lifecycle source",
                    kind=ManagedInstanceLifecycleKind.START,
                    requested_at=at,
                    started_at=at,
                    prior_state=ManagedInstanceSemanticState.STOPPED,
                    state=ManagedInstanceLifecycleOperationState.SUCCEEDED,
                    resulting_state=ManagedInstanceSemanticState.RUNNING_NOT_READY,
                    completed_at=at,
                )
            )
        unresolved_at = start + timedelta(minutes=2)
        repository.create(
            ManagedInstanceLifecycleOperation(
                id=uuid4(),
                original_managed_instance_id=INSTANCE_ID,
                managed_instance_id=INSTANCE_ID,
                instance_name="lifecycle source",
                kind=ManagedInstanceLifecycleKind.STOP,
                requested_at=unresolved_at,
                started_at=unresolved_at,
                prior_state=ManagedInstanceSemanticState.RUNNING_NOT_READY,
                state=(ManagedInstanceLifecycleOperationState.RECONCILIATION_REQUIRED),
                resulting_state=ManagedInstanceSemanticState.AMBIGUOUS,
                completed_at=unresolved_at,
                failure_reason="Reconciliation is required.",
                reconciliation_required=True,
            )
        )
        running_id = uuid4()
        repository.create(
            ManagedInstanceLifecycleOperation(
                id=running_id,
                original_managed_instance_id=INSTANCE_ID,
                managed_instance_id=INSTANCE_ID,
                instance_name="lifecycle source",
                kind=ManagedInstanceLifecycleKind.PROVISION,
                requested_at=unresolved_at + timedelta(seconds=1),
                started_at=unresolved_at + timedelta(seconds=1),
                prior_state=ManagedInstanceSemanticState.UNPROVISIONED,
            )
        )
        assert repository.prune_terminal(INSTANCE_ID) == 2

    with factory() as session:
        records = ManagedInstanceLifecycleOperationRepository(
            session
        ).list_for_original_instance(INSTANCE_ID)
    assert (
        sum(
            item.state is ManagedInstanceLifecycleOperationState.SUCCEEDED
            for item in records
        )
        == 20
    )
    assert any(item.reconciliation_required for item in records)
    assert any(item.id == running_id and item.active for item in records)


def test_history_survives_managed_instance_deletion_with_live_fk_cleared(
    factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 9, 16, tzinfo=UTC)
    operation_id = uuid4()
    with factory.begin() as session:
        ManagedInstanceLifecycleOperationRepository(session).create(
            ManagedInstanceLifecycleOperation(
                id=operation_id,
                original_managed_instance_id=INSTANCE_ID,
                managed_instance_id=INSTANCE_ID,
                instance_name="lifecycle source",
                kind=ManagedInstanceLifecycleKind.DEPROVISION,
                requested_at=now,
                started_at=now,
                prior_state=ManagedInstanceSemanticState.STOPPED,
                state=ManagedInstanceLifecycleOperationState.SUCCEEDED,
                resulting_state=ManagedInstanceSemanticState.UNPROVISIONED,
                completed_at=now,
            )
        )
        assert TargetRepository(session).delete(INSTANCE_ID)
    with factory() as session:
        retained = ManagedInstanceLifecycleOperationRepository(session).get(
            operation_id
        )
        assert retained is not None
        assert retained.managed_instance_id is None
        assert retained.original_managed_instance_id == INSTANCE_ID
