from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_inventory import (
    AddonInventoryQueryStatus,
    KodiAddonInventoryItem,
    KodiAddonProvenance,
    KodiAddonProvenanceClassification,
    KodiAddonProvenanceEvidenceStatus,
    ManagedInstanceAddonInventory,
)
from kodi_lab.application.agent_workspace_promotions import (
    AgentWorkspacePromotionService,
)
from kodi_lab.application.agent_workspaces import AgentWorkspaceService
from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.application.mcp_promotion_requests import McpPromotionRequestService
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
from kodi_lab.domain import (
    DevelopmentSource,
    DevelopmentSourceKind,
    ExternalAdministrativeState,
    ExternalBridgePairingState,
    ExternalBridgeReachabilityState,
)
from kodi_lab.kodi.addons import (
    KodiAddonDetailsRecord,
    KodiAddonInventoryRecord,
    KodiAddonInventoryResult,
    KodiAddonSmokeResult,
    KodiAddonSmokeTimeoutError,
)
from kodi_lab.kodi.bridge import BridgeAddonTree, BridgeDeploymentReceipt
from kodi_lab.kodi.readiness import KodiApplicationIdentity, UsabilityState
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    AgentWorkspacePromotionPlanRow,
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    ExternalBridgeDeploymentReceiptRow,
    ExternalBridgePairingRow,
    ExternalDevelopmentSourceBaselineRow,
    ExternalKodiTargetRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceRow,
    McpAgentWorkspaceBindingRow,
    McpDevelopmentRequestRow,
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
EXTERNAL_TARGET_ID = UUID("72000000-0000-4000-8000-000000000006")
EXTERNAL_BRIDGE_ID = UUID("72000000-0000-4000-8000-000000000007")


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
class FakeExternalDevelopmentBridge:
    manifest_digest: str
    file_count: int
    total_bytes: int
    version: str = "1.0.0"
    enabled: bool = True
    replacement_count: int = 0
    receipt: BridgeDeploymentReceipt | None = None
    pre_commit_hook: Callable[[], None] | None = None

    def inspect_addon(self, target_id: UUID, addon_id: str) -> BridgeAddonTree:
        assert target_id == EXTERNAL_TARGET_ID
        assert addon_id == "plugin.video.slice4"
        return BridgeAddonTree(
            addon_id=addon_id,
            addon_version=self.version,
            manifest_digest=self.manifest_digest,
            file_count=self.file_count,
            total_bytes=self.total_bytes,
            enabled=self.enabled,
        )

    def deploy_workspace(
        self, target_id: UUID, **kwargs: Any
    ) -> BridgeDeploymentReceipt:
        assert target_id == EXTERNAL_TARGET_ID
        operation_id = cast(UUID, kwargs["operation_id"])
        request_id = cast(UUID, kwargs["request_id"])
        if self.receipt is not None and self.receipt.deployment_id == operation_id:
            return self.receipt
        before_commit = kwargs["before_commit"]
        if self.pre_commit_hook is not None:
            self.pre_commit_hook()
        before_commit()
        manifest = kwargs["manifest"]
        self.replacement_count += 1
        self.manifest_digest = manifest.digest
        self.file_count = sum(entry.entry_type == "file" for entry in manifest.entries)
        self.total_bytes = manifest.included_bytes
        self.version = str(kwargs["addon_version"])
        self.receipt = BridgeDeploymentReceipt(
            receipt_id=uuid4(),
            deployment_id=operation_id,
            request_id=request_id,
            bridge_id=EXTERNAL_BRIDGE_ID,
            addon_id="plugin.video.slice4",
            addon_version=self.version,
            generation=int(kwargs["generation"]),
            manifest_digest=self.manifest_digest,
            file_count=self.file_count,
            total_bytes=self.total_bytes,
            state="succeeded",
            classification="deployed",
            enabled_state="enabled",
            inventory_verified=True,
            completed_at=NOW,
        )
        return self.receipt

    def deployment_receipt(
        self, target_id: UUID, operation_id: UUID
    ) -> BridgeDeploymentReceipt:
        assert target_id == EXTERNAL_TARGET_ID
        assert self.receipt is not None and self.receipt.deployment_id == operation_id
        return self.receipt


@dataclass
class FakeExternalTargetAdapters:
    bridge: FakeExternalDevelopmentBridge
    execution_count: int = 0

    def addon_inventory(self, target_id: UUID) -> ManagedInstanceAddonInventory:
        tree = self.bridge.inspect_addon(target_id, "plugin.video.slice4")
        return ManagedInstanceAddonInventory(
            instance_id=target_id,
            observed_at=NOW,
            kodi_release="21.3",
            query_status=AddonInventoryQueryStatus.AVAILABLE,
            complete=True,
            items=[
                KodiAddonInventoryItem(
                    addon_id=tree.addon_id,
                    name="Slice 4",
                    version=tree.addon_version,
                    enabled=tree.enabled,
                    kodi_type="xbmc.python.pluginsource",
                    provenance=KodiAddonProvenance(
                        classification=KodiAddonProvenanceClassification.UNKNOWN,
                        evidence_status=KodiAddonProvenanceEvidenceStatus.UNKNOWN,
                    ),
                )
            ],
        )

    def fixed_execute(
        self, target_id: UUID, addon_id: str, invocation_id: UUID
    ) -> KodiAddonSmokeResult:
        assert target_id == EXTERNAL_TARGET_ID
        assert addon_id == "plugin.video.slice4"
        assert invocation_id
        self.execution_count += 1
        return KodiAddonSmokeResult(item_count=1, duration_ms=3)


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


def _restarted_development(
    env: Environment,
    tmp_path: Path,
    *,
    max_per_client: int = 10_000,
    max_global: int = 100_000,
) -> McpWorkspaceDevelopmentService:
    return McpWorkspaceDevelopmentService(
        env.sessions,
        env.broker,
        FakeInventory(),  # type: ignore[arg-type]
        env.workspaces,
        data_root=tmp_path / "data",
        clock=lambda: NOW,
        readiness_factory=lambda _broker, _profile: SimpleNamespace(
            wait_until_usable=lambda _target: SimpleNamespace(
                usability_state=UsabilityState.USABLE
            )
        ),
        deployment_rate=BoundedIntervalRateLimiter(300, clock=lambda: 2000.0),
        execution_rate=BoundedIntervalRateLimiter(60, clock=lambda: 2000.0),
        inventory_adapter_factory=lambda _observation, _profile: InventoryAdapter(),
        details_adapter_factory=lambda _observation, _profile: DetailsAdapter(),
        smoke_adapter_factory=lambda _observation, _profile: env.smoke,
        max_per_client=max_per_client,
        max_global=max_global,
    )


def _configure_external_environment(
    env: Environment,
) -> tuple[FakeExternalDevelopmentBridge, FakeExternalTargetAdapters]:
    manifest = env.workspaces.capture_registered_source_manifest(SOURCE_ID)
    files = [entry for entry in manifest.entries if entry.entry_type == "file"]
    with env.sessions.begin() as session:
        session.add(
            TargetRow(
                id=str(EXTERNAL_TARGET_ID),
                name="Slice 4 external",
                target_type="external_development",
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.flush()
        session.add(
            ExternalKodiTargetRow(
                target_id=str(EXTERNAL_TARGET_ID),
                protocol_kind="jsonrpc_http",
                host="127.0.0.1",
                port=8080,
                authentication_mode="none",
                secret_reference=None,
                configured_capabilities_json=json.dumps(
                    ["fixed_execution", "target_inspect", "workspace_deployment"]
                ),
                verified_capabilities_json=json.dumps(
                    ["fixed_execution", "target_inspect"]
                ),
                administrative_state="enabled",
                verification_state="verified",
                observation_state="reachable",
                verified_addresses_json='["127.0.0.1"]',
                verified_kodi_name="Kodi",
                verified_kodi_version="21.3",
                observed_kodi_name="Kodi",
                observed_kodi_version="21.3",
                observed_protocol_version="13.0.0",
                pending_observation_id=None,
                created_at=NOW_TEXT,
                verified_at=NOW_TEXT,
                deactivated_at=None,
                last_observed_at=NOW_TEXT,
            )
        )
        session.flush()
        session.add(
            ExternalBridgePairingRow(
                target_id=str(EXTERNAL_TARGET_ID),
                bridge_port=8765,
                pairing_state="paired",
                reachability_state="reachable",
                pairing_token_reference=None,
                bridge_secret_reference=str(uuid4()),
                pairing_token_expires_at=None,
                observed_bridge_id=str(EXTERNAL_BRIDGE_ID),
                observed_protocol_version="1",
                observed_addon_id="service.kodi_lab_bridge",
                observed_addon_version="1.1.0",
                observed_kodi_version="21.3",
                observed_build_fingerprint="a" * 64,
                observed_capabilities_json='["screenshot","workspace_deployment"]',
                pinned_bridge_id=str(EXTERNAL_BRIDGE_ID),
                pinned_protocol_version="1",
                pinned_addon_id="service.kodi_lab_bridge",
                pinned_addon_version="1.1.0",
                pinned_kodi_version="21.3",
                pinned_build_fingerprint="a" * 64,
                verified_capabilities_json='["screenshot","workspace_deployment"]',
                endpoint_revision_digest="b" * 64,
                token_generated_at=NOW_TEXT,
                pairing_attempted_at=NOW_TEXT,
                paired_at=NOW_TEXT,
                verified_at=NOW_TEXT,
                revoked_at=None,
            )
        )
    bridge = FakeExternalDevelopmentBridge(
        manifest_digest=manifest.digest,
        file_count=len(files),
        total_bytes=manifest.included_bytes,
    )
    targets = FakeExternalTargetAdapters(bridge)
    env.development.configure_external(
        bridges=cast(Any, bridge), targets=cast(Any, targets)
    )
    env.development._deployment_rate = BoundedIntervalRateLimiter(0)
    env.development._execution_rate = BoundedIntervalRateLimiter(0)
    env.development.establish_external_baseline(
        target_id=EXTERNAL_TARGET_ID, source_id=SOURCE_ID
    )
    return bridge, targets


def test_external_deploy_execute_replay_and_drift_invalidation(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    bridge, targets = _configure_external_environment(env)
    edit = env.workspaces.replace_text_file(
        env.workspace_id,
        expected_generation=0,
        relative_path="main.py",
        content="print('external')\n",
    )
    assert edit.generation == 1
    env.development.record_validation(
        workspace_id=env.workspace_id,
        source_id=SOURCE_ID,
        generation=1,
        addon_id="plugin.video.slice4",
        addon_version="1.0.0",
        extension_types=("xbmc.python.pluginsource",),
        validated_at=NOW,
    )
    deploy_request = uuid4()
    first = env.development.deploy(
        client_id=env.client_id,
        request_id=deploy_request,
        workspace_id=env.workspace_id,
        expected_generation=1,
        target_id=EXTERNAL_TARGET_ID,
    )
    replay = env.development.deploy(
        client_id=env.client_id,
        request_id=deploy_request,
        workspace_id=env.workspace_id,
        expected_generation=1,
        target_id=EXTERNAL_TARGET_ID,
    )
    assert replay == first
    assert bridge.replacement_count == 1
    execute_request = uuid4()
    executed = env.development.execute(
        client_id=env.client_id,
        request_id=execute_request,
        workspace_id=env.workspace_id,
        expected_generation=1,
        target_id=EXTERNAL_TARGET_ID,
    )
    execute_replay = env.development.execute(
        client_id=env.client_id,
        request_id=execute_request,
        workspace_id=env.workspace_id,
        expected_generation=1,
        target_id=EXTERNAL_TARGET_ID,
    )
    assert execute_replay == executed
    assert targets.execution_count == 1
    promotions = AgentWorkspacePromotionService(
        env.sessions,
        data_root=tmp_path / "data",
        development_root=tmp_path / "sources",
        verification_publisher=lambda _session, _operation: uuid4(),
        clock=lambda: NOW,
    )
    promotion_requests = McpPromotionRequestService(
        env.sessions, promotions, clock=lambda: NOW
    )
    promotion_request_id = uuid4()
    promotion = promotion_requests.request(
        client_id=env.client_id,
        request_id=promotion_request_id,
        workspace_id=env.workspace_id,
        expected_generation=1,
        target_id=EXTERNAL_TARGET_ID,
    )
    promotion_replay = promotion_requests.request(
        client_id=env.client_id,
        request_id=promotion_request_id,
        workspace_id=env.workspace_id,
        expected_generation=1,
        target_id=EXTERNAL_TARGET_ID,
    )
    assert promotion_replay == promotion
    assert promotion.review_state == "reviewable"
    with env.sessions() as session:
        plan = session.get(
            AgentWorkspacePromotionPlanRow, str(promotion.promotion_plan_id)
        )
        assert plan is not None
        assert plan.target_class == "external_development"
        assert plan.target_bridge_id == str(EXTERNAL_BRIDGE_ID)
    with env.sessions.begin() as session:
        deployment_row = session.get(
            McpWorkspaceDeploymentOperationRow, str(first.deployment_id)
        )
        receipt_row = session.get(
            ExternalBridgeDeploymentReceiptRow, str(first.deployment_id)
        )
        assert deployment_row is not None and receipt_row is not None
        deployment_row.state = "reconciliation_required"
        deployment_row.failure_code = "deployment_ambiguous"
        receipt_row.state = "ambiguous"
        receipt_row.classification = "deployment_ambiguous"
    restarted = _restarted_development(env, tmp_path)
    restarted.configure_external(bridges=cast(Any, bridge), targets=cast(Any, targets))
    assert restarted.reconcile_external_deployment(first.deployment_id) == "succeeded"
    assert bridge.replacement_count == 1
    bridge.manifest_digest = "sha256:" + "f" * 64
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="target_provenance_mismatch"
    ):
        env.development.deploy(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=1,
            target_id=EXTERNAL_TARGET_ID,
        )
    with env.sessions() as session:
        baseline = session.get(
            ExternalDevelopmentSourceBaselineRow,
            {
                "target_id": str(EXTERNAL_TARGET_ID),
                "development_source_id": str(SOURCE_ID),
            },
        )
        assert baseline is not None and baseline.state == "stale"

    source_manifest = env.workspaces.capture_registered_source_manifest(SOURCE_ID)
    bridge.manifest_digest = source_manifest.digest
    bridge.file_count = sum(
        entry.entry_type == "file" for entry in source_manifest.entries
    )
    bridge.total_bytes = source_manifest.included_bytes
    env.development._clock = lambda: NOW + timedelta(seconds=10)
    restored = env.development.establish_external_baseline(
        target_id=EXTERNAL_TARGET_ID, source_id=SOURCE_ID
    )
    assert restored.state == "current"
    recovered = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=1,
        target_id=EXTERNAL_TARGET_ID,
    )
    assert recovered.state == "succeeded"
    assert bridge.replacement_count == 2


@pytest.mark.parametrize("revoked", [False, True])
def test_external_disable_or_bridge_revoke_before_commit_denies_mutation(
    tmp_path: Path, revoked: bool
) -> None:
    env = _environment(tmp_path)
    bridge, _targets = _configure_external_environment(env)

    def revoke_authority() -> None:
        with env.sessions.begin() as session:
            if revoked:
                pairing = session.get(ExternalBridgePairingRow, str(EXTERNAL_TARGET_ID))
                assert pairing is not None
                pairing.pairing_state = ExternalBridgePairingState.REVOKED
                pairing.reachability_state = ExternalBridgeReachabilityState.UNKNOWN
                pairing.bridge_secret_reference = None
                pairing.verified_capabilities_json = "[]"
            else:
                external = session.get(ExternalKodiTargetRow, str(EXTERNAL_TARGET_ID))
                assert external is not None
                external.administrative_state = ExternalAdministrativeState.DISABLED

    bridge.pre_commit_hook = revoke_authority
    with pytest.raises(McpWorkspaceDevelopmentError, match="deployment_not_allowed"):
        env.development.deploy(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=EXTERNAL_TARGET_ID,
        )
    assert bridge.replacement_count == 0


def test_external_terminal_receipt_prunes_only_with_coherent_phase12_evidence(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)
    _bridge, _targets = _configure_external_environment(env)
    request_id = uuid4()
    deployed = env.development.deploy(
        client_id=env.client_id,
        request_id=request_id,
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=EXTERNAL_TARGET_ID,
    )
    env.workspaces.replace_text_file(
        env.workspace_id,
        expected_generation=0,
        relative_path="main.py",
        content="print('next generation')\n",
    )
    old = (
        (NOW - timedelta(days=31))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    with env.sessions.begin() as session:
        session.execute(
            update(McpDevelopmentRequestRow)
            .where(McpDevelopmentRequestRow.request_id == str(request_id))
            .values(created_at=old, updated_at=old)
        )
        session.execute(
            update(McpWorkspaceDeploymentOperationRow)
            .where(McpWorkspaceDeploymentOperationRow.id == str(deployed.deployment_id))
            .values(completed_at=old)
        )
        session.execute(
            update(ExternalBridgeDeploymentReceiptRow)
            .where(
                ExternalBridgeDeploymentReceiptRow.operation_id
                == str(deployed.deployment_id)
            )
            .values(completed_at=old)
        )
        env.development._prune_and_require_capacity(session, env.client_id, NOW)
        assert (
            session.scalar(
                select(McpDevelopmentRequestRow).where(
                    McpDevelopmentRequestRow.request_id == str(request_id)
                )
            )
            is None
        )
        assert (
            session.get(McpWorkspaceDeploymentOperationRow, str(deployed.deployment_id))
            is None
        )
        assert (
            session.get(ExternalBridgeDeploymentReceiptRow, str(deployed.deployment_id))
            is None
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


def test_long_horizon_ambiguous_deployment_survives_pruning_and_restart_replay(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)

    def fail(phase: str) -> None:
        if phase == "after_stop":
            raise RuntimeError("lost after deployment mutation marker")

    env.development._failure = fail
    request_id = uuid4()
    with pytest.raises(McpWorkspaceDevelopmentError, match="deployment_ambiguous"):
        env.development.deploy(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    before = (env.broker.stop_count, env.broker.sync_count, env.broker.start_count)
    old = (
        (NOW - timedelta(days=31))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    with env.sessions.begin() as session:
        session.execute(
            update(McpDevelopmentRequestRow)
            .where(McpDevelopmentRequestRow.request_id == str(request_id))
            .values(created_at=old, updated_at=old)
        )
    restarted = _restarted_development(env, tmp_path)
    with env.sessions.begin() as session:
        restarted._prune_and_require_capacity(session, env.client_id, NOW)
        assert session.scalar(select(McpDevelopmentRequestRow)) is not None
    with pytest.raises(McpWorkspaceDevelopmentError, match="deployment_ambiguous"):
        restarted.deploy(
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
    ) == before


def test_long_horizon_ambiguous_execution_survives_pruning_and_restart_replay(
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
            raise RuntimeError("lost after execution dispatch marker")

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
    old = (
        (NOW - timedelta(days=31))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    with env.sessions.begin() as session:
        session.execute(
            update(McpDevelopmentRequestRow)
            .where(McpDevelopmentRequestRow.request_id == str(request_id))
            .values(created_at=old, updated_at=old)
        )
    restarted = _restarted_development(env, tmp_path)
    with env.sessions.begin() as session:
        restarted._prune_and_require_capacity(session, env.client_id, NOW)
        assert (
            session.scalar(
                select(func.count())
                .select_from(McpDevelopmentRequestRow)
                .where(McpDevelopmentRequestRow.request_id == str(request_id))
            )
            == 1
        )
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_ambiguous"):
        restarted.execute(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert env.smoke.count == 0


def test_old_terminal_deployment_and_execution_mappings_are_pruned(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)
    deployment_request = uuid4()
    execution_request = uuid4()
    env.development.deploy(
        client_id=env.client_id,
        request_id=deployment_request,
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=TARGET_ID,
    )
    env.development.execute(
        client_id=env.client_id,
        request_id=execution_request,
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=TARGET_ID,
    )
    old = (
        (NOW - timedelta(days=31))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    with env.sessions.begin() as session:
        session.execute(
            update(McpDevelopmentRequestRow).values(created_at=old, updated_at=old)
        )
        env.development._prune_and_require_capacity(session, env.client_id, NOW)
        assert (
            session.scalar(select(func.count()).select_from(McpDevelopmentRequestRow))
            == 0
        )


@pytest.mark.parametrize(
    "contradictory_evidence",
    [
        {"target_stopped": True},
        {"inventory_classification": "unknown"},
        {"source_manifest_digest": "sha256:" + "9" * 64},
    ],
    ids=("target-stopped", "inventory-checked", "sync-digest"),
)
def test_malformed_pre_mutation_deployment_failure_survives_restart_pruning(
    tmp_path: Path, contradictory_evidence: dict[str, object]
) -> None:
    env = _environment(tmp_path)
    request_id = uuid4()
    operation, created = env.development._claim_deployment(
        env.client_id, request_id, env.workspace_id, 0, TARGET_ID
    )
    assert created is True
    env.development._finish_deployment(operation.id, "failed", "deployment_interrupted")
    old = (
        (NOW - timedelta(days=31))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    with env.sessions.begin() as session:
        session.execute(
            update(McpWorkspaceDeploymentOperationRow)
            .where(McpWorkspaceDeploymentOperationRow.id == operation.id)
            .values(**contradictory_evidence)
        )
        session.execute(
            update(McpDevelopmentRequestRow)
            .where(McpDevelopmentRequestRow.request_id == str(request_id))
            .values(created_at=old, updated_at=old)
        )

    restarted = _restarted_development(env, tmp_path)
    with env.sessions.begin() as session:
        restarted._prune_and_require_capacity(session, env.client_id, NOW)
        assert (
            session.scalar(
                select(func.count())
                .select_from(McpDevelopmentRequestRow)
                .where(McpDevelopmentRequestRow.request_id == str(request_id))
            )
            == 1
        )


def test_incomplete_deployment_success_evidence_is_retained(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    request_id = uuid4()
    env.development.deploy(
        client_id=env.client_id,
        request_id=request_id,
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=TARGET_ID,
    )
    old = (
        (NOW - timedelta(days=31))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    with env.sessions.begin() as session:
        session.execute(
            update(McpWorkspaceDeploymentOperationRow)
            .where(McpWorkspaceDeploymentOperationRow.request_id == str(request_id))
            .values(target_stopped=False)
        )
        session.execute(
            update(McpDevelopmentRequestRow)
            .where(McpDevelopmentRequestRow.request_id == str(request_id))
            .values(created_at=old, updated_at=old)
        )
        env.development._prune_and_require_capacity(session, env.client_id, NOW)
        assert session.scalar(select(McpDevelopmentRequestRow)) is not None


def test_malformed_pre_dispatch_execution_failure_and_incomplete_success_are_retained(
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
    malformed_request = uuid4()
    operation, created = env.development._claim_execution(
        env.client_id, malformed_request, env.workspace_id, 0, TARGET_ID
    )
    assert created is True
    env.development._finish_execution(operation.id, "failed", "execution_interrupted")
    success_request = uuid4()
    env.development.execute(
        client_id=env.client_id,
        request_id=success_request,
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=TARGET_ID,
    )
    old = (
        (NOW - timedelta(days=31))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    with env.sessions.begin() as session:
        session.execute(
            update(McpWorkspaceExecutionOperationRow)
            .where(McpWorkspaceExecutionOperationRow.id == operation.id)
            .values(execution_classification="ambiguous")
        )
        session.execute(
            update(McpWorkspaceExecutionOperationRow)
            .where(McpWorkspaceExecutionOperationRow.request_id == str(success_request))
            .values(item_count=None)
        )
        session.execute(
            update(McpDevelopmentRequestRow)
            .where(
                McpDevelopmentRequestRow.request_id.in_(
                    (str(malformed_request), str(success_request))
                )
            )
            .values(created_at=old, updated_at=old)
        )

    restarted = _restarted_development(env, tmp_path)
    with env.sessions.begin() as session:
        restarted._prune_and_require_capacity(session, env.client_id, NOW)
        retained = set(
            session.scalars(
                select(McpDevelopmentRequestRow.request_id).where(
                    McpDevelopmentRequestRow.request_id.in_(
                        (str(malformed_request), str(success_request))
                    )
                )
            )
        )
    assert retained == {str(malformed_request), str(success_request)}


def test_terminal_operation_cleanup_is_bounded_and_rejects_malformed_evidence(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)
    canonical, created = env.development._claim_deployment(
        env.client_id, uuid4(), env.workspace_id, 0, TARGET_ID
    )
    assert created is True
    env.development._finish_deployment(canonical.id, "failed", "deployment_interrupted")
    malformed, created = env.development._claim_deployment(
        env.client_id, uuid4(), env.workspace_id, 0, TARGET_ID
    )
    assert created is True
    env.development._finish_deployment(malformed.id, "failed", "deployment_interrupted")
    old = (
        (NOW - timedelta(days=31))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    with env.sessions.begin() as session:
        session.execute(delete(McpDevelopmentRequestRow))
        session.execute(
            update(McpWorkspaceDeploymentOperationRow)
            .where(
                McpWorkspaceDeploymentOperationRow.id.in_((canonical.id, malformed.id))
            )
            .values(completed_at=old)
        )
        session.execute(
            update(McpWorkspaceDeploymentOperationRow)
            .where(McpWorkspaceDeploymentOperationRow.id == malformed.id)
            .values(target_stopped=True)
        )
        env.development._prune_and_require_capacity(session, env.client_id, NOW)
        retained = set(session.scalars(select(McpWorkspaceDeploymentOperationRow.id)))
    assert canonical.id not in retained
    assert malformed.id in retained


def test_protected_request_consumes_capacity_until_safely_terminalized(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)
    capped = _restarted_development(env, tmp_path, max_per_client=1, max_global=1)
    first_request = uuid4()
    operation, created = capped._claim_deployment(
        env.client_id, first_request, env.workspace_id, 0, TARGET_ID
    )
    assert created is True
    old = (
        (NOW - timedelta(days=31))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    capped._finish_deployment(operation.id, "failed", "deployment_interrupted")
    with env.sessions.begin() as session:
        session.execute(
            update(McpWorkspaceDeploymentOperationRow)
            .where(McpWorkspaceDeploymentOperationRow.id == operation.id)
            .values(target_stopped=True)
        )
        session.execute(
            update(McpDevelopmentRequestRow)
            .where(McpDevelopmentRequestRow.request_id == str(first_request))
            .values(created_at=old, updated_at=old)
        )
        with pytest.raises(McpWorkspaceDevelopmentError, match="request_id_conflict"):
            capped._prune_and_require_capacity(session, env.client_id, NOW)
    with env.sessions() as session:
        assert session.scalar(select(McpDevelopmentRequestRow)) is not None

    with env.sessions.begin() as session:
        session.execute(
            update(McpWorkspaceDeploymentOperationRow)
            .where(McpWorkspaceDeploymentOperationRow.id == operation.id)
            .values(target_stopped=False)
        )
        capped._prune_and_require_capacity(session, env.client_id, NOW)
        assert session.scalar(select(McpDevelopmentRequestRow)) is None
    replacement, created = capped._claim_deployment(
        env.client_id, uuid4(), env.workspace_id, 0, TARGET_ID
    )
    assert created is True
    capped._finish_deployment(replacement.id, "failed", "deployment_interrupted")
