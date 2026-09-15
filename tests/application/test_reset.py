from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reset import (
    DisposableInstanceResetService,
    FixtureApplyFailedError,
    FixtureInitializationFailedError,
    PerInstanceResetLocks,
    PersistentResetUnsupportedError,
    ResetDeprovisionFailedError,
    ResetInProgressError,
    ResetProvisionFailedError,
    ResetReconciliationRequiredError,
    ResetStateConflictError,
    ResetStopFailedError,
)
from kodi_lab.application.reset_activity import (
    ResetActivityRegistry,
    ResetActivitySnapshot,
    ResetActivityState,
    ResetOutcome,
)
from kodi_lab.application.runtime_eligibility import (
    RuntimeRevisionEligibilityPolicy,
    RuntimeSelectionPolicy,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
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
    KODI_RUNTIME_VALIDATION_CONTRACT,
    ConnectionState,
    HealthStatus,
    InstanceRuntimeState,
    InstanceType,
    ManagedInstance,
    ProcessObservationKind,
    ReadinessProbeKind,
    RevisionApprovalState,
    RuntimeContainerArtifact,
    RuntimeDefinition,
    RuntimeExecutionProfile,
    RuntimeKind,
    RuntimeRevision,
    RuntimeValidationEvidence,
    RuntimeValidationResult,
    Target,
    TargetAvailability,
    TargetType,
)
from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.kodi.fixtures import FixtureLibraryState
from kodi_lab.kodi.readiness import (
    JsonRpcState,
    ReadinessResult,
    UsabilityState,
)
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    FixtureDefinitionRepository,
    ManagedInstanceRepository,
    RuntimeContainerArtifactRepository,
    RuntimeDefinitionRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
    RuntimeValidationEvidenceRepository,
    TargetRepository,
)

NOW = datetime(2026, 9, 15, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'reset.db'}")
    metadata.create_all(engine)
    factory = create_session_factory(engine)
    yield factory
    engine.dispose()


def seed_reset_context(
    factory: sessionmaker[Session],
    *,
    instance_type: InstanceType = InstanceType.DISPOSABLE,
    assigned_fixture: bool = False,
    fixture_slug: str = "small-mixed-v1",
    kodi_version: str = "21.3",
) -> tuple[UUID, UUID, UUID | None]:
    instance_id = uuid4()
    definition = RuntimeDefinition(
        uuid4(),
        f"kodi-reset-{kodi_version.replace('.', '-')}-{instance_id.hex}",
        kodi_version,
    )
    revision = RuntimeRevision(
        id=uuid4(),
        runtime_definition_id=definition.id,
        revision_number=1,
        source_kind="test",
        source_reference="test",
        content_digest="a" * 64,
        manifest_reference="test",
        manifest_digest="b" * 64,
        built_at=NOW,
        approval_state=RevisionApprovalState.APPROVED,
        approved_at=NOW,
    )
    fixture = load_packaged_fixture(fixture_slug).definition
    fixture_id = fixture.id if assigned_fixture else None
    with factory.begin() as session:
        TargetRepository(session).create(
            Target(
                id=instance_id,
                name="Reset target",
                target_type=TargetType.MANAGED,
                kodi_version=kodi_version,
                availability=TargetAvailability.UNAVAILABLE,
                health=HealthStatus.WARNING,
                connection_state=ConnectionState.DISCONNECTED,
            )
        )
        RuntimeDefinitionRepository(session).create(definition)
        RuntimeRevisionRepository(session).create(revision)
        RuntimeContainerArtifactRepository(session).create(
            RuntimeContainerArtifact(revision.id, "sha256:" + ("c" * 64))
        )
        RuntimeExecutionProfileRepository(session).create(
            RuntimeExecutionProfile(
                runtime_revision_id=revision.id,
                runtime_kind=RuntimeKind.KODI,
                expected_application_name="Kodi",
                expected_kodi_version=kodi_version,
                expected_version_tag="stable",
                jsonrpc_port=8080,
                jsonrpc_path="/jsonrpc",
                process_observation=ProcessObservationKind.DOCKER_HEALTHCHECK,
                readiness_probe=ReadinessProbeKind.KODI_JSONRPC,
                readiness_timeout_seconds=90,
                poll_interval_seconds=1,
                http_timeout_seconds=3,
            )
        )
        if assigned_fixture:
            fixtures = FixtureDefinitionRepository(session)
            if fixtures.get(fixture.id) is None:
                fixtures.create(fixture)
        ManagedInstanceRepository(session).create(
            ManagedInstance(
                target_id=instance_id,
                instance_type=instance_type,
                runtime_revision_id=revision.id,
                runtime_state=InstanceRuntimeState.STOPPED,
                notes="preserve me",
                fixture_definition_id=fixture_id,
            )
        )
        newer = RuntimeRevision(
            id=uuid4(),
            runtime_definition_id=definition.id,
            revision_number=2,
            source_kind="test",
            source_reference="newer",
            content_digest="d" * 64,
            manifest_reference="newer",
            manifest_digest="e" * 64,
            built_at=NOW,
            approval_state=RevisionApprovalState.APPROVED,
            approved_at=NOW,
        )
        RuntimeRevisionRepository(session).create(newer)
        RuntimeContainerArtifactRepository(session).create(
            RuntimeContainerArtifact(newer.id, "sha256:" + ("f" * 64))
        )
        RuntimeExecutionProfileRepository(session).create(
            RuntimeExecutionProfile(
                runtime_revision_id=newer.id,
                runtime_kind=RuntimeKind.KODI,
                expected_application_name="Kodi",
                expected_kodi_version=kodi_version,
                expected_version_tag="stable",
                jsonrpc_port=8080,
                jsonrpc_path="/jsonrpc",
                process_observation=ProcessObservationKind.DOCKER_HEALTHCHECK,
                readiness_probe=ReadinessProbeKind.KODI_JSONRPC,
                readiness_timeout_seconds=90,
                poll_interval_seconds=1,
                http_timeout_seconds=3,
            )
        )
        RuntimeValidationEvidenceRepository(session).create(
            RuntimeValidationEvidence(
                id=uuid4(),
                runtime_revision_id=newer.id,
                contract_version=KODI_RUNTIME_VALIDATION_CONTRACT,
                result=RuntimeValidationResult.PASSED,
                started_at=NOW,
                completed_at=NOW,
                evidence_reference="test://newer-eligible",
                recorded_at=NOW,
            )
        )
    return instance_id, revision.id, fixture_id


class FakeBroker:
    def __init__(
        self,
        instance_id: UUID,
        revision_id: UUID,
        *,
        state: ContainerRuntimeState | None = None,
        fixture_slug: str = "small-mixed-v1",
    ) -> None:
        self.instance_id = str(instance_id)
        self.revision_id = str(revision_id)
        self.state = state
        self.container_id = None if state is None else "1" * 64
        self.next_id = 2
        self.calls: list[str] = []
        self.fail: str | None = None
        self.stop_leaves_running = False
        self.duplicate_inventory = False
        self.fixture_slug = fixture_slug

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        self.calls.append("inventory")
        if self.fail == "inventory":
            raise RuntimeError("ambiguous")
        containers = []
        if self.container_id is not None and self.state is not None:
            containers.append(
                ManagedContainerInventoryEntry(
                    managed_instance_id=self.instance_id,
                    container_id=self.container_id,
                    image_id="sha256:" + ("c" * 64),
                    runtime_state=self.state,
                    health_state=ContainerHealthState.HEALTHY,
                )
            )
            if self.duplicate_inventory:
                containers.append(containers[0].model_copy())
        return ManagedContainerInventoryResult(containers=containers)

    def provision_managed_container(
        self, **_kwargs: object
    ) -> ProvisionManagedContainerResult:
        self.calls.append("provision")
        if self.fail == "provision":
            raise RuntimeError("provision failed")
        self.container_id = f"{self.next_id:064x}"
        self.next_id += 1
        self.state = ContainerRuntimeState.STOPPED
        return ProvisionManagedContainerResult(
            managed_instance_id=self.instance_id,
            runtime_revision_id=self.revision_id,
            container_id=self.container_id,
            image_id="sha256:" + ("c" * 64),
            runtime_state=self.state,
            created=True,
        )

    def deprovision_managed_container(
        self, **_kwargs: object
    ) -> DeprovisionManagedContainerResult:
        self.calls.append("deprovision")
        if self.fail == "deprovision":
            raise RuntimeError("deprovision failed")
        old = self.container_id
        assert old is not None
        self.container_id = None
        self.state = None
        return DeprovisionManagedContainerResult(
            managed_instance_id=self.instance_id,
            removed_container_id=old,
            runtime_revision_id=self.revision_id,
            removed=True,
        )

    def stop_managed_container(self, **_kwargs: object) -> StopManagedContainerResult:
        self.calls.append("stop")
        if self.fail in {"stop", "final_stop"}:
            raise RuntimeError("stop failed")
        assert self.container_id is not None
        if not self.stop_leaves_running:
            self.state = ContainerRuntimeState.STOPPED
        return StopManagedContainerResult(
            managed_instance_id=self.instance_id,
            container_id=self.container_id,
            runtime_state=ContainerRuntimeState.STOPPED,
            stop_performed=True,
        )

    def start_managed_container(self, **_kwargs: object) -> StartManagedContainerResult:
        self.calls.append("start")
        if self.fail == "start":
            raise RuntimeError("start failed")
        assert self.container_id is not None
        self.state = ContainerRuntimeState.RUNNING
        return StartManagedContainerResult(
            managed_instance_id=self.instance_id,
            container_id=self.container_id,
            runtime_state=self.state,
            start_performed=True,
        )

    def apply_assigned_fixture(self, **_kwargs: object) -> ApplyAssignedFixtureResult:
        self.calls.append("apply_fixture")
        if self.fail == "apply_fixture":
            raise RuntimeError("fixture apply failed")
        assert self.container_id is not None
        fixture = load_packaged_fixture(self.fixture_slug).definition
        return ApplyAssignedFixtureResult(
            managed_instance_id=self.instance_id,
            runtime_revision_id=self.revision_id,
            container_id=self.container_id,
            fixture_slug=cast(
                Literal[
                    "small-mixed-v1",
                    "small-mixed-v2",
                    "small-mixed-v3",
                    "small-mixed-v4",
                    "small-movies-v1",
                    "small-tv-v1",
                ],
                fixture.slug,
            ),
            fixture_version=cast(Literal[1, 2, 3, 4], fixture.fixture_version),
            content_digest=fixture.content_digest,
            runtime_state=ContainerRuntimeState.STOPPED,
        )

    def observe_managed_kodi_runtime(
        self, **_kwargs: object
    ) -> ObserveManagedKodiRuntimeResult:
        assert self.container_id is not None
        return ObserveManagedKodiRuntimeResult(
            managed_instance_id=self.instance_id,
            runtime_revision_id=self.revision_id,
            container_id=self.container_id,
            runtime_state=ContainerRuntimeState.RUNNING,
            process_state=KodiProcessState.RUNNING,
            ipv4_address="10.250.0.2",
            jsonrpc_port=8080,
        )


class FakeReadiness:
    def __init__(self, usable: bool = True) -> None:
        self.usable = usable

    def wait_until_usable(self, _instance_id: str) -> ReadinessResult:
        return ReadinessResult(
            container_state=ContainerRuntimeState.RUNNING,
            process_state=KodiProcessState.RUNNING,
            jsonrpc_state=JsonRpcState.RESPONSIVE,
            usability_state=(
                UsabilityState.USABLE if self.usable else UsabilityState.NOT_USABLE
            ),
            failure_reason=None,
            last_observed_failure=None,
            identity=None,
            elapsed_seconds=1,
            attempts=1,
        )


class FakeFixtureAdapter:
    def __init__(
        self,
        *,
        fail_scan: bool = False,
        state: FixtureLibraryState | None = None,
    ) -> None:
        self.fail_scan = fail_scan
        self.state = state or FixtureLibraryState(True, True, True, 1, 1, 1)

    def scan_assigned_fixture_library(self) -> None:
        if self.fail_scan:
            raise RuntimeError("scan failed")

    def wait_for_fixture_state(self) -> FixtureLibraryState:
        return self.state


def service(
    factory: sessionmaker[Session],
    broker: FakeBroker,
    *,
    readiness: FakeReadiness | None = None,
    fixture_adapter: FakeFixtureAdapter | None = None,
    locks: PerInstanceResetLocks | None = None,
    activity: ResetActivityRegistry | None = None,
) -> DisposableInstanceResetService:
    return DisposableInstanceResetService(
        factory,
        broker,
        locks=locks,
        activity=activity,
        readiness_factory=lambda _broker, _profile: readiness or FakeReadiness(),
        fixture_adapter_factory=(
            lambda _observation, _profile, _semantics: (
                fixture_adapter or FakeFixtureAdapter()
            )
        ),
    )


@pytest.mark.parametrize(
    "initial", [None, ContainerRuntimeState.STOPPED, ContainerRuntimeState.RUNNING]
)
def test_no_fixture_reset_recreates_exact_pin_and_ends_stopped(
    session_factory: sessionmaker[Session], initial: ContainerRuntimeState | None
) -> None:
    instance_id, revision_id, _fixture = seed_reset_context(session_factory)
    broker = FakeBroker(instance_id, revision_id, state=initial)
    old_container = broker.container_id
    result = service(session_factory, broker).reset(instance_id)
    assert result.runtime_revision_id == revision_id
    assert result.managed_instance_id == instance_id
    assert result.final_container_state is ContainerRuntimeState.STOPPED
    assert result.fixture_slug is None
    assert broker.container_id != old_container
    assert "apply_fixture" not in broker.calls
    assert "start" not in broker.calls


@pytest.mark.parametrize(
    "initial", [None, ContainerRuntimeState.STOPPED, ContainerRuntimeState.RUNNING]
)
def test_fixture_reset_applies_scans_and_retains_assignment(
    session_factory: sessionmaker[Session], initial: ContainerRuntimeState | None
) -> None:
    instance_id, revision_id, fixture_id = seed_reset_context(
        session_factory, assigned_fixture=True
    )
    broker = FakeBroker(instance_id, revision_id, state=initial)
    result = service(session_factory, broker).reset(instance_id)
    assert result.fixture_slug == "small-mixed-v1"
    assert result.fixture_version == 1
    assert result.fixture_state is not None and result.fixture_state.is_baseline
    assert broker.state is ContainerRuntimeState.STOPPED
    assert broker.calls.index("apply_fixture") < broker.calls.index("start")
    with session_factory() as session:
        retained = ManagedInstanceRepository(session).get(instance_id)
        assert retained is not None
        assert retained.runtime_revision_id == revision_id
        assert retained.fixture_definition_id == fixture_id
        assert retained.notes == "preserve me"


def test_v2_reset_retains_exact_revision_and_database_seed_assignment(
    session_factory: sessionmaker[Session],
) -> None:
    instance_id, revision_id, fixture_id = seed_reset_context(
        session_factory,
        assigned_fixture=True,
        fixture_slug="small-mixed-v2",
    )
    broker = FakeBroker(
        instance_id,
        revision_id,
        fixture_slug="small-mixed-v2",
    )

    result = service(session_factory, broker).reset(instance_id)

    assert result.managed_instance_id == instance_id
    assert result.runtime_revision_id == revision_id
    assert result.fixture_slug == "small-mixed-v2"
    assert result.fixture_version == 2
    with session_factory() as session:
        retained = ManagedInstanceRepository(session).get(instance_id)
        assert retained is not None
        assert retained.runtime_revision_id == revision_id
        assert retained.fixture_definition_id == fixture_id


def test_v3_reset_uses_same_flow_and_retains_exact_nexus_revision(
    session_factory: sessionmaker[Session],
) -> None:
    instance_id, revision_id, fixture_id = seed_reset_context(
        session_factory,
        assigned_fixture=True,
        fixture_slug="small-mixed-v3",
        kodi_version="20.5",
    )
    broker = FakeBroker(
        instance_id,
        revision_id,
        fixture_slug="small-mixed-v3",
    )

    result = service(session_factory, broker).reset(instance_id)

    assert result.runtime_revision_id == revision_id
    assert result.fixture_slug == "small-mixed-v3"
    assert result.fixture_version == 3
    assert broker.calls.index("apply_fixture") < broker.calls.index("start")
    with session_factory() as session:
        retained = ManagedInstanceRepository(session).get(instance_id)
        assert retained is not None
        assert retained.runtime_revision_id == revision_id
        assert retained.fixture_definition_id == fixture_id


def test_v4_reset_uses_same_flow_and_retains_exact_matrix_revision(
    session_factory: sessionmaker[Session],
) -> None:
    instance_id, revision_id, fixture_id = seed_reset_context(
        session_factory,
        assigned_fixture=True,
        fixture_slug="small-mixed-v4",
        kodi_version="19.5",
    )
    broker = FakeBroker(
        instance_id,
        revision_id,
        fixture_slug="small-mixed-v4",
    )

    result = service(session_factory, broker).reset(instance_id)

    assert result.runtime_revision_id == revision_id
    assert result.fixture_slug == "small-mixed-v4"
    assert result.fixture_version == 4
    assert broker.calls.index("apply_fixture") < broker.calls.index("start")
    with session_factory() as session:
        retained = ManagedInstanceRepository(session).get(instance_id)
        assert retained is not None
        assert retained.runtime_revision_id == revision_id
        assert retained.fixture_definition_id == fixture_id


def test_small_movies_reset_uses_generic_flow_and_retains_exact_assignment(
    session_factory: sessionmaker[Session],
) -> None:
    instance_id, revision_id, fixture_id = seed_reset_context(
        session_factory,
        assigned_fixture=True,
        fixture_slug="small-movies-v1",
    )
    broker = FakeBroker(
        instance_id,
        revision_id,
        fixture_slug="small-movies-v1",
    )
    adapter = FakeFixtureAdapter(
        state=FixtureLibraryState(True, False, False, 3, 0, 0, 3, 0, 0)
    )

    result = service(session_factory, broker, fixture_adapter=adapter).reset(
        instance_id
    )

    assert result.runtime_revision_id == revision_id
    assert result.fixture_slug == "small-movies-v1"
    assert result.fixture_state is not None and result.fixture_state.is_baseline
    assert broker.calls.index("apply_fixture") < broker.calls.index("start")
    with session_factory() as session:
        retained = ManagedInstanceRepository(session).get(instance_id)
        assert retained is not None
        assert retained.runtime_revision_id == revision_id
        assert retained.fixture_definition_id == fixture_id


def test_small_tv_reset_uses_generic_flow_and_retains_exact_assignment(
    session_factory: sessionmaker[Session],
) -> None:
    instance_id, revision_id, fixture_id = seed_reset_context(
        session_factory,
        assigned_fixture=True,
        fixture_slug="small-tv-v1",
    )
    broker = FakeBroker(instance_id, revision_id, fixture_slug="small-tv-v1")
    adapter = FakeFixtureAdapter(
        state=FixtureLibraryState(False, True, True, 0, 1, 3, 0, 1, 3)
    )

    result = service(session_factory, broker, fixture_adapter=adapter).reset(
        instance_id
    )

    assert result.runtime_revision_id == revision_id
    assert result.fixture_slug == "small-tv-v1"
    assert result.fixture_state is not None and result.fixture_state.is_baseline
    assert broker.calls.index("apply_fixture") < broker.calls.index("start")
    with session_factory() as session:
        retained = ManagedInstanceRepository(session).get(instance_id)
        assert retained is not None
        assert retained.runtime_revision_id == revision_id
        assert retained.fixture_definition_id == fixture_id


def test_reset_deliberately_ignores_a_newer_eligible_revision(
    session_factory: sessionmaker[Session],
) -> None:
    instance_id, pinned_revision_id, _fixture = seed_reset_context(session_factory)
    with session_factory() as session:
        target = TargetRepository(session).get(instance_id)
        pinned = RuntimeRevisionRepository(session).get(pinned_revision_id)
        assert target is not None and pinned is not None
        definition = RuntimeDefinitionRepository(session).get(
            pinned.runtime_definition_id
        )
        assert definition is not None
        revisions = RuntimeRevisionRepository(session).list_for_definition(
            definition.id
        )
        newer = next(
            revision for revision in revisions if revision.revision_number == 2
        )
        assert (
            RuntimeRevisionEligibilityPolicy()
            .evaluate(
                session,
                target=target,
                definition=definition,
                revision=newer,
                policy=RuntimeSelectionPolicy(),
            )
            .eligible
        )

    broker = FakeBroker(instance_id, pinned_revision_id)
    result = service(session_factory, broker).reset(instance_id)
    assert result.runtime_revision_id == pinned_revision_id
    assert result.runtime_revision_id != newer.id
    assert broker.revision_id == str(pinned_revision_id)


def test_persistent_transitional_and_ambiguous_reset_are_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    persistent, revision_id, _ = seed_reset_context(
        session_factory, instance_type=InstanceType.PERSISTENT
    )
    with pytest.raises(PersistentResetUnsupportedError):
        service(session_factory, FakeBroker(persistent, revision_id)).reset(persistent)

    instance_id, revision_id, _ = seed_reset_context(session_factory)
    with pytest.raises(ResetStateConflictError):
        service(
            session_factory,
            FakeBroker(
                instance_id, revision_id, state=ContainerRuntimeState.TRANSITIONAL
            ),
        ).reset(instance_id)
    ambiguous = FakeBroker(instance_id, revision_id)
    ambiguous.state = ContainerRuntimeState.STOPPED
    ambiguous.container_id = "9" * 64
    ambiguous.duplicate_inventory = True
    with pytest.raises(ResetStateConflictError):
        service(session_factory, ambiguous).reset(instance_id)

    unreadable = FakeBroker(instance_id, revision_id)
    unreadable.fail = "inventory"
    with pytest.raises(ResetStateConflictError):
        service(session_factory, unreadable).reset(instance_id)


@pytest.mark.parametrize(
    ("failure", "error_type", "initial"),
    [
        ("stop", ResetStopFailedError, ContainerRuntimeState.RUNNING),
        ("deprovision", ResetDeprovisionFailedError, ContainerRuntimeState.STOPPED),
        ("provision", ResetProvisionFailedError, ContainerRuntimeState.STOPPED),
        ("apply_fixture", FixtureApplyFailedError, None),
    ],
)
def test_destructive_phase_failures_have_typed_outcomes(
    session_factory: sessionmaker[Session],
    failure: str,
    error_type: type[Exception],
    initial: ContainerRuntimeState | None,
) -> None:
    instance_id, revision_id, _ = seed_reset_context(
        session_factory, assigned_fixture=failure == "apply_fixture"
    )
    broker = FakeBroker(instance_id, revision_id, state=initial)
    broker.fail = failure
    with pytest.raises(error_type):
        service(session_factory, broker).reset(instance_id)
    if failure == "provision":
        assert broker.container_id is None
    if failure == "apply_fixture":
        assert broker.container_id is not None
        assert broker.state is ContainerRuntimeState.STOPPED


def test_readiness_and_scan_failures_stop_and_retain_fresh_container(
    session_factory: sessionmaker[Session],
) -> None:
    for readiness, adapter in (
        (FakeReadiness(False), FakeFixtureAdapter()),
        (FakeReadiness(True), FakeFixtureAdapter(fail_scan=True)),
    ):
        instance_id, revision_id, _ = seed_reset_context(
            session_factory, assigned_fixture=True
        )
        broker = FakeBroker(instance_id, revision_id)
        with pytest.raises(FixtureInitializationFailedError):
            service(
                session_factory,
                broker,
                readiness=readiness,
                fixture_adapter=adapter,
            ).reset(instance_id)
        assert broker.container_id is not None
        assert broker.state is ContainerRuntimeState.STOPPED


def test_start_and_final_stop_failures_have_bounded_cleanup_semantics(
    session_factory: sessionmaker[Session],
) -> None:
    instance_id, revision_id, _ = seed_reset_context(
        session_factory, assigned_fixture=True
    )
    start_failure = FakeBroker(instance_id, revision_id)
    start_failure.fail = "start"
    with pytest.raises(FixtureInitializationFailedError):
        service(session_factory, start_failure).reset(instance_id)
    assert start_failure.state is ContainerRuntimeState.STOPPED

    second_instance, second_revision, _ = seed_reset_context(
        session_factory, assigned_fixture=True
    )
    final_stop_failure = FakeBroker(second_instance, second_revision)
    final_stop_failure.fail = "final_stop"
    with pytest.raises(ResetReconciliationRequiredError):
        service(session_factory, final_stop_failure).reset(second_instance)
    assert final_stop_failure.state is ContainerRuntimeState.RUNNING


def test_failed_fixture_cleanup_returns_reconciliation_required(
    session_factory: sessionmaker[Session],
) -> None:
    instance_id, revision_id, _ = seed_reset_context(
        session_factory, assigned_fixture=True
    )
    broker = FakeBroker(instance_id, revision_id)
    broker.stop_leaves_running = True
    with pytest.raises(ResetReconciliationRequiredError):
        service(
            session_factory,
            broker,
            fixture_adapter=FakeFixtureAdapter(fail_scan=True),
        ).reset(instance_id)


def test_reset_lock_rejects_duplicate_and_repeated_reset_is_deterministic(
    session_factory: sessionmaker[Session],
) -> None:
    instance_id, revision_id, _ = seed_reset_context(
        session_factory, assigned_fixture=True
    )
    broker = FakeBroker(instance_id, revision_id)
    locks = PerInstanceResetLocks()
    held = locks.acquire(instance_id)
    assert held is not None
    with pytest.raises(ResetInProgressError):
        service(session_factory, broker, locks=locks).reset(instance_id)
    held.release()

    reset = service(session_factory, broker, locks=locks)
    first = reset.reset(instance_id)
    first_container = broker.container_id
    second = reset.reset(instance_id)
    assert broker.container_id != first_container
    assert first.runtime_revision_id == second.runtime_revision_id == revision_id
    assert first.fixture_state == second.fixture_state


def test_reset_publishes_bounded_success_and_failure_results(
    session_factory: sessionmaker[Session],
) -> None:
    instance_id, revision_id, _ = seed_reset_context(
        session_factory, assigned_fixture=True
    )
    registry = ResetActivityRegistry()
    broker = FakeBroker(instance_id, revision_id)
    service(session_factory, broker, activity=registry).reset(instance_id)
    succeeded = registry.snapshot(instance_id)
    assert succeeded.state is ResetActivityState.SUCCEEDED
    assert succeeded.last_outcome is ResetOutcome.SUCCEEDED

    broker.fail = "apply_fixture"
    with pytest.raises(FixtureApplyFailedError):
        service(session_factory, broker, activity=registry).reset(instance_id)
    failed = registry.snapshot(instance_id)
    assert failed.state is ResetActivityState.FAILED
    assert failed.failure_phase == "fixture_apply"
    assert (
        failed.failure_reason
        == "the trusted assigned fixture archive could not be applied"
    )


def test_reset_publishes_reconciliation_required_result(
    session_factory: sessionmaker[Session],
) -> None:
    instance_id, revision_id, _ = seed_reset_context(
        session_factory, assigned_fixture=True
    )
    registry = ResetActivityRegistry()
    broker = FakeBroker(instance_id, revision_id)
    broker.fail = "final_stop"
    with pytest.raises(ResetReconciliationRequiredError):
        service(session_factory, broker, activity=registry).reset(instance_id)
    status = registry.snapshot(instance_id)
    assert status.state is ResetActivityState.RECONCILIATION_REQUIRED
    assert status.last_outcome is ResetOutcome.RECONCILIATION_REQUIRED
    assert status.reconciliation_required
    assert status.failure_phase == "final_stop"


class RecordingResetActivityRegistry(ResetActivityRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.phases: list[ResetActivityState] = []

    def transition(
        self,
        instance_id: UUID,
        generation: int,
        state: ResetActivityState,
    ) -> ResetActivitySnapshot:
        self.phases.append(state)
        return super().transition(instance_id, generation, state)


def test_fixture_reset_publishes_actual_phase_order(
    session_factory: sessionmaker[Session],
) -> None:
    instance_id, revision_id, _ = seed_reset_context(
        session_factory, assigned_fixture=True
    )
    registry = RecordingResetActivityRegistry()
    broker = FakeBroker(instance_id, revision_id, state=ContainerRuntimeState.RUNNING)
    service(session_factory, broker, activity=registry).reset(instance_id)
    assert registry.phases == [
        ResetActivityState.STOPPING,
        ResetActivityState.DEPROVISIONING,
        ResetActivityState.PROVISIONING,
        ResetActivityState.APPLYING_FIXTURE,
        ResetActivityState.STARTING_FOR_FIXTURE,
        ResetActivityState.WAITING_READINESS,
        ResetActivityState.SCANNING_FIXTURE,
        ResetActivityState.VERIFYING_FIXTURE,
        ResetActivityState.FINAL_STOPPING,
        ResetActivityState.FINAL_VERIFYING,
    ]
