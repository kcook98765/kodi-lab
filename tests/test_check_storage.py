"""Release-gate storage selection without launching pytest or other check stages."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kodi_lab.snapshot.storage import MIN_FREE_BYTES

CHECK = Path(__file__).resolve().parents[1] / "scripts" / "check"
STOP_BEFORE_PYTEST = 86


@dataclass
class CheckHarness:
    root: Path
    environment: dict[str, str] = field(repr=False)
    mktemp_log: Path
    uv_log: Path

    def run(self, override: str | None = None) -> subprocess.CompletedProcess[str]:
        environment = dict(self.environment)
        environment.pop("KODI_LAB_CHECK_PYTEST_TMP_PARENT", None)
        if override is not None:
            environment["KODI_LAB_CHECK_PYTEST_TMP_PARENT"] = override
        return subprocess.run(
            [str(CHECK)],
            cwd=CHECK.parent.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )


@pytest.fixture
def check_harness(tmp_path: Path) -> CheckHarness:
    commands = tmp_path / "commands"
    commands.mkdir()
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    mktemp_log, uv_log = tmp_path / "mktemp.log", tmp_path / "uv.log"
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp is not None
    scripts = {
        "mktemp": (
            'printf \'%s\\n\' "$*" >> "$CHECK_TEST_MKTEMP_LOG"\n'
            f'exec {shlex.quote(real_mktemp)} "$@"\n'
        ),
        "df": "printf 'Avail\\n%s\\n' \"$CHECK_TEST_FREE_BYTES\"\n",
        "stat": "printf '%s\\n' \"$CHECK_TEST_FILESYSTEM\"\n",
        "uv": (
            'printf \'%s\\n\' "$*" >> "$CHECK_TEST_UV_LOG"\n'
            'if [[ "$*" == '
            '*"from kodi_lab.snapshot.storage import MIN_FREE_BYTES"* ]]; then\n'
            "    printf '%s\\n' \"$CHECK_TEST_SNAPSHOT_MIN_BYTES\"\n"
            "    exit 0\n"
            "fi\n"
            f"exit {STOP_BEFORE_PYTEST}\n"
        ),
    }
    for name, script in scripts.items():
        executable = commands / name
        executable.write_text("#!/usr/bin/env bash\nset -eu\n" + script)
        executable.chmod(0o700)
    environment = {
        **os.environ,
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "TMPDIR": str(ordinary),
        "KODI_LAB_TEST_SNAPSHOT_SCRATCH_PARENT": str(snapshots),
        "CHECK_TEST_MKTEMP_LOG": str(mktemp_log),
        "CHECK_TEST_UV_LOG": str(uv_log),
        "CHECK_TEST_FREE_BYTES": str(3 * 1024**3),
        "CHECK_TEST_SNAPSHOT_MIN_BYTES": str(MIN_FREE_BYTES),
        "CHECK_TEST_FILESYSTEM": "tmpfs",
    }
    return CheckHarness(tmp_path, environment, mktemp_log, uv_log)


@pytest.mark.parametrize(
    "kind", ["empty", "relative", "missing", "file", "symlink", "unwritable"]
)
def test_invalid_pytest_parent_is_rejected_before_creating_temporary_directories(
    check_harness: CheckHarness, kind: str
) -> None:
    override = check_harness.root / "pytest-parent"
    if kind == "empty":
        value = ""
    elif kind == "relative":
        value = "relative-parent"
    else:
        value = str(override)
        if kind == "file":
            override.write_text("regular file")
        elif kind == "symlink":
            override.symlink_to(check_harness.root, target_is_directory=True)
        elif kind == "unwritable":
            override.mkdir(mode=0o500)
    try:
        result = check_harness.run(value)
        assert result.returncode == 1
        assert "absolute, writable, non-symlink directory" in result.stderr
        assert not check_harness.mktemp_log.exists()
        assert not check_harness.uv_log.exists()
    finally:
        if kind == "unwritable":
            override.chmod(0o700)


def test_explicit_parent_wins_over_tmpfs_and_owned_directories_are_cleaned(
    check_harness: CheckHarness,
) -> None:
    parent = check_harness.root / "pytest parent with spaces"
    parent.mkdir()
    result = check_harness.run(str(parent))
    assert result.returncode == STOP_BEFORE_PYTEST
    assert f"pytest temp root: {parent}/klp." in result.stdout
    commands = check_harness.mktemp_log.read_text().splitlines()
    assert commands[1].startswith(f"-d --tmpdir={parent} klp.")
    assert not commands[1].startswith("-d --tmpdir=/dev/shm klp.")
    assert list(parent.iterdir()) == []
    assert list((check_harness.root / "snapshots").iterdir()) == []
    assert list((check_harness.root / "ordinary").iterdir()) == []
    calls = check_harness.uv_log.read_text().splitlines()
    assert len(calls) == 2
    assert calls[-1].startswith("run --locked pytest --basetemp ")


def test_unset_override_preserves_current_tmpfs_selection(
    check_harness: CheckHarness,
) -> None:
    result = check_harness.run()
    assert result.returncode == STOP_BEFORE_PYTEST
    shared_memory = Path("/dev/shm")
    selected = (
        shared_memory
        if shared_memory.is_dir() and os.access(shared_memory, os.W_OK)
        else check_harness.root / "ordinary"
    )
    assert f"pytest temp root: {selected}/klp." in result.stdout


def test_unset_override_preserves_non_tmpfs_fallback(
    check_harness: CheckHarness,
) -> None:
    check_harness.environment["CHECK_TEST_FILESYSTEM"] = "ext4"
    result = check_harness.run()
    assert result.returncode == STOP_BEFORE_PYTEST
    assert f"pytest temp root: {check_harness.root}/ordinary/klp." in result.stdout


def test_pytest_parent_override_preserves_snapshot_free_space_reserve(
    check_harness: CheckHarness,
) -> None:
    parent = check_harness.root / "pytest-parent"
    parent.mkdir()
    check_harness.environment["CHECK_TEST_FREE_BYTES"] = str(MIN_FREE_BYTES - 1)
    result = check_harness.run(str(parent))
    assert result.returncode == 1
    assert f"{MIN_FREE_BYTES} are required" in result.stderr
    assert "pytest temp root:" not in result.stdout
    assert len(check_harness.uv_log.read_text().splitlines()) == 1
    assert list(parent.iterdir()) == []
    assert list((check_harness.root / "snapshots").iterdir()) == []
