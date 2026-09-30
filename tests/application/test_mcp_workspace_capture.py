"""Exact source grant, workspace binding, and opt-in capture permissions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    FULL_LAB_CONTROL_PRESET_ID,
    McpAuthenticationService,
)
from kodi_lab.application.workspace_source_captures import WorkspaceSourceCaptureService
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import (
    McpTargetToolService,
    WorkspaceCaptureToolInput,
)
from tests.application.test_mcp_development_inspection import (
    OTHER_SOURCE_ID,
    SOURCE_ID,
    _environment,
)


def test_opt_in_scan_and_capture_keep_existing_clients_unprivileged(
    tmp_path: Path,
) -> None:
    sessions, workspaces, administration, _old_tools, workspace_id, _tree = (
        _environment(tmp_path)
    )
    registry = build_mcp_tool_registry()
    authorization = McpAuthorizationService(sessions, registry)
    captures = WorkspaceSourceCaptureService(
        sessions, data_root=tmp_path / "data", development_root=tmp_path / "sources"
    )
    tools = McpTargetToolService(
        sessions,
        authorization,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        workspaces=workspaces,
        workspace_source_captures=captures,
    )
    old = administration.create_client(
        display_name="Preexisting client", preset_id=FULL_LAB_CONTROL_PRESET_ID
    )
    old_token = administration.issue_credential(old.id).token
    old_principal = McpAuthenticationService(sessions).authenticate(old_token)
    assert old_principal is not None
    assert "workspace_source_scan" not in authorization.discoverable_tools(
        old_principal
    )
    assert "workspace_capture" not in authorization.discoverable_tools(old_principal)
    administration.grant_development_source(old.id, SOURCE_ID)
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        tools.workspace_source_scan(old_principal, workspace_id)
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        tools.workspace_capture(
            old_principal,
            WorkspaceCaptureToolInput(workspace_id=workspace_id, expected_generation=0),
        )

    selected = frozenset({"development_source.inspect", "workspace.capture"})
    preset = administration.create_custom_preset(
        display_name="Source capture only",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=selected,
    )
    assert frozenset(preset.permissions) == selected
    client = administration.create_client(
        display_name="Capture client", preset_id=preset.id
    )
    token = administration.issue_credential(client.id).token
    principal = McpAuthenticationService(sessions).authenticate(token)
    assert principal is not None
    assert {"workspace_source_scan", "workspace_capture"} <= set(
        authorization.discoverable_tools(principal)
    )
    assert not {"workspace_edit", "promotion_request", "remote_action"} & set(
        authorization.discoverable_tools(principal)
    )
    with pytest.raises(
        McpAuthorizationError, match="workspace_not_found_or_not_authorized"
    ):
        tools.workspace_source_scan(principal, workspace_id)
    administration.grant_development_source(client.id, SOURCE_ID)
    with pytest.raises(
        McpAuthorizationError, match="workspace_not_found_or_not_authorized"
    ):
        tools.workspace_source_scan(principal, workspace_id)
    administration.bind_workspace(client.id, workspace_id)
    scanned = tools.workspace_source_scan(principal, workspace_id)
    assert scanned.source_id == SOURCE_ID
    assert scanned.source_matches_capture
    assert scanned.workspace_generation == 0
    source = tmp_path / "sources/plugin.video.example"
    (source / "main.py").write_text("edited normally\n", encoding="utf-8")
    changed = tools.workspace_source_scan(principal, workspace_id)
    assert not changed.source_matches_capture
    captured = tools.workspace_capture(
        principal,
        WorkspaceCaptureToolInput(workspace_id=workspace_id, expected_generation=0),
    )
    assert captured.changed and captured.generation == 1
    assert captured.source_id == SOURCE_ID
    assert captured.tree_digest == changed.source_tree_digest
    identical = tools.workspace_capture(
        principal,
        WorkspaceCaptureToolInput(workspace_id=workspace_id, expected_generation=1),
    )
    assert not identical.changed and identical.generation == 1
    with pytest.raises(McpAuthorizationError, match="workspace_capture_rejected"):
        tools.workspace_capture(
            principal,
            WorkspaceCaptureToolInput(workspace_id=workspace_id, expected_generation=0),
        )
    with pytest.raises(
        McpAuthorizationError, match="workspace_not_found_or_not_authorized"
    ):
        tools.workspace_source_scan(
            principal, UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        )
    assert (
        OTHER_SOURCE_ID
        not in administration.get_client(client.id).development_source_grants
    )
    workspaces.replace_text_file(
        workspace_id,
        expected_generation=1,
        relative_path="main.py",
        content="private workspace edit\n",
    )
    with pytest.raises(McpAuthorizationError, match="workspace_capture_rejected"):
        tools.workspace_capture(
            principal,
            WorkspaceCaptureToolInput(workspace_id=workspace_id, expected_generation=2),
        )
    administration.remove_development_source_grant(client.id, SOURCE_ID)
    with pytest.raises(
        McpAuthorizationError, match="workspace_not_found_or_not_authorized"
    ):
        tools.workspace_source_scan(principal, workspace_id)
    assert not registry.require("workspace_capture").production_eligible
    assert not registry.require("workspace_source_scan").production_eligible


def test_scan_and_capture_permissions_are_separate(tmp_path: Path) -> None:
    sessions, workspaces, administration, _tools, workspace_id, _tree = _environment(
        tmp_path
    )
    registry = build_mcp_tool_registry()
    authorization = McpAuthorizationService(sessions, registry)
    captures = WorkspaceSourceCaptureService(
        sessions, data_root=tmp_path / "data", development_root=tmp_path / "sources"
    )
    tools = McpTargetToolService(
        sessions,
        authorization,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        workspaces=workspaces,
        workspace_source_captures=captures,
    )
    preset = administration.create_custom_preset(
        display_name="Source scanner",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=frozenset({"development_source.inspect"}),
    )
    client = administration.create_client(display_name="Scanner", preset_id=preset.id)
    token = administration.issue_credential(client.id).token
    principal = McpAuthenticationService(sessions).authenticate(token)
    assert principal is not None
    administration.grant_development_source(client.id, SOURCE_ID)
    administration.bind_workspace(client.id, workspace_id)
    assert tools.workspace_source_scan(principal, workspace_id).source_matches_capture
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        tools.workspace_capture(
            principal,
            WorkspaceCaptureToolInput(workspace_id=workspace_id, expected_generation=0),
        )
