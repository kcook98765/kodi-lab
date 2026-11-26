"""Application lifecycle tests for the broker's rowless installer replay guard."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config

from kodi_lab import remote_lifecycle
from kodi_lab.application.runtime_installations import preview_acknowledgement
from kodi_lab.broker.reviewed_installations import (
    DockerReviewedInstallationWorker,
    ReviewedWorkerError,
)
from kodi_lab.broker.worker_replay import persist_terminal_operation
from kodi_lab.domain import RuntimeRecipeInstallationState
from kodi_lab.host_bootstrap import generate_master_key, initialize_state
from kodi_lab.repository.development import DevelopmentRepositoryService
from kodi_lab.repository.development import initialize as initialize_development
from kodi_lab.repository.stable import generate as generate_stable
from kodi_lab.runtime.preview import PreviewAcknowledgement
from kodi_lab.runtime.recipes import load_installation_recipe
from kodi_lab.storage import packaged_migrations
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
from tests.broker.test_reviewed_runtime_installation_operation import FakeContainers
from tests.broker.test_reviewed_runtime_installation_operation import backend as backend
from tests.migration_support import migration_config
from tests.repository.test_development import BASE as REPOSITORY_BASE
from tests.repository.test_development import phase2_candidate
from tests.runtime.test_ostree_installation import ready

HEAD = "0002_worker_replay"


@pytest.fixture(autouse=True)
def source_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def package_config(url: str) -> Iterator[Config]:
        config = migration_config(Path(url.removeprefix("sqlite:///")))
        yield config

    monkeypatch.setattr(packaged_migrations, "packaged_config", package_config)
    monkeypatch.setattr(remote_lifecycle, "packaged_config", package_config)
    monkeypatch.delenv("KODI_LAB_REPOSITORY_DATA_ROOT", raising=False)
    monkeypatch.delenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", raising=False)


def database_url(root: Path) -> str:
    return f"sqlite:///{root / remote_lifecycle.DATABASE}"


@pytest.mark.parametrize("with_repository", [False, True])
def test_clean_recovery_with_fresh_broker_volume_keeps_replay_guard(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_repository: bool,
) -> None:
    fixture, containers = backend
    original = tmp_path / "original-application"
    recovered = tmp_path / "recovered-application"
    assert remote_lifecycle.initialize(original, database_url(original)) == HEAD
    engine = create_database_engine(database_url(original))
    original_sessions = create_session_factory(engine)
    if with_repository:
        bundle = phase2_candidate(tmp_path)
        original_repository = tmp_path / "original-repository"
        generate_stable(bundle, original_repository, REPOSITORY_BASE)
        initialize_development(
            original_repository / "development-data", REPOSITORY_BASE, bundle
        )
        DevelopmentRepositoryService(
            original_sessions, original_repository, REPOSITORY_BASE
        ).reconcile()
        monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(original_repository))
        monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", REPOSITORY_BASE)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(original))
    worker = DockerReviewedInstallationWorker(
        client_factory=fixture._client_factory, runtime_root=fixture._runtime_root
    )
    ready_operation = ready(original_sessions)
    rowless, failed = uuid4(), uuid4()
    reviewed = load_installation_recipe("omega")
    with original_sessions.begin() as session:
        RuntimeRecipeInstallationRepository(session).create_failed_before_start(
            operation_id=failed,
            recipe_id=reviewed.recipe.recipe_id,
            recipe_revision=reviewed.recipe.recipe_revision,
            recipe_digest=reviewed.digest,
            generation="omega",
            code="interrupted",
            now=datetime.now(UTC),
        )

    assert worker.launch("omega", rowless)
    containers.items[0].exit(137)
    worker.retire_terminal_workers()
    retired = worker._receipts.directory("retired") / f"{rowless.hex}.json"
    os.utime(retired, (0, 0))
    worker.retire_terminal_workers()
    assert not retired.exists() and not containers.items
    assert worker._recorded_operation(rowless) is None
    assert worker._terminal_operation_recorded(rowless)
    with remote_lifecycle.lifecycle_lock(original):
        archive = remote_lifecycle.backup(original, database_url(original))
    manifest = remote_lifecycle._verify_backup(original, archive)
    assert manifest["complete"] is True
    assert bool(manifest.get("repository_identity")) is with_repository
    engine.dispose()

    recovered.mkdir(mode=0o700)
    (recovered / "backups").mkdir(mode=0o700)
    shutil.copytree(original / "backups" / archive, recovered / "backups" / archive)
    if with_repository:
        recovered_repository = tmp_path / "recovered-repository"
        generate_stable(bundle, recovered_repository, REPOSITORY_BASE)
        initialize_development(
            recovered_repository / "development-data", REPOSITORY_BASE, bundle
        )
        monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(recovered_repository))
    assert (
        remote_lifecycle.recover_clean(
            recovered, database_url(recovered), archive, archive
        )
        == HEAD
    )
    assert (
        remote_lifecycle.validate(recovered, database_url(recovered), require_head=True)
        == HEAD
    )
    assert remote_lifecycle._key_digest(recovered) == remote_lifecycle._key_digest(
        original
    )
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(recovered))
    fresh_socket = tmp_path / "empty-broker-socket"
    fresh_socket.mkdir(mode=0o700)
    restarted = DockerReviewedInstallationWorker(
        client_factory=fixture._client_factory, runtime_root=fresh_socket
    )
    assert restarted._recorded_operation(rowless) is None
    with pytest.raises(ReviewedWorkerError, match="worker launch already recorded"):
        restarted.launch("omega", rowless)
    row = restarted._recorded_operation(failed)
    assert row is not None and row.state is RuntimeRecipeInstallationState.FAILED
    with pytest.raises(ReviewedWorkerError, match="installation already recorded"):
        restarted.launch("omega", failed)
    ready_row = restarted._recorded_operation(ready_operation)
    assert (
        ready_row is not None
        and ready_row.state is RuntimeRecipeInstallationState.READY
    )
    with pytest.raises(ReviewedWorkerError, match="installation already recorded"):
        restarted.launch(
            "piers",
            ready_operation,
            preview_acknowledgement=PreviewAcknowledgement.model_validate(
                preview_acknowledgement()
            ),
        )
    assert not restarted._terminal_operation_recorded(ready_operation)
    assert not restarted._terminal_operation_recorded(failed)
    assert not restarted._receipts.pending() and not containers.items
    assert restarted.launch("omega", uuid4())


def test_supported_upgrade_adds_indexed_immutable_uuid_storage(tmp_path: Path) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    generate_master_key(state)
    database = state / remote_lifecycle.DATABASE
    config = migration_config(database)
    previous_umask = os.umask(0o077)
    try:
        command.upgrade(config, "0001_foundation")
    finally:
        os.umask(previous_umask)
    previous, current, archive = remote_lifecycle.upgrade(state, database_url(state))
    assert (previous, current) == ("0001_foundation", HEAD)
    assert archive is not None
    assert remote_lifecycle._verify_backup(state, archive)["revision"] == previous

    operation = uuid4()
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "kodi_lab.broker.worker_replay",
            str(state),
            str(operation),
        ],
        check=True,
        timeout=5,
    )
    persist_terminal_operation(state, operation)
    table = "runtime_worker_terminal_operations"
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(f"SELECT operation_id FROM {table}").fetchall() == [
            (operation.bytes,)
        ]
        plan = connection.execute(
            f"EXPLAIN QUERY PLAN SELECT 1 FROM {table} WHERE operation_id=?",
            (operation.bytes,),
        ).fetchone()
        assert "USING PRIMARY KEY" in plan[3]
        with pytest.raises(
            sqlite3.IntegrityError, match="runtime_worker_terminal_uuid"
        ):
            connection.execute(f"INSERT INTO {table} VALUES (?)", (b"short",))
        with pytest.raises(
            sqlite3.IntegrityError, match="runtime_worker_terminal_immutable"
        ):
            connection.execute(f"DELETE FROM {table}")
        with pytest.raises(
            sqlite3.IntegrityError, match="runtime_worker_terminal_immutable"
        ):
            connection.execute(f"UPDATE {table} SET operation_id=?", (uuid4().bytes,))
        connection.rollback()
    with pytest.raises(RuntimeError, match="replay history cannot be downgraded"):
        command.downgrade(config, "0001_foundation")
