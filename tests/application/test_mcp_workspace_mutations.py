from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.agent_workspace_cleanup import AgentWorkspaceCleanupService
from kodi_lab.application.agent_workspaces import (
    AgentWorkspaceService,
    AgentWorkspaceStructuralChangeRead,
    AgentWorkspaceStructuralValidationRead,
)
from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
)
from kodi_lab.application.mcp_workspace_mutations import (
    McpWorkspaceCreationService,
    McpWorkspaceMutationError,
)
from kodi_lab.application.mcp_workspace_retention import (
    DISABLED_CLIENT_QUARANTINE,
    TERMINAL_BYTE_RETENTION,
    McpWorkspaceRetentionService,
)
from kodi_lab.domain import (
    AgentWorkspaceState,
    DevelopmentSource,
    DevelopmentSourceKind,
)
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import (
    McpTargetToolService,
    WorkspaceEditInput,
    WorkspaceValidateInput,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    AgentWorkspaceCreationOperationRow,
    AgentWorkspaceRow,
    McpAgentWorkspaceBindingRow,
    McpClientRow,
    McpDevelopmentRequestRow,
    McpSecurityEventRow,
)
from kodi_lab.storage.repositories import DevelopmentSourceRepository
from kodi_lab.workspace import workspace_coordination_lock

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


def test_malformed_creation_terminal_phase_is_retained(tmp_path: Path) -> None:
    sessions, workspaces, administration, _tools, submitted = _environment(tmp_path)
    client_id, _principal_value = _principal(
        administration, sessions, "Malformed creation"
    )
    administration.grant_development_source(client_id, SOURCE_ID)
    now = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)
    service = McpWorkspaceCreationService(
        sessions, workspaces, submitted.append, clock=lambda: now
    )
    created = service.create(
        client_id=client_id, request_id=uuid4(), source_id=SOURCE_ID
    )
    _finish(workspaces, created.creation_operation_id)
    with sessions.begin() as session:
        session.execute(
            update(AgentWorkspaceCreationOperationRow)
            .where(
                AgentWorkspaceCreationOperationRow.id
                == str(created.creation_operation_id)
            )
            .values(phase="queued")
        )
        session.execute(
            update(McpDevelopmentRequestRow)
            .where(
                McpDevelopmentRequestRow.creation_operation_id
                == str(created.creation_operation_id)
            )
            .values(
                created_at="2026-01-01T00:00:00.000000Z",
                updated_at="2026-01-01T00:00:00.000000Z",
            )
        )
        service._prune_expired_requests(session, now)
        assert session.scalar(select(McpDevelopmentRequestRow)) is not None


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


def _ready_owned_workspace(
    tmp_path: Path, name: str = "Validator"
) -> tuple[
    sessionmaker[Session],
    AgentWorkspaceService,
    McpAdministrationService,
    McpTargetToolService,
    UUID,
    Any,
    Any,
]:
    sessions, workspaces, administration, tools, _submitted = _environment(tmp_path)
    client_id, principal = _principal(administration, sessions, name)
    administration.grant_development_source(client_id, SOURCE_ID)
    created = tools.workspace_create(principal, request_id=uuid4(), source_id=SOURCE_ID)
    _finish(workspaces, created.creation_operation_id)
    return (
        sessions,
        workspaces,
        administration,
        tools,
        client_id,
        principal,
        created,
    )


def test_workspace_validate_exact_generation_and_change_classification(
    tmp_path: Path,
) -> None:
    (
        _sessions,
        _workspaces,
        _administration,
        tools,
        _client_id,
        principal,
        created,
    ) = _ready_owned_workspace(tmp_path)
    unchanged = tools.workspace_validate(
        principal,
        WorkspaceValidateInput(
            workspace_id=created.workspace_id, expected_generation=0
        ),
    )
    assert unchanged.identity_classification == "stable"
    assert unchanged.changed_file_count == 0
    assert unchanged.promotable
    assert unchanged.changed_paths == ()

    tools.workspace_edit(
        principal,
        WorkspaceEditInput(
            workspace_id=created.workspace_id,
            expected_generation=0,
            operation="create",
            relative_path="resources/new.py",
            content="new\n",
        ),
    )
    changed = tools.workspace_validate(
        principal,
        WorkspaceValidateInput(
            workspace_id=created.workspace_id, expected_generation=1
        ),
    )
    assert (changed.changed_file_count, changed.added_file_count) == (1, 1)
    assert [(item.path, item.change_kind) for item in changed.changed_paths] == [
        ("resources/new.py", "added")
    ]
    with pytest.raises(McpAuthorizationError, match="workspace_generation_conflict"):
        tools.workspace_validate(
            principal,
            WorkspaceValidateInput(
                workspace_id=created.workspace_id, expected_generation=0
            ),
        )


@pytest.mark.parametrize(
    ("addon_id", "version", "point", "id_state", "version_state", "type_state"),
    (
        (
            "plugin.video.one",
            "2.0.0",
            "xbmc.python.pluginsource",
            "unchanged",
            "changed",
            "unchanged",
        ),
        (
            "plugin.video.changed",
            "1.0.0",
            "xbmc.python.pluginsource",
            "changed",
            "unchanged",
            "unchanged",
        ),
        (
            "plugin.video.one",
            "1.0.0",
            "xbmc.service",
            "unchanged",
            "unchanged",
            "changed",
        ),
    ),
)
def test_workspace_validate_addon_identity_classifications(
    tmp_path: Path,
    addon_id: str,
    version: str,
    point: str,
    id_state: str,
    version_state: str,
    type_state: str,
) -> None:
    *_, tools, _client_id, principal, created = _ready_owned_workspace(tmp_path)
    descriptor = (
        f'<addon id="{addon_id}" name="Mutation" version="{version}">'
        f'<extension point="{point}" library="main.py" />'
        "</addon>\n"
    )
    tools.workspace_edit(
        principal,
        WorkspaceEditInput(
            workspace_id=created.workspace_id,
            expected_generation=0,
            operation="replace",
            relative_path="addon.xml",
            content=descriptor,
        ),
    )
    result = tools.workspace_validate(
        principal,
        WorkspaceValidateInput(
            workspace_id=created.workspace_id, expected_generation=1
        ),
    )
    assert result.addon_id_classification == id_state
    assert result.version_classification == version_state
    assert result.extension_type_classification == type_state
    assert result.promotable is (id_state == "unchanged")


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        ("delete", "unsupported_deletion"),
        ("missing_addon", "unsupported_deletion"),
        ("binary", "unsupported_file_type"),
        ("type", "unsupported_type_transition"),
        ("unsafe", "unsupported_file_type"),
        ("malformed_addon", "validation_failed"),
    ),
)
def test_workspace_validate_rejects_unsupported_structure(
    tmp_path: Path, mutation: str, error: str
) -> None:
    *_, tools, _client_id, principal, created = _ready_owned_workspace(tmp_path)
    tree = tmp_path / "data" / "agent-workspaces" / str(created.workspace_id) / "tree"
    if mutation in {"delete", "missing_addon"}:
        (tree / ("addon.xml" if mutation == "missing_addon" else "main.py")).unlink()
    elif mutation == "binary":
        (tree / "asset.bin").write_bytes(b"\x00changed\xff")
    elif mutation == "type":
        (tree / "main.py").unlink()
        (tree / "main.py").mkdir()
    elif mutation == "unsafe":
        (tree / "unsafe").symlink_to("main.py")
    else:
        (tree / "addon.xml").write_text("<addon", encoding="utf-8")
    with pytest.raises(McpAuthorizationError, match=error):
        tools.workspace_validate(
            principal,
            WorkspaceValidateInput(
                workspace_id=created.workspace_id, expected_generation=0
            ),
        )


def test_workspace_validate_isolation_revocation_and_closed_schema(
    tmp_path: Path,
) -> None:
    sessions, _workspaces, administration, tools, client_id, principal, created = (
        _ready_owned_workspace(tmp_path)
    )
    other_id, other = _principal(administration, sessions, "Other validator")
    administration.grant_development_source(other_id, SOURCE_ID)
    with pytest.raises(
        McpAuthorizationError, match="workspace_not_found_or_not_authorized"
    ):
        tools.workspace_validate(
            other,
            WorkspaceValidateInput(
                workspace_id=created.workspace_id, expected_generation=0
            ),
        )
    administration.remove_development_source_grant(client_id, SOURCE_ID)
    with pytest.raises(
        McpAuthorizationError, match="workspace_not_found_or_not_authorized"
    ):
        tools.workspace_validate(
            principal,
            WorkspaceValidateInput(
                workspace_id=created.workspace_id, expected_generation=0
            ),
        )
    for field in (
        "command",
        "module",
        "pytest_selector",
        "shell_args",
        "env",
        "script_path",
        "validation_plugin",
        "package_manager",
        "timeout",
    ):
        with pytest.raises(ValidationError):
            WorkspaceValidateInput.model_validate(
                {
                    "workspace_id": str(created.workspace_id),
                    "expected_generation": 0,
                    field: "forbidden",
                }
            )


def test_workspace_validate_path_bound_and_busy_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    *_, workspaces, _administration, tools, _client_id, principal, created = (
        _ready_owned_workspace(tmp_path)
    )
    original = workspaces.validate_structure
    entered = Event()
    release = Event()

    def blocked(workspace_id: UUID, *, expected_generation: int) -> Any:
        entered.set()
        assert release.wait(timeout=5)
        return original(workspace_id, expected_generation=expected_generation)

    monkeypatch.setattr(workspaces, "validate_structure", blocked)
    request = WorkspaceValidateInput(
        workspace_id=created.workspace_id, expected_generation=0
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(tools.workspace_validate, principal, request)
        assert entered.wait(timeout=5)
        with pytest.raises(McpAuthorizationError, match="validation_busy"):
            tools.workspace_validate(principal, request)
        release.set()
        assert running.result().changed_file_count == 0

    changes = tuple(
        AgentWorkspaceStructuralChangeRead(
            path=f"resources/generated-{index:03d}.py", change_kind="added"
        )
        for index in range(70)
    )
    monkeypatch.setattr(
        workspaces,
        "validate_structure",
        lambda _workspace_id, *, expected_generation: (
            AgentWorkspaceStructuralValidationRead(
                workspace_id=created.workspace_id,
                development_source_id=SOURCE_ID,
                observed_generation=expected_generation,
                validated_at=datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
                addon_id="plugin.video.one",
                addon_version="1.0.0",
                extension_types=("xbmc.python.pluginsource",),
                addon_id_classification="unchanged",
                version_classification="unchanged",
                extension_type_classification="unchanged",
                promotable=True,
                promotable_classification="promotable",
                changes=changes,
            )
        ),
    )
    bounded = tools.workspace_validate(principal, request)
    assert bounded.changed_file_count == 70
    assert len(bounded.changed_paths) == 64
    assert bounded.truncated
    assert len(bounded.model_dump_json().encode()) < 48 * 1024


def _timestamp(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _retention(
    tmp_path: Path,
    sessions: sessionmaker[Session],
    workspaces: AgentWorkspaceService,
    now: datetime,
    *,
    failure_injector: Any = lambda _phase: None,
) -> tuple[
    AgentWorkspaceCleanupService,
    McpWorkspaceRetentionService,
    list[UUID],
]:
    cleanup = AgentWorkspaceCleanupService(
        sessions,
        data_root=tmp_path / "data",
        clock=lambda: now,
        failure_injector=failure_injector,
    )
    submitted: list[UUID] = []
    retention = McpWorkspaceRetentionService(
        sessions,
        workspaces,
        cleanup,
        submitted.append,
        data_root=tmp_path / "data",
        clock=lambda: now,
    )
    return cleanup, retention, submitted


def _set_terminal_time(
    sessions: sessionmaker[Session], workspace_id: UUID, value: datetime
) -> None:
    with sessions.begin() as session:
        row = session.get(AgentWorkspaceRow, str(workspace_id))
        assert row is not None
        row.abandoned_at = _timestamp(value)


def _set_disabled_time(
    sessions: sessionmaker[Session], client_id: UUID, value: datetime
) -> None:
    with sessions.begin() as session:
        row = session.get(McpClientRow, str(client_id))
        assert row is not None and not row.enabled
        row.disabled_at = _timestamp(value)


def test_retention_terminal_grace_cleanup_quota_and_history(tmp_path: Path) -> None:
    sessions, workspaces, _administration, _tools, _client_id, _principal, created = (
        _ready_owned_workspace(tmp_path, "Terminal retention")
    )
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    cleanup, retention, submitted = _retention(tmp_path, sessions, workspaces, now)
    workspaces.abandon(created.workspace_id)
    _set_terminal_time(
        sessions,
        created.workspace_id,
        now - TERMINAL_BYTE_RETENTION + timedelta(seconds=1),
    )
    assert retention.sweep(force=True).cleanups_queued == 0
    assert not submitted
    assert workspaces.get(created.workspace_id).bytes_state.value == "present"

    _set_terminal_time(sessions, created.workspace_id, now - TERMINAL_BYTE_RETENTION)
    assert retention.sweep(force=True).cleanups_queued == 1
    lease = cleanup.acquire(submitted.pop())
    assert lease is not None
    cleanup.run(lease)
    retained = workspaces.get(created.workspace_id)
    assert retained.bytes_state.value == "cleaned"
    assert retained.state.value == "abandoned"
    with sessions() as session:
        assert (
            session.get(McpAgentWorkspaceBindingRow, str(created.workspace_id))
            is not None
        )
        present = session.scalar(
            select(func.count())
            .select_from(McpAgentWorkspaceBindingRow)
            .join(
                AgentWorkspaceRow,
                AgentWorkspaceRow.id == McpAgentWorkspaceBindingRow.workspace_id,
            )
            .where(AgentWorkspaceRow.bytes_state == "present")
        )
        assert present == 0


def test_disabled_client_quarantine_abandon_and_reenable_cancellation(
    tmp_path: Path,
) -> None:
    sessions, workspaces, administration, tools, client_id, principal, created = (
        _ready_owned_workspace(tmp_path, "Quarantine")
    )
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    _cleanup, retention, submitted = _retention(tmp_path, sessions, workspaces, now)
    administration.set_client_enabled(client_id, False)
    with pytest.raises(McpAuthorizationError, match="permission_denied"):
        tools.workspace_validate(
            principal,
            WorkspaceValidateInput(
                workspace_id=created.workspace_id, expected_generation=0
            ),
        )
    _set_disabled_time(
        sessions,
        client_id,
        now - DISABLED_CLIENT_QUARANTINE + timedelta(seconds=1),
    )
    projection = retention.projection(created.workspace_id)
    assert projection.classification == "disabled_client_quarantine"
    assert retention.sweep(force=True).lifecycle_transitions == 0
    assert workspaces.get(created.workspace_id).state.value == "ready"
    administration.set_client_enabled(client_id, True)
    assert retention.projection(created.workspace_id).classification == "active"
    with sessions() as session:
        client = session.get(McpClientRow, str(client_id))
        assert client is not None and client.disabled_at is None
        events = set(session.scalars(select(McpSecurityEventRow.event_kind)).all())
        assert "retention_quarantine_entered" in events
        assert "retention_quarantine_cancelled" in events
    assert not submitted
    assert tools.workspace_validate(
        principal,
        WorkspaceValidateInput(
            workspace_id=created.workspace_id, expected_generation=0
        ),
    ).promotable

    administration.set_client_enabled(client_id, False)
    _set_disabled_time(sessions, client_id, now - DISABLED_CLIENT_QUARANTINE)
    result = retention.sweep(force=True)
    assert result.lifecycle_transitions == 1
    assert workspaces.get(created.workspace_id).state.value == "abandoned"
    assert not submitted


def test_retention_never_ages_active_or_reconciliation_workspace(
    tmp_path: Path,
) -> None:
    sessions, workspaces, _administration, _tools, _client_id, _principal, created = (
        _ready_owned_workspace(tmp_path, "Protected retention")
    )
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    _cleanup, retention, submitted = _retention(tmp_path, sessions, workspaces, now)
    assert retention.sweep(force=True).inspected == 0
    assert workspaces.get(created.workspace_id).state.value == "ready"
    with sessions.begin() as session:
        row = session.get(AgentWorkspaceRow, str(created.workspace_id))
        assert row is not None
        row.state = AgentWorkspaceState.RECONCILIATION_REQUIRED
    assert retention.sweep(force=True).inspected == 0
    assert not submitted
    projection = retention.projection(created.workspace_id)
    assert projection.classification == "manual_attention_required"
    assert projection.automatic_cleanup_blocked


def test_retention_promoted_terminal_uses_same_seven_day_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, workspaces, _administration, _tools, _client_id, _principal, created = (
        _ready_owned_workspace(tmp_path, "Promoted retention")
    )
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    cleanup, retention, submitted = _retention(tmp_path, sessions, workspaces, now)
    with sessions.begin() as session:
        row = session.get(AgentWorkspaceRow, str(created.workspace_id))
        assert row is not None
        row.state = AgentWorkspaceState.PROMOTED
        row.promoted_at = _timestamp(now - TERMINAL_BYTE_RETENTION)
    operation_id = uuid4()
    monkeypatch.setattr(cleanup, "eligibility_code", lambda _workspace_id: None)
    monkeypatch.setattr(
        cleanup,
        "queue",
        lambda _workspace_id: SimpleNamespace(operation_id=operation_id),
    )
    assert retention.sweep(force=True).cleanups_queued == 1
    assert submitted == [operation_id]


def test_retention_skips_workspace_lock_and_failed_cleanup_requires_attention(
    tmp_path: Path,
) -> None:
    sessions, workspaces, administration, _tools, client_id, _principal, created = (
        _ready_owned_workspace(tmp_path, "Retention race")
    )
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    administration.set_client_enabled(client_id, False)
    _set_disabled_time(sessions, client_id, now - DISABLED_CLIENT_QUARANTINE)
    _cleanup, retention, submitted = _retention(tmp_path, sessions, workspaces, now)
    with workspace_coordination_lock(
        tmp_path / "data", created.workspace_id, exclusive=False
    ):
        assert retention.sweep(force=True).lifecycle_transitions == 0
    assert workspaces.get(created.workspace_id).state.value == "ready"
    assert retention.sweep(force=True).lifecycle_transitions == 1

    _set_terminal_time(sessions, created.workspace_id, now - TERMINAL_BYTE_RETENTION)

    def fail_before_marker(phase: str) -> None:
        if phase == "before_deletion_marker":
            raise RuntimeError("bounded injected failure")

    cleanup, failing_retention, failing_submitted = _retention(
        tmp_path,
        sessions,
        workspaces,
        now,
        failure_injector=fail_before_marker,
    )
    assert failing_retention.sweep(force=True).cleanups_queued == 1
    lease = cleanup.acquire(failing_submitted.pop())
    assert lease is not None
    cleanup.run(lease)
    assert workspaces.get(created.workspace_id).bytes_state.value == "present"
    projection = failing_retention.projection(created.workspace_id)
    assert projection.classification == "manual_attention_required"
    assert projection.automatic_cleanup_blocked
    assert failing_retention.sweep(force=True).cleanups_queued == 0
    assert not submitted


def test_slice3_disposable_validation_and_retention_end_to_end(
    tmp_path: Path,
) -> None:
    sessions, workspaces, administration, tools, client_id, principal, created = (
        _ready_owned_workspace(tmp_path, "Slice 3 end to end")
    )
    edited = tools.workspace_edit(
        principal,
        WorkspaceEditInput(
            workspace_id=created.workspace_id,
            expected_generation=0,
            operation="replace",
            relative_path="main.py",
            content="changed through the bounded edit service\n",
        ),
    )
    validated = tools.workspace_validate(
        principal,
        WorkspaceValidateInput(
            workspace_id=created.workspace_id,
            expected_generation=edited.new_generation,
        ),
    )
    assert validated.observed_generation == 1
    assert validated.modified_file_count == 1
    assert [(item.path, item.change_kind) for item in validated.changed_paths] == [
        ("main.py", "modified")
    ]
    with pytest.raises(McpAuthorizationError, match="workspace_generation_conflict"):
        tools.workspace_validate(
            principal,
            WorkspaceValidateInput(
                workspace_id=created.workspace_id, expected_generation=0
            ),
        )

    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    cleanup, retention, submitted = _retention(tmp_path, sessions, workspaces, now)
    administration.set_client_enabled(client_id, False)
    assert retention.projection(created.workspace_id).classification == (
        "disabled_client_quarantine"
    )
    administration.set_client_enabled(client_id, True)
    assert retention.projection(created.workspace_id).classification == "active"

    workspaces.abandon(created.workspace_id)
    _set_terminal_time(sessions, created.workspace_id, now - TERMINAL_BYTE_RETENTION)
    assert retention.sweep(force=True).cleanups_queued == 1
    lease = cleanup.acquire(submitted.pop())
    assert lease is not None
    cleanup.run(lease)
    final = workspaces.get(created.workspace_id)
    assert final.bytes_state.value == "cleaned"
    with sessions() as session:
        binding = session.get(McpAgentWorkspaceBindingRow, str(created.workspace_id))
        assert binding is not None
        assert binding.client_id == str(client_id)
