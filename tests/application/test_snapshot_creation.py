from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.snapshot_operations import (
    SnapshotCreateOperationCoordinator,
)
from kodi_lab.application.snapshots import (
    DisposableSnapshotCreationService,
    SnapshotCreateError,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.operations import (
    CaptureManagedInstanceSnapshotResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.domain import SnapshotState, SnapshotValidationResult
from kodi_lab.kodi.readiness import JsonRpcState, ReadinessResult, UsabilityState
from kodi_lab.snapshot.storage import SnapshotArtifactIdentity, SnapshotStorageError
from kodi_lab.snapshot.validation import SnapshotValidation
from kodi_lab.storage.engine import create_session_factory
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
from kodi_lab.storage.repositories import ManagedInstanceRepository, SnapshotRepository

INSTANCE_ID = UUID("93000000-0000-4000-8000-000000000001")
REVISION_ID = UUID("93000000-0000-4000-8000-000000000002")
DEFINITION_ID = UUID("93000000-0000-4000-8000-000000000003")
CONTAINER_ID = "a" * 64
IMAGE_ID = "sha256:" + "b" * 64


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot-create.db'}")
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
                revision_number=3,
                source_kind="test",
                source_reference="test",
                content_digest="sha256:" + "c" * 64,
                manifest_reference="test",
                manifest_digest="sha256:" + "d" * 64,
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
            RuntimeExecutionProfileRow(
                runtime_revision_id=str(REVISION_ID),
                runtime_kind="kodi",
                expected_application_name="Kodi",
                expected_kodi_version="21.3",
                expected_version_tag="stable",
                expected_prerelease_ordinal=None,
                jsonrpc_port=8080,
                jsonrpc_path="/jsonrpc",
                process_observation="docker_healthcheck",
                readiness_probe="kodi_jsonrpc",
                readiness_timeout_seconds=60,
                poll_interval_seconds=0.1,
                http_timeout_seconds=1.0,
            )
        )
        session.add(
            TargetRow(
                id=str(INSTANCE_ID),
                name="snapshot source",
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
            DevelopmentSourceRow(
                id="93000000-0000-4000-8000-000000000020",
                logical_name="Snapshot source attachment",
                source_kind="local_directory",
                relative_locator="sources/snapshot",
                addon_id="plugin.video.snapshot-attachment",
                observed_name="Snapshot attachment",
                observed_version="1.0.0",
                extension_types_json='["xbmc.python.pluginsource"]',
                registered_at="2026-09-15T00:00:00.000000Z",
                validated_at="2026-09-15T00:00:00.000000Z",
            )
        )
        session.add(
            ManagedInstanceDevelopmentSourceRow(
                managed_instance_id=str(INSTANCE_ID),
                development_source_id="93000000-0000-4000-8000-000000000020",
                attached_at="2026-09-15T00:00:00.000000Z",
            )
        )
    yield result
    engine.dispose()


@dataclass
class FakeBroker:
    state: ContainerRuntimeState
    calls: list[str] = field(default_factory=list)
    fail: str | None = None

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(INSTANCE_ID),
                    container_id=CONTAINER_ID,
                    image_id=IMAGE_ID,
                    runtime_state=self.state,
                    health_state=(
                        ContainerHealthState.HEALTHY
                        if self.state is ContainerRuntimeState.RUNNING
                        else ContainerHealthState.NOT_CONFIGURED
                    ),
                )
            ]
        )

    def stop_managed_container(self, **_kwargs: object) -> StopManagedContainerResult:
        self.calls.append("stop")
        if self.fail == "stop":
            raise RuntimeError("stop")
        if self.state is not ContainerRuntimeState.RUNNING:
            raise RuntimeError("wrong stop state")
        self.state = ContainerRuntimeState.STOPPED
        return StopManagedContainerResult(
            managed_instance_id=str(INSTANCE_ID),
            container_id=CONTAINER_ID,
            runtime_state=self.state,
            stop_performed=True,
        )

    def start_managed_container(self, **_kwargs: object) -> StartManagedContainerResult:
        self.calls.append("start")
        if self.fail == "start":
            raise RuntimeError("start")
        if self.state is not ContainerRuntimeState.STOPPED:
            raise RuntimeError("wrong start state")
        self.state = ContainerRuntimeState.RUNNING
        return StartManagedContainerResult(
            managed_instance_id=str(INSTANCE_ID),
            container_id=CONTAINER_ID,
            runtime_state=self.state,
            start_performed=True,
        )

    def capture_managed_instance_snapshot(
        self, *, snapshot_id: str, **_kwargs: object
    ) -> CaptureManagedInstanceSnapshotResult:
        self.calls.append("capture")
        if self.fail == "capture":
            raise RuntimeError("capture")
        assert self.state is ContainerRuntimeState.STOPPED
        return CaptureManagedInstanceSnapshotResult(
            snapshot_id=snapshot_id,
            managed_instance_id=str(INSTANCE_ID),
            runtime_revision_id=str(REVISION_ID),
            container_id=CONTAINER_ID,
            archive_digest="sha256:" + "1" * 64,
            manifest_digest="sha256:" + "2" * 64,
            archive_size=10240,
            member_count=3,
        )

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        return ObserveManagedKodiRuntimeResult(
            managed_instance_id=managed_instance_id,
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            runtime_state=ContainerRuntimeState.RUNNING,
            process_state=KodiProcessState.RUNNING,
            ipv4_address="10.250.0.2",
            jsonrpc_port=8080,
        )


class Readiness:
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
            elapsed_seconds=0.1,
            attempts=1,
        )


def validation(snapshot_id: UUID) -> SnapshotValidation:
    return SnapshotValidation(
        artifact=SnapshotArtifactIdentity(
            snapshot_id=snapshot_id,
            archive_digest="sha256:" + "1" * 64,
            manifest_digest="sha256:" + "2" * 64,
            archive_size=10240,
            member_count=3,
        ),
        recognized_databases_checked=1,
    )


def service(
    factory: sessionmaker[Session],
    broker: FakeBroker,
    *,
    usable: bool = True,
    invalid: bool = False,
) -> tuple[DisposableSnapshotCreationService, SnapshotCreateOperationCoordinator]:
    coordinator = SnapshotCreateOperationCoordinator(factory)
    operation = coordinator.queue(INSTANCE_ID, label="test").operation
    lease = coordinator.acquire(operation.operation_id)
    assert lease is not None

    def validator(**_kwargs: object) -> SnapshotValidation:
        if invalid:
            raise SnapshotStorageError(
                "snapshot_database_invalid", "Recognized database is invalid."
            )
        return validation(lease.snapshot_id)

    return (
        DisposableSnapshotCreationService(
            factory,
            broker,
            coordinator,
            lease,
            data_root=Path("/unused"),
            owner_gid=os.getgid(),
            readiness_factory=lambda *_args: Readiness(usable),
            validator=validator,
        ),
        coordinator,
    )


def test_initially_stopped_capture_validates_and_remains_stopped(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker(ContainerRuntimeState.STOPPED)
    capture, coordinator = service(factory, broker)
    result = capture.create()
    coordinator.finish_success(capture._lease)
    assert broker.calls == ["capture"]
    assert result.final_runtime_state is ContainerRuntimeState.STOPPED
    assert result.recognized_databases_checked == 1
    with factory() as session:
        snapshot = SnapshotRepository(session).get(result.snapshot_id)
        attachment = session.get(
            ManagedInstanceDevelopmentSourceRow,
            (str(INSTANCE_ID), "93000000-0000-4000-8000-000000000020"),
        )
        assert snapshot is not None and snapshot.state is SnapshotState.AVAILABLE
        assert snapshot.validation_result is SnapshotValidationResult.PASSED
        assert attachment is not None


def test_initially_running_stops_captures_restarts_and_requires_readiness(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker(ContainerRuntimeState.RUNNING)
    capture, coordinator = service(factory, broker)
    result = capture.create()
    coordinator.finish_success(capture._lease)
    assert broker.calls == ["stop", "capture", "start"]
    assert result.final_runtime_state is ContainerRuntimeState.RUNNING


def test_capture_failure_restores_prior_running_state(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker(ContainerRuntimeState.RUNNING, fail="capture")
    capture, _coordinator = service(factory, broker)
    with pytest.raises(SnapshotCreateError) as captured:
        capture.create()
    assert not captured.value.reconciliation_required
    assert broker.calls == ["stop", "capture", "start"]
    assert broker.state is ContainerRuntimeState.RUNNING


def test_restart_failure_after_validation_leaves_snapshot_available(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker(ContainerRuntimeState.RUNNING, fail="start")
    capture, _coordinator = service(factory, broker)
    with pytest.raises(SnapshotCreateError) as captured:
        capture.create()
    assert captured.value.reconciliation_required
    with factory() as session:
        snapshot = SnapshotRepository(session).get(capture._lease.snapshot_id)
        assert snapshot is not None and snapshot.state is SnapshotState.AVAILABLE


def test_validation_failure_does_not_change_runtime_or_fixture_pins(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker(ContainerRuntimeState.STOPPED)
    capture, _coordinator = service(factory, broker, invalid=True)
    with factory() as session:
        before = ManagedInstanceRepository(session).get(INSTANCE_ID)
    with pytest.raises(SnapshotCreateError) as captured:
        capture.create()
    assert captured.value.quarantine_artifact
    with factory() as session:
        after = ManagedInstanceRepository(session).get(INSTANCE_ID)
    assert after == before
