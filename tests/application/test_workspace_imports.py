"""Reviewed batches preserve private edits, exact bytes and recovery evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.mcp_administration import (
    ADDON_DEVELOPMENT_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
)
from kodi_lab.application.workspace_imports import (
    ImportApplyInput,
    ImportInput,
    WorkspaceImportService,
)
from kodi_lab.domain import AgentWorkspaceState
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import McpTargetToolService
from kodi_lab.storage.models import WorkspaceImportOperationRow
from kodi_lab.workspace.storage import AgentWorkspaceReconciliationRequired
from tests.application.test_workspace_source_capture import (
    SOURCE_ID,
    WORKSPACE_ID,
    _fixture,
    _record,
)


def _environment(tmp_path: Path) -> tuple[Any, Any, Path, Any, Any, Any, UUID]:
    workspaces, _captures, source, sessions = _fixture(tmp_path)
    administration = McpAdministrationService(sessions)
    preset = administration.create_custom_preset(
        display_name="Import reviewed source",
        template_preset_id=ADDON_DEVELOPMENT_PRESET_ID,
        permissions=frozenset({"workspace.edit", "development_source.inspect"}),
    )
    client = administration.create_client(display_name="Importer", preset_id=preset.id)
    administration.grant_development_source(client.id, SOURCE_ID)
    administration.bind_workspace(client.id, WORKSPACE_ID)
    principal = McpAuthenticationService(sessions).authenticate(
        administration.issue_credential(client.id).token
    )
    assert principal is not None
    tools = McpTargetToolService(
        sessions,
        McpAuthorizationService(sessions, build_mcp_tool_registry()),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        workspaces=workspaces,
    )
    return workspaces, tools, source, sessions, administration, principal, client.id


def _request(tools: Any, principal: Any, generation: int = 0) -> ImportApplyInput:
    preview = tools.workspace_import_preview(
        principal,
        ImportInput(workspace_id=WORKSPACE_ID, expected_generation=generation),
    )
    assert preview.eligible
    return ImportApplyInput(
        workspace_id=WORKSPACE_ID,
        expected_generation=generation,
        request_id=uuid4(),
        source_digest=preview.source_digest,
        preview_digest=preview.preview_digest,
    )


def test_import_publishes_one_generation_preserves_baseline_and_private_files(
    tmp_path: Path,
) -> None:
    workspaces, tools, source, sessions, administration, principal, client_id = (
        _environment(tmp_path)
    )
    baseline = _record(sessions).baseline_manifest_digest
    workspaces.create_text_file(
        WORKSPACE_ID,
        expected_generation=0,
        relative_path="private.py",
        content="private\n",
    )
    (source / "default.py").write_text("print('reviewed')\n")
    (source / "helper.py").write_text("helper = True\n")
    request = _request(tools, principal, 1)
    assert workspaces.read_file(WORKSPACE_ID, "default.py").content == "print('v1')\n"
    result = tools.workspace_import(principal, request)
    assert result.state == "succeeded" and result.generation == 2
    assert len(result.files) == 2
    assert (
        workspaces.read_file(WORKSPACE_ID, "default.py").content
        == "print('reviewed')\n"
    )
    assert workspaces.read_file(WORKSPACE_ID, "helper.py").content == "helper = True\n"
    assert workspaces.read_file(WORKSPACE_ID, "private.py").content == "private\n"
    assert _record(sessions).baseline_manifest_digest == baseline
    assert tools.workspace_import(principal, request) == result
    assert workspaces.get(WORKSPACE_ID).generation == 2
    with sessions() as session:
        assert len(session.scalars(select(WorkspaceImportOperationRow)).all()) == 1
    administration.remove_development_source_grant(client_id, SOURCE_ID)
    with pytest.raises(
        McpAuthorizationError, match="workspace_not_found_or_not_authorized"
    ):
        tools.workspace_import(principal, request)


@pytest.mark.parametrize("change", ["source", "workspace"])
def test_changed_preview_is_rejected_without_partial_application(
    tmp_path: Path, change: str
) -> None:
    workspaces, tools, source, sessions, _administration, principal, _client = (
        _environment(tmp_path)
    )
    (source / "default.py").write_text("new\n")
    (source / "helper.py").write_text("helper\n")
    request = _request(tools, principal)
    if change == "source":
        (source / "helper.py").write_text("unreviewed\n")
    else:
        workspaces.create_text_file(
            WORKSPACE_ID,
            expected_generation=0,
            relative_path="private.py",
            content="private",
        )
    with pytest.raises(McpAuthorizationError, match="workspace_import_rejected"):
        tools.workspace_import(principal, request)
    assert workspaces.read_file(WORKSPACE_ID, "default.py").content == "print('v1')\n"
    assert not (
        tmp_path / "data/agent-workspaces" / str(WORKSPACE_ID) / "tree/helper.py"
    ).exists()
    with sessions() as session:
        assert session.scalars(select(WorkspaceImportOperationRow)).all() == []


@pytest.mark.parametrize(
    "change,reason",
    [
        ("delete", "deletion_unsupported"),
        ("binary", "not_utf8_text"),
        ("identity", "identity_change"),
        ("overlap", "private_edit_overlap"),
        ("parent", "parent_missing"),
        ("large", "file_too_large"),
        ("mode", "mode_change_unsupported"),
        ("type", "type_change_unsupported"),
    ],
)
def test_preview_explains_policy_rejections(
    tmp_path: Path, change: str, reason: str
) -> None:
    workspaces, tools, source, _sessions, _administration, principal, _client = (
        _environment(tmp_path)
    )
    generation = 0
    if change == "delete":
        (source / "default.py").unlink()
    elif change == "binary":
        (source / "default.py").write_bytes(b"\x00\xff")
    elif change == "identity":
        descriptor = source / "addon.xml"
        descriptor.write_text(
            descriptor.read_text().replace("plugin.video.example", "plugin.video.other")
        )
    elif change == "overlap":
        workspaces.replace_text_file(
            WORKSPACE_ID,
            expected_generation=0,
            relative_path="default.py",
            content="private\n",
        )
        (source / "default.py").write_text("incoming\n")
        generation = 1
    elif change == "parent":
        (source / "new").mkdir()
        (source / "new/helper.py").write_text("helper\n")
    elif change == "large":
        (source / "default.py").write_text("a" * (48 * 1024 + 1))
    elif change == "mode":
        (source / "default.py").chmod(0o755)
    else:
        (source / "default.py").unlink()
        (source / "default.py").mkdir()
    preview = tools.workspace_import_preview(
        principal,
        ImportInput(workspace_id=WORKSPACE_ID, expected_generation=generation),
    )
    assert not preview.eligible
    assert reason in {item.rejection for item in preview.files}
    assert workspaces.get(WORKSPACE_ID).generation == generation


def test_failed_publication_rolls_back_whole_batch_and_replays_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces, tools, source, _sessions, _administration, principal, _client = (
        _environment(tmp_path)
    )
    (source / "default.py").write_text("new\n")
    (source / "helper.py").write_text("helper\n")
    request = _request(tools, principal)
    service = tools._workspace_imports
    assert service is not None
    with monkeypatch.context() as patch:

        def fail(*_args: Any) -> None:
            raise ValueError("fixture publication failure")

        patch.setattr(service, "_publish", fail)
        with pytest.raises(McpAuthorizationError, match="workspace_import_rejected"):
            tools.workspace_import(principal, request)
    assert workspaces.get(WORKSPACE_ID).generation == 0
    assert workspaces.read_file(WORKSPACE_ID, "default.py").content == "print('v1')\n"
    assert not (
        tmp_path / "data/agent-workspaces" / str(WORKSPACE_ID) / "tree/helper.py"
    ).exists()
    assert tools.workspace_import(principal, request).state == "failed"


def test_uncertain_publication_recovers_exact_tree_without_reimport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces, tools, source, sessions, _administration, principal, _client = (
        _environment(tmp_path)
    )
    (source / "default.py").write_text("reviewed\n")
    request = _request(tools, principal)
    service: WorkspaceImportService = tools._workspace_imports
    with monkeypatch.context() as patch:

        def uncertain(*_args: Any) -> None:
            raise AgentWorkspaceReconciliationRequired(
                "workspace_import_reconciliation_required", "fixture lost publication"
            )

        patch.setattr(service, "_publish", uncertain)
        with pytest.raises(McpAuthorizationError, match="workspace_import_rejected"):
            tools.workspace_import(principal, request)
    assert _record(sessions).state is AgentWorkspaceState.RECONCILIATION_REQUIRED
    # Current source may change after dispatch; recovery uses the exact retained
    # tree and durable receipt, never imports the new unreviewed bytes.
    (source / "default.py").write_text("unreviewed\n")
    recovered = tools.workspace_import(principal, request)
    assert recovered.state == "succeeded" and recovered.generation == 1
    assert workspaces.read_file(WORKSPACE_ID, "default.py").content == "reviewed\n"
    assert tools.workspace_import(principal, request) == recovered


def test_request_identity_cannot_change_preview(tmp_path: Path) -> None:
    _workspaces, tools, source, _sessions, _administration, principal, _client = (
        _environment(tmp_path)
    )
    (source / "default.py").write_text("new\n")
    request = _request(tools, principal)
    tools.workspace_import(principal, request)
    changed = request.model_copy(update={"preview_digest": "0" * 64})
    with pytest.raises(McpAuthorizationError, match="workspace_import_rejected"):
        tools.workspace_import(principal, changed)


def test_symlink_source_is_rejected_before_receipt_allocation(tmp_path: Path) -> None:
    workspaces, tools, source, sessions, _administration, principal, _client = (
        _environment(tmp_path)
    )
    (source / "default.py").unlink()
    (source / "default.py").symlink_to(tmp_path / "outside.py")
    with pytest.raises(McpAuthorizationError, match="workspace_import_rejected"):
        tools.workspace_import_preview(
            principal, ImportInput(workspace_id=WORKSPACE_ID, expected_generation=0)
        )
    assert workspaces.get(WORKSPACE_ID).generation == 0
    with sessions() as session:
        assert session.scalars(select(WorkspaceImportOperationRow)).all() == []


def test_later_import_tracks_source_provenance_and_preserves_new_private_edits(
    tmp_path: Path,
) -> None:
    workspaces, tools, source, _sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    (source / "default.py").write_text("first imported\n")
    first = tools.workspace_import(principal, _request(tools, principal))
    (source / "default.py").write_text("second imported\n")
    second = tools.workspace_import(
        principal, _request(tools, principal, first.generation)
    )
    assert second.generation == 2
    workspaces.replace_text_file(
        WORKSPACE_ID,
        expected_generation=2,
        relative_path="default.py",
        content="private edit\n",
    )
    (source / "helper.py").write_text("helper\n")
    third = tools.workspace_import(principal, _request(tools, principal, 3))
    assert third.generation == 4
    assert workspaces.read_file(WORKSPACE_ID, "default.py").content == "private edit\n"
    (source / "default.py").write_text("conflicting source edit\n")
    preview = tools.workspace_import_preview(
        principal, ImportInput(workspace_id=WORKSPACE_ID, expected_generation=4)
    )
    assert not preview.eligible
    assert {item.rejection for item in preview.files} == {"private_edit_overlap"}


def test_lost_pre_switch_publication_replays_failed_without_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kodi_lab.workspace import batch_import

    workspaces, tools, source, sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    (source / "default.py").write_text("reviewed\n")
    request = _request(tools, principal)
    with monkeypatch.context() as patch:

        def unavailable(*_args: Any) -> None:
            raise AgentWorkspaceReconciliationRequired(
                "workspace_import_reconciliation_required",
                "fixture stopped before switch",
            )

        patch.setattr(batch_import, "_exchange", unavailable)
        with pytest.raises(McpAuthorizationError, match="workspace_import_rejected"):
            tools.workspace_import(principal, request)
    assert _record(sessions).state is AgentWorkspaceState.RECONCILIATION_REQUIRED
    assert tools.workspace_import(principal, request).state == "failed"
    assert workspaces.get(WORKSPACE_ID).generation == 0
    assert workspaces.read_file(WORKSPACE_ID, "default.py").content == "print('v1')\n"
