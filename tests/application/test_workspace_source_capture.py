"""Whole-source captures retain a clean workspace's historical evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.agent_workspace_cleanup import (
    AgentWorkspaceCleanupError,
    AgentWorkspaceCleanupService,
)
from kodi_lab.application.agent_workspaces import AgentWorkspaceService
from kodi_lab.application.workspace_source_captures import (
    WorkspaceSourceCaptureError,
    WorkspaceSourceCaptureService,
)
from kodi_lab.development.deployment import DevelopmentTreeError
from kodi_lab.domain import AgentWorkspace, DevelopmentSource, DevelopmentSourceKind
from kodi_lab.storage import Base
from kodi_lab.storage.models import (
    AgentWorkspaceCaptureOperationRow,
    McpWorkspaceValidationRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
)
from kodi_lab.workspace.storage import AgentWorkspaceStorageError

SOURCE_ID = UUID("10000000-0000-0000-0000-000000000001")
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")


def _fixture(
    tmp_path: Path,
) -> tuple[
    AgentWorkspaceService,
    WorkspaceSourceCaptureService,
    Path,
    sessionmaker[Session],
]:
    engine = create_engine(f"sqlite:///{tmp_path / 'workspace.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    source_root = tmp_path / "sources"
    source = source_root / "plugin.video.example"
    source.mkdir(parents=True)
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.example" name="Example" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="default.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    (source / "default.py").write_text("print('v1')\n", encoding="utf-8")
    now = datetime(2026, 9, 30, tzinfo=UTC)
    with sessions.begin() as session:
        DevelopmentSourceRepository(session).create(
            DevelopmentSource(
                id=SOURCE_ID,
                logical_name="Example",
                source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
                relative_locator="plugin.video.example",
                addon_id="plugin.video.example",
                observed_name="Example",
                observed_version="1.0.0",
                extension_types=("xbmc.python.pluginsource",),
                registered_at=now,
                validated_at=now,
            )
        )
    ids = iter((WORKSPACE_ID, uuid4(), uuid4()))
    workspace_service = AgentWorkspaceService(
        sessions,
        data_root=tmp_path / "data",
        development_root=source_root,
        clock=lambda: now,
        uuid_factory=lambda: next(ids, uuid4()),
    )
    queued = workspace_service.queue_creation(SOURCE_ID)
    lease = workspace_service.acquire(queued.operation_id)
    assert lease is not None
    workspace_service.run(lease)
    assert (
        workspace_service.creation_status(queued.operation_id).state.value
        == "succeeded"
    )
    capture_service = WorkspaceSourceCaptureService(
        sessions,
        data_root=tmp_path / "data",
        development_root=source_root,
        clock=lambda: now,
    )
    return workspace_service, capture_service, source, sessions


def _record(sessions: sessionmaker[Session]) -> AgentWorkspace:
    with sessions() as session:
        workspace = AgentWorkspaceRepository(session).get(WORKSPACE_ID)
        assert workspace is not None
        return workspace


def _v2(source: Path) -> None:
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.example" name="Example" version="1.0.1">'
        '<extension point="xbmc.python.pluginsource" library="default.py" />'
        "</addon>\n",
        encoding="utf-8",
    )


def test_unchanged_capture_is_noop(tmp_path: Path) -> None:
    workspace, capture, _source, sessions = _fixture(tmp_path)
    before = _record(sessions)
    result = capture.capture(WORKSPACE_ID, expected_generation=0)
    assert not result.changed and result.operation_id is None
    assert workspace.get(WORKSPACE_ID).generation == 0
    assert _record(sessions).current_tree_digest == before.current_tree_digest
    with sessions() as session:
        assert session.scalars(select(AgentWorkspaceCaptureOperationRow)).all() == []


def test_fresh_scan_observes_external_edits_without_mutating_workspace(
    tmp_path: Path,
) -> None:
    workspace, capture, source, sessions = _fixture(tmp_path)
    before = _record(sessions)
    _v2(source)
    assert capture.scan_source(WORKSPACE_ID) != before.current_tree_digest
    assert _record(sessions).generation == 0
    assert workspace.get(WORKSPACE_ID).generation == 0


def test_capture_leaves_prior_validation_identity_unchanged(tmp_path: Path) -> None:
    _workspace, capture, source, sessions = _fixture(tmp_path)
    receipt_id = uuid4()
    with sessions.begin() as session:
        session.add(
            McpWorkspaceValidationRow(
                id=str(receipt_id),
                workspace_id=str(WORKSPACE_ID),
                development_source_id=str(SOURCE_ID),
                generation=0,
                classification="passed",
                addon_id="plugin.video.example",
                addon_version="1.0.0",
                extension_types_json='["xbmc.python.pluginsource"]',
                validated_at="2026-09-30T12:00:00.000000Z",
            )
        )
    _v2(source)
    capture.capture(WORKSPACE_ID, expected_generation=0)
    with sessions() as session:
        old = session.get(McpWorkspaceValidationRow, str(receipt_id))
        assert old is not None and old.generation == 0
        assert old.addon_version == "1.0.0"


@pytest.mark.parametrize("files_changed", [1, 2])
def test_whole_source_capture_is_one_generation_and_retains_history(
    tmp_path: Path, files_changed: int
) -> None:
    workspace, capture, source, sessions = _fixture(tmp_path)
    before = _record(sessions)
    _v2(source)
    if files_changed == 2:
        (source / "default.py").write_text("print('v2')\n", encoding="utf-8")
        (source / "extra.py").write_text("pass\n", encoding="utf-8")
    result = capture.capture(WORKSPACE_ID, expected_generation=0)
    assert result.changed and result.generation == 1 and result.operation_id
    current = _record(sessions)
    assert current.generation == 1
    assert current.baseline_manifest_digest == result.tree_digest
    assert current.creation_baseline_digest == before.baseline_manifest_digest
    assert current.active_capture_id == result.operation_id
    assert current.active_observed_version == "1.0.1"
    root = tmp_path / "data" / "agent-workspaces" / str(WORKSPACE_ID)
    assert (
        root / "history" / str(before.baseline_artifact_id) / "tree" / "addon.xml"
    ).read_bytes() == (source / "addon.xml").read_bytes().replace(b"1.0.1", b"1.0.0")
    assert (root / "tree" / "addon.xml").read_bytes() == (
        source / "addon.xml"
    ).read_bytes()
    assert (root / "baseline" / "tree" / "addon.xml").read_bytes() == (
        source / "addon.xml"
    ).read_bytes()
    assert (
        root
        / "capture-operations"
        / str(result.operation_id)
        / "new-tree"
        / "addon.xml"
    ).is_file()
    with sessions() as session:
        receipt = session.get(
            AgentWorkspaceCaptureOperationRow, str(result.operation_id)
        )
        assert receipt is not None and receipt.state == "succeeded"
        assert receipt.prior_generation == 0 and receipt.resulting_generation == 1
        assert receipt.prior_baseline_digest == before.baseline_manifest_digest
        assert receipt.captured_digest == result.tree_digest
    assert (
        workspace.validate_structure(WORKSPACE_ID, expected_generation=1).addon_version
        == "1.0.1"
    )
    assert not capture.capture(WORKSPACE_ID, expected_generation=1).changed


def test_removed_and_added_files_are_captured_together(tmp_path: Path) -> None:
    workspace, capture, source, _sessions = _fixture(tmp_path)
    _v2(source)
    (source / "default.py").unlink()
    (source / "replacement.py").write_text("pass\n", encoding="utf-8")
    result = capture.capture(WORKSPACE_ID, expected_generation=0)
    assert result.generation == 1
    root = tmp_path / "data" / "agent-workspaces" / str(WORKSPACE_ID)
    assert not (root / "tree" / "default.py").exists()
    assert (root / "tree" / "replacement.py").is_file()
    assert workspace.get(WORKSPACE_ID).generation == 1


def test_second_capture_retains_first_capture_baseline(tmp_path: Path) -> None:
    workspace, capture, source, sessions = _fixture(tmp_path)
    _v2(source)
    first = capture.capture(WORKSPACE_ID, expected_generation=0)
    (source / "default.py").write_text("print('v3')\n", encoding="utf-8")
    second = capture.capture(WORKSPACE_ID, expected_generation=1)
    assert second.generation == 2 and second.operation_id != first.operation_id
    current = _record(sessions)
    assert current.generation == 2
    assert current.active_capture_id == second.operation_id
    root = tmp_path / "data" / "agent-workspaces" / str(WORKSPACE_ID)
    assert (
        root / "history" / str(first.operation_id) / "tree" / "default.py"
    ).read_text(encoding="utf-8") == "print('v1')\n"
    assert (
        b"1.0.1"
        in (
            root / "history" / str(first.operation_id) / "tree" / "addon.xml"
        ).read_bytes()
    )
    assert workspace.get(WORKSPACE_ID).generation == 2


def test_source_change_during_stage_fails_without_switch(tmp_path: Path) -> None:
    workspace, capture, source, sessions = _fixture(tmp_path)
    _v2(source)
    before = _record(sessions)

    def mutate(phase: str) -> None:
        if phase == "staged":
            (source / "default.py").write_text("print('raced')\n", encoding="utf-8")

    with pytest.raises(DevelopmentTreeError):
        capture.capture(WORKSPACE_ID, expected_generation=0, checkpoint=mutate)
    assert workspace.get(WORKSPACE_ID).generation == 0
    assert _record(sessions).current_tree_digest == before.current_tree_digest
    with sessions() as session:
        receipts = session.scalars(select(AgentWorkspaceCaptureOperationRow)).all()
        assert len(receipts) == 1 and receipts[0].state == "failed"


def test_unsafe_source_and_addon_identity_drift_rejected(tmp_path: Path) -> None:
    workspace, capture, source, sessions = _fixture(tmp_path)
    (source / "escape").symlink_to("/etc/passwd")
    with pytest.raises(DevelopmentTreeError):
        capture.capture(WORKSPACE_ID, expected_generation=0)
    (source / "escape").unlink()
    (source / "addon.xml").write_text(
        (source / "addon.xml")
        .read_text(encoding="utf-8")
        .replace("plugin.video.example", "plugin.video.other"),
        encoding="utf-8",
    )
    with pytest.raises(AgentWorkspaceStorageError) as error:
        capture.capture(WORKSPACE_ID, expected_generation=0)
    assert error.value.code == "agent_workspace_capture_identity_changed"
    assert workspace.get(WORKSPACE_ID).generation == 0
    with sessions() as session:
        assert (
            len(session.scalars(select(AgentWorkspaceCaptureOperationRow)).all()) == 1
        )


def test_source_root_symlink_is_rejected(tmp_path: Path) -> None:
    workspace, capture, source, _sessions = _fixture(tmp_path)
    moved = source.with_name("moved")
    source.rename(moved)
    source.symlink_to(moved, target_is_directory=True)
    with pytest.raises(DevelopmentTreeError):
        capture.capture(WORKSPACE_ID, expected_generation=0)
    assert workspace.get(WORKSPACE_ID).generation == 0


def test_private_workspace_edits_are_not_overwritten(tmp_path: Path) -> None:
    workspace, capture, source, _sessions = _fixture(tmp_path)
    workspace.replace_text_file(
        WORKSPACE_ID,
        expected_generation=0,
        relative_path="default.py",
        content="print('private')\n",
    )
    _v2(source)
    with pytest.raises(WorkspaceSourceCaptureError) as error:
        capture.capture(WORKSPACE_ID, expected_generation=1)
    assert error.value.code == "workspace_private_edits_present"
    assert workspace.get(WORKSPACE_ID).generation == 1


def test_capture_history_blocks_existing_explicit_byte_cleanup(tmp_path: Path) -> None:
    workspace, capture, source, sessions = _fixture(tmp_path)
    _v2(source)
    capture.capture(WORKSPACE_ID, expected_generation=0)
    workspace.abandon(WORKSPACE_ID)
    cleanup = AgentWorkspaceCleanupService(sessions, data_root=tmp_path / "data")
    with pytest.raises(AgentWorkspaceCleanupError) as error:
        cleanup.queue(WORKSPACE_ID)
    assert error.value.code == "workspace_capture_history_retained"


@pytest.mark.parametrize(
    "interrupt_at",
    ["pending", "baseline_switched", "tree_switched", "baseline_archived"],
)
def test_interrupted_capture_recovery_is_deterministic(
    tmp_path: Path, interrupt_at: str
) -> None:
    workspace, capture, source, sessions = _fixture(tmp_path)
    _v2(source)

    def interrupt(phase: str) -> None:
        if phase == interrupt_at:
            raise RuntimeError("simulated termination")

    with pytest.raises(RuntimeError, match="simulated termination"):
        capture.capture(WORKSPACE_ID, expected_generation=0, checkpoint=interrupt)
    with sessions() as session:
        receipt = session.scalars(select(AgentWorkspaceCaptureOperationRow)).one()
        operation_id = UUID(receipt.id)
        assert workspace.get(WORKSPACE_ID).state.value == "capturing"
    settled = capture.recover(operation_id)
    if interrupt_at == "pending":
        assert settled is None
        assert workspace.get(WORKSPACE_ID).generation == 0
    else:
        assert settled is not None and settled.generation == 1
        assert workspace.get(WORKSPACE_ID).generation == 1
        assert capture.recover(operation_id) == settled
