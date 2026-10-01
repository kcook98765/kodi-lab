"""Shared Linux source confinement used by Web and broker captures."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.development.confined_fs import (
    open_development_root,
    open_source_component,
)
from kodi_lab.development.deployment import capture_development_tree


def test_valid_source_uses_same_confinement_for_registration_and_capture(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Source Trees"
    source = root / "example"
    source.mkdir(parents=True)
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.example" name="Example" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="main.py">'
        "<provides>video</provides></extension></addon>"
    )
    (source / "main.py").write_text("print('safe')\n")
    assert DevelopmentSourceValidator(root).validate("example").addon_id == (
        "plugin.video.example"
    )
    assert "main.py" in capture_development_tree(root, "example").by_path()


@pytest.mark.skipif(not os.path.ismount("/proc"), reason="/proc mount unavailable")
def test_openat2_rejects_real_nested_mount_crossing() -> None:
    root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError) as captured:
            open_source_component(root_fd, "proc", os.O_RDONLY | os.O_DIRECTORY)
        assert captured.value.errno == errno.EXDEV
    finally:
        os.close(root_fd)


@pytest.mark.parametrize("component", ("", ".", "..", "/etc", "a/b", "a\\b"))
def test_source_component_rejects_unsafe_path(component: str, tmp_path: Path) -> None:
    root_fd = open_development_root(tmp_path)
    try:
        with pytest.raises(OSError) as captured:
            open_source_component(root_fd, component, os.O_RDONLY)
        assert captured.value.errno == errno.EINVAL
    finally:
        os.close(root_fd)


def test_unsupported_openat2_platform_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("kodi_lab.development.confined_fs._SUPPORTED", False)
    root_fd = open_development_root(tmp_path)
    try:
        with pytest.raises(OSError) as captured:
            open_source_component(root_fd, "child", os.O_RDONLY)
        assert captured.value.errno == errno.ENOSYS
    finally:
        os.close(root_fd)
