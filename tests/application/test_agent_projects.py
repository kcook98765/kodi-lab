"""Trusted agents bootstrap private projects without Web source/workspace setup."""

from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from kodi_lab.application.agent_projects import ProjectCreateInput, ProjectStatusInput
from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAuthenticationService,
)
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.mcp.tools import WorkspaceEditInput
from kodi_lab.storage.models import AgentProjectRow, McpAgentWorkspaceBindingRow
from tests.application.test_mcp_workspace_mutations import _environment, _finish


@pytest.mark.parametrize("kind", ["program", "video"])
def test_new_project_needs_only_role_and_retains_owned_edits_on_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["program", "video"],
) -> None:
    sessions, workspaces, admin, tools, _submitted = _environment(tmp_path)
    workspaces._development_root = None
    client = admin.create_client(
        display_name="Project owner",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    principal = McpAuthenticationService(sessions).authenticate(
        admin.issue_credential(client.id).token
    )
    assert principal is not None
    operation = ProjectCreateInput(
        request_id=uuid4(),
        addon_id="script.agent.example"
        if kind == "program"
        else "plugin.video.agent.example",
        kind=kind,
        display_name="Agent example",
    )
    created = tools.project_create(principal, operation)
    status = tools.project_status(
        principal, ProjectStatusInput(project_id=created.project_id)
    )
    assert status.projects[0].intake_state == "seeded"
    assert status.projects[0].workspace_ids == (created.workspace.workspace_id,)
    assert created.addon_id == operation.addon_id
    assert created.workspace.source_id == created.project_id
    assert admin.get_client(client.id).development_source_grants == (
        created.project_id,
    )
    _finish(workspaces, created.workspace.creation_operation_id)
    validation = workspaces.validate_structure(
        created.workspace.workspace_id, expected_generation=0
    )
    assert validation.addon_id == operation.addon_id
    tools.workspace_edit(
        principal,
        WorkspaceEditInput(
            workspace_id=created.workspace.workspace_id,
            expected_generation=0,
            operation="create",
            relative_path="owned.py",
            content="# private edit\n",
        ),
    )
    from kodi_lab.application import agent_projects

    original_seed = agent_projects.project_seed

    def changed_template(value: ProjectCreateInput) -> dict[str, bytes]:
        return {**original_seed(value), "main.py": b"# later template\n"}

    monkeypatch.setattr(agent_projects, "project_seed", changed_template)
    replay = tools.project_create(principal, operation)
    assert replay.project_id == created.project_id
    assert replay.workspace.workspace_id == created.workspace.workspace_id
    assert workspaces.get(created.workspace.workspace_id).generation == 1
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentProjectRow)) == 1
        assert (
            session.scalar(
                select(func.count()).select_from(McpAgentWorkspaceBindingRow)
            )
            == 1
        )
    with pytest.raises(McpAuthorizationError, match="request_id_conflict"):
        tools.project_create(
            principal, operation.model_copy(update={"display_name": "Different"})
        )


def test_legacy_and_other_agent_cannot_acquire_project_source_or_workspace(
    tmp_path: Path,
) -> None:
    sessions, workspaces, admin, tools, _submitted = _environment(tmp_path)
    legacy = admin.create_client(
        display_name="Legacy", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    principal = McpAuthenticationService(sessions).authenticate(
        admin.issue_credential(legacy.id).token
    )
    assert principal is not None
    operation = ProjectCreateInput(
        request_id=uuid4(), addon_id="script.private.example", display_name="Private"
    )
    assert "project_create" not in tools._authorization.discoverable_tools(principal)
    with pytest.raises(McpAuthorizationError):
        tools.project_create(principal, operation)
    admin.set_managed_lab_role(legacy.id, True)
    assert "project_create" in tools._authorization.discoverable_tools(principal)
    created = tools.project_create(principal, operation)
    _finish(workspaces, created.workspace.creation_operation_id)
    other = admin.create_client(
        display_name="Other",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    other_principal = McpAuthenticationService(sessions).authenticate(
        admin.issue_credential(other.id).token
    )
    assert other_principal is not None
    with pytest.raises(
        McpAuthorizationError, match="project_not_found_or_not_authorized"
    ):
        tools.project_status(
            other_principal, ProjectStatusInput(project_id=created.project_id)
        )
    admin.grant_development_source(other.id, created.project_id)
    with pytest.raises(McpAuthorizationError):
        tools.workspace_create(
            other_principal, request_id=uuid4(), source_id=created.project_id
        )
    with pytest.raises(McpAuthorizationError):
        tools.workspace_status(
            other_principal, workspace_id=created.workspace.workspace_id
        )
    with pytest.raises(McpAuthorizationError, match="project_addon_id_conflict"):
        tools.project_create(
            other_principal, operation.model_copy(update={"request_id": uuid4()})
        )
    with sessions.begin() as session:
        project = session.get(AgentProjectRow, str(created.project_id))
        assert project is not None
        project.client_id = str(other.id)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


@pytest.mark.parametrize(
    "unsafe", ["../script.bad", "/tmp/evil", "script.bad/evil", "script.bad..evil"]
)
def test_project_identity_cannot_become_a_host_path(
    tmp_path: Path, unsafe: str
) -> None:
    with pytest.raises(ValidationError):
        ProjectCreateInput(request_id=uuid4(), addon_id=unsafe, display_name="Unsafe")


def test_revocation_between_seed_and_allocation_keeps_original_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application import agent_projects

    sessions, workspaces, admin, tools, _submitted = _environment(tmp_path)
    client = admin.create_client(
        display_name="Interrupted owner",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    principal = McpAuthenticationService(sessions).authenticate(
        admin.issue_credential(client.id).token
    )
    assert principal is not None
    operation = ProjectCreateInput(
        request_id=uuid4(), addon_id="script.interrupted", display_name="Interrupted"
    )
    original = agent_projects._seed_source

    def revoke(data_root: Path, project_id: UUID, files: dict[str, bytes]) -> None:
        original(data_root, project_id, files)
        admin.set_managed_lab_role(client.id, False)

    monkeypatch.setattr(agent_projects, "_seed_source", revoke)
    with pytest.raises(McpAuthorizationError):
        tools.project_create(principal, operation)
    with sessions() as session:
        project = session.scalar(select(AgentProjectRow))
        assert project is not None
        identity = project.source_id
        assert project.state == "reserved"
        assert (
            session.scalar(
                select(func.count()).select_from(McpAgentWorkspaceBindingRow)
            )
            == 0
        )
    monkeypatch.setattr(agent_projects, "_seed_source", original)
    admin.set_managed_lab_role(client.id, True)
    pending = tools.project_status(principal, ProjectStatusInput())
    assert pending.projects[0].intake_state == "reserved"
    assert pending.projects[0].workspace_ids == ()
    result = tools.project_create(principal, operation)
    assert str(result.project_id) == identity
    _finish(workspaces, result.workspace.creation_operation_id)


def test_seed_symlink_is_refused_without_touching_other_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application import agent_projects

    sessions, _workspaces, admin, tools, _submitted = _environment(tmp_path)
    client = admin.create_client(
        display_name="Symlink owner",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    principal = McpAuthenticationService(sessions).authenticate(
        admin.issue_credential(client.id).token
    )
    assert principal is not None
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (outside / "retained").write_text("unchanged")
    original = agent_projects._seed_source

    def substitute(data_root: Path, project_id: UUID, files: dict[str, bytes]) -> None:
        data_root.mkdir(mode=0o700, exist_ok=True)
        (data_root / "agent-projects").mkdir(mode=0o700)
        parent = data_root / "agent-projects" / "sources"
        parent.mkdir(mode=0o700)
        (parent / str(project_id)).symlink_to(outside, target_is_directory=True)
        original(data_root, project_id, files)

    monkeypatch.setattr(agent_projects, "_seed_source", substitute)
    with pytest.raises(McpAuthorizationError, match="project_seed_requires_review"):
        tools.project_create(
            principal,
            ProjectCreateInput(
                request_id=uuid4(), addon_id="script.symlink", display_name="Symlink"
            ),
        )
    assert (outside / "retained").read_text() == "unchanged"
    assert sorted(p.name for p in outside.iterdir()) == ["retained"]
    status = tools.project_status(principal, ProjectStatusInput())
    assert status.projects[0].intake_state == "reserved"
    assert status.projects[0].workspace_ids == ()


def test_project_quota_and_owned_inventory_pagination(tmp_path: Path) -> None:
    sessions, workspaces, admin, tools, _submitted = _environment(tmp_path)
    client = admin.create_client(
        display_name="Bounded owner",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    principal = McpAuthenticationService(sessions).authenticate(
        admin.issue_credential(client.id).token
    )
    assert principal is not None
    identities = set()
    for index in range(4):
        result = tools.project_create(
            principal,
            ProjectCreateInput(
                request_id=uuid4(),
                addon_id=f"script.quota.{index}",
                display_name=f"Project {index}",
            ),
        )
        identities.add(result.project_id)
        _finish(workspaces, result.workspace.creation_operation_id)
    first = tools.project_status(principal, ProjectStatusInput(limit=2))
    assert first.next_cursor is not None
    second = tools.project_status(
        principal, ProjectStatusInput(limit=2, cursor=first.next_cursor)
    )
    assert second.next_cursor is None
    assert {p.project_id for p in (*first.projects, *second.projects)} == identities
    with pytest.raises(McpAuthorizationError, match="project_quota_exceeded"):
        tools.project_create(
            principal,
            ProjectCreateInput(
                request_id=uuid4(),
                addon_id="script.quota.excess",
                display_name="Excess",
            ),
        )
    assert len(tools.project_status(principal, ProjectStatusInput()).projects) == 4
