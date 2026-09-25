from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from kodi_lab.build_identity import package_sha256
from kodi_lab.host_bootstrap import (
    HostBootstrapError,
    explicit_data_root,
    generate_master_key,
    initialize_state,
)
from kodi_lab.storage.secrets import decode_master_key


def test_master_key_is_private_valid_never_overwritten_or_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "private"
    monkeypatch.delenv("KODI_LAB_EXTERNAL_TARGET_MASTER_KEY", raising=False)
    initialize_state(root)
    path = generate_master_key(root)
    key = path.read_text().strip()
    assert len(decode_master_key(key) or b"") == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert key not in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        generate_master_key(root)
    assert path.read_text().strip() == key
    monkeypatch.setenv("KODI_LAB_EXTERNAL_TARGET_MASTER_KEY", key)
    with pytest.raises(HostBootstrapError, match="already configured"):
        generate_master_key(root)


def test_bootstrap_requires_explicit_normalized_private_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KODI_LAB_DATA_ROOT", raising=False)
    with pytest.raises(HostBootstrapError, match="KODI_LAB_DATA_ROOT"):
        explicit_data_root()
    root = tmp_path / "private"
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(root))
    assert explicit_data_root() == root
    initialize_state(root)
    for path in (root, root / "database", root / "secrets"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert path.stat().st_uid == os.getuid()
    symlink = tmp_path / "alias"
    symlink.symlink_to(root, target_is_directory=True)
    with pytest.raises(HostBootstrapError, match="unsafe"):
        initialize_state(symlink)


def test_build_identity_changes_with_runtime_bytes(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("value = 1\n")
    initial = package_sha256(tmp_path)
    (tmp_path / "module.py").write_text("value = 2\n")
    assert package_sha256(tmp_path) != initial
