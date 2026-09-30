"""Remote container lifecycle proof against packaged historical migrations."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config

from kodi_lab import remote_lifecycle
from kodi_lab.host_bootstrap import (
    HostBootstrapError,
    generate_master_key,
    initialize_state,
)
from kodi_lab.remote_lifecycle import (
    _key_digest,
    _revision,
    _verify_backup,
    backup,
    initialize,
    lifecycle_lock,
    recover_clean,
    restore,
    upgrade,
    validate,
)
from kodi_lab.repository.development import (
    DevelopmentRepositoryService,
)
from kodi_lab.repository.development import (
    initialize as initialize_development,
)
from kodi_lab.repository.stable import generate as generate_stable
from kodi_lab.repository.stable import sha256
from kodi_lab.storage import packaged_migrations
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import (
    McpWorkspaceDeploymentOperationRow,
    RepositoryWorkspaceDeploymentRow,
    RepositoryWorkspaceValidationSnapshotRow,
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
from tests.repository.test_development import BASE as REPOSITORY_BASE
from tests.repository.test_development import fixture_zip, phase2_candidate


@pytest.fixture(autouse=True)
def repository_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the real tree while the installed-wheel path has its own tests."""

    @contextmanager
    def config_for(database_url: str) -> Iterator[Config]:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).parents[2] / "migrations")
        )
        config.set_main_option("sqlalchemy.url", database_url)
        yield config

    monkeypatch.setattr(packaged_migrations, "packaged_config", config_for)
    monkeypatch.setattr(remote_lifecycle, "packaged_config", config_for)


def _url(root: Path) -> str:
    return f"sqlite:///{root / 'database' / 'kodi-lab.db'}"


def _historical(root: Path) -> None:
    initialize_state(root)
    generate_master_key(root)
    previous_umask = os.umask(0o077)
    try:
        with packaged_migrations.packaged_config(_url(root)) as config:
            command.upgrade(config, "0052_production_policy_revision_binding")
    finally:
        os.umask(previous_umask)


def test_fresh_state_is_idempotent_and_partial_state_fails_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    revision = initialize(root, _url(root))
    key = _key_digest(root)
    assert initialize(root, _url(root)) == revision
    assert _key_digest(root) == key
    assert validate(root, _url(root), require_head=True) == revision
    assert stat.S_IMODE((root / "master-key").stat().st_mode) == 0o600
    (root / ".state.json").unlink()
    with pytest.raises(HostBootstrapError, match="marker is missing"):
        validate(root, _url(root), require_marker=True)
    with pytest.raises(HostBootstrapError, match="marker is missing"):
        initialize(root, _url(root))
    previous, adopted, identifier = upgrade(root, _url(root))
    assert previous == adopted == revision and identifier is not None
    assert (
        validate(root, _url(root), require_head=True, require_marker=True) == revision
    )
    (root / "master-key").unlink()
    with pytest.raises(FileNotFoundError):
        initialize(root, _url(root))
    with pytest.raises(FileNotFoundError):
        validate(root, _url(root))
    partial = tmp_path / "partial"
    partial.mkdir(mode=0o700)
    (partial / "unexpected").write_text("x")
    with pytest.raises(FileNotFoundError):
        initialize(partial, _url(partial))


def test_backup_is_private_complete_and_uses_sqlite_backup_api(tmp_path: Path) -> None:
    root = tmp_path / "state"
    initialize(root, _url(root))
    db_path = root / "database" / "kodi-lab.db"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE lifecycle_probe (value TEXT NOT NULL)")
        db.execute("INSERT INTO lifecycle_probe VALUES ('committed')")
        db.commit()
        with lifecycle_lock(root):
            identifier = backup(root, _url(root))
        folder = root / "backups" / identifier
        metadata = _verify_backup(root, identifier)
        assert metadata["complete"] is True
        assert metadata["key_sha256"] == _key_digest(root)
        assert "application" in metadata and "created_at" in metadata
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        assert stat.S_IMODE((folder / "master-key").stat().st_mode) == 0o600
        assert not (folder / "database" / "kodi-lab.db-wal").exists()
        with sqlite3.connect(folder / "database" / "kodi-lab.db") as copy:
            assert copy.execute("SELECT value FROM lifecycle_probe").fetchone() == (
                "committed",
            )
        (folder / "backups").mkdir(mode=0o700)
        with pytest.raises(HostBootstrapError, match="directories differ"):
            _verify_backup(root, identifier)
        (folder / "backups").rmdir()

    def second_lock() -> None:
        with lifecycle_lock(root):
            pass

    with lifecycle_lock(root), pytest.raises(HostBootstrapError, match="in use"):
        second_lock()


def test_development_repository_backup_recover_clean_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = phase2_candidate(tmp_path)
    repository_data = tmp_path / "repository-data"
    generate_stable(bundle, repository_data, REPOSITORY_BASE)
    initialize_development(
        repository_data / "development-data", REPOSITORY_BASE, bundle
    )
    root = tmp_path / "state"
    initialize(root, _url(root))
    engine = create_database_engine(_url(root))
    service = DevelopmentRepositoryService(
        create_session_factory(engine), repository_data, REPOSITORY_BASE
    )
    service.reconcile()
    body = fixture_zip("1.0.0")
    prepared = service.prepare_import(body, ("matrix", "nexus", "omega"))
    published = service.approve(UUID(prepared.id), observed_sha256=sha256(body))
    monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(repository_data))
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", REPOSITORY_BASE)
    with lifecycle_lock(root):
        identifier = backup(root, _url(root))
    manifest = _verify_backup(root, identifier)
    assert (
        cast(dict[str, str], manifest["repository_identity"])["base_url"]
        == REPOSITORY_BASE
    )
    backed_up_package = (
        root
        / "backups"
        / identifier
        / "repository-data"
        / "development-data"
        / "packages"
        / prepared.addon_id
        / f"{prepared.addon_id}-1.0.0.zip"
    )
    assert backed_up_package.read_bytes() == body
    backed_up_package.chmod(0o600)
    backed_up_package.write_bytes(b"tampered")
    backed_up_package.chmod(0o444)
    with pytest.raises(HostBootstrapError, match="content differs"):
        _verify_backup(root, identifier)
    backed_up_package.chmod(0o600)
    backed_up_package.write_bytes(body)
    backed_up_package.chmod(0o444)
    assert (
        _verify_backup(root, identifier)["repository_identity"]
        == manifest["repository_identity"]
    )
    engine.dispose()

    recovered_repository = tmp_path / "recovered-repository"
    generate_stable(bundle, recovered_repository, REPOSITORY_BASE)
    initialize_development(
        recovered_repository / "development-data", REPOSITORY_BASE, bundle
    )
    recovered_root = tmp_path / "recovered-state"
    recovered_root.mkdir(mode=0o700)
    (recovered_root / "backups").mkdir(mode=0o700)
    shutil.copytree(
        root / "backups" / identifier,
        recovered_root / "backups" / identifier,
    )
    monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(recovered_repository))
    assert recover_clean(
        recovered_root, _url(recovered_root), identifier, identifier
    ) == ("0057_development_capability_amendments")
    recovered_engine = create_database_engine(_url(recovered_root))
    recovered_service = DevelopmentRepositoryService(
        create_session_factory(recovered_engine), recovered_repository, REPOSITORY_BASE
    )
    assert recovered_service.reconcile()["tree_sha256"] == published.feed_generation
    recovered_publication = recovered_service.get_publication(UUID(prepared.id))
    assert recovered_publication is not None
    assert recovered_publication.zip_sha256 == sha256(body)
    recovered_engine.dispose()


def test_pending_and_completed_repository_deployment_backup_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    initialize(state, _url(state))
    deployment_root = tmp_path / "deployment"
    env, repository, target = repository_workspace_setup(
        deployment_root, database_path=state / "database" / "kodi-lab.db"
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
    monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(repository.root.parent))
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", REPOSITORY_BASE)
    with lifecycle_lock(state):
        pending_backup = backup(state, _url(state))

    def recover_and_check(identifier: str, expected_state: str) -> None:
        destination = tmp_path / f"recovered-{expected_state}"
        bundle = deployment_root / "candidate" / "candidate"
        recovered_repository = tmp_path / f"repository-{expected_state}"
        generate_stable(bundle, recovered_repository, REPOSITORY_BASE)
        initialize_development(
            recovered_repository / "development-data", REPOSITORY_BASE, bundle
        )
        destination.mkdir(mode=0o700)
        (destination / "backups").mkdir(mode=0o700)
        shutil.copytree(
            state / "backups" / identifier,
            destination / "backups" / identifier,
        )
        monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(recovered_repository))
        assert recover_clean(destination, _url(destination), identifier, identifier)
        engine = create_database_engine(_url(destination))
        sessions = create_session_factory(engine)
        with sessions() as session:
            operation = session.get(
                McpWorkspaceDeploymentOperationRow, str(pending.deployment_id)
            )
            linked = session.get(
                RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
            )
            assert operation is not None and linked is not None
            assert linked.state == expected_state
            assert linked.publication_id == str(pending.publication_id)
            assert linked.zip_sha256 == pending.zip_sha256
            assert linked.expected_manifest_digest == pending.expected_manifest_digest
            assert operation.generation == generation
            snapshot = session.get(
                RepositoryWorkspaceValidationSnapshotRow, linked.validation_receipt
            )
            assert snapshot is not None
            assert snapshot.generation == generation
            assert snapshot.addon_version == pending.addon_version
        restored = DevelopmentRepositoryService(
            sessions, recovered_repository, REPOSITORY_BASE
        )
        restored.reconcile()
        engine.dispose()

    recover_and_check(pending_backup, "awaiting_kodi_update")
    monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(repository.root.parent))
    with env.sessions() as session:
        linked = session.get(
            RepositoryWorkspaceDeploymentRow, str(pending.deployment_id)
        )
        assert linked is not None
        target.installed_version = "1.0.0"
        target.installed_digest = linked.expected_manifest_digest
        target.installed_files = linked.file_count
        target.installed_bytes = linked.total_bytes
    assert (
        env.development.recheck_repository_deployment(pending.deployment_id).state
        == "succeeded"
    )
    with lifecycle_lock(state):
        completed_backup = backup(state, _url(state))
    recover_and_check(completed_backup, "succeeded")


def test_historical_transition_and_matching_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    _historical(root)
    before = _revision(root / "database" / "kodi-lab.db")
    key = _key_digest(root)
    previous, current, identifier = upgrade(root, _url(root))
    assert identifier is not None
    assert previous == before
    assert current == "0057_development_capability_amendments"
    assert upgrade(root, _url(root)) == (current, current, None)
    assert (
        _revision(root / "backups" / identifier / "database" / "kodi-lab.db") == before
    )
    with pytest.raises(HostBootstrapError, match="confirmation"):
        restore(root, _url(root), identifier, "wrong")
    wrong = backup(root, _url(root))
    with pytest.raises(HostBootstrapError, match="most recent upgrade"):
        restore(root, _url(root), wrong, wrong)
    original_copyfile = shutil.copyfile

    def interrupted_copy(
        source: str | Path, destination: str | Path, *, follow_symlinks: bool = True
    ) -> str | Path:
        if Path(destination) == root / "master-key":
            raise OSError("injected restore interruption")
        return original_copyfile(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(shutil, "copyfile", interrupted_copy)
    with pytest.raises(OSError, match="injected restore interruption"):
        restore(root, _url(root), identifier, identifier)
    with pytest.raises(HostBootstrapError, match="restore is incomplete"):
        validate(root, _url(root), require_head=True)
    monkeypatch.setattr(shutil, "copyfile", original_copyfile)
    restored, quarantine_id = restore(root, _url(root), identifier, identifier)
    assert restored == before
    assert _key_digest(root) == key
    assert (root / "quarantine" / quarantine_id).is_dir()
    assert any(
        _revision(path / "database" / "kodi-lab.db") == current
        for path in (root / "quarantine").iterdir()
        if (path / "database" / "kodi-lab.db").exists()
    )
    with pytest.raises(HostBootstrapError, match="does not match installed head"):
        validate(root, _url(root), require_head=True)


def test_failed_migration_blocks_startup_and_restore_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    _historical(root)

    def fail(*_args: object) -> tuple[str, str]:
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr("kodi_lab.remote_lifecycle.migrate_database", fail)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        upgrade(root, _url(root))
    receipt = json.loads((root / ".upgrade-pending.json").read_text())
    with pytest.raises(HostBootstrapError, match="incomplete"):
        validate(root, _url(root), require_head=True)
    restored, _ = restore(root, _url(root), receipt["backup_id"], receipt["backup_id"])
    assert restored == "0052_production_policy_revision_binding"


def test_failed_backup_prevents_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    _historical(root)

    def fail(*_args: object) -> str:
        raise RuntimeError("injected backup failure")

    monkeypatch.setattr("kodi_lab.remote_lifecycle.backup", fail)
    with pytest.raises(RuntimeError, match="injected backup failure"):
        upgrade(root, _url(root))
    assert (
        _revision(root / "database" / "kodi-lab.db")
        == "0052_production_policy_revision_binding"
    )
    assert not (root / ".upgrade-pending.json").exists()


def test_rollback_excludes_snapshot_and_preserves_web_artifact_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    _historical(root)
    snapshots = root / "snapshots"
    snapshots.mkdir(mode=0o750)
    artifact = snapshots / "broker-owned-proof"
    artifact.write_bytes(b"snapshot proof")
    artifact.chmod(0o440)
    dependency = {
        "90000000-0000-4000-8000-000000000102": {
            "manifest.json": "a" * 64,
            "state.tar": "b" * 64,
        }
    }
    monkeypatch.setattr(
        remote_lifecycle,
        "published_snapshot_inventory",
        lambda _root, *, web_gid: dependency,
    )
    zip_root = root / "addon-zips" / "artifacts" / "fixture"
    zip_root.mkdir(parents=True, mode=0o700)
    original = zip_root / "original.zip"
    original.write_bytes(b"zip proof")
    original.chmod(0o400)
    zip_root.chmod(0o500)
    (root / "addon-zips" / "artifacts").chmod(0o700)
    (root / "addon-zips").chmod(0o700)
    workspace_tree = root / "agent-workspaces" / "fixture" / "tree"
    workspace_tree.mkdir(parents=True, mode=0o700)
    workspace_file = workspace_tree / "addon.xml"
    workspace_file.write_text("<addon/>")
    workspace_file.chmod(0o644)
    workspace_tree.chmod(0o755)
    (root / "agent-workspaces" / "fixture").chmod(0o755)
    (root / "agent-workspaces").chmod(0o700)
    previous, _, identifier = upgrade(root, _url(root))
    assert identifier is not None
    backup_root = root / "backups" / identifier
    assert not (backup_root / "snapshots").exists()
    assert _verify_backup(root, identifier)["snapshot_dependencies"] == dependency
    assert (
        backup_root / "addon-zips" / "artifacts" / "fixture" / "original.zip"
    ).read_bytes() == b"zip proof"
    assert (
        stat.S_IMODE(
            (backup_root / "addon-zips" / "artifacts" / "fixture").stat().st_mode
        )
        == 0o500
    )
    restored, _ = restore(root, _url(root), identifier, identifier)
    assert restored == previous
    assert artifact.read_bytes() == b"snapshot proof"
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o440
    assert original.read_bytes() == b"zip proof"
    assert stat.S_IMODE(original.stat().st_mode) == 0o400
    assert workspace_file.read_text() == "<addon/>"
    assert stat.S_IMODE(workspace_tree.stat().st_mode) == 0o755
    assert stat.S_IMODE(workspace_file.stat().st_mode) == 0o644


def test_rollback_rejects_changed_snapshot_dependencies_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    _historical(root)
    observed = {
        "90000000-0000-4000-8000-000000000102": {
            "manifest.json": "a" * 64,
            "state.tar": "b" * 64,
        }
    }
    monkeypatch.setattr(
        remote_lifecycle,
        "published_snapshot_inventory",
        lambda _root, *, web_gid: observed,
    )
    _, current, identifier = upgrade(root, _url(root))
    assert identifier is not None
    observed = {}
    with pytest.raises(HostBootstrapError, match="differ from backup dependency"):
        restore(root, _url(root), identifier, identifier)
    assert _revision(root / "database" / "kodi-lab.db") == current
    assert not (root / ".restore-pending.json").exists()


def test_backup_rejects_unsafe_modes_and_unclassified_state(tmp_path: Path) -> None:
    root = tmp_path / "state"
    initialize(root, _url(root))
    (root / "secrets").chmod(0o750)
    with pytest.raises(HostBootstrapError, match="metadata is unsafe"):
        backup(root, _url(root))
    (root / "secrets").chmod(0o700)
    (root / "unknown-durable-tree").mkdir(mode=0o700)
    with pytest.raises(HostBootstrapError, match="unclassified state root"):
        backup(root, _url(root))


def test_backup_manifest_metadata_and_path_attacks_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "state"
    initialize(root, _url(root))
    identifier = backup(root, _url(root))
    folder = root / "backups" / identifier
    manifest_path = folder / "manifest.json"
    original = json.loads(manifest_path.read_text())

    def manifest(value: dict[str, object]) -> None:
        manifest_path.write_text(json.dumps(value) + "\n")
        manifest_path.chmod(0o600)

    bad = json.loads(json.dumps(original))
    bad["files"]["master-key"]["uid"] = 0
    manifest(bad)
    with pytest.raises(HostBootstrapError, match="ownership differs"):
        _verify_backup(root, identifier)
    bad = json.loads(json.dumps(original))
    bad["files"]["../master-key"] = bad["files"]["master-key"]
    manifest(bad)
    with pytest.raises(HostBootstrapError, match="reserved state"):
        _verify_backup(root, identifier)
    bad = json.loads(json.dumps(original))
    bad["files"]["master-key"]["sha256"] = "0" * 64
    manifest(bad)
    with pytest.raises(HostBootstrapError, match="content differs"):
        _verify_backup(root, identifier)
    manifest(original)
    (folder / "master-key").unlink()
    (folder / "master-key").symlink_to(root / "master-key")
    with pytest.raises(HostBootstrapError, match="metadata is unsafe"):
        _verify_backup(root, identifier)


def test_verified_backup_recovers_fresh_application_volume(tmp_path: Path) -> None:
    old = tmp_path / "old"
    initialize(old, _url(old))
    identifier = backup(old, _url(old))
    fresh = tmp_path / "fresh"
    fresh.mkdir(mode=0o700)
    (fresh / "backups").mkdir(mode=0o700)
    shutil.copytree(old / "backups" / identifier, fresh / "backups" / identifier)
    revision = recover_clean(fresh, _url(fresh), identifier, identifier)
    assert revision == validate(
        fresh, _url(fresh), require_head=True, require_marker=True
    )
    assert _key_digest(fresh) == _key_digest(old)
    assert (fresh / "database" / "kodi-lab.db").stat().st_uid == os.getuid()
    assert (fresh / "master-key").stat().st_gid == os.getgid()


def test_clean_recovery_wrong_key_and_interruption_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = tmp_path / "old"
    other = tmp_path / "other"
    initialize(old, _url(old))
    initialize(other, _url(other))
    identifier = backup(old, _url(old))
    fresh = tmp_path / "fresh"
    fresh.mkdir(mode=0o700)
    (fresh / "backups").mkdir(mode=0o700)
    folder = fresh / "backups" / identifier
    shutil.copytree(old / "backups" / identifier, folder)
    original_copyfile = shutil.copyfile

    def interrupted(
        source: str | Path, destination: str | Path, *, follow_symlinks: bool = True
    ) -> str | Path:
        if Path(destination) == fresh / "master-key":
            raise OSError("injected clean recovery interruption")
        return original_copyfile(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(shutil, "copyfile", interrupted)
    with pytest.raises(OSError, match="interruption"):
        recover_clean(fresh, _url(fresh), identifier, identifier)
    with pytest.raises(HostBootstrapError, match="recovery is incomplete"):
        validate(fresh, _url(fresh))
    monkeypatch.setattr(shutil, "copyfile", original_copyfile)
    shutil.rmtree(fresh)
    fresh.mkdir(mode=0o700)
    (fresh / "backups").mkdir(mode=0o700)
    folder = fresh / "backups" / identifier
    shutil.copytree(old / "backups" / identifier, folder)
    original_copyfile(other / "master-key", folder / "master-key")
    record = json.loads((folder / "manifest.json").read_text())
    content = (folder / "master-key").read_bytes()
    record["files"]["master-key"]["sha256"] = hashlib.sha256(content).hexdigest()
    record["key_sha256"] = _key_digest(other)
    (folder / "manifest.json").write_text(json.dumps(record) + "\n")
    with pytest.raises(HostBootstrapError, match="marker and master key"):
        recover_clean(fresh, _url(fresh), identifier, identifier)
    with pytest.raises(HostBootstrapError, match="recovery is incomplete"):
        validate(fresh, _url(fresh))


def test_clean_recovery_rejects_untrusted_snapshot_root(tmp_path: Path) -> None:
    old = tmp_path / "old"
    initialize(old, _url(old))
    identifier = backup(old, _url(old))
    fresh = tmp_path / "fresh"
    fresh.mkdir(mode=0o700)
    (fresh / "backups").mkdir(mode=0o700)
    shutil.copytree(old / "backups" / identifier, fresh / "backups" / identifier)
    snapshots = fresh / "snapshots"
    snapshots.symlink_to(old / "database", target_is_directory=True)
    with pytest.raises(HostBootstrapError, match="ownership is unsafe"):
        recover_clean(fresh, _url(fresh), identifier, identifier)
    assert not (fresh / ".recovery-pending.json").exists()
