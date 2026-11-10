"""Opt-in launch reuses exact deployment, selected-session and replay safeguards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    FULL_LAB_CONTROL_PRESET_ID,
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationError,
    McpAdministrationService,
    McpAuthenticationService,
    McpPrincipal,
)
from kodi_lab.application.workspace_launch import WorkspaceLaunchService
from kodi_lab.development.deployment import expected_destination_digest
from kodi_lab.domain import TargetType
from kodi_lab.mcp.authorization import (
    InternalManagedTargetAuthorityProvider,
    McpAuthorizationError,
    McpAuthorizationService,
)
from kodi_lab.mcp.registry import TargetClass, build_mcp_tool_registry
from kodi_lab.mcp.tools import McpTargetToolService, WorkspaceLaunchInput
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    McpWorkspaceDeploymentOperationRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.workspace import capture_workspace_deployment_tree
from tests.application import test_mcp_workspace_development as fixtures
from tests.application.test_workspace_launch import Bridge

PERMISSIONS = frozenset(
    {
        "target.inspect",
        "workspace.inspect",
        "workspace.edit",
        "workspace.deploy",
        "managed.versions",
        "development.launch",
    }
)


@dataclass
class LaunchEnvironment:
    env: fixtures.Environment
    admin: McpAdministrationService
    auth: McpAuthorizationService
    principal: McpPrincipal
    tools: McpTargetToolService
    launch: WorkspaceLaunchService
    bridge: Bridge

    def request(self, **changes: Any) -> WorkspaceLaunchInput:
        return WorkspaceLaunchInput.model_validate(
            {
                "request_id": str(uuid4()),
                "workspace_id": str(self.env.workspace_id),
                "target_id": str(fixtures.TARGET_ID),
                "expected_generation": 1,
                "extension_ordinal": 0,
                **changes,
            }
        )


def environment(tmp_path: Path) -> LaunchEnvironment:
    env = fixtures._environment(tmp_path)
    # Program classification comes from a real captured xbmc.python.script
    # manifest, independently of the add-on ID's spelling.
    env.workspaces.replace_text_file(
        env.workspace_id,
        expected_generation=0,
        relative_path="addon.xml",
        content='<addon id="plugin.video.slice4" name="Program" version="1.0.0">'
        '<extension point="xbmc.python.script" library="main.py"/></addon>',
    )
    structural = env.workspaces.validate_structure(
        env.workspace_id, expected_generation=1
    )
    env.development.record_validation(
        workspace_id=env.workspace_id,
        source_id=fixtures.SOURCE_ID,
        generation=1,
        addon_id=structural.addon_id,
        addon_version=structural.addon_version,
        extension_types=structural.extension_types,
        validated_at=fixtures.NOW,
    )
    manifest = capture_workspace_deployment_tree(tmp_path / "data", env.workspace_id)
    env.broker.sync_source_digest = manifest.digest
    env.broker.sync_destination_digest = expected_destination_digest(
        manifest, runtime_uid=1000, runtime_gid=1000
    )
    deployed = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=1,
        target_id=fixtures.TARGET_ID,
    )
    assert deployed.state == "succeeded"
    with env.sessions.begin() as db:
        workspace = db.get(AgentWorkspaceRow, str(env.workspace_id))
        deployment = db.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert workspace is not None and deployment is not None
        deployment.source_manifest_digest = workspace.current_tree_digest
        deployment.verified_destination_digest = env.broker.sync_destination_digest
        db.add(
            DevelopmentSessionRow(
                generation=1,
                request_id=str(uuid4()),
                target_id=str(fixtures.TARGET_ID),
                runtime_revision_id=deployment.runtime_revision_id,
                workspace_id=str(env.workspace_id),
                source_id=str(fixtures.SOURCE_ID),
                container_id=fixtures.CONTAINER_ID,
                phase="bound",
                created_at=fixtures.NOW_TEXT,
                updated_at=fixtures.NOW_TEXT,
            )
        )
        db.flush()
        db.add(DevelopmentSessionPolicyRow(id=1, active_generation=1))
    admin = McpAdministrationService(env.sessions)
    preset = admin.create_custom_preset(
        display_name="Trusted local development",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=PERMISSIONS,
    )
    admin.assign_client_preset(env.client_id, preset.id)
    admin.assign_target_preset(fixtures.TARGET_ID, preset.id)
    admin.grant_target(env.client_id, fixtures.TARGET_ID)
    admin.grant_development_source(env.client_id, fixtures.SOURCE_ID)
    credential = admin.issue_credential(env.client_id)
    principal = McpAuthenticationService(env.sessions).authenticate(credential.token)
    assert principal is not None
    auth = McpAuthorizationService(
        env.sessions,
        build_mcp_tool_registry(),
        targets=InternalManagedTargetAuthorityProvider(env.sessions),
    )
    bridge = Bridge()
    launch = WorkspaceLaunchService(
        env.sessions,
        env.development,
        bridge,
        data_root=tmp_path / "data",
        clock=lambda: fixtures.NOW + timedelta(minutes=1),
    )
    tools = McpTargetToolService(
        env.sessions,
        auth,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        workspaces=env.workspaces,
        workspace_development=env.development,
        workspace_launch=launch,
    )
    return LaunchEnvironment(env, admin, auth, principal, tools, launch, bridge)


def test_opt_in_launches_captured_program_and_reuses_original_receipt(
    tmp_path: Path,
) -> None:
    e = environment(tmp_path)
    names = set(e.auth.discoverable_tools(e.principal))
    assert {
        "target_status",
        "inspect_ui_state",
        "screenshot_capture",
        "managed_start",
        "managed_stop",
        "workspace_deploy",
        "development_launch",
    } <= names
    assert (
        not {
            "remote_action",
            "managed_reset",
            "workspace_launch",
            "workspace_execute",
            "workspace_execution_recover",
            "addon_settings_set",
            "promotion_request",
        }
        & names
    )
    operation = e.request()
    first = e.tools.development_launch(e.principal, operation)
    assert first.dispatch_accepted and first.generation == 1
    assert first.script_completion == first.ui_acceptance == "not_observed"
    repeated = e.tools.development_launch(e.principal, operation)
    assert repeated.replayed and repeated.launch_id == first.launch_id
    assert e.bridge.calls == [False]
    with pytest.raises(McpAuthorizationError, match="rate_limited"):
        e.tools.development_launch(e.principal, e.request())
    assert e.bridge.calls == [False]


@pytest.mark.parametrize(
    "denial",
    [
        "source",
        "target",
        "credential",
        "client_permission",
        "target_permission",
        "disabled",
        "other_client",
        "unselected",
        "other_workspace",
        "uncertain",
        "runtime",
        "container",
        "external_development",
        "external_production",
        "unrelated_target",
        "stale_generation",
        "stale_deployment",
    ],
)
def test_scope_and_provenance_refusals_do_not_dispatch(
    tmp_path: Path, denial: str
) -> None:
    e = environment(tmp_path)
    principal, operation = e.principal, e.request()
    if denial == "source":
        e.admin.remove_development_source_grant(e.env.client_id, fixtures.SOURCE_ID)
    elif denial == "target":
        e.admin.remove_target_grant(e.env.client_id, fixtures.TARGET_ID)
    elif denial == "credential":
        e.admin.revoke_credential(e.env.client_id, principal.credential_id)
    elif denial == "client_permission":
        e.admin.assign_client_preset(e.env.client_id, READ_ONLY_INSPECTION_PRESET_ID)
    elif denial == "target_permission":
        e.admin.assign_target_preset(fixtures.TARGET_ID, FULL_LAB_CONTROL_PRESET_ID)
    elif denial == "disabled":
        e.admin.set_client_enabled(e.env.client_id, False)
    elif denial == "other_client":
        other = e.admin.create_client(
            display_name="Other developer", preset_id=principal.preset_id
        )
        e.admin.grant_target(other.id, fixtures.TARGET_ID)
        e.admin.grant_development_source(other.id, fixtures.SOURCE_ID)
        other_principal = McpAuthenticationService(e.env.sessions).authenticate(
            e.admin.issue_credential(other.id).token
        )
        assert other_principal is not None
        principal = other_principal
    elif denial in {
        "unselected",
        "other_workspace",
        "uncertain",
        "runtime",
        "container",
    }:
        other_workspace = None
        if denial == "other_workspace":
            created = e.env.workspaces.queue_creation(fixtures.SOURCE_ID)
            lease = e.env.workspaces.acquire(created.operation_id)
            assert lease is not None
            e.env.workspaces.run(lease)
            other_workspace = created.workspace_id
        with e.env.sessions.begin() as db:
            current = db.get(DevelopmentSessionRow, 1)
            policy = db.get(DevelopmentSessionPolicyRow, 1)
            assert current is not None and policy is not None
            if denial == "unselected":
                policy.active_generation = None
            elif denial == "other_workspace":
                current.workspace_id = str(other_workspace)
            elif denial == "uncertain":
                current.phase = "uncertain"
            elif denial == "runtime":
                original = db.get(RuntimeRevisionRow, current.runtime_revision_id)
                assert original is not None
                attributes = {
                    c.name: getattr(original, c.name)
                    for c in RuntimeRevisionRow.__table__.columns
                }
                attributes.update(
                    id=str(uuid4()), revision_number=original.revision_number + 1
                )
                db.add(RuntimeRevisionRow(**attributes))
                db.flush()
                current.runtime_revision_id = attributes["id"]
            else:
                current.container_id = "b" * 64
    elif denial.startswith("external_"):
        with e.env.sessions.begin() as db:
            target = db.get(TargetRow, str(fixtures.TARGET_ID))
            assert target is not None
            target.target_type = TargetType(denial)
    elif denial == "unrelated_target":
        operation = e.request(target_id=str(uuid4()))
    elif denial == "stale_generation":
        operation = e.request(expected_generation=0)
    else:
        with e.env.sessions.begin() as db:
            deployment = db.scalar(select(McpWorkspaceDeploymentOperationRow))
            assert deployment is not None
            deployment.source_manifest_digest = "sha256:" + "0" * 64
    with pytest.raises(McpAuthorizationError):
        e.tools.development_launch(principal, operation)
    assert not e.bridge.calls


def test_launch_permission_does_not_recover_or_repeat_an_uncertain_dispatch(
    tmp_path: Path,
) -> None:
    e = environment(tmp_path)
    e.bridge.pending = True
    operation = e.request()
    first = e.tools.development_launch(e.principal, operation)
    assert first.state == "ambiguous"
    replay = e.tools.development_launch(e.principal, operation)
    assert replay.state == "ambiguous" and replay.launch_id == first.launch_id
    assert e.bridge.calls == [False, True]  # Status query only on replay.
    e.launch._clock = lambda: fixtures.NOW + timedelta(minutes=2)
    with pytest.raises(McpAuthorizationError, match="target_reconciliation_required"):
        e.tools.development_launch(e.principal, e.request())
    assert e.bridge.calls == [False, True]
    assert "workspace_execution_recover" not in e.auth.discoverable_tools(e.principal)
    with pytest.raises(McpAuthorizationError):
        e.auth.authorize_workspace_target(
            e.principal,
            "workspace_execution_recover",
            e.env.workspace_id,
            fixtures.TARGET_ID,
        )


def test_default_and_existing_presets_gain_no_launch_authority(tmp_path: Path) -> None:
    e = environment(tmp_path)
    for preset in e.admin.list_presets():
        if preset.builtin:
            assert "development.launch" not in preset.permissions
    for template in (
        ADDON_DEVELOPMENT_PRESET_ID,
        FULL_LAB_CONTROL_PRESET_ID,
        READ_ONLY_INSPECTION_PRESET_ID,
    ):
        copy = e.admin.create_custom_preset(
            display_name=str(uuid4()), template_preset_id=template
        )
        assert "development.launch" not in copy.permissions
    with pytest.raises(McpAdministrationError):
        e.admin.create_custom_preset(
            display_name="Invalid inspection ceiling",
            template_preset_id=READ_ONLY_INSPECTION_PRESET_ID,
            permissions=frozenset({"development.launch"}),
        )
    definition = build_mcp_tool_registry().require("development_launch")
    assert definition.allowed_target_classes == frozenset({TargetClass.INTERNAL_LAB})
    assert not definition.production_eligible


@pytest.mark.parametrize("change", ["source", "credential", "selection"])
def test_dispatch_rechecks_authority_after_claim(
    tmp_path: Path, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = environment(tmp_path)

    def revoke(phase: str) -> None:
        if phase != "claimed":
            return
        if change == "source":
            e.admin.remove_development_source_grant(e.env.client_id, fixtures.SOURCE_ID)
        elif change == "credential":
            e.admin.revoke_credential(e.env.client_id, e.principal.credential_id)
        else:
            with e.env.sessions.begin() as db:
                selected = db.get(DevelopmentSessionRow, 1)
                assert selected is not None
                selected.phase = "uncertain"

    monkeypatch.setattr(e.launch, "_failure", revoke)
    with pytest.raises(McpAuthorizationError):
        e.tools.development_launch(e.principal, e.request())
    assert not e.bridge.calls


def test_supported_client_deploys_the_exact_owned_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.mcp_workspace_development import (
        BoundedIntervalRateLimiter,
    )
    from kodi_lab.mcp.authorization import TargetTechnicalAuthority
    from kodi_lab.mcp.tools import WorkspaceDeployInput

    e = environment(tmp_path)

    # The technical adapter supplies fixture readiness. The actual existing
    # deployment service still checks its broker runtime, source and tree bytes.
    def eligible(target: object) -> TargetTechnicalAuthority:
        assert target == fixtures.TARGET_ID
        return TargetTechnicalAuthority(
            target_class=TargetClass.INTERNAL_LAB,
            capabilities=frozenset({"workspace_deployment", "workspace_execution"}),
            current_states=frozenset({"running", "ready", "reconciled"}),
        )

    monkeypatch.setattr(e.auth._targets, "resolve", eligible)
    e.env.development._deployment_rate = BoundedIntervalRateLimiter(0)
    result = e.tools.workspace_deploy(
        e.principal,
        WorkspaceDeployInput(
            request_id=uuid4(),
            workspace_id=e.env.workspace_id,
            expected_generation=1,
            target_id=fixtures.TARGET_ID,
        ),
    )
    assert result.state == "succeeded" and result.generation == 1
    assert e.env.broker.sync_count == 2


def test_launch_still_requires_the_native_bridge_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.kodi.bridge import ExternalBridgeError

    e = environment(tmp_path)

    def unavailable(_observed: object, _version: str) -> None:
        raise ExternalBridgeError("capability_unavailable")

    monkeypatch.setattr(e.bridge, "binding", unavailable)
    with pytest.raises(McpAuthorizationError, match="capability_unavailable"):
        e.tools.development_launch(e.principal, e.request())
    assert not e.bridge.calls
