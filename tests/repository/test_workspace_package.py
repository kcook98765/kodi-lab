"""Exact-generation repository package bytes and bridge digest prediction."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from kodi_lab.development.deployment import capture_development_tree
from kodi_lab.repository.workspace_package import (
    bridge_manifest_for_zip,
    build_workspace_package,
)

ADDON_ID = "plugin.video.kodi-lab-repository-workspace-test"


def _source(root: Path) -> Path:
    source = root / "source"
    source.mkdir()
    (source / "addon.xml").write_text(
        f'<addon id="{ADDON_ID}" version="1.0.0" name="Synthetic" '
        'provider-name="Kodi Lab"><requires><import addon="xbmc.python" '
        'version="3.0.0"/></requires><extension point="xbmc.python.pluginsource" '
        'library="default.py"/></addon>',
        encoding="utf-8",
    )
    (source / "default.py").write_text(
        "import xbmcplugin\nxbmcplugin.endOfDirectory(1)\n", encoding="utf-8"
    )
    return source


def test_workspace_zip_is_deterministic_and_predicts_bridge_tree(
    tmp_path: Path,
) -> None:
    _source(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    manifest = capture_development_tree(tmp_path, "source", stage_root=stage)
    first = build_workspace_package(ADDON_ID, manifest, stage)
    second = build_workspace_package(ADDON_ID, manifest, stage)
    assert first == second
    assert first.installed_manifest_digest == manifest.digest
    assert (first.installed_manifest_digest, first.file_count, first.total_bytes) == (
        bridge_manifest_for_zip(first.content, ADDON_ID)
    )
    with zipfile.ZipFile(io.BytesIO(first.content)) as archive:
        assert f"{ADDON_ID}/addon.xml" in archive.namelist()
        assert f"{ADDON_ID}/default.py" in archive.namelist()


def test_workspace_zip_rejects_executable_source(tmp_path: Path) -> None:
    source = _source(tmp_path)
    (source / "default.py").chmod(0o755)
    stage = tmp_path / "stage"
    stage.mkdir()
    manifest = capture_development_tree(tmp_path, "source", stage_root=stage)
    with pytest.raises(ValueError, match="non-executable"):
        build_workspace_package(ADDON_ID, manifest, stage)
