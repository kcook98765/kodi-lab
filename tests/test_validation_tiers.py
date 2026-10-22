from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import validation_tiers as lanes


@pytest.mark.parametrize(
    "kind", ["empty", "relative", "missing", "file", "link", "readonly"]
)
def test_lane_rejects_unsafe_temp_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    path = tmp_path / "parent"
    if kind == "file":
        path.touch()
    elif kind == "link":
        path.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "readonly":
        path.mkdir(mode=0o500)
    value = "" if kind == "empty" else "relative" if kind == "relative" else str(path)
    monkeypatch.setenv("KODI_LAB_CHECK_PYTEST_TMP_PARENT", value)
    with pytest.raises(SystemExit, match="absolute, writable and non-symlink"):
        lanes._temp_parent()


def test_lane_honors_explicit_parent_and_cleans_its_own_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private parent"
    parent.mkdir()
    marker = parent / "unrelated"
    marker.write_text("preserved")
    monkeypatch.setenv("KODI_LAB_CHECK_PYTEST_TMP_PARENT", str(parent))
    assert lanes._temp_parent() == str(parent)
    with lanes._pytest_temp() as base:
        base.mkdir()
        child = base / "readonly"
        child.mkdir()
        (child / "data").write_text("test")
        child.chmod(0o500)
        assert base.parent.parent == parent
    assert list(parent.iterdir()) == [marker]
    assert marker.read_text() == "preserved"


@pytest.mark.parametrize(
    "free,expected", [(2 * 1024**3, None), (3 * 1024**3, "/dev/shm")]
)
def test_lane_keeps_reserve_headroom_when_selecting_tmpfs(
    monkeypatch: pytest.MonkeyPatch, free: int, expected: str | None
) -> None:
    monkeypatch.delenv("KODI_LAB_CHECK_PYTEST_TMP_PARENT", raising=False)
    monkeypatch.setattr(
        os, "statvfs", lambda _path: SimpleNamespace(f_bavail=free, f_frsize=1)
    )
    assert lanes._temp_parent() == expected


@pytest.mark.parametrize("failed", [False, True])
def test_lane_final_cleanup_never_changes_outside_link_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"preserved")
    sentinel.chmod(0o400)
    monkeypatch.setenv("KODI_LAB_CHECK_PYTEST_TMP_PARENT", str(parent))
    try:
        with lanes._pytest_temp() as base:
            base.mkdir()
            os.link(sentinel, base / "hardlink")
            (base / "link").symlink_to(tmp_path, target_is_directory=True)
            child = base / "readonly"
            child.mkdir()
            child.chmod(0o500)
            if failed:
                raise RuntimeError("fixture failure")
    except RuntimeError as error:
        assert failed and str(error) == "fixture failure"
    assert not list(parent.iterdir())
    assert sentinel.read_bytes() == b"preserved"
    assert sentinel.stat().st_mode & 0o777 == 0o400
