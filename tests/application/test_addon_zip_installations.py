from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_repositories import (
    AddonRepositoryAdministrationService,
    AddonRepositoryConflictError,
)
from kodi_lab.application.addon_zip_installations import (
    AddonZipInstallationConflictError,
    AddonZipInstallationCoordinator,
    AddonZipInstallationError,
    AddonZipInstallationService,
    invalidate_addon_zip_installation_evidence,
)
from kodi_lab.application.development_source_deployments import (
    DevelopmentSourceDeploymentConflictError,
    DevelopmentSourceDeploymentOperationCoordinator,
)
from kodi_lab.application.readiness_tasks import (
    ReadinessTaskRegistry,
    ReadinessTaskSnapshot,
    ReadinessTaskState,
)
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationRequiredError,
    ManagedInstanceReconciliationService,
    ReconciliationAction,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.application.repository_replacements import (
    APPROVAL_TTL,
    RepositoryReplacementConflictError,
    RepositoryReplacementCoordinator,
    RepositoryReplacementService,
    RepositoryReplacementUnsupportedError,
)
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.application.snapshot_delete_operations import (
    SnapshotDeleteOperationCoordinator,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import (
    BrokerUnavailableError,
    ClientTransportError,
    RemoteBrokerError,
)
from kodi_lab.broker.operations import (
    AddonZipDestinationResult,
    ApplyAddonZipInstallationResult,
    ApplyRepositoryReplacementResult,
    CopySyncAttachedDevelopmentSourceResult,
    InspectRepositoryReplacementResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.domain import (
    AddonZipInstallationConfidence,
    AddonZipInstallationOperation,
    AddonZipInstallationOperationState,
    AddonZipOrigin,
    PythonBytecodePolicy,
    RepositoryReplacementIncumbentClass,
    RepositoryReplacementOperationPhase,
    RepositoryReplacementOperationState,
    RepositoryReplacementPlanState,
)
from kodi_lab.kodi.addons import KodiAddonInventoryRecord, KodiAddonInventoryResult
from kodi_lab.kodi.readiness import (
    JsonRpcState,
    KodiApplicationIdentity,
    ReadinessResult,
    UsabilityState,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    AddonRepositoryCandidateRow,
    AddonRepositoryMetadataSnapshotRow,
    AddonRepositorySourceRow,
    AddonZipArtifactRow,
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    DevelopmentSourceRow,
    ManagedInstanceAddonRepositorySourceRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    SnapshotRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    AddonZipInstallationEvidenceRepository,
    AddonZipInstallationOperationRepository,
    DevelopmentSourceDeploymentRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedRuntimeProjectionRepository,
    RepositoryReplacementOperationRepository,
    RepositoryReplacementPlanRepository,
)

INSTANCE_ID = UUID("a8240000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("a8240000-0000-4000-8000-000000000002")
REVISION_ID = UUID("a8240000-0000-4000-8000-000000000003")
DEFINITION_ID = UUID("a8240000-0000-4000-8000-000000000004")
CONTAINER_ID = "a" * 64
ADDON_ID = "plugin.video.uploaded"
VERSION = "1.2.3"
NOW = datetime(2026, 9, 17, 19, 0, tzinfo=UTC)
REPOSITORY_ID = UUID("a8240000-0000-4000-8000-000000000005")
SNAPSHOT_ID = UUID("a8240000-0000-4000-8000-000000000006")
CANDIDATE_ID = UUID("a8240000-0000-4000-8000-000000000007")
SOURCE_ID = UUID("a8240000-0000-4000-8000-000000000008")
CENTRAL_SNAPSHOT_ID = UUID("a8240000-0000-4000-8000-000000000009")
DEPLOYMENT_OPERATION_ID = UUID("a8240000-0000-4000-8000-00000000000a")


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'zip-install.db'}")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    with result.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="zip-runtime", kodi_version="21.3"
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
                content_digest="sha256:" + "1" * 64,
                manifest_reference="test",
                manifest_digest="sha256:" + "2" * 64,
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
                runtime_uid=1000,
                runtime_gid=1000,
                python_bytecode_policy="suppress",
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
        session.add(
            TargetRow(
                id=str(INSTANCE_ID),
                name="ZIP instance",
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
                runtime_state="running",
                start_on_host_boot=False,
            )
        )
        session.add(
            AddonZipArtifactRow(
                id=str(ARTIFACT_ID),
                logical_label="Uploaded test",
                sha256_digest="sha256:" + "3" * 64,
                compressed_size=100,
                total_uncompressed_bytes=200,
                member_count=2,
                addon_id=ADDON_ID,
                addon_name="Uploaded Test",
                addon_version=VERSION,
                extension_classification="python_video_plugin",
                dependencies_json="[]",
                payload_manifest_digest="sha256:" + "4" * 64,
                imported_at=NOW.isoformat(),
                origin="uploaded_zip",
                validation_state="accepted",
            )
        )
    yield result
    engine.dispose()


def addon(
    addon_id: str,
    *,
    version: str = "1.0.0",
    enabled: bool = False,
    kodi_type: str = "xbmc.python.module",
) -> KodiAddonInventoryRecord:
    return KodiAddonInventoryRecord(addon_id, addon_id, version, enabled, kodi_type)


class InventorySequence:
    def __init__(self, values: list[tuple[KodiAddonInventoryRecord, ...]]) -> None:
        self.values = values

    def factory(self, _observation: object, _profile: object) -> InventorySequence:
        return self

    def get_inventory(self) -> KodiAddonInventoryResult:
        return KodiAddonInventoryResult(
            KodiApplicationIdentity("Kodi", 21, 3, "stable"), self.values.pop(0)
        )


class Ready:
    def wait_until_usable(self, _managed_instance_id: str) -> ReadinessResult:
        return ReadinessResult(
            ContainerRuntimeState.RUNNING,
            KodiProcessState.RUNNING,
            JsonRpcState.RESPONSIVE,
            UsabilityState.USABLE,
            None,
            None,
            KodiApplicationIdentity("Kodi", 21, 3, "stable"),
            1.0,
            1,
        )


class FakeBroker:
    def __init__(self) -> None:
        self.state = ContainerRuntimeState.RUNNING
        self.destination: Literal[
            "absent", "exact_payload_present", "partial_or_unexpected"
        ] = "absent"
        self.calls: list[str] = []
        self.fail_on: str | None = None
        self.destination_calls = 0
        self.fail_destination_call: int | None = None
        self.replacement_topology: Literal[
            "absent_eligible",
            "overwrite_add_eligible",
            "exact_payload_noop",
            "deletion_required",
            "type_transition",
            "incumbent_drift",
        ] = "absent_eligible"

    def _fail(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_on == name:
            raise RuntimeError("injected")

    def managed_container_inventory(
        self, *, managed_instance_id: str | None = None
    ) -> ManagedContainerInventoryResult:
        assert managed_instance_id in {None, str(INSTANCE_ID)}
        self._fail("inventory")
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(INSTANCE_ID),
                    container_id=CONTAINER_ID,
                    image_id="sha256:" + "5" * 64,
                    runtime_state=self.state,
                    health_state=(
                        ContainerHealthState.HEALTHY
                        if self.state is ContainerRuntimeState.RUNNING
                        else ContainerHealthState.NOT_CONFIGURED
                    ),
                )
            ]
        )

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        self._fail("runtime")
        return ObserveManagedKodiRuntimeResult(
            managed_instance_id=managed_instance_id,
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            runtime_state=ContainerRuntimeState.RUNNING,
            process_state=KodiProcessState.RUNNING,
            ipv4_address="10.250.0.2",
            jsonrpc_port=8080,
        )

    def stop_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StopManagedContainerResult:
        assert expected_runtime_state is ContainerRuntimeState.RUNNING
        self._fail("stop")
        self.state = ContainerRuntimeState.STOPPED
        return StopManagedContainerResult(
            managed_instance_id=managed_instance_id,
            container_id=expected_container_id,
            runtime_state=self.state,
            stop_performed=True,
        )

    def start_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StartManagedContainerResult:
        assert expected_runtime_state is ContainerRuntimeState.STOPPED
        self._fail("start")
        self.state = ContainerRuntimeState.RUNNING
        return StartManagedContainerResult(
            managed_instance_id=managed_instance_id,
            container_id=expected_container_id,
            runtime_state=self.state,
            start_performed=True,
        )

    def observe_addon_zip_destination(
        self,
        *,
        managed_instance_id: str,
        artifact_id: str,
        installation_operation_id: str,
        expected_container_id: str,
    ) -> AddonZipDestinationResult:
        self.destination_calls += 1
        if self.destination_calls == self.fail_destination_call:
            raise RuntimeError("injected destination fault")
        self._fail("destination")
        return AddonZipDestinationResult(
            managed_instance_id=managed_instance_id,
            artifact_id=artifact_id,
            installation_operation_id=installation_operation_id,
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            destination_state=self.destination,
            matched=self.destination == "exact_payload_present",
        )

    def apply_addon_zip_installation(
        self,
        *,
        managed_instance_id: str,
        artifact_id: str,
        installation_operation_id: str,
        expected_container_id: str,
    ) -> ApplyAddonZipInstallationResult:
        self.destination = "partial_or_unexpected"
        self._fail("apply")
        self.destination = "exact_payload_present"
        return ApplyAddonZipInstallationResult(
            managed_instance_id=managed_instance_id,
            artifact_id=artifact_id,
            installation_operation_id=installation_operation_id,
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            matched=True,
        )

    def inspect_repository_replacement(
        self,
        *,
        managed_instance_id: str,
        approval_id: str,
        expected_container_id: str,
    ) -> InspectRepositoryReplacementResult:
        self._fail("inspect_replacement")
        return InspectRepositoryReplacementResult(
            managed_instance_id=managed_instance_id,
            approval_id=approval_id,
            artifact_id=str(ARTIFACT_ID),
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            topology=self.replacement_topology,
        )

    def apply_repository_replacement(
        self,
        *,
        managed_instance_id: str,
        approval_id: str,
        replacement_operation_id: str,
        expected_container_id: str,
    ) -> ApplyRepositoryReplacementResult:
        self._fail("apply_replacement")
        self.replacement_topology = "exact_payload_noop"
        return ApplyRepositoryReplacementResult(
            managed_instance_id=managed_instance_id,
            approval_id=approval_id,
            replacement_operation_id=replacement_operation_id,
            artifact_id=str(ARTIFACT_ID),
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            matched=True,
        )

    def copy_sync_attached_development_source(
        self,
        *,
        managed_instance_id: str,
        development_source_id: str,
        expected_container_id: str,
        deployment_operation_id: str,
    ) -> CopySyncAttachedDevelopmentSourceResult:
        del (
            managed_instance_id,
            development_source_id,
            expected_container_id,
            deployment_operation_id,
        )
        raise AssertionError("copy/sync must be blocked before broker mutation")


def execute(
    factory: sessionmaker[Session],
    broker: FakeBroker,
    before: tuple[KodiAddonInventoryRecord, ...],
    after: tuple[KodiAddonInventoryRecord, ...],
    *,
    after_application_hook: Callable[[], None] | None = None,
) -> UUID:
    coordinator = AddonZipInstallationCoordinator(factory)
    status = coordinator.queue(INSTANCE_ID, ARTIFACT_ID)
    lease = coordinator.acquire(status.operation_id)
    assert lease is not None
    inventories = InventorySequence([before, after])
    with suppress(Exception):
        AddonZipInstallationService(
            factory,
            broker,
            coordinator,
            lease,
            inventory_adapter_factory=inventories.factory,
            readiness_factory=lambda _broker, _profile: Ready(),
            after_application_hook=after_application_hook,
        ).install()
    return status.operation_id


def operation(
    factory: sessionmaker[Session], operation_id: UUID
) -> AddonZipInstallationOperation:
    with factory() as session:
        result = AddonZipInstallationOperationRepository(session).get(operation_id)
    assert result is not None
    return result


def set_dependencies(
    factory: sessionmaker[Session],
    *values: tuple[str, bool],
) -> None:
    with factory.begin() as session:
        row = session.get(AddonZipArtifactRow, str(ARTIFACT_ID))
        assert row is not None
        row.dependencies_json = json.dumps(
            [
                {
                    "id": addon_id,
                    "version": None,
                    "minversion": None,
                    "optional": optional,
                }
                for addon_id, optional in values
            ]
        )


def test_complete_lifecycle_succeeds_and_retains_verified_evidence(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    operation_id = execute(
        factory,
        broker,
        (),
        (addon(ADDON_ID, version=VERSION, kodi_type="xbmc.python.pluginsource"),),
    )
    result = operation(factory, operation_id)
    assert result.state is AddonZipInstallationOperationState.SUCCEEDED, (
        broker.calls,
        result.expected_container_id,
        result.phase,
    )
    assert result.stopped_readback_matched and result.final_readback_matched
    assert result.recognition_enabled is False
    assert broker.state is ContainerRuntimeState.RUNNING
    assert broker.calls.count("stop") == 1
    assert broker.calls.count("start") == 1
    with factory() as session:
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            INSTANCE_ID, ADDON_ID
        )
    assert evidence is not None and evidence.installation_operation_id == operation_id


@pytest.mark.parametrize(
    ("dependency_id", "optional", "inventory"),
    [
        ("script.module.missing", False, ()),
        ("script.module.disabled", False, (addon("script.module.disabled"),)),
        ("script.module.optional", True, ()),
    ],
)
def test_dependency_states_may_be_missing_or_disabled_but_must_not_change(
    factory: sessionmaker[Session],
    dependency_id: str,
    optional: bool,
    inventory: tuple[KodiAddonInventoryRecord, ...],
) -> None:
    set_dependencies(factory, (dependency_id, optional))
    operation_id = execute(
        factory,
        FakeBroker(),
        inventory,
        (
            *inventory,
            addon(ADDON_ID, version=VERSION, kodi_type="xbmc.python.pluginsource"),
        ),
    )
    assert operation(factory, operation_id).state is (
        AddonZipInstallationOperationState.SUCCEEDED
    )


@pytest.mark.parametrize("collision", ["inventory", "destination"])
def test_existing_id_or_destination_blocks_before_stop(
    factory: sessionmaker[Session], collision: str
) -> None:
    broker = FakeBroker()
    before: tuple[KodiAddonInventoryRecord, ...] = ()
    if collision == "inventory":
        before = (addon(ADDON_ID),)
    else:
        broker.destination = "partial_or_unexpected"
    operation_id = execute(factory, broker, before, ())
    assert operation(factory, operation_id).state is (
        AddonZipInstallationOperationState.FAILED
    )
    assert "stop" not in broker.calls and "apply" not in broker.calls


@pytest.mark.parametrize(
    "recognized",
    [
        (),
        (addon(ADDON_ID, version="9.9.9", kodi_type="xbmc.python.pluginsource"),),
        (addon(ADDON_ID, version=VERSION, kodi_type="xbmc.python.module"),),
        (
            addon(
                ADDON_ID,
                version=VERSION,
                enabled=True,
                kodi_type="xbmc.python.pluginsource",
            ),
        ),
    ],
)
def test_recognition_mismatch_requires_reconciliation_and_leaves_stopped(
    factory: sessionmaker[Session],
    recognized: tuple[KodiAddonInventoryRecord, ...],
) -> None:
    broker = FakeBroker()
    operation_id = execute(factory, broker, (), recognized)
    assert operation(factory, operation_id).state is (
        AddonZipInstallationOperationState.RECONCILIATION_REQUIRED
    )
    assert broker.state is ContainerRuntimeState.STOPPED


def test_failure_after_stop_before_application_restores_running(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()
    broker.fail_destination_call = 2
    operation_id = execute(factory, broker, (), ())
    assert operation(factory, operation_id).state is (
        AddonZipInstallationOperationState.FAILED
    )
    assert broker.state is ContainerRuntimeState.RUNNING
    assert "apply" not in broker.calls


def test_zip_stop_lost_response_blocks_without_false_application_history(
    factory: sessionmaker[Session],
) -> None:
    broker = FakeBroker()

    def lose_stop_response(**_kwargs: object) -> StopManagedContainerResult:
        broker.calls.append("stop")
        raise ClientTransportError("Broker Stop response lost")

    broker.stop_managed_container = lose_stop_response  # type: ignore[method-assign]
    operation_id = execute(factory, broker, (), ())
    recorded = operation(factory, operation_id)
    assert recorded.state is AddonZipInstallationOperationState.RECONCILIATION_REQUIRED
    assert recorded.failure_reason == "broker_runtime_dispatch_unconfirmed"
    assert not recorded.application_started
    assert "start" not in broker.calls and "apply" not in broker.calls
    with factory() as session:
        projections = ManagedRuntimeProjectionRepository(session)
        assert projections.has_blocking_runtime_reconciliation(INSTANCE_ID)


def test_interpreter_default_runtime_blocks_before_live_or_filesystem_work(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        profile = session.get(RuntimeExecutionProfileRow, str(REVISION_ID))
        assert profile is not None
        profile.python_bytecode_policy = PythonBytecodePolicy.INTERPRETER_DEFAULT
    broker = FakeBroker()
    operation_id = execute(factory, broker, (), ())
    assert operation(factory, operation_id).state is (
        AddonZipInstallationOperationState.FAILED
    )
    assert broker.calls == []


def test_dependency_mutation_after_restart_requires_reconciliation(
    factory: sessionmaker[Session],
) -> None:
    set_dependencies(factory, ("script.module.changed", False))
    broker = FakeBroker()
    operation_id = execute(
        factory,
        broker,
        (),
        (
            addon("script.module.changed", enabled=True),
            addon(ADDON_ID, version=VERSION, kodi_type="xbmc.python.pluginsource"),
        ),
    )
    assert operation(factory, operation_id).state is (
        AddonZipInstallationOperationState.RECONCILIATION_REQUIRED
    )
    assert broker.state is ContainerRuntimeState.STOPPED


@pytest.mark.parametrize("fault", ["during_apply", "after_apply"])
def test_any_fault_after_application_marker_reconciles_and_leaves_stopped(
    factory: sessionmaker[Session], fault: str
) -> None:
    broker = FakeBroker()
    hook = None
    if fault == "during_apply":
        broker.fail_on = "apply"
    else:

        def fail_after_apply() -> None:
            raise RuntimeError("after apply")

        hook = fail_after_apply
    operation_id = execute(factory, broker, (), (), after_application_hook=hook)
    result = operation(factory, operation_id)
    assert result.state is AddonZipInstallationOperationState.RECONCILIATION_REQUIRED
    assert result.application_started
    assert broker.state is ContainerRuntimeState.STOPPED


@pytest.mark.parametrize("fault", ["stop", "start", "final_readback"])
def test_fault_boundaries_preserve_running_or_stopped_policy(
    factory: sessionmaker[Session], fault: str
) -> None:
    broker = FakeBroker()
    if fault == "final_readback":
        broker.fail_destination_call = 4
    else:
        broker.fail_on = fault
    after = (addon(ADDON_ID, version=VERSION, kodi_type="xbmc.python.pluginsource"),)
    operation_id = execute(factory, broker, (), after)
    result = operation(factory, operation_id)
    if fault == "stop":
        assert result.state is AddonZipInstallationOperationState.FAILED
        assert broker.state is ContainerRuntimeState.RUNNING
        assert not result.application_started
    else:
        assert result.state is (
            AddonZipInstallationOperationState.RECONCILIATION_REQUIRED
        )
        assert broker.state is ContainerRuntimeState.STOPPED
        assert result.application_started


def configure_repository_artifact(
    factory: sessionmaker[Session], *, attach: bool, development_collision: bool
) -> None:
    with factory.begin() as session:
        session.add(
            AddonRepositorySourceRow(
                id=str(REPOSITORY_ID),
                logical_name="Repository authority",
                canonical_index_url="https://example.com/addons.xml",
                origin="external_https_repository",
                created_at=NOW.isoformat(),
            )
        )
        session.flush()
        session.add(
            AddonRepositoryMetadataSnapshotRow(
                id=str(SNAPSHOT_ID),
                repository_source_id=str(REPOSITORY_ID),
                fetched_at=NOW.isoformat(),
                transfer_digest="sha256:" + "6" * 64,
                canonical_metadata_digest="sha256:" + "7" * 64,
                transfer_size=100,
                expanded_size=100,
                entry_count=1,
            )
        )
        session.flush()
        session.add(
            AddonRepositoryCandidateRow(
                id=str(CANDIDATE_ID),
                snapshot_id=str(SNAPSHOT_ID),
                repository_source_id=str(REPOSITORY_ID),
                addon_id=ADDON_ID,
                name="Repository candidate",
                version=VERSION,
                classification="python_video_plugin",
                dependencies_json="[]",
                support="supported",
            )
        )
        if attach:
            session.add(
                ManagedInstanceAddonRepositorySourceRow(
                    managed_instance_id=str(INSTANCE_ID),
                    repository_source_id=str(REPOSITORY_ID),
                    attached_at=NOW.isoformat(),
                )
            )
        artifact = session.get(AddonZipArtifactRow, str(ARTIFACT_ID))
        assert artifact is not None
        artifact.origin = AddonZipOrigin.REPOSITORY_DOWNLOAD
        artifact.repository_source_id = str(REPOSITORY_ID)
        artifact.repository_snapshot_id = str(SNAPSHOT_ID)
        artifact.repository_candidate_id = str(CANDIDATE_ID)
        artifact.repository_fetched_at = NOW.isoformat()
        if development_collision:
            session.add(
                DevelopmentSourceRow(
                    id=str(SOURCE_ID),
                    logical_name="Protected source",
                    source_kind="local_directory",
                    relative_locator="protected-source",
                    addon_id=ADDON_ID,
                    observed_name="Protected Source",
                    observed_version=VERSION,
                    extension_types_json=(
                        '["xbmc.addon.metadata","xbmc.python.pluginsource"]'
                    ),
                    registered_at=NOW.isoformat(),
                    validated_at=NOW.isoformat(),
                )
            )
            session.flush()
            session.add(
                ManagedInstanceDevelopmentSourceRow(
                    managed_instance_id=str(INSTANCE_ID),
                    development_source_id=str(SOURCE_ID),
                    attached_at=NOW.isoformat(),
                )
            )


def replacement_coordinator(
    factory: sessionmaker[Session],
    broker: FakeBroker,
    inventories: InventorySequence,
    *,
    clock: Callable[[], datetime] = lambda: NOW,
) -> RepositoryReplacementCoordinator:
    return RepositoryReplacementCoordinator(
        factory,
        broker,
        clock=clock,
        inventory_factory=inventories.factory,
    )


def configure_verified_copied_incumbent(
    factory: sessionmaker[Session], *, snapshot: bool = True
) -> None:
    configure_repository_artifact(factory, attach=True, development_collision=True)
    incumbent_version = "1.0.0"
    with factory.begin() as session:
        source = session.get(DevelopmentSourceRow, str(SOURCE_ID))
        assert source is not None
        source.observed_version = incumbent_version
        session.add(
            DevelopmentSourceDeploymentOperationRow(
                id=str(DEPLOYMENT_OPERATION_ID),
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_ID),
                source_logical_name="Protected source",
                addon_id=ADDON_ID,
                runtime_revision_id=str(REVISION_ID),
                request_kind="copy_sync",
                requested_at=NOW.isoformat(),
                state="succeeded",
                phase="finalize",
                result_classification="initial_deployment",
                new_count=2,
                changed_count=0,
                unchanged_count=0,
                destination_only_count=0,
                transferred_bytes=100,
                expected_container_id=CONTAINER_ID,
                started_at=NOW.isoformat(),
                heartbeat_at=NOW.isoformat(),
                completed_at=NOW.isoformat(),
                reconciliation_required=False,
            )
        )
        session.flush()
        session.add(
            DevelopmentSourceDeploymentRow(
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_ID),
                addon_id=ADDON_ID,
                mode="copy",
                last_successful_operation_id=str(DEPLOYMENT_OPERATION_ID),
                source_manifest_digest="sha256:" + "8" * 64,
                verified_destination_digest="sha256:" + "9" * 64,
                runtime_revision_id=str(REVISION_ID),
                trusted_container_id=CONTAINER_ID,
                observed_source_name="Protected Source",
                observed_source_version=incumbent_version,
                verified_at=NOW.isoformat(),
                confidence="verified_at_last_operation",
            )
        )
        if snapshot:
            session.add(
                SnapshotRow(
                    id=str(CENTRAL_SNAPSHOT_ID),
                    source_managed_instance_id=str(INSTANCE_ID),
                    original_source_managed_instance_id=str(INSTANCE_ID),
                    source_binding="attached",
                    runtime_revision_id=str(REVISION_ID),
                    captured_at=NOW.isoformat(),
                    prior_runtime_state="running",
                    manifest_version=1,
                    archive_digest="sha256:" + "a" * 64,
                    manifest_digest="sha256:" + "b" * 64,
                    archive_size=100,
                    member_count=2,
                    state="available",
                    validation_at=NOW.isoformat(),
                    validation_result="passed",
                )
            )


def test_attached_unmaterialized_repository_replacement_is_one_shot_and_retained(
    factory: sessionmaker[Session],
) -> None:
    configure_repository_artifact(factory, attach=True, development_collision=True)
    broker = FakeBroker()
    coordinator = replacement_coordinator(
        factory, broker, InventorySequence([(), (), ()])
    )
    plan = coordinator.prepare(INSTANCE_ID, ARTIFACT_ID)
    assert plan.incumbent_class is (
        RepositoryReplacementIncumbentClass.ATTACHED_UNMATERIALIZED
    )
    assert plan.state is RepositoryReplacementPlanState.PENDING
    assert not plan.snapshot_ready
    queued = coordinator.approve_and_queue(INSTANCE_ID, plan.plan_id)
    with pytest.raises(RepositoryReplacementConflictError):
        coordinator.approve_and_queue(INSTANCE_ID, plan.plan_id)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    inventories = InventorySequence(
        [(), (addon(ADDON_ID, version=VERSION, kodi_type="xbmc.python.pluginsource"),)]
    )
    RepositoryReplacementService(
        factory,
        broker,
        coordinator,
        lease,
        inventory_factory=inventories.factory,
        readiness_factory=lambda _broker, _profile: Ready(),
    ).execute()
    with factory() as session:
        operation = RepositoryReplacementOperationRepository(session).get(
            queued.operation_id
        )
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            INSTANCE_ID, ADDON_ID
        )
        attachment = ManagedInstanceDevelopmentSourceRepository(session).get(
            INSTANCE_ID, SOURCE_ID
        )
    assert operation is not None
    assert operation.state is RepositoryReplacementOperationState.SUCCEEDED
    assert operation.recognition_enabled is False
    assert evidence is not None
    assert evidence.replacement_operation_id == operation.id
    assert evidence.installation_operation_id is None
    assert attachment is not None
    deployment = DevelopmentSourceDeploymentOperationCoordinator(
        factory,
        broker,
        DevelopmentSourceValidator(None),
    )
    with pytest.raises(DevelopmentSourceDeploymentConflictError) as blocked:
        deployment.queue(INSTANCE_ID, SOURCE_ID)
    assert blocked.value.code == "repository_override_active"


def test_verified_copy_replacement_preserves_enabled_and_stales_source_evidence(
    factory: sessionmaker[Session],
) -> None:
    configure_verified_copied_incumbent(factory)
    broker = FakeBroker()
    broker.replacement_topology = "overwrite_add_eligible"
    incumbent = addon(
        ADDON_ID,
        version="1.0.0",
        enabled=True,
        kodi_type="xbmc.python.pluginsource",
    )
    incoming = addon(
        ADDON_ID,
        version=VERSION,
        enabled=True,
        kodi_type="xbmc.python.pluginsource",
    )
    coordinator = replacement_coordinator(
        factory, broker, InventorySequence([(incumbent,), (incumbent,), (incumbent,)])
    )
    plan = coordinator.prepare(INSTANCE_ID, ARTIFACT_ID)
    assert plan.incumbent_class is RepositoryReplacementIncumbentClass.VERIFIED_COPIED
    assert plan.snapshot_ready and plan.enabled is True
    queued = coordinator.approve_and_queue(INSTANCE_ID, plan.plan_id)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    RepositoryReplacementService(
        factory,
        broker,
        coordinator,
        lease,
        inventory_factory=InventorySequence([(incumbent,), (incoming,)]).factory,
        readiness_factory=lambda _broker, _profile: Ready(),
    ).execute()
    with factory() as session:
        operation = RepositoryReplacementOperationRepository(session).get(
            queued.operation_id
        )
        development = DevelopmentSourceDeploymentRepository(session).get(
            INSTANCE_ID, SOURCE_ID
        )
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            INSTANCE_ID, ADDON_ID
        )
    assert operation is not None and operation.recognition_enabled is True
    assert development is not None
    assert development.confidence.value == "stale"
    assert evidence is not None
    assert (
        evidence.confidence is AddonZipInstallationConfidence.VERIFIED_AT_LAST_OPERATION
    )


def test_verified_copy_requires_recent_available_snapshot(
    factory: sessionmaker[Session],
) -> None:
    configure_verified_copied_incumbent(factory, snapshot=False)
    broker = FakeBroker()
    broker.replacement_topology = "overwrite_add_eligible"
    incumbent = addon(
        ADDON_ID,
        version="1.0.0",
        enabled=True,
        kodi_type="xbmc.python.pluginsource",
    )
    coordinator = replacement_coordinator(
        factory, broker, InventorySequence([(incumbent,)])
    )
    with pytest.raises(RepositoryReplacementUnsupportedError) as raised:
        coordinator.prepare(INSTANCE_ID, ARTIFACT_ID)
    assert raised.value.code == "qualifying_snapshot_required"


def test_snapshot_deletion_invalidates_pending_replacement_plan(
    factory: sessionmaker[Session],
) -> None:
    configure_verified_copied_incumbent(factory)
    broker = FakeBroker()
    broker.replacement_topology = "overwrite_add_eligible"
    incumbent = addon(
        ADDON_ID,
        version="1.0.0",
        enabled=True,
        kodi_type="xbmc.python.pluginsource",
    )
    coordinator = replacement_coordinator(
        factory, broker, InventorySequence([(incumbent,)])
    )
    plan = coordinator.prepare(INSTANCE_ID, ARTIFACT_ID)
    SnapshotDeleteOperationCoordinator(factory).queue(INSTANCE_ID, CENTRAL_SNAPSHOT_ID)
    with factory() as session:
        persisted = RepositoryReplacementPlanRepository(session).get(plan.plan_id)
    assert persisted is not None
    assert persisted.state is RepositoryReplacementPlanState.INVALIDATED
    assert persisted.invalidation_reason == "qualifying_snapshot_deleted"


@pytest.mark.parametrize(
    ("topology", "code"),
    [
        ("exact_payload_noop", "exact_payload_noop"),
        ("deletion_required", "unsupported_replacement_topology"),
        ("type_transition", "unsupported_replacement_topology"),
        ("incumbent_drift", "incumbent_state_drift"),
    ],
)
def test_replacement_plan_rejects_noop_deleting_type_change_and_drift(
    factory: sessionmaker[Session],
    topology: Literal[
        "exact_payload_noop",
        "deletion_required",
        "type_transition",
        "incumbent_drift",
    ],
    code: str,
) -> None:
    configure_repository_artifact(factory, attach=True, development_collision=True)
    broker = FakeBroker()
    broker.replacement_topology = topology
    coordinator = replacement_coordinator(factory, broker, InventorySequence([()]))
    with pytest.raises(RepositoryReplacementUnsupportedError) as raised:
        coordinator.prepare(INSTANCE_ID, ARTIFACT_ID)
    assert raised.value.code == code
    with factory() as session:
        plans = RepositoryReplacementPlanRepository(session).list_for_original_instance(
            INSTANCE_ID
        )
    assert len(plans) == 1
    assert plans[0].state is RepositoryReplacementPlanState.INVALIDATED


def test_replacement_approval_expires_after_fixed_fifteen_minutes(
    factory: sessionmaker[Session],
) -> None:
    configure_repository_artifact(factory, attach=True, development_collision=True)
    current = [NOW]
    coordinator = replacement_coordinator(
        factory,
        FakeBroker(),
        InventorySequence([()]),
        clock=lambda: current[0],
    )
    plan = coordinator.prepare(INSTANCE_ID, ARTIFACT_ID)
    assert plan.expires_at == NOW + APPROVAL_TTL
    current[0] = plan.expires_at
    with pytest.raises(RepositoryReplacementConflictError):
        coordinator.approve_and_queue(INSTANCE_ID, plan.plan_id)
    with factory() as session:
        persisted = RepositoryReplacementPlanRepository(session).get(plan.plan_id)
    assert persisted is not None
    assert persisted.state is RepositoryReplacementPlanState.EXPIRED


def test_attachment_drift_invalidates_before_stop_and_cannot_be_reused(
    factory: sessionmaker[Session],
) -> None:
    configure_repository_artifact(factory, attach=True, development_collision=True)
    broker = FakeBroker()
    coordinator = replacement_coordinator(factory, broker, InventorySequence([()]))
    plan = coordinator.prepare(INSTANCE_ID, ARTIFACT_ID)
    queued = coordinator.approve_and_queue(INSTANCE_ID, plan.plan_id)
    with factory.begin() as session:
        attachment = session.get(
            ManagedInstanceDevelopmentSourceRow, (str(INSTANCE_ID), str(SOURCE_ID))
        )
        assert attachment is not None
        attachment.attached_at = "2026-09-17T19:00:01+00:00"
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    with pytest.raises(RepositoryReplacementConflictError):
        RepositoryReplacementService(
            factory,
            broker,
            coordinator,
            lease,
            inventory_factory=InventorySequence([]).factory,
            readiness_factory=lambda _broker, _profile: Ready(),
        ).execute()
    with factory() as session:
        persisted = RepositoryReplacementPlanRepository(session).get(plan.plan_id)
    assert persisted is not None
    assert persisted.state is RepositoryReplacementPlanState.INVALIDATED
    assert "stop" not in broker.calls
    with pytest.raises(RepositoryReplacementConflictError):
        coordinator.approve_and_queue(INSTANCE_ID, plan.plan_id)


def test_post_application_replacement_failure_is_consumed_and_reconciliation_required(
    factory: sessionmaker[Session],
) -> None:
    configure_repository_artifact(factory, attach=True, development_collision=True)
    broker = FakeBroker()
    coordinator = replacement_coordinator(
        factory, broker, InventorySequence([(), (), ()])
    )
    plan = coordinator.prepare(INSTANCE_ID, ARTIFACT_ID)
    queued = coordinator.approve_and_queue(INSTANCE_ID, plan.plan_id)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    broker.fail_on = "apply_replacement"
    with pytest.raises(RuntimeError, match="injected"):
        RepositoryReplacementService(
            factory,
            broker,
            coordinator,
            lease,
            inventory_factory=InventorySequence([()]).factory,
            readiness_factory=lambda _broker, _profile: Ready(),
        ).execute()
    with factory() as session:
        operation = RepositoryReplacementOperationRepository(session).get(
            queued.operation_id
        )
        persisted = RepositoryReplacementPlanRepository(session).get(plan.plan_id)
    assert operation is not None and persisted is not None
    assert (
        operation.state is RepositoryReplacementOperationState.RECONCILIATION_REQUIRED
    )
    assert operation.application_started
    assert persisted.state is RepositoryReplacementPlanState.CONSUMED
    assert broker.state is ContainerRuntimeState.STOPPED


def test_unconfirmed_preapplication_stop_is_durable_and_mutation_blocking(
    factory: sessionmaker[Session],
) -> None:
    class UnconfirmedStopBroker(FakeBroker):
        def stop_managed_container(
            self,
            *,
            managed_instance_id: str,
            expected_container_id: str,
            expected_runtime_state: ContainerRuntimeState,
        ) -> StopManagedContainerResult:
            self.calls.append("stop")
            raise ClientTransportError("Broker response lost after Stop dispatch")

    configure_repository_artifact(factory, attach=True, development_collision=True)
    broker = UnconfirmedStopBroker()
    coordinator = replacement_coordinator(factory, broker, InventorySequence([()]))
    plan = coordinator.prepare(INSTANCE_ID, ARTIFACT_ID)
    queued = coordinator.approve_and_queue(INSTANCE_ID, plan.plan_id)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    with pytest.raises(ClientTransportError):
        RepositoryReplacementService(
            factory,
            broker,
            coordinator,
            lease,
            inventory_factory=InventorySequence([()]).factory,
            readiness_factory=lambda _broker, _profile: Ready(),
        ).execute()
    with factory() as session:
        operation = RepositoryReplacementOperationRepository(session).get(
            queued.operation_id
        )
    assert operation is not None
    assert (
        operation.state is RepositoryReplacementOperationState.RECONCILIATION_REQUIRED
    )
    assert operation.phase.value == "stopping"
    assert not operation.application_started
    assert operation.failure_reason == "broker_runtime_dispatch_unconfirmed"
    assert broker.calls.count("stop") == 1
    gate = ManagedInstanceReconciliationService(factory, broker=broker)
    for _ in range(2):
        assert gate.status(INSTANCE_ID).blocking
    assert (
        ManagedInstanceReconciliationService(factory, broker=broker)
        .status(INSTANCE_ID)
        .blocking
    )
    assert (
        StaleOperationReconciliationDispatcher(factory)
        .sweep_instance(INSTANCE_ID)
        .released_reservations
        == 0
    )
    for action in (
        ReconciliationAction.START,
        ReconciliationAction.STOP,
        ReconciliationAction.RESET,
        ReconciliationAction.MCP_WORKSPACE_DEPLOYMENT,
    ):
        with pytest.raises(ManagedInstanceReconciliationRequiredError):
            gate.require_allowed(INSTANCE_ID, action)
    readiness = ReadinessTaskRegistry()
    try:
        assert (
            ManagedRuntimeStatusService(factory, broker, readiness)
            .get(INSTANCE_ID)
            .managed_instance_id
            == INSTANCE_ID
        )
    finally:
        readiness.close()

    class UsableReadiness:
        def snapshot(self, _instance_id: UUID) -> ReadinessTaskSnapshot:
            return ReadinessTaskSnapshot(
                generation=1,
                state=ReadinessTaskState.USABLE,
                result=Ready().wait_until_usable(str(INSTANCE_ID)),
            )

    ManagedRuntimeStatusService(
        factory,
        broker,
        UsableReadiness(),  # type: ignore[arg-type]
    ).get(INSTANCE_ID)
    with factory() as session:
        target = session.get(TargetRow, str(INSTANCE_ID))
        assert target is not None
        assert target.availability.value == "unavailable"
    assert gate.status(INSTANCE_ID).blocking


def test_unconfirmed_replacement_restart_skips_containment_stop(
    factory: sessionmaker[Session],
) -> None:
    class UnconfirmedStartBroker(FakeBroker):
        def start_managed_container(
            self,
            *,
            managed_instance_id: str,
            expected_container_id: str,
            expected_runtime_state: ContainerRuntimeState,
        ) -> StartManagedContainerResult:
            self.calls.append("start")
            raise ClientTransportError("Broker Start response lost")

    configure_repository_artifact(factory, attach=True, development_collision=True)
    broker = UnconfirmedStartBroker()
    coordinator = replacement_coordinator(
        factory, broker, InventorySequence([(), (), ()])
    )
    plan = coordinator.prepare(INSTANCE_ID, ARTIFACT_ID)
    queued = coordinator.approve_and_queue(INSTANCE_ID, plan.plan_id)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    with pytest.raises(ClientTransportError):
        RepositoryReplacementService(
            factory,
            broker,
            coordinator,
            lease,
            inventory_factory=InventorySequence([()]).factory,
            readiness_factory=lambda _broker, _profile: Ready(),
        ).execute()
    with factory() as session:
        operation = RepositoryReplacementOperationRepository(session).get(
            queued.operation_id
        )
    assert operation is not None
    assert operation.application_started
    assert (
        operation.state is RepositoryReplacementOperationState.RECONCILIATION_REQUIRED
    )
    assert operation.failure_reason == "broker_runtime_dispatch_unconfirmed"
    assert broker.calls.count("stop") == 1


@pytest.mark.parametrize("after_stop_marker", [False, True])
def test_expired_replacement_stopping_blocks_but_predispatch_expiry_recovers(
    factory: sessionmaker[Session],
    after_stop_marker: bool,
) -> None:
    configure_repository_artifact(factory, attach=True, development_collision=True)
    broker = FakeBroker()
    coordinator = replacement_coordinator(factory, broker, InventorySequence([()]))
    plan = coordinator.prepare(INSTANCE_ID, ARTIFACT_ID)
    queued = coordinator.approve_and_queue(INSTANCE_ID, plan.plan_id)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    if after_stop_marker:
        coordinator.update(lease, phase=RepositoryReplacementOperationPhase.STOPPING)
    sweep = StaleOperationReconciliationDispatcher(
        factory, clock=lambda: NOW + timedelta(minutes=16)
    ).sweep_instance(INSTANCE_ID)
    assert sweep.released_reservations == 2
    with factory() as session:
        operation = RepositoryReplacementOperationRepository(session).get(
            queued.operation_id
        )
    assert operation is not None
    assert operation.state is (
        RepositoryReplacementOperationState.RECONCILIATION_REQUIRED
        if after_stop_marker
        else RepositoryReplacementOperationState.FAILED
    )
    assert not operation.application_started
    assert operation.failure_reason == (
        "broker_runtime_dispatch_unconfirmed"
        if after_stop_marker
        else "repository_replacement_lease_expired"
    )
    assert (
        ManagedInstanceReconciliationService(factory, broker=broker)
        .status(INSTANCE_ID)
        .blocking
        is after_stop_marker
    )


def test_definitive_broker_stop_failure_remains_class_a(
    factory: sessionmaker[Session],
) -> None:
    class DefinitiveFailureBroker(FakeBroker):
        def stop_managed_container(
            self,
            *,
            managed_instance_id: str,
            expected_container_id: str,
            expected_runtime_state: ContainerRuntimeState,
        ) -> StopManagedContainerResult:
            self.calls.append("stop")
            raise RemoteBrokerError(
                "runtime_state_precondition_failed",
                "Stop rejected",
                request_id="test-request",
            )

    configure_repository_artifact(factory, attach=True, development_collision=True)
    broker = DefinitiveFailureBroker()
    coordinator = replacement_coordinator(factory, broker, InventorySequence([()]))
    plan = coordinator.prepare(INSTANCE_ID, ARTIFACT_ID)
    queued = coordinator.approve_and_queue(INSTANCE_ID, plan.plan_id)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    with pytest.raises(RemoteBrokerError):
        RepositoryReplacementService(
            factory,
            broker,
            coordinator,
            lease,
            inventory_factory=InventorySequence([()]).factory,
            readiness_factory=lambda _broker, _profile: Ready(),
        ).execute()
    with factory() as session:
        operation = RepositoryReplacementOperationRepository(session).get(
            queued.operation_id
        )
    assert operation is not None
    assert operation.state is RepositoryReplacementOperationState.FAILED
    assert not operation.application_started
    assert (
        not ManagedInstanceReconciliationService(factory, broker=broker)
        .status(INSTANCE_ID)
        .blocking
    )


def test_known_pre_dispatch_broker_unavailable_remains_class_a(
    factory: sessionmaker[Session],
) -> None:
    class UnavailableBroker(FakeBroker):
        def stop_managed_container(
            self,
            *,
            managed_instance_id: str,
            expected_container_id: str,
            expected_runtime_state: ContainerRuntimeState,
        ) -> StopManagedContainerResult:
            self.calls.append("stop")
            raise BrokerUnavailableError("Broker socket was unavailable")

    configure_repository_artifact(factory, attach=True, development_collision=True)
    broker = UnavailableBroker()
    coordinator = replacement_coordinator(factory, broker, InventorySequence([()]))
    plan = coordinator.prepare(INSTANCE_ID, ARTIFACT_ID)
    queued = coordinator.approve_and_queue(INSTANCE_ID, plan.plan_id)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    with pytest.raises(BrokerUnavailableError):
        RepositoryReplacementService(
            factory,
            broker,
            coordinator,
            lease,
            inventory_factory=InventorySequence([()]).factory,
            readiness_factory=lambda _broker, _profile: Ready(),
        ).execute()
    with factory() as session:
        operation = RepositoryReplacementOperationRepository(session).get(
            queued.operation_id
        )
    assert operation is not None
    assert operation.state is RepositoryReplacementOperationState.FAILED
    assert not operation.application_started and not operation.reconciliation_required


def install_repository_successfully(factory: sessionmaker[Session]) -> UUID:
    coordinator = AddonZipInstallationCoordinator(factory)
    status = coordinator.queue_repository(INSTANCE_ID, ARTIFACT_ID)
    lease = coordinator.acquire(status.operation_id)
    assert lease is not None
    inventories = InventorySequence(
        [
            (),
            (addon(ADDON_ID, version=VERSION, kodi_type="xbmc.python.pluginsource"),),
        ]
    )
    AddonZipInstallationService(
        factory,
        FakeBroker(),
        coordinator,
        lease,
        inventory_adapter_factory=inventories.factory,
        readiness_factory=lambda _broker, _profile: Ready(),
    ).install()
    return status.operation_id


def test_attached_same_id_development_source_blocks_repository_before_lifecycle(
    factory: sessionmaker[Session],
) -> None:
    configure_repository_artifact(factory, attach=True, development_collision=True)
    coordinator = AddonZipInstallationCoordinator(factory)
    status = coordinator.queue_repository(INSTANCE_ID, ARTIFACT_ID)
    lease = coordinator.acquire(status.operation_id)
    assert lease is not None
    broker = FakeBroker()
    with pytest.raises(AddonZipInstallationConflictError) as captured:
        AddonZipInstallationService(factory, broker, coordinator, lease).install()
    assert captured.value.code == "protected_development_source_collision"
    result = operation(factory, status.operation_id)
    assert result.state is AddonZipInstallationOperationState.FAILED
    assert result.failure_reason == "protected_development_source_collision"
    assert broker.calls == []


def test_repository_must_be_attached_and_install_conflicts_with_detach(
    factory: sessionmaker[Session],
) -> None:
    configure_repository_artifact(factory, attach=False, development_collision=False)
    coordinator = AddonZipInstallationCoordinator(factory)
    status = coordinator.queue_repository(INSTANCE_ID, ARTIFACT_ID)
    lease = coordinator.acquire(status.operation_id)
    assert lease is not None
    broker = FakeBroker()
    with pytest.raises(AddonZipInstallationError) as captured:
        AddonZipInstallationService(factory, broker, coordinator, lease).install()
    assert captured.value.code == "repository_not_attached"
    assert broker.calls == []

    # A fresh fixture is used for each test; this assertion exercises the shared
    # attachment/install mutation lease directly while installation owns it.


def test_repository_install_lease_blocks_attachment_changes(
    factory: sessionmaker[Session],
) -> None:
    configure_repository_artifact(factory, attach=True, development_collision=False)
    coordinator = AddonZipInstallationCoordinator(factory)
    status = coordinator.queue_repository(INSTANCE_ID, ARTIFACT_ID)
    lease = coordinator.acquire(status.operation_id)
    assert lease is not None
    administration = AddonRepositoryAdministrationService(factory)
    with pytest.raises(AddonRepositoryConflictError):
        administration.detach(INSTANCE_ID, REPOSITORY_ID)


@pytest.mark.parametrize("incumbent_origin", ["uploaded_zip", "repository_download"])
def test_prior_verified_installation_blocks_repository_even_if_live_state_is_absent(
    factory: sessionmaker[Session], incumbent_origin: str
) -> None:
    if incumbent_origin == "uploaded_zip":
        execute(
            factory,
            FakeBroker(),
            (),
            (addon(ADDON_ID, version=VERSION, kodi_type="xbmc.python.pluginsource"),),
        )
        configure_repository_artifact(factory, attach=True, development_collision=False)
    else:
        configure_repository_artifact(factory, attach=True, development_collision=False)
        install_repository_successfully(factory)

    coordinator = AddonZipInstallationCoordinator(factory)
    status = coordinator.queue_repository(INSTANCE_ID, ARTIFACT_ID)
    lease = coordinator.acquire(status.operation_id)
    assert lease is not None
    broker = FakeBroker()
    with pytest.raises(AddonZipInstallationConflictError) as captured:
        AddonZipInstallationService(factory, broker, coordinator, lease).install()
    assert captured.value.code == "verified_installation_collision"
    assert broker.calls == []


def test_snapshot_reset_invalidation_stales_only_repository_install_evidence(
    factory: sessionmaker[Session],
) -> None:
    configure_repository_artifact(factory, attach=True, development_collision=False)
    operation_id = install_repository_successfully(factory)

    with factory.begin() as session:
        evidence = AddonZipInstallationEvidenceRepository(session).get(
            INSTANCE_ID, ADDON_ID
        )
        assert evidence is not None
        assert evidence.artifact_origin is AddonZipOrigin.REPOSITORY_DOWNLOAD
        assert invalidate_addon_zip_installation_evidence(session, INSTANCE_ID) == 1

    with factory() as session:
        stale = AddonZipInstallationEvidenceRepository(session).get(
            INSTANCE_ID, ADDON_ID
        )
        assert stale is not None
        assert stale.confidence is AddonZipInstallationConfidence.STALE
        assert session.get(AddonRepositorySourceRow, str(REPOSITORY_ID)) is not None
        assert (
            session.get(
                ManagedInstanceAddonRepositorySourceRow,
                (str(INSTANCE_ID), str(REPOSITORY_ID)),
            )
            is not None
        )
        assert (
            session.get(AddonRepositoryMetadataSnapshotRow, str(SNAPSHOT_ID))
            is not None
        )
        assert session.get(AddonRepositoryCandidateRow, str(CANDIDATE_ID)) is not None
        assert session.get(AddonZipArtifactRow, str(ARTIFACT_ID)) is not None
        assert (
            AddonZipInstallationOperationRepository(session).get(operation_id)
            is not None
        )

    coordinator = AddonZipInstallationCoordinator(factory)
    status = coordinator.queue_repository(INSTANCE_ID, ARTIFACT_ID)
    lease = coordinator.acquire(status.operation_id)
    assert lease is not None
    broker = FakeBroker()
    with pytest.raises(AddonZipInstallationConflictError) as captured:
        AddonZipInstallationService(factory, broker, coordinator, lease).install()
    assert captured.value.code == "verified_installation_collision"
    assert broker.calls == []
