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
    WorkspaceExecutionKind,
    WorkspaceExecutionResult,
)
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationAction,
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
from kodi_lab.broker.kodi_logs import KodiLogWindowError
from kodi_lab.broker.operations import (
    CaptureSmokeLogResult,
    CopySyncAttachedDevelopmentSourceResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    ReadSmokeLogResult,
    VerifyAttachedDevelopmentSourceDestinationResult,
)
from kodi_lab.broker.smoke_diagnostics import SqliteSmokeLogAuthorityResolver
from kodi_lab.domain import (
    DevelopmentSource,
    DevelopmentSourceKind,
    ExternalAdministrativeState,
    ExternalBridgePairingState,
    ExternalBridgeReachabilityState,
    InstanceType,
    TargetType,
)
from kodi_lab.domain.smoke_diagnostics import (
    AddonExceptionDiagnostic,
    SmokeDiagnosticStatus,
)
from kodi_lab.kodi.addons import (
    KodiAddonDetailsRecord,
    KodiAddonInventoryRecord,
    KodiAddonInventoryResult,
    KodiAddonSmokeAdapter,
    KodiAddonSmokeJsonRpcError,
    KodiAddonSmokeResult,
    KodiAddonSmokeTimeoutError,
    KodiJsonRpcDiagnostic,
    KodiReportedAddonDependency,
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
    ManagedInstanceLifecycleOperationRow,
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
    def __init__(self, kodi_release: str = "21.3") -> None:
        self.kodi_release = kodi_release

    def get(self, target_id: UUID) -> ManagedInstanceAddonInventory:
        assert target_id == TARGET_ID
        return ManagedInstanceAddonInventory(
            instance_id=target_id,
            observed_at=NOW,
            kodi_release=self.kodi_release,
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
    capture_count: int = 0
    read_count: int = 0
    capture_failure: bool = False
    read_failure: bool = False
    smoke_diagnostic: AddonExceptionDiagnostic | None = None
    capture_hook: Callable[[str], str | None] | None = None

    def capture_smoke_log_cursor(self, *, execution_id: str) -> CaptureSmokeLogResult:
        self.capture_count += 1
        selected = (
            None if self.capture_hook is None else self.capture_hook(execution_id)
        )
        if self.capture_failure:
            raise RuntimeError("log unavailable")
        return CaptureSmokeLogResult(
            execution_id=execution_id,
            container_id=selected or CONTAINER_ID,
            cursor="cursor",
        )

    def read_smoke_log_window(
        self, *, execution_id: str, cursor: str
    ) -> ReadSmokeLogResult:
        assert cursor == "cursor"
        self.read_count += 1
        if self.read_failure:
            raise RuntimeError("parser unavailable")
        return ReadSmokeLogResult(
            execution_id=execution_id,
            container_id=CONTAINER_ID,
            diagnostic=self.smoke_diagnostic
            or AddonExceptionDiagnostic(status=SmokeDiagnosticStatus.NO_MATCH),
        )

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
    def __init__(
        self,
        kodi_release: str = "21.3",
        extra: KodiAddonInventoryRecord | None = None,
    ) -> None:
        self.kodi_release = kodi_release
        self.extra = extra

    def get_inventory(self) -> KodiAddonInventoryResult:
        major, minor = (int(part) for part in self.kodi_release.split("."))
        return KodiAddonInventoryResult(
            identity=KodiApplicationIdentity("Kodi", major, minor, "stable"),
            items=(
                KodiAddonInventoryRecord(
                    addon_id="plugin.video.slice4",
                    name="Slice 4",
                    version="1.0.0",
                    enabled=True,
                    kodi_type="xbmc.python.pluginsource",
                ),
            )
            + (() if self.extra is None else (self.extra,)),
        )


class DetailsAdapter:
    def __init__(self, dependency: KodiReportedAddonDependency | None = None) -> None:
        self.dependency = dependency

    def get_addon_details(self, addon_id: str) -> KodiAddonDetailsRecord:
        return KodiAddonDetailsRecord(
            addon_id=addon_id,
            name="Slice 4",
            version="1.0.0",
            enabled=True,
            kodi_type="xbmc.python.pluginsource",
            dependencies=() if self.dependency is None else (self.dependency,),
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


def _environment(
    tmp_path: Path,
    *,
    database_path: Path | None = None,
    kodi_release: str = "21.3",
    dependency: tuple[str, str | None] | None = None,
    dependency_inventory: KodiAddonInventoryRecord | None = None,
    reported_dependency: KodiReportedAddonDependency | None = None,
) -> Environment:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_database_engine(
        f"sqlite:///{database_path or (tmp_path / 'slice4.db')}"
    )
    if database_path is None:
        Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    source_root = tmp_path / "sources" / "plugin.video.slice4"
    source_root.mkdir(parents=True)
    dependency_xml = ""
    if dependency is not None:
        version = "" if dependency[1] is None else f' version="{dependency[1]}"'
        dependency_xml = (
            f'<requires><import addon="{dependency[0]}"{version} /></requires>'
        )
    (source_root / "addon.xml").write_text(
        f'<addon id="plugin.video.slice4" name="Slice 4" version="1.0.0">'
        f"{dependency_xml}"
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
                slug=f"kodi-{kodi_release}-slice4",
                kodi_version=kodi_release,
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
                kodi_version=kodi_release,
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
                expected_kodi_version=kodi_release,
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
        FakeInventory(kodi_release),  # type: ignore[arg-type]
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
        inventory_adapter_factory=lambda _observation, _profile: InventoryAdapter(
            kodi_release, dependency_inventory
        ),
        details_adapter_factory=lambda _observation, _profile: DetailsAdapter(
            reported_dependency
            if reported_dependency is not None
            else (
                None
                if dependency is None
                else KodiReportedAddonDependency(dependency[0], dependency[1], False)
            )
        ),
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
    *,
    configured_capabilities: list[str] | None = None,
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
                    configured_capabilities
                    if configured_capabilities is not None
                    else ["fixed_execution", "target_inspect", "workspace_deployment"]
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
    with pytest.raises(McpWorkspaceDevelopmentError) as unsupported:
        env.development.execute(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=1,
            target_id=EXTERNAL_TARGET_ID,
            execution_kind=WorkspaceExecutionKind.KODI_RPC_NEGATIVE_DIAGNOSTIC,
        )
    assert unsupported.value.code == "execution_unsupported"
    assert targets.execution_count == 0
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
    assert (env.broker.capture_count, env.broker.read_count) == (0, 0)
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
        assert (execution.kodi_error_code, execution.kodi_error_message) == (None, None)
        assert set(deployment.__dict__).isdisjoint(
            {"path", "content", "container_id", "raw_response", "log"}
        )


def _deploy_workspace(env: Environment) -> None:
    deployed = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=TARGET_ID,
    )
    assert deployed.state == "succeeded"


def _execute_workspace(
    env: Environment, *, request_id: UUID | None = None
) -> WorkspaceExecutionResult:
    return env.development.execute(
        client_id=env.client_id,
        request_id=request_id or uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=TARGET_ID,
    )


@pytest.mark.parametrize(
    ("code", "message"),
    [(-32601, "Method not found."), (-32602, "Invalid params.")],
)
def test_failed_execution_persists_safe_kodi_diagnostic_and_replays(
    tmp_path: Path, code: int, message: str
) -> None:
    env = _environment(tmp_path)
    _deploy_workspace(env)

    def failed(_addon_id: str) -> KodiAddonSmokeResult:
        env.smoke.count += 1
        raise KodiAddonSmokeJsonRpcError(
            8, diagnostic=KodiJsonRpcDiagnostic(code, message)
        )

    env.smoke.invoke = failed  # type: ignore[assignment]
    request_id = uuid4()
    for _ in range(2):
        with pytest.raises(McpWorkspaceDevelopmentError) as raised:
            _execute_workspace(env, request_id=request_id)
        assert raised.value.code == "execution_failed"
        assert raised.value.diagnostic == KodiJsonRpcDiagnostic(code, message)

    assert env.smoke.count == 1
    with env.sessions() as session:
        row = session.scalar(select(McpWorkspaceExecutionOperationRow))
        assert row is not None
        assert (row.state, row.failure_code, row.execution_classification) == (
            "failed",
            "execution_failed",
            "jsonrpc_error",
        )
        assert (row.kodi_error_code, row.kodi_error_message) == (code, message)


@pytest.mark.parametrize(
    "diagnostic,unavailable",
    (
        (
            AddonExceptionDiagnostic(
                status=SmokeDiagnosticStatus.MATCHED,
                exception_class="AttributeError",
                addon_file="default.py",
                line_number=23,
            ),
            False,
        ),
        (AddonExceptionDiagnostic(status=SmokeDiagnosticStatus.NO_MATCH), False),
        (AddonExceptionDiagnostic(status=SmokeDiagnosticStatus.REJECTED), False),
        (None, True),
    ),
)
def test_smoke_exception_is_atomic_and_replay_does_not_reobserve(
    tmp_path: Path,
    diagnostic: AddonExceptionDiagnostic | None,
    unavailable: bool,
) -> None:
    env = _environment(tmp_path)
    _deploy_workspace(env)
    env.broker.smoke_diagnostic = diagnostic
    env.broker.read_failure = unavailable

    def failed(_addon_id: str) -> KodiAddonSmokeResult:
        env.smoke.count += 1
        raise KodiAddonSmokeJsonRpcError(
            8, diagnostic=KodiJsonRpcDiagnostic(-32602, "Invalid params.")
        )

    env.smoke.invoke = failed  # type: ignore[assignment]
    request_id = uuid4()
    observed = (
        AddonExceptionDiagnostic(status=SmokeDiagnosticStatus.UNAVAILABLE)
        if unavailable
        else (
            diagnostic
            or AddonExceptionDiagnostic(status=SmokeDiagnosticStatus.NO_MATCH)
        )
    )
    for _ in range(2):
        with pytest.raises(McpWorkspaceDevelopmentError) as raised:
            _execute_workspace(env, request_id=request_id)
        assert raised.value.code == "execution_failed"
        assert raised.value.diagnostic == KodiJsonRpcDiagnostic(
            -32602, "Invalid params."
        )
        assert raised.value.addon_exception == observed
    assert (env.smoke.count, env.broker.capture_count, env.broker.read_count) == (
        1,
        1,
        1,
    )
    with env.sessions() as session:
        rows = list(session.scalars(select(McpWorkspaceExecutionOperationRow)))
        assert len(rows) == 1
        row = rows[0]
        assert (row.state, row.failure_code, row.kodi_error_code) == (
            "failed",
            "execution_failed",
            -32602,
        )
        assert (
            row.addon_exception_status,
            row.addon_exception_class,
            row.addon_exception_file,
            row.addon_exception_line,
            row.addon_exception_detail,
        ) == (
            observed.status.value,
            observed.exception_class,
            observed.addon_file,
            observed.line_number,
            None,
        )


def test_success_and_fixed_negative_never_read_smoke_logs(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    _deploy_workspace(env)
    _execute_workspace(env)
    assert (env.broker.capture_count, env.broker.read_count) == (1, 0)
    with env.sessions() as session:
        row = session.scalar(select(McpWorkspaceExecutionOperationRow))
        assert row is not None and row.addon_exception_status is None


def test_no_log_observation_before_dispatch_or_after_rate_limit(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_not_deployed"):
        _execute_workspace(env)
    assert (env.broker.capture_count, env.broker.read_count) == (0, 0)
    _deploy_workspace(env)
    _execute_workspace(env)
    with pytest.raises(McpWorkspaceDevelopmentError, match="rate_limited"):
        _execute_workspace(env)
    assert (env.broker.capture_count, env.broker.read_count) == (1, 0)


def test_target_failure_before_dispatch_has_no_log_observation(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    _deploy_workspace(env)
    env.broker.state = ContainerRuntimeState.STOPPED
    with pytest.raises(McpWorkspaceDevelopmentError, match="target_not_ready"):
        _execute_workspace(env)
    assert (env.broker.capture_count, env.broker.read_count) == (0, 0)


@pytest.mark.parametrize(
    "scenario",
    (
        "valid",
        "non_disposable",
        "external",
        "deployment_failed",
        "generation",
        "addon_identity",
        "runtime_label",
        "container_changed",
    ),
)
def test_broker_smoke_authority_rechecks_persisted_lineage_and_container(
    tmp_path: Path, scenario: str
) -> None:
    env = _environment(tmp_path)
    _deploy_workspace(env)
    selected_container = "d" * 64 if scenario == "container_changed" else CONTAINER_ID
    label_revision = (
        "e" * 36 if scenario == "runtime_label" else str(RUNTIME_REVISION_ID)
    )

    class Docker:
        def containers(self, **_kwargs: object) -> list[dict[str, str]]:
            return [{"Id": selected_container}]

        def inspect_container(self, _container_id: str) -> dict[str, object]:
            return {
                "Id": selected_container,
                "State": {"Running": True},
                "Config": {
                    "Labels": {
                        "io.kodi-lab.managed": "true",
                        "io.kodi-lab.instance-id": str(TARGET_ID),
                        "io.kodi-lab.runtime-revision-id": label_revision,
                    }
                },
            }

        def close(self) -> None:
            pass

    resolver = SqliteSmokeLogAuthorityResolver(env.sessions, Docker)
    outcomes: list[str] = []

    def check(execution_id: str) -> str | None:
        restore: tuple[type, str, object] | None = None
        if scenario in {
            "non_disposable",
            "external",
            "deployment_failed",
            "generation",
            "addon_identity",
        }:
            changes = {
                "non_disposable": (
                    ManagedInstanceRow,
                    "instance_type",
                    InstanceType.PERSISTENT,
                ),
                "external": (TargetRow, "target_type", TargetType.EXTERNAL_DEVELOPMENT),
                "deployment_failed": (
                    McpWorkspaceDeploymentOperationRow,
                    "state",
                    "failed",
                ),
                "generation": (McpWorkspaceDeploymentOperationRow, "generation", 1),
                "addon_identity": (
                    McpWorkspaceDeploymentOperationRow,
                    "addon_id",
                    "plugin.video.other",
                ),
            }
            model, name, value = changes[scenario]
            key = str(TARGET_ID) if model in {ManagedInstanceRow, TargetRow} else None
            with env.sessions.begin() as session:
                row = (
                    session.get(model, key)
                    if key is not None
                    else session.scalar(select(McpWorkspaceDeploymentOperationRow))
                )
                assert row is not None
                restore = (model, name, getattr(row, name))
                setattr(row, name, value)
        try:
            authority = resolver.resolve(execution_id)
            outcomes.append(authority.container_id)
            return authority.container_id
        except KodiLogWindowError:
            outcomes.append("rejected")
            return None
        finally:
            if restore is not None:
                restore_model, restore_name, restore_value = restore
                key = (
                    str(TARGET_ID)
                    if restore_model in {ManagedInstanceRow, TargetRow}
                    else None
                )
                with env.sessions.begin() as session:
                    row = (
                        session.get(restore_model, key)
                        if key is not None
                        else session.scalar(select(McpWorkspaceDeploymentOperationRow))
                    )
                    assert row is not None
                    setattr(row, restore_name, restore_value)

    env.broker.capture_hook = check
    _execute_workspace(env)
    assert outcomes == [
        selected_container if scenario in {"valid", "container_changed"} else "rejected"
    ]
    assert env.broker.read_count == 0


def test_fixed_negative_diagnostic_uses_real_adapter_and_replays_without_dispatch(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "kodi-lab-rpc-negative-diagnostic",
            "error": {
                "code": -32601,
                "message": "Method\nnot found token=secret-value",
                "data": {"secret": "unretained"},
            },
        }
    ).encode()

    class DiagnosticTransport:
        def __init__(self) -> None:
            self.requests: list[bytes] = []

        def post(
            self, *, host: str, port: int, path: str, body: bytes, timeout: float
        ) -> bytes:
            assert (host, port, path, timeout) == ("10.250.0.4", 8080, "/jsonrpc", 10.0)
            self.requests.append(body)
            return payload

    transport = DiagnosticTransport()
    env.development._smoke_factory = lambda observation, profile: KodiAddonSmokeAdapter(
        observation, profile, transport=transport
    )
    request_id = uuid4()
    with pytest.raises(McpWorkspaceDevelopmentError) as missing_deployment:
        env.development.execute(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
            execution_kind=WorkspaceExecutionKind.KODI_RPC_NEGATIVE_DIAGNOSTIC,
        )
    assert missing_deployment.value.code == "execution_not_deployed"
    assert transport.requests == []
    _deploy_workspace(env)
    with env.sessions() as session:
        instance = session.get(ManagedInstanceRow, str(TARGET_ID))
        target = session.get(TargetRow, str(TARGET_ID))
        assert instance is not None and target is not None
        original_revision = instance.runtime_state_revision
        original_projection = (
            target.availability,
            target.health,
            target.connection_state,
        )
        original_lifecycle_count = session.scalar(
            select(func.count()).select_from(ManagedInstanceLifecycleOperationRow)
        )
    for _ in range(2):
        with pytest.raises(McpWorkspaceDevelopmentError) as raised:
            env.development.execute(
                client_id=env.client_id,
                request_id=request_id,
                workspace_id=env.workspace_id,
                expected_generation=0,
                target_id=TARGET_ID,
                execution_kind=WorkspaceExecutionKind.KODI_RPC_NEGATIVE_DIAGNOSTIC,
            )
        assert raised.value.code == "execution_failed"
        assert raised.value.diagnostic == KodiJsonRpcDiagnostic(
            -32601, "Method not found [redacted]"
        )
    assert [json.loads(body) for body in transport.requests] == [
        {
            "jsonrpc": "2.0",
            "id": "kodi-lab-rpc-negative-diagnostic",
            "method": "KodiLab.NonexistentDiagnosticMethod",
        }
    ]
    with pytest.raises(McpWorkspaceDevelopmentError) as mismatched:
        env.development.execute(
            client_id=env.client_id,
            request_id=request_id,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert mismatched.value.code == "request_id_conflict"
    assert len(transport.requests) == 1
    with env.sessions() as session:
        operations = list(session.scalars(select(McpWorkspaceExecutionOperationRow)))
        assert len(operations) == 1
        row = operations[0]
        assert (row.request_id, row.state, row.execution_classification) == (
            str(request_id),
            "failed",
            "jsonrpc_error",
        )
        assert row.result_classification == "kodi_rpc_diagnostic_failed"
        assert row.kodi_error_code == -32601
        assert row.kodi_error_message == "Method not found [redacted]"
        instance = session.get(ManagedInstanceRow, str(TARGET_ID))
        target = session.get(TargetRow, str(TARGET_ID))
        assert instance is not None and target is not None
        assert instance.runtime_state_revision == original_revision
        assert (
            target.availability,
            target.health,
            target.connection_state,
        ) == original_projection
        assert (
            session.scalar(
                select(func.count()).select_from(ManagedInstanceLifecycleOperationRow)
            )
            == original_lifecycle_count
        )
        assert row.addon_exception_status is None
    assert (env.broker.capture_count, env.broker.read_count) == (0, 0)


def test_historical_style_failed_execution_replays_without_kodi_diagnostic(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)
    _deploy_workspace(env)

    def failed(_addon_id: str) -> KodiAddonSmokeResult:
        env.smoke.count += 1
        raise KodiAddonSmokeJsonRpcError(8)

    env.smoke.invoke = failed  # type: ignore[assignment]
    request_id = uuid4()
    for _ in range(2):
        with pytest.raises(McpWorkspaceDevelopmentError) as raised:
            _execute_workspace(env, request_id=request_id)
        assert raised.value.code == "execution_failed"
        assert raised.value.diagnostic is None
    assert env.smoke.count == 1
    with env.sessions() as session:
        row = session.scalar(select(McpWorkspaceExecutionOperationRow))
        assert row is not None
        assert (row.kodi_error_code, row.kodi_error_message) == (None, None)


def test_failed_execution_persists_only_sanitized_message(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    _deploy_workspace(env)

    def failed(_addon_id: str) -> KodiAddonSmokeResult:
        raise KodiAddonSmokeJsonRpcError(
            8,
            diagnostic=KodiJsonRpcDiagnostic(
                -32602, "Invalid\nparams: /home/kodi/.kodi token=secret-value"
            ),
        )

    env.smoke.invoke = failed  # type: ignore[assignment]
    with pytest.raises(McpWorkspaceDevelopmentError) as raised:
        _execute_workspace(env)
    assert raised.value.diagnostic is not None
    assert raised.value.diagnostic.message == ("Invalid params: [redacted] [redacted]")
    with env.sessions() as session:
        row = session.scalar(select(McpWorkspaceExecutionOperationRow))
        assert row is not None
        assert row.kodi_error_message == raised.value.diagnostic.message
        assert "secret-value" not in row.kodi_error_message


def test_execute_uses_verified_kodi_release_for_compatible_python_api(
    tmp_path: Path,
) -> None:
    env = _environment(
        tmp_path, kodi_release="20.5", dependency=("xbmc.python", "3.0.0")
    )
    _deploy_workspace(env)
    with env.sessions.begin() as session:
        session.execute(
            update(TargetRow)
            .where(TargetRow.id == str(TARGET_ID))
            .values(kodi_version="22.0")
        )

    executed = _execute_workspace(env)

    assert executed.state == "succeeded"
    assert env.smoke.count == 1


def test_execute_rejects_incompatible_python_api_on_verified_kodi_release(
    tmp_path: Path,
) -> None:
    env = _environment(
        tmp_path, kodi_release="20.5", dependency=("xbmc.python", "4.0.0")
    )
    _deploy_workspace(env)

    with pytest.raises(
        McpWorkspaceDevelopmentError, match="execution_unsupported"
    ) as caught:
        _execute_workspace(env)

    assert caught.value.reason == "dependency_incompatible"
    assert env.smoke.count == 0


def test_execute_rejects_python_api_when_kodi_release_is_unverified(
    tmp_path: Path,
) -> None:
    env = _environment(
        tmp_path, kodi_release="22.0", dependency=("xbmc.python", "3.0.0")
    )
    _deploy_workspace(env)

    with pytest.raises(
        McpWorkspaceDevelopmentError, match="execution_unsupported"
    ) as caught:
        _execute_workspace(env)

    assert caught.value.reason == "dependency_version_unverified"
    assert env.smoke.count == 0


@pytest.mark.parametrize(
    ("inventory", "reason"),
    [
        (None, "dependency_missing"),
        (
            KodiAddonInventoryRecord(
                "script.module.example", "Example", "1.0.0", False, "xbmc.python.module"
            ),
            "dependency_disabled",
        ),
    ],
)
def test_execute_reports_blocking_dependency_reason(
    tmp_path: Path,
    inventory: KodiAddonInventoryRecord | None,
    reason: str,
) -> None:
    env = _environment(
        tmp_path,
        dependency=("script.module.example", None),
        dependency_inventory=inventory,
    )
    _deploy_workspace(env)

    with pytest.raises(
        McpWorkspaceDevelopmentError, match="execution_unsupported"
    ) as caught:
        _execute_workspace(env)

    assert caught.value.reason == reason
    assert env.smoke.count == 0


def test_execute_reports_dependency_declaration_mismatch(tmp_path: Path) -> None:
    env = _environment(
        tmp_path,
        dependency=("script.module.example", None),
        reported_dependency=KodiReportedAddonDependency(
            "script.module.other", None, False
        ),
    )
    _deploy_workspace(env)

    with pytest.raises(
        McpWorkspaceDevelopmentError, match="execution_unsupported"
    ) as caught:
        _execute_workspace(env)

    assert caught.value.reason == "dependency_declaration_mismatch"
    assert env.smoke.count == 0


def test_execute_replay_retains_bounded_dependency_reason(tmp_path: Path) -> None:
    env = _environment(tmp_path, dependency=("script.module.example", None))
    _deploy_workspace(env)
    request_id = uuid4()

    for _ in range(2):
        with pytest.raises(
            McpWorkspaceDevelopmentError, match="execution_unsupported"
        ) as caught:
            _execute_workspace(env, request_id=request_id)
        assert caught.value.reason == "dependency_missing"

    with env.sessions() as session:
        operation = session.scalar(select(McpWorkspaceExecutionOperationRow))
        assert operation is not None
        assert operation.failure_code == "execution_unsupported"
        assert operation.result_classification == "dependency_missing"
    assert env.smoke.count == 0


def test_execute_preserves_non_python_builtin_dependency_behavior(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path, dependency=("xbmc.gui", None))
    _deploy_workspace(env)

    executed = _execute_workspace(env)

    assert executed.state == "succeeded"
    assert env.smoke.count == 1


def test_execute_still_requires_succeeded_deployment(tmp_path: Path) -> None:
    env = _environment(
        tmp_path, kodi_release="20.5", dependency=("xbmc.python", "3.0.0")
    )
    _deploy_workspace(env)
    with env.sessions.begin() as session:
        session.execute(
            update(McpWorkspaceDeploymentOperationRow)
            .where(
                McpWorkspaceDeploymentOperationRow.workspace_id == str(env.workspace_id)
            )
            .values(state="failed")
        )

    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_not_deployed"):
        _execute_workspace(env)

    assert env.smoke.count == 0


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
    with env.sessions() as session:
        row = session.scalar(select(McpWorkspaceExecutionOperationRow))
        assert row is not None
        assert (row.kodi_error_code, row.kodi_error_message) == (None, None)


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


def test_workspace_deployment_lost_sync_or_start_response_stays_blocked(
    tmp_path: Path,
) -> None:
    sync_env = _environment(tmp_path / "lost-sync")
    sync_env.broker.fail_sync = ClientTransportError("Broker sync response lost")
    with pytest.raises(McpWorkspaceDevelopmentError, match="deployment_ambiguous"):
        sync_env.development.deploy(
            client_id=sync_env.client_id,
            request_id=uuid4(),
            workspace_id=sync_env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert sync_env.broker.start_count == 0
    with sync_env.sessions() as session:
        row = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert row is not None and row.state == "reconciliation_required"

    blocked = ManagedInstanceReconciliationService(sync_env.sessions).status(TARGET_ID)
    assert blocked.blocking
    assert ReconciliationAction.STOP in blocked.blocked_actions

    start_env = _environment(tmp_path / "lost-start")

    def lose_start_response(**_kwargs: Any) -> object:
        start_env.broker.start_count += 1
        raise ClientTransportError("Broker Start response lost")

    start_env.broker.start_managed_container = lose_start_response  # type: ignore[method-assign]
    with pytest.raises(McpWorkspaceDevelopmentError, match="deployment_ambiguous"):
        start_env.development.deploy(
            client_id=start_env.client_id,
            request_id=uuid4(),
            workspace_id=start_env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert start_env.broker.stop_count == 1
    assert start_env.broker.start_count == 1
    with start_env.sessions() as session:
        row = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert row is not None and row.state == "reconciliation_required"


def test_workspace_deployment_known_pre_dispatch_failure_is_recoverable(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)

    def unavailable_stop(**_kwargs: Any) -> object:
        env.broker.stop_count += 1
        raise BrokerUnavailableError("Broker socket unavailable")

    env.broker.stop_managed_container = unavailable_stop  # type: ignore[method-assign]
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="broker_unavailable_before_dispatch"
    ):
        env.development.deploy(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    assert env.broker.sync_count == 0 and env.broker.start_count == 0
    with env.sessions() as session:
        row = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert row is not None and row.state == "failed"
    status = ManagedInstanceReconciliationService(env.sessions).status(TARGET_ID)
    assert not status.blocking


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
