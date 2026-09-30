"""A reviewed compatibility repair for the one legacy development pointer inode."""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from kodi_lab.host_bootstrap import HostBootstrapError
from kodi_lab.repository.legacy_pointer_repair import (
    repair_legacy_development_current,
)
from kodi_lab.repository.stable import sha256
from kodi_lab.repository.transition import (
    _identity,
    _verify_development_metadata,
    transition,
)
from tests.repository.test_candidate_transition import (
    _replacement,
    _revision_identity,
    _web_owned_transition_fixture,
)
from tests.repository.test_candidate_transition import (
    migrations as migrations,
)
from tests.repository.test_development import BASE


@dataclass
class Legacy:
    installed: Path
    admin: Path
    data: Path
    state: Path
    installed_hash: str
    admin_hash: str
    pointer_bytes: bytes

    def repair(self) -> dict[str, object]:
        return repair_legacy_development_current(
            self.installed,
            self.admin,
            self.data,
            self.state,
            BASE,
            self.installed_hash,
            self.admin_hash,
        )


@pytest.fixture
def legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Legacy:
    if os.geteuid() != 0:
        pytest.skip("requires root-owned legacy inode")
    old, installed, data, state, old_hash, installed_hash, backup_id = (
        _web_owned_transition_fixture(
            tmp_path, monkeypatch, old_revision="0056_repository_workspace_deployment"
        )
    )
    _revision_identity(installed, "0056_repository_workspace_deployment")
    installed_hash = sha256((installed / "BUNDLE.json").read_bytes())
    # The source test wheel is at 0057; this fixture models the retained A
    # package, whose own reviewed installed head was 0056.
    with monkeypatch.context() as patch:
        patch.setattr(
            "kodi_lab.repository.transition._verify_migration_relationship",
            lambda *_args: None,
        )
        transition(
            old, installed, data, state, BASE, old_hash, installed_hash, backup_id
        )
    admin = _replacement(installed, tmp_path / "admin", source_commit="e" * 40)
    _revision_identity(admin, "0057_development_capability_amendments")
    admin_hash = sha256((admin / "BUNDLE.json").read_bytes())
    pointer = data / "development-data" / "CURRENT"
    before = pointer.read_bytes()
    os.chown(pointer, 0, 0)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", installed_hash)
    from kodi_lab.repository import legacy_pointer_repair

    monkeypatch.setattr(legacy_pointer_repair, "LEGACY_BUNDLE_SHA256", installed_hash)
    return Legacy(installed, admin, data, state, installed_hash, admin_hash, before)


def test_legacy_root_pointer_repair_allows_ordinary_backup(legacy: Legacy) -> None:
    from kodi_lab import remote_lifecycle

    pointer = legacy.data / "development-data" / "CURRENT"
    db = legacy.state / "database/kodi-lab.db"
    db_hash = sha256(db.read_bytes())
    old_stable = (legacy.data / "CURRENT").read_bytes()
    old_ack = (legacy.data / "development-data/ACK").read_bytes()
    old_provenance = (
        legacy.data
        / "development-data/generations"
        / legacy.pointer_bytes.decode().strip()
        / "PROVENANCE.json"
    ).read_bytes()
    with _identity(10001, 10001), remote_lifecycle.lifecycle_lock(legacy.state):  # noqa: SIM117
        with pytest.raises(Exception, match="CURRENT"):
            remote_lifecycle.backup(legacy.state, f"sqlite:///{db}")
    receipt = legacy.repair()
    assert receipt["status"] == "completed"
    assert receipt["path"] == "development-data/CURRENT"
    assert receipt["prior_owner"] == [0, 0]
    assert receipt["result_owner"] == [10001, 10001]
    assert receipt["mode"] == 0o644
    assert pointer.read_bytes() == legacy.pointer_bytes
    assert (pointer.stat().st_uid, pointer.stat().st_gid) == (10001, 10001)
    assert sha256(db.read_bytes()) == db_hash
    assert (legacy.data / "CURRENT").read_bytes() == old_stable
    assert (legacy.data / "development-data/ACK").read_bytes() == old_ack
    assert (
        legacy.data
        / "development-data/generations"
        / legacy.pointer_bytes.decode().strip()
        / "PROVENANCE.json"
    ).read_bytes() == old_provenance
    receipt_bytes = (legacy.data / "DEVELOPMENT_CURRENT_OWNER_REPAIR.json").read_bytes()
    assert legacy.repair() == receipt
    assert (
        legacy.data / "DEVELOPMENT_CURRENT_OWNER_REPAIR.json"
    ).read_bytes() == receipt_bytes
    with _identity(10001, 10001), remote_lifecycle.lifecycle_lock(legacy.state):
        new_backup = remote_lifecycle.backup(legacy.state, f"sqlite:///{db}")
        assert (
            remote_lifecycle._verify_backup(legacy.state, new_backup)["complete"]
            is True
        )


@pytest.mark.parametrize(
    "damage",
    [
        "symlink_pointer",
        "wrong_mode",
        "invalid_content",
        "missing_generation",
        "tampered_generation",
        "wrong_provenance",
        "unexpected_owner",
        "unsafe_ack",
        "unsafe_parent",
        "unexpected_file",
    ],
)
def test_legacy_repair_fails_closed_on_other_state(legacy: Legacy, damage: str) -> None:
    pointer = legacy.data / "development-data/CURRENT"
    development = legacy.data / "development-data"
    if damage == "symlink_pointer":
        pointer.unlink()
        pointer.symlink_to("ACK")
    elif damage == "wrong_mode":
        pointer.chmod(0o666)
    elif damage == "invalid_content":
        pointer.write_text("invalid\n")
    elif damage == "missing_generation":
        pointer.write_text("f" * 64 + "\n")
        (development / "ACK").write_text("f" * 64 + "\n")
    elif damage == "tampered_generation":
        (
            development
            / "generations"
            / legacy.pointer_bytes.decode().strip()
            / "PROVENANCE.json"
        ).write_text("{}\n")
    elif damage == "wrong_provenance":
        (
            legacy.data
            / "generations"
            / (legacy.data / "CURRENT").read_text().strip()
            / "PROVENANCE.json"
        ).write_text("{}\n")
    elif damage == "unexpected_owner":
        os.chown(pointer, 1000, 1000)
    elif damage == "unsafe_ack":
        os.chown(development / "ACK", 0, 0)
    elif damage == "unsafe_parent":
        os.chown(development, 0, 0)
    elif damage == "unexpected_file":
        (development / "unreviewed").write_text("extra\n")
        os.chown(development / "unreviewed", 10001, 10001)
    original = pointer.lstat()
    with pytest.raises((ValueError, OSError, KeyError, HostBootstrapError)):
        legacy.repair()
    assert pointer.lstat().st_ino == original.st_ino
    assert (pointer.lstat().st_uid, pointer.lstat().st_gid) == (
        original.st_uid,
        original.st_gid,
    )
    assert not (legacy.data / "DEVELOPMENT_CURRENT_OWNER_REPAIR.json").exists()


def test_legacy_repair_already_canonical_is_read_only(legacy: Legacy) -> None:
    pointer = legacy.data / "development-data/CURRENT"
    os.chown(pointer, 10001, 10001)
    before = pointer.stat()
    result = legacy.repair()
    after = pointer.stat()
    assert result["status"] == "already_canonical"
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert not (legacy.data / "DEVELOPMENT_CURRENT_OWNER_REPAIR.json").exists()


def test_legacy_repair_retries_after_owner_switch(
    legacy: Legacy, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.repository import legacy_pointer_repair

    actual = _verify_development_metadata
    calls = 0

    def interrupt(root: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated interruption after fchown")
        actual(root)

    with monkeypatch.context() as patch:
        patch.setattr(legacy_pointer_repair, "_verify_development_metadata", interrupt)
        with pytest.raises(RuntimeError, match="interruption"):
            legacy.repair()
    pointer = legacy.data / "development-data/CURRENT"
    assert (pointer.stat().st_uid, pointer.stat().st_gid) == (10001, 10001)
    pending = legacy.data / "DEVELOPMENT_CURRENT_OWNER_REPAIR.json"
    assert b'"status":"pending"' in pending.read_bytes()
    assert legacy.repair()["status"] == "completed"
    assert b'"status":"completed"' in pending.read_bytes()


def test_legacy_repair_rejects_other_bundle_and_stable_pointer(legacy: Legacy) -> None:
    stable = legacy.data / "CURRENT"
    before = (stable.stat().st_ino, stable.read_bytes())
    with pytest.raises(ValueError, match="reviewed legacy candidate"):
        repair_legacy_development_current(
            legacy.installed,
            legacy.admin,
            legacy.data,
            legacy.state,
            BASE,
            "f" * 64,
            legacy.admin_hash,
        )
    assert (stable.stat().st_ino, stable.read_bytes()) == before
    assert stat.S_IMODE(stable.stat().st_mode) == 0o644


def test_legacy_repair_rejects_wrong_admin_bundle(legacy: Legacy) -> None:
    pointer = legacy.data / "development-data/CURRENT"
    with pytest.raises(ValueError, match="candidate reviewed inputs differ"):
        repair_legacy_development_current(
            legacy.installed,
            legacy.admin,
            legacy.data,
            legacy.state,
            BASE,
            legacy.installed_hash,
            "f" * 64,
        )
    assert (pointer.stat().st_uid, pointer.stat().st_gid) == (0, 0)
    assert not (legacy.data / "DEVELOPMENT_CURRENT_OWNER_REPAIR.json").exists()


def test_packaged_repair_cli_rejects_arbitrary_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.repository.server import main

    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", "f" * 64)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", BASE)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "kodi-lab-repository",
            "repair-legacy-pointer",
            "--data-root",
            str(tmp_path),
            "--previous-bundle",
            "/previous",
            "--expected-old-bundle",
            "e68e66d271e8a36873f830526c53c1f253ef48afdda260e589a5e5eaaff41709",
        ],
    )
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 1
    assert tuple(tmp_path.iterdir()) == ()
