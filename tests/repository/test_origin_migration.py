"""Origin changes preserve immutable publications and recover each publication gap."""

from __future__ import annotations

import json
import sqlite3
import threading
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen
from uuid import UUID, uuid4

import pytest

from kodi_lab import remote_lifecycle
from kodi_lab.host_bootstrap import HostBootstrapError
from kodi_lab.repository import development, stable
from kodi_lab.repository.origin_migration import PENDING, migrate_origin
from kodi_lab.repository.server import RepositoryHandler
from kodi_lab.repository.transition import transition
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from tests.repository import test_candidate_transition as candidate_fixtures
from tests.repository.test_development import BASE, fixture_zip
from tests.storage.test_migrations import migration_templates as migration_templates

migrations = candidate_fixtures.migrations
NEW = "http://kodi-lab.home.arpa:18120"


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    bundle, _new, root, state, digest, _new_digest, backup = candidate_fixtures._setup(
        tmp_path, monkeypatch
    )
    site = tmp_path / "site"
    site.mkdir()
    environment = site / ".env"
    environment.write_text(
        "# preserve all unrelated site settings\n"
        "KODI_LAB_REPOSITORY_BIND=192.168.50.10\n"
        f"KODI_LAB_REPOSITORY_BASE_URL={BASE}\n"
        "UNRELATED_VALUE=unchanged\n"
    )
    environment.chmod(0o600)
    receipt = site / "INSTALLATION.json"
    receipt.write_text(
        json.dumps(
            {
                "repository_base_url": BASE,
                "repository_bind": "192.168.50.10",
                "bundle_sha256": digest,
                "project": "unchanged-project",
                "source_commit": "unchanged-source",
                "completed_stage": "ready",
            }
        )
    )
    receipt.chmod(0o600)
    return dict(
        bundle=bundle,
        repository_root=root,
        state_root=state,
        old_origin=BASE,
        new_origin=NEW,
        version="1.0.1",
        operation_id=uuid4(),
        backup_id=backup,
        site_environment=environment,
        installation_receipt=receipt,
        repository_bind="192.168.50.11",
    )


def _inventory(database: Path) -> dict[str, list[tuple[Any, ...]]]:
    with sqlite3.connect(database) as connection:
        tables = [
            item[0]
            for item in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if item[0] != "development_repository_state"
        ]
        return {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
            for table in tables
        }


@pytest.mark.parametrize(
    "stage",
    [None, "prepared", "stable", "development", "database", "environment", "receipt"],
)
def test_origin_migration_recovers_without_rewriting_artifacts_or_other_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str | None
) -> None:
    options = _setup(tmp_path, monkeypatch)
    root, state = options["repository_root"], options["state_root"]
    database = state / remote_lifecycle.DATABASE
    before_database = _inventory(database)
    old_stable = stable.load_current(root)[1]
    old_development = development.load_current(root / "development-data")[1]
    old_package = (
        root / "packages/repository.kodi-lab/repository.kodi-lab-1.0.0.zip"
    ).read_bytes()
    if stage is not None:
        with pytest.raises(RuntimeError, match="injected interruption"):
            migrate_origin(**options, interrupt_after=stage)
        assert (root / PENDING).exists()
    receipt = migrate_origin(**options)
    assert not (root / PENDING).exists()
    assert migrate_origin(**options) == receipt
    assert _inventory(database) == before_database
    current = stable.load_current(root, expected_base_url=NEW)[1]
    dev = development.load_current(root / "development-data", expected_base_url=NEW)[1]
    assert current["repository_version"] == dev["repository_version"] == "1.0.1"
    assert dev["packages"] == old_development["packages"]
    assert stable.load_generation(root, str(old_stable["tree_sha256"]))[1] == old_stable
    assert (
        development.load_generation(
            root / "development-data", str(old_development["tree_sha256"])
        )[1]
        == old_development
    )
    assert (
        root / "packages/repository.kodi-lab/repository.kodi-lab-1.0.0.zip"
    ).read_bytes() == old_package
    remote_lifecycle._verify_repository_pair(database, root / "development-data", NEW)
    assert "UNRELATED_VALUE=unchanged\n" in options["site_environment"].read_text()
    assert (
        "KODI_LAB_REPOSITORY_BIND=192.168.50.11\n"
        in options["site_environment"].read_text()
    )
    installed = json.loads(options["installation_receipt"].read_bytes())
    assert installed["repository_base_url"] == NEW
    assert installed["repository_origin_migration"] == str(options["operation_id"])
    assert installed["project"] == "unchanged-project"
    for name in ("master-key", ".state.json"):
        assert (state / name).is_file()


@pytest.mark.parametrize(
    "change", ["old", "version", "configuration", "receipt", "candidate", "backup"]
)
def test_wrong_migration_intent_fails_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    options = _setup(tmp_path, monkeypatch)
    root = options["repository_root"]
    before = (root / "CURRENT").read_bytes()
    if change == "old":
        options["old_origin"] = "http://192.168.50.99:18120"
    elif change == "version":
        options["version"] = "1.0.0"
    elif change == "configuration":
        options["site_environment"].write_text("KODI_LAB_REPOSITORY_BASE_URL=wrong\n")
    elif change == "receipt":
        options["installation_receipt"].write_text("{}\n")
    elif change == "candidate":
        (options["bundle"] / "bridge/BRIDGE.json").write_text("{}\n")
    else:
        options["backup_id"] = "0" * 32
    with pytest.raises((ValueError, HostBootstrapError, OSError)):
        migrate_origin(**options)
    assert (root / "CURRENT").read_bytes() == before
    assert not (root / PENDING).exists()


@pytest.mark.parametrize(
    "change", ["generation", "configuration", "database", "operation"]
)
def test_recovery_rejects_drift_before_more_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    options = _setup(tmp_path, monkeypatch)
    root = options["repository_root"]
    with pytest.raises(RuntimeError, match="injected interruption"):
        migrate_origin(**options, interrupt_after="prepared")
    before = (root / "CURRENT").read_bytes()
    intent = json.loads((root / PENDING).read_bytes())
    if change == "generation":
        (
            root / "generations" / intent["new_stable_generation"] / "stable/addons.xml"
        ).write_text("tampered")
    elif change == "configuration":
        with options["site_environment"].open("a") as stream:
            stream.write("ADMIN_EDIT=yes\n")
    elif change == "database":
        with sqlite3.connect(
            options["state_root"] / remote_lifecycle.DATABASE
        ) as connection:
            connection.execute(
                "UPDATE development_repository_state SET current_index_sha256=?",
                ("0" * 64,),
            )
    else:
        options["operation_id"] = uuid4()
    with pytest.raises(ValueError):
        migrate_origin(**options)
    assert (root / "CURRENT").read_bytes() == before
    assert (root / PENDING).exists()


def test_successor_feed_serves_both_repository_updates_and_further_publications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = _setup(tmp_path, monkeypatch)
    root = options["repository_root"]
    migrate_origin(**options)
    handler = type(
        "MigratedRepository",
        (RepositoryHandler,),
        {
            "root": root,
            "expected_base_url": NEW,
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(url + "/install/") as response:
            body = response.read()
            assert b"repository.kodi-lab-1.0.1.zip" in body
            assert b"repository.kodi-lab-dev-1.0.1.zip" in body
        for addon_id, feed in (
            ("repository.kodi-lab", "stable"),
            ("repository.kodi-lab-dev", "development"),
        ):
            with urlopen(url + f"/{feed}/addons.xml") as response:
                index = ET.fromstring(response.read())
                repository = next(
                    item for item in index if item.attrib["id"] == addon_id
                )
                assert repository.attrib["version"] == "1.0.1"
                assert all(
                    (item.text or "").startswith(NEW)
                    for item in repository.findall("./extension/dir/*")
                    if item.tag in {"info", "checksum", "datadir"}
                )
            with urlopen(url + f"/install/{addon_id}-1.0.1.zip") as response:
                bootstrap = response.read()
            with urlopen(
                url + f"/{feed}/zips/{addon_id}/{addon_id}-1.0.1.zip"
            ) as response:
                assert response.read() == bootstrap
            with urlopen(
                url + f"/{feed}/zips/{addon_id}/{addon_id}-1.0.1.zip.sha256"
            ) as response:
                assert response.read().decode().strip() == stable.sha256(bootstrap)
        (root / PENDING).write_text("pending")
        with pytest.raises(HTTPError) as error:
            urlopen(url + "/health")
        assert error.value.code == 503
        (root / PENDING).unlink()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    engine = create_database_engine(
        f"sqlite:///{options['state_root'] / remote_lifecycle.DATABASE}"
    )
    service = development.DevelopmentRepositoryService(
        create_session_factory(engine), root, NEW
    )
    body = fixture_zip("1.0.1")
    prepared = service.prepare_import(body, ("omega",))
    service.approve(UUID(prepared.id), observed_sha256=stable.sha256(body))
    assert service.reconcile()["repository_version"] == "1.0.1"
    engine.dispose()


def test_reversing_a_published_origin_uses_a_newer_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = _setup(tmp_path, monkeypatch)
    migrate_origin(**options)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", NEW)
    state = options["state_root"]
    with remote_lifecycle.lifecycle_lock(state):
        backup = remote_lifecycle.backup(
            state, f"sqlite:///{state / remote_lifecycle.DATABASE}"
        )
    reverse = {
        **options,
        "old_origin": NEW,
        "new_origin": BASE,
        "version": "1.0.2",
        "operation_id": uuid4(),
        "backup_id": backup,
    }
    migrate_origin(**reverse)
    assert (
        stable.load_current(options["repository_root"], expected_base_url=BASE)[1][
            "repository_version"
        ]
        == "1.0.2"
    )
    assert (
        development.load_current(
            options["repository_root"] / "development-data", expected_base_url=BASE
        )[1]["repository_version"]
        == "1.0.2"
    )


def test_candidate_upgrade_after_origin_move_preserves_repository_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = _setup(tmp_path, monkeypatch)
    migrate_origin(**options)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", NEW)
    state, root = options["state_root"], options["repository_root"]
    url = f"sqlite:///{state / remote_lifecycle.DATABASE}"
    with remote_lifecycle.lifecycle_lock(state):
        backup = remote_lifecycle.backup(state, url)
    successor = candidate_fixtures._replacement(
        options["bundle"], tmp_path / "successor"
    )
    old_hash = stable.sha256((options["bundle"] / "BUNDLE.json").read_bytes())
    new_hash = stable.sha256((successor / "BUNDLE.json").read_bytes())
    transition(
        options["bundle"], successor, root, state, NEW, old_hash, new_hash, backup
    )
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", new_hash)
    assert remote_lifecycle.verify_candidate_transition_completion(state, url)
    assert stable.load_current(root)[1]["repository_version"] == "1.0.1"
    assert (
        development.load_current(root / "development-data")[1]["repository_version"]
        == "1.0.1"
    )


def test_origin_move_after_completed_upgrade_retains_historical_backup_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = _setup(tmp_path, monkeypatch)
    state, root = options["state_root"], options["repository_root"]
    url = f"sqlite:///{state / remote_lifecycle.DATABASE}"
    successor = candidate_fixtures._replacement(
        options["bundle"], tmp_path / "successor"
    )
    old_hash = stable.sha256((options["bundle"] / "BUNDLE.json").read_bytes())
    new_hash = stable.sha256((successor / "BUNDLE.json").read_bytes())
    transition(
        options["bundle"],
        successor,
        root,
        state,
        BASE,
        old_hash,
        new_hash,
        options["backup_id"],
    )
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", new_hash)
    with remote_lifecycle.lifecycle_lock(state):
        backup = remote_lifecycle.backup(state, url)
    receipt_path = options["installation_receipt"]
    receipt = json.loads(receipt_path.read_bytes())
    receipt["bundle_sha256"] = new_hash
    receipt_path.write_text(json.dumps(receipt))
    options.update(bundle=successor, backup_id=backup)
    migrate_origin(**options)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", NEW)
    assert remote_lifecycle.verify_candidate_transition_completion(state, url)
