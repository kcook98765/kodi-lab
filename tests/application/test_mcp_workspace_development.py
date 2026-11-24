from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_inventory import (
    AddonInventoryQueryStatus,
    ExternalAddonInventory,
    KodiAddonInventoryItem,
    KodiAddonProvenance,
    KodiAddonProvenanceClassification,
    KodiAddonProvenanceEvidenceStatus,
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
    WorkspaceDeploymentResult,
    WorkspaceExecutionKind,
)
from kodi_lab.domain import (
    DevelopmentSource,
    DevelopmentSourceKind,
    ExternalAdministrativeState,
    ExternalBridgePairingState,
    ExternalBridgeReachabilityState,
)
from kodi_lab.kodi.addons import (
    KodiAddonSmokeResult,
)
from kodi_lab.kodi.bridge import BridgeAddonTree, BridgeDeploymentReceipt
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ExternalBridgeDeploymentReceiptRow,
    ExternalBridgePairingRow,
    ExternalDevelopmentSourceBaselineRow,
    ExternalKodiTargetRow,
    McpAgentWorkspaceBindingRow,
    McpDevelopmentRequestRow,
    McpWorkspaceDeploymentOperationRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceRepository,
)

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

    def addon_inventory(self, target_id: UUID) -> ExternalAddonInventory:
        tree = self.bridge.inspect_addon(target_id, "plugin.video.slice4")
        return ExternalAddonInventory(
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
    dependency: tuple[str, str | None] | None = None,
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
    with sessions.begin() as session:
        DevelopmentSourceRepository(session).create(_source())
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
    now[0] += 299.0
    denied = uuid4()
    with pytest.raises(McpWorkspaceDevelopmentError, match="rate_limited"):
        deploy(denied)
    assert bridge.replacement_count == 1
    now[0] = 1300.0
    with pytest.raises(McpWorkspaceDevelopmentError, match="rate_limited"):
        deploy(denied)
    assert deploy(uuid4()).state == "succeeded"
    assert bridge.replacement_count == 2


def test_external_tool_validates_deploys_and_rechecks_credential(
    tmp_path: Path,
) -> None:
    from kodi_lab.application.mcp_administration import McpAuthenticationService
    from kodi_lab.mcp.authorization import (
        McpAuthorizationError,
        McpAuthorizationService,
        TargetTechnicalAuthority,
    )
    from kodi_lab.mcp.registry import TargetClass, build_mcp_tool_registry
    from kodi_lab.mcp.tools import (
        McpTargetToolService,
        WorkspaceDeployInput,
        WorkspaceExecuteInput,
        WorkspaceValidateInput,
    )
    from kodi_lab.storage.models import McpWorkspaceValidationRow

    env = _environment(tmp_path)
    bridge, adapters = _configure_external_environment(env)
    with env.sessions.begin() as session:
        session.execute(
            delete(McpWorkspaceValidationRow).where(
                McpWorkspaceValidationRow.workspace_id == str(env.workspace_id)
            )
        )
    admin = McpAdministrationService(env.sessions, clock=lambda: NOW)
    admin.grant_development_source(env.client_id, SOURCE_ID)
    admin.assign_target_preset(EXTERNAL_TARGET_ID, FULL_LAB_CONTROL_PRESET_ID)
    credential = admin.issue_credential(env.client_id)
    principal = McpAuthenticationService(env.sessions, clock=lambda: NOW).authenticate(
        credential.token
    )
    assert principal is not None

    class Technical:
        def resolve(self, target_id: UUID) -> TargetTechnicalAuthority:
            assert target_id == EXTERNAL_TARGET_ID
            return TargetTechnicalAuthority(
                TargetClass.EXTERNAL_DEVELOPMENT,
                frozenset({"workspace_deployment", "workspace_execution"}),
                frozenset({"running", "ready", "reconciled"}),
            )

    auth = McpAuthorizationService(
        env.sessions, build_mcp_tool_registry(), targets=Technical(), clock=lambda: NOW
    )
    tools = McpTargetToolService(
        env.sessions, auth, workspace_development=env.development
    )
    validation = WorkspaceValidateInput(
        workspace_id=env.workspace_id, expected_generation=0
    )
    evidence = tools.workspace_validate(principal, validation)
    assert evidence.source_id == SOURCE_ID and evidence.observed_generation == 0
    deploy = WorkspaceDeployInput(
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=EXTERNAL_TARGET_ID,
        deployment_mode="direct",
    )
    with pytest.raises(
        McpAuthorizationError, match="target_not_found_or_not_authorized"
    ):
        tools.workspace_deploy(principal, deploy)
    assert bridge.replacement_count == 0
    admin.grant_target(env.client_id, EXTERNAL_TARGET_ID)
    assert tools.workspace_deploy(principal, deploy).state == "succeeded"
    assert bridge.replacement_count == 1
    execute = WorkspaceExecuteInput(
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=EXTERNAL_TARGET_ID,
    )
    assert tools.workspace_execute(principal, execute).state == "succeeded"
    assert adapters.execution_count == 1
    admin.revoke_credential(env.client_id, principal.credential_id)
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        tools.workspace_validate(principal, validation)
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        tools.workspace_execute(principal, execute)
