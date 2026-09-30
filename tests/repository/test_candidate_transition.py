"""Reviewed candidate transitions preserve both repository and application evidence."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config

from kodi_lab import remote_lifecycle
from kodi_lab.remote_lifecycle import (
    _verify_backup,
    backup,
    initialize,
    lifecycle_lock,
    restore,
)
from kodi_lab.repository.development import DevelopmentRepositoryService
from kodi_lab.repository.development import initialize as initialize_development
from kodi_lab.repository.development import load_current as load_development_current
from kodi_lab.repository.stable import canonical, generate, load_current, sha256
from kodi_lab.repository.transition import transition
from kodi_lab.storage import packaged_migrations
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import RepositoryWorkspaceDeploymentRow
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
from tests.repository.test_development import BASE, fixture_zip, phase2_candidate


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


def _replacement(old: Path, target: Path) -> Path:
    shutil.copytree(old, target)
    provenance = json.loads((target / "bridge/BRIDGE.json").read_bytes())
    provenance["source_commit"] = "b" * 40
    provenance["source_tree"] = "d" * 40
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


def _setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path, str, str, str]:
    old = phase2_candidate(tmp_path)
    new = _replacement(old, tmp_path / "replacement")
    data = tmp_path / "repository-data"
    generate(old, data, BASE)
    initialize_development(data / "development-data", BASE, old)
    state = tmp_path / "state"
    url = f"sqlite:///{state / 'database/kodi-lab.db'}"
    initialize(state, url)
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
        == "0057_development_capability_amendments"
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
