from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_inventory import (
    AddonInventoryQueryStatus,
    KodiAddonInventoryItem,
    KodiAddonProvenance,
    KodiAddonProvenanceClassification,
    KodiAddonProvenanceEvidenceStatus,
    ManagedInstanceAddonInventory,
)
from kodi_lab.application.agent_workspaces import AgentWorkspaceService
from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.application.mcp_workspace_development import (
    BoundedIntervalRateLimiter,
    McpWorkspaceDevelopmentError,
    McpWorkspaceDevelopmentService,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import RemoteBrokerError
from kodi_lab.broker.operations import (
    CopySyncAttachedDevelopmentSourceResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    VerifyAttachedDevelopmentSourceDestinationResult,
)
from kodi_lab.domain import DevelopmentSource, DevelopmentSourceKind
from kodi_lab.kodi.addons import (
    KodiAddonDetailsRecord,
    KodiAddonInventoryRecord,
    KodiAddonInventoryResult,
    KodiAddonSmokeResult,
    KodiAddonSmokeTimeoutError,
)
from kodi_lab.kodi.readiness import KodiApplicationIdentity, UsabilityState
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceRow,
    McpAgentWorkspaceBindingRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import DevelopmentSourceRepository

SOURCE_ID = UUID("72000000-0000-4000-8000-000000000001")
TARGET_ID = UUID("72000000-0000-4000-8000-000000000002")
RUNTIME_DEFINITION_ID = UUID("72000000-0000-4000-8000-000000000003")
RUNTIME_REVISION_ID = UUID("72000000-0000-4000-8000-000000000004")
BASELINE_OPERATION_ID = UUID("72000000-0000-4000-8000-000000000005")
CONTAINER_ID = "a" * 64
IMAGE_ID = "sha256:" + "b" * 64
NOW = datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
NOW_TEXT = "2026-09-20T18:00:00.000000Z"


class FakeInventory:
    def get(self, target_id: UUID) -> ManagedInstanceAddonInventory:
        assert target_id == TARGET_ID
        return ManagedInstanceAddonInventory(
            instance_id=target_id,
            observed_at=NOW,
            kodi_release="21.3",
            query_status=AddonInventoryQueryStatus.AVAILABLE,
            complete=True,
            items=[
                KodiAddonInventoryItem(
                    addon_id="plugin.video.slice4",
                    name="Slice 4",
                    version="1.0.0",
                    enabled=True,
                    kodi_type="xbmc.python.pluginsource",
                    provenance=KodiAddonProvenance(
                        classification=(KodiAddonProvenanceClassification.UNKNOWN),
                        evidence_status=KodiAddonProvenanceEvidenceStatus.UNKNOWN,
                    ),
                )
            ],
        )


@dataclass
class FakeBroker:
    state: ContainerRuntimeState = ContainerRuntimeState.RUNNING
    stop_count: int = 0
    sync_count: int = 0
    start_count: int = 0
    verify_count: int = 0
    fail_sync: BaseException | None = None

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(TARGET_ID),
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

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        assert managed_instance_id == str(TARGET_ID)
        assert expected_container_id == CONTAINER_ID
        return ObserveManagedKodiRuntimeResult(
            managed_instance_id=managed_instance_id,
            runtime_revision_id=str(RUNTIME_REVISION_ID),
            container_id=expected_container_id,
            runtime_state=ContainerRuntimeState.RUNNING,
            process_state=KodiProcessState.RUNNING,
            ipv4_address="10.250.0.4",
            jsonrpc_port=8080,
        )

    def stop_managed_container(self, **_kwargs: Any) -> object:
        self.stop_count += 1
        self.state = ContainerRuntimeState.STOPPED
        return object()

    def start_managed_container(self, **_kwargs: Any) -> object:
        self.start_count += 1
        self.state = ContainerRuntimeState.RUNNING
        return object()

    def copy_sync_mcp_workspace(
        self, **kwargs: Any
    ) -> CopySyncAttachedDevelopmentSourceResult:
        self.sync_count += 1
        if self.fail_sync is not None:
            raise self.fail_sync
        return CopySyncAttachedDevelopmentSourceResult(
            managed_instance_id=str(TARGET_ID),
            development_source_id=str(SOURCE_ID),
            deployment_operation_id=kwargs["deployment_operation_id"],
            runtime_revision_id=str(RUNTIME_REVISION_ID),
            container_id=CONTAINER_ID,
            result_classification="synchronized",
            source_manifest_digest="sha256:" + "1" * 64,
            verified_destination_digest="sha256:" + "2" * 64,
            observed_source_name="Slice 4",
            observed_source_version="1.0.0",
            new_count=0,
            changed_count=1,
            unchanged_count=1,
            destination_only_count=0,
            transferred_bytes=8,
        )

    def verify_mcp_workspace_destination(
        self, **_kwargs: Any
    ) -> VerifyAttachedDevelopmentSourceDestinationResult:
        self.verify_count += 1
        return VerifyAttachedDevelopmentSourceDestinationResult(
            managed_instance_id=str(TARGET_ID),
            development_source_id=str(SOURCE_ID),
            runtime_revision_id=str(RUNTIME_REVISION_ID),
            container_id=CONTAINER_ID,
            addon_id="plugin.video.slice4",
            addon_version="1.0.0",
            matched=True,
            video_plugin_descriptor_eligible=True,
        )


@dataclass
class SmokeCounter:
    count: int = 0

    def invoke(self, addon_id: str) -> KodiAddonSmokeResult:
        assert addon_id == "plugin.video.slice4"
        self.count += 1
        return KodiAddonSmokeResult(item_count=1, duration_ms=2)


class InventoryAdapter:
    def get_inventory(self) -> KodiAddonInventoryResult:
        return KodiAddonInventoryResult(
            identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
            items=(
                KodiAddonInventoryRecord(
                    addon_id="plugin.video.slice4",
                    name="Slice 4",
                    version="1.0.0",
                    enabled=True,
                    kodi_type="xbmc.python.pluginsource",
                ),
            ),
        )


class DetailsAdapter:
    def get_addon_details(self, addon_id: str) -> KodiAddonDetailsRecord:
        return KodiAddonDetailsRecord(
            addon_id=addon_id,
            name="Slice 4",
            version="1.0.0",
            enabled=True,
            kodi_type="xbmc.python.pluginsource",
            dependencies=(),
        )


@dataclass
class Environment:
    sessions: sessionmaker[Session]
    workspaces: AgentWorkspaceService
    development: McpWorkspaceDevelopmentService
    broker: FakeBroker
    smoke: SmokeCounter
    client_id: UUID
    workspace_id: UUID


def _source() -> DevelopmentSource:
    return DevelopmentSource(
        id=SOURCE_ID,
        logical_name="Slice 4 source",
        source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
        relative_locator="plugin.video.slice4",
        addon_id="plugin.video.slice4",
        observed_name="Slice 4",
        observed_version="1.0.0",
        extension_types=("xbmc.python.pluginsource",),
        registered_at=NOW,
        validated_at=NOW,
    )


def _environment(tmp_path: Path) -> Environment:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_database_engine(f"sqlite:///{tmp_path / 'slice4.db'}")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    source_root = tmp_path / "sources" / "plugin.video.slice4"
    source_root.mkdir(parents=True)
    (source_root / "addon.xml").write_text(
        '<addon id="plugin.video.slice4" name="Slice 4" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py">'
        "<provides>video</provides></extension>"
        "</addon>\n",
        encoding="utf-8",
    )
    (source_root / "main.py").write_text("pass\n", encoding="utf-8")
    digest = "sha256:" + "c" * 64
    with sessions.begin() as session:
        DevelopmentSourceRepository(session).create(_source())
        session.add(
            RuntimeDefinitionRow(
                id=str(RUNTIME_DEFINITION_ID),
                slug="kodi-21.3-slice4",
                kodi_version="21.3",
            )
        )
        session.flush()
        session.add(
            RuntimeRevisionRow(
                id=str(RUNTIME_REVISION_ID),
                runtime_definition_id=str(RUNTIME_DEFINITION_ID),
                revision_number=1,
                source_kind="official_package",
                source_reference="slice4-test",
                content_digest=digest,
                manifest_reference="runtime/slice4.json",
                manifest_digest=digest,
                built_at=NOW_TEXT,
                build_log_reference=None,
                approval_state="approved",
                approved_at=NOW_TEXT,
                approval_note=None,
                requires_trusted_build_confirmation=False,
                trusted_build_confirmed_at=None,
            )
        )
        session.add(
            TargetRow(
                id=str(TARGET_ID),
                name="Slice 4 disposable",
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
                target_id=str(TARGET_ID),
                instance_type="disposable",
                runtime_revision_id=str(RUNTIME_REVISION_ID),
                runtime_state="running",
                start_on_host_boot=False,
                notes=None,
            )
        )
        session.flush()
        session.add(
            RuntimeExecutionProfileRow(
                runtime_revision_id=str(RUNTIME_REVISION_ID),
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
            ManagedInstanceDevelopmentSourceRow(
                managed_instance_id=str(TARGET_ID),
                development_source_id=str(SOURCE_ID),
                attached_at=NOW_TEXT,
            )
        )
        session.flush()
        session.add(
            DevelopmentSourceDeploymentOperationRow(
                id=str(BASELINE_OPERATION_ID),
                original_managed_instance_id=str(TARGET_ID),
                managed_instance_id=str(TARGET_ID),
                development_source_id=str(SOURCE_ID),
                source_logical_name="Slice 4 source",
                addon_id="plugin.video.slice4",
                runtime_revision_id=str(RUNTIME_REVISION_ID),
                request_kind="copy_sync",
                requested_at=NOW_TEXT,
                state="succeeded",
                phase="finalize",
                result_classification="initial_deployment",
                new_count=2,
                changed_count=0,
                unchanged_count=0,
                destination_only_count=0,
                transferred_bytes=10,
                expected_container_id=CONTAINER_ID,
                started_at=NOW_TEXT,
                heartbeat_at=NOW_TEXT,
                completed_at=NOW_TEXT,
                failure_reason=None,
                reconciliation_required=False,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        session.flush()
        session.add(
            DevelopmentSourceDeploymentRow(
                managed_instance_id=str(TARGET_ID),
                development_source_id=str(SOURCE_ID),
                addon_id="plugin.video.slice4",
                mode="copy",
                last_successful_operation_id=str(BASELINE_OPERATION_ID),
                source_manifest_digest="sha256:" + "3" * 64,
                verified_destination_digest="sha256:" + "4" * 64,
                runtime_revision_id=str(RUNTIME_REVISION_ID),
                trusted_container_id=CONTAINER_ID,
                observed_source_name="Slice 4",
                observed_source_version="1.0.0",
                verified_at=NOW_TEXT,
                confidence="verified_at_last_operation",
            )
        )
    workspaces = AgentWorkspaceService(
        sessions,
        data_root=tmp_path / "data",
        development_root=tmp_path / "sources",
        clock=lambda: NOW,
    )
    creation = workspaces.queue_creation(SOURCE_ID)
    lease = workspaces.acquire(creation.operation_id)
    assert lease is not None
    workspaces.run(lease)
    administration = McpAdministrationService(sessions)
    client = administration.create_client(
        display_name="Slice 4 client", preset_id=FULL_LAB_CONTROL_PRESET_ID
    )
    with sessions.begin() as session:
        session.add(
            McpAgentWorkspaceBindingRow(
                workspace_id=str(creation.workspace_id),
                client_id=str(client.id),
                development_source_id=str(SOURCE_ID),
                binding_origin="mcp_created",
                created_at=NOW_TEXT,
            )
        )
    broker = FakeBroker()
    smoke = SmokeCounter()
    development = McpWorkspaceDevelopmentService(
        sessions,
        broker,
        FakeInventory(),  # type: ignore[arg-type]
        workspaces,
        data_root=tmp_path / "data",
        clock=lambda: NOW,
        readiness_factory=lambda _broker, _profile: SimpleNamespace(
            wait_until_usable=lambda _target: SimpleNamespace(
                usability_state=UsabilityState.USABLE
            )
        ),
        deployment_rate=BoundedIntervalRateLimiter(300, clock=lambda: 1000.0),
        execution_rate=BoundedIntervalRateLimiter(60, clock=lambda: 1000.0),
        inventory_adapter_factory=lambda _observation, _profile: InventoryAdapter(),
        details_adapter_factory=lambda _observation, _profile: DetailsAdapter(),
        smoke_adapter_factory=lambda _observation, _profile: smoke,
    )
    development.record_validation(
        workspace_id=creation.workspace_id,
        source_id=SOURCE_ID,
        generation=0,
        addon_id="plugin.video.slice4",
        addon_version="1.0.0",
        extension_types=("xbmc.python.pluginsource",),
        validated_at=NOW,
    )
    return Environment(
        sessions,
        workspaces,
        development,
        broker,
        smoke,
        client.id,
        creation.workspace_id,
    )


def test_exact_generation_deploy_execute_and_replay_counts(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    deploy_request = uuid4()
    first = env.development.deploy(
        client_id=env.client_id,
        request_id=deploy_request,
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=TARGET_ID,
    )
    replay = env.development.deploy(
        client_id=env.client_id,
        request_id=deploy_request,
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=TARGET_ID,
    )
    assert replay == first
    assert (env.broker.stop_count, env.broker.sync_count, env.broker.start_count) == (
        1,
        1,
        1,
    )
    execute_request = uuid4()
    executed = env.development.execute(
        client_id=env.client_id,
        request_id=execute_request,
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=TARGET_ID,
    )
    execute_replay = env.development.execute(
        client_id=env.client_id,
        request_id=execute_request,
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=TARGET_ID,
    )
    assert execute_replay == executed
    assert env.smoke.count == 1
    assert env.broker.verify_count == 2
    with env.sessions() as session:
        deployment = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        execution = session.scalar(select(McpWorkspaceExecutionOperationRow))
        assert deployment is not None and deployment.state == "succeeded"
        assert execution is not None and execution.state == "succeeded"
        assert set(deployment.__dict__).isdisjoint(
            {"path", "content", "container_id", "raw_response", "log"}
        )


def test_validation_generation_conflict_and_edit_blocks_old_deployment(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)
    with pytest.raises(McpWorkspaceDevelopmentError, match="request_id_conflict"):
        request_id = uuid4()
        env.development.deploy(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
        env.development.deploy(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=1,
            target_id=TARGET_ID,
        )
    edit = env.workspaces.replace_text_file(
        env.workspace_id,
        expected_generation=0,
        relative_path="main.py",
        content="print('new')\n",
    )
    assert edit.generation == 1
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="validation_generation_mismatch"
    ):
        env.development.deploy(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=1,
            target_id=TARGET_ID,
        )
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_not_deployed"):
        env.development.execute(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=1,
            target_id=TARGET_ID,
        )


@pytest.mark.parametrize(
    ("phase", "expected_state"),
    [
        ("claim_before_target_mutation", "failed"),
        ("after_stop", "reconciliation_required"),
        ("after_sync_before_readiness", "reconciliation_required"),
        ("after_readiness_before_evidence", "reconciliation_required"),
    ],
)
def test_deployment_crash_windows_never_redispatch(
    tmp_path: Path, phase: str, expected_state: str
) -> None:
    env = _environment(tmp_path)

    def fail(selected: str) -> None:
        if selected == phase:
            raise RuntimeError("injected crash")

    env.development._failure = fail
    request_id = uuid4()
    expected_error = (
        "deployment_ambiguous"
        if expected_state == "reconciliation_required"
        else "deployment_conflict"
    )
    with pytest.raises(McpWorkspaceDevelopmentError, match=expected_error):
        env.development.deploy(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    counts = (env.broker.stop_count, env.broker.sync_count, env.broker.start_count)
    with pytest.raises(McpWorkspaceDevelopmentError):
        env.development.deploy(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert (
        env.broker.stop_count,
        env.broker.sync_count,
        env.broker.start_count,
    ) == counts
    with env.sessions() as session:
        row = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert row is not None and row.state == expected_state


def test_execution_dispatch_crash_and_startup_sweep_are_at_most_once(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)
    env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=TARGET_ID,
    )

    def fail(phase: str) -> None:
        if phase == "execution_dispatch_marker":
            raise RuntimeError("lost after marker")

    env.development._failure = fail
    request_id = uuid4()
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_ambiguous"):
        env.development.execute(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert env.smoke.count == 0
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_ambiguous"):
        env.development.execute(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert env.smoke.count == 0
    assert env.development.sweep_startup() == 0


def test_execution_timeout_after_dispatch_is_ambiguous_and_not_retried(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)
    env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=TARGET_ID,
    )

    def timeout(_addon_id: str) -> KodiAddonSmokeResult:
        env.smoke.count += 1
        raise KodiAddonSmokeTimeoutError(5)

    env.smoke.invoke = timeout  # type: ignore[assignment]
    request_id = uuid4()
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_ambiguous"):
        env.development.execute(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_ambiguous"):
        env.development.execute(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert env.smoke.count == 1


def test_retention_blocks_current_and_unresolved_deployment(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    assert env.development.retention_blocker(env.workspace_id, 0) is None
    env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=TARGET_ID,
    )
    assert (
        env.development.retention_blocker(env.workspace_id, 0)
        == "workspace_current_deployment_retained"
    )


def test_deployment_and_execution_rate_windows_use_bounded_fake_clock() -> None:
    now = [1000.0]
    deployment = BoundedIntervalRateLimiter(300, maximum_keys=2, clock=lambda: now[0])
    execution = BoundedIntervalRateLimiter(60, maximum_keys=2, clock=lambda: now[0])
    assert deployment.allow("client:target")
    assert not deployment.allow("client:target")
    assert execution.allow("client")
    assert not execution.allow("client")
    now[0] += 60
    assert execution.allow("client")
    assert not deployment.allow("client:target")
    now[0] += 240
    assert deployment.allow("client:target")
    assert deployment.key_count <= 2 and execution.key_count <= 2


def test_correlated_stop_sync_readiness_and_inventory_failures_are_bounded(
    tmp_path: Path,
) -> None:
    stop_env = _environment(tmp_path / "stop")

    def fail_stop(**_kwargs: Any) -> object:
        stop_env.broker.stop_count += 1
        raise RuntimeError("stop failed")

    stop_env.broker.stop_managed_container = fail_stop  # type: ignore[method-assign]
    with pytest.raises(McpWorkspaceDevelopmentError, match="deployment_ambiguous"):
        stop_env.development.deploy(
            client_id=stop_env.client_id,
            request_id=uuid4(),
            workspace_id=stop_env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert stop_env.broker.sync_count == 0

    sync_env = _environment(tmp_path / "sync")
    sync_env.broker.fail_sync = RemoteBrokerError(
        "development_source_reconciliation_required",
        "destination uncertain",
        request_id=None,
    )
    with pytest.raises(McpWorkspaceDevelopmentError, match="deployment_ambiguous"):
        sync_env.development.deploy(
            client_id=sync_env.client_id,
            request_id=uuid4(),
            workspace_id=sync_env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert sync_env.broker.start_count == 0

    readiness_env = _environment(tmp_path / "readiness")
    readiness_env.development._readiness_factory = lambda _broker, _profile: (
        SimpleNamespace(
            wait_until_usable=lambda _target: SimpleNamespace(
                usability_state=UsabilityState.NOT_USABLE
            )
        )
    )
    with pytest.raises(McpWorkspaceDevelopmentError, match="deployment_ambiguous"):
        readiness_env.development.deploy(
            client_id=readiness_env.client_id,
            request_id=uuid4(),
            workspace_id=readiness_env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert readiness_env.broker.state is ContainerRuntimeState.STOPPED

    inventory_env = _environment(tmp_path / "inventory")
    inventory_env.development._inventory = cast(
        Any,
        SimpleNamespace(
            get=lambda _target: ManagedInstanceAddonInventory(
                instance_id=TARGET_ID,
                observed_at=NOW,
                kodi_release="21.3",
                query_status=AddonInventoryQueryStatus.AVAILABLE,
                complete=True,
                items=[],
            )
        ),
    )
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="deployment_verification_failed"
    ):
        inventory_env.development.deploy(
            client_id=inventory_env.client_id,
            request_id=uuid4(),
            workspace_id=inventory_env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert inventory_env.broker.state is ContainerRuntimeState.STOPPED

    disabled_env = _environment(tmp_path / "disabled")
    disabled_env.development._inventory = cast(
        Any,
        SimpleNamespace(
            get=lambda _target: ManagedInstanceAddonInventory(
                instance_id=TARGET_ID,
                observed_at=NOW,
                kodi_release="21.3",
                query_status=AddonInventoryQueryStatus.AVAILABLE,
                complete=True,
                items=[
                    KodiAddonInventoryItem(
                        addon_id="plugin.video.slice4",
                        name="Slice 4",
                        version="1.0.0",
                        enabled=False,
                        kodi_type="xbmc.python.pluginsource",
                        provenance=KodiAddonProvenance(
                            classification=KodiAddonProvenanceClassification.UNKNOWN,
                            evidence_status=(KodiAddonProvenanceEvidenceStatus.UNKNOWN),
                        ),
                    )
                ],
            )
        ),
    )
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="deployment_verification_failed"
    ):
        disabled_env.development.deploy(
            client_id=disabled_env.client_id,
            request_id=uuid4(),
            workspace_id=disabled_env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert disabled_env.broker.state is ContainerRuntimeState.STOPPED


def test_process_restart_after_stop_terminalizes_without_redispatch(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)

    def crash(phase: str) -> None:
        if phase == "after_stop":
            raise KeyboardInterrupt

    env.development._failure = crash
    request_id = uuid4()
    with pytest.raises(KeyboardInterrupt):
        env.development.deploy(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert (env.broker.stop_count, env.broker.sync_count) == (1, 0)
    assert (
        env.development.retention_blocker(env.workspace_id, 0)
        == "workspace_development_operation_unresolved"
    )
    assert env.development.sweep_startup() == 1
    with pytest.raises(McpWorkspaceDevelopmentError, match="deployment_ambiguous"):
        env.development.deploy(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert (env.broker.stop_count, env.broker.sync_count) == (1, 0)
