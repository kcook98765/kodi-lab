from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest

from kodi_lab.snapshot.storage import (
    SnapshotArtifactDeletionError,
    delete_snapshot_artifact,
    snapshot_artifact_presence,
    snapshot_directory,
)

SNAPSHOT_ID = UUID("94100000-0000-4000-8000-000000000001")
SIBLING_ID = UUID("94100000-0000-4000-8000-000000000002")


def prepare_root(tmp_path: Path) -> Path:
    root = tmp_path / "snapshots"
    root.mkdir(mode=0o750)
    os.chmod(root, 0o750)
    return root


def test_exact_nested_tree_and_symlink_are_removed_without_following(
    tmp_path: Path,
) -> None:
    root = prepare_root(tmp_path)
    target = snapshot_directory(tmp_path, SNAPSHOT_ID)
    target.mkdir(mode=0o750)
    nested = target / "nested"
    nested.mkdir()
    (nested / "state.tar").write_bytes(b"state")
    outside = tmp_path / "outside-secret"
    outside.write_bytes(b"keep")
    (nested / "link").symlink_to(outside)
    sibling = snapshot_directory(tmp_path, SIBLING_ID)
    sibling.mkdir(mode=0o750)
    (sibling / "manifest.json").write_bytes(b"sibling")
    staging = root / ".staging"
    staging.mkdir()
    (staging / "sentinel").write_bytes(b"staging")

    assert delete_snapshot_artifact(
        data_root=tmp_path,
        snapshot_id=SNAPSHOT_ID,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )

    assert snapshot_artifact_presence(tmp_path, SNAPSHOT_ID) == "absent"
    assert root.is_dir()
    assert (sibling / "manifest.json").read_bytes() == b"sibling"
    assert (staging / "sentinel").read_bytes() == b"staging"
    assert outside.read_bytes() == b"keep"


def test_absent_exact_artifact_is_idempotent_but_root_must_be_trusted(
    tmp_path: Path,
) -> None:
    root = prepare_root(tmp_path)
    assert not delete_snapshot_artifact(
        data_root=tmp_path,
        snapshot_id=SNAPSHOT_ID,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )
    os.chmod(root, 0o770)
    with pytest.raises(SnapshotArtifactDeletionError, match="storage root identity"):
        delete_snapshot_artifact(
            data_root=tmp_path,
            snapshot_id=SNAPSHOT_ID,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )


def test_target_symlink_is_rejected_and_outside_is_untouched(tmp_path: Path) -> None:
    prepare_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    snapshot_directory(tmp_path, SNAPSHOT_ID).symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(SnapshotArtifactDeletionError, match="directory identity"):
        delete_snapshot_artifact(
            data_root=tmp_path,
            snapshot_id=SNAPSHOT_ID,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        )
    assert sentinel.read_bytes() == b"keep"
    assert snapshot_artifact_presence(tmp_path, SNAPSHOT_ID) == "inconsistent"
