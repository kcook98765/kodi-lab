"""Run-wrapper and abandoned scratch ownership boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import test_scratch as scratch

CLEANER = Path(__file__).resolve().parents[1] / "scripts" / "clean-test-scratch"


def root_in(parent: Path) -> Path:
    root = parent / "klp.fixture"
    root.mkdir(mode=0o700)
    return root


def test_live_or_unknown_owner_is_never_abandoned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = root_in(tmp_path)
    scratch.register(root, os.getpid())
    assert scratch.abandoned(root, 0) is None

    def unavailable(_pid: int) -> None:
        raise PermissionError("unknown owner")

    monkeypatch.setattr(scratch, "process_start_ticks", unavailable)
    scratch.sweep(tmp_path, 0, delete=True)
    assert root.exists()


def test_abandoned_cleanup_is_marker_bound_and_preserves_external_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = root_in(tmp_path)
    scratch.register(root, os.getpid())
    sentinel = tmp_path / "outside"
    sentinel.write_bytes(b"untouched")
    sentinel.chmod(0o400)
    os.link(sentinel, root / "hardlink")
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    child = root / "readonly"
    child.mkdir(mode=0o500)
    unrelated = tmp_path / "klp.unmarked"
    unrelated.mkdir(mode=0o700)
    (unrelated / "keep").touch()
    monkeypatch.setattr(scratch, "process_start_ticks", lambda _pid: None)
    scratch.sweep(tmp_path, 0, delete=False)
    assert root.exists()
    scratch.sweep(tmp_path, 0, delete=True)
    assert not root.exists()
    assert (unrelated / "keep").exists()
    assert sentinel.read_bytes() == b"untouched"
    assert sentinel.stat().st_mode & 0o777 == 0o400


@pytest.mark.parametrize("defect", ["identity", "symlink", "hardlink", "mode", "large"])
def test_abandoned_markers_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    root = root_in(tmp_path)
    scratch.register(root, os.getpid())
    marker = root / scratch.MARKER
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(marker.read_bytes())
    sentinel.chmod(0o600)
    if defect == "identity":
        document = json.loads(marker.read_text())
        document["ownership"]["identity"][1] += 1
        marker.write_text(json.dumps(document))
    elif defect in {"symlink", "hardlink"}:
        marker.unlink()
        if defect == "symlink":
            marker.symlink_to(sentinel)
        else:
            os.link(sentinel, marker)
    elif defect == "mode":
        marker.chmod(0o644)
    else:
        marker.write_bytes(b"x" * 8193)
    monkeypatch.setattr(scratch, "process_start_ticks", lambda _pid: None)
    scratch.sweep(tmp_path, 0, delete=True)
    assert root.exists()
    assert sentinel.stat().st_mode & 0o777 == 0o600


def test_permission_error_retains_directory_without_rewriting_files(
    tmp_path: Path,
) -> None:
    root = root_in(tmp_path)
    owned = scratch.register(root, os.getpid())
    child = root / "inaccessible"
    child.mkdir(mode=0o000)
    try:
        with pytest.raises(PermissionError):
            owned.remove()
        assert root.exists()
        assert child.stat().st_mode & 0o777 == 0
    finally:
        child.chmod(0o700)
        owned.remove()


def test_cli_sweeper_dry_run_then_removes_only_dead_registered_root(
    tmp_path: Path,
) -> None:
    root = root_in(tmp_path)
    scratch.register(root, os.getpid())
    marker = root / scratch.MARKER
    document = json.loads(marker.read_text())
    document["pid"] = 2**30
    marker.write_text(json.dumps(document))
    command = [
        sys.executable,
        str(CLEANER),
        "--parent",
        str(tmp_path),
        "--minimum-age-seconds",
        "0",
    ]
    dry = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert dry.returncode == 0, dry.stderr
    assert "dead candidate:" in dry.stdout and root.exists()
    deleted = subprocess.run(
        [*command, "--delete"], capture_output=True, text=True, timeout=10
    )
    assert deleted.returncode == 0, deleted.stderr
    assert "removed:" in deleted.stdout and not root.exists()
    assert "validated dead roots: 1; removed: 1" in deleted.stdout


@pytest.mark.parametrize("kind", ["run", "snapshot"])
def test_fifo_markers_do_not_block_cleaner(tmp_path: Path, kind: str) -> None:
    prefix = "klp." if kind == "run" else "kodi-lab-snapshot-tests."
    root = tmp_path / f"{prefix}fifo"
    root.mkdir(mode=0o700)
    name = scratch.MARKER if kind == "run" else ".kodi-lab-test-scratch.json"
    os.mkfifo(root / name, 0o600)
    result = subprocess.run(
        [
            sys.executable,
            str(CLEANER),
            "--parent",
            str(tmp_path),
            "--minimum-age-seconds",
            "0",
            "--delete",
        ],
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr
    assert root.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", True),
        ("pid", 0),
        ("pid", -1),
        ("start_ticks", False),
        ("start_ticks", 0),
        ("start_ticks", -1),
        ("created_at", float("nan")),
        ("created_at", float("inf")),
        ("created_at", 10**400),
        ("created_at", -1),
    ],
)
def test_invalid_marker_numbers_do_not_authorize_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    root = root_in(tmp_path)
    scratch.register(root, os.getpid())
    marker = root / scratch.MARKER
    document = json.loads(marker.read_text())
    document[field] = value
    marker.write_text(json.dumps(document))
    monkeypatch.setattr(scratch, "process_start_ticks", lambda _pid: None)
    scratch.sweep(tmp_path, 0, delete=True)
    assert root.exists()


def test_malformed_process_identity_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "read_text", lambda _path: "1234 (name) S")
    with pytest.raises(ValueError, match="unknown process identity"):
        scratch.process_start_ticks(1234)


def test_snapshot_teardown_uses_creation_identity(tmp_path: Path) -> None:
    from tests import conftest

    root = tmp_path / "case"
    root.mkdir(mode=0o700)
    owned = scratch.OwnedScratch.capture(root, tmp_path)
    conftest._write_marker(root, "fixture-id")
    root.rename(tmp_path / "original")
    root.mkdir(mode=0o700)
    conftest._write_marker(root, "fixture-id")
    sentinel = root / "must-stay"
    sentinel.write_bytes(b"replacement")
    with pytest.raises(ValueError, match="root replaced"):
        conftest._remove_owned_tree(
            root, parent=tmp_path, run_id="fixture-id", owned=owned
        )
    assert sentinel.read_bytes() == b"replacement"


def test_malformed_candidate_does_not_block_later_dead_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first = tmp_path / "klp.a"
    later = tmp_path / "klp.b"
    for root in (first, later):
        root.mkdir(mode=0o700)
        scratch.register(root, os.getpid())
    marker = first / scratch.MARKER
    document = json.loads(marker.read_text())
    document["created_at"] = 10**400
    marker.write_text(json.dumps(document))
    monkeypatch.setattr(scratch, "process_start_ticks", lambda _pid: None)
    scratch.sweep(tmp_path, 0, delete=True)
    assert first.exists() and not later.exists()
    assert "retained unverified" in capsys.readouterr().err
