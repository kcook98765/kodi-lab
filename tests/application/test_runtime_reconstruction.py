"""Bounded reconstruction behavior against isolated SQL and broker state."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.application.reset import (
    DisposableInstanceResetService,
    FixtureInitializationFailedError,
    ResetPhase,
)
from kodi_lab.application.reset_activity import ResetActivityState
from kodi_lab.application.runtime_reconstruction import (
    ManagedRuntimeReconstructionCoordinator,
    ManagedRuntimeReconstructionExecutor,
    ReconstructionConflictError,
    _ReconstructionFixtureActivity,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
)
from kodi_lab.broker.errors import ClientTransportError, ProvisioningStateError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ProvisionManagedContainerResult,
    ReconstructionDestinationResult,
    RemoveMismatchedManagedContainerResult,
)
from kodi_lab.broker.provisioning import (
    ManagedContainerProvisioningSpec,
    SqliteReconstructionAuthorizationResolver,
)
from kodi_lab.domain import ManagedInstanceMutationKind, PythonBytecodePolicy
from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedRuntimeReconstructionRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import FixtureDefinitionRepository

INSTANCE = UUID("a0000000-0000-4000-8000-000000000001")
OTHER = UUID("a0000000-0000-4000-8000-000000000002")
DESIRED = UUID("a0000000-0000-4000-8000-000000000003")
OLD = UUID("a0000000-0000-4000-8000-000000000004")
DEFINITION = UUID("a0000000-0000-4000-8000-000000000005")
OLD_ID = "1" * 64
FRESH_ID = "2" * 64
OLD_IMAGE = "sha256:" + "3" * 64
NEW_IMAGE = "sha256:" + "4" * 64


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'reconstruction.db'}")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION), slug="kodi-20-5", kodi_version="20.5"
            )
        )
        session.flush()
        session.add(
            RuntimeRevisionRow(
                id=str(DESIRED),
                runtime_definition_id=str(DEFINITION),
                revision_number=2,
                source_kind="test",
                source_reference="test",
                content_digest="sha256:" + "a" * 64,
                manifest_reference="test",
                manifest_digest="sha256:" + "b" * 64,
                built_at="2026-10-04T00:00:00Z",
                approval_state="approved",
                approved_at="2026-10-04T00:00:00Z",
                requires_trusted_build_confirmation=False,
            )
        )
        session.flush()
        for target in (INSTANCE, OTHER):
            session.add(
                TargetRow(
                    id=str(target),
                    name=str(target),
                    target_type="managed",
                    kodi_version="20.5",
                    availability="unavailable",
                    health="warning",
                    connection_state="disconnected",
                )
            )
            session.flush()
            session.add(
                ManagedInstanceRow(
                    target_id=str(target),
                    instance_type="disposable",
                    runtime_revision_id=str(DESIRED),
                    runtime_state="stopped",
                    start_on_host_boot=False,
                )
            )
    yield sessions
    engine.dispose()


def entry(
    *,
    instance: UUID = INSTANCE,
    container_id: str = OLD_ID,
    revision: UUID = OLD,
    image: str = OLD_IMAGE,
    state: ContainerRuntimeState = ContainerRuntimeState.STOPPED,
) -> ManagedContainerInventoryEntry:
    return ManagedContainerInventoryEntry(
        managed_instance_id=str(instance),
        container_id=container_id,
        image_id=image,
        runtime_revision_id=str(revision),
        runtime_state=state,
        health_state=ContainerHealthState.NOT_CONFIGURED,
    )


class Broker:
    def __init__(self) -> None:
        self.containers = [entry(), entry(instance=OTHER)]
        self.removals = 0
        self.provisions = 0
        self.destination_available = True
        self.destination_image = NEW_IMAGE
        self.provision_fails = False
        self.remove_uncertain = False

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(containers=list(self.containers))

    def reconstruction_destination(
        self, *, managed_instance_id: str
    ) -> ReconstructionDestinationResult:
        if not self.destination_available:
            raise RuntimeError("image missing")
        return ReconstructionDestinationResult(
            managed_instance_id=managed_instance_id,
            desired_runtime_revision_id=str(DESIRED),
            desired_image_id=self.destination_image,
        )

    def remove_mismatched_managed_container(
        self, **kwargs: object
    ) -> RemoveMismatchedManagedContainerResult:
        self.removals += 1
        if self.remove_uncertain:
            raise ClientTransportError("timed out")
        assert kwargs["old_container_id"] == OLD_ID
        assert kwargs["old_runtime_revision_id"] == str(OLD)
        self.containers = [
            item
            for item in self.containers
            if item.managed_instance_id != str(INSTANCE)
        ]
        return RemoveMismatchedManagedContainerResult(
            managed_instance_id=str(INSTANCE),
            removed_container_id=OLD_ID,
            old_runtime_revision_id=str(OLD),
            removed=True,
        )

    def provision_managed_container(
        self, *, managed_instance_id: str
    ) -> ProvisionManagedContainerResult:
        self.provisions += 1
        if self.provision_fails:
            raise RuntimeError("definitive create failure")
        self.containers.append(
            entry(container_id=FRESH_ID, revision=DESIRED, image=NEW_IMAGE)
        )
        return ProvisionManagedContainerResult(
            managed_instance_id=managed_instance_id,
            runtime_revision_id=str(DESIRED),
            container_id=FRESH_ID,
            image_id=NEW_IMAGE,
            runtime_state=ContainerRuntimeState.STOPPED,
            created=True,
        )


def test_exact_mismatch_reconstructs_same_instance_and_pin(
    factory: sessionmaker[Session],
) -> None:
    broker = Broker()
    coordinator = ManagedRuntimeReconstructionCoordinator(factory, broker)
    eligibility = coordinator.eligibility(INSTANCE)
    assert eligibility.eligible and eligibility.observation_token
    accepted = coordinator.queue(INSTANCE, eligibility.observation_token)
    executor = ManagedRuntimeReconstructionExecutor(coordinator, factory, broker)
    executor._execute(accepted.operation.operation_id)
    result = coordinator.get(accepted.operation.operation_id)
    assert result is not None
    assert result.state == "succeeded"
    assert result.old_discarded
    assert result.fresh_container_id == FRESH_ID
    assert broker.removals == broker.provisions == 1
    assert broker.containers == [
        entry(instance=OTHER),
        entry(container_id=FRESH_ID, revision=DESIRED, image=NEW_IMAGE),
    ]
    with factory() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE))
        assert instance is not None
        assert instance.runtime_revision_id == str(DESIRED)
    executor.close()


@pytest.mark.parametrize(
    "replacement",
    [
        entry(revision=DESIRED),
        entry(state=ContainerRuntimeState.RUNNING),
        entry(container_id="9" * 64),
        entry(revision=DESIRED, container_id="9" * 64),
    ],
)
def test_ineligible_or_stale_confirmation_never_removes(
    factory: sessionmaker[Session],
    replacement: ManagedContainerInventoryEntry,
) -> None:
    broker = Broker()
    coordinator = ManagedRuntimeReconstructionCoordinator(factory, broker)
    original = coordinator.eligibility(INSTANCE)
    assert original.observation_token
    broker.containers[0] = replacement
    with pytest.raises(ReconstructionConflictError):
        coordinator.queue(INSTANCE, original.observation_token)
    assert broker.removals == 0


def test_container_change_after_queue_fails_before_destructive_dispatch(
    factory: sessionmaker[Session],
) -> None:
    broker = Broker()
    coordinator = ManagedRuntimeReconstructionCoordinator(factory, broker)
    token = coordinator.eligibility(INSTANCE).observation_token
    assert token
    operation_id = coordinator.queue(INSTANCE, token).operation.operation_id
    broker.containers[0] = entry(container_id="9" * 64)
    executor = ManagedRuntimeReconstructionExecutor(coordinator, factory, broker)
    executor._execute(operation_id)
    row = coordinator.get(operation_id)
    assert row is not None and row.state == "failed"
    assert not row.old_discarded
    assert broker.removals == broker.provisions == 0
    executor.close()


def test_missing_destination_and_stale_cas_leave_old_container(
    factory: sessionmaker[Session],
) -> None:
    broker = Broker()
    coordinator = ManagedRuntimeReconstructionCoordinator(factory, broker)
    broker.destination_available = False
    assert not coordinator.eligibility(INSTANCE).eligible
    broker.destination_available = True
    token = coordinator.eligibility(INSTANCE).observation_token
    assert token
    with factory.begin() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE))
        assert instance is not None
        instance.runtime_state_revision += 1
    with pytest.raises(ReconstructionConflictError):
        coordinator.queue(INSTANCE, token)
    assert broker.removals == 0


def test_missing_or_invalid_old_revision_is_not_eligible(
    factory: sessionmaker[Session],
) -> None:
    broker = Broker()
    broker.containers[0] = entry().model_copy(update={"runtime_revision_id": None})
    coordinator = ManagedRuntimeReconstructionCoordinator(factory, broker)
    assert not coordinator.eligibility(INSTANCE).eligible
    assert broker.removals == 0


def test_conflicting_instance_lease_blocks_reconstruction_without_mutation(
    factory: sessionmaker[Session],
) -> None:
    broker = Broker()
    with factory.begin() as session:
        session.add(
            ManagedInstanceMutationLeaseRow(
                managed_instance_id=str(INSTANCE),
                operation_kind=ManagedInstanceMutationKind.RESET,
                operation_id=str(uuid4()),
            )
        )
    coordinator = ManagedRuntimeReconstructionCoordinator(factory, broker)
    assert not coordinator.eligibility(INSTANCE).eligible
    assert broker.removals == broker.provisions == 0


def test_changed_desired_image_invalidates_confirmation_and_broker_authorization(
    factory: sessionmaker[Session],
) -> None:
    broker = Broker()
    coordinator = ManagedRuntimeReconstructionCoordinator(factory, broker)
    token = coordinator.eligibility(INSTANCE).observation_token
    assert token
    broker.destination_image = "sha256:" + "5" * 64
    with pytest.raises(ReconstructionConflictError):
        coordinator.queue(INSTANCE, token)
    broker.destination_image = NEW_IMAGE
    operation_id = coordinator.queue(INSTANCE, token).operation.operation_id
    digest = coordinator.acquire(operation_id)
    assert digest
    coordinator.phase(operation_id, digest, "removing_old")
    row = coordinator.get(operation_id)
    assert row is not None and row.desired_image_id == NEW_IMAGE
    resolver = SqliteReconstructionAuthorizationResolver(factory)
    resolver._provisioning.resolve = lambda _instance: ManagedContainerProvisioningSpec(
        managed_instance_id=str(INSTANCE),
        runtime_revision_id=str(DESIRED),
        image_id="sha256:" + "5" * 64,
        container_name=f"kodi-lab-managed-{INSTANCE.hex}",
        runtime_uid=1000,
        runtime_gid=1000,
        python_bytecode_policy=PythonBytecodePolicy.INTERPRETER_DEFAULT,
    )
    with pytest.raises(ProvisioningStateError):
        resolver.authorize_removal(
            managed_instance_id=str(INSTANCE),
            operation_id=str(operation_id),
            lease_token_digest=digest,
            owned_runtime_state_revision=row.owned_runtime_state_revision,
            old_container_id=OLD_ID,
            old_image_id=OLD_IMAGE,
            old_runtime_revision_id=str(OLD),
            desired_runtime_revision_id=str(DESIRED),
        )
    assert broker.removals == 0


def test_proven_removal_with_provision_failure_is_coherent_unprovisioned(
    factory: sessionmaker[Session],
) -> None:
    broker = Broker()
    broker.provision_fails = True
    coordinator = ManagedRuntimeReconstructionCoordinator(factory, broker)
    token = coordinator.eligibility(INSTANCE).observation_token
    assert token
    accepted = coordinator.queue(INSTANCE, token)
    executor = ManagedRuntimeReconstructionExecutor(coordinator, factory, broker)
    executor._execute(accepted.operation.operation_id)
    row = coordinator.get(accepted.operation.operation_id)
    assert row is not None and row.state == "failed" and row.old_discarded
    assert coordinator.reconciliation_state(row) == "unprovisioned"
    assert broker.containers == [entry(instance=OTHER)]
    executor.close()


def test_uncertain_broker_result_blocks_retry_until_proven_resolution(
    factory: sessionmaker[Session],
) -> None:
    broker = Broker()
    broker.remove_uncertain = True
    coordinator = ManagedRuntimeReconstructionCoordinator(factory, broker)
    token = coordinator.eligibility(INSTANCE).observation_token
    assert token
    accepted = coordinator.queue(INSTANCE, token)
    executor = ManagedRuntimeReconstructionExecutor(coordinator, factory, broker)
    executor._execute(accepted.operation.operation_id)
    row = coordinator.get(accepted.operation.operation_id)
    assert row is not None and row.state == "reconciliation_required"
    assert coordinator.reconciliation_state(row) == "exact_old_container"
    assert not coordinator.eligibility(INSTANCE).eligible
    with pytest.raises(ReconstructionConflictError):
        coordinator.resolve_interrupted(
            row_id := accepted.operation.operation_id, "unprovisioned"
        )
    resolved = coordinator.resolve_interrupted(row_id, "exact_old_container")
    assert resolved.state == "resolved"
    assert coordinator.eligibility(INSTANCE).eligible
    executor.close()


def test_broker_authorization_binds_exact_live_operation_and_cas(
    factory: sessionmaker[Session],
) -> None:
    broker = Broker()
    coordinator = ManagedRuntimeReconstructionCoordinator(factory, broker)
    token = coordinator.eligibility(INSTANCE).observation_token
    assert token
    operation_id = coordinator.queue(INSTANCE, token).operation.operation_id
    digest = coordinator.acquire(operation_id)
    assert digest
    coordinator.phase(operation_id, digest, "removing_old")
    row = coordinator.get(operation_id)
    assert row is not None
    resolver = SqliteReconstructionAuthorizationResolver(factory)
    resolver._provisioning.resolve = lambda _instance: ManagedContainerProvisioningSpec(
        managed_instance_id=str(INSTANCE),
        runtime_revision_id=str(DESIRED),
        image_id=NEW_IMAGE,
        container_name=f"kodi-lab-managed-{INSTANCE.hex}",
        runtime_uid=1000,
        runtime_gid=1000,
        python_bytecode_policy=PythonBytecodePolicy.INTERPRETER_DEFAULT,
    )
    request = dict(
        managed_instance_id=str(INSTANCE),
        operation_id=str(operation_id),
        lease_token_digest=digest,
        owned_runtime_state_revision=row.owned_runtime_state_revision,
        old_container_id=OLD_ID,
        old_image_id=OLD_IMAGE,
        old_runtime_revision_id=str(OLD),
        desired_runtime_revision_id=str(DESIRED),
    )
    assert resolver.authorize_removal(**request).image_id == NEW_IMAGE
    for changes in (
        {"old_container_id": "9" * 64},
        {"old_image_id": "sha256:" + "9" * 64},
        {"old_runtime_revision_id": str(DESIRED)},
        {"desired_runtime_revision_id": str(OLD)},
        {"owned_runtime_state_revision": row.owned_runtime_state_revision - 1},
        {"lease_token_digest": "f" * 64},
    ):
        with pytest.raises(ProvisioningStateError):
            resolver.authorize_removal(**(request | changes))
    with factory.begin() as session:
        operation = session.get(ManagedRuntimeReconstructionRow, str(operation_id))
        assert operation is not None
        operation.lease_expires_at = "2020-01-01T00:00:00Z"
    with pytest.raises(ProvisioningStateError):
        resolver.authorize_removal(**request)
    with factory.begin() as session:
        revision = session.get(RuntimeRevisionRow, str(DESIRED))
        assert revision is not None
        revision.approval_state = "not_approved"
        revision.approved_at = None
    with pytest.raises(ProvisioningStateError):
        resolver.authorize_removal(**request)
    assert broker.removals == 0


@pytest.mark.parametrize("fixture_fails", [False, True])
def test_assigned_fixture_is_retained_and_failure_keeps_fresh_stopped(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    fixture_fails: bool,
) -> None:
    fixture = load_packaged_fixture("small-movies-v5").definition
    with factory.begin() as session:
        FixtureDefinitionRepository(session).create(fixture)
        instance = session.get(ManagedInstanceRow, str(INSTANCE))
        assert instance is not None
        instance.fixture_definition_id = str(fixture.id)
    calls: list[str] = []

    def apply_fixture(
        _self: DisposableInstanceResetService,
        _instance_id: UUID,
        container_id: str,
    ) -> object:
        calls.append(container_id)
        if fixture_fails:
            raise FixtureInitializationFailedError(
                ResetPhase.FIXTURE_INITIALIZATION,
                "fixture initialization failed",
            )
        return object()

    monkeypatch.setattr(
        DisposableInstanceResetService,
        "initialize_assigned_fixture_on_fresh_container",
        apply_fixture,
    )
    broker = Broker()
    coordinator = ManagedRuntimeReconstructionCoordinator(factory, broker)
    token = coordinator.eligibility(INSTANCE).observation_token
    assert token
    accepted = coordinator.queue(INSTANCE, token)
    executor = ManagedRuntimeReconstructionExecutor(coordinator, factory, broker)
    executor._execute(accepted.operation.operation_id)
    row = coordinator.get(accepted.operation.operation_id)
    assert row is not None
    assert row.state == ("failed" if fixture_fails else "succeeded")
    assert row.old_discarded and row.fresh_container_id == FRESH_ID
    assert calls == [FRESH_ID]
    assert coordinator.reconciliation_state(row) == "expected_fresh_stopped"
    with factory() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE))
        assert instance is not None
        assert instance.fixture_definition_id == str(fixture.id)
    executor.close()


def test_fixture_stage_transitions_renew_reconstruction_lease(
    factory: sessionmaker[Session],
) -> None:
    broker = Broker()
    coordinator = ManagedRuntimeReconstructionCoordinator(factory, broker)
    token = coordinator.eligibility(INSTANCE).observation_token
    assert token
    operation_id = coordinator.queue(INSTANCE, token).operation.operation_id
    digest = coordinator.acquire(operation_id)
    assert digest
    coordinator.phase(operation_id, digest, "applying_fixture")
    observer = _ReconstructionFixtureActivity(coordinator, operation_id, digest)
    activity = observer.begin(INSTANCE)
    observer.transition(
        INSTANCE, activity.generation, ResetActivityState.APPLYING_FIXTURE
    )
    row = coordinator.get(operation_id)
    assert row is not None and row.lease_expires_at is not None
    with factory() as session:
        lease = session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE))
        assert lease is not None
        assert lease.lease_expires_at == row.lease_expires_at


def test_expired_reconstruction_becomes_read_only_reconciliation(
    factory: sessionmaker[Session],
) -> None:
    broker = Broker()
    coordinator = ManagedRuntimeReconstructionCoordinator(factory, broker)
    token = coordinator.eligibility(INSTANCE).observation_token
    assert token
    operation_id = coordinator.queue(INSTANCE, token).operation.operation_id
    digest = coordinator.acquire(operation_id)
    assert digest
    coordinator.phase(operation_id, digest, "removing_old")
    expired = (
        (datetime.now(UTC) - timedelta(minutes=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    with factory.begin() as session:
        row = session.get(ManagedRuntimeReconstructionRow, str(operation_id))
        lease = session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE))
        assert row is not None and lease is not None
        row.lease_expires_at = expired
        lease.lease_expires_at = expired
    StaleOperationReconciliationDispatcher(factory, broker=broker).sweep_instance(
        INSTANCE
    )
    row = coordinator.get(operation_id)
    assert row is not None and row.state == "reconciliation_required"
    assert row.phase == "removing_old"
    status = ManagedInstanceReconciliationService(factory, broker=broker).status(
        INSTANCE
    )
    assert status.blocking
    assert status.observed_state == "exact_old_container"
    assert broker.removals == 0
    broker.containers[0] = entry(container_id="9" * 64)
    with pytest.raises(ReconstructionConflictError):
        coordinator.resolve_interrupted(operation_id, "exact_old_container")
