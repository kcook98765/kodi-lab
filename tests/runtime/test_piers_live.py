"""Opt-in real assembled RC1 container acceptance; no production install."""

import os
from pathlib import Path

import pytest

from tests.runtime.piers_acceptance import run_acceptance


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("KODI_LAB_TEST_PHASE3_DIRECTORY") is None,
    reason="explicit isolated Phase 3 assembly/acceptance directory required",
)
def test_real_assembled_rc1_runtime_acceptance() -> None:
    root = Path(os.environ["KODI_LAB_TEST_PHASE3_DIRECTORY"])
    report = run_acceptance(root)
    assert report["checks"] and all(report["checks"].values())
    assert len(report["cycles"]) == 3
    assert report["native_addon"]["mapped"] and report["native_addon"]["activated"]
