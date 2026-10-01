"""Broker source root requires the fixed read-only deployment mount."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from kodi_lab.broker.development_sources import (
    DevelopmentSourceDeploymentStateError,
    require_readonly_source_mount,
)
from kodi_lab.broker.operations import CopySyncAttachedDevelopmentSourceParams


def _mountinfo(path: Path, *, mode: str) -> str:
    return f"42 1 8:2 / {path} {mode},relatime - ext4 /dev/sda2 rw\n"


def test_fixed_readonly_mount_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "mounted-source"
    root.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(_mountinfo(root, mode="ro"))
    require_readonly_source_mount(root, expected_root=root, mountinfo=mountinfo)


@pytest.mark.parametrize("mode", ("rw", "missing", "duplicate"))
def test_writable_missing_or_ambiguous_mount_fails_closed(
    tmp_path: Path, mode: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        ""
        if mode == "missing"
        else _mountinfo(root, mode="ro") * 2
        if mode == "duplicate"
        else _mountinfo(root, mode="rw")
    )
    with pytest.raises(DevelopmentSourceDeploymentStateError):
        require_readonly_source_mount(root, expected_root=root, mountinfo=mountinfo)


def test_absent_or_wrong_root_fails_before_mount_lookup(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    for root in (None, tmp_path / "wrong"):
        with pytest.raises(DevelopmentSourceDeploymentStateError):
            require_readonly_source_mount(
                root, expected_root=tmp_path / "expected", mountinfo=mountinfo
            )


def test_broker_request_cannot_select_host_root() -> None:
    identity = str(UUID(int=1))
    with pytest.raises(ValidationError):
        CopySyncAttachedDevelopmentSourceParams.model_validate(
            {
                "managed_instance_id": identity,
                "development_source_id": identity,
                "deployment_operation_id": identity,
                "expected_container_id": "a" * 64,
                "development_root": "/etc",
            }
        )
