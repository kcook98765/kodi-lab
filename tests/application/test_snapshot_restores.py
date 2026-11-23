from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.snapshot_restore_operations import (
    SnapshotRestoreLease,
    SnapshotRestoreLeaseLostError,
    SnapshotRestoreOperationCoordinator,
    SnapshotRestoreUnsupportedError,
)
from kodi_lab.application.snapshot_restores import (
    DisposableSnapshotRestoreService,
    SnapshotRestoreError,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
    ManagedContainerRecord,
    ProvisionedManagedContainerRecord,
    StartManagedContainerRecord,
)
from kodi_lab.broker.errors import ClientTransportError, RemoteBrokerError
from kodi_lab.broker.managed_capacity import (
    ManagedCapacityAdmission,
    ManagedCapacityError,
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
    GlobalHeavyOperationKind,
    InstanceRuntimeState,
    Snapshot,
    SnapshotRestoreOperationPhase,
    SnapshotState,
    SnapshotValidationResult,
)
from kodi_lab.kodi.readiness import JsonRpcState, ReadinessResult, UsabilityState
from kodi_lab.runtime.recipes import load_installation_recipe, recipe_target_id
from kodi_lab.snapshot.storage import SnapshotArtifactIdentity, SnapshotStorageError
from kodi_lab.snapshot.validation import SnapshotValidation
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    DevelopmentSourceRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedProvisionReservationRow,
    RuntimeContainerArtifactRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import ManagedInstanceRepository, SnapshotRepository

INSTANCE_ID = recipe_target_id(load_installation_recipe("omega"))
REVISION_ID = UUID("93300000-0000-4000-8000-000000000002")
DEFINITION_ID = UUID("93300000-0000-4000-8000-000000000003")
SNAPSHOT_ID = UUID("93300000-0000-4000-8000-000000000004")
OLD_ID = "a" * 64
FRESH_ID = "b" * 64
IMAGE_ID = "sha256:" + "c" * 64
SOURCE_ID = "93300000-0000-4000-8000-000000000020"
WORKSPACE_ID = "93300000-0000-4000-8000-000000000030"


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
            RuntimeContainerArtifactRow(
                runtime_revision_id=str(REVISION_ID), image_id=IMAGE_ID
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
    sessions: sessionmaker[Session] | None = None

    def records(self) -> tuple[ManagedContainerRecord, ...]:
        return tuple(
            ManagedContainerRecord(
                item.managed_instance_id,
                item.container_id,
                item.image_id,
                item.runtime_state,
                item.health_state,
                item.runtime_revision_id,
            )
            for item in self.managed_container_inventory().containers
        )

    def admission(self) -> ManagedCapacityAdmission:
        assert self.sessions is not None
        return ManagedCapacityAdmission(self.sessions, self.records)

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
                    runtime_revision_id=str(REVISION_ID),
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
        def start() -> StartManagedContainerRecord:
            self.calls.append("start")
            if self.fail == "start":
                raise RuntimeError("start")
            self.state = ContainerRuntimeState.RUNNING
            assert self.container_id is not None
            return StartManagedContainerRecord(
                str(INSTANCE_ID), self.container_id, self.state, True
            )

        assert self.container_id is not None
        result = self.admission().run_start(
            instance_id=str(INSTANCE_ID),
            container_id=self.container_id,
            revision_id=str(REVISION_ID),
            dispatch=start,
        )
        return StartManagedContainerResult.model_validate(asdict(result))

    def deprovision_managed_container(
        self, **_kwargs: object
    ) -> DeprovisionManagedContainerResult:
        self.calls.append("deprovision")
        if self.fail == "deprovision":
            raise RemoteBrokerError(
                "container_must_be_stopped",
                "fixture pre-dispatch refusal",
                request_id=None,
            )
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
        def create() -> ProvisionedManagedContainerRecord:
            self.calls.append("provision")
            if self.fail == "provision":
                raise RuntimeError("provision")
            self.container_id = FRESH_ID
            self.state = ContainerRuntimeState.STOPPED
            if self.fail == "unknown_create":
                raise ClientTransportError("fixture lost create reply")
            return ProvisionedManagedContainerRecord(
                str(INSTANCE_ID),
                str(REVISION_ID),
                FRESH_ID,
                IMAGE_ID,
                self.state,
                True,
            )

        result = self.admission().run_provision(
            instance_id=str(INSTANCE_ID),
            revision_id=str(REVISION_ID),
            dispatch=create,
        )
        return ProvisionManagedContainerResult.model_validate(asdict(result))

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
    broker.sessions = factory
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


def test_lost_stop_response_requires_reconciliation_without_recovery_start(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker(ContainerRuntimeState.RUNNING)

    def lose_stop_response(**_kwargs: object) -> StopManagedContainerResult:
        broker.calls.append("stop")
        raise ClientTransportError("Broker Stop response lost")

    broker.stop_managed_container = lose_stop_response  # type: ignore[method-assign]
    with pytest.raises(SnapshotRestoreError) as captured:
        service(factory, broker).restore()
    assert captured.value.reconciliation_required
    assert captured.value.safe_reason == "broker_runtime_dispatch_unconfirmed"
    assert broker.calls == ["stop"]


def test_readiness_failure_retains_structurally_restored_fresh_container_stopped(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker(ContainerRuntimeState.RUNNING)
    with pytest.raises(SnapshotRestoreError) as captured:
        service(factory, broker, usable=False).restore()
    assert captured.value.reconciliation_required
    assert broker.container_id == FRESH_ID
    assert broker.state is ContainerRuntimeState.STOPPED


def select_session(
    factory: sessionmaker[Session], *, retained: bool = True, generation: int = 55
) -> None:
    now = "2026-09-16T00:00:00.000000Z"
    with factory.begin() as session:
        session.add(
            AgentWorkspaceRow(
                id=WORKSPACE_ID,
                development_source_id=SOURCE_ID,
                source_addon_id="plugin.video.restore-attachment",
                source_logical_name="restore",
                source_observed_name="restore",
                source_observed_version="1.0.0",
                state="ready",
                bytes_state="present",
                baseline_artifact_id=str(uuid4()),
                baseline_manifest_digest="sha256:" + "1" * 64,
                current_tree_digest="sha256:" + "2" * 64,
                capture_started_at=now,
                captured_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            DevelopmentSessionRow(
                generation=generation,
                request_id=str(uuid4()),
                target_id=str(INSTANCE_ID),
                runtime_revision_id=str(REVISION_ID),
                workspace_id=WORKSPACE_ID,
                source_id=SOURCE_ID,
                container_id=OLD_ID,
                phase="bound",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            DevelopmentSessionPolicyRow(
                id=1,
                active_generation=generation,
                retain_profiles=retained,
                retained_omega_target_id=str(INSTANCE_ID) if retained else None,
            )
        )


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize(
    "state", [ContainerRuntimeState.STOPPED, ContainerRuntimeState.RUNNING]
)
def test_selected_restore_hands_off_history_through_real_capacity(
    factory: sessionmaker[Session],
    retained: bool,
    state: ContainerRuntimeState,
) -> None:
    select_session(factory, retained=retained)
    broker = FakeBroker(state)
    restore = service(factory, broker)
    result = restore.restore()
    restore._coordinator.finish_success(restore._lease)
    assert result.final_runtime_state is state
    assert broker.calls == (
        ["stop"] if state is ContainerRuntimeState.RUNNING else []
    ) + ["deprovision", "provision", "apply"] + (
        ["start"] if state is ContainerRuntimeState.RUNNING else []
    )
    with factory() as session:
        old = session.get(DevelopmentSessionRow, 55)
        new = session.get(DevelopmentSessionRow, 56)
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        assert old is not None and new is not None and policy is not None
        assert (old.phase, old.container_id, old.workspace_id, old.source_id) == (
            "closed",
            OLD_ID,
            WORKSPACE_ID,
            SOURCE_ID,
        )
        assert (new.phase, new.container_id, new.workspace_id, new.source_id) == (
            "bound",
            FRESH_ID,
            WORKSPACE_ID,
            SOURCE_ID,
        )
        assert policy.active_generation == 56
        assert session.scalar(select(ManagedProvisionReservationRow)) is None
        evidence = session.get(
            DevelopmentSourceDeploymentRow, (str(INSTANCE_ID), SOURCE_ID)
        )
        assert (
            evidence is not None
            and evidence.confidence is DevelopmentSourceDeploymentConfidence.STALE
        )
    calls = broker.calls[:]
    with pytest.raises(SnapshotRestoreLeaseLostError):
        restore.restore()
    assert broker.calls == calls


@pytest.mark.parametrize(
    "change",
    [
        "generation",
        "workspace",
        "workspace_generation",
        "source",
        "source_locator",
        "policy",
        "revision",
        "fixture",
        "image",
        "mutation_token",
        "heavy_kind",
        "expiry",
        "cid",
    ],
)
@pytest.mark.parametrize(
    "state", [ContainerRuntimeState.STOPPED, ContainerRuntimeState.RUNNING]
)
def test_change_after_admission_refuses_destructive_dispatch(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    state: ContainerRuntimeState,
) -> None:
    select_session(factory)
    broker = FakeBroker(state)
    restore = service(factory, broker)
    update = restore._coordinator.update_phase

    def change_before_removal(
        lease: SnapshotRestoreLease,
        phase: SnapshotRestoreOperationPhase,
        *,
        expected_phase: SnapshotRestoreOperationPhase | None = None,
    ) -> None:
        # Keep the production phase/lease renewal, then simulate identity drift.
        update(lease, phase, expected_phase=expected_phase)
        boundary = (
            "stopping" if state is ContainerRuntimeState.RUNNING else "deprovisioning"
        )
        if phase != boundary:
            return
        with factory.begin() as session:
            current = session.get(DevelopmentSessionRow, 55)
            policy = session.get(DevelopmentSessionPolicyRow, 1)
            instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
            assert current is not None and policy is not None and instance is not None
            if change == "generation":
                policy.active_generation = None
            elif change == "workspace":
                current.workspace_id = str(uuid4())
            elif change == "workspace_generation":
                workspace = session.get(AgentWorkspaceRow, WORKSPACE_ID)
                assert workspace is not None
                workspace.generation += 1
            elif change == "source":
                current.source_id = str(uuid4())
            elif change == "source_locator":
                source = session.get(DevelopmentSourceRow, SOURCE_ID)
                assert source is not None
                source.relative_locator = "sources/replaced"
            elif change == "policy":
                policy.retain_profiles, policy.retained_omega_target_id = False, None
            elif change == "revision":
                instance.runtime_revision_id = str(uuid4())
            elif change == "fixture":
                instance.fixture_definition_id = str(uuid4())
            elif change == "image":
                artifact = session.get(RuntimeContainerArtifactRow, str(REVISION_ID))
                assert artifact is not None
                artifact.image_id = "sha256:" + "d" * 64
            elif change == "cid":
                broker.container_id = "f" * 64
            elif change == "mutation_token":
                mutation = session.get(
                    ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)
                )
                assert mutation is not None
                mutation.lease_token_digest = "0" * 64
            else:
                heavy = session.get(GlobalHeavyOperationLeaseRow, 1)
                assert heavy is not None
                if change == "expiry":
                    heavy.lease_expires_at = "2020-01-01T00:00:00.000000Z"
                else:
                    heavy.operation_kind = GlobalHeavyOperationKind.SNAPSHOT_CREATE

    monkeypatch.setattr(restore._coordinator, "update_phase", change_before_removal)
    with pytest.raises(SnapshotRestoreError):
        restore.restore()
    assert broker.calls == []
    with factory() as session:
        evidence = session.get(
            DevelopmentSourceDeploymentRow, (str(INSTANCE_ID), SOURCE_ID)
        )
        assert (
            evidence is not None
            and evidence.confidence
            is DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
        )


def test_generation_capacity_refused_before_stop(
    factory: sessionmaker[Session],
) -> None:
    select_session(factory, generation=256)
    broker = FakeBroker(ContainerRuntimeState.RUNNING)
    with pytest.raises(DevelopmentSessionConflict):
        service(factory, broker).restore()
    assert broker.calls == []


def test_ordinary_stale_provision_still_refused(factory: sessionmaker[Session]) -> None:
    select_session(factory)
    broker = FakeBroker(
        ContainerRuntimeState.STOPPED, container_id=None, sessions=factory
    )
    with pytest.raises(
        ManagedCapacityError, match="development_session_identity_changed"
    ):
        broker.provision_managed_container()
    assert broker.calls == []


def test_other_selected_target_is_refused_at_queue(
    factory: sessionmaker[Session],
) -> None:
    select_session(factory)
    with factory.begin() as session:
        current = session.get(DevelopmentSessionRow, 55)
        assert current is not None
        current.target_id = str(uuid4())
    with pytest.raises(SnapshotRestoreUnsupportedError):
        SnapshotRestoreOperationCoordinator(factory).queue(INSTANCE_ID, SNAPSHOT_ID)
    with factory() as session:
        assert session.scalar(select(ManagedInstanceMutationLeaseRow)) is None
        assert session.scalar(select(GlobalHeavyOperationLeaseRow)) is None


@pytest.mark.parametrize("after_handoff", [False, True])
def test_interruption_never_reenters_original_restore(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    after_handoff: bool,
) -> None:
    select_session(factory)
    broker = FakeBroker(ContainerRuntimeState.STOPPED)
    restore = service(factory, broker)
    if after_handoff:

        def interrupt_create(**_kwargs: object) -> ProvisionManagedContainerResult:
            raise KeyboardInterrupt

        monkeypatch.setattr(broker, "provision_managed_container", interrupt_create)
    else:
        remove = broker.deprovision_managed_container

        def interrupt_removal(**kwargs: object) -> DeprovisionManagedContainerResult:
            remove(**kwargs)
            raise KeyboardInterrupt

        monkeypatch.setattr(broker, "deprovision_managed_container", interrupt_removal)
    with pytest.raises(KeyboardInterrupt):
        restore.restore()
    calls = broker.calls[:]
    assert broker.container_id is None
    with factory() as session:
        old = session.get(DevelopmentSessionRow, 55)
        new = session.get(DevelopmentSessionRow, 56)
        assert old is not None and old.container_id == OLD_ID
        assert old.phase == ("closed" if after_handoff else "bound")
        assert (new is not None) is after_handoff
        if new is not None:
            assert (new.phase, new.container_id) == ("reserved", None)
    assert restore._coordinator.acquire(restore._lease.operation_id) is None
    with pytest.raises(SnapshotRestoreLeaseLostError):
        restore.restore()
    assert broker.calls == calls


def test_historical_selected_incident_cannot_adopt_current_generation(
    factory: sessionmaker[Session],
) -> None:
    select_session(factory)
    broker = FakeBroker(ContainerRuntimeState.STOPPED, container_id=None)
    coordinator = SnapshotRestoreOperationCoordinator(factory)
    queued = coordinator.queue(INSTANCE_ID, SNAPSHOT_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    coordinator.record_prior_state(lease, InstanceRuntimeState.STOPPED)
    coordinator.record_container_identity(lease, old_container_id=OLD_ID)
    coordinator.update_phase(lease, SnapshotRestoreOperationPhase.PROVISIONING)
    coordinator.finish_failure(
        lease,
        reason="source removed; provisioning refused",
        reconciliation_required=True,
    )
    original = coordinator.get(lease.operation_id)
    assert coordinator.acquire(lease.operation_id) is None
    with pytest.raises(SnapshotRestoreLeaseLostError):
        coordinator.update_phase(
            lease,
            SnapshotRestoreOperationPhase.VALIDATING_SNAPSHOT,
            expected_phase=SnapshotRestoreOperationPhase.ACQUIRING_LEASES,
        )
    assert coordinator.get(lease.operation_id) == original
    with factory() as session:
        current = session.get(DevelopmentSessionRow, 55)
        assert current is not None and (current.phase, current.container_id) == (
            "bound",
            OLD_ID,
        )
        assert session.get(DevelopmentSessionRow, 56) is None
    assert broker.calls == []


def test_unknown_create_keeps_broker_and_successor_fenced(
    factory: sessionmaker[Session],
) -> None:
    select_session(factory)
    broker = FakeBroker(ContainerRuntimeState.STOPPED, fail="unknown_create")
    restore = service(factory, broker)
    with pytest.raises(SnapshotRestoreError) as error:
        restore.restore()
    assert error.value.reconciliation_required
    restore._coordinator.finish_failure(
        restore._lease, reason=error.value.safe_reason, reconciliation_required=True
    )
    assert broker.calls == ["deprovision", "provision"]
    with factory() as session:
        old = session.get(DevelopmentSessionRow, 55)
        new = session.get(DevelopmentSessionRow, 56)
        reservation = session.get(ManagedProvisionReservationRow, str(INSTANCE_ID))
        assert old is not None and (old.phase, old.container_id) == ("closed", OLD_ID)
        assert new is not None and new.phase == "uncertain"
        assert reservation is not None and reservation.phase == "uncertain"
    assert restore._coordinator.acquire(restore._lease.operation_id) is None
    with pytest.raises(SnapshotRestoreLeaseLostError):
        restore.restore()
    assert broker.calls == ["deprovision", "provision"]


@pytest.mark.parametrize("reply", ["lost", "wrong", "remote_unknown"])
def test_unconfirmed_removal_never_hands_off_or_restarts(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    reply: str,
) -> None:
    select_session(factory)
    broker = FakeBroker(ContainerRuntimeState.RUNNING)
    remove = broker.deprovision_managed_container

    def unconfirmed(**kwargs: object) -> DeprovisionManagedContainerResult:
        result = remove(**kwargs)
        if reply == "lost":
            raise ClientTransportError("lost removal reply")
        if reply == "remote_unknown":
            raise RemoteBrokerError(
                "internal_error", "unknown removal", request_id=None
            )
        return result.model_copy(update={"removed_container_id": "f" * 64})

    monkeypatch.setattr(broker, "deprovision_managed_container", unconfirmed)
    restore = service(factory, broker)
    with pytest.raises(SnapshotRestoreError) as error:
        restore.restore()
    assert error.value.reconciliation_required
    assert broker.calls == ["stop", "deprovision"]
    with factory() as session:
        assert session.get(DevelopmentSessionRow, 56) is None
        old = session.get(DevelopmentSessionRow, 55)
        assert old is not None and (old.phase, old.container_id) == ("bound", OLD_ID)


@pytest.mark.parametrize(
    "step,field,value",
    [
        ("provision", "managed_instance_id", str(uuid4())),
        ("provision", "image_id", "sha256:" + "d" * 64),
        ("provision", "runtime_revision_id", str(uuid4())),
        ("provision", "runtime_state", ContainerRuntimeState.RUNNING),
        ("apply", "snapshot_id", str(uuid4())),
        ("apply", "container_id", "f" * 64),
        ("start", "container_id", "f" * 64),
        ("stop", "managed_instance_id", str(uuid4())),
    ],
)
def test_remote_success_identity_is_not_trusted(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    field: str,
    value: object,
) -> None:
    broker = FakeBroker(ContainerRuntimeState.RUNNING)
    method_name = {
        "provision": "provision_managed_container",
        "apply": "apply_managed_instance_snapshot",
        "start": "start_managed_container",
        "stop": "stop_managed_container",
    }[step]
    method = getattr(broker, method_name)

    def wrong_reply(**kwargs: object) -> object:
        return method(**kwargs).model_copy(update={field: value})

    monkeypatch.setattr(broker, method_name, wrong_reply)
    with pytest.raises(SnapshotRestoreError) as error:
        service(factory, broker).restore()
    assert error.value.reconciliation_required
    assert broker.calls[-1] == step
