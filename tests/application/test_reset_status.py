from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reset_activity import (
    ResetActivityRegistry,
    ResetActivityState,
)
from kodi_lab.application.reset_operations import (
    LEASE_DURATION,
    ResetOperationCoordinator,
)
from kodi_lab.application.reset_status import (
    FixtureCompatibilityState,
    ResetEligibility,
    ResetEligibilityReason,
    ResetStatusService,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
)
from kodi_lab.broker.errors import BrokerUnavailableError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
)
from kodi_lab.domain import (
    FixtureContentCategory,
    FixtureDefinition,
    FixtureSizeCategory,
    InstanceType,
)
from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import FixtureDefinitionRepository

INSTANCE_ID = UUID("00000000-0000-4000-8000-000000000101")
DEFINITION_ID = UUID("00000000-0000-4000-8000-000000000102")
REVISION_ID = UUID("00000000-0000-4000-8000-000000000103")


def seed_instance(factory: sessionmaker[Session]) -> None:
    digest = "sha256:" + ("c" * 64)
    with factory.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="kodi-21.3", kodi_version="21.3"
            )
        )
        session.add(
            RuntimeRevisionRow(
                id=str(REVISION_ID),
                runtime_definition_id=str(DEFINITION_ID),
                revision_number=2,
                source_kind="official_package",
                source_reference="test",
                content_digest=digest,
                manifest_reference="runtime/kodi-21.3/manifest.json",
                manifest_digest=digest,
                built_at="2026-09-15T00:00:00.000000Z",
                build_log_reference=None,
                approval_state="approved",
                approved_at="2026-09-15T00:00:00.000000Z",
                approval_note=None,
                requires_trusted_build_confirmation=False,
                trusted_build_confirmed_at=None,
            )
        )
        session.add(
            TargetRow(
                id=str(INSTANCE_ID),
                name="Status target",
                target_type="managed",
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
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
        session.add(
            RuntimeExecutionProfileRow(
                runtime_revision_id=str(REVISION_ID),
                runtime_kind="kodi",
                expected_application_name="Kodi",
                expected_kodi_version="21.3",
                expected_version_tag="stable",
                jsonrpc_port=8080,
                jsonrpc_path="/jsonrpc",
                process_observation="docker_healthcheck",
                readiness_probe="kodi_jsonrpc",
                readiness_timeout_seconds=90,
                poll_interval_seconds=1.0,
                http_timeout_seconds=3.0,
            )
        )


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'reset-status.db'}")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    seed_instance(result)
    yield result
    engine.dispose()


class InventoryBroker:
    def __init__(self, states: list[ContainerRuntimeState] | None = None) -> None:
        self.states = [] if states is None else states
        self.failure = False

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        if self.failure:
            raise BrokerUnavailableError("private socket path")
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(INSTANCE_ID),
                    container_id=str(index + 1) * 64,
                    image_id="sha256:" + ("b" * 64),
                    runtime_state=state,
                    health_state=ContainerHealthState.NOT_CONFIGURED,
                )
                for index, state in enumerate(self.states)
            ]
        )


def assign_fixture(factory: sessionmaker[Session], fixture: FixtureDefinition) -> None:
    with factory.begin() as session:
        FixtureDefinitionRepository(session).create(fixture)
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert instance is not None
        instance.fixture_definition_id = str(fixture.id)


@pytest.mark.parametrize(
    ("states", "provision", "container"),
    [
        ([], "unprovisioned", None),
        ([ContainerRuntimeState.STOPPED], "provisioned", "stopped"),
        ([ContainerRuntimeState.RUNNING], "provisioned", "running"),
    ],
)
def test_disposable_safe_states_are_supported_with_compatible_fixture(
    factory: sessionmaker[Session],
    states: list[ContainerRuntimeState],
    provision: str,
    container: str | None,
) -> None:
    fixture = load_packaged_fixture("small-mixed-v2").definition
    assign_fixture(factory, fixture)
    status = ResetStatusService(
        factory, InventoryBroker(states), ResetActivityRegistry()
    ).get(INSTANCE_ID)
    assert status.eligibility is ResetEligibility.SUPPORTED
    assert status.fixture.compatibility is FixtureCompatibilityState.COMPATIBLE
    assert status.fixture.slug == "small-mixed-v2"
    assert status.fixture.fixture_version == 2
    assert status.provision_state == provision
    assert status.container_state == container
    assert status.pinned_runtime_revision_id == REVISION_ID
    assert status.pinned_runtime_revision_number == 2
    assert status.pinned_kodi_release == "21.3"
    assert "does not upgrade Kodi" in status.exact_pin_statement


def test_historical_v1_assignment_is_presented_without_substitution(
    factory: sessionmaker[Session],
) -> None:
    fixture = load_packaged_fixture("small-mixed-v1").definition
    assign_fixture(factory, fixture)
    status = ResetStatusService(
        factory, InventoryBroker(), ResetActivityRegistry()
    ).get(INSTANCE_ID)
    assert status.fixture.slug == "small-mixed-v1"
    assert status.fixture.fixture_version == 1
    assert status.fixture.compatibility is FixtureCompatibilityState.COMPATIBLE
    assert status.fixture.baseline_note is not None
    assert "not proven" in status.fixture.baseline_note


def test_none_persistent_incompatible_and_unsafe_states(
    factory: sessionmaker[Session],
) -> None:
    registry = ResetActivityRegistry()
    none = ResetStatusService(factory, InventoryBroker(), registry).get(INSTANCE_ID)
    assert none.fixture.compatibility is FixtureCompatibilityState.NONE_ASSIGNED

    with factory.begin() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert instance is not None
        instance.instance_type = InstanceType.PERSISTENT
    persistent = ResetStatusService(factory, InventoryBroker(), registry).get(
        INSTANCE_ID
    )
    assert persistent.eligibility_reason is ResetEligibilityReason.PERSISTENT_INSTANCE

    incompatible = FixtureDefinition(
        id=uuid4(),
        slug="status-incompatible-v1",
        fixture_version=1,
        size_category=FixtureSizeCategory.SMALL,
        content_category=FixtureContentCategory.MIXED,
        content_digest="a" * 64,
        compatible_kodi_version="20.5",
        description="Status-only incompatible fixture metadata.",
    )
    with factory.begin() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert instance is not None
        instance.instance_type = InstanceType.DISPOSABLE
    assign_fixture(factory, incompatible)
    mismatch = ResetStatusService(factory, InventoryBroker(), registry).get(INSTANCE_ID)
    assert mismatch.eligibility_reason is ResetEligibilityReason.FIXTURE_INCOMPATIBLE

    transitional = ResetStatusService(
        factory,
        InventoryBroker([ContainerRuntimeState.TRANSITIONAL]),
        registry,
    ).get(INSTANCE_ID)
    assert (
        transitional.eligibility_reason is ResetEligibilityReason.FIXTURE_INCOMPATIBLE
    )
    # Remove the incompatible assignment so container proof gates become decisive.
    with factory.begin() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert instance is not None
        instance.fixture_definition_id = None
    transitional = ResetStatusService(
        factory,
        InventoryBroker([ContainerRuntimeState.TRANSITIONAL]),
        registry,
    ).get(INSTANCE_ID)
    assert (
        transitional.eligibility_reason
        is ResetEligibilityReason.TRANSITIONAL_CONTAINER_STATE
    )
    ambiguous = ResetStatusService(
        factory,
        InventoryBroker([ContainerRuntimeState.STOPPED, ContainerRuntimeState.STOPPED]),
        registry,
    ).get(INSTANCE_ID)
    assert (
        ambiguous.eligibility_reason
        is ResetEligibilityReason.AMBIGUOUS_CONTAINER_OWNERSHIP
    )


def test_broker_activity_results_reconciliation_and_restart_semantics(
    factory: sessionmaker[Session],
) -> None:
    broker = InventoryBroker()
    broker.failure = True
    registry = ResetActivityRegistry()
    unavailable = ResetStatusService(factory, broker, registry).get(INSTANCE_ID)
    assert unavailable.eligibility_reason is ResetEligibilityReason.BROKER_UNAVAILABLE
    assert "private socket path" not in unavailable.model_dump_json()

    broker.failure = False
    activity = registry.begin(INSTANCE_ID)
    registry.transition(
        INSTANCE_ID, activity.generation, ResetActivityState.PROVISIONING
    )
    active = ResetStatusService(factory, broker, registry).get(INSTANCE_ID)
    assert active.eligibility_reason is ResetEligibilityReason.RESET_IN_PROGRESS
    assert active.activity_state is ResetActivityState.PROVISIONING
    registry.succeed(INSTANCE_ID, activity.generation)
    succeeded = ResetStatusService(factory, broker, registry).get(INSTANCE_ID)
    assert succeeded.eligibility is ResetEligibility.SUPPORTED
    assert succeeded.last_outcome == "succeeded"

    failed_activity = registry.begin(INSTANCE_ID)
    registry.fail(
        INSTANCE_ID,
        failed_activity.generation,
        failure_phase="provision",
        failure_reason="The exact pin could not be provisioned.",
        reconciliation_required=False,
    )
    ordinary_failure = ResetStatusService(factory, broker, registry).get(INSTANCE_ID)
    assert ordinary_failure.eligibility is ResetEligibility.SUPPORTED
    assert ordinary_failure.last_outcome == "failed"
    assert ordinary_failure.failure_phase == "provision"

    next_activity = registry.begin(INSTANCE_ID)
    registry.fail(
        INSTANCE_ID,
        next_activity.generation,
        failure_phase="final_stop",
        failure_reason="The fresh container could not be safely stopped.",
        reconciliation_required=True,
    )
    failed = ResetStatusService(factory, broker, registry).get(INSTANCE_ID)
    assert failed.reconciliation_required
    assert failed.eligibility_reason is ResetEligibilityReason.RECONCILIATION_REQUIRED
    assert failed.failure_phase == "final_stop"

    restarted = ResetStatusService(factory, broker, ResetActivityRegistry()).get(
        INSTANCE_ID
    )
    assert restarted.activity_state is ResetActivityState.IDLE
    assert not restarted.reconciliation_required


def test_status_does_not_substitute_newer_revision_or_mutate_assignment(
    factory: sessionmaker[Session],
) -> None:
    fixture = load_packaged_fixture("small-mixed-v2").definition
    assign_fixture(factory, fixture)
    newer_id = uuid4()
    with factory.begin() as session:
        pinned = session.get(RuntimeRevisionRow, str(REVISION_ID))
        assert pinned is not None
        session.add(
            RuntimeRevisionRow(
                id=str(newer_id),
                runtime_definition_id=pinned.runtime_definition_id,
                revision_number=3,
                source_kind=pinned.source_kind,
                source_reference="newer",
                content_digest="sha256:" + ("d" * 64),
                manifest_reference="newer",
                manifest_digest="sha256:" + ("e" * 64),
                built_at=pinned.built_at,
                build_log_reference=None,
                approval_state="approved",
                approved_at=pinned.approved_at,
                approval_note=None,
                requires_trusted_build_confirmation=False,
                trusted_build_confirmed_at=None,
            )
        )
    status = ResetStatusService(
        factory, InventoryBroker(), ResetActivityRegistry()
    ).get(INSTANCE_ID)
    assert status.pinned_runtime_revision_id == REVISION_ID
    assert status.pinned_runtime_revision_id != newer_id
    with factory() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert instance is not None
        assert instance.fixture_definition_id == str(fixture.id)


def test_durable_operation_is_authoritative_across_status_service_restart(
    factory: sessionmaker[Session],
) -> None:
    coordinator = ResetOperationCoordinator(factory)
    queued = coordinator.queue(INSTANCE_ID)
    first_process = ResetStatusService(
        factory, InventoryBroker(), ResetActivityRegistry(), coordinator
    ).get(INSTANCE_ID)
    assert first_process.activity_state is ResetActivityState.QUEUED
    assert first_process.operation is not None
    assert first_process.operation.operation_id == queued.operation.operation_id
    assert first_process.eligibility_reason is ResetEligibilityReason.RESET_IN_PROGRESS

    restarted = ResetStatusService(
        factory,
        InventoryBroker(),
        ResetActivityRegistry(),
        ResetOperationCoordinator(factory),
    ).get(INSTANCE_ID)
    assert restarted.operation is not None
    assert restarted.operation.state == "queued"
    assert restarted.activity_state is ResetActivityState.QUEUED


def test_stale_durable_running_operation_becomes_reconciliation_required(
    factory: sessionmaker[Session],
) -> None:
    current = [datetime(2026, 9, 15, tzinfo=UTC)]
    coordinator = ResetOperationCoordinator(factory, clock=lambda: current[0])
    queued = coordinator.queue(INSTANCE_ID)
    assert coordinator.acquire(queued.operation.operation_id) is not None
    current[0] += LEASE_DURATION + timedelta(seconds=1)
    status = ResetStatusService(
        factory, InventoryBroker(), ResetActivityRegistry(), coordinator
    ).get(INSTANCE_ID)
    assert status.operation is not None
    assert status.operation.state == "reconciliation_required"
    assert status.reconciliation_required
    assert status.eligibility_reason is ResetEligibilityReason.RECONCILIATION_REQUIRED
