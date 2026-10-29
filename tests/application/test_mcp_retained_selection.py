"""Authenticated selection keeps the existing target/source/workspace intersection."""

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import delete

from kodi_lab.application.development_sessions import RetainedSelectionRequest
from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
)
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import ManagedSelectionPreviewInput, McpTargetToolService
from kodi_lab.storage.models import (
    McpAgentWorkspaceBindingRow,
    McpClientDevelopmentSourceGrantRow,
    McpClientTargetGrantRow,
    McpCredentialRow,
)
from tests.application.test_mcp_workspace_development import SOURCE_ID
from tests.application.test_retained_development import environment


def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    s = environment(tmp_path, monkeypatch)
    s.choose("omega")
    return authorize(s)


def authorize(s: Any) -> Any:
    admin = McpAdministrationService(s.env.sessions)
    preset = admin.create_custom_preset(
        display_name="Retained narrow development",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=frozenset({"managed.lifecycle", "workspace.inspect"}),
    )
    admin.assign_client_preset(s.env.client_id, preset.id)
    admin.grant_development_source(s.env.client_id, SOURCE_ID)
    for target in s.ids.values():
        admin.assign_target_preset(target, preset.id)
        admin.grant_target(s.env.client_id, target)
    issued = admin.issue_credential(s.env.client_id)
    principal = McpAuthenticationService(s.env.sessions).authenticate(issued.token)
    assert principal is not None
    authorization = McpAuthorizationService(s.env.sessions, build_mcp_tool_registry())
    tools = McpTargetToolService(
        s.env.sessions,
        authorization,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        retained_sessions=s.service,
    )
    s.admin, s.principal, s.tools, s.token = admin, principal, tools, issued.token
    return s


def request(s: Any, generation: str) -> RetainedSelectionRequest:
    p = s.tools.managed_selection_preview(
        s.principal,
        ManagedSelectionPreviewInput(
            target_id=s.ids[generation], workspace_id=s.env.workspace_id
        ),
    )
    return RetainedSelectionRequest(
        request_id=uuid4(),
        expected_generation=p.latest_generation,
        target_id=p.target_id,
        workspace_id=p.workspace_id,
        identity=p.identity,
    )


def test_scoped_selection_replay_never_republishes_old_target_or_host_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = setup(tmp_path, monkeypatch)
    body = request(s, "nexus")
    selected = s.tools.managed_select(s.principal, body)
    assert selected.selected and selected.target_id == s.ids["nexus"]
    s.tools.managed_select(s.principal, request(s, "matrix"))
    replay = s.tools.managed_select(s.principal, body)
    assert replay.generation == selected.generation and replay.phase == "closed"
    assert not replay.selected and s.service.status().target_id == s.ids["matrix"]
    payload = replay.model_dump_json()
    assert (
        "container" not in payload
        and "image" not in payload
        and str(tmp_path) not in payload
    )
    assert len(s.records) == 4


@pytest.mark.parametrize(
    "revocation",
    ["target", "source", "binding", "credential", "client_policy", "target_policy"],
)
def test_current_authority_is_rechecked_even_for_historical_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    s = setup(tmp_path, monkeypatch)
    body = request(s, "nexus")
    selected = s.tools.managed_select(s.principal, body)
    with s.env.sessions.begin() as session:
        if revocation == "target":
            session.execute(
                delete(McpClientTargetGrantRow).where(
                    McpClientTargetGrantRow.target_id == str(s.ids["nexus"])
                )
            )
        elif revocation == "source":
            session.execute(delete(McpClientDevelopmentSourceGrantRow))
        elif revocation == "binding":
            session.execute(delete(McpAgentWorkspaceBindingRow))
        elif revocation == "credential":
            session.get(
                McpCredentialRow, str(s.principal.credential_id)
            ).revoked_at = "2030-01-01T00:00:00.000000Z"
    if revocation == "client_policy":
        s.admin.assign_client_preset(s.env.client_id, ADDON_DEVELOPMENT_PRESET_ID)
    if revocation == "target_policy":
        s.admin.assign_target_preset(s.ids["nexus"], ADDON_DEVELOPMENT_PRESET_ID)
    with pytest.raises(McpAuthorizationError):
        s.tools.managed_select(s.principal, body)
    assert s.service.status().generation == selected.generation


def test_scope_does_not_authorize_ungranted_target_or_another_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = setup(tmp_path, monkeypatch)
    with pytest.raises(McpAuthorizationError):
        s.tools.managed_selection_preview(
            s.principal,
            ManagedSelectionPreviewInput(
                target_id=uuid4(), workspace_id=s.env.workspace_id
            ),
        )
    queued = s.env.workspaces.queue_creation(SOURCE_ID)
    lease = s.env.workspaces.acquire(queued.operation_id)
    assert lease is not None
    s.env.workspaces.run(lease)
    s.admin.bind_workspace(s.env.client_id, queued.workspace_id)
    with pytest.raises(McpAuthorizationError, match="requires_administrator"):
        s.tools.managed_selection_preview(
            s.principal,
            ManagedSelectionPreviewInput(
                target_id=s.ids["nexus"], workspace_id=queued.workspace_id
            ),
        )
