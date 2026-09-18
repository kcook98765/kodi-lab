from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kodi_lab.application.agent_workspaces import (
    AgentWorkspaceNotFoundError,
    AgentWorkspaceService,
)
from kodi_lab.application.reconciliation import StaleOperationReconciliationDispatcher
from kodi_lab.development.deployment import (
    DevelopmentTreeManifest,
    capture_development_tree,
    compare_captures,
)
from kodi_lab.domain import DevelopmentSource, DevelopmentSourceKind
from kodi_lab.storage import Base
from kodi_lab.storage.repositories import DevelopmentSourceRepository
from kodi_lab.workspace import AgentWorkspaceStorageError
from kodi_lab.workspace import storage as workspace_storage

SOURCE_ID = UUID("10000000-0000-0000-0000-000000000001")
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
BASELINE_ID = UUID("30000000-0000-0000-0000-000000000001")
OPERATION_ID = UUID("40000000-0000-0000-0000-000000000001")


def _service(tmp_path: Path) -> tuple[AgentWorkspaceService, Path]:
    engine = create_engine(f"sqlite:///{tmp_path / 'workspace.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    development_root = tmp_path / "sources"
    source = development_root / "plugin.video.example"
    (source / "resources").mkdir(parents=True)
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.example" name="Example" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    (source / "resources" / "main.py").write_text("print('safe')\n", encoding="utf-8")
    timestamp = datetime(2026, 9, 18, tzinfo=UTC)
    with factory.begin() as session:
        DevelopmentSourceRepository(session).create(
            DevelopmentSource(
                id=SOURCE_ID,
                logical_name="Example source",
                source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
                relative_locator="plugin.video.example",
                addon_id="plugin.video.example",
                observed_name="Example",
                observed_version="1.0.0",
                extension_types=("xbmc.python.pluginsource",),
                registered_at=timestamp,
                validated_at=timestamp,
            )
        )
    ids = iter(
        (
            WORKSPACE_ID,
            BASELINE_ID,
            OPERATION_ID,
            UUID("20000000-0000-0000-0000-000000000002"),
            UUID("30000000-0000-0000-0000-000000000002"),
            UUID("40000000-0000-0000-0000-000000000002"),
        )
    )
    return (
        AgentWorkspaceService(
            factory,
            data_root=tmp_path / "data",
            development_root=development_root,
            clock=lambda: timestamp,
            uuid_factory=lambda: next(ids),
        ),
        source,
    )


def _create(service: AgentWorkspaceService) -> UUID:
    queued = service.queue_creation(SOURCE_ID)
    lease = service.acquire(queued.operation_id)
    assert lease is not None
    service.run(lease)
    assert service.creation_status(queued.operation_id).state.value == "succeeded"
    return queued.workspace_id


def test_create_workspace_publishes_equal_private_baseline_and_tree(
    tmp_path: Path,
) -> None:
    service, source = _service(tmp_path)
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("secret", encoding="utf-8")

    workspace_id = _create(service)

    workspace = service.get(workspace_id)
    assert workspace.state.value == "ready"
    assert workspace.generation == 0
    root = tmp_path / "data" / "agent-workspaces" / str(workspace_id)
    baseline = root / "baseline" / "tree"
    tree = root / "tree"
    assert (baseline / "resources" / "main.py").read_bytes() == (
        tree / "resources" / "main.py"
    ).read_bytes()
    assert not (baseline / ".git").exists()
    assert not (tree / ".git").exists()
    assert (root / "baseline" / "manifest.json").is_file()
    assert os.stat(tree / "addon.xml").st_uid == os.geteuid()


def test_read_inspect_is_text_only_bounded_and_no_follow(tmp_path: Path) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    entries = service.inspect_directory(workspace_id, "")
    assert [(entry.name, entry.entry_type) for entry in entries] == [
        ("addon.xml", "file"),
        ("resources", "directory"),
    ]
    assert (
        service.read_file(workspace_id, "resources/main.py").content
        == "print('safe')\n"
    )

    tree = tmp_path / "data" / "agent-workspaces" / str(workspace_id) / "tree"
    (tree / "binary").write_bytes(b"\xff")
    with pytest.raises(AgentWorkspaceStorageError) as binary:
        service.read_file(workspace_id, "binary")
    assert binary.value.code == "agent_workspace_file_not_text"
    (tree / "escape").symlink_to("/etc/passwd")
    with pytest.raises(AgentWorkspaceStorageError):
        service.read_file(workspace_id, "escape")
    with pytest.raises(AgentWorkspaceStorageError):
        service.read_file(workspace_id, "../etc/passwd")
    with pytest.raises(AgentWorkspaceStorageError):
        service.inspect_directory(workspace_id, "/")


def test_multiple_workspaces_are_independent(tmp_path: Path) -> None:
    service, _source = _service(tmp_path)
    first = _create(service)

    second = _create(service)
    first_tree = tmp_path / "data" / "agent-workspaces" / str(first) / "tree"
    second_tree = tmp_path / "data" / "agent-workspaces" / str(second) / "tree"
    (first_tree / "addon.xml").write_text("changed", encoding="utf-8")
    assert 'id="plugin.video.example"' in (second_tree / "addon.xml").read_text(
        encoding="utf-8"
    )
    assert first != second


def test_unknown_workspace_is_safe(tmp_path: Path) -> None:
    service, _source = _service(tmp_path)
    with pytest.raises(AgentWorkspaceNotFoundError):
        service.get(UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"))


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_unsafe_source_entry_fails_without_published_workspace(
    tmp_path: Path, kind: str
) -> None:
    service, source = _service(tmp_path)
    unsafe = source / "unsafe"
    if kind == "symlink":
        unsafe.symlink_to("addon.xml")
    elif kind == "hardlink":
        os.link(source / "addon.xml", unsafe)
    else:
        os.mkfifo(unsafe)

    queued = service.queue_creation(SOURCE_ID)
    lease = service.acquire(queued.operation_id)
    assert lease is not None
    service.run(lease)

    assert service.creation_status(queued.operation_id).state.value == "failed"
    assert service.get(queued.workspace_id).state.value == "abandoned"
    assert not (
        tmp_path / "data" / "agent-workspaces" / str(queued.workspace_id)
    ).exists()


def test_post_baseline_failure_is_retained_for_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _source = _service(tmp_path)

    def fail_copy(_source: Path, _destination: Path) -> None:
        raise OSError("injected after publication")

    monkeypatch.setattr(workspace_storage, "_copy_normalized_tree", fail_copy)
    queued = service.queue_creation(SOURCE_ID)
    lease = service.acquire(queued.operation_id)
    assert lease is not None
    service.run(lease)

    operation = service.creation_status(queued.operation_id)
    assert operation.state.value == "reconciliation_required"
    assert operation.failure_code == "agent_workspace_creation_failed"
    assert service.get(queued.workspace_id).state.value == "reconciliation_required"
    baseline = (
        tmp_path
        / "data"
        / "agent-workspaces"
        / str(queued.workspace_id)
        / "baseline"
        / "tree"
    )
    assert baseline.is_dir()


def test_read_rejects_type_mismatch_special_entry_and_fixed_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    tree = tmp_path / "data" / "agent-workspaces" / str(workspace_id) / "tree"
    with pytest.raises(AgentWorkspaceStorageError):
        service.read_file(workspace_id, "resources")
    with pytest.raises(AgentWorkspaceStorageError):
        service.inspect_directory(workspace_id, "addon.xml")
    os.mkfifo(tree / "late-fifo")
    with pytest.raises(AgentWorkspaceStorageError) as special:
        service.inspect_directory(workspace_id, "")
    assert special.value.code == "agent_workspace_entry_unsafe"
    (tree / "late-fifo").unlink()
    (tree / "large.txt").write_text("12345", encoding="utf-8")
    monkeypatch.setattr(workspace_storage, "MAX_TEXT_READ_BYTES", 4)
    with pytest.raises(AgentWorkspaceStorageError) as oversized:
        service.read_file(workspace_id, "large.txt")
    assert oversized.value.code == "agent_workspace_file_too_large"


def test_expired_creation_is_failed_and_releases_global_lease(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'stale.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    development_root = tmp_path / "sources"
    source_root = development_root / "plugin.video.example"
    source_root.mkdir(parents=True)
    now = [datetime(2026, 9, 18, tzinfo=UTC)]
    with factory.begin() as session:
        DevelopmentSourceRepository(session).create(
            DevelopmentSource(
                id=SOURCE_ID,
                logical_name="Example source",
                source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
                relative_locator="plugin.video.example",
                addon_id="plugin.video.example",
                observed_name="Example",
                observed_version="1.0.0",
                extension_types=("xbmc.python.pluginsource",),
                registered_at=now[0],
                validated_at=now[0],
            )
        )
    ids = iter((WORKSPACE_ID, BASELINE_ID, OPERATION_ID))
    service = AgentWorkspaceService(
        factory,
        data_root=tmp_path / "data",
        development_root=development_root,
        clock=lambda: now[0],
        uuid_factory=lambda: next(ids),
    )
    queued = service.queue_creation(SOURCE_ID)
    assert service.acquire(queued.operation_id) is not None
    now[0] += timedelta(minutes=31)

    result = StaleOperationReconciliationDispatcher(
        factory, clock=lambda: now[0], data_root=tmp_path / "data"
    ).sweep_global()

    assert result.terminalized_operations == [queued.operation_id]
    assert service.creation_status(queued.operation_id).state.value == "failed"
    assert service.get(queued.workspace_id).state.value == "abandoned"


def test_captured_addon_identity_must_match_registration(tmp_path: Path) -> None:
    service, source = _service(tmp_path)
    descriptor = (source / "addon.xml").read_text(encoding="utf-8")
    (source / "addon.xml").write_text(
        descriptor.replace("plugin.video.example", "plugin.video.different"),
        encoding="utf-8",
    )

    queued = service.queue_creation(SOURCE_ID)
    lease = service.acquire(queued.operation_id)
    assert lease is not None
    service.run(lease)

    operation = service.creation_status(queued.operation_id)
    assert operation.state.value == "failed"
    assert operation.failure_code == "agent_workspace_source_identity_changed"


def test_second_source_pass_detects_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, source = _service(tmp_path)
    calls = 0

    def capture_with_drift(
        development_root: Path, locator: str, *, stage_root: Path | None = None
    ) -> DevelopmentTreeManifest:
        nonlocal calls
        result = capture_development_tree(
            development_root, locator, stage_root=stage_root
        )
        calls += 1
        if calls == 1:
            (source / "resources" / "main.py").write_text(
                "print('drift')\n", encoding="utf-8"
            )
        return result

    monkeypatch.setattr(
        "kodi_lab.workspace.storage.capture_development_tree", capture_with_drift
    )
    queued = service.queue_creation(SOURCE_ID)
    lease = service.acquire(queued.operation_id)
    assert lease is not None
    service.run(lease)

    operation = service.creation_status(queued.operation_id)
    assert operation.state.value == "failed"
    assert operation.failure_code == "development_source_changed_during_capture"


def test_failure_after_generation_zero_tree_requires_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _source = _service(tmp_path)
    calls = 0

    def fail_final_compare(
        first: DevelopmentTreeManifest, second: DevelopmentTreeManifest
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected before ready publication")
        compare_captures(first, second)

    monkeypatch.setattr(
        "kodi_lab.workspace.storage.compare_captures", fail_final_compare
    )
    queued = service.queue_creation(SOURCE_ID)
    lease = service.acquire(queued.operation_id)
    assert lease is not None
    service.run(lease)

    assert (
        service.creation_status(queued.operation_id).state.value
        == "reconciliation_required"
    )
    root = tmp_path / "data" / "agent-workspaces" / str(queued.workspace_id)
    assert (root / "baseline" / "tree").is_dir()
    assert (root / "tree").is_dir()
