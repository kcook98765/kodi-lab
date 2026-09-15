from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from kodi_lab.storage.engine import create_database_engine


def migration_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Config, str]:
    project_root = Path(__file__).parents[2]
    data_root = tmp_path / "data"
    (data_root / "database").mkdir(parents=True)
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(data_root))
    monkeypatch.delenv("KODI_LAB_RUNTIME_ROOT", raising=False)
    config = Config(str(project_root / "alembic.ini"))
    database_url = f"sqlite:///{data_root / 'database' / 'kodi-lab.db'}"
    return config, database_url


def test_empty_database_upgrades_to_runtime_catalog_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)

    command.upgrade(config, "head")

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == {
        "alembic_version",
        "managed_instances",
        "runtime_definitions",
        "runtime_container_artifacts",
        "runtime_execution_profiles",
        "runtime_revisions",
        "targets",
    }
    managed_foreign_keys = {
        foreign_key["referred_table"]: foreign_key
        for foreign_key in inspector.get_foreign_keys("managed_instances")
    }
    assert managed_foreign_keys["targets"]["options"] == {"ondelete": "CASCADE"}
    assert managed_foreign_keys["runtime_revisions"]["options"] == {
        "ondelete": "RESTRICT"
    }
    revision_foreign_key = inspector.get_foreign_keys("runtime_revisions")[0]
    assert revision_foreign_key["referred_table"] == "runtime_definitions"
    assert revision_foreign_key["options"] == {"ondelete": "RESTRICT"}
    artifact_foreign_key = inspector.get_foreign_keys("runtime_container_artifacts")[0]
    assert artifact_foreign_key["referred_table"] == "runtime_revisions"
    assert artifact_foreign_key["options"] == {"ondelete": "RESTRICT"}
    profile_foreign_key = inspector.get_foreign_keys("runtime_execution_profiles")[0]
    assert profile_foreign_key["referred_table"] == "runtime_revisions"
    assert profile_foreign_key["options"] == {"ondelete": "RESTRICT"}
    unique_constraints = {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints("runtime_revisions")
    }
    assert unique_constraints["uq_runtime_revision_number"] == [
        "runtime_definition_id",
        "revision_number",
    ]
    engine.dispose()

    command.check(config)


def test_explicit_0001_upgrades_to_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0001_targets_instances")

    engine = create_database_engine(database_url)
    assert "runtime_revision" in {
        column["name"] for column in inspect(engine).get_columns("managed_instances")
    }
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_database_engine(database_url)
    columns = {
        column["name"] for column in inspect(engine).get_columns("managed_instances")
    }
    assert "runtime_revision" not in columns
    assert "runtime_revision_id" in columns
    engine.dispose()


def test_explicit_0002_upgrades_to_artifact_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0002_runtime_catalog")
    engine = create_database_engine(database_url)
    assert "runtime_container_artifacts" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    assert "runtime_container_artifacts" in inspect(engine).get_table_names()
    engine.dispose()
    command.check(config)


def test_prior_head_upgrades_to_execution_profile_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0003_runtime_container_artifacts")
    engine = create_database_engine(database_url)
    assert "runtime_execution_profiles" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    assert "runtime_execution_profiles" in inspect(engine).get_table_names()
    engine.dispose()
    command.check(config)

    command.downgrade(config, "0003_runtime_container_artifacts")
    command.upgrade(config, "head")
    command.check(config)


def test_populated_0001_migration_refuses_ambiguous_legacy_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0001_targets_instances")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO targets (
                    id, name, target_type, kodi_version,
                    availability, health, connection_state
                ) VALUES (
                    '00000000-0000-0000-0000-000000000001', 'legacy',
                    'managed', '21.3', 'unavailable', 'warning', 'disconnected'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO managed_instances (
                    target_id, instance_type, runtime_revision,
                    runtime_state, start_on_host_boot
                ) VALUES (
                    '00000000-0000-0000-0000-000000000001', 'persistent',
                    'legacy-free-form', 'stopped', 0
                )
                """
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="predates the runtime catalog"):
        command.upgrade(config, "head")

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "runtime_definitions" not in inspector.get_table_names()
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT runtime_revision FROM managed_instances"))
            == "legacy-free-form"
        )
    engine.dispose()


def test_head_downgrades_to_0001_and_upgrades_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "head")

    command.downgrade(config, "0001_targets_instances")

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert "runtime_definitions" not in inspector.get_table_names()
    assert "runtime_revision" in {
        column["name"] for column in inspector.get_columns("managed_instances")
    }
    engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


def test_populated_artifact_mapping_refuses_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions (id, slug, kodi_version) "
                "VALUES ('00000000-0000-0000-0000-000000000001', 'kodi-21', '21')"
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO runtime_revisions (
                    id, runtime_definition_id, revision_number, source_kind,
                    source_reference, content_digest, manifest_reference,
                    manifest_digest, built_at, approval_state,
                    requires_trusted_build_confirmation
                ) VALUES (
                    '00000000-0000-0000-0000-000000000002',
                    '00000000-0000-0000-0000-000000000001', 1, 'test', 'test',
                    :digest, 'test', :digest, '2026-09-14T00:00:00Z',
                    'not_approved', 0
                )
                """
            ),
            {"digest": "sha256:" + ("a" * 64)},
        )
        connection.execute(
            text(
                "INSERT INTO runtime_container_artifacts "
                "(runtime_revision_id, image_id) VALUES "
                "('00000000-0000-0000-0000-000000000002', :image_id)"
            ),
            {"image_id": "sha256:" + ("b" * 64)},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated runtime-container artifact"):
        command.downgrade(config, "0002_runtime_catalog")

    engine = create_database_engine(database_url)
    assert "runtime_container_artifacts" in inspect(engine).get_table_names()
    engine.dispose()
