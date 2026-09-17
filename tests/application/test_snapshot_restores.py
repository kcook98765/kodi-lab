from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.snapshot_restore_operations import (
    SnapshotRestoreOperationCoordinator,
)
from kodi_lab.application.snapshot_restores import (
    DisposableSnapshotRestoreService,
    SnapshotRestoreError,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
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
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentOperationPhase,
    DevelopmentSourceDeploymentOperationState,
    DevelopmentSourceDeploymentRequestKind,
    DevelopmentSourceDeploymentResult,
    InstanceRuntimeState,
    Snapshot,
    SnapshotState,
    SnapshotValidationResult,
)
from kodi_lab.kodi.readiness import JsonRpcState, ReadinessResult, UsabilityState
from kodi_lab.snapshot.storage import SnapshotArtifactIdentity, SnapshotStorageError
from kodi_lab.snapshot.validation import SnapshotValidation
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    DevelopmentSourceRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import ManagedInstanceRepository, SnapshotRepository

INSTANCE_ID = UUID("93300000-0000-4000-8000-000000000001")
REVISION_ID = UUID("93300000-0000-4000-8000-000000000002")
DEFINITION_ID = UUID("93300000-0000-4000-8000-000000000003")
SNAPSHOT_ID = UUID("93300000-0000-4000-8000-000000000004")
OLD_ID = "a" * 64
FRESH_ID = "b" * 64
IMAGE_ID = "sha256:" + "c" * 64


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot-restore.db'}")
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
                content_digest="sha256:" + "d" * 64,
                manifest_reference="test",
                manifest_digest="sha256:" + "e" * 64,
                built_at="2026-09-16T00:00:00.000000Z",
                build_log_reference=None,
                approval_state="approved",
                approved_at="2026-09-16T00:00:00.000000Z",
                approval_note=None,
                requires_trusted_build_confirmation=False,
                trusted_build_confirmed_at=None,
            )
        )
        session.add(
            RuntimeExecutionProfileRow(
                runtime_revision_id=str(REVISION_ID),
                runtime_kind="kodi",
                runtime_uid=1000,
                runtime_gid=1000,
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
                poll_interval_seconds=0.1,
                http_timeout_seconds=1.0,
            )
        )
        session.add(
            TargetRow(
                id=str(INSTANCE_ID),
                name="restore source",
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
                id="93300000-0000-4000-8000-000000000020",
                logical_name="Restore source attachment",
                source_kind="local_directory",
                relative_locator="sources/restore",
                addon_id="plugin.video.restore-attachment",
                observed_name="Restore attachment",
                observed_version="1.0.0",
                extension_types_json='["xbmc.python.pluginsource"]',
                registered_at="2026-09-16T00:00:00.000000Z",
                validated_at="2026-09-16T00:00:00.000000Z",
            )
        )
        session.add(
            ManagedInstanceDevelopmentSourceRow(
                managed_instance_id=str(INSTANCE_ID),
                development_source_id="93300000-0000-4000-8000-000000000020",
                attached_at="2026-09-16T00:00:00.000000Z",
            )
        )
        session.add(
            DevelopmentSourceDeploymentOperationRow(
                id="93300000-0000-4000-8000-000000000021",
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                development_source_id="93300000-0000-4000-8000-000000000020",
                source_logical_name="Restore source attachment",
                addon_id="plugin.video.restore-attachment",
                runtime_revision_id=str(REVISION_ID),
                request_kind=DevelopmentSourceDeploymentRequestKind.COPY_SYNC,
                requested_at="2026-09-16T00:00:00.000000Z",
                state=DevelopmentSourceDeploymentOperationState.SUCCEEDED,
                phase=DevelopmentSourceDeploymentOperationPhase.FINALIZE,
                result_classification=DevelopmentSourceDeploymentResult.UNCHANGED,
                new_count=0,
                changed_count=0,
                unchanged_count=1,
                destination_only_count=0,
                transferred_bytes=0,
                expected_container_id=OLD_ID,
                started_at="2026-09-16T00:00:00.000000Z",
                heartbeat_at="2026-09-16T00:00:00.000000Z",
                completed_at="2026-09-16T00:00:01.000000Z",
                failure_reason=None,
                reconciliation_required=False,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        session.add(
            DevelopmentSourceDeploymentRow(
                managed_instance_id=str(INSTANCE_ID),
                development_source_id="93300000-0000-4000-8000-000000000020",
                addon_id="plugin.video.restore-attachment",
                mode="copy",
                last_successful_operation_id=("93300000-0000-4000-8000-000000000021"),
                source_manifest_digest="sha256:" + "4" * 64,
                verified_destination_digest="sha256:" + "5" * 64,
                runtime_revision_id=str(REVISION_ID),
                trusted_container_id=OLD_ID,
                observed_source_name="Restore attachment",
                observed_source_version="1.0.0",
                verified_at="2026-09-16T00:00:01.000000Z",
                confidence=DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION,
            )
        )
        SnapshotRepository(session).create(
            Snapshot(
                id=SNAPSHOT_ID,
                source_managed_instance_id=INSTANCE_ID,
                runtime_revision_id=REVISION_ID,
                captured_fixture_definition_id=None,
                captured_at=datetime(2026, 9, 16, tzinfo=UTC),
                prior_runtime_state=InstanceRuntimeState.STOPPED,
                label="drifted",
                manifest_version=1,
                archive_digest="sha256:" + "1" * 64,
                manifest_digest="sha256:" + "2" * 64,
                archive_size=10240,
                member_count=3,
                state=SnapshotState.AVAILABLE,
                validation_at=datetime(2026, 9, 16, tzinfo=UTC),
                validation_result=SnapshotValidationResult.PASSED,
            )
        )
    yield result
    engine.dispose()


@dataclass
class FakeBroker:
    state: ContainerRuntimeState
    container_id: str | None = OLD_ID
    calls: list[str] = field(default_factory=list)
    fail: str | None = None

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        containers = []
        if self.container_id is not None:
            containers.append(
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(INSTANCE_ID),
                    container_id=self.container_id,
                    image_id=IMAGE_ID,
                    runtime_state=self.state,
                    health_state=ContainerHealthState.NOT_CONFIGURED,
                )
            )
        return ManagedContainerInventoryResult(containers=containers)

    def stop_managed_container(self, **_kwargs: object) -> StopManagedContainerResult:
        self.calls.append("stop")
        if self.fail == "stop":
            raise RuntimeError("stop")
        self.state = ContainerRuntimeState.STOPPED
        assert self.container_id is not None
        return StopManagedContainerResult(
            managed_instance_id=str(INSTANCE_ID),
            container_id=self.container_id,
            runtime_state=self.state,
            stop_performed=True,
        )

    def start_managed_container(self, **_kwargs: object) -> StartManagedContainerResult:
        self.calls.append("start")
        if self.fail == "start":
            raise RuntimeError("start")
        self.state = ContainerRuntimeState.RUNNING
        assert self.container_id is not None
        return StartManagedContainerResult(
            managed_instance_id=str(INSTANCE_ID),
            container_id=self.container_id,
            runtime_state=self.state,
            start_performed=True,
        )

    def deprovision_managed_container(
        self, **_kwargs: object
    ) -> DeprovisionManagedContainerResult:
        self.calls.append("deprovision")
        if self.fail == "deprovision":
            raise RuntimeError("deprovision")
        removed = self.container_id
        assert removed is not None
        self.container_id = None
        return DeprovisionManagedContainerResult(
            managed_instance_id=str(INSTANCE_ID),
            removed_container_id=removed,
            runtime_revision_id=str(REVISION_ID),
            removed=True,
        )

    def provision_managed_container(
        self, **_kwargs: object
    ) -> ProvisionManagedContainerResult:
        self.calls.append("provision")
        if self.fail == "provision":
            raise RuntimeError("provision")
        self.container_id = FRESH_ID
        self.state = ContainerRuntimeState.STOPPED
        return ProvisionManagedContainerResult(
            managed_instance_id=str(INSTANCE_ID),
            runtime_revision_id=str(REVISION_ID),
            container_id=FRESH_ID,
            image_id=IMAGE_ID,
            runtime_state=self.state,
            created=True,
        )

    def apply_managed_instance_snapshot(
        self, *, snapshot_id: str, **_kwargs: object
    ) -> ApplyManagedInstanceSnapshotResult:
        self.calls.append("apply")
        if self.fail == "apply":
            raise RuntimeError("apply")
        return ApplyManagedInstanceSnapshotResult(
            snapshot_id=snapshot_id,
            managed_instance_id=str(INSTANCE_ID),
            runtime_revision_id=str(REVISION_ID),
            container_id=FRESH_ID,
            archive_digest="sha256:" + "1" * 64,
            manifest_digest="sha256:" + "2" * 64,
            archive_size=10240,
            member_count=3,
            recognized_databases_checked=1,
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
    def __init__(self, usable: bool) -> None:
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


def service(
    factory: sessionmaker[Session],
    broker: FakeBroker,
    *,
    usable: bool = True,
    invalid: bool = False,
) -> DisposableSnapshotRestoreService:
    coordinator = SnapshotRestoreOperationCoordinator(factory)
    operation = coordinator.queue(INSTANCE_ID, SNAPSHOT_ID)
    lease = coordinator.acquire(operation.operation_id)
    assert lease is not None

    def validator(**_kwargs: object) -> SnapshotValidation:
        if invalid:
            raise SnapshotStorageError("snapshot_archive_invalid", "Artifact invalid.")
        return SnapshotValidation(
            artifact=SnapshotArtifactIdentity(
                snapshot_id=SNAPSHOT_ID,
                archive_digest="sha256:" + "1" * 64,
                manifest_digest="sha256:" + "2" * 64,
                archive_size=10240,
                member_count=3,
            ),
            recognized_databases_checked=1,
        )

    return DisposableSnapshotRestoreService(
        factory,
        broker,
        coordinator,
        lease,
        data_root=Path("/unused"),
        owner_gid=os.getgid(),
        readiness_factory=lambda *_args: Readiness(usable),
        validator=validator,
    )


def test_stopped_restore_uses_fresh_container_and_remains_stopped(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker(ContainerRuntimeState.STOPPED)
    original_deprovision = broker.deprovision_managed_container

    def prove_stale_before_deprovision(
        **kwargs: object,
    ) -> DeprovisionManagedContainerResult:
        with factory() as session:
            evidence = session.get(
                DevelopmentSourceDeploymentRow,
                (str(INSTANCE_ID), "93300000-0000-4000-8000-000000000020"),
            )
            assert evidence is not None
            assert evidence.confidence is DevelopmentSourceDeploymentConfidence.STALE
        return original_deprovision(**kwargs)

    broker.deprovision_managed_container = prove_stale_before_deprovision  # type: ignore[method-assign]
    result = service(factory, broker).restore()
    assert broker.calls == ["deprovision", "provision", "apply"]
    assert result.old_container_id == OLD_ID
    assert result.fresh_container_id == FRESH_ID
    assert result.final_runtime_state is ContainerRuntimeState.STOPPED
    with factory() as session:
        instance = ManagedInstanceRepository(session).get(INSTANCE_ID)
        snapshot = SnapshotRepository(session).get(SNAPSHOT_ID)
        attachment = session.get(
            ManagedInstanceDevelopmentSourceRow,
            (str(INSTANCE_ID), "93300000-0000-4000-8000-000000000020"),
        )
        evidence = session.get(
            DevelopmentSourceDeploymentRow,
            (str(INSTANCE_ID), "93300000-0000-4000-8000-000000000020"),
        )
    assert instance is not None and instance.runtime_revision_id == REVISION_ID
    assert snapshot is not None and snapshot.state is SnapshotState.AVAILABLE
    assert attachment is not None
    assert evidence is not None
    assert evidence.confidence is DevelopmentSourceDeploymentConfidence.STALE


def test_running_restore_stops_replaces_starts_and_waits_readiness(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker(ContainerRuntimeState.RUNNING)
    result = service(factory, broker).restore()
    assert broker.calls == ["stop", "deprovision", "provision", "apply", "start"]
    assert result.prior_runtime_state is InstanceRuntimeState.RUNNING
    assert result.final_runtime_state is ContainerRuntimeState.RUNNING


def test_invalid_artifact_fails_before_any_lifecycle_mutation(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker(ContainerRuntimeState.RUNNING)
    with pytest.raises(SnapshotRestoreError, match="Artifact invalid"):
        service(factory, broker, invalid=True).restore()
    assert broker.calls == []
    assert broker.container_id == OLD_ID


@pytest.mark.parametrize("failure", ["provision", "apply"])
def test_failure_after_old_removal_requires_reconciliation(
    factory: sessionmaker[Session], failure: str
) -> None:
    broker = FakeBroker(ContainerRuntimeState.STOPPED, fail=failure)
    with pytest.raises(SnapshotRestoreError) as captured:
        service(factory, broker).restore()
    assert captured.value.reconciliation_required
    assert "deprovision" in broker.calls


def test_failure_before_deprovision_recovers_original_running_state(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker(ContainerRuntimeState.RUNNING, fail="deprovision")
    with pytest.raises(SnapshotRestoreError) as captured:
        service(factory, broker).restore()
    assert not captured.value.reconciliation_required
    assert broker.calls == ["stop", "deprovision", "start"]
    assert broker.container_id == OLD_ID
    assert broker.state is ContainerRuntimeState.RUNNING


def test_readiness_failure_retains_structurally_restored_fresh_container_stopped(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker(ContainerRuntimeState.RUNNING)
    with pytest.raises(SnapshotRestoreError) as captured:
        service(factory, broker, usable=False).restore()
    assert captured.value.reconciliation_required
    assert broker.container_id == FRESH_ID
    assert broker.state is ContainerRuntimeState.STOPPED
