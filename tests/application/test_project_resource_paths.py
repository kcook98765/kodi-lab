"""Nested project files share one generation and rollback new parents safely."""

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.agent_projects import ProjectCreateInput
from kodi_lab.application.agent_workspaces import AgentWorkspaceService
from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    McpAuthenticationService,
    McpPrincipal,
)
from kodi_lab.mcp.tools import (
    McpTargetToolService,
    WorkspaceEditInput,
    WorkspaceTargetOperationInput,
    WorkspaceValidateInput,
)
from kodi_lab.storage.repositories import AgentWorkspaceRepository
from tests.application.test_mcp_workspace_mutations import (
    _environment,
    _finish,
)


def setup(
    tmp_path: Path,
) -> tuple[
    sessionmaker[Session],
    AgentWorkspaceService,
    McpTargetToolService,
    McpPrincipal,
    UUID,
]:
    sessions, workspaces, admin, tools, _submitted = _environment(tmp_path)
    client = admin.create_client(
        display_name="Resource owner",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
        trusted_managed_lab=True,
    )
    principal = McpAuthenticationService(sessions).authenticate(
        admin.issue_credential(client.id).token
    )
    assert principal is not None
    created = tools.project_create(
        principal,
        ProjectCreateInput(
            request_id=uuid4(), addon_id="script.resources", display_name="Resources"
        ),
    )
    _finish(workspaces, created.workspace.creation_operation_id)
    return sessions, workspaces, tools, principal, created.workspace.workspace_id


def test_create_nested_resource_in_one_generation_preserves_source(
    tmp_path: Path,
) -> None:
    _sessions, workspaces, tools, principal, workspace_id = setup(tmp_path)
    path = "resources/skins/Default/720p/agent.xml"
    result = tools.workspace_edit(
        principal,
        WorkspaceEditInput(
            workspace_id=workspace_id,
            expected_generation=0,
            operation="create",
            relative_path=path,
            content="<window />\n",
        ),
    )
    assert result.new_generation == 1
    assert workspaces.get(workspace_id).generation == 1
    assert workspaces.read_file(workspace_id, path).content == "<window />\n"
    source_id = workspaces.get(workspace_id).development_source_id
    assert not (workspaces.source_root(source_id) / str(source_id) / path).exists()
    validated = tools.workspace_validate(
        principal,
        WorkspaceValidateInput(workspace_id=workspace_id, expected_generation=1),
    )
    assert validated.addon_id == "script.resources"
    assert not validated.promotable
    assert validated.promotable_classification == "agent_owned_project"
    from kodi_lab.mcp.authorization import McpAuthorizationError

    with pytest.raises(McpAuthorizationError, match="promotion_not_allowed"):
        tools.promotion_request(
            principal,
            WorkspaceTargetOperationInput(
                request_id=uuid4(),
                workspace_id=workspace_id,
                expected_generation=1,
                target_id=uuid4(),
            ),
        )


@pytest.mark.parametrize("phase", ["write", "publication"])
def test_failed_publication_rolls_back_file_and_only_new_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    sessions, workspaces, tools, principal, workspace_id = setup(tmp_path)
    with sessions() as session:
        before = AgentWorkspaceRepository(session).get(workspace_id)
        assert before is not None

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fixture publication failure")

    if phase == "publication":
        monkeypatch.setattr(AgentWorkspaceRepository, "publish_mutation", fail)
    else:
        from kodi_lab.workspace import storage

        monkeypatch.setattr(storage, "_write_private_file", fail)
    with pytest.raises(RuntimeError, match="fixture publication failure"):
        tools.workspace_edit(
            principal,
            WorkspaceEditInput(
                workspace_id=workspace_id,
                expected_generation=0,
                operation="create",
                relative_path="resources/new/deep/code.py",
                content="# fixture\n",
            ),
        )
    with sessions() as session:
        after = AgentWorkspaceRepository(session).get(workspace_id)
        assert after is not None
    assert (after.generation, after.current_tree_digest) == (
        before.generation,
        before.current_tree_digest,
    )
    assert "resources" not in {
        entry.name for entry in workspaces.inspect_directory(workspace_id, "")
    }

    with sessions() as session:
        workspace = AgentWorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.generation == 0


@pytest.mark.parametrize("change", ["bytes", "mode"])
def test_unknown_bytes_in_new_parent_are_retained_for_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from kodi_lab.domain import AgentWorkspaceState
    from kodi_lab.mcp.authorization import McpAuthorizationError

    _sessions, workspaces, tools, principal, workspace_id = setup(tmp_path)
    tree = workspaces._data_root / "agent-workspaces" / str(workspace_id) / "tree"

    def unknown(*args: Any, **kwargs: Any) -> None:
        if change == "bytes":
            (tree / "resources/new/retained.py").write_text("# unowned postimage\n")
        else:
            (tree / "resources/new").chmod(0o700)
        raise RuntimeError("fixture publication failure")

    monkeypatch.setattr(AgentWorkspaceRepository, "publish_mutation", unknown)
    with pytest.raises(McpAuthorizationError):
        tools.workspace_edit(
            principal,
            WorkspaceEditInput(
                workspace_id=workspace_id,
                expected_generation=0,
                operation="create",
                relative_path="resources/new/deep/code.py",
                content="# expected\n",
            ),
        )
    if change == "bytes":
        assert (
            tree / "resources/new/retained.py"
        ).read_text() == "# unowned postimage\n"
    else:
        assert (tree / "resources/new").is_dir()
        assert (tree / "resources/new").stat().st_mode & 0o777 == 0o700
    assert (
        workspaces.get(workspace_id).state
        is AgentWorkspaceState.RECONCILIATION_REQUIRED
    )
    assert workspaces.get(workspace_id).generation == 0


def test_registered_source_workspace_keeps_existing_parent_rule(tmp_path: Path) -> None:
    from kodi_lab.workspace import AgentWorkspaceStorageError
    from tests.application.test_mcp_workspace_mutations import SOURCE_ID

    _sessions, workspaces, _admin, _tools, _submitted = _environment(tmp_path)
    created = workspaces.queue_creation(SOURCE_ID)
    _finish(workspaces, created.operation_id)
    with pytest.raises(AgentWorkspaceStorageError, match="parent_unsafe"):
        workspaces.create_text_file(
            created.workspace_id,
            expected_generation=0,
            relative_path="resources/new/module.py",
            content="# unchanged contract\n",
        )
    assert workspaces.get(created.workspace_id).generation == 0
