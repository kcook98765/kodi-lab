from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.agent_workspaces import AgentWorkspaceService
from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
)
from kodi_lab.application.mcp_workspace_mutations import (
    McpWorkspaceCreationService,
    McpWorkspaceMutationError,
)
from kodi_lab.domain import DevelopmentSource, DevelopmentSourceKind
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import McpTargetToolService, WorkspaceEditInput
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    McpAgentWorkspaceBindingRow,
    McpDevelopmentRequestRow,
)
from kodi_lab.storage.repositories import DevelopmentSourceRepository

SOURCE_ID = UUID("62000000-0000-4000-8000-000000000001")
OTHER_SOURCE_ID = UUID("62000000-0000-4000-8000-000000000002")


def _source(source_id: UUID, locator: str, addon_id: str) -> DevelopmentSource:
    observed = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)
    return DevelopmentSource(
        id=source_id,
        logical_name=f"Source {addon_id}",
        source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
        relative_locator=locator,
        addon_id=addon_id,
        observed_name="Mutation Example",
        observed_version="1.0.0",
        extension_types=("xbmc.python.pluginsource",),
        registered_at=observed,
        validated_at=observed,
    )


def _write_source(root: Path, locator: str, addon_id: str) -> None:
    source = root / locator
    (source / "resources").mkdir(parents=True)
    (source / "addon.xml").write_text(
        f'<addon id="{addon_id}" name="Mutation" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    (source / "main.py").write_text("old\n", encoding="utf-8")
    (source / "asset.bin").write_bytes(b"\x00\xffbinary")


def _environment(
    tmp_path: Path,
) -> tuple[
    sessionmaker[Session],
    AgentWorkspaceService,
    McpAdministrationService,
    McpTargetToolService,
    list[UUID],
]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'mutation.db'}")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    development_root = tmp_path / "sources"
    _write_source(development_root, "plugin.video.one", "plugin.video.one")
    _write_source(development_root, "plugin.video.two", "plugin.video.two")
    with sessions.begin() as session:
        repository = DevelopmentSourceRepository(session)
        repository.create(_source(SOURCE_ID, "plugin.video.one", "plugin.video.one"))
        repository.create(
            _source(OTHER_SOURCE_ID, "plugin.video.two", "plugin.video.two")
        )
    workspaces = AgentWorkspaceService(
        sessions,
        data_root=tmp_path / "data",
        development_root=development_root,
    )
    submitted: list[UUID] = []
    creation = McpWorkspaceCreationService(sessions, workspaces, submitted.append)
    administration = McpAdministrationService(sessions)
    authorization = McpAuthorizationService(sessions, build_mcp_tool_registry())
    tools = McpTargetToolService(
        sessions,
        authorization,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        workspaces=workspaces,
        workspace_creation=creation,
    )
    return sessions, workspaces, administration, tools, submitted


def _principal(
    administration: McpAdministrationService,
    sessions: sessionmaker[Session],
    name: str,
) -> tuple[UUID, Any]:
    client = administration.create_client(
        display_name=name, preset_id=FULL_LAB_CONTROL_PRESET_ID
    )
    issued = administration.issue_credential(client.id)
    principal = McpAuthenticationService(sessions).authenticate(issued.token)
    assert principal is not None
    return client.id, principal


def _finish(workspaces: AgentWorkspaceService, operation_id: UUID) -> None:
    lease = workspaces.acquire(operation_id)
    assert lease is not None
    workspaces.run(lease)


def test_creation_replay_binding_restart_window_and_conflict(tmp_path: Path) -> None:
    sessions, workspaces, administration, tools, submitted = _environment(tmp_path)
    client_id, principal = _principal(administration, sessions, "Creator")
    administration.grant_development_source(client_id, SOURCE_ID)
    request_id = uuid4()

    first = tools.workspace_create(
        principal, request_id=request_id, source_id=SOURCE_ID
    )
    assert first.creation_operation_state == "queued"
    assert first.lifecycle_state == "creating"
    assert first.binding_classification == "mcp_created"
    assert submitted == [first.creation_operation_id]
    with sessions() as session:
        binding = session.get(McpAgentWorkspaceBindingRow, str(first.workspace_id))
        request = session.scalar(select(McpDevelopmentRequestRow))
        assert binding is not None
        assert binding.client_id == str(client_id)
        assert binding.binding_origin == "mcp_created"
        assert request is not None
        assert request.workspace_id == str(first.workspace_id)
        assert set(request.__dict__).isdisjoint(
            {"content", "path", "locator", "request_body", "metadata"}
        )

    # Simulate restart after the atomic marker/allocation commit but before a
    # worker consumes the queued operation.
    restarted = McpWorkspaceCreationService(sessions, workspaces, submitted.append)
    replay = restarted.create(
        client_id=client_id, request_id=request_id, source_id=SOURCE_ID
    )
    assert replay == first
    assert submitted == [first.creation_operation_id]
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentWorkspaceRow)) == 1
        assert (
            session.scalar(select(func.count()).select_from(McpDevelopmentRequestRow))
            == 1
        )

    administration.grant_development_source(client_id, OTHER_SOURCE_ID)
    with pytest.raises(McpAuthorizationError, match="request_id_conflict"):
        tools.workspace_create(
            principal, request_id=request_id, source_id=OTHER_SOURCE_ID
        )
    _finish(workspaces, first.creation_operation_id)
    other_client_id, other_principal = _principal(administration, sessions, "Other")
    with pytest.raises(
        McpAuthorizationError, match="source_not_found_or_not_authorized"
    ):
        tools.workspace_create(
            other_principal, request_id=request_id, source_id=SOURCE_ID
        )
    administration.grant_development_source(other_client_id, SOURCE_ID)
    isolated = tools.workspace_create(
        other_principal, request_id=request_id, source_id=SOURCE_ID
    )
    assert isolated.workspace_id != first.workspace_id


def test_concurrent_identical_creation_allocates_once_and_creating_quota_holds(
    tmp_path: Path,
) -> None:
    sessions, _workspaces, administration, tools, _submitted = _environment(tmp_path)
    client_id, principal = _principal(administration, sessions, "Concurrent")
    administration.grant_development_source(client_id, SOURCE_ID)
    request_id = uuid4()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: tools.workspace_create(
                    principal, request_id=request_id, source_id=SOURCE_ID
                ),
                range(2),
            )
        )
    assert results[0].workspace_id == results[1].workspace_id
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentWorkspaceRow)) == 1

    with pytest.raises(McpAuthorizationError, match="workspace_quota_exceeded"):
        tools.workspace_create(principal, request_id=uuid4(), source_id=SOURCE_ID)


def test_present_workspace_quota_counts_abandoned_bytes_and_no_eviction(
    tmp_path: Path,
) -> None:
    sessions, workspaces, administration, tools, _submitted = _environment(tmp_path)
    client_id, principal = _principal(administration, sessions, "Quota")
    administration.grant_development_source(client_id, SOURCE_ID)
    created = []
    for _index in range(4):
        result = tools.workspace_create(
            principal, request_id=uuid4(), source_id=SOURCE_ID
        )
        _finish(workspaces, result.creation_operation_id)
        created.append(result.workspace_id)
    workspaces.abandon(created[0])

    with pytest.raises(McpAuthorizationError, match="workspace_quota_exceeded"):
        tools.workspace_create(principal, request_id=uuid4(), source_id=SOURCE_ID)
    with sessions() as session:
        rows = session.scalars(select(AgentWorkspaceRow)).all()
        assert len(rows) == 4
        assert all(row.bytes_state == "present" for row in rows)


def test_request_retention_cleanup_and_capacity_are_bounded(tmp_path: Path) -> None:
    sessions, workspaces, administration, tools, submitted = _environment(tmp_path)
    client_id, principal = _principal(administration, sessions, "Retention")
    administration.grant_development_source(client_id, SOURCE_ID)
    first = tools.workspace_create(principal, request_id=uuid4(), source_id=SOURCE_ID)
    _finish(workspaces, first.creation_operation_id)
    with sessions.begin() as session:
        row = session.scalar(select(McpDevelopmentRequestRow))
        assert row is not None
        row.created_at = "2026-01-01T00:00:00.000000Z"
        row.updated_at = row.created_at

    second = tools.workspace_create(principal, request_id=uuid4(), source_id=SOURCE_ID)
    _finish(workspaces, second.creation_operation_id)
    with sessions() as session:
        requests = session.scalars(select(McpDevelopmentRequestRow)).all()
        assert len(requests) == 1
        retained_request_id = UUID(requests[0].request_id)
        assert requests[0].workspace_id == str(second.workspace_id)

    bounded = McpWorkspaceCreationService(
        sessions,
        workspaces,
        submitted.append,
        max_per_client=1,
        max_global=100,
    )
    replay = bounded.create(
        client_id=client_id,
        request_id=retained_request_id,
        source_id=SOURCE_ID,
    )
    assert replay.workspace_id == second.workspace_id
    with pytest.raises(McpWorkspaceMutationError, match="workspace_creation_ambiguous"):
        bounded.create(client_id=client_id, request_id=uuid4(), source_id=SOURCE_ID)


def test_edit_create_replace_patch_cas_and_cross_client_isolation(
    tmp_path: Path,
) -> None:
    _sessions, workspaces, administration, tools, _submitted = _environment(tmp_path)
    sessions = _sessions
    client_a, principal_a = _principal(administration, sessions, "Editor A")
    client_b, principal_b = _principal(administration, sessions, "Editor B")
    administration.grant_development_source(client_a, SOURCE_ID)
    administration.grant_development_source(client_b, SOURCE_ID)
    created = tools.workspace_create(
        principal_a, request_id=uuid4(), source_id=SOURCE_ID
    )
    _finish(workspaces, created.creation_operation_id)

    create = tools.workspace_edit(
        principal_a,
        WorkspaceEditInput(
            workspace_id=created.workspace_id,
            expected_generation=0,
            operation="create",
            relative_path="resources/new.py",
            content="one\n",
        ),
    )
    assert (create.prior_generation, create.new_generation) == (0, 1)
    assert create.file_byte_size == 4
    assert create.result_classification == "succeeded"
    replace = tools.workspace_edit(
        principal_a,
        WorkspaceEditInput(
            workspace_id=created.workspace_id,
            expected_generation=1,
            operation="replace",
            relative_path="resources/new.py",
            content="two\n",
        ),
    )
    patch = tools.workspace_edit(
        principal_a,
        WorkspaceEditInput(
            workspace_id=created.workspace_id,
            expected_generation=2,
            operation="exact_patch",
            relative_path="resources/new.py",
            expected_text="two\n",
            replacement_text="three\n",
        ),
    )
    assert (replace.new_generation, patch.new_generation) == (2, 3)
    assert (
        tools.workspace_read(
            principal_a,
            workspace_id=created.workspace_id,
            relative_path="resources/new.py",
        ).text
        == "three\n"
    )
    with pytest.raises(McpAuthorizationError, match="unsupported_file_type"):
        tools.workspace_edit(
            principal_a,
            WorkspaceEditInput(
                workspace_id=created.workspace_id,
                expected_generation=3,
                operation="replace",
                relative_path="asset.bin",
                content="not binary\n",
            ),
        )

    with pytest.raises(McpAuthorizationError, match="workspace_generation_conflict"):
        tools.workspace_edit(
            principal_a,
            WorkspaceEditInput(
                workspace_id=created.workspace_id,
                expected_generation=2,
                operation="replace",
                relative_path="resources/new.py",
                content="stale\n",
            ),
        )
    with pytest.raises(
        McpAuthorizationError, match="workspace_not_found_or_not_authorized"
    ):
        tools.workspace_edit(
            principal_b,
            WorkspaceEditInput(
                workspace_id=created.workspace_id,
                expected_generation=3,
                operation="replace",
                relative_path="resources/new.py",
                content="isolated\n",
            ),
        )

    administration.remove_development_source_grant(client_a, SOURCE_ID)
    with pytest.raises(
        McpAuthorizationError, match="workspace_not_found_or_not_authorized"
    ):
        tools.workspace_edit(
            principal_a,
            WorkspaceEditInput(
                workspace_id=created.workspace_id,
                expected_generation=3,
                operation="replace",
                relative_path="resources/new.py",
                content="revoked\n",
            ),
        )


def test_concurrent_same_generation_edits_publish_once(tmp_path: Path) -> None:
    _sessions, workspaces, administration, tools, _submitted = _environment(tmp_path)
    sessions = _sessions
    client_id, principal = _principal(administration, sessions, "CAS")
    administration.grant_development_source(client_id, SOURCE_ID)
    created = tools.workspace_create(principal, request_id=uuid4(), source_id=SOURCE_ID)
    _finish(workspaces, created.creation_operation_id)

    def edit(value: str) -> str:
        try:
            result = tools.workspace_edit(
                principal,
                WorkspaceEditInput(
                    workspace_id=created.workspace_id,
                    expected_generation=0,
                    operation="replace",
                    relative_path="main.py",
                    content=value,
                ),
            )
            return f"success:{result.new_generation}"
        except McpAuthorizationError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(edit, ("first\n", "second\n")))
    assert outcomes.count("success:1") == 1
    assert outcomes.count("workspace_generation_conflict") == 1
    status = tools.workspace_status(principal, workspace_id=created.workspace_id)
    assert status.workspaces[0].generation == 1
    assert tools.workspace_read(
        principal, workspace_id=created.workspace_id, relative_path="main.py"
    ).text in {"first\n", "second\n"}


def test_edit_schema_bounds_and_no_parent_creation(tmp_path: Path) -> None:
    sessions, workspaces, administration, tools, _submitted = _environment(tmp_path)
    client_id, principal = _principal(administration, sessions, "Bounds")
    administration.grant_development_source(client_id, SOURCE_ID)
    created = tools.workspace_create(principal, request_id=uuid4(), source_id=SOURCE_ID)
    _finish(workspaces, created.creation_operation_id)

    with pytest.raises(ValidationError):
        WorkspaceEditInput(
            workspace_id=created.workspace_id,
            expected_generation=0,
            operation="create",
            relative_path="new.py",
            content="x" * (48 * 1024 + 1),
        )
    with pytest.raises(ValidationError):
        WorkspaceEditInput(
            workspace_id=created.workspace_id,
            expected_generation=0,
            operation="replace",
            relative_path="main.py",
            content="new\n",
            replacement_text="forbidden",
        )
    with pytest.raises(McpAuthorizationError, match="path_not_allowed"):
        tools.workspace_edit(
            principal,
            WorkspaceEditInput(
                workspace_id=created.workspace_id,
                expected_generation=0,
                operation="create",
                relative_path="missing/child.py",
                content="no mkdir\n",
            ),
        )


def test_edit_rejects_injected_special_object_and_marks_integrity(
    tmp_path: Path,
) -> None:
    sessions, workspaces, administration, tools, _submitted = _environment(tmp_path)
    client_id, principal = _principal(administration, sessions, "Special object")
    administration.grant_development_source(client_id, SOURCE_ID)
    created = tools.workspace_create(principal, request_id=uuid4(), source_id=SOURCE_ID)
    _finish(workspaces, created.creation_operation_id)
    tree = tmp_path / "data" / "agent-workspaces" / str(created.workspace_id) / "tree"
    (tree / "unsafe-link").symlink_to("main.py")
    with pytest.raises(McpAuthorizationError, match="workspace_integrity_changed"):
        tools.workspace_edit(
            principal,
            WorkspaceEditInput(
                workspace_id=created.workspace_id,
                expected_generation=0,
                operation="replace",
                relative_path="main.py",
                content="blocked\n",
            ),
        )
    assert (
        tools.workspace_status(principal, workspace_id=created.workspace_id)
        .workspaces[0]
        .lifecycle_state
        == "reconciliation_required"
    )
