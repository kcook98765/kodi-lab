"""Private project seeds survive the supported application rollback inventory."""

import base64
import hashlib
import os
import shutil
import stat
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from kodi_lab.application.agent_projects import _seed_source
from kodi_lab.application.project_transfers import (
    ProjectTransferInput,
    read_source_transfer,
    transfer_source,
)
from kodi_lab.host_bootstrap import HostBootstrapError
from kodi_lab.remote_lifecycle import (
    _files,
    _verify_backup,
    backup,
    initialize,
    lifecycle_lock,
    recover_clean,
)
from tests.storage.test_remote_lifecycle import (
    repository_migrations as repository_migrations,
)

PROJECT = UUID("b43d2670-48fe-41ef-9553-d37a4019f952")


def url(root: Path) -> str:
    return "sqlite:///" + str(root / "database/kodi-lab.db")


def seed(root: Path) -> Path:
    _seed_source(
        root, PROJECT, {"addon.xml": b"inert", "resources/window.xml": b"window"}
    )
    return root / "agent-projects/sources" / str(PROJECT)


def test_sealed_private_seed_backup_and_clean_recovery(tmp_path: Path) -> None:
    root = tmp_path / "state"
    initialize(root, url(root))
    source = seed(root)
    transfer_id = UUID("55fe6010-9786-4707-b286-608607d54a43")
    payload = b"source transfer recovery"
    digest = hashlib.sha256(payload).hexdigest()
    transfer_source(
        root,
        PROJECT,
        ProjectTransferInput(
            request_id=transfer_id,
            archive_size=len(payload),
            archive_sha256=digest,
            chunk_base64=base64.b64encode(payload).decode(),
        ),
        authorize=lambda: None,
    )
    with lifecycle_lock(root):
        identifier = backup(root, url(root))
    manifest = cast(dict[str, Any], _verify_backup(root, identifier))
    prefix = "agent-projects/sources/" + str(PROJECT)
    assert manifest["files"][prefix + "/addon.xml"]["mode"] == 0o400
    assert manifest["files"]["agent-projects/" + str(PROJECT) + ".lock"]["size"] == 0
    assert manifest["directory_modes"][prefix + "/resources"] == 0o500
    restored = tmp_path / "recovered"
    restored.mkdir(mode=0o700)
    (restored / "backups").mkdir(mode=0o700)
    shutil.copytree(root / "backups" / identifier, restored / "backups" / identifier)
    recover_clean(restored, url(restored), identifier, identifier)
    recovered = restored / "agent-projects/sources" / str(PROJECT)
    assert (recovered / "addon.xml").read_bytes() == (source / "addon.xml").read_bytes()
    assert (recovered / "resources/window.xml").read_bytes() == b"window"
    assert stat.S_IMODE(recovered.stat().st_mode) == 0o500
    assert stat.S_IMODE((recovered / "addon.xml").stat().st_mode) == 0o400
    # Intake itself must accept the restored immutable seed without replacing it.
    assert seed(restored) == recovered
    assert (
        read_source_transfer(
            restored, PROJECT, transfer_id, digest, authorize=lambda: None
        )
        == payload
    )


@pytest.mark.parametrize(
    "defect", ["symlink", "hardlink", "public", "executable", "lock_bytes"]
)
def test_unsafe_private_seed_is_never_adopted_into_backup(
    tmp_path: Path, defect: str
) -> None:
    root = tmp_path / "state"
    initialize(root, url(root))
    source = seed(root)
    target = source / "addon.xml"
    if defect == "symlink":
        os.chmod(source, 0o700)
        target.unlink()
        target.symlink_to(tmp_path / "outside")
    elif defect == "hardlink":
        os.link(target, tmp_path / "alias")
    elif defect == "public":
        os.chmod(target, 0o644)
    elif defect == "executable":
        os.chmod(target, 0o500)
    else:
        (root / "agent-projects" / (str(PROJECT) + ".lock")).write_bytes(b"unknown")
    with pytest.raises(HostBootstrapError):
        _files(root)


def test_interrupted_private_seed_bytes_are_preserved_without_publication(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    initialize(root, url(root))
    source = seed(root)
    os.chmod(source, 0o700)
    os.chmod(source / "addon.xml", 0o600)
    (source / "addon.xml").write_bytes(b"incomplete")
    with lifecycle_lock(root):
        identifier = backup(root, url(root))
    retained = root / "backups" / identifier / "agent-projects/sources" / str(PROJECT)
    assert (retained / "addon.xml").read_bytes() == b"incomplete"
    assert stat.S_IMODE(retained.stat().st_mode) == 0o700
