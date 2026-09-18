from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_zip_installations import (
    AddonZipInstallationCoordinator,
    AddonZipInstallationService,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.operations import (
    AddonZipDestinationResult,
    ApplyAddonZipInstallationResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.domain import (
    AddonZipInstallationOperation,
    AddonZipInstallationOperationState,
    PythonBytecodePolicy,
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
    AddonZipArtifactRow,
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    AddonZipInstallationEvidenceRepository,
    AddonZipInstallationOperationRepository,
)

INSTANCE_ID = UUID("a8240000-0000-4000-8000-000000000001")
ARTIFACT_ID = UUID("a8240000-0000-4000-8000-000000000002")
REVISION_ID = UUID("a8240000-0000-4000-8000-000000000003")
DEFINITION_ID = UUID("a8240000-0000-4000-8000-000000000004")
CONTAINER_ID = "a" * 64
ADDON_ID = "plugin.video.uploaded"
VERSION = "1.2.3"
NOW = datetime(2026, 9, 17, 19, 0, tzinfo=UTC)


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

    def _fail(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_on == name:
            raise RuntimeError("injected")

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
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
