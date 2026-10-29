"""Scratch diagnostics advise without changing explicit selections."""

import os
from pathlib import Path
from typing import Any

import pytest

from scripts import check_environment


@pytest.mark.parametrize(
    "kind,free,warning",
    [
        ("ext2/ext3", 4 * 1024**3, True),
        ("tmpfs", 4 * 1024**3, False),
        ("ext2/ext3", 2 * 1024**3, False),
    ],
)
def test_disk_warning_respects_tmpfs_capacity(
    monkeypatch: pytest.MonkeyPatch, kind: str, free: int, warning: bool
) -> None:
    selected = Path("/selected")

    def filesystem(path: Path) -> dict[str, Any]:
        return {"type": kind if path == selected else "tmpfs", "free_bytes": free}

    monkeypatch.setattr(check_environment, "filesystem", filesystem)
    monkeypatch.setattr(os, "access", lambda *_: True)
    monkeypatch.setenv("KODI_LAB_CHECK_PYTEST_TMP_PARENT", str(selected))
    result = check_environment.environment(selected)
    assert result["disk_with_suitable_tmpfs"] is warning
    assert result["explicit_pytest_parent"] is True
    assert result["selected_filesystem"]["type"] == kind
