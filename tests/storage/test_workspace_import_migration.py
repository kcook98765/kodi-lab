"""Import receipts upgrade the real schema and prohibit lossy downgrade."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text

from kodi_lab.application.workspace_imports import ImportApplyInput, ImportInput
from kodi_lab.storage.engine import create_database_engine
from kodi_lab.storage.metadata import Base
from tests.application import test_workspace_source_capture as capture_fixture
from tests.application.test_workspace_imports import _environment, _request
from tests.application.test_workspace_source_capture import WORKSPACE_ID
from tests.storage.test_migrations import (  # noqa: F401
    migration_setup,
    migration_templates,
)

REVISION = "0073_workspace_imports"
PREDECESSOR = "0072_kodi_log_capability"


def test_import_empty_upgrade_and_downgrade_preserve_existing_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    engine = create_database_engine(url)
    before = set(inspect(engine).get_table_names())
    command.upgrade(config, REVISION)
    assert set(inspect(engine).get_table_names()) == before | {
        "workspace_import_operations"
    }
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
    command.downgrade(config, PREDECESSOR)
    assert set(inspect(engine).get_table_names()) == before
    command.upgrade(config, REVISION)
    engine.dispose()


def test_real_import_receipt_on_upgraded_schema_refuses_lossy_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    command.upgrade(config, REVISION)
    engine = create_database_engine(url)
    # The application fixture now uses the migrated engine. create_all is
    # disabled so current ORM metadata cannot repair a missing migration.
    monkeypatch.setattr(capture_fixture, "create_engine", lambda _url: engine)
    monkeypatch.setattr(Base.metadata, "create_all", lambda _engine: None)
    workspaces, tools, source, _sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    (source / "default.py").write_text("migrated import\n")
    preview = tools.workspace_import_preview(
        principal, ImportInput(workspace_id=WORKSPACE_ID, expected_generation=0)
    )
    assert preview.eligible
    request = _request(tools, principal)
    receipt = tools.workspace_import(principal, request)
    assert receipt.state == "succeeded" and receipt.generation == 1
    assert (
        workspaces.read_file(WORKSPACE_ID, "default.py").content == "migrated import\n"
    )
    with pytest.raises(RuntimeError, match="cannot be safely downgraded"):
        command.downgrade(config, PREDECESSOR)
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == REVISION
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM workspace_import_operations"))
            == 1
        )
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    assert tools.workspace_import(principal, request) == receipt
    engine.dispose()


def test_reviewed_tombstone_receipt_runs_on_existing_migrated_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    command.upgrade(config, REVISION)
    engine = create_database_engine(url)
    monkeypatch.setattr(capture_fixture, "create_engine", lambda _url: engine)
    monkeypatch.setattr(Base.metadata, "create_all", lambda _engine: None)
    workspaces, tools, source, _sessions, _admin, principal, _client = _environment(
        tmp_path
    )
    (source / "default.py").unlink()
    preview = tools.workspace_import_preview(
        principal,
        ImportInput(
            workspace_id=WORKSPACE_ID, expected_generation=0, mode="reviewed_tree"
        ),
    )
    operation = ImportApplyInput(
        workspace_id=WORKSPACE_ID,
        expected_generation=0,
        mode="reviewed_tree",
        source_digest=preview.source_digest,
        preview_digest=preview.preview_digest,
        request_id=uuid4(),
    )
    result = tools.workspace_import(principal, operation)
    assert result.state == "succeeded" and result.matches_source
    assert result.files[0].operation == "deleted"
    assert workspaces.get(WORKSPACE_ID).generation == 1
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    with pytest.raises(RuntimeError, match="cannot be safely downgraded"):
        command.downgrade(config, PREDECESSOR)
    assert tools.workspace_import(principal, operation) == result
    engine.dispose()
