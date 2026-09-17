from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_deployments import (
    DEPLOYMENT_LEASE_DURATION,
    DevelopmentSourceDeploymentConflictError,
    DevelopmentSourceDeploymentError,
    DevelopmentSourceDeploymentOperationCoordinator,
    DevelopmentSourceDeploymentService,
)
from kodi_lab.application.reconciliation import (
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.broker.development_sources import (
    DevelopmentSourceDeploymentStateError,
    SqliteDevelopmentSourceDeploymentResolver,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
)
from kodi_lab.broker.errors import DevelopmentSourceDeploymentClientError
from kodi_lab.broker.operations import (
    CopySyncAttachedDevelopmentSourceResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.domain import (
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentOperationState,
    GlobalHeavyOperationKind,
    ManagedInstanceMutationKind,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    DevelopmentSourceRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceDeploymentOperationRepository,
    DevelopmentSourceDeploymentRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
)

INSTANCE_ID = UUID("a8100000-0000-4000-8000-000000000001")
SECOND_INSTANCE_ID = UUID("a8100000-0000-4000-8000-000000000002")
SOURCE_ID = UUID("a8100000-0000-4000-8000-000000000011")
REVISION_ID = UUID("a8100000-0000-4000-8000-000000000021")
DEFINITION_ID = UUID("a8100000-0000-4000-8000-000000000022")
CONTAINER_ID = "b" * 64
NOW = datetime(2026, 9, 16, 22, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class FakeBroker:
    def __init__(self) -> None:
        self.error_code: str | None = None
        self.calls: list[tuple[str, str, str, str]] = []

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(INSTANCE_ID),
                    container_id=CONTAINER_ID,
                    image_id="sha256:" + "c" * 64,
                    runtime_state=ContainerRuntimeState.STOPPED,
                    health_state=ContainerHealthState.NOT_CONFIGURED,
                ),
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(SECOND_INSTANCE_ID),
                    container_id="d" * 64,
                    image_id="sha256:" + "e" * 64,
                    runtime_state=ContainerRuntimeState.STOPPED,
                    health_state=ContainerHealthState.NOT_CONFIGURED,
                ),
            ]
        )

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        raise AssertionError("deployment reconciliation has no repair observation")

    def copy_sync_attached_development_source(
        self,
        *,
        managed_instance_id: str,
        development_source_id: str,
        expected_container_id: str,
        deployment_operation_id: str,
    ) -> CopySyncAttachedDevelopmentSourceResult:
        self.calls.append(
            (
                managed_instance_id,
                development_source_id,
                expected_container_id,
                deployment_operation_id,
            )
        )
        if self.error_code is not None:
            raise DevelopmentSourceDeploymentClientError(
                self.error_code,
                "typed failure",
                request_id=deployment_operation_id,
            )
        return CopySyncAttachedDevelopmentSourceResult(
            managed_instance_id=managed_instance_id,
            development_source_id=development_source_id,
            deployment_operation_id=deployment_operation_id,
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            result_classification="synchronized",
            source_manifest_digest="sha256:" + "1" * 64,
            verified_destination_digest="sha256:" + "2" * 64,
            observed_source_name="Example",
            observed_source_version="1.0.0",
            new_count=1,
            changed_count=1,
            unchanged_count=2,
            destination_only_count=1,
            transferred_bytes=8,
        )


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'deployments.db'}")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    with result.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="deployment-runtime", kodi_version="21.3"
            )
        )
        session.flush()
        session.add(
            RuntimeRevisionRow(
                id=str(REVISION_ID),
                runtime_definition_id=str(DEFINITION_ID),
                revision_number=1,
                source_kind="test",
                source_reference="test",
                content_digest="sha256:" + "a" * 64,
                manifest_reference="test",
                manifest_digest="sha256:" + "b" * 64,
                built_at=NOW.isoformat(),
                approval_state="approved",
                approved_at=NOW.isoformat(),
                requires_trusted_build_confirmation=False,
            )
        )
        session.flush()
        session.add(
            RuntimeExecutionProfileRow(
                runtime_revision_id=str(REVISION_ID),
                runtime_kind="kodi",
                runtime_uid=2468,
                runtime_gid=1357,
                python_bytecode_policy="interpreter_default",
                expected_application_name="Kodi",
                expected_kodi_version="21.3",
                expected_version_tag="stable",
                expected_prerelease_ordinal=None,
                jsonrpc_port=8080,
                jsonrpc_path="/jsonrpc",
                process_observation="docker_healthcheck",
                readiness_probe="kodi_jsonrpc",
                readiness_timeout_seconds=60,
                poll_interval_seconds=1.0,
                http_timeout_seconds=1.0,
            )
        )
        for instance_id in (INSTANCE_ID, SECOND_INSTANCE_ID):
            session.add(
                TargetRow(
                    id=str(instance_id),
                    name=f"instance-{instance_id}",
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
                    target_id=str(instance_id),
                    instance_type="disposable",
                    runtime_revision_id=str(REVISION_ID),
                    runtime_state="stopped",
                    start_on_host_boot=False,
                )
            )
        session.flush()
        session.add(
            DevelopmentSourceRow(
                id=str(SOURCE_ID),
                logical_name="Example source",
                source_kind="local_directory",
                relative_locator="source",
                addon_id="plugin.video.example",
                observed_name="Example",
                observed_version="1.0.0",
                extension_types_json='["xbmc.python.pluginsource"]',
                registered_at=NOW.isoformat(),
                validated_at=NOW.isoformat(),
            )
        )
        session.flush()
        for instance_id in (INSTANCE_ID, SECOND_INSTANCE_ID):
            session.add(
                ManagedInstanceDevelopmentSourceRow(
                    managed_instance_id=str(instance_id),
                    development_source_id=str(SOURCE_ID),
                    attached_at=NOW.isoformat(),
                )
            )
    source = tmp_path / "source"
    source.mkdir()
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.example" name="Example" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource"/></addon>'
    )
    yield result
    engine.dispose()


def _coordinator(
    factory: sessionmaker[Session], root: Path, broker: FakeBroker, clock: Clock
) -> DevelopmentSourceDeploymentOperationCoordinator:
    return DevelopmentSourceDeploymentOperationCoordinator(
        factory, broker, DevelopmentSourceValidator(root), clock=clock
    )


def _successful_deployment(
    factory: sessionmaker[Session], root: Path, broker: FakeBroker, clock: Clock
) -> tuple[DevelopmentSourceDeploymentOperationCoordinator, UUID]:
    coordinator = _coordinator(factory, root, broker, clock)
    queued = coordinator.queue(INSTANCE_ID, SOURCE_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    return coordinator, queued.operation_id


def test_success_persists_distinct_source_and_full_destination_evidence(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    broker = FakeBroker()
    clock = Clock()
    coordinator, operation_id = _successful_deployment(factory, tmp_path, broker, clock)
    operation = coordinator.get(operation_id)
    with factory() as session:
        evidence = DevelopmentSourceDeploymentRepository(session).get(
            INSTANCE_ID, SOURCE_ID
        )
        mutation = ManagedInstanceMutationLeaseRepository(session).current(INSTANCE_ID)
        heavy = GlobalHeavyOperationLeaseRepository(session).current()
    assert operation is not None
    assert operation.state is DevelopmentSourceDeploymentOperationState.SUCCEEDED
    assert evidence is not None
    assert evidence.source_manifest_digest == "sha256:" + "1" * 64
    assert evidence.verified_destination_digest == "sha256:" + "2" * 64
    assert evidence.last_successful_operation_id == operation_id
    assert evidence.confidence is (
        DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
    )
    assert mutation is None
    assert heavy is None


def test_broker_resolver_derives_synthetic_runtime_ownership_and_rejects_corruption(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    broker = FakeBroker()
    coordinator = _coordinator(factory, tmp_path, broker, Clock())
    queued = coordinator.queue(INSTANCE_ID, SOURCE_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    resolver = SqliteDevelopmentSourceDeploymentResolver(
        factory, development_root=tmp_path, data_root=tmp_path / "data"
    )
    resolved = resolver.resolve(
        str(INSTANCE_ID), str(SOURCE_ID), str(queued.operation_id), CONTAINER_ID
    )
    assert (resolved.runtime_uid, resolved.runtime_gid) == (2468, 1357)
    assert resolved.relative_locator == "source"

    with factory.begin() as session:
        session.execute(text("PRAGMA ignore_check_constraints = ON"))
        session.execute(
            text(
                "UPDATE development_sources SET relative_locator = '../escape' "
                "WHERE id = :source_id"
            ),
            {"source_id": str(SOURCE_ID)},
        )
        session.execute(text("PRAGMA ignore_check_constraints = OFF"))
    with pytest.raises(DevelopmentSourceDeploymentStateError):
        resolver.resolve(
            str(INSTANCE_ID), str(SOURCE_ID), str(queued.operation_id), CONTAINER_ID
        )


def test_destination_verification_resolver_derives_all_sensitive_authority(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    _successful_deployment(factory, tmp_path, FakeBroker(), Clock())
    resolver = SqliteDevelopmentSourceDeploymentResolver(
        factory, development_root=tmp_path, data_root=tmp_path / "data"
    )
    resolved = resolver.resolve_destination_verification(
        str(INSTANCE_ID), str(SOURCE_ID), CONTAINER_ID
    )
    assert resolved.addon_id == "plugin.video.example"
    assert resolved.addon_version == "1.0.0"
    assert resolved.verified_destination_digest == "sha256:" + "2" * 64
    assert (resolved.runtime_uid, resolved.runtime_gid) == (2468, 1357)
    with pytest.raises(DevelopmentSourceDeploymentStateError):
        resolver.resolve_destination_verification(
            str(INSTANCE_ID), str(SOURCE_ID), "0" * 64
        )


def test_queue_always_reserves_instance_and_global_heavy_authority(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    broker = FakeBroker()
    coordinator = _coordinator(factory, tmp_path, broker, Clock())
    queued = coordinator.queue(INSTANCE_ID, SOURCE_ID)
    with factory() as session:
        mutation = ManagedInstanceMutationLeaseRepository(session).current(INSTANCE_ID)
        heavy = GlobalHeavyOperationLeaseRepository(session).current()
    assert mutation is not None
    assert mutation.operation_kind is (
        ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_DEPLOYMENT
    )
    assert mutation.operation_id == str(queued.operation_id)
    assert heavy is not None
    assert heavy.operation_kind is (
        GlobalHeavyOperationKind.DEVELOPMENT_SOURCE_DEPLOYMENT
    )
    assert heavy.operation_id == str(queued.operation_id)
    with pytest.raises(DevelopmentSourceDeploymentConflictError):
        coordinator.queue(SECOND_INSTANCE_ID, SOURCE_ID)


@pytest.mark.parametrize(
    ("code", "confidence", "state"),
    [
        (
            "development_source_replacement_conflict",
            DevelopmentSourceDeploymentConfidence.STALE,
            DevelopmentSourceDeploymentOperationState.FAILED,
        ),
        (
            "development_source_reconciliation_required",
            DevelopmentSourceDeploymentConfidence.UNKNOWN,
            DevelopmentSourceDeploymentOperationState.RECONCILIATION_REQUIRED,
        ),
    ],
)
def test_failure_transitions_confidence_and_releases_exact_leases(
    factory: sessionmaker[Session],
    tmp_path: Path,
    code: str,
    confidence: DevelopmentSourceDeploymentConfidence,
    state: DevelopmentSourceDeploymentOperationState,
) -> None:
    broker = FakeBroker()
    clock = Clock()
    coordinator, _ = _successful_deployment(factory, tmp_path, broker, clock)
    queued = coordinator.queue(INSTANCE_ID, SOURCE_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    broker.error_code = code
    with pytest.raises(DevelopmentSourceDeploymentError):
        DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    operation = coordinator.get(queued.operation_id)
    with factory() as session:
        evidence = DevelopmentSourceDeploymentRepository(session).get(
            INSTANCE_ID, SOURCE_ID
        )
        mutation = ManagedInstanceMutationLeaseRepository(session).current(INSTANCE_ID)
        heavy = GlobalHeavyOperationLeaseRepository(session).current()
    assert operation is not None and operation.state is state
    assert evidence is not None and evidence.confidence is confidence
    assert mutation is None and heavy is None


def test_expired_deployment_never_resumes_and_releases_both_exact_leases(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    broker = FakeBroker()
    clock = Clock()
    coordinator, _ = _successful_deployment(factory, tmp_path, broker, clock)
    queued = coordinator.queue(INSTANCE_ID, SOURCE_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    clock.now += DEPLOYMENT_LEASE_DURATION + timedelta(seconds=1)
    dispatcher = StaleOperationReconciliationDispatcher(
        factory, clock=clock, broker=broker
    )
    result = dispatcher.sweep_instance(INSTANCE_ID)
    operation = coordinator.get(queued.operation_id)
    with factory() as session:
        evidence = DevelopmentSourceDeploymentRepository(session).get(
            INSTANCE_ID, SOURCE_ID
        )
        mutation = ManagedInstanceMutationLeaseRepository(session).current(INSTANCE_ID)
        heavy = GlobalHeavyOperationLeaseRepository(session).current()
        retained = DevelopmentSourceDeploymentOperationRepository(session).get(
            queued.operation_id
        )
    assert result.terminalized_operations == [queued.operation_id]
    assert operation is not None
    assert operation.state is (
        DevelopmentSourceDeploymentOperationState.RECONCILIATION_REQUIRED
    )
    assert retained is not None and retained.reconciliation_required
    assert evidence is not None
    assert evidence.confidence is DevelopmentSourceDeploymentConfidence.UNKNOWN
    assert mutation is None and heavy is None
    assert len(broker.calls) == 1
