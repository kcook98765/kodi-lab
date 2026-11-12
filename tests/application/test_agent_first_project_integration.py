"""Real project/create/edit/validation/deploy/launch, with native I/O doubles only.

No successful receipt is inserted or rewritten to make the next service pass.
This is source subsystem integration, not native Kodi acceptance.
"""

from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.agent_projects import ProjectCreateInput, ProjectStatusInput
from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
)
from kodi_lab.application.mcp_workspace_development import McpWorkspaceDevelopmentError
from kodi_lab.application.mcp_workspace_mutations import McpWorkspaceCreationService
from kodi_lab.application.workspace_launch import WorkspaceLaunchService
from kodi_lab.broker.development_sources import (
    SqliteDevelopmentSourceDeploymentResolver,
)
from kodi_lab.development.deployment import expected_destination_digest
from kodi_lab.domain import KodiReleaseIdentity
from kodi_lab.kodi.addons import (
    KodiAddonDetailsRecord,
    KodiAddonInventoryRecord,
    KodiAddonInventoryResult,
    KodiReportedAddonDependency,
)
from kodi_lab.kodi.readiness import KodiApplicationIdentity
from kodi_lab.mcp.authorization import (
    InternalManagedTargetAuthorityProvider,
    McpAuthorizationError,
    McpAuthorizationService,
    TargetTechnicalAuthority,
)
from kodi_lab.mcp.registry import TargetClass, build_mcp_tool_registry
from kodi_lab.mcp.tools import (
    McpTargetToolService,
    WorkspaceDeployInput,
    WorkspaceEditInput,
    WorkspaceLaunchInput,
    WorkspaceValidateInput,
)
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    McpWorkspaceDeploymentOperationRow,
)
from kodi_lab.workspace import capture_workspace_deployment_tree
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_workspace_launch import Bridge

ADDON = "script.agentfirst.integration"


@pytest.mark.parametrize("release", ["20.5", "21.3"])
@pytest.mark.parametrize(
    "initial",
    [
        "enabled",
        "disabled",
        "lost_enable",
        "dependency_changed",
        "role_revoked",
        "tree_changed",
        "lease_expired",
    ],
)
def test_new_owned_program_real_service_chain_without_web_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release: str, initial: str
) -> None:
    e = fixture._environment(tmp_path, kodi_release=release)
    e.workspaces._development_root = None
    admin = McpAdministrationService(e.sessions, clock=lambda: fixture.NOW)
    owner = admin.create_client(
        display_name="Integrated coding agent",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    principal = McpAuthenticationService(
        e.sessions, clock=lambda: fixture.NOW
    ).authenticate(admin.issue_credential(owner.id).token)
    assert principal is not None
    native_capabilities = InternalManagedTargetAuthorityProvider(e.sessions)

    class Authority:
        def resolve(self, target: UUID) -> TargetTechnicalAuthority:
            return TargetTechnicalAuthority(
                TargetClass.INTERNAL_LAB,
                native_capabilities.capabilities(target),
                frozenset({"running", "ready", "reconciled"}),
            )

    authorization = McpAuthorizationService(
        e.sessions,
        build_mcp_tool_registry(),
        targets=Authority(),
        clock=lambda: fixture.NOW,
    )
    submitted: list[UUID] = []
    creation = McpWorkspaceCreationService(
        e.sessions, e.workspaces, submitted.append, clock=lambda: fixture.NOW
    )
    bridge = Bridge()
    launch = WorkspaceLaunchService(
        e.sessions,
        e.development,
        bridge,
        data_root=tmp_path / "data",
        clock=lambda: fixture.NOW,
    )
    tools = McpTargetToolService(
        e.sessions,
        authorization,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        workspaces=e.workspaces,
        workspace_creation=creation,
        workspace_development=e.development,
        workspace_launch=launch,
    )
    project = tools.project_create(
        principal,
        ProjectCreateInput(
            request_id=uuid4(), addon_id=ADDON, display_name="Integration Program"
        ),
    )
    workspace_id = project.workspace.workspace_id
    capture = e.workspaces.acquire(project.workspace.creation_operation_id)
    assert capture is not None
    e.workspaces.run(capture)
    # The actual package resources use the existing bounded editor, including parents.
    tools.workspace_edit(
        principal,
        WorkspaceEditInput(
            workspace_id=workspace_id,
            expected_generation=0,
            operation="create",
            relative_path="resources/skins/Default/720p/agent.xml",
            content=(
                '<window><controls><control type="label" id="1">'
                "<label>Inert</label></control></controls></window>"
            ),
        ),
    )
    validated = tools.workspace_validate(
        principal,
        WorkspaceValidateInput(workspace_id=workspace_id, expected_generation=1),
    )
    assert validated.addon_id == ADDON and validated.observed_generation == 1
    manifest = capture_workspace_deployment_tree(tmp_path / "data", workspace_id)
    source = project.project_id
    enabled = initial == "enabled"
    enable_calls: list[str] = []
    # The broker's production receipt/owner/path resolver runs before simulated copy.
    resolver = SqliteDevelopmentSourceDeploymentResolver(
        e.sessions,
        development_root=None,
        data_root=tmp_path / "data",
        clock=lambda: fixture.NOW,
    )
    synchronize = e.broker.copy_sync_mcp_workspace

    def copy(**values: Any) -> Any:
        spec = resolver.resolve_workspace_deployment(
            values["managed_instance_id"],
            values["workspace_id"],
            values["expected_generation"],
            values["deployment_operation_id"],
            values["expected_container_id"],
        )
        assert spec.development_source_id == str(source)
        assert (spec.prior_evidence is None) == (e.broker.sync_count == 0)
        return synchronize(**values).model_copy(
            update={
                "development_source_id": str(source),
                "source_manifest_digest": manifest.digest,
                "verified_destination_digest": expected_destination_digest(
                    manifest, runtime_uid=1000, runtime_gid=1000
                ),
                "observed_source_name": "Integration Program",
                "observed_source_version": "0.1.0",
            }
        )

    monkeypatch.setattr(e.broker, "copy_sync_mcp_workspace", copy)
    verify = e.broker.verify_mcp_workspace_destination

    def readback(**values: Any) -> Any:
        if initial == "lease_expired":
            resolver._clock = lambda: fixture.NOW + timedelta(hours=1)
        resolver.resolve_workspace_destination_verification(
            values["deployment_operation_id"],
            values["managed_instance_id"],
            values["expected_container_id"],
        )
        if initial == "tree_changed":
            raise RuntimeError("Native copied tree changed")
        return verify(**values).model_copy(
            update={
                "development_source_id": str(source),
                "addon_id": ADDON,
                "addon_version": "0.1.0",
                "video_plugin_descriptor_eligible": False,
            }
        )

    monkeypatch.setattr(e.broker, "verify_mcp_workspace_destination", readback)
    # Only native Kodi observations are simulated: no collision initially, then enabled.
    read_inventory = e.development._inventory.get

    def status(target: UUID) -> Any:
        original = read_inventory(target)
        items = [
            item.model_copy(
                update={"addon_id": "xbmc.python", "name": "Python", "version": "3.0.1"}
            )
            for item in original.items
        ]
        if e.broker.sync_count:
            items.append(
                original.items[0].model_copy(
                    update={
                        "addon_id": ADDON,
                        "name": "Integration Program",
                        "version": "0.1.0",
                        "kodi_type": "xbmc.python.script",
                        "enabled": enabled,
                    }
                )
            )
        return original.model_copy(update={"items": items})

    monkeypatch.setattr(e.development._inventory, "get", status)
    version = KodiReleaseIdentity.parse(release)

    class NativeInventory:
        def get_inventory(self) -> KodiAddonInventoryResult:
            # Kodi system API capabilities are reviewed per release, not
            # enumerable add-ons. A reported fake system record is unverified.
            items: list[KodiAddonInventoryRecord] = []
            if e.broker.sync_count:
                items.append(
                    KodiAddonInventoryRecord(
                        ADDON,
                        "Integration Program",
                        "0.1.0",
                        enabled,
                        "xbmc.python.script",
                    )
                )
            return KodiAddonInventoryResult(
                KodiApplicationIdentity("Kodi", version.major, version.minor, "stable"),
                tuple(items),
            )

    e.development._inventory_factory = lambda _observation, _profile: NativeInventory()

    class Details:
        def enable(self, addon_id: str) -> None:
            nonlocal enabled
            enable_calls.append(addon_id)
            with e.sessions() as db:
                receipt = db.scalar(select(McpWorkspaceDeploymentOperationRow))
                assert receipt is not None and receipt.phase == "inventory"
                assert receipt.mutation_started and receipt.sync_completed
                assert receipt.addon_enabled is False
            enabled = True
            if initial == "lost_enable":
                raise RuntimeError("Lost native enable response")

        def get_addon_details(self, addon_id: str) -> KodiAddonDetailsRecord:
            if initial == "role_revoked" and not enabled:
                admin.set_managed_lab_role(owner.id, False)
            return KodiAddonDetailsRecord(
                addon_id=addon_id,
                name="Integration Program",
                version="0.1.0",
                enabled=enabled,
                kodi_type="xbmc.python.script",
                dependencies=()
                if initial == "dependency_changed"
                else (KodiReportedAddonDependency("xbmc.python", "3.0.0", False),),
            )

    e.development._details_factory = lambda _observation, _profile: Details()
    operation = WorkspaceDeployInput(
        request_id=uuid4(),
        workspace_id=workspace_id,
        expected_generation=1,
        target_id=fixture.TARGET_ID,
    )
    if initial not in {"enabled", "disabled"}:
        with pytest.raises(McpAuthorizationError, match="deployment_ambiguous"):
            tools.workspace_deploy(principal, operation)
        with e.sessions() as db:
            receipt = db.scalar(select(McpWorkspaceDeploymentOperationRow))
            assert receipt is not None and receipt.state == "reconciliation_required"
            assert receipt.sync_completed and receipt.addon_enabled is not True
        assert enable_calls == ([ADDON] if initial == "lost_enable" else [])
        assert e.broker.state.value == "stopped"
        counts = (e.broker.sync_count, e.broker.start_count, e.broker.stop_count)
        with pytest.raises((McpAuthorizationError, McpWorkspaceDevelopmentError)):
            tools.workspace_deploy(principal, operation)
        assert counts == (
            e.broker.sync_count,
            e.broker.start_count,
            e.broker.stop_count,
        )
        assert enable_calls == ([ADDON] if initial == "lost_enable" else [])
        if initial in {"lost_enable", "tree_changed"}:
            from kodi_lab.application.mcp_workspace_development import (
                BoundedIntervalRateLimiter,
            )
            from kodi_lab.application.workspace_deployment_recovery import (
                WorkspaceDeploymentRecoveryService,
            )
            from kodi_lab.broker.docker_inventory import ContainerRuntimeState
            from kodi_lab.broker.operations import InspectMcpWorkspaceDeploymentResult

            read_containers = e.broker.managed_container_inventory

            def containers() -> Any:
                observed = read_containers()
                return observed.model_copy(
                    update={
                        "containers": [
                            item.model_copy(
                                update={
                                    "runtime_revision_id": str(
                                        fixture.RUNTIME_REVISION_ID
                                    )
                                }
                            )
                            for item in observed.containers
                        ]
                    }
                )

            monkeypatch.setattr(e.broker, "managed_container_inventory", containers)
            inspections: list[str] = []

            def inspect(**values: Any) -> Any:
                spec = resolver.resolve_workspace_reconciliation(
                    values["deployment_operation_id"],
                    values["managed_instance_id"],
                    values["expected_container_id"],
                )
                assert (
                    spec.require_stopped and spec.alternate_destination_digest is None
                )
                inspections.append(values["deployment_operation_id"])
                if initial == "tree_changed":
                    raise RuntimeError("Native tree cannot prove requested bytes")
                assert spec.requested_source_digest is not None
                return InspectMcpWorkspaceDeploymentResult(
                    deployment_operation_id=values["deployment_operation_id"],
                    managed_instance_id=spec.managed_instance_id,
                    development_source_id=spec.development_source_id,
                    runtime_revision_id=spec.runtime_revision_id,
                    container_id=spec.trusted_container_id,
                    requested_manifest_digest=spec.requested_source_digest,
                    requested_destination_digest=spec.verified_destination_digest,
                    installed_manifest_digest=spec.verified_destination_digest,
                    matches_requested=True,
                    matches_prior=False,
                )

            monkeypatch.setattr(
                e.broker, "inspect_mcp_workspace_deployment", inspect, raising=False
            )
            recovery = WorkspaceDeploymentRecoveryService(
                e.sessions, cast(Any, e.broker), data_root=tmp_path / "data"
            )
            recovered = recovery.reconcile(fixture.TARGET_ID, UUID(receipt.id))
            if initial == "tree_changed":
                assert recovered.outcome == "still_requires_admin"
                assert counts == (
                    e.broker.sync_count,
                    e.broker.start_count,
                    e.broker.stop_count,
                )
                with e.sessions() as db:
                    fenced = db.get(McpWorkspaceDeploymentOperationRow, receipt.id)
                    assert (
                        fenced is not None and fenced.state == "reconciliation_required"
                    )
                return
            assert recovered.outcome == "contained_requested_tree"
            assert not recovered.deployment_succeeded
            assert recovery.reconcile(fixture.TARGET_ID, UUID(receipt.id)).replayed
            assert inspections == [receipt.id]
            assert counts == (
                e.broker.sync_count,
                e.broker.start_count,
                e.broker.stop_count,
            )
            e.development._clock = lambda: fixture.NOW + timedelta(minutes=10)
            e.development._deployment_rate = BoundedIntervalRateLimiter(
                300, clock=lambda: 2000.0
            )
            e.broker.state = ContainerRuntimeState.RUNNING
            fresh = tools.workspace_deploy(
                principal, operation.model_copy(update={"request_id": uuid4()})
            )
            assert fresh.state == "succeeded" and fresh.deployment_id != UUID(
                receipt.id
            )
            assert enable_calls == [ADDON]
        return
    deployed = tools.workspace_deploy(principal, operation)
    assert enable_calls == ([ADDON] if initial == "disabled" else [])
    assert deployed.state == "succeeded", deployed
    launch_request = WorkspaceLaunchInput(
        request_id=uuid4(),
        workspace_id=workspace_id,
        expected_generation=1,
        target_id=fixture.TARGET_ID,
        extension_ordinal=0,
    )
    launched = tools.development_launch(principal, launch_request)
    assert launched.dispatch_accepted
    replay = tools.development_launch(principal, launch_request)
    assert replay.replayed and replay.launch_id == launched.launch_id
    assert bridge.calls == [False]
    assert (
        tools.workspace_deploy(principal, operation).deployment_id
        == deployed.deployment_id
    )
    assert e.broker.sync_count == 1
    with e.sessions() as db:
        workspace = db.get(AgentWorkspaceRow, str(workspace_id))
        receipt = db.get(
            McpWorkspaceDeploymentOperationRow, str(deployed.deployment_id)
        )
        assert workspace is not None and receipt is not None
        assert receipt.source_manifest_digest == manifest.digest
        assert workspace.current_tree_digest == manifest.digest
        assert db.scalar(select(DevelopmentSessionRow)) is None
        assert db.get(DevelopmentSessionPolicyRow, 1) is None
    assert admin.get_client(owner.id).target_grants == ()
    assert admin.get_client(owner.id).development_source_grants == (source,)
    other = admin.create_client(
        display_name="Other trusted agent",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    peer = McpAuthenticationService(e.sessions, clock=lambda: fixture.NOW).authenticate(
        admin.issue_credential(other.id).token
    )
    assert peer is not None
    with pytest.raises(McpAuthorizationError, match="not_found_or_not_authorized"):
        tools.project_status(peer, ProjectStatusInput(project_id=source))
    with pytest.raises(McpAuthorizationError, match="not_found_or_not_authorized"):
        tools.development_launch(
            peer, launch_request.model_copy(update={"request_id": uuid4()})
        )
    assert bridge.calls == [False]
