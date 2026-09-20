from __future__ import annotations

import hashlib
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.agent_workspaces import AgentWorkspaceService
from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    McpAdministrationError,
    McpAdministrationService,
    McpAuthenticationService,
)
from kodi_lab.domain import (
    AgentWorkspaceState,
    DevelopmentSource,
    DevelopmentSourceKind,
)
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import McpTargetToolService
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import AgentWorkspaceRow, McpAgentWorkspaceBindingRow
from kodi_lab.storage.repositories import DevelopmentSourceRepository

SOURCE_ID = UUID("61000000-0000-4000-8000-000000000001")
OTHER_SOURCE_ID = UUID("61000000-0000-4000-8000-000000000002")


def _source(source_id: UUID, locator: str, addon_id: str) -> DevelopmentSource:
    observed = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    return DevelopmentSource(
        id=source_id,
        logical_name=f"Source {addon_id}",
        source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
        relative_locator=locator,
        addon_id=addon_id,
        observed_name=f"Add-on {addon_id}",
        observed_version="1.0.0",
        extension_types=("xbmc.python.pluginsource",),
        registered_at=observed,
        validated_at=observed,
    )


def _write_source(root: Path, locator: str, addon_id: str) -> Path:
    source = root / locator
    source.mkdir(parents=True)
    (source / "addon.xml").write_text(
        f'<addon id="{addon_id}" name="Example" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    return source


def _environment(
    tmp_path: Path,
) -> tuple[
    sessionmaker[Session],
    AgentWorkspaceService,
    McpAdministrationService,
    McpTargetToolService,
    UUID,
    Path,
]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'mcp-development.db'}")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    development_root = tmp_path / "sources"
    source = _write_source(
        development_root, "plugin.video.example", "plugin.video.example"
    )
    _write_source(development_root, "plugin.video.other", "plugin.video.other")
    (source / "main.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    for name in ("Case.py", "case.py", "é.py", "e\N{COMBINING ACUTE ACCENT}.py"):
        (source / name).write_text(name, encoding="utf-8")
    (source / "\N{ONE DOT LEADER}\N{ONE DOT LEADER}").write_text(
        "lookalike\n", encoding="utf-8"
    )
    with sessions.begin() as session:
        repository = DevelopmentSourceRepository(session)
        repository.create(
            _source(SOURCE_ID, "plugin.video.example", "plugin.video.example")
        )
        repository.create(
            _source(OTHER_SOURCE_ID, "plugin.video.other", "plugin.video.other")
        )
    workspaces = AgentWorkspaceService(
        sessions,
        data_root=tmp_path / "data",
        development_root=development_root,
        uuid_factory=uuid4,
    )
    queued = workspaces.queue_creation(SOURCE_ID)
    lease = workspaces.acquire(queued.operation_id)
    assert lease is not None
    workspaces.run(lease)
    administration = McpAdministrationService(sessions)
    authorization = McpAuthorizationService(sessions, build_mcp_tool_registry())
    tools = McpTargetToolService(
        sessions,
        authorization,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        workspaces=workspaces,
    )
    return (
        sessions,
        workspaces,
        administration,
        tools,
        queued.workspace_id,
        tmp_path / "data" / "agent-workspaces" / str(queued.workspace_id) / "tree",
    )


def _principal(
    administration: McpAdministrationService,
    sessions: sessionmaker[Session],
    name: str,
) -> tuple[UUID, str, Any]:
    client = administration.create_client(
        display_name=name, preset_id=FULL_LAB_CONTROL_PRESET_ID
    )
    token = administration.issue_credential(client.id).token
    principal = McpAuthenticationService(sessions).authenticate(token)
    assert principal is not None
    return client.id, token, principal


def test_grants_binding_and_bounded_read_tools(tmp_path: Path) -> None:
    sessions, _workspaces, administration, tools, workspace_id, _tree = _environment(
        tmp_path
    )
    client_id, _token, principal = _principal(administration, sessions, "Developer")

    administration.grant_development_source(client_id, SOURCE_ID)
    with pytest.raises(
        McpAdministrationError, match="mcp_development_source_already_granted"
    ):
        administration.grant_development_source(client_id, SOURCE_ID)
    eligible = administration.list_eligible_unbound_workspaces(client_id)
    assert [item.workspace_id for item in eligible] == [workspace_id]
    binding = administration.bind_workspace(client_id, workspace_id)
    assert binding.binding_origin == "web_admin"
    with pytest.raises(McpAdministrationError, match="mcp_workspace_already_bound"):
        administration.bind_workspace(client_id, workspace_id)

    sources = tools.development_source_list(principal)
    assert [item.source_id for item in sources.sources] == [SOURCE_ID]
    assert "plugin.video.example" in sources.model_dump_json()
    assert str(tmp_path) not in sources.model_dump_json()
    status = tools.workspace_status(principal, workspace_id=workspace_id)
    assert status.workspaces[0].lifecycle_state == "ready"
    assert status.workspaces[0].bytes_state == "present"

    tree = tools.workspace_tree(principal, workspace_id=workspace_id, limit=3)
    assert len(tree.entries) == 3
    assert tree.next_cursor is not None
    second = tools.workspace_tree(
        principal, workspace_id=workspace_id, cursor=tree.next_cursor, limit=200
    )
    paths = [item.path for item in (*tree.entries, *second.entries)]
    assert paths == sorted(paths, key=lambda value: value.encode("utf-8"))
    assert {"Case.py", "case.py", "é.py", "é.py"}.issubset(paths)

    read = tools.workspace_read(
        principal,
        workspace_id=workspace_id,
        relative_path="main.py",
        start_line=2,
        max_lines=1,
    )
    assert read.text == "two\n"
    assert read.next_line == 3
    assert read.truncated
    assert read.sha256 == hashlib.sha256(b"one\ntwo\nthree\n").hexdigest()
    assert len(read.model_dump_json().encode()) < 48 * 1024


def test_authority_intersection_revocation_disable_rotation_and_isolation(
    tmp_path: Path,
) -> None:
    sessions, _workspaces, administration, tools, workspace_id, _tree = _environment(
        tmp_path
    )
    client_a, old_token, principal_a = _principal(administration, sessions, "A")
    client_b, _token_b, principal_b = _principal(administration, sessions, "B")
    administration.grant_development_source(client_a, SOURCE_ID)
    administration.grant_development_source(client_b, SOURCE_ID)
    administration.grant_development_source(client_b, OTHER_SOURCE_ID)
    administration.bind_workspace(client_a, workspace_id)

    assert tools.workspace_status(principal_a, workspace_id=workspace_id).workspaces
    assert tools.workspace_status(principal_b).workspaces == ()
    assert [
        item.source_id for item in tools.development_source_list(principal_a).sources
    ] == [SOURCE_ID]
    for operation in (
        lambda: tools.workspace_status(principal_b, workspace_id=workspace_id),
        lambda: tools.workspace_tree(principal_b, workspace_id=workspace_id),
        lambda: tools.workspace_read(
            principal_b, workspace_id=workspace_id, relative_path="main.py"
        ),
    ):
        with pytest.raises(
            McpAuthorizationError,
            match="workspace_not_found_or_not_authorized",
        ):
            operation()

    administration.remove_development_source_grant(client_a, SOURCE_ID)
    for operation in (
        lambda: tools.workspace_status(principal_a, workspace_id=workspace_id),
        lambda: tools.workspace_tree(principal_a, workspace_id=workspace_id),
        lambda: tools.workspace_read(
            principal_a, workspace_id=workspace_id, relative_path="main.py"
        ),
    ):
        with pytest.raises(
            McpAuthorizationError,
            match="workspace_not_found_or_not_authorized",
        ):
            operation()
    with sessions() as session:
        assert session.get(McpAgentWorkspaceBindingRow, str(workspace_id)) is not None
    administration.grant_development_source(client_a, SOURCE_ID)
    assert tools.workspace_status(principal_a, workspace_id=workspace_id).workspaces

    administration.set_client_enabled(client_a, False)
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        tools.workspace_status(principal_a, workspace_id=workspace_id)
    administration.set_client_enabled(client_a, True)
    old_credential = administration.get_client(client_a).credentials[0]
    parallel = administration.issue_credential(client_a)
    administration.revoke_credential(client_a, old_credential.id)
    assert McpAuthenticationService(sessions).authenticate(old_token) is None
    parallel_principal = McpAuthenticationService(sessions).authenticate(parallel.token)
    assert parallel_principal is not None
    assert tools.workspace_status(
        parallel_principal, workspace_id=workspace_id
    ).workspaces
    expiring = administration.issue_credential(client_a, lifetime=timedelta(minutes=1))
    expired_authentication = McpAuthenticationService(
        sessions,
        clock=lambda: expiring.credential.created_at + timedelta(minutes=2),
    )
    assert expired_authentication.authenticate(expiring.token) is None
    assert tools.workspace_status(
        parallel_principal, workspace_id=workspace_id
    ).workspaces
    rotated = administration.rotate_credential(client_a, parallel.credential.id)
    assert McpAuthenticationService(sessions).authenticate(parallel.token) is None
    rotated_principal = McpAuthenticationService(sessions).authenticate(rotated.token)
    assert rotated_principal is not None
    assert rotated_principal.client_id == client_a
    assert tools.workspace_status(
        rotated_principal, workspace_id=workspace_id
    ).workspaces


def test_phase9_path_unicode_and_special_object_safety(tmp_path: Path) -> None:
    sessions, _workspaces, administration, tools, workspace_id, tree = _environment(
        tmp_path
    )
    client_id, _token, principal = _principal(administration, sessions, "Paths")
    administration.grant_development_source(client_id, SOURCE_ID)
    administration.bind_workspace(client_id, workspace_id)

    assert (
        tools.workspace_read(
            principal,
            workspace_id=workspace_id,
            relative_path="\N{ONE DOT LEADER}\N{ONE DOT LEADER}",
        ).text
        == "lookalike\n"
    )
    for path in ("/main.py", "../main.py", ".", "a/../b", "a\\b", "\ud800"):
        with pytest.raises(McpAuthorizationError, match="path_not_allowed"):
            tools.workspace_read(
                principal, workspace_id=workspace_id, relative_path=path
            )

    (tree / "symlink").symlink_to("main.py")
    os.link(tree / "main.py", tree / "hardlink")
    os.mkfifo(tree / "fifo")
    unix_socket = socket.socket(socket.AF_UNIX)
    short_tree = Path("/tmp") / f"kl-mcp-socket-{uuid4().hex[:8]}"
    short_tree.symlink_to(tree, target_is_directory=True)
    unix_socket.bind(str(short_tree / "socket"))
    try:
        with pytest.raises(McpAuthorizationError, match="path_not_allowed"):
            tools.workspace_read(
                principal, workspace_id=workspace_id, relative_path="symlink"
            )
        for path in ("hardlink", "fifo", "socket"):
            with pytest.raises(McpAuthorizationError, match="unsupported_file_type"):
                tools.workspace_read(
                    principal, workspace_id=workspace_id, relative_path=path
                )
        with pytest.raises(McpAuthorizationError, match="unsupported_file_type"):
            tools.workspace_tree(principal, workspace_id=workspace_id)
    finally:
        unix_socket.close()
        short_tree.unlink()


def test_source_grant_and_binding_rows_have_no_content_or_locator(
    tmp_path: Path,
) -> None:
    sessions, _workspaces, administration, _tools, workspace_id, _tree = _environment(
        tmp_path
    )
    client_id, _token, _principal_value = _principal(
        administration, sessions, "Metadata"
    )
    administration.grant_development_source(client_id, SOURCE_ID)
    administration.bind_workspace(client_id, workspace_id)
    with sessions() as session:
        binding = session.scalar(select(McpAgentWorkspaceBindingRow))
    assert binding is not None
    assert set(binding.__dict__).isdisjoint(
        {"content", "relative_locator", "path", "hash", "metadata"}
    )


def test_development_response_bounds_and_reconciliation_policy(tmp_path: Path) -> None:
    sessions, _workspaces, administration, tools, workspace_id, tree = _environment(
        tmp_path
    )
    client_id, _token, principal = _principal(administration, sessions, "Bounds")
    administration.grant_development_source(client_id, SOURCE_ID)
    administration.bind_workspace(client_id, workspace_id)

    (tree / "long-line.txt").write_text("x" * (45 * 1024), encoding="utf-8")
    with pytest.raises(McpAuthorizationError, match="response_too_large"):
        tools.workspace_read(
            principal, workspace_id=workspace_id, relative_path="long-line.txt"
        )
    for index in range(200):
        name = f"bounded-{index:03d}-" + "x" * 225
        (tree / name).write_text("x", encoding="utf-8")
    with pytest.raises(McpAuthorizationError, match="response_too_large"):
        tools.workspace_tree(principal, workspace_id=workspace_id, limit=200)

    with sessions.begin() as session:
        workspace = session.get(AgentWorkspaceRow, str(workspace_id))
        assert workspace is not None
        workspace.state = AgentWorkspaceState.RECONCILIATION_REQUIRED
    status = tools.workspace_status(principal, workspace_id=workspace_id)
    assert status.workspaces[0].lifecycle_state == "reconciliation_required"
    with pytest.raises(McpAuthorizationError, match="workspace_not_ready"):
        tools.workspace_tree(principal, workspace_id=workspace_id)
    with pytest.raises(McpAuthorizationError, match="workspace_not_ready"):
        tools.workspace_read(
            principal, workspace_id=workspace_id, relative_path="main.py"
        )
