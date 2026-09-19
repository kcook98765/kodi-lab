from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import pytest

from kodi_lab.workspace import (
    AgentWorkspacePromotionStorageError,
    PromotionPathMaterial,
    PromotionReviewMaterial,
    execute_promotion,
    prepare_promotion_review,
)
from kodi_lab.workspace.storage import _capture_workspace_tree

WORKSPACE_ID = UUID("81000000-0000-4000-8000-000000000001")
OPERATION_ID = UUID("81000000-0000-4000-8000-000000000002")
BACKUP_ID = UUID("81000000-0000-4000-8000-000000000003")
ADDON_ID = "plugin.video.example"


def _source(root: Path) -> Path:
    source = root / "source"
    (source / "resources").mkdir(parents=True)
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.example" name="Example" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    (source / "main.py").write_text("print('old')\n", encoding="utf-8")
    (source / "resources" / "settings.xml").write_text(
        "<settings />\n", encoding="utf-8"
    )
    return source


def _workspace(data_root: Path, source: Path) -> tuple[Path, Path]:
    root = data_root / "agent-workspaces" / str(WORKSPACE_ID)
    baseline = root / "baseline" / "tree"
    tree = root / "tree"
    baseline.parent.mkdir(parents=True)
    shutil.copytree(source, baseline)
    shutil.copytree(source, tree)
    return baseline, tree


def _review(data_root: Path, development_root: Path) -> PromotionReviewMaterial:
    baseline = _capture_workspace_tree(data_root, WORKSPACE_ID, baseline=True)
    workspace = _capture_workspace_tree(data_root, WORKSPACE_ID)
    return prepare_promotion_review(
        data_root,
        WORKSPACE_ID,
        development_root,
        "source",
        expected_baseline_digest=baseline.digest,
        expected_workspace_digest=workspace.digest,
        expected_addon_id=ADDON_ID,
    )


def test_three_way_review_is_exact_and_ignores_safe_unrelated_source_changes(
    tmp_path: Path,
) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    (tree / "main.py").write_text("print('new')\n", encoding="utf-8")
    (tree / "resources" / "new.py").write_text("value = 1\n", encoding="utf-8")
    (source / "unrelated.txt").write_text("safe dirtiness\n", encoding="utf-8")
    (source / "resources" / "settings.xml").unlink()

    review = _review(data_root, development_root)

    assert not review.conflicted
    assert [item.relative_path for item in review.paths] == [
        "main.py",
        "resources/new.py",
    ]
    assert [item.change_kind for item in review.paths] == ["modify", "add"]
    assert review.paths[0].before_text == "print('old')\n"
    assert review.paths[0].after_text == "print('new')\n"
    assert review.paths[1].before_text is None
    assert review.paths[1].after_text == "value = 1\n"
    assert review.review_text_bytes == 36
    assert review.permission_classification == "promotion_enabled"


@pytest.mark.parametrize(
    ("workspace_change", "source_change", "reason"),
    [
        ("modify", "modify", "overlapping_source_modification"),
        ("add", "add", "source_addition_conflict"),
    ],
)
def test_three_way_review_reports_only_overlapping_conflicts(
    tmp_path: Path, workspace_change: str, source_change: str, reason: str
) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    relative = "main.py" if workspace_change == "modify" else "added.py"
    (tree / relative).write_text("workspace\n", encoding="utf-8")
    if source_change == "modify":
        (source / relative).write_text("source\n", encoding="utf-8")
    else:
        (source / relative).write_text("source addition\n", encoding="utf-8")

    review = _review(data_root, development_root)

    assert review.conflicted
    assert review.paths[0].conflict_reason == reason


def test_unsafe_source_object_anywhere_blocks_review(tmp_path: Path) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    (tree / "main.py").write_text("workspace\n", encoding="utf-8")
    (source / "unsafe").symlink_to("main.py")

    with pytest.raises(AgentWorkspacePromotionStorageError) as failure:
        _review(data_root, development_root)

    assert failure.value.code == "promotion_source_unsafe"


def test_addon_id_change_and_binary_change_are_ineligible(tmp_path: Path) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    (tree / "addon.xml").write_text(
        '<addon id="plugin.video.other" name="Other" version="2">'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )
    with pytest.raises(AgentWorkspacePromotionStorageError) as identity:
        _review(data_root, development_root)
    assert identity.value.code == "promotion_source_identity_drift"

    shutil.rmtree(data_root)
    _baseline, tree = _workspace(data_root, source)
    (tree / "main.py").write_bytes(b"\xff\x00")
    with pytest.raises(AgentWorkspacePromotionStorageError) as binary:
        _review(data_root, development_root)
    assert binary.value.code == "promotion_binary_change"


def test_valid_descriptor_metadata_edit_is_promotable_without_xml_merge(
    tmp_path: Path,
) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    (tree / "addon.xml").write_text(
        '<addon id="plugin.video.example" name="Renamed" version="2.0.0" '
        'provider-name="Provider">'
        '<requires><import addon="xbmc.python" version="3.0.0" /></requires>'
        '<extension point="xbmc.python.pluginsource" library="main.py" />'
        "</addon>\n",
        encoding="utf-8",
    )

    review = _review(data_root, development_root)

    assert not review.conflicted
    assert review.current_name == "Example"
    assert review.current_version == "1.0.0"
    assert review.promoted_name == "Renamed"
    assert review.promoted_version == "2.0.0"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("delete", "promotion_delete_unsupported"),
        ("rename", "promotion_delete_unsupported"),
        ("directory", "promotion_directory_creation_unsupported"),
        ("type", "promotion_type_transition"),
        ("mode", "promotion_mode_only_transition"),
    ],
)
def test_delete_rename_directory_type_and_mode_only_changes_are_rejected(
    tmp_path: Path, mutation: str, code: str
) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    target = tree / "main.py"
    if mutation == "delete":
        target.unlink()
    elif mutation == "rename":
        target.rename(tree / "renamed.py")
    elif mutation == "directory":
        (tree / "new-directory").mkdir()
    elif mutation == "type":
        target.unlink()
        target.mkdir()
    else:
        target.chmod(0o755)

    with pytest.raises(AgentWorkspacePromotionStorageError) as failure:
        _review(data_root, development_root)

    assert failure.value.code == code


def test_review_file_limit_accepts_100_and_rejects_101(tmp_path: Path) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    for index in range(100):
        (tree / f"new-{index:03d}.txt").write_text("x\n", encoding="utf-8")
    assert len(_review(data_root, development_root).paths) == 100
    (tree / "new-100.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(AgentWorkspacePromotionStorageError) as failure:
        _review(data_root, development_root)
    assert failure.value.code == "promotion_review_too_large"


def test_review_text_limit_accepts_four_mib_and_rejects_larger(tmp_path: Path) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    payload = "a" * (2 * 1024 * 1024)
    (source / "main.py").write_text(payload, encoding="utf-8")
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    (tree / "main.py").write_text("b" * len(payload), encoding="utf-8")
    assert _review(data_root, development_root).review_text_bytes == 4 * 1024 * 1024
    (tree / "main.py").write_text("b" * (len(payload) + 1), encoding="utf-8")
    with pytest.raises(AgentWorkspacePromotionStorageError) as failure:
        _review(data_root, development_root)
    assert failure.value.code == "promotion_review_too_large"


def test_readable_source_without_write_authority_is_classified_and_rejected(
    tmp_path: Path,
) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    (tree / "main.py").write_text("new\n", encoding="utf-8")
    source.chmod(0o555)
    try:
        review = _review(data_root, development_root)
        assert review.permission_classification == "readable_not_promotion_writable"
        with pytest.raises(AgentWorkspacePromotionStorageError) as failure:
            _execute(data_root, development_root, review.paths)
        assert failure.value.code == "promotion_source_not_writable"
    finally:
        source.chmod(0o755)


def _execute(
    data_root: Path,
    development_root: Path,
    paths: tuple[PromotionPathMaterial, ...],
    *,
    injector: Callable[[str], None] = lambda _phase: None,
) -> tuple[list[str], list[tuple[int, str, int | None, int | None]]]:
    baseline = _capture_workspace_tree(data_root, WORKSPACE_ID, baseline=True)
    workspace = _capture_workspace_tree(data_root, WORKSPACE_ID)
    journal: list[tuple[int, str, int | None, int | None]] = []
    events: list[str] = []
    execute_promotion(
        data_root,
        WORKSPACE_ID,
        development_root,
        "source",
        operation_id=OPERATION_ID,
        backup_id=BACKUP_ID,
        expected_baseline_digest=baseline.digest,
        expected_workspace_digest=workspace.digest,
        expected_addon_id=ADDON_ID,
        paths=paths,
        backup_published=lambda _value: events.append("backup"),
        application_marker=lambda: events.append("marker"),
        journal_update=lambda ordinal, state, device, inode: journal.append(
            (ordinal, state, device, inode)
        ),
        final_capture_started=lambda: events.append("final_capture"),
        publish_success=lambda: events.append("success"),
        failure_injector=injector,
    )
    return events, journal


def test_exact_apply_uses_lexical_order_and_preserves_unrelated_source(
    tmp_path: Path,
) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    (source / "main.py").chmod(0o664)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    (tree / "main.py").write_text("print('new')\n", encoding="utf-8")
    (tree / "resources" / "new.py").write_text("new = True\n", encoding="utf-8")
    (source / "unrelated").write_text("untouched\n", encoding="utf-8")
    review = _review(data_root, development_root)

    events, journal = _execute(data_root, development_root, review.paths)

    assert events == ["backup", "marker", "final_capture", "success"]
    assert [(ordinal, state) for ordinal, state, _device, _inode in journal] == [
        (0, "applied"),
        (1, "applied"),
    ]
    assert (source / "main.py").read_text(encoding="utf-8") == "print('new')\n"
    assert (source / "resources" / "new.py").read_text(encoding="utf-8") == (
        "new = True\n"
    )
    assert (source / "unrelated").read_text(encoding="utf-8") == "untouched\n"
    backup = data_root / "source-promotions" / str(OPERATION_ID) / f"backup-{BACKUP_ID}"
    assert backup.is_dir()
    assert (backup / "000.preimage").read_text(encoding="utf-8") == "print('old')\n"
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    assert manifest[0]["normalized_mode"] == 0o644
    assert manifest[0]["raw_mode"] == 0o664
    assert not (backup / "001.preimage").exists()
    assert stat.S_IMODE(os.stat(source / "main.py").st_mode) == 0o644
    assert (tree / "main.py").read_text(encoding="utf-8") == "print('new')\n"
    baseline = data_root / "agent-workspaces" / str(WORKSPACE_ID) / "baseline" / "tree"
    assert (baseline / "main.py").read_text(encoding="utf-8") == "print('old')\n"


@pytest.mark.parametrize(
    "phase",
    [
        "after_marker_before_first_write",
        "after_first_modification",
        "after_first_addition",
        "mid_multi_file_apply",
        "after_all_writes_before_final_recapture",
        "after_final_recapture_before_db_success",
    ],
)
def test_failure_after_marker_rolls_back_exactly(tmp_path: Path, phase: str) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    (tree / "main.py").write_text("modified\n", encoding="utf-8")
    (tree / "resources" / "added.py").write_text("added\n", encoding="utf-8")
    review = _review(data_root, development_root)

    def inject(current: str) -> None:
        if current == phase:
            raise RuntimeError("injected")

    with pytest.raises(AgentWorkspacePromotionStorageError) as failure:
        _execute(data_root, development_root, review.paths, injector=inject)

    assert not failure.value.reconciliation_required
    assert (source / "main.py").read_text(encoding="utf-8") == "print('old')\n"
    assert not (source / "resources" / "added.py").exists()


@pytest.mark.parametrize(
    "phase", ["before_backup_publication", "after_backup_before_marker"]
)
def test_failure_before_marker_leaves_source_unchanged(
    tmp_path: Path, phase: str
) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    (tree / "main.py").write_text("modified\n", encoding="utf-8")
    review = _review(data_root, development_root)

    def inject(current: str) -> None:
        if current == phase:
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        _execute(data_root, development_root, review.paths, injector=inject)
    assert (source / "main.py").read_text(encoding="utf-8") == "print('old')\n"


def test_unprovable_postimage_never_blindly_rolls_back(tmp_path: Path) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    (tree / "main.py").write_text("modified\n", encoding="utf-8")
    review = _review(data_root, development_root)

    def inject(phase: str) -> None:
        if phase == "after_first_modification":
            (source / "main.py").write_text("external-after-apply\n", encoding="utf-8")
            raise RuntimeError("injected")

    with pytest.raises(AgentWorkspacePromotionStorageError) as failure:
        _execute(data_root, development_root, review.paths, injector=inject)

    assert failure.value.code == "promotion_reconciliation_required"
    assert failure.value.reconciliation_required
    assert (source / "main.py").read_text(encoding="utf-8") == (
        "external-after-apply\n"
    )


def test_apply_revalidates_exact_target_preimage_and_workspace_tree(
    tmp_path: Path,
) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    (tree / "main.py").write_text("modified\n", encoding="utf-8")
    review = _review(data_root, development_root)
    (source / "main.py").write_text("overlap\n", encoding="utf-8")
    with pytest.raises(AgentWorkspacePromotionStorageError) as overlap:
        _execute(data_root, development_root, review.paths)
    assert overlap.value.code == "promotion_source_overlap"

    (source / "main.py").write_text("print('old')\n", encoding="utf-8")
    expected = _capture_workspace_tree(data_root, WORKSPACE_ID)
    (tree / "main.py").write_text("external drift\n", encoding="utf-8")
    baseline = _capture_workspace_tree(data_root, WORKSPACE_ID, baseline=True)
    with pytest.raises(AgentWorkspacePromotionStorageError) as drift:
        execute_promotion(
            data_root,
            WORKSPACE_ID,
            development_root,
            "source",
            operation_id=OPERATION_ID,
            backup_id=BACKUP_ID,
            expected_baseline_digest=baseline.digest,
            expected_workspace_digest=expected.digest,
            expected_addon_id=ADDON_ID,
            paths=review.paths,
            backup_published=lambda _value: None,
            application_marker=lambda: None,
            journal_update=lambda _ordinal, _state, _device, _inode: None,
            final_capture_started=lambda: None,
            publish_success=lambda: None,
        )
    assert drift.value.code == "promotion_workspace_drift"


def test_apply_never_creates_missing_parent(tmp_path: Path) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    (tree / "resources" / "new.py").write_text("new\n", encoding="utf-8")
    review = _review(data_root, development_root)
    (source / "resources" / "settings.xml").unlink()
    (source / "resources").rmdir()
    with pytest.raises(AgentWorkspacePromotionStorageError) as failure:
        _execute(data_root, development_root, review.paths)
    assert failure.value.code == "promotion_source_overlap"
    assert not (source / "resources").exists()


def test_registration_or_review_never_changes_source_permissions(
    tmp_path: Path,
) -> None:
    development_root = tmp_path / "sources"
    source = _source(development_root)
    data_root = tmp_path / "data"
    _baseline, tree = _workspace(data_root, source)
    (tree / "main.py").write_text("new\n", encoding="utf-8")
    source.chmod(0o755)
    original = os.stat(source).st_mode & 0o777
    _review(data_root, development_root)
    assert os.stat(source).st_mode & 0o777 == original
