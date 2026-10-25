"""Reviewed removals and private resolutions retain exact, replayable lineage."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any, Literal
from uuid import uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.agent_workspaces import AgentWorkspaceConflictError
from kodi_lab.application.workspace_imports import (
    ImportApplyInput,
    ImportInput,
    ImportResolution,
    WorkspaceImportService,
)
from kodi_lab.application.workspace_source_captures import WorkspaceSourceCaptureService
from kodi_lab.development.deployment import capture_development_tree
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.storage.models import WorkspaceImportOperationRow
from kodi_lab.workspace.storage import AgentWorkspaceReconciliationRequired
from tests.application.test_workspace_imports import _environment, _request
from tests.application.test_workspace_source_capture import WORKSPACE_ID, _record


def reviewed(tools: Any, principal: Any, generation: int = 0, **options: Any) -> Any:
    return tools.workspace_import_preview(
        principal,
        ImportInput(
            workspace_id=WORKSPACE_ID,
            expected_generation=generation,
            mode="reviewed_tree",
            **options,
        ),
    )


def request(preview: Any, **options: Any) -> ImportApplyInput:
    return ImportApplyInput(
        workspace_id=WORKSPACE_ID,
        expected_generation=preview.generation,
        request_id=uuid4(),
        source_digest=preview.source_digest,
        preview_digest=preview.preview_digest,
        mode="reviewed_tree",
        **options,
    )


def tree(tmp_path: Path) -> Path:
    return tmp_path / "data/agent-workspaces" / str(WORKSPACE_ID) / "tree"


def test_exact_80_to_78_shape_preserves_original_baseline_and_old_tree(
    tmp_path: Path,
) -> None:
    _workspaces, tools, source, sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    for number in range(78):
        (source / f"module{number}.py").write_text(f"value = {number}\n")
    capture = WorkspaceSourceCaptureService(
        sessions, data_root=tmp_path / "data", development_root=tmp_path / "sources"
    )
    capture.capture(WORKSPACE_ID, expected_generation=0)
    before = _record(sessions)
    for number in range(7):
        (source / f"module{number}.py").write_text(f"reviewed = {number}\n")
    for number in range(7, 11):
        (source / f"module{number}.py").unlink()
    for number in range(2):
        (source / f"added{number}.py").write_text("added = True\n")
    preview = reviewed(tools, principal, 1)
    assert preview.eligible and preview.matches_source
    assert len(preview.files) == 13
    assert [
        sum(item.operation == op for item in preview.files)
        for op in ("modified", "added", "deleted")
    ] == [7, 2, 4]
    assert all(
        item.preimage_sha256 for item in preview.files if item.operation == "deleted"
    )
    applied = tools.workspace_import(principal, request(preview))
    assert applied.generation == 2 and applied.matches_source
    actual = capture_development_tree(tree(tmp_path).parent, "tree")
    assert len(actual.entries) == 78
    assert actual.digest == preview.source_digest == applied.tree_digest
    assert _record(sessions).baseline_manifest_digest == before.baseline_manifest_digest
    retained = tree(tmp_path).parent / f".import-{applied.operation_id}"
    assert (
        capture_development_tree(retained.parent, retained.name).digest
        == before.current_tree_digest
    )
    with sessions() as session:
        row = session.scalar(select(WorkspaceImportOperationRow))
        assert row is not None
        lineage = json.loads(row.files_json)
        assert lineage["version"] == 2 and lineage["input"]["mode"] == "reviewed_tree"


@pytest.mark.parametrize("choice", ["keep_private", "take_source"])
@pytest.mark.parametrize("change", ["replace", "delete", "private_only"])
def test_private_conflict_requires_review_and_reports_exactness(
    tmp_path: Path, choice: Literal["keep_private", "take_source"], change: str
) -> None:
    workspaces, tools, source, _sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    path = "private.py" if change == "private_only" else "default.py"
    if change == "private_only":
        workspaces.create_text_file(
            WORKSPACE_ID, expected_generation=0, relative_path=path, content="private\n"
        )
    else:
        workspaces.replace_text_file(
            WORKSPACE_ID, expected_generation=0, relative_path=path, content="private\n"
        )
        if change == "replace":
            (source / path).write_text("incoming\n")
        else:
            (source / path).unlink()
    unresolved = reviewed(tools, principal, 1)
    assert not unresolved.eligible and not unresolved.matches_source
    assert unresolved.files[0].rejection == "private_edit_overlap"
    resolutions = [ImportResolution(path=path, choice=choice)]
    preview = reviewed(tools, principal, 1, resolutions=resolutions)
    assert preview.eligible and preview.matches_source is (choice == "take_source")
    result = tools.workspace_import(
        principal, request(preview, resolutions=resolutions)
    )
    assert result.matches_source is (choice == "take_source")
    if choice == "keep_private":
        assert workspaces.read_file(WORKSPACE_ID, path).content == "private\n"
        assert result.files[0].operation == "retained_private"
        # A kept edit never acquires source provenance through a receipt.
        if change == "replace":
            (source / path).write_text("third\n")
        again = reviewed(tools, principal, 2)
        assert not again.eligible and again.files[0].rejection == "private_edit_overlap"
    else:
        assert (
            capture_development_tree(tree(tmp_path).parent, "tree").digest
            == preview.source_digest
        )
    assert (
        tools.workspace_import(
            principal,
            request(preview, resolutions=resolutions).model_copy(
                update={"request_id": result.request_id}
            ),
        )
        == result
    )


def test_deleted_imported_file_uses_tombstone_then_can_be_readded(
    tmp_path: Path,
) -> None:
    _workspaces, tools, source, sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    (source / "helper.py").write_text("first\n")
    first_request = _request(tools, principal)
    first = tools.workspace_import(principal, first_request)
    # Original list receipts remain readable alongside new envelopes.
    with sessions.begin() as session:
        prior = session.get(WorkspaceImportOperationRow, str(first.operation_id))
        assert prior is not None
        prior.files_json = json.dumps(json.loads(prior.files_json)["files"])
    assert tools.workspace_import(principal, first_request) == first
    (source / "helper.py").unlink()
    removed = tools.workspace_import(principal, request(reviewed(tools, principal, 1)))
    assert removed.files[0].operation == "deleted" and removed.files[0].sha256 is None
    (source / "helper.py").write_text("second\n")
    preview = reviewed(tools, principal, 2)
    assert preview.eligible and preview.files[0].operation == "added"
    final = tools.workspace_import(principal, request(preview))
    assert final.generation == 3 and final.matches_source


@pytest.mark.parametrize("change", ["source", "generation", "preimage", "options"])
def test_stale_review_never_partly_deletes(tmp_path: Path, change: str) -> None:
    workspaces, tools, source, sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    (source / "default.py").unlink()
    preview = reviewed(tools, principal)
    operation = request(preview)
    if change == "source":
        (source / "helper.py").write_text("unreviewed\n")
    elif change == "generation":
        workspaces.create_text_file(
            WORKSPACE_ID,
            expected_generation=0,
            relative_path="private.py",
            content="new\n",
        )
    elif change == "preimage":
        (tree(tmp_path) / "default.py").write_text("unexpected\n")
    else:
        operation = operation.model_copy(update={"mode": "changes"})
    with pytest.raises(McpAuthorizationError, match="workspace_import_rejected"):
        tools.workspace_import(principal, operation)
    assert (tree(tmp_path) / "default.py").exists()
    with sessions() as session:
        assert session.scalars(select(WorkspaceImportOperationRow)).all() == []


@pytest.mark.parametrize("uncertain", [False, True])
def test_deletion_publication_failure_rolls_back_or_recovers_retained_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uncertain: bool
) -> None:
    workspaces, tools, source, _sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    (source / "default.py").unlink()
    operation = request(reviewed(tools, principal))
    with monkeypatch.context() as patch:

        def fail(*_args: Any) -> None:
            if uncertain:
                raise AgentWorkspaceReconciliationRequired(
                    "workspace_import_reconciliation_required", "lost commit"
                )
            raise ValueError("failed commit")

        patch.setattr(tools._workspace_imports, "_publish", fail)
        with pytest.raises(McpAuthorizationError, match="workspace_import_rejected"):
            tools.workspace_import(principal, operation)
    # A new service represents process restart; replay uses durable bytes/journal.
    tools._workspace_imports = WorkspaceImportService(workspaces)
    replay = tools.workspace_import(principal, operation)
    assert replay.state == ("succeeded" if uncertain else "failed")
    assert replay.matches_source is uncertain
    assert (tree(tmp_path) / "default.py").exists() is (not uncertain)
    assert workspaces.get(WORKSPACE_ID).generation == int(uncertain)
    with pytest.raises(McpAuthorizationError, match="workspace_import_rejected"):
        tools.workspace_import(
            principal, operation.model_copy(update={"mode": "changes"})
        )


@pytest.mark.parametrize(
    "defect", ["directory", "new_directory", "mode", "identity", "symlink"]
)
def test_reviewed_tree_keeps_unsupported_type_mode_identity_and_link_guards(
    tmp_path: Path, defect: str
) -> None:
    workspaces, tools, source, _sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    if defect == "directory":
        (source / "default.py").unlink()
        (source / "default.py").mkdir()
    elif defect == "new_directory":
        (source / "new").mkdir()
        (source / "new/child.py").write_text("new\n")
    elif defect == "mode":
        (source / "default.py").chmod(0o755)
    elif defect == "identity":
        (source / "addon.xml").unlink()
    else:
        (source / "default.py").unlink()
        (source / "default.py").symlink_to(tmp_path / "outside")
    if defect == "symlink":
        with pytest.raises(McpAuthorizationError, match="workspace_import_rejected"):
            reviewed(tools, principal)
    else:
        preview = reviewed(tools, principal)
        assert not preview.eligible and not preview.matches_source
    assert workspaces.get(WORKSPACE_ID).generation == 0


def test_concurrent_import_and_edit_publish_only_one_generation(tmp_path: Path) -> None:
    workspaces, tools, source, _sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    (source / "default.py").unlink()
    operation = request(reviewed(tools, principal))
    entered, proceed = Event(), Event()
    service = tools._workspace_imports
    publish = service._publish

    def waiting(*args: Any) -> None:
        entered.set()
        assert proceed.wait(10)
        publish(*args)

    service._publish = waiting
    with ThreadPoolExecutor(max_workers=2) as pool:
        importing = pool.submit(tools.workspace_import, principal, operation)
        assert entered.wait(10)
        editing = pool.submit(
            workspaces.create_text_file,
            WORKSPACE_ID,
            expected_generation=0,
            relative_path="racing.py",
            content="race\n",
        )
        proceed.set()
        assert importing.result(timeout=10).generation == 1
        with pytest.raises(AgentWorkspaceConflictError):
            editing.result(timeout=10)
    assert not (tree(tmp_path) / "racing.py").exists()


def test_unreviewed_resolution_and_unsafe_request_paths_fail_before_changes(
    tmp_path: Path,
) -> None:
    from pydantic import ValidationError

    _workspaces, tools, source, _sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    (source / "default.py").unlink()
    with pytest.raises(McpAuthorizationError, match="workspace_import_rejected"):
        reviewed(
            tools,
            principal,
            resolutions=[ImportResolution(path="default.py", choice="take_source")],
        )
    for path in ("../outside", "/absolute", ".git/config"):
        with pytest.raises(ValidationError):
            ImportInput(
                workspace_id=WORKSPACE_ID,
                expected_generation=0,
                mode="reviewed_tree",
                resolutions=(ImportResolution(path=path, choice="take_source"),),
            )
    repeated = ImportResolution(path="default.py", choice="take_source")
    with pytest.raises(ValidationError):
        ImportInput(
            workspace_id=WORKSPACE_ID,
            expected_generation=0,
            mode="reviewed_tree",
            resolutions=(repeated, repeated),
        )
    assert (tree(tmp_path) / "default.py").exists()


def test_interrupted_deletion_before_exchange_retains_both_trees_and_fails_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.workspace import batch_import

    workspaces, tools, source, _sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    (source / "default.py").unlink()
    operation = request(reviewed(tools, principal))
    with monkeypatch.context() as patch:

        def interrupted(*_args: Any) -> None:
            raise AgentWorkspaceReconciliationRequired(
                "workspace_import_reconciliation_required", "interrupted exchange"
            )

        patch.setattr(batch_import, "_exchange", interrupted)
        with pytest.raises(McpAuthorizationError, match="workspace_import_rejected"):
            tools.workspace_import(principal, operation)
    tools._workspace_imports = WorkspaceImportService(workspaces)
    assert tools.workspace_import(principal, operation).state == "failed"
    assert (tree(tmp_path) / "default.py").exists()
    assert len(list(tree(tmp_path).parent.glob(".import-*"))) == 1
    assert workspaces.get(WORKSPACE_ID).generation == 0
