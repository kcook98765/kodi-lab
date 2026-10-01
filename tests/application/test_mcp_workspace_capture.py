"""Exact source grant, workspace binding, and opt-in capture permissions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import update

from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    FULL_LAB_CONTROL_PRESET_ID,
    McpAuthenticationService,
)
from kodi_lab.application.workspace_source_captures import (
    WorkspaceSourceCaptureError,
    WorkspaceSourceCaptureService,
)
from kodi_lab.development.deployment import DevelopmentTreeError
from kodi_lab.development.sources import (
    DevelopmentSourceValidationCode,
    DevelopmentSourceValidationError,
)
from kodi_lab.domain import AgentWorkspaceState
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import (
    McpTargetToolService,
    WorkspaceCaptureToolInput,
    _source_capture_reason,
)
from kodi_lab.mcp.transport import _tool_error_result
from kodi_lab.storage.models import AgentWorkspaceRow
from kodi_lab.workspace.storage import AgentWorkspaceStorageError
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
    descriptor = source / "addon.xml"
    original = descriptor.read_text(encoding="utf-8")
    descriptor.write_text(
        original.replace('id="plugin.video.example"', 'id="plugin.video.changed"'),
        encoding="utf-8",
    )
    with pytest.raises(McpAuthorizationError) as changed_identity:
        tools.workspace_source_scan(principal, workspace_id)
    assert changed_identity.value.code == "development_source_scan_unsafe"
    assert changed_identity.value.reason == "source_identity_changed"
    descriptor.write_text(original, encoding="utf-8")
    with pytest.raises(
        McpAuthorizationError, match="workspace_capture_rejected"
    ) as stale:
        tools.workspace_capture(
            principal,
            WorkspaceCaptureToolInput(workspace_id=workspace_id, expected_generation=0),
        )
    assert stale.value.reason == "workspace_generation_conflict"
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
    with pytest.raises(
        McpAuthorizationError, match="workspace_capture_rejected"
    ) as private:
        tools.workspace_capture(
            principal,
            WorkspaceCaptureToolInput(workspace_id=workspace_id, expected_generation=2),
        )
    assert private.value.reason == "workspace_private_edits_present"
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


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("source_addon_identity_changed", "source_identity_changed"),
        ("registered_source_unavailable", "registered_source_unavailable"),
        ("capture_reconciliation_required", "capture_reconciliation_required"),
        ("workspace_capture_evidence_changed", "capture_reconciliation_required"),
        ("unexpected /home/private/token", None),
    ],
)
def test_capture_error_reasons_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, reason: str | None
) -> None:
    sessions, workspaces, administration, _old_tools, workspace_id, _tree = (
        _environment(tmp_path)
    )
    authorization = McpAuthorizationService(sessions, build_mcp_tool_registry())
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
        display_name="Diagnostic capture preset",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=frozenset({"development_source.inspect", "workspace.capture"}),
    )
    client = administration.create_client(
        display_name="Diagnostic client", preset_id=preset.id
    )
    administration.grant_development_source(client.id, SOURCE_ID)
    administration.bind_workspace(client.id, workspace_id)
    token = administration.issue_credential(client.id).token
    principal = McpAuthenticationService(sessions).authenticate(token)
    assert principal is not None
    monkeypatch.setattr(
        captures,
        "capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            WorkspaceSourceCaptureError(code)
        ),
    )

    with pytest.raises(McpAuthorizationError) as caught:
        tools.workspace_capture(
            principal,
            WorkspaceCaptureToolInput(workspace_id=workspace_id, expected_generation=0),
        )

    assert caught.value.code == "workspace_capture_rejected"
    assert caught.value.reason == reason
    structured = _tool_error_result(caught.value.code, reason=caught.value.reason)
    assert structured.structured_content == (
        {"error": "workspace_capture_rejected"}
        if reason is None
        else {"error": "workspace_capture_rejected", "reason": reason}
    )


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("source_addon_identity_changed", "source_identity_changed"),
        ("registered_source_unavailable", "registered_source_unavailable"),
        ("unexpected /home/private/token", None),
        ("unexpected_runtime", None),
    ],
)
def test_scan_error_reasons_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, reason: str | None
) -> None:
    sessions, workspaces, administration, _old_tools, workspace_id, _tree = (
        _environment(tmp_path)
    )
    captures = WorkspaceSourceCaptureService(
        sessions, data_root=tmp_path / "data", development_root=tmp_path / "sources"
    )
    tools = McpTargetToolService(
        sessions,
        McpAuthorizationService(sessions, build_mcp_tool_registry()),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        workspaces=workspaces,
        workspace_source_captures=captures,
    )
    preset = administration.create_custom_preset(
        display_name="Diagnostic scan preset",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=frozenset({"development_source.inspect"}),
    )
    client = administration.create_client(
        display_name="Diagnostic scanner", preset_id=preset.id
    )
    administration.grant_development_source(client.id, SOURCE_ID)
    administration.bind_workspace(client.id, workspace_id)
    token = administration.issue_credential(client.id).token
    principal = McpAuthenticationService(sessions).authenticate(token)
    assert principal is not None
    failure: Exception = (
        RuntimeError("/home/private/token")
        if code == "unexpected_runtime"
        else WorkspaceSourceCaptureError(code)
    )
    monkeypatch.setattr(
        captures,
        "scan_source",
        lambda *_args: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(McpAuthorizationError) as caught:
        tools.workspace_source_scan(principal, workspace_id)

    assert caught.value.code == "development_source_scan_unsafe"
    assert caught.value.reason == reason
    assert _tool_error_result(
        caught.value.code, reason=caught.value.reason
    ).structured_content == (
        {"error": "development_source_scan_unsafe"}
        if reason is None
        else {"error": "development_source_scan_unsafe", "reason": reason}
    )


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            AgentWorkspaceStorageError(
                "agent_workspace_capture_identity_changed", "/private/path"
            ),
            "source_identity_changed",
        ),
        (
            AgentWorkspaceStorageError(
                "agent_workspace_capture_reconciliation_required", "/private/path"
            ),
            "capture_reconciliation_required",
        ),
        (
            DevelopmentTreeError("development_source_unavailable", "/private/path"),
            "registered_source_unavailable",
        ),
        (
            DevelopmentSourceValidationError(
                DevelopmentSourceValidationCode.ADDON_XML_MALFORMED, "/private/path"
            ),
            "source_invalid",
        ),
        (AgentWorkspaceStorageError("unknown", "/private/path"), None),
        (DevelopmentTreeError("unknown", "/private/path"), None),
    ],
)
def test_source_error_mapping_never_emits_raw_messages(
    error: Exception, reason: str | None
) -> None:
    assert _source_capture_reason(error) == reason
    assert "/private/path" not in str(reason)


def test_capture_preflight_identifies_durable_reconciliation_state(
    tmp_path: Path,
) -> None:
    sessions, _workspaces, _administration, _tools, workspace_id, _tree = _environment(
        tmp_path
    )
    with sessions.begin() as session:
        session.execute(
            update(AgentWorkspaceRow)
            .where(AgentWorkspaceRow.id == str(workspace_id))
            .values(state=AgentWorkspaceState.RECONCILIATION_REQUIRED)
        )
    captures = WorkspaceSourceCaptureService(
        sessions, data_root=tmp_path / "data", development_root=tmp_path / "sources"
    )

    assert captures.capture_eligibility(workspace_id, 0) == (
        "capture_reconciliation_required"
    )
