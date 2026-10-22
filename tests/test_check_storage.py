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

    def run(
        self, override: str | None = None, *, script: Path = CHECK
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(self.environment)
        # Nested wrappers never borrow the caller's real snapshot test root.
        environment.pop("KODI_LAB_TEST_SNAPSHOT_SCRATCH_ROOT", None)
        environment.pop("KODI_LAB_CHECK_PYTEST_TMP_PARENT", None)
        if override is not None:
            environment["KODI_LAB_CHECK_PYTEST_TMP_PARENT"] = override
        return subprocess.run(
            [str(script)],
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
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    mktemp_log, uv_log = tmp_path / "mktemp.log", tmp_path / "uv.log"
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp is not None
    scripts = {
        "mktemp": (
            'printf \'%s\\n\' "$*" >> "$CHECK_TEST_MKTEMP_LOG"\n'
            f'exec {shlex.quote(real_mktemp)} "$@"\n'
        ),
        "df": (
            'value="$CHECK_TEST_FREE_BYTES"\n'
            'if [[ "${*: -1}" == "/dev/shm" ]]; then\n'
            '    value="${CHECK_TEST_SHM_FREE_BYTES:-$value}"\n'
            "fi\n"
            "printf 'Avail\\n%s\\n' \"$value\"\n"
        ),
        "stat": "printf '%s\\n' \"$CHECK_TEST_FILESYSTEM\"\n",
        "uv": (
            'printf \'%s\\n\' "$*" >> "$CHECK_TEST_UV_LOG"\n'
            'if [[ "$*" == '
            '*"from kodi_lab.snapshot.storage import MIN_FREE_BYTES"* ]]; then\n'
            "    printf '%s\\n' \"$CHECK_TEST_SNAPSHOT_MIN_BYTES\"\n"
            "    exit 0\n"
            "fi\n"
            'if [[ "$*" == *"ruff format"* && '
            '"${CHECK_TEST_FAIL_STAGE:-}" == "format" ]]; then exit 9; fi\n'
            'if [[ "$*" == *"pytest"* ]]; then\n'
            '    if [[ "$*" == '
            '*"tests/broker/test_authorization_dispatch.py"* ]]; then\n'
            '        if [[ "${CHECK_TEST_FAIL_STAGE:-}" == "contracts" ]]; '
            "then exit 10; fi\n"
            "        exit 0\n"
            "    fi\n"
            '    if [[ -n "${CHECK_TEST_SENTINEL:-}" ]]; then\n'
            "        python3 - \"$@\" <<'PYTEST_SCRATCH_FIXTURE'\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "root = Path(args[args.index('--basetemp') + 1])\n"
            "roots = [root]\n"
            "if os.environ.get('KODI_LAB_TEST_SNAPSHOT_SCRATCH_ROOT'):\n"
            "    roots.append(Path(os.environ[\n"
            "        'KODI_LAB_TEST_SNAPSHOT_SCRATCH_ROOT']))\n"
            "for folder in roots:\n"
            "    folder.mkdir(mode=0o700, exist_ok=True)\n"
            "    child = folder / 'readonly'\n"
            "    child.mkdir()\n"
            "    os.link(os.environ['CHECK_TEST_SENTINEL'], child / 'hardlink')\n"
            "    outside = Path(os.environ['CHECK_TEST_SENTINEL']).parent\n"
            "    (child / 'link').symlink_to(outside)\n"
            "    child.chmod(0o500)\n"
            "if '--junitxml' in args:\n"
            "    trace = Path(args[args.index('--junitxml') + 1])\n"
            "    trace.write_text('retained failure trace')\n"
            "PYTEST_SCRATCH_FIXTURE\n"
            "    fi\n"
            f"    exit {STOP_BEFORE_PYTEST}\n"
            "fi\n"
            "exit 0\n"
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
        "KODI_LAB_CHECK_EVIDENCE_PARENT": str(evidence),
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
    assert len(calls) == 6
    assert calls[-1].startswith("run --locked pytest --basetemp ")
    assert not any(
        option in calls[-1].split() for option in ("-k", "-m", "--lf", "--last-failed")
    )
    assert not any(argument.startswith("tests/") for argument in calls[-1].split())
    assert "--junitxml" in calls[-1] and "--harness-report" in calls[-1]
    assert calls[1].startswith("run --locked ruff format")
    assert calls[2].startswith("run --locked ruff check")
    assert calls[3].startswith("run --locked mypy")
    assert "test_foundation_registry_contains_exact_operation_set" in calls[4]
    assert "test_migration_metadata_contains_core_storage_tables" in calls[4]
    assert "tests/runtime/test_ostree_installation_schema.py" in calls[4]


@pytest.mark.parametrize("stage,status", [("format", 9), ("contracts", 10)])
def test_cheap_failure_prevents_full_suite(
    check_harness: CheckHarness, stage: str, status: int
) -> None:
    check_harness.environment["CHECK_TEST_FAIL_STAGE"] = stage
    result = check_harness.run()
    assert result.returncode == status
    calls = check_harness.uv_log.read_text().splitlines()
    assert not any(call.startswith("run --locked pytest --basetemp ") for call in calls)
    assert "All foundation checks passed" not in result.stdout
    stages = list((check_harness.root / "evidence").glob("*/stages.tsv"))
    assert len(stages) == 1
    assert stages[0].read_text().splitlines()[-1].endswith(f"\t{status}")


def test_low_tmpfs_capacity_uses_disk_fallback(check_harness: CheckHarness) -> None:
    check_harness.environment["CHECK_TEST_SHM_FREE_BYTES"] = str(2 * 1024**3)
    result = check_harness.run()
    assert result.returncode == STOP_BEFORE_PYTEST
    assert f"pytest temp root: {check_harness.root}/ordinary/klp." in result.stdout


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


def test_full_gate_checks_test_headroom_before_running_stages(
    check_harness: CheckHarness,
) -> None:
    parent = check_harness.root / "parent"
    parent.mkdir()
    check_harness.environment["CHECK_TEST_FREE_BYTES"] = str(MIN_FREE_BYTES)
    result = check_harness.run(str(parent))
    assert result.returncode == 1
    assert "production reserve plus test headroom" in result.stderr
    assert len(check_harness.uv_log.read_text().splitlines()) == 1
    assert list(parent.iterdir()) == []


@pytest.mark.parametrize("script", ["check", "check-fast", "check-smoke"])
def test_final_wrapper_cleanup_preserves_external_links_and_failure_trace(
    check_harness: CheckHarness, script: str
) -> None:
    caller_snapshot = check_harness.root / "caller-snapshot"
    caller_snapshot.mkdir(mode=0o700)
    caller_data = caller_snapshot / "keep"
    caller_data.write_bytes(b"caller data")
    caller_data.chmod(0o400)
    check_harness.environment["KODI_LAB_TEST_SNAPSHOT_SCRATCH_ROOT"] = str(
        caller_snapshot
    )
    sentinel = check_harness.root / "protected"
    sentinel.write_bytes(b"unchanged")
    sentinel.chmod(0o400)
    check_harness.environment["CHECK_TEST_SENTINEL"] = str(sentinel)
    check_harness.environment["CHECK_TEST_FILESYSTEM"] = "ext4"
    result = check_harness.run(
        str(check_harness.root / "ordinary"), script=CHECK.parent / script
    )
    assert result.returncode == STOP_BEFORE_PYTEST, result.stdout + result.stderr
    assert list(caller_snapshot.iterdir()) == [caller_data]
    assert caller_data.read_bytes() == b"caller data"
    assert caller_data.stat().st_mode & 0o777 == 0o400
    assert sentinel.read_bytes() == b"unchanged"
    assert sentinel.stat().st_mode & 0o777 == 0o400
    assert list((check_harness.root / "ordinary").iterdir()) == []
    assert list((check_harness.root / "snapshots").iterdir()) == []
    if script == "check":
        traces = list((check_harness.root / "evidence").glob("*/pytest.xml"))
        assert len(traces) == 1
        assert traces[0].read_text() == "retained failure trace"
