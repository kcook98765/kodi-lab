from __future__ import annotations

import os
import socket
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from kodi_lab.application.agent_workspace_cleanup import (
    AgentWorkspaceCleanupError,
    AgentWorkspaceCleanupService,
)
from kodi_lab.application.agent_workspaces import (
    AgentWorkspaceConflictError,
    AgentWorkspaceMutationRead,
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
from kodi_lab.storage.repositories import (
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
)
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
            UUID("50000000-0000-0000-0000-000000000001"),
            UUID("50000000-0000-0000-0000-000000000002"),
            UUID("50000000-0000-0000-0000-000000000003"),
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


def test_ready_workspace_abandonment_is_immutable_but_retains_bytes(
    tmp_path: Path,
) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    root = tmp_path / "data" / "agent-workspaces" / str(workspace_id)

    abandoned = service.abandon(workspace_id)

    assert abandoned.state.value == "abandoned"
    assert abandoned.bytes_state.value == "present"
    assert abandoned.abandoned_at is not None
    assert root.is_dir()
    assert service.abandon(workspace_id).state.value == "abandoned"
    with pytest.raises(AgentWorkspaceConflictError):
        service.create_text_file(
            workspace_id,
            expected_generation=0,
            relative_path="new.py",
            content="pass\n",
        )


def test_cleanup_is_exact_safe_and_retains_metadata_history(tmp_path: Path) -> None:
    service, _source = _service(tmp_path)
    first = _create(service)
    second = _create(service)
    service.create_text_file(
        first,
        expected_generation=0,
        relative_path="new.py",
        content="pass\n",
    )
    service.abandon(first)
    first_root = tmp_path / "data" / "agent-workspaces" / str(first)
    second_root = tmp_path / "data" / "agent-workspaces" / str(second)
    outside = tmp_path / "outside"
    outside.write_text("protected", encoding="utf-8")
    (first_root / "tree" / "escape").symlink_to(outside)
    os.mkfifo(first_root / "tree" / "injected-fifo")
    cleanup = AgentWorkspaceCleanupService(
        service._session_factory,
        data_root=tmp_path / "data",
    )

    queued = cleanup.queue(first)
    lease = cleanup.acquire(queued.operation_id)
    assert lease is not None
    cleanup.run(lease)

    result = cleanup.get(queued.operation_id)
    assert result.state.value == "succeeded"
    assert not first_root.exists()
    assert second_root.is_dir()
    assert outside.read_text(encoding="utf-8") == "protected"
    retained = service.get(first)
    assert retained.state.value == "abandoned"
    assert retained.bytes_state.value == "cleaned"
    assert retained.cleaned_at is not None
    assert service.list_activity(first)[0].path == "new.py"
    assert {
        item.operation.value for item in service.list_lifecycle_activity(first)
    } == {
        "abandoned",
        "cleanup_queued",
        "cleanup_succeeded",
    }
    assert cleanup.queue(first).operation_id == queued.operation_id
    with pytest.raises(AgentWorkspaceConflictError) as cleaned:
        service.read_file(first, "addon.xml")
    assert cleaned.value.code == "workspace_bytes_cleaned"


def test_cleanup_requires_abandonment_and_fails_safely_before_marker(
    tmp_path: Path,
) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    cleanup = AgentWorkspaceCleanupService(
        service._session_factory,
        data_root=tmp_path / "data",
    )
    with pytest.raises(AgentWorkspaceCleanupError) as active:
        cleanup.queue(workspace_id)
    assert active.value.code == "workspace_cleanup_not_eligible"

    service.abandon(workspace_id)
    cleanup = AgentWorkspaceCleanupService(
        service._session_factory,
        data_root=tmp_path / "data",
        failure_injector=lambda phase: (
            (_ for _ in ()).throw(RuntimeError("stop"))
            if phase == "before_deletion_marker"
            else None
        ),
    )
    queued = cleanup.queue(workspace_id)
    lease = cleanup.acquire(queued.operation_id)
    assert lease is not None
    cleanup.run(lease)
    assert cleanup.get(queued.operation_id).state.value == "failed"
    assert (tmp_path / "data" / "agent-workspaces" / str(workspace_id)).is_dir()
    retry = AgentWorkspaceCleanupService(
        service._session_factory,
        data_root=tmp_path / "data",
    )
    retried = retry.queue(workspace_id)
    assert retried.operation_id != queued.operation_id
    retry_lease = retry.acquire(retried.operation_id)
    assert retry_lease is not None
    retry.run(retry_lease)
    assert retry.get(retried.operation_id).state.value == "succeeded"


def test_cleanup_same_operation_resumes_after_stale_destructive_crash(
    tmp_path: Path,
) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    service.abandon(workspace_id)
    crashed = False
    current = [datetime(2026, 9, 18, tzinfo=UTC)]

    def inject(phase: str) -> None:
        nonlocal crashed
        if phase == "before_delete" and not crashed:
            crashed = True
            raise SystemExit("simulated worker crash")

    cleanup = AgentWorkspaceCleanupService(
        service._session_factory,
        data_root=tmp_path / "data",
        clock=lambda: current[0],
        failure_injector=inject,
    )
    queued = cleanup.queue(workspace_id)
    lease = cleanup.acquire(queued.operation_id)
    assert lease is not None
    with pytest.raises(SystemExit):
        cleanup.run(lease)
    interrupted = cleanup.get(queued.operation_id)
    assert interrupted.state.value == "running"
    assert interrupted.deletion_started is True

    current[0] += timedelta(minutes=31)
    recovered = AgentWorkspaceCleanupService(
        service._session_factory,
        data_root=tmp_path / "data",
        clock=lambda: current[0],
    )
    recovered.sweep_startup()
    assert recovered.get(queued.operation_id).state.value == "queued"
    resumed_lease = recovered.acquire(queued.operation_id)
    assert resumed_lease is not None
    recovered.run(resumed_lease)

    assert recovered.get(queued.operation_id).state.value == "succeeded"
    assert service.get(workspace_id).bytes_state.value == "cleaned"


def test_abandonment_and_cleanup_protect_active_or_reconciliation_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    monkeypatch.setattr(
        "kodi_lab.application.agent_workspaces."
        "AgentWorkspacePromotionPlanRepository.active_for_workspace",
        lambda *_args, **_kwargs: object(),
    )
    with pytest.raises(AgentWorkspaceConflictError) as active:
        service.abandon(workspace_id)
    assert active.value.code == "workspace_promotion_active"
    monkeypatch.undo()

    with service._session_factory.begin() as session:
        workspace = AgentWorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert AgentWorkspaceRepository(session).require_reconciliation(
            workspace_id,
            expected_generation=workspace.generation,
            now=datetime(2026, 9, 18, tzinfo=UTC),
        )
    with pytest.raises(AgentWorkspaceConflictError) as reconciliation:
        service.abandon(workspace_id)
    assert reconciliation.value.code == "workspace_reconciliation_blocked"
    cleanup = AgentWorkspaceCleanupService(
        service._session_factory,
        data_root=tmp_path / "data",
    )
    with pytest.raises(AgentWorkspaceCleanupError) as protected:
        cleanup.queue(workspace_id)
    assert protected.value.code == "workspace_reconciliation_blocked"


def test_cleanup_blocks_active_promotion_and_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    service.abandon(workspace_id)
    cleanup = AgentWorkspaceCleanupService(
        service._session_factory,
        data_root=tmp_path / "data",
    )
    monkeypatch.setattr(
        "kodi_lab.application.agent_workspace_cleanup."
        "AgentWorkspacePromotionOperationRepository.active_for_workspace",
        lambda *_args, **_kwargs: object(),
    )
    with pytest.raises(AgentWorkspaceCleanupError) as promotion:
        cleanup.queue(workspace_id)
    assert promotion.value.code == "workspace_cleanup_promotion_active"
    monkeypatch.undo()
    monkeypatch.setattr(
        "kodi_lab.application.agent_workspace_cleanup."
        "PromotionVerificationOperationRepository.blocking_for_workspace",
        lambda *_args, **_kwargs: [object()],
    )
    with pytest.raises(AgentWorkspaceCleanupError) as verification:
        cleanup.queue(workspace_id)
    assert verification.value.code == "workspace_cleanup_verification_blocked"


def test_cleanup_does_not_adopt_unexplained_missing_storage(tmp_path: Path) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    service.abandon(workspace_id)
    root = tmp_path / "data" / "agent-workspaces" / str(workspace_id)
    displaced = tmp_path / "externally-displaced-workspace"
    root.rename(displaced)
    cleanup = AgentWorkspaceCleanupService(
        service._session_factory,
        data_root=tmp_path / "data",
    )
    queued = cleanup.queue(workspace_id)
    lease = cleanup.acquire(queued.operation_id)
    assert lease is not None

    cleanup.run(lease)

    assert cleanup.get(queued.operation_id).state.value == "reconciliation_required"
    assert service.get(workspace_id).state.value == "reconciliation_required"
    assert displaced.is_dir()


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


def test_create_replace_patch_increment_generation_and_derive_diff(
    tmp_path: Path,
) -> None:
    service, source = _service(tmp_path)
    workspace_id = _create(service)

    created = service.create_text_file(
        workspace_id,
        expected_generation=0,
        relative_path="resources/new.txt",
        content="Snowman ☃\n",
    )
    assert created.generation == 1
    replaced = service.replace_text_file(
        workspace_id,
        expected_generation=1,
        relative_path="resources/main.py",
        content="print('replaced')\n",
    )
    assert replaced.generation == 2
    patched = service.apply_exact_patch(
        workspace_id,
        expected_generation=2,
        relative_path="resources/main.py",
        expected_text="print('replaced')\n",
        replacement_text="",
    )
    assert patched.generation == 3
    assert service.read_file(workspace_id, "resources/main.py").content == ""

    changes = service.list_changes(workspace_id)
    assert changes.generation == 3
    assert changes.identity_status == "stable"
    assert [(item.path, item.change_kind) for item in changes.changes] == [
        ("resources/main.py", "modified"),
        ("resources/new.txt", "added"),
    ]
    assert [item.operation for item in service.list_activity(workspace_id)] == [
        "patched",
        "replaced",
        "created",
    ]
    assert (source / "resources" / "main.py").read_text() == "print('safe')\n"


def test_create_replace_patch_preconditions_leave_bytes_and_generation_unchanged(
    tmp_path: Path,
) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    original = service.read_file(workspace_id, "resources/main.py").content

    failures: tuple[Callable[[], AgentWorkspaceMutationRead], ...] = (
        lambda: service.create_text_file(
            workspace_id,
            expected_generation=0,
            relative_path="resources/main.py",
            content="overwrite",
        ),
        lambda: service.replace_text_file(
            workspace_id,
            expected_generation=0,
            relative_path="resources/missing.py",
            content="missing",
        ),
        lambda: service.apply_exact_patch(
            workspace_id,
            expected_generation=0,
            relative_path="resources/main.py",
            expected_text="wrong preimage",
            replacement_text="changed",
        ),
    )
    for operation in failures:
        with pytest.raises(AgentWorkspaceStorageError):
            operation()
        assert service.get(workspace_id).generation == 0
        assert service.read_file(workspace_id, "resources/main.py").content == original

    service.create_text_file(
        workspace_id,
        expected_generation=0,
        relative_path="resources/first.txt",
        content="first",
    )
    with pytest.raises(AgentWorkspaceConflictError) as stale:
        service.create_text_file(
            workspace_id,
            expected_generation=0,
            relative_path="resources/second.txt",
            content="second",
        )
    assert stale.value.code == "agent_workspace_generation_conflict"
    assert service.get(workspace_id).generation == 1


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/absolute",
        ".",
        "..",
        "resources/../escape",
        "resources\\file",
        "resources/\x00file",
        "resources/\x01file",
        "x" * 256,
        "/".join(("a" * 200, "b" * 200, "c" * 200)),
        "/".join("x" for _ in range(33)),
    ],
)
def test_mutation_path_policy_rejects_unsafe_paths(tmp_path: Path, path: str) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    with pytest.raises(AgentWorkspaceStorageError) as rejected:
        service.create_text_file(
            workspace_id,
            expected_generation=0,
            relative_path=path,
            content="safe",
        )
    assert rejected.value.code == "agent_workspace_path_invalid"
    assert service.get(workspace_id).generation == 0


@pytest.mark.parametrize("kind", ["directory", "symlink", "hardlink", "fifo", "socket"])
def test_mutation_rejects_unsafe_targets_and_external_tree_objects(
    tmp_path: Path, kind: str
) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    tree = tmp_path / "data" / "agent-workspaces" / str(workspace_id) / "tree"
    target = tree / "unsafe"
    opened_socket = None
    socket_link = None
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to("addon.xml")
    elif kind == "hardlink":
        os.link(tree / "addon.xml", target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        socket_link = Path("/dev/shm") / f"kodi-lab-ws-{uuid4().hex}"
        socket_link.symlink_to(tree, target_is_directory=True)
        opened_socket = socket.socket(socket.AF_UNIX)
        opened_socket.bind(str(socket_link / "unsafe"))
    try:
        with pytest.raises(AgentWorkspaceStorageError):
            service.replace_text_file(
                workspace_id,
                expected_generation=0,
                relative_path="unsafe",
                content="safe",
            )
        assert service.get(workspace_id).state.value == "reconciliation_required"
        assert service.get(workspace_id).generation == 0
    finally:
        if opened_socket is not None:
            opened_socket.close()
        if socket_link is not None:
            socket_link.unlink()


def test_text_bounds_utf8_and_existing_binary_policy(tmp_path: Path) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    service.create_text_file(
        workspace_id,
        expected_generation=0,
        relative_path="resources/boundary.txt",
        content="x" * (2 * 1024 * 1024),
    )
    assert service.get(workspace_id).generation == 1
    with pytest.raises(AgentWorkspaceStorageError) as oversized:
        service.replace_text_file(
            workspace_id,
            expected_generation=1,
            relative_path="resources/boundary.txt",
            content="x" * (2 * 1024 * 1024 + 1),
        )
    assert oversized.value.code == "agent_workspace_content_too_large"

    tree = tmp_path / "data" / "agent-workspaces" / str(workspace_id) / "tree"
    (tree / "resources" / "binary").write_bytes(b"\xff")
    with pytest.raises(AgentWorkspaceStorageError):
        service.replace_text_file(
            workspace_id,
            expected_generation=1,
            relative_path="resources/binary",
            content="text",
        )
    assert service.get(workspace_id).state.value == "reconciliation_required"


def test_post_write_capture_failure_rolls_back_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    original = service.read_file(workspace_id, "resources/main.py").content
    real_capture = workspace_storage._capture_workspace_tree
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> DevelopmentTreeManifest:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AgentWorkspaceStorageError("injected", "Injected recapture failure.")
        return real_capture(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(workspace_storage, "_capture_workspace_tree", fail_once)
    with pytest.raises(AgentWorkspaceStorageError):
        service.replace_text_file(
            workspace_id,
            expected_generation=0,
            relative_path="resources/main.py",
            content="changed",
        )
    assert service.read_file(workspace_id, "resources/main.py").content == original
    assert service.get(workspace_id).generation == 0
    assert service.get(workspace_id).state.value == "ready"


@pytest.mark.parametrize("failure_point", ["temporary_write", "rename"])
def test_prepublication_write_failures_leave_original_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    original = service.read_file(workspace_id, "resources/main.py").content

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("injected prepublication failure")

    target = (
        "_write_private_file"
        if failure_point == "temporary_write"
        else "_atomic_replace"
    )
    monkeypatch.setattr(workspace_storage, target, fail)
    with pytest.raises(OSError):
        service.replace_text_file(
            workspace_id,
            expected_generation=0,
            relative_path="resources/main.py",
            content="changed",
        )
    assert service.read_file(workspace_id, "resources/main.py").content == original
    assert service.get(workspace_id).generation == 0
    assert service.get(workspace_id).state.value == "ready"


def test_database_publication_failure_rolls_back_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    original = service.read_file(workspace_id, "resources/main.py").content
    monkeypatch.setattr(
        "kodi_lab.storage.repositories.AgentWorkspaceRepository.publish_mutation",
        lambda *args, **kwargs: False,
    )
    with pytest.raises(AgentWorkspaceConflictError):
        service.replace_text_file(
            workspace_id,
            expected_generation=0,
            relative_path="resources/main.py",
            content="changed",
        )
    assert service.read_file(workspace_id, "resources/main.py").content == original
    assert service.get(workspace_id).generation == 0


def test_external_injection_during_postcapture_requires_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    original = service.read_file(workspace_id, "resources/main.py").content
    tree = tmp_path / "data" / "agent-workspaces" / str(workspace_id) / "tree"
    real_capture = workspace_storage._capture_workspace_tree
    calls = 0

    def inject(*args: object, **kwargs: object) -> DevelopmentTreeManifest:
        nonlocal calls
        calls += 1
        if calls == 2:
            (tree / "late-link").symlink_to("addon.xml")
        return real_capture(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(workspace_storage, "_capture_workspace_tree", inject)
    with pytest.raises(workspace_storage.AgentWorkspaceReconciliationRequired):
        service.replace_text_file(
            workspace_id,
            expected_generation=0,
            relative_path="resources/main.py",
            content="changed",
        )
    assert (tree / "resources" / "main.py").read_text() == original
    assert service.get(workspace_id).generation == 0
    assert service.get(workspace_id).state.value == "reconciliation_required"


def test_full_tree_recapture_detects_injected_vcs_and_missing_baseline_file(
    tmp_path: Path,
) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    tree = tmp_path / "data" / "agent-workspaces" / str(workspace_id) / "tree"
    (tree / ".git").mkdir()
    with pytest.raises(AgentWorkspaceStorageError):
        service.create_text_file(
            workspace_id,
            expected_generation=0,
            relative_path="resources/new.txt",
            content="new",
        )
    assert service.get(workspace_id).state.value == "reconciliation_required"

    (tmp_path / "second").mkdir()
    service2, _source2 = _service(tmp_path / "second")
    workspace2 = _create(service2)
    tree2 = tmp_path / "second" / "data" / "agent-workspaces" / str(workspace2) / "tree"
    (tree2 / "resources" / "main.py").unlink()
    with pytest.raises(AgentWorkspaceStorageError):
        service2.list_changes(workspace2)
    assert service2.get(workspace2).state.value == "reconciliation_required"


def test_addon_identity_change_is_allowed_but_classified(tmp_path: Path) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)
    current = service.read_file(workspace_id, "addon.xml").content
    changed = current.replace("plugin.video.example", "plugin.video.changed")
    service.apply_exact_patch(
        workspace_id,
        expected_generation=0,
        relative_path="addon.xml",
        expected_text=current,
        replacement_text=changed,
    )
    diff = service.list_changes(workspace_id)
    assert diff.identity_status == "identity_changed"
    assert [item.path for item in diff.changes] == ["addon.xml"]
    assert service.get(workspace_id).source_addon_id == "plugin.video.example"


def test_pristine_binary_is_omitted_and_external_binary_change_fails_closed(
    tmp_path: Path,
) -> None:
    service, source = _service(tmp_path)
    (source / "resources" / "asset.bin").write_bytes(b"\x00\xff")
    workspace_id = _create(service)
    assert service.list_changes(workspace_id).changes == []

    tree = tmp_path / "data" / "agent-workspaces" / str(workspace_id) / "tree"
    (tree / "resources" / "asset.bin").write_bytes(b"\x00\xfe")
    with pytest.raises(AgentWorkspaceStorageError):
        service.list_changes(workspace_id)
    assert service.get(workspace_id).state.value == "reconciliation_required"


def test_unknown_and_non_ready_workspaces_reject_mutation(tmp_path: Path) -> None:
    service, _source = _service(tmp_path)
    unknown = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    with pytest.raises(AgentWorkspaceNotFoundError):
        service.create_text_file(
            unknown,
            expected_generation=0,
            relative_path="new.txt",
            content="new",
        )

    queued = service.queue_creation(SOURCE_ID)
    with pytest.raises(AgentWorkspaceConflictError) as not_ready:
        service.create_text_file(
            queued.workspace_id,
            expected_generation=0,
            relative_path="new.txt",
            content="new",
        )
    assert not_ready.value.code == "agent_workspace_not_ready"


def test_mutation_does_not_change_source_or_sibling_workspace(tmp_path: Path) -> None:
    service, source = _service(tmp_path)
    first = _create(service)
    second = _create(service)
    original = (source / "resources" / "main.py").read_text()

    service.replace_text_file(
        first,
        expected_generation=0,
        relative_path="resources/main.py",
        content="first only\n",
    )

    assert (source / "resources" / "main.py").read_text() == original
    assert service.read_file(second, "resources/main.py").content == original
    assert service.get(second).generation == 0


def test_competing_mutations_cannot_both_publish_same_generation(
    tmp_path: Path,
) -> None:
    service, _source = _service(tmp_path)
    workspace_id = _create(service)

    def create(name: str) -> str:
        try:
            service.create_text_file(
                workspace_id,
                expected_generation=0,
                relative_path=f"resources/{name}.txt",
                content=name,
            )
        except AgentWorkspaceConflictError:
            return "conflict"
        return "succeeded"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, ("one", "two")))
    assert sorted(results) == ["conflict", "succeeded"]
    assert service.get(workspace_id).generation == 1
    assert service.get(workspace_id).state.value == "ready"
