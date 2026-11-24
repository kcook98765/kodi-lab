from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
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
    MANAGED_DEPLOYMENT_INTERVAL_SECONDS,
    BoundedIntervalRateLimiter,
    McpWorkspaceDevelopmentError,
    McpWorkspaceDevelopmentService,
    WorkspaceDeploymentResult,
    WorkspaceExecutionKind,
    WorkspaceExecutionResult,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.operations import (
    CaptureSmokeLogResult,
    CopySyncAttachedDevelopmentSourceResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    ReadSmokeLogResult,
    VerifyAttachedDevelopmentSourceDestinationResult,
)
from kodi_lab.development.deployment import expected_destination_digest
from kodi_lab.domain import (
    DevelopmentSource,
    DevelopmentSourceKind,
    ExternalAdministrativeState,
    ExternalBridgePairingState,
    ExternalBridgeReachabilityState,
    KodiReleaseIdentity,
)
from kodi_lab.domain.smoke_diagnostics import (
    AddonExceptionDiagnostic,
    SmokeDiagnosticStatus,
)
from kodi_lab.kodi.addons import (
    KodiAddonDetailsRecord,
    KodiAddonInventoryRecord,
    KodiAddonInventoryResult,
    KodiAddonSmokeAmbiguousError,
    KodiAddonSmokeResult,
    KodiAddonSmokeTimeoutError,
    KodiReportedAddonDependency,
)
from kodi_lab.kodi.bridge import BridgeAddonTree, BridgeDeploymentReceipt
from kodi_lab.kodi.readiness import (
    KodiApplicationIdentity,
)
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
    McpClientDevelopmentSourceGrantRow,
    McpClientTargetGrantRow,
    McpDevelopmentRequestRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceRepository,
)
from kodi_lab.workspace import capture_workspace_deployment_tree

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
    last_stop_kwargs: dict[str, Any] | None = None
    last_start_kwargs: dict[str, Any] | None = None
    verify_count: int = 0
    fail_sync: BaseException | None = None
    sync_result_classification: Literal["synchronized", "unchanged"] = "synchronized"
    sync_source_digest: str = "sha256:" + "1" * 64
    sync_destination_digest: str = "sha256:" + "2" * 64
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
        self.last_stop_kwargs = _kwargs
        self.state = ContainerRuntimeState.STOPPED
        return SimpleNamespace(
            container_id=CONTAINER_ID, runtime_state=ContainerRuntimeState.STOPPED
        )

    def start_managed_container(self, **_kwargs: Any) -> object:
        self.start_count += 1
        self.last_start_kwargs = _kwargs
        self.state = ContainerRuntimeState.RUNNING
        return SimpleNamespace(
            container_id=CONTAINER_ID, runtime_state=ContainerRuntimeState.RUNNING
        )

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
            result_classification=self.sync_result_classification,
            source_manifest_digest=self.sync_source_digest,
            verified_destination_digest=self.sync_destination_digest,
            observed_source_name="Slice 4",
            observed_source_version="1.0.0",
            new_count=0,
            changed_count=0 if self.sync_result_classification == "unchanged" else 1,
            unchanged_count=1,
            destination_only_count=0,
            transferred_bytes=0
            if self.sync_result_classification == "unchanged"
            else 8,
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
        release = KodiReleaseIdentity.parse(self.kodi_release)
        return KodiAddonInventoryResult(
            identity=KodiApplicationIdentity(
                "Kodi",
                release.major,
                release.minor,
                "releasecandidate"
                if release.channel.value == "rc"
                else release.channel.value,
                release.prerelease_ordinal,
            ),
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
    use_default_rates: bool = False,
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
                expected_kodi_version=(
                    f"{KodiReleaseIdentity.parse(kodi_release).major}."
                    f"{KodiReleaseIdentity.parse(kodi_release).minor}"
                ),
                expected_version_tag=KodiReleaseIdentity.parse(
                    kodi_release
                ).channel.value,
                expected_prerelease_ordinal=KodiReleaseIdentity.parse(
                    kodi_release
                ).prerelease_ordinal,
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
    manifest = capture_workspace_deployment_tree(
        tmp_path / "data", creation.workspace_id
    )
    broker = FakeBroker(
        sync_source_digest=manifest.digest,
        sync_destination_digest=expected_destination_digest(
            manifest, runtime_uid=1000, runtime_gid=1000
        ),
    )
    smoke = SmokeCounter()
    development = McpWorkspaceDevelopmentService(
        sessions,
        workspaces,
        data_root=tmp_path / "data",
        clock=lambda: NOW,
        deployment_rate=(
            None
            if use_default_rates
            else BoundedIntervalRateLimiter(300, clock=lambda: 1000.0)
        ),
        execution_rate=BoundedIntervalRateLimiter(60, clock=lambda: 1000.0),
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
        env.workspaces,
        data_root=tmp_path / "data",
        clock=lambda: NOW,
        deployment_rate=BoundedIntervalRateLimiter(300, clock=lambda: 2000.0),
        execution_rate=BoundedIntervalRateLimiter(60, clock=lambda: 2000.0),
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


def _ambiguous_recovery_environment(
    tmp_path: Path, *, non_timeout: bool = False
) -> tuple[Environment, UUID, UUID]:
    env = _environment(tmp_path)
    _deploy_workspace(env)
    with env.sessions.begin() as session:
        session.add(
            McpClientTargetGrantRow(
                client_id=str(env.client_id),
                target_id=str(TARGET_ID),
                grant_id=str(uuid4()),
                created_at=NOW_TEXT,
            )
        )
        session.add(
            McpClientDevelopmentSourceGrantRow(
                client_id=str(env.client_id),
                development_source_id=str(SOURCE_ID),
                created_at=NOW_TEXT,
            )
        )

    def timeout(_addon_id: str) -> KodiAddonSmokeResult:
        env.smoke.count += 1
        if non_timeout:
            raise KodiAddonSmokeAmbiguousError("Unsupported directory response")
        raise KodiAddonSmokeTimeoutError(10)

    env.smoke.invoke = timeout  # type: ignore[assignment]
    execution_request = uuid4()
    with pytest.raises(McpWorkspaceDevelopmentError, match="execution_ambiguous"):
        env.development.execute(
            client_id=env.client_id,
            request_id=execution_request,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=TARGET_ID,
        )
    with env.sessions() as session:
        execution = session.scalar(select(McpWorkspaceExecutionOperationRow))
        assert execution is not None
        assert execution.execution_kind == "smoke"
        assert execution.original_container_id == CONTAINER_ID
        assert execution.original_runtime_state_revision is not None
        return env, execution_request, UUID(execution.id)


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


def test_default_external_cooldown_stays_300_seconds_and_replays_denial(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path, use_default_rates=True)
    bridge, _targets = _configure_external_environment(env)
    now = [1000.0]
    rate = env.development._external_deployment_rate
    assert rate is not None
    rate._clock = lambda: now[0]

    def deploy(request: UUID) -> WorkspaceDeploymentResult:
        return env.development.deploy(
            client_id=env.client_id,
            request_id=request,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=EXTERNAL_TARGET_ID,
        )

    assert deploy(uuid4()).state == "succeeded"
    now[0] += MANAGED_DEPLOYMENT_INTERVAL_SECONDS
    denied = uuid4()
    with pytest.raises(McpWorkspaceDevelopmentError, match="rate_limited"):
        deploy(denied)
    assert bridge.replacement_count == 1
    now[0] = 1300.0
    with pytest.raises(McpWorkspaceDevelopmentError, match="rate_limited"):
        deploy(denied)
    assert deploy(uuid4()).state == "succeeded"
    assert bridge.replacement_count == 2
