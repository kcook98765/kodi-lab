"""Reviewed candidate transitions preserve both repository and application evidence."""

from __future__ import annotations

import importlib
import io
import json
import os
import shutil
import sqlite3
import stat
import zipfile
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config

from kodi_lab import remote_lifecycle
from kodi_lab.host_bootstrap import HostBootstrapError
from kodi_lab.remote_lifecycle import (
    _verify_backup,
    backup,
    initialize,
    lifecycle_lock,
    restore,
    upgrade,
)
from kodi_lab.repository.development import DevelopmentRepositoryService
from kodi_lab.repository.development import initialize as initialize_development
from kodi_lab.repository.development import load_current as load_development_current
from kodi_lab.repository.stable import (
    canonical,
    generate,
    load_current,
    reviewed_bridge_from_bundle,
    sha256,
)
from kodi_lab.repository.transition import _identity, transition
from kodi_lab.storage import packaged_migrations
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    ExternalBridgePairingRow,
    ExternalDevelopmentCapabilityAmendmentRow,
    ExternalKodiTargetRow,
    RepositoryWorkspaceDeploymentRow,
    TargetRow,
)
from tests.application.test_mcp_workspace_development import EXTERNAL_TARGET_ID
from tests.application.test_repository_workspace_deployment import (
    _approve as approve_workspace_publication,
)
from tests.application.test_repository_workspace_deployment import (
    _setup as repository_workspace_setup,
)
from tests.application.test_repository_workspace_deployment import (
    _version as workspace_source_version,
)
from tests.repository.test_development import (
    BASE,
    current_migration_head,
    fixture_zip,
    phase2_candidate,
)

CURRENT_HEAD = current_migration_head()
HISTORICAL_SHA256 = "665b15cb6597f6a8e40b3cbdfc29606f453f567d49b6877a1ca2ee704c90f5f3"


@pytest.fixture(autouse=True)
def migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    @contextmanager
    def source_config(database_url: str) -> Iterator[Config]:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).parents[2] / "migrations")
        )
        config.set_main_option("sqlalchemy.url", database_url)
        yield config

    monkeypatch.setattr(packaged_migrations, "packaged_config", source_config)
    monkeypatch.setattr(remote_lifecycle, "packaged_config", source_config)


def _replacement(
    old: Path,
    target: Path,
    *,
    source_commit: str = "b" * 40,
    source_tree: str = "d" * 40,
) -> Path:
    shutil.copytree(old, target)
    provenance = json.loads((target / "bridge/BRIDGE.json").read_bytes())
    provenance["source_commit"] = source_commit
    provenance["source_tree"] = source_tree
    (target / "bridge/BRIDGE.json").write_bytes(canonical(provenance))
    manifest = json.loads((target / "BUNDLE.json").read_bytes())
    manifest["source_commit"] = provenance["source_commit"]
    manifest["source_tree"] = provenance["source_tree"]
    body = (target / "bridge/BRIDGE.json").read_bytes()
    manifest["files"]["bridge/BRIDGE.json"] = {
        "sha256": "sha256:" + sha256(body),
        "size": len(body),
    }
    (target / "BUNDLE.json").write_bytes(canonical(manifest))
    return target


def _revision_identity(bundle: Path, revision: str) -> None:
    manifest = json.loads((bundle / "BUNDLE.json").read_bytes())
    manifest["repository"] = {"migration_head": revision}
    (bundle / "BUNDLE.json").write_bytes(canonical(manifest))


def _historical_bridge(bundle: Path) -> None:
    """Give a disposable candidate the exact reviewed 1.1.13 Bridge inputs."""
    fixtures = Path(__file__).parents[1] / "fixtures"
    historical_zip = (fixtures / "reviewed-bridge-1.1.13.zip").read_bytes()
    assert sha256(historical_zip) == HISTORICAL_SHA256
    historical_provenance = (fixtures / "reviewed-bridge-1.1.13.json").read_bytes()
    historical_stable = (fixtures / "reviewed-stable-1.1.13.json").read_bytes()
    current_zip = bundle / "bridge/service.kodi_lab_bridge-1.1.17.zip"
    current_zip.unlink()
    old_zip = bundle / "bridge/service.kodi_lab_bridge-1.1.13.zip"
    old_zip.write_bytes(historical_zip)
    (bundle / "bridge/BRIDGE.json").write_bytes(historical_provenance)
    (bundle / "repository/STABLE.json").write_bytes(historical_stable)
    manifest = json.loads((bundle / "BUNDLE.json").read_bytes())
    provenance = json.loads(historical_provenance)
    manifest["source_commit"] = provenance["source_commit"]
    manifest["source_tree"] = provenance["source_tree"]
    manifest["bridge"]["zip"] = "bridge/service.kodi_lab_bridge-1.1.13.zip"
    del manifest["files"]["bridge/service.kodi_lab_bridge-1.1.17.zip"]
    for name, content in (
        ("bridge/service.kodi_lab_bridge-1.1.13.zip", historical_zip),
        ("bridge/BRIDGE.json", historical_provenance),
        ("repository/STABLE.json", historical_stable),
    ):
        manifest["files"][name] = {
            "sha256": "sha256:" + sha256(content),
            "size": len(content),
        }
    (bundle / "BUNDLE.json").write_bytes(canonical(manifest))


def _cross_version_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path, str, str, str]:
    old = phase2_candidate(tmp_path)
    _historical_bridge(old)
    new = phase2_candidate(tmp_path / "replacement")
    data = tmp_path / "repository-data"
    old_hash = sha256((old / "BUNDLE.json").read_bytes())
    old_review = reviewed_bridge_from_bundle(old, old_hash)
    generate(old, data, BASE, reviewed_bridge=old_review)
    initialize_development(data / "development-data", BASE, old)
    state = tmp_path / "state"
    url = f"sqlite:///{state / 'database/kodi-lab.db'}"
    initialize(state, url)
    assert remote_lifecycle._revision(state / remote_lifecycle.DATABASE) == CURRENT_HEAD
    engine = create_database_engine(url)
    DevelopmentRepositoryService(create_session_factory(engine), data, BASE).reconcile()
    engine.dispose()
    new_hash = sha256((new / "BUNDLE.json").read_bytes())
    monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(data))
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", BASE)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", old_hash)
    # A live pre-transition backup runs under the retained old image, whose
    # ordinary repository reader is pinned to this historical Bridge.
    with monkeypatch.context() as old_image:
        old_image.setattr(
            remote_lifecycle,
            "load_stable_current",
            lambda root, **kwargs: load_current(
                root, reviewed_bridge=old_review, **kwargs
            ),
        )
        with lifecycle_lock(state):
            backup_id = backup(state, url)
    return old, new, data, state, old_hash, new_hash, backup_id


def test_reviewed_bridge_113_to_115_and_reverse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _cross_version_setup(
        tmp_path, monkeypatch
    )
    before = load_current(
        data, reviewed_bridge=reviewed_bridge_from_bundle(old, old_hash)
    )[1]
    forward = transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert forward["bridge_zip_sha256"] == sha256(
        (new / "bridge/service.kodi_lab_bridge-1.1.17.zip").read_bytes()
    )
    packages = cast(
        list[dict[str, str]],
        load_current(data, expected_bundle_sha256=new_hash)[1]["packages"],
    )
    assert packages[1]["version"] == "1.1.17"
    reverse = transition(
        new, old, data, state, BASE, new_hash, old_hash, backup_id, rollback=True
    )
    assert reverse["bridge_zip_sha256"] == HISTORICAL_SHA256
    assert (
        load_current(
            data,
            expected_bundle_sha256=old_hash,
            reviewed_bridge=reviewed_bridge_from_bundle(old, old_hash),
        )[1]
        == before
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "old_stable",
        "new_stable",
        "old_provenance",
        "new_provenance",
        "old_manifest",
        "new_manifest",
        "old_zip",
        "missing_old_provenance",
    ],
)
def test_cross_version_candidate_tampering_fails_before_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _cross_version_setup(
        tmp_path, monkeypatch
    )
    before_pointer = (data / "CURRENT").read_bytes()
    side, component = tamper.removeprefix("missing_").split("_", 1)
    bundle = old if side == "old" else new
    version = "1.1.13" if side == "old" else "1.1.17"
    name = {
        "stable": "repository/STABLE.json",
        "provenance": "bridge/BRIDGE.json",
        "manifest": "BUNDLE.json",
        "zip": f"bridge/service.kodi_lab_bridge-{version}.zip",
    }[component]
    path = bundle / name
    if tamper == "missing_old_provenance":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises((ValueError, OSError)):
        transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert (data / "CURRENT").read_bytes() == before_pointer
    assert not (data / "TRANSITION.json").exists()


def test_self_consistent_uninstalled_candidate_cannot_replace_active_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _old, new, data, state, _old_hash, new_hash, backup_id = _cross_version_setup(
        tmp_path, monkeypatch
    )
    rogue = _replacement(new, tmp_path / "rogue")
    rogue_hash = sha256((rogue / "BUNDLE.json").read_bytes())
    assert reviewed_bridge_from_bundle(rogue, rogue_hash)
    before_pointer = (data / "CURRENT").read_bytes()
    with pytest.raises(ValueError):
        transition(rogue, new, data, state, BASE, rogue_hash, new_hash, backup_id)
    assert (data / "CURRENT").read_bytes() == before_pointer
    assert not (data / "TRANSITION.json").exists()


def test_self_consistent_unreviewed_bridge_cannot_replace_current_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, _new_hash, backup_id = _cross_version_setup(
        tmp_path, monkeypatch
    )
    original_zip = new / "bridge/service.kodi_lab_bridge-1.1.17.zip"
    unreviewed_zip = new / "bridge/service.kodi_lab_bridge-1.1.16.zip"
    output = io.BytesIO()
    with (
        zipfile.ZipFile(original_zip) as source,
        zipfile.ZipFile(output, "w") as target,
    ):
        for member in source.infolist():
            content = source.read(member)
            if member.filename == "service.kodi_lab_bridge/addon.xml":
                assert b'version="1.1.17"' in content
                content = content.replace(b'version="1.1.17"', b'version="1.1.16"')
            target.writestr(member, content)
    original_zip.unlink()
    unreviewed_zip.write_bytes(output.getvalue())
    digest = sha256(output.getvalue())
    fingerprint = "e" * 64
    stable_path = new / "repository/STABLE.json"
    stable = json.loads(stable_path.read_bytes())
    stable.update(
        bridge_version="1.1.16",
        bridge_zip_sha256=digest,
        bridge_semantic_fingerprint=fingerprint,
    )
    stable_path.write_bytes(canonical(stable))
    provenance_path = new / "bridge/BRIDGE.json"
    provenance = json.loads(provenance_path.read_bytes())
    provenance.update(
        version="1.1.16",
        filename=unreviewed_zip.name,
        sha256=digest,
        semantic_fingerprint=fingerprint,
    )
    if "inventory" in provenance:
        with zipfile.ZipFile(unreviewed_zip) as archive:
            addon_xml = archive.read("service.kodi_lab_bridge/addon.xml")
        for item in provenance["inventory"]:
            if item["path"] == "addon.xml":
                item.update(sha256=sha256(addon_xml), size=len(addon_xml))
        provenance["inventory_sha256"] = sha256(canonical(provenance["inventory"]))
    provenance_path.write_bytes(canonical(provenance))
    manifest_path = new / "BUNDLE.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["bridge"]["zip"] = "bridge/service.kodi_lab_bridge-1.1.16.zip"
    del manifest["files"]["bridge/service.kodi_lab_bridge-1.1.17.zip"]
    for name in (
        "bridge/service.kodi_lab_bridge-1.1.16.zip",
        "bridge/BRIDGE.json",
        "repository/STABLE.json",
    ):
        content = (new / name).read_bytes()
        manifest["files"][name] = {
            "sha256": "sha256:" + sha256(content),
            "size": len(content),
        }
    manifest_path.write_bytes(canonical(manifest))
    unreviewed_hash = sha256(manifest_path.read_bytes())
    assert reviewed_bridge_from_bundle(new, unreviewed_hash).version == "1.1.16"
    before_pointer = (data / "CURRENT").read_bytes()
    with pytest.raises(
        ValueError, match="replacement candidate reviewed inputs differ"
    ):
        transition(old, new, data, state, BASE, old_hash, unreviewed_hash, backup_id)
    assert (data / "CURRENT").read_bytes() == before_pointer
    assert not (data / "TRANSITION.json").exists()


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    old_revision: str = CURRENT_HEAD,
    new_revision: str = CURRENT_HEAD,
    legacy_old: bool = False,
    live_revision: str | None = None,
) -> tuple[Path, Path, Path, Path, str, str, str]:
    old = phase2_candidate(tmp_path)
    _revision_identity(old, old_revision)
    if legacy_old:
        legacy = json.loads((old / "BUNDLE.json").read_bytes())
        legacy["repository"] = {
            "workspace_deployment_migration": old_revision,
        }
        (old / "BUNDLE.json").write_bytes(canonical(legacy))
    new = _replacement(old, tmp_path / "replacement")
    _revision_identity(new, new_revision)
    data = tmp_path / "repository-data"
    generate(old, data, BASE)
    initialize_development(data / "development-data", BASE, old)
    state = tmp_path / "state"
    url = f"sqlite:///{state / 'database/kodi-lab.db'}"
    initialize(state, url)
    assert remote_lifecycle._revision(state / remote_lifecycle.DATABASE) == CURRENT_HEAD
    initial_revision = live_revision or old_revision
    if initial_revision != CURRENT_HEAD:
        with packaged_migrations.packaged_config(url) as config:
            command.downgrade(config, initial_revision)
    assert (
        remote_lifecycle._revision(state / remote_lifecycle.DATABASE)
        == initial_revision
    )
    engine = create_database_engine(url)
    service = DevelopmentRepositoryService(create_session_factory(engine), data, BASE)
    service.reconcile()
    body = fixture_zip("1.0.0")
    item = service.prepare_import(body, ("matrix", "nexus", "omega"))
    service.approve(UUID(item.id), observed_sha256=sha256(body))
    engine.dispose()
    old_hash = sha256((old / "BUNDLE.json").read_bytes())
    new_hash = sha256((new / "BUNDLE.json").read_bytes())
    monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(data))
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", BASE)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", old_hash)
    with lifecycle_lock(state):
        backup_id = backup(state, url)
    return old, new, data, state, old_hash, new_hash, backup_id


def test_forward_migration_transition_before_application_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(
        tmp_path,
        monkeypatch,
        old_revision="0056_repository_workspace_deployment",
    )
    url = f"sqlite:///{state / 'database/kodi-lab.db'}"
    assert remote_lifecycle._revision(state / remote_lifecycle.DATABASE) == (
        "0056_repository_workspace_deployment"
    )
    receipt = transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert receipt["old_migration_revision"] == "0056_repository_workspace_deployment"
    assert receipt["new_migration_revision"] == CURRENT_HEAD
    assert remote_lifecycle._revision(state / remote_lifecycle.DATABASE) == (
        "0056_repository_workspace_deployment"
    )
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", new_hash)
    assert upgrade(state, url)[:2] == (
        "0056_repository_workspace_deployment",
        CURRENT_HEAD,
    )
    assert remote_lifecycle.validate(state, url, require_head=True) == (CURRENT_HEAD)
    assert remote_lifecycle.verify_candidate_transition_completion(state, url)
    assert (
        transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
        == receipt
    )


def _web_owned_transition_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    old_revision: str = "0056_repository_workspace_deployment",
) -> tuple[Path, Path, Path, Path, str, str, str]:
    old, new, data, state, old_hash, new_hash, _ = _setup(
        tmp_path,
        monkeypatch,
        old_revision=old_revision,
    )
    # The application UID must traverse pytest's root-owned scratch parents.
    tmp_path.parent.parent.chmod(0o755)
    tmp_path.parent.chmod(0o755)
    tmp_path.chmod(0o755)
    web_uid = web_gid = 10001

    def make_web_owned(root: Path) -> None:
        for parent, directories, files in os.walk(root, followlinks=False):
            for name in (*directories, *files):
                os.chown(Path(parent) / name, web_uid, web_gid, follow_symlinks=False)
        os.chown(root, web_uid, web_gid, follow_symlinks=False)

    make_web_owned(state)
    make_web_owned(data / "development-data")
    url = f"sqlite:///{state / 'database/kodi-lab.db'}"
    with _identity(web_uid, web_gid), lifecycle_lock(state):
        backup_id = backup(state, url)
        assert _verify_backup(state, backup_id)["revision"] == old_revision
    return old, new, data, state, old_hash, new_hash, backup_id


@pytest.mark.skipif(
    os.geteuid() != 0, reason="requires packaged root transition identity"
)
def test_packaged_transition_preserves_web_owned_repository_for_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root transition and Web backup must agree on real inode ownership."""
    old, new, data, state, old_hash, new_hash, backup_id = (
        _web_owned_transition_fixture(tmp_path, monkeypatch)
    )
    url = f"sqlite:///{state / 'database/kodi-lab.db'}"
    web_uid = web_gid = 10001
    previous_dev_inode = (data / "development-data/CURRENT").stat().st_ino
    previous_stable_inode = (data / "CURRENT").stat().st_ino

    receipt = transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert receipt["old_migration_revision"] == "0056_repository_workspace_deployment"
    assert receipt["new_migration_revision"] == CURRENT_HEAD
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", new_hash)
    with _identity(web_uid, web_gid):
        assert upgrade(state, url)[:2] == (
            "0056_repository_workspace_deployment",
            CURRENT_HEAD,
        )
        assert remote_lifecycle.verify_candidate_transition_completion(state, url)

    stable_pointer = data / "CURRENT"
    development = data / "development-data"
    assert (development / "CURRENT").stat().st_ino != previous_dev_inode
    assert stable_pointer.stat().st_ino != previous_stable_inode
    assert not tuple(development.glob(".CURRENT-*"))
    assert not tuple(data.glob(".CURRENT-*"))
    for pointer in (development / "CURRENT", development / "ACK"):
        info = pointer.lstat()
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (
            web_uid,
            web_gid,
            0o644,
        )
    stable_info = stable_pointer.lstat()
    assert (
        stable_info.st_uid,
        stable_info.st_gid,
        stat.S_IMODE(stable_info.st_mode),
    ) == (
        0,
        0,
        0o644,
    )
    new_generation = (
        development / "generations" / str(receipt["new_development_generation"])
    )
    for item in (new_generation, *new_generation.rglob("*")):
        info = item.lstat()
        assert (info.st_uid, info.st_gid) == (web_uid, web_gid)
        assert stat.S_IMODE(info.st_mode) == (0o555 if item.is_dir() else 0o444)
    stable_generation = data / "generations" / str(receipt["new_stable_generation"])
    for item in (stable_generation, *stable_generation.rglob("*")):
        info = item.lstat()
        assert (info.st_uid, info.st_gid) == (0, 0)
        assert stat.S_IMODE(info.st_mode) == (0o755 if item.is_dir() else 0o644)
    for item, mode in (
        (data / "transitions", 0o700),
        (data / "transitions" / f"{receipt['operation_id']}.json", 0o600),
        (data / "LAST_TRANSITION.json", 0o600),
    ):
        info = item.lstat()
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (0, 0, mode)

    transition(
        new, old, data, state, BASE, new_hash, old_hash, backup_id, rollback=True
    )
    for pointer in (development / "CURRENT", development / "ACK"):
        info = pointer.lstat()
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (
            web_uid,
            web_gid,
            0o644,
        )
    stable_info = stable_pointer.lstat()
    assert (
        stable_info.st_uid,
        stable_info.st_gid,
        stat.S_IMODE(stable_info.st_mode),
    ) == (
        0,
        0,
        0o644,
    )
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", old_hash)
    with _identity(web_uid, web_gid):
        assert restore(state, url, backup_id, backup_id)[0] == (
            "0056_repository_workspace_deployment"
        )
    assert (development / "CURRENT").stat().st_uid == web_uid
    assert stable_pointer.stat().st_uid == 0


@pytest.mark.skipif(
    os.geteuid() != 0, reason="requires packaged root transition identity"
)
@pytest.mark.parametrize("stage", ["stable", "development", "database", "receipt"])
def test_packaged_transition_retry_preserves_web_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = (
        _web_owned_transition_fixture(tmp_path, monkeypatch)
    )
    if stage == "receipt":
        module = importlib.import_module("kodi_lab.repository.transition")
        original_write = module._write

        def interrupt_last_pointer(path: Path, value: dict[str, object]) -> None:
            if path.name == "LAST_TRANSITION.json":
                raise RuntimeError("injected interruption after receipt")
            original_write(path, value)

        monkeypatch.setattr(module, "_write", interrupt_last_pointer)
        with pytest.raises(RuntimeError, match="injected interruption"):
            transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
        monkeypatch.setattr(module, "_write", original_write)
    else:
        with pytest.raises(RuntimeError, match="injected interruption"):
            transition(
                old,
                new,
                data,
                state,
                BASE,
                old_hash,
                new_hash,
                backup_id,
                interrupt_after=stage,
            )
    receipt = transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert receipt["new_migration_revision"] == CURRENT_HEAD
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", new_hash)
    with _identity(10001, 10001), lifecycle_lock(state):
        assert _verify_backup(
            state, backup(state, f"sqlite:///{state / 'database/kodi-lab.db'}")
        )
    for pointer in (data / "development-data/CURRENT", data / "development-data/ACK"):
        info = pointer.stat()
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (
            10001,
            10001,
            0o644,
        )


@pytest.mark.skipif(
    os.geteuid() != 0, reason="requires packaged root transition identity"
)
@pytest.mark.parametrize("defect", ["owner", "mode", "symlink"])
def test_packaged_transition_rejects_unsafe_development_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = (
        _web_owned_transition_fixture(tmp_path, monkeypatch)
    )
    pointer = data / "development-data/CURRENT"
    original = pointer.read_bytes()
    sentinel = tmp_path / "outside-current"
    sentinel.write_bytes(original)
    if defect == "owner":
        os.chown(pointer, 0, 0)
    elif defect == "mode":
        pointer.chmod(0o666)
    else:
        pointer.unlink()
        pointer.symlink_to(sentinel)
    if defect != "symlink":
        with (
            _identity(10001, 10001),
            lifecycle_lock(state),
            pytest.raises(
                HostBootstrapError, match="rollback state metadata is unsafe"
            ),
        ):
            backup(state, f"sqlite:///{state / 'database/kodi-lab.db'}")
    with pytest.raises(HostBootstrapError, match="rollback state metadata is unsafe"):
        transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert sentinel.read_bytes() == original
    assert load_current(data, expected_bundle_sha256=old_hash)
    assert not (data / "TRANSITION.json").exists()


@pytest.mark.skipif(
    os.geteuid() != 0, reason="requires packaged root transition identity"
)
def test_same_head_root_transition_keeps_web_owned_development_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = (
        _web_owned_transition_fixture(
            tmp_path,
            monkeypatch,
            old_revision=CURRENT_HEAD,
        )
    )
    receipt = transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert receipt["old_migration_revision"] == receipt["new_migration_revision"]
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", new_hash)
    with _identity(10001, 10001), lifecycle_lock(state):
        _verify_backup(
            state, backup(state, f"sqlite:///{state / 'database/kodi-lab.db'}")
        )
    assert (data / "development-data/CURRENT").stat().st_uid == 10001


def test_retained_0056_bundle_migration_marker_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(
        tmp_path,
        monkeypatch,
        old_revision="0056_repository_workspace_deployment",
        legacy_old=True,
    )
    receipt = transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert receipt["old_migration_revision"] == "0056_repository_workspace_deployment"
    assert receipt["new_migration_revision"] == CURRENT_HEAD


def test_completion_rejects_tampered_receipt_and_missing_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(
        tmp_path,
        monkeypatch,
        old_revision="0056_repository_workspace_deployment",
    )
    url = f"sqlite:///{state / 'database/kodi-lab.db'}"
    transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", new_hash)
    with pytest.raises(HostBootstrapError, match="schema or bundle differs"):
        remote_lifecycle.verify_candidate_transition_completion(state, url)
    upgrade(state, url)
    upgrade_path = state / ".last-upgrade.json"
    completed = json.loads(upgrade_path.read_bytes())
    completed["repository_transition_operation_id"] = "f" * 32
    upgrade_path.write_bytes(canonical(completed))
    with pytest.raises(HostBootstrapError, match="upgrade is incomplete"):
        remote_lifecycle.verify_candidate_transition_completion(state, url)
    completed["repository_transition_operation_id"] = json.loads(
        (state / ".repository-candidate-transition.json").read_bytes()
    )["operation_id"]
    upgrade_path.write_bytes(canonical(completed))
    receipt_path = state / ".repository-candidate-transition.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["old_bundle_sha256"] = "f" * 64
    receipt_path.write_bytes(canonical(receipt))
    with pytest.raises(HostBootstrapError, match="backup candidate differs"):
        remote_lifecycle.verify_candidate_transition_completion(state, url)


def test_missing_transition_receipt_blocks_upgrade_and_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(
        tmp_path,
        monkeypatch,
        old_revision="0056_repository_workspace_deployment",
    )
    url = f"sqlite:///{state / 'database/kodi-lab.db'}"
    transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    receipt_path = state / ".repository-candidate-transition.json"
    original = receipt_path.read_bytes()
    receipt_path.unlink()
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", new_hash)
    with pytest.raises(HostBootstrapError, match="transition receipt is missing"):
        upgrade(state, url)
    assert remote_lifecycle._revision(state / remote_lifecycle.DATABASE) == (
        "0056_repository_workspace_deployment"
    )
    receipt_path.write_bytes(original)
    receipt_path.chmod(0o600)
    upgrade(state, url)
    receipt_path.unlink()
    with pytest.raises(HostBootstrapError, match="transition receipt is missing"):
        remote_lifecycle.verify_candidate_transition_completion(state, url)


@pytest.mark.parametrize(
    "wrong",
    [
        "0055_development_repository_publications",
        "0059_workspace_validation_digest",
        CURRENT_HEAD,
    ],
)
def test_forward_transition_rejects_wrong_live_revision_before_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong: str
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(
        tmp_path,
        monkeypatch,
        old_revision="0056_repository_workspace_deployment",
    )
    with sqlite3.connect(state / remote_lifecycle.DATABASE) as connection:
        connection.execute("UPDATE alembic_version SET version_num=?", (wrong,))
    with pytest.raises(ValueError, match="live database revision"):
        transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert load_current(data, expected_bundle_sha256=old_hash)
    assert not (data / "TRANSITION.json").exists()


def test_forward_transition_rejects_unrelated_reviewed_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, _, backup_id = _setup(
        tmp_path,
        monkeypatch,
        old_revision="0056_repository_workspace_deployment",
    )
    _revision_identity(new, "0054_repository_phase1")
    new_hash = sha256((new / "BUNDLE.json").read_bytes())
    with pytest.raises(ValueError, match="head differs"):
        transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert load_current(data, expected_bundle_sha256=old_hash)
    assert not (data / "TRANSITION.json").exists()


def test_forward_transition_rejects_nonancestor_old_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(
        tmp_path,
        monkeypatch,
        old_revision="0058_unrelated_schema",
        live_revision="0056_repository_workspace_deployment",
    )
    with pytest.raises(ValueError, match="unrelated or branched"):
        transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert load_current(data, expected_bundle_sha256=old_hash)
    assert not (data / "TRANSITION.json").exists()


@pytest.mark.parametrize("stage", ["stable", "development", "database"])
def test_forward_migration_transition_interruption_is_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(
        tmp_path,
        monkeypatch,
        old_revision="0056_repository_workspace_deployment",
    )
    with pytest.raises(RuntimeError, match="injected interruption"):
        transition(
            old,
            new,
            data,
            state,
            BASE,
            old_hash,
            new_hash,
            backup_id,
            interrupt_after=stage,
        )
    assert (data / "TRANSITION.json").exists()
    receipt = transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert receipt["old_migration_revision"] == "0056_repository_workspace_deployment"
    assert remote_lifecycle._revision(state / remote_lifecycle.DATABASE) == (
        "0056_repository_workspace_deployment"
    )
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", new_hash)
    assert upgrade(state, f"sqlite:///{state / 'database/kodi-lab.db'}")[:2] == (
        "0056_repository_workspace_deployment",
        CURRENT_HEAD,
    )


@pytest.mark.parametrize("after_upgrade", [False, True])
def test_forward_migration_rollback_to_verified_a_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_upgrade: bool
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(
        tmp_path,
        monkeypatch,
        old_revision="0056_repository_workspace_deployment",
    )
    url = f"sqlite:///{state / 'database/kodi-lab.db'}"
    transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    if after_upgrade:
        monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", new_hash)
        upgrade(state, url)
    transition(
        new, old, data, state, BASE, new_hash, old_hash, backup_id, rollback=True
    )
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", old_hash)
    assert restore(state, url, backup_id, backup_id)[0] == (
        "0056_repository_workspace_deployment"
    )
    assert load_current(data, expected_bundle_sha256=old_hash)
    assert (
        load_development_current(data / "development-data")[1]["bundle_sha256"]
        == old_hash
    )
    assert not (state / ".repository-candidate-transition.json").exists()


def test_0057_evidence_refuses_downgrade_but_a_backup_can_be_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(
        tmp_path,
        monkeypatch,
        old_revision="0056_repository_workspace_deployment",
    )
    url = f"sqlite:///{state / 'database/kodi-lab.db'}"
    transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", new_hash)
    upgrade(state, url)
    target_id = str(uuid4())
    bridge_id = str(uuid4())
    before = '["screenshot","target_inspect"]'
    after = (
        '["addon_inventory","fixed_execution","screenshot",'
        '"target_inspect","workspace_deployment"]'
    )
    now = "2026-09-30T12:00:00.000000Z"
    sessions = create_session_factory(create_database_engine(url))
    with sessions.begin() as session:
        session.add(
            TargetRow(
                id=target_id,
                name="isolated-development",
                target_type="external_development",
                kodi_version="20.5",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.flush()
        session.add(
            ExternalKodiTargetRow(
                target_id=target_id,
                protocol_kind="jsonrpc_http",
                host="127.0.0.1",
                port=8080,
                authentication_mode="none",
                secret_reference=None,
                configured_capabilities_json=before,
                verified_capabilities_json='["target_inspect"]',
                administrative_state="enabled",
                verification_state="verified",
                observation_state="reachable",
                verified_addresses_json='["127.0.0.1"]',
                verified_kodi_name="Kodi",
                verified_kodi_version="20.5",
                observed_kodi_name="Kodi",
                observed_kodi_version="20.5",
                observed_protocol_version="13.0.0",
                pending_observation_id=None,
                created_at=now,
                verified_at=now,
                deactivated_at=None,
                last_observed_at=now,
            )
        )
        session.flush()
        session.add(
            ExternalBridgePairingRow(
                target_id=target_id,
                bridge_port=8765,
                pairing_state="paired",
                reachability_state="reachable",
                pairing_token_reference=None,
                bridge_secret_reference=str(uuid4()),
                pairing_token_expires_at=None,
                observed_bridge_id=bridge_id,
                observed_protocol_version="1",
                observed_addon_id="service.kodi_lab_bridge",
                observed_addon_version="1.1.13",
                observed_kodi_version="20.5",
                observed_build_fingerprint="a" * 64,
                observed_capabilities_json='["screenshot","workspace_deployment"]',
                pinned_bridge_id=bridge_id,
                pinned_protocol_version="1",
                pinned_addon_id="service.kodi_lab_bridge",
                pinned_addon_version="1.1.13",
                pinned_kodi_version="20.5",
                pinned_build_fingerprint="a" * 64,
                verified_capabilities_json='["screenshot","workspace_deployment"]',
                endpoint_revision_digest="b" * 64,
                token_generated_at=None,
                pairing_attempted_at=now,
                paired_at=now,
                verified_at=now,
                revoked_at=None,
                production_identity_generation=None,
                production_bridge_trust_revision=None,
                reviewed_build_id=None,
                production_confirmed_by=None,
                production_confirmed_at=None,
            )
        )
        session.flush()
        session.add(
            ExternalDevelopmentCapabilityAmendmentRow(
                id=str(uuid4()),
                target_id=target_id,
                target_classification="external_development",
                bridge_id=bridge_id,
                bridge_addon_id="service.kodi_lab_bridge",
                bridge_version="1.1.13",
                bridge_protocol="1",
                bridge_fingerprint="a" * 64,
                before_json=before,
                requested_json=after,
                resulting_json=after,
                native_verified_json='["addon_inventory","fixed_execution","target_inspect"]',
                bridge_verified_json='["screenshot","workspace_deployment"]',
                actor="isolated-test",
                requested_at=now,
                completed_at=now,
            )
        )
        session.flush()
        external = session.get(ExternalKodiTargetRow, target_id)
        assert external is not None
        external.configured_capabilities_json = after
    # Exercise downgrade refusal on a copy: Alembic may complete the safe
    # 0058 -> 0057 step before 0057 refuses to discard amendment evidence.
    downgrade_database = tmp_path / "downgrade-check.db"
    shutil.copy2(state / remote_lifecycle.DATABASE, downgrade_database)
    with (
        packaged_migrations.packaged_config(
            f"sqlite:///{downgrade_database}"
        ) as config,
        pytest.raises(RuntimeError, match="refusing to discard"),
    ):
        command.downgrade(config, "0056_repository_workspace_deployment")
    assert remote_lifecycle._revision(downgrade_database) == (
        "0057_development_capability_amendments"
    )
    assert remote_lifecycle._revision(state / remote_lifecycle.DATABASE) == CURRENT_HEAD
    transition(
        new, old, data, state, BASE, new_hash, old_hash, backup_id, rollback=True
    )
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", old_hash)
    assert restore(state, url, backup_id, backup_id)[0] == (
        "0056_repository_workspace_deployment"
    )
    with sqlite3.connect(state / remote_lifecycle.DATABASE) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name="
            "'external_development_capability_amendments'"
        ).fetchone() == (0,)


def test_later_same_head_transition_rollback_preserves_prior_upgrade_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(
        tmp_path,
        monkeypatch,
        old_revision="0056_repository_workspace_deployment",
    )
    url = f"sqlite:///{state / 'database/kodi-lab.db'}"
    transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", new_hash)
    upgrade(state, url)
    with lifecycle_lock(state):
        b_backup = backup(state, url)
    third = _replacement(
        new,
        tmp_path / "third",
        source_commit="e" * 40,
        source_tree="f" * 40,
    )
    third_hash = sha256((third / "BUNDLE.json").read_bytes())
    transition(new, third, data, state, BASE, new_hash, third_hash, b_backup)
    transition(
        third,
        new,
        data,
        state,
        BASE,
        third_hash,
        new_hash,
        b_backup,
        rollback=True,
    )
    assert restore(state, url, b_backup, b_backup)[0] == (CURRENT_HEAD)
    assert load_current(data, expected_bundle_sha256=new_hash)
    assert remote_lifecycle.verify_candidate_transition_completion(state, url)


def test_interrupted_application_upgrade_restores_intermediate_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(
        tmp_path,
        monkeypatch,
        old_revision="0056_repository_workspace_deployment",
    )
    url = f"sqlite:///{state / 'database/kodi-lab.db'}"
    transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", new_hash)
    original_write = remote_lifecycle._write_json

    def fail_before_completion(path: Path, value: dict[str, object]) -> None:
        if path.name == ".last-upgrade.json":
            raise RuntimeError("injected completion interruption")
        original_write(path, value)

    monkeypatch.setattr(remote_lifecycle, "_write_json", fail_before_completion)
    with pytest.raises(RuntimeError, match="injected completion interruption"):
        upgrade(state, url)
    assert (state / ".upgrade-pending.json").exists()
    with pytest.raises(HostBootstrapError, match="upgrade is incomplete"):
        remote_lifecycle.validate(state, url, require_head=True)
    pending = json.loads((state / ".upgrade-pending.json").read_bytes())
    monkeypatch.setattr(remote_lifecycle, "_write_json", original_write)
    assert restore(state, url, pending["backup_id"], pending["backup_id"])[0] == (
        "0056_repository_workspace_deployment"
    )
    assert (state / ".repository-candidate-transition.json").exists()
    assert upgrade(state, url)[:2] == (
        "0056_repository_workspace_deployment",
        CURRENT_HEAD,
    )
    assert remote_lifecycle.verify_candidate_transition_completion(state, url)


def test_rollback_refuses_unrestored_application_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(
        tmp_path,
        monkeypatch,
        old_revision="0056_repository_workspace_deployment",
    )
    transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    (state / ".upgrade-pending.json").write_bytes(canonical({"backup_id": backup_id}))
    with pytest.raises(HostBootstrapError, match="upgrade is incomplete"):
        transition(
            new,
            old,
            data,
            state,
            BASE,
            new_hash,
            old_hash,
            backup_id,
            rollback=True,
        )
    assert load_current(data, expected_bundle_sha256=new_hash)


def test_candidate_transition_preserves_development_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(tmp_path, monkeypatch)
    previous = load_current(data, expected_bundle_sha256=old_hash)[1]
    old_development = load_development_current(data / "development-data")[1]
    receipt = transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert receipt["old_stable_generation"] == previous["tree_sha256"]
    assert (
        load_current(data, expected_bundle_sha256=new_hash)[1]["tree_sha256"]
        != previous["tree_sha256"]
    )
    current_dev = load_development_current(data / "development-data")[1]
    assert current_dev["bundle_sha256"] == new_hash
    assert current_dev["packages"] == old_development["packages"]
    assert (
        data
        / "development-data"
        / "packages"
        / "plugin.video.kodi-lab-phase2-synthetic"
        / "plugin.video.kodi-lab-phase2-synthetic-1.0.0.zip"
    ).exists()
    assert (
        transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
        == receipt
    )
    assert not (data / "TRANSITION.json").exists()
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", new_hash)
    with lifecycle_lock(state):
        new_backup = backup(state, f"sqlite:///{state / 'database/kodi-lab.db'}")
    identity = cast(
        dict[str, str], _verify_backup(state, new_backup)["repository_identity"]
    )
    assert identity["stable_tree_sha256"] == receipt["new_stable_generation"]


def test_candidate_transition_rejects_wrong_identity_and_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(tmp_path, monkeypatch)
    original = load_current(data)[1]["tree_sha256"]
    with pytest.raises(ValueError):
        transition(old, new, data, state, BASE, "f" * 64, new_hash, backup_id)
    with pytest.raises(ValueError):
        transition(old, new, data, state, BASE, old_hash, "f" * 64, backup_id)
    with pytest.raises(ValueError):
        transition(
            old,
            new,
            data,
            state,
            "http://192.168.50.11:18120",
            old_hash,
            new_hash,
            backup_id,
        )
    assert load_current(data)[1]["tree_sha256"] == original


@pytest.mark.parametrize("stage", ["stable", "development", "database"])
def test_interrupted_transition_is_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="injected interruption"):
        transition(
            old,
            new,
            data,
            state,
            BASE,
            old_hash,
            new_hash,
            backup_id,
            interrupt_after=stage,
        )
    assert (data / "TRANSITION.json").exists()
    transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert load_current(data, expected_bundle_sha256=new_hash)
    assert (
        load_development_current(data / "development-data")[1]["bundle_sha256"]
        == new_hash
    )


def test_reverse_transition_restores_old_backup_coherently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(tmp_path, monkeypatch)
    old_stable = load_current(data)[1]
    old_development = load_development_current(data / "development-data")[1]
    first = transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    transition(
        new,
        old,
        data,
        state,
        BASE,
        new_hash,
        old_hash,
        backup_id,
        rollback=True,
    )
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", old_hash)
    assert (
        restore(
            state, f"sqlite:///{state / 'database/kodi-lab.db'}", backup_id, backup_id
        )[0]
        == CURRENT_HEAD
    )
    assert load_current(data, expected_bundle_sha256=old_hash)[1] == old_stable
    assert load_development_current(data / "development-data")[1] == old_development
    assert not (state / ".repository-candidate-transition.json").exists()
    again = transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert again["operation_id"] != first["operation_id"]


def test_tampered_prepared_generation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="injected interruption"):
        transition(
            old,
            new,
            data,
            state,
            BASE,
            old_hash,
            new_hash,
            backup_id,
            interrupt_after="stable",
        )
    pending = json.loads((data / "TRANSITION.json").read_bytes())
    new_index = (
        data / "generations" / pending["new_stable_generation"] / "stable/addons.xml"
    )
    new_index.write_bytes(b"tampered")
    with pytest.raises(ValueError):
        transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert (data / "TRANSITION.json").exists()


def test_workspace_deployment_linkage_survives_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    url = f"sqlite:///{state / 'database/kodi-lab.db'}"
    initialize(state, url)
    deployment_root = tmp_path / "deployment"
    env, repository, _ = repository_workspace_setup(
        deployment_root, database_path=state / "database/kodi-lab.db"
    )
    generation = workspace_source_version(env, 0, "1.0.0")
    pending = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=generation,
        target_id=EXTERNAL_TARGET_ID,
    )
    approve_workspace_publication(env, pending)
    old = deployment_root / "candidate/candidate"
    new = _replacement(old, tmp_path / "replacement")
    data = repository.root.parent
    old_hash = sha256((old / "BUNDLE.json").read_bytes())
    new_hash = sha256((new / "BUNDLE.json").read_bytes())
    monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(data))
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", BASE)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", old_hash)
    with lifecycle_lock(state):
        backup_id = backup(state, url)
    with env.sessions() as session:
        linked = session.get(
            RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
        )
        assert linked is not None
        before = {
            column.name: getattr(linked, column.name)
            for column in linked.__table__.columns
        }
    transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    with env.sessions() as session:
        linked = session.get(
            RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
        )
        assert linked is not None
        assert {
            column.name: getattr(linked, column.name)
            for column in linked.__table__.columns
        } == before
    assert repository.reconcile()["bundle_sha256"] == new_hash


def test_tampered_current_or_replacement_rejected_before_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, data, state, old_hash, new_hash, backup_id = _setup(tmp_path, monkeypatch)
    (new / "bridge/BRIDGE.json").write_bytes(b"unreviewed")
    with pytest.raises(ValueError):
        transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert not (data / "TRANSITION.json").exists()
    new = _replacement(old, tmp_path / "another-replacement")
    current = load_current(data)[0]
    (current / "stable/addons.xml.sha256").write_text("0" * 64 + "\n")
    with pytest.raises(ValueError):
        transition(old, new, data, state, BASE, old_hash, new_hash, backup_id)
    assert not (data / "TRANSITION.json").exists()
