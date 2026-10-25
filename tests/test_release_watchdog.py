"""Exercise wrapper timeout arguments/owned cleanup with costly stages stubbed.

These unit probes never execute pytest, lint, build or release acceptance stages.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from kodi_lab.snapshot.storage import MIN_FREE_BYTES

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "selected,expected", [(None, "2400s"), ("17", "17s"), ("0", None)]
)
def test_release_watchdog_arguments_and_run_owned_cleanup(
    tmp_path: Path, selected: str | None, expected: str | None
) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    preserved = scratch / "unrelated"
    preserved.write_text("preserved")
    calls = tmp_path / "timeout.json"
    uv = binaries / "uv"
    uv.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if any('from kodi_lab.snapshot.storage import MIN_FREE_BYTES' in arg "
        "for arg in sys.argv):\n"
        f"    print({MIN_FREE_BYTES})\n"
    )
    timeout = binaries / "timeout"
    timeout.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['PROGRAM54_TIMEOUT_LOG']).write_text("
        "json.dumps(sys.argv[1:]))\n"
    )
    uv.chmod(0o700)
    timeout.chmod(0o700)
    environment = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "PROGRAM54_TIMEOUT_LOG": str(calls),
        "KODI_LAB_CHECK_PYTEST_TMP_PARENT": str(scratch),
        "KODI_LAB_TEST_SNAPSHOT_SCRATCH_PARENT": str(scratch),
        "KODI_LAB_CHECK_EVIDENCE_PARENT": str(scratch),
    }
    environment.pop("KODI_LAB_CHECK_PYTEST_TIMEOUT_SECONDS", None)
    if selected is not None:
        environment["KODI_LAB_CHECK_PYTEST_TIMEOUT_SECONDS"] = selected
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/check")],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if expected is None:
        assert result.returncode == 1
        assert "must be a positive integer" in result.stderr
        assert not calls.exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        arguments = json.loads(calls.read_text())
        assert arguments[:4] == [
            "--foreground",
            "--signal=TERM",
            "--kill-after=30s",
            expected,
        ]
        assert arguments[4:8] == ["uv", "run", "--locked", "pytest"]
        assert "--basetemp" in arguments
    assert preserved.read_text() == "preserved"
    assert all(
        path == preserved or path.name.startswith("kodi-lab-check-results.")
        for path in scratch.iterdir()
    )
