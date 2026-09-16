from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, String, inspect, text
from sqlalchemy.exc import IntegrityError

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
        "fixture_definitions",
        "global_heavy_operation_lease",
        "managed_instances",
        "managed_instance_delete_operations",
        "managed_instance_lifecycle_operations",
        "managed_instance_mutation_leases",
        "reset_operations",
        "runtime_definitions",
        "runtime_container_artifacts",
        "runtime_execution_profiles",
        "runtime_revisions",
        "runtime_validation_evidence",
        "snapshot_create_operations",
        "snapshot_delete_operations",
        "snapshot_restore_operations",
        "snapshots",
        "targets",
        "web_admin_credentials",
        "web_sessions",
    }
    managed_foreign_keys = {
        foreign_key["referred_table"]: foreign_key
        for foreign_key in inspector.get_foreign_keys("managed_instances")
    }
    assert managed_foreign_keys["targets"]["options"] == {"ondelete": "CASCADE"}
    assert managed_foreign_keys["runtime_revisions"]["options"] == {
        "ondelete": "RESTRICT"
    }
    assert managed_foreign_keys["fixture_definitions"]["options"] == {
        "ondelete": "RESTRICT"
    }
    assert inspector.get_foreign_keys("reset_operations") == []
    reset_indexes = {
        index["name"]: index for index in inspector.get_indexes("reset_operations")
    }
    assert reset_indexes["uq_reset_operations_active_instance"]["unique"] == 1
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


def test_0007_upgrades_to_reset_operations_and_empty_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0007_fixture_definitions")
    engine = create_database_engine(database_url)
    assert "reset_operations" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    assert "reset_operations" in inspect(engine).get_table_names()
    engine.dispose()
    command.check(config)
    command.downgrade(config, "0007_fixture_definitions")
    command.upgrade(config, "head")
    command.check(config)

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) "
                "VALUES ('81000000-0000-4000-8000-000000000001', 'reset-test', "
                "'21.3', 'current')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions (id, runtime_definition_id, "
                "revision_number, source_kind, source_reference, content_digest, "
                "manifest_reference, manifest_digest, built_at, approval_state, "
                "requires_trusted_build_confirmation) VALUES "
                "('81000000-0000-4000-8000-000000000002', "
                "'81000000-0000-4000-8000-000000000001', 1, 'test', 'test', "
                ":digest, 'test', :digest, '2026-09-15T00:00:00Z', "
                "'not_approved', 0)"
            ),
            {"digest": "sha256:" + "a" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO targets (id, name, target_type, kodi_version, "
                "availability, health, connection_state) VALUES "
                "('81000000-0000-4000-8000-000000000003', 'reset', 'managed', "
                "'21.3', 'unavailable', 'warning', 'disconnected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances (target_id, instance_type, "
                "runtime_revision_id, runtime_state, start_on_host_boot) VALUES "
                "('81000000-0000-4000-8000-000000000003', 'disposable', "
                "'81000000-0000-4000-8000-000000000002', 'stopped', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reset_operations (id, managed_instance_id, requested_at, "
                "completed_at, state, phase, reconciliation_required) VALUES "
                "('81000000-0000-4000-8000-000000000004', "
                "'81000000-0000-4000-8000-000000000003', "
                "'2026-09-15T00:00:00.000000Z', "
                "'2026-09-15T00:01:00.000000Z', 'succeeded', 'succeeded', 0)"
            )
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="populated reset operations"):
        command.downgrade(config, "0007_fixture_definitions")


def test_0008_upgrades_to_fixture_taxonomy_and_empty_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0008_reset_operations")

    engine = create_database_engine(database_url)
    before = next(
        column
        for column in inspect(engine).get_columns("fixture_definitions")
        if column["name"] == "content_category"
    )
    assert isinstance(before["type"], String)
    assert before["type"].length == 5
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    after = next(
        column
        for column in inspect(engine).get_columns("fixture_definitions")
        if column["name"] == "content_category"
    )
    assert isinstance(after["type"], String)
    assert after["type"].length == 16
    engine.dispose()
    command.check(config)

    command.downgrade(config, "0008_reset_operations")
    command.upgrade(config, "head")
    command.check(config)


def test_populated_mixed_fixture_assignment_and_reset_survive_0009(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0008_reset_operations")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-15T00:00:00.000000Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('82000000-0000-4000-8000-000000000001', 'taxonomy-test', "
                "'21.3', 'current')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions (id, runtime_definition_id, "
                "revision_number, source_kind, source_reference, content_digest, "
                "manifest_reference, manifest_digest, built_at, approval_state, "
                "requires_trusted_build_confirmation) VALUES "
                "('82000000-0000-4000-8000-000000000002', "
                "'82000000-0000-4000-8000-000000000001', 1, 'test', 'test', "
                ":digest, 'test', :digest, :timestamp, 'not_approved', 0)"
            ),
            {"digest": "sha256:" + "a" * 64, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO targets (id, name, target_type, kodi_version, "
                "availability, health, connection_state) VALUES "
                "('82000000-0000-4000-8000-000000000003', 'taxonomy', 'managed', "
                "'21.3', 'unavailable', 'warning', 'disconnected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO fixture_definitions "
                "(id, slug, fixture_version, size_category, content_category, "
                "content_digest, compatible_kodi_version, description) VALUES "
                "('82000000-0000-4000-8000-000000000004', 'retained-mixed', 1, "
                "'small', 'mixed', :digest, '21.3', 'retained mixed fixture')"
            ),
            {"digest": "sha256:" + "b" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances (target_id, instance_type, "
                "runtime_revision_id, runtime_state, start_on_host_boot, "
                "fixture_definition_id) VALUES "
                "('82000000-0000-4000-8000-000000000003', 'disposable', "
                "'82000000-0000-4000-8000-000000000002', 'stopped', 0, "
                "'82000000-0000-4000-8000-000000000004')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reset_operations (id, managed_instance_id, requested_at, "
                "completed_at, state, phase, reconciliation_required) VALUES "
                "('82000000-0000-4000-8000-000000000005', "
                "'82000000-0000-4000-8000-000000000003', :timestamp, :timestamp, "
                "'succeeded', 'succeeded', 0)"
            ),
            {"timestamp": timestamp},
        )
        before = {
            table: connection.execute(
                text(f'SELECT * FROM "{table}" ORDER BY 1')
            ).fetchall()
            for table in (
                "fixture_definitions",
                "managed_instances",
                "reset_operations",
            )
        }
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        after = {
            table: connection.execute(
                text(f'SELECT * FROM "{table}" ORDER BY 1')
            ).fetchall()
            for table in before
        }
    assert after == before
    engine.dispose()


def test_fixture_taxonomy_constraint_accepts_only_mixed_and_movies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    statement = text(
        "INSERT INTO fixture_definitions "
        "(id, slug, fixture_version, size_category, content_category, "
        "content_digest, compatible_kodi_version, description) VALUES "
        "(:id, :slug, 1, 'small', :category, :digest, '21.3', 'taxonomy')"
    )
    with engine.begin() as connection:
        for ordinal, category in enumerate(("mixed", "movies"), start=1):
            connection.execute(
                statement,
                {
                    "id": f"83000000-0000-4000-8000-{ordinal:012d}",
                    "slug": f"category-{category}",
                    "category": category,
                    "digest": "sha256:" + f"{ordinal:064x}",
                },
            )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            statement,
            {
                "id": "83000000-0000-4000-8000-000000000003",
                "slug": "category-unknown",
                "category": "unknown",
                "digest": "sha256:" + "3" * 64,
            },
        )
    engine.dispose()


def test_movies_fixture_refuses_lossy_0009_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO fixture_definitions "
                "(id, slug, fixture_version, size_category, content_category, "
                "content_digest, compatible_kodi_version, description) VALUES "
                "('84000000-0000-4000-8000-000000000001', 'movies-retained', 1, "
                "'small', 'movies', :digest, '21.3', 'movies retained')"
            ),
            {"digest": "sha256:" + "d" * 64},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated Movies definitions"):
        command.downgrade(config, "0008_reset_operations")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT slug, content_category FROM fixture_definitions "
                "WHERE id = '84000000-0000-4000-8000-000000000001'"
            )
        ).one() == ("movies-retained", "movies")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0009_fixture_content_taxonomy"
        )
    engine.dispose()


def test_0009_upgrades_to_tv_taxonomy_and_empty_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, _database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0009_fixture_content_taxonomy")
    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "0009_fixture_content_taxonomy")
    command.upgrade(config, "head")
    command.check(config)


def test_existing_movies_definition_survives_0010_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0009_fixture_content_taxonomy")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO fixture_definitions "
                "(id, slug, fixture_version, size_category, content_category, "
                "content_digest, compatible_kodi_version, description) VALUES "
                "('85500000-0000-4000-8000-000000000001', 'movies-existing', 1, "
                "'small', 'movies', :digest, '21.3', 'existing Movies fixture')"
            ),
            {"digest": "sha256:" + "5" * 64},
        )
        before = connection.execute(
            text(
                "SELECT * FROM fixture_definitions "
                "WHERE id = '85500000-0000-4000-8000-000000000001'"
            )
        ).one()
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        after = connection.execute(
            text(
                "SELECT * FROM fixture_definitions "
                "WHERE id = '85500000-0000-4000-8000-000000000001'"
            )
        ).one()
    assert after == before
    engine.dispose()


def test_tv_taxonomy_constraint_accepts_only_three_supported_categories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    statement = text(
        "INSERT INTO fixture_definitions "
        "(id, slug, fixture_version, size_category, content_category, "
        "content_digest, compatible_kodi_version, description) VALUES "
        "(:id, :slug, 1, 'small', :category, :digest, '21.3', 'taxonomy')"
    )
    with engine.begin() as connection:
        for ordinal, category in enumerate(("mixed", "movies", "tv"), start=1):
            connection.execute(
                statement,
                {
                    "id": f"85000000-0000-4000-8000-{ordinal:012d}",
                    "slug": f"supported-{category}",
                    "category": category,
                    "digest": "sha256:" + f"{ordinal:064x}",
                },
            )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            statement,
            {
                "id": "85000000-0000-4000-8000-000000000004",
                "slug": "unsupported-category",
                "category": "unknown",
                "digest": "sha256:" + "4" * 64,
            },
        )
    engine.dispose()


def test_tv_fixture_refuses_lossy_0010_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO fixture_definitions "
                "(id, slug, fixture_version, size_category, content_category, "
                "content_digest, compatible_kodi_version, description) VALUES "
                "('86000000-0000-4000-8000-000000000001', 'tv-retained', 1, "
                "'small', 'tv', :digest, '21.3', 'TV retained')"
            ),
            {"digest": "sha256:" + "e" * 64},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated TV definitions"):
        command.downgrade(config, "0009_fixture_content_taxonomy")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT slug, content_category FROM fixture_definitions "
                "WHERE id = '86000000-0000-4000-8000-000000000001'"
            )
        ).one() == ("tv-retained", "tv")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0010_fixture_tv_taxonomy"
        )
    engine.dispose()


def test_0010_upgrades_to_medium_taxonomy_and_empty_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0010_fixture_tv_taxonomy")
    engine = create_database_engine(database_url)
    before = next(
        column
        for column in inspect(engine).get_columns("fixture_definitions")
        if column["name"] == "size_category"
    )
    assert isinstance(before["type"], String)
    assert before["type"].length == 5
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    after = next(
        column
        for column in inspect(engine).get_columns("fixture_definitions")
        if column["name"] == "size_category"
    )
    assert isinstance(after["type"], String)
    assert after["type"].length == 16
    engine.dispose()
    command.check(config)
    command.downgrade(config, "0010_fixture_tv_taxonomy")
    command.upgrade(config, "head")
    command.check(config)


def test_medium_size_constraint_accepts_only_small_and_medium(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    statement = text(
        "INSERT INTO fixture_definitions "
        "(id, slug, fixture_version, size_category, content_category, "
        "content_digest, compatible_kodi_version, description) VALUES "
        "(:id, :slug, 1, :size, 'mixed', :digest, '21.3', 'size taxonomy')"
    )
    with engine.begin() as connection:
        for ordinal, size in enumerate(("small", "medium"), start=1):
            connection.execute(
                statement,
                {
                    "id": f"87000000-0000-4000-8000-{ordinal:012d}",
                    "slug": f"supported-{size}",
                    "size": size,
                    "digest": "sha256:" + f"{ordinal:064x}",
                },
            )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            statement,
            {
                "id": "87000000-0000-4000-8000-000000000003",
                "slug": "unsupported-large",
                "size": "large",
                "digest": "sha256:" + "3" * 64,
            },
        )
    engine.dispose()


def test_medium_fixture_refuses_lossy_0011_downgrade_and_remains_intact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO fixture_definitions "
                "(id, slug, fixture_version, size_category, content_category, "
                "content_digest, compatible_kodi_version, description) VALUES "
                "('88000000-0000-4000-8000-000000000001', 'medium-retained', "
                "1, 'medium', 'mixed', :digest, '21.3', 'Medium retained')"
            ),
            {"digest": "sha256:" + "8" * 64},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated Medium definitions"):
        command.downgrade(config, "0010_fixture_tv_taxonomy")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT slug, size_category FROM fixture_definitions "
                "WHERE id = '88000000-0000-4000-8000-000000000001'"
            )
        ).one() == ("medium-retained", "medium")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0011_fixture_medium_taxonomy"
        )
    engine.dispose()


def test_0011_active_reset_is_seeded_into_phase7_mutation_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0011_fixture_medium_taxonomy")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-15T00:00:00.000000Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('89000000-0000-4000-8000-000000000001', 'phase7-test', "
                "'21.3', 'current')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions (id, runtime_definition_id, "
                "revision_number, source_kind, source_reference, content_digest, "
                "manifest_reference, manifest_digest, built_at, approval_state, "
                "requires_trusted_build_confirmation) VALUES "
                "('89000000-0000-4000-8000-000000000002', "
                "'89000000-0000-4000-8000-000000000001', 1, 'test', 'test', "
                ":digest, 'test', :digest, :timestamp, 'not_approved', 0)"
            ),
            {"digest": "sha256:" + "9" * 64, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO targets (id, name, target_type, kodi_version, "
                "availability, health, connection_state) VALUES "
                "('89000000-0000-4000-8000-000000000003', 'phase7', 'managed', "
                "'21.3', 'unavailable', 'warning', 'disconnected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances (target_id, instance_type, "
                "runtime_revision_id, runtime_state, start_on_host_boot) VALUES "
                "('89000000-0000-4000-8000-000000000003', 'disposable', "
                "'89000000-0000-4000-8000-000000000002', 'stopped', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO reset_operations (id, managed_instance_id, "
                "requested_at, state, phase, reconciliation_required) VALUES "
                "('89000000-0000-4000-8000-000000000004', "
                "'89000000-0000-4000-8000-000000000003', :timestamp, "
                "'queued', 'queued', 0)"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT operation_kind, operation_id, lease_token_digest "
                "FROM managed_instance_mutation_leases"
            )
        ).one() == (
            "reset",
            "89000000-0000-4000-8000-000000000004",
            None,
        )
    engine.dispose()


def test_populated_phase7_snapshot_state_refuses_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-15T00:00:00.000000Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('89500000-0000-4000-8000-000000000001', 'snapshot-test', "
                "'21.3', 'current')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions (id, runtime_definition_id, "
                "revision_number, source_kind, source_reference, content_digest, "
                "manifest_reference, manifest_digest, built_at, approval_state, "
                "requires_trusted_build_confirmation) VALUES "
                "('89500000-0000-4000-8000-000000000002', "
                "'89500000-0000-4000-8000-000000000001', 1, 'test', 'test', "
                ":digest, 'test', :digest, :timestamp, 'not_approved', 0)"
            ),
            {"digest": "sha256:" + "5" * 64, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO targets (id, name, target_type, kodi_version, "
                "availability, health, connection_state) VALUES "
                "('89500000-0000-4000-8000-000000000003', 'snapshot', 'managed', "
                "'21.3', 'unavailable', 'warning', 'disconnected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances (target_id, instance_type, "
                "runtime_revision_id, runtime_state, start_on_host_boot) VALUES "
                "('89500000-0000-4000-8000-000000000003', 'disposable', "
                "'89500000-0000-4000-8000-000000000002', 'stopped', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO snapshots (id, source_managed_instance_id, "
                "original_source_managed_instance_id, source_binding, "
                "runtime_revision_id, captured_at, manifest_version, state, "
                "validation_result) VALUES "
                "('89500000-0000-4000-8000-000000000004', "
                "'89500000-0000-4000-8000-000000000003', "
                "'89500000-0000-4000-8000-000000000003', 'attached', "
                "'89500000-0000-4000-8000-000000000002', :timestamp, 1, "
                "'creating', 'pending')"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated Phase 7 snapshot state"):
        command.downgrade(config, "0011_fixture_medium_taxonomy")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM snapshots")) == 1
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0012_disposable_snapshot_creation"
        )
    engine.dispose()


def test_populated_snapshot_restore_state_refuses_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0013_disposable_snapshot_restore")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T00:00:00.000000Z"
    digest = "sha256:" + "6" * 64
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('89600000-0000-4000-8000-000000000001', 'restore-test', "
                "'21.3', 'current')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions (id, runtime_definition_id, "
                "revision_number, source_kind, source_reference, content_digest, "
                "manifest_reference, manifest_digest, built_at, approval_state, "
                "requires_trusted_build_confirmation) VALUES "
                "('89600000-0000-4000-8000-000000000002', "
                "'89600000-0000-4000-8000-000000000001', 1, 'test', 'test', "
                ":digest, 'test', :digest, :timestamp, 'not_approved', 0)"
            ),
            {"digest": digest, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO targets (id, name, target_type, kodi_version, "
                "availability, health, connection_state) VALUES "
                "('89600000-0000-4000-8000-000000000003', 'restore', 'managed', "
                "'21.3', 'unavailable', 'warning', 'disconnected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances (target_id, instance_type, "
                "runtime_revision_id, runtime_state, start_on_host_boot) VALUES "
                "('89600000-0000-4000-8000-000000000003', 'disposable', "
                "'89600000-0000-4000-8000-000000000002', 'stopped', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO snapshots (id, source_managed_instance_id, "
                "runtime_revision_id, captured_at, prior_runtime_state, "
                "manifest_version, archive_digest, manifest_digest, archive_size, "
                "member_count, state, validation_at, validation_result) VALUES "
                "('89600000-0000-4000-8000-000000000004', "
                "'89600000-0000-4000-8000-000000000003', "
                "'89600000-0000-4000-8000-000000000002', :timestamp, 'stopped', "
                "1, :digest, :digest, 10240, 1, 'available', :timestamp, 'passed')"
            ),
            {"digest": digest, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO snapshot_restore_operations "
                "(id, snapshot_id, managed_instance_id, requested_at, state, "
                "phase, reconciliation_required) VALUES "
                "('89600000-0000-4000-8000-000000000005', "
                "'89600000-0000-4000-8000-000000000004', "
                "'89600000-0000-4000-8000-000000000003', :timestamp, "
                "'queued', 'queued', 0)"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated snapshot restore state"):
        command.downgrade(config, "0012_disposable_snapshot_creation")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0013_disposable_snapshot_restore"
        )
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM snapshot_restore_operations"))
            == 1
        )
    engine.dispose()


def test_0014_preserves_history_across_snapshot_delete_and_refuses_lossy_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0013_disposable_snapshot_restore")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T00:00:00.000000Z"
    digest = "sha256:" + "7" * 64
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('89700000-0000-4000-8000-000000000001', 'delete-test', "
                "'21.3', 'current')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_revisions (id, runtime_definition_id, "
                "revision_number, source_kind, source_reference, content_digest, "
                "manifest_reference, manifest_digest, built_at, approval_state, "
                "requires_trusted_build_confirmation) VALUES "
                "('89700000-0000-4000-8000-000000000002', "
                "'89700000-0000-4000-8000-000000000001', 1, 'test', 'test', "
                ":digest, 'test', :digest, :timestamp, 'not_approved', 0)"
            ),
            {"digest": digest, "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO targets (id, name, target_type, kodi_version, "
                "availability, health, connection_state) VALUES "
                "('89700000-0000-4000-8000-000000000003', 'delete', 'managed', "
                "'21.3', 'unavailable', 'warning', 'disconnected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances (target_id, instance_type, "
                "runtime_revision_id, runtime_state, start_on_host_boot) VALUES "
                "('89700000-0000-4000-8000-000000000003', 'disposable', "
                "'89700000-0000-4000-8000-000000000002', 'stopped', 0)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO snapshots (id, source_managed_instance_id, "
                "runtime_revision_id, captured_at, prior_runtime_state, "
                "manifest_version, archive_digest, manifest_digest, archive_size, "
                "member_count, state, validation_at, validation_result) VALUES "
                "('89700000-0000-4000-8000-000000000004', "
                "'89700000-0000-4000-8000-000000000003', "
                "'89700000-0000-4000-8000-000000000002', :timestamp, 'stopped', "
                "1, :digest, :digest, 10240, 1, 'available', :timestamp, 'passed')"
            ),
            {"digest": digest, "timestamp": timestamp},
        )
        for table, operation_id in (
            ("snapshot_create_operations", "89700000-0000-4000-8000-000000000005"),
            ("snapshot_restore_operations", "89700000-0000-4000-8000-000000000006"),
        ):
            connection.execute(
                text(
                    f"INSERT INTO {table} (id, snapshot_id, managed_instance_id, "
                    "requested_at, completed_at, state, phase, "
                    "reconciliation_required) VALUES (:id, "
                    "'89700000-0000-4000-8000-000000000004', "
                    "'89700000-0000-4000-8000-000000000003', :timestamp, "
                    ":timestamp, 'succeeded', 'succeeded', 0)"
                ),
                {"id": operation_id, "timestamp": timestamp},
            )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        assert not any(
            foreign_key["constrained_columns"] == ["snapshot_id"]
            for table in (
                "snapshot_create_operations",
                "snapshot_restore_operations",
            )
            for foreign_key in inspect(connection).get_foreign_keys(table)
        )
        connection.execute(
            text(
                "UPDATE snapshots SET state='deleting' WHERE "
                "id='89700000-0000-4000-8000-000000000004'"
            )
        )
        connection.execute(
            text(
                "INSERT INTO snapshot_delete_operations "
                "(id, snapshot_id, managed_instance_id, prior_snapshot_state, "
                "requested_at, completed_at, state, phase, reconciliation_required) "
                "VALUES ('89700000-0000-4000-8000-000000000007', "
                "'89700000-0000-4000-8000-000000000004', "
                "'89700000-0000-4000-8000-000000000003', 'available', :timestamp, "
                ":timestamp, 'succeeded', 'succeeded', 0)"
            ),
            {"timestamp": timestamp},
        )
        connection.execute(
            text(
                "DELETE FROM snapshots WHERE id='89700000-0000-4000-8000-000000000004'"
            )
        )
        assert connection.scalar(text("SELECT COUNT(*) FROM snapshots")) == 0
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM snapshot_create_operations"))
            == 1
        )
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM snapshot_restore_operations"))
            == 1
        )
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM snapshot_delete_operations"))
            == 1
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated snapshot deletion history"):
        command.downgrade(config, "0013_disposable_snapshot_restore")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0014_snapshot_deletion"
        )
    engine.dispose()


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


def test_0004_upgrades_to_web_auth_head_and_empty_state_round_trips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0004_runtime_execution_profiles")
    engine = create_database_engine(database_url)
    assert "web_admin_credentials" not in inspect(engine).get_table_names()
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert {"web_admin_credentials", "web_sessions"}.issubset(
        inspector.get_table_names()
    )
    session_foreign_key = inspector.get_foreign_keys("web_sessions")[0]
    assert session_foreign_key["referred_table"] == "web_admin_credentials"
    assert session_foreign_key["options"] == {"ondelete": "RESTRICT"}
    engine.dispose()
    command.check(config)

    command.downgrade(config, "0004_runtime_execution_profiles")
    command.upgrade(config, "head")
    command.check(config)


def test_populated_web_auth_state_refuses_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO web_admin_credentials "
                "(admin_key, password_hash, password_updated_at) VALUES "
                "('local-admin', '$argon2id$test', '2026-09-15T00:00:00.000000Z')"
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated Web authentication"):
        command.downgrade(config, "0004_runtime_execution_profiles")


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


def test_0005_populated_state_upgrades_without_fabricated_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0005_web_auth_sessions")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-15T12:00:00.000000Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions (id, slug, kodi_version) VALUES "
                "('00000000-0000-4000-8000-000000000001', 'kodi-21-3', '21.3')"
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO runtime_revisions (
                    id, runtime_definition_id, revision_number, source_kind,
                    source_reference, content_digest, manifest_reference,
                    manifest_digest, built_at, approval_state, approved_at,
                    requires_trusted_build_confirmation
                ) VALUES (
                    '00000000-0000-4000-8000-000000000002',
                    '00000000-0000-4000-8000-000000000001', 1,
                    'official-package', 'ubuntu', :digest, 'manifest', :digest,
                    :timestamp, 'approved', :timestamp, 0
                )
                """
            ),
            {"digest": "sha256:" + ("a" * 64), "timestamp": timestamp},
        )
        connection.execute(
            text(
                "INSERT INTO runtime_container_artifacts "
                "(runtime_revision_id, image_id) VALUES "
                "('00000000-0000-4000-8000-000000000002', :digest)"
            ),
            {"digest": "sha256:" + ("b" * 64)},
        )
        connection.execute(
            text(
                """
                INSERT INTO runtime_execution_profiles (
                    runtime_revision_id, runtime_kind,
                    expected_application_name, expected_kodi_version,
                    expected_version_tag, jsonrpc_port, jsonrpc_path,
                    process_observation, readiness_probe,
                    readiness_timeout_seconds, poll_interval_seconds,
                    http_timeout_seconds
                ) VALUES (
                    '00000000-0000-4000-8000-000000000002', 'kodi', 'Kodi',
                    '21.3', 'stable', 8080, '/jsonrpc', 'docker_healthcheck',
                    'kodi_jsonrpc', 90, 1.0, 3.0
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO targets (
                    id, name, target_type, kodi_version, availability, health,
                    connection_state
                ) VALUES (
                    '00000000-0000-4000-8000-000000000003', 'retained',
                    'managed', '21.3', 'unavailable', 'warning', 'disconnected'
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO managed_instances (
                    target_id, instance_type, runtime_revision_id,
                    runtime_state, start_on_host_boot
                ) VALUES (
                    '00000000-0000-4000-8000-000000000003', 'disposable',
                    '00000000-0000-4000-8000-000000000002', 'stopped', 0
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO web_admin_credentials (
                    admin_key, password_hash, password_updated_at
                ) VALUES ('local-admin', 'argon2id-test-hash', :timestamp)
                """
            ),
            {"timestamp": timestamp},
        )
        connection.execute(
            text(
                """
                INSERT INTO web_sessions (
                    token_digest, admin_key, csrf_token, created_at,
                    last_used_at, absolute_expires_at
                ) VALUES (
                    :token_digest, 'local-admin', :csrf_token, :timestamp,
                    :timestamp, '2026-09-22T12:00:00.000000Z'
                )
                """
            ),
            {
                "token_digest": "c" * 64,
                "csrf_token": "d" * 43,
                "timestamp": timestamp,
            },
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT support_tier FROM runtime_definitions"))
            == "current"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT expected_prerelease_ordinal FROM runtime_execution_profiles"
                )
            )
            is None
        )
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM runtime_validation_evidence"))
            == 0
        )
        assert connection.scalar(text("SELECT COUNT(*) FROM managed_instances")) == 1
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM runtime_container_artifacts"))
            == 1
        )
        assert connection.scalar(text("SELECT COUNT(*) FROM web_sessions")) == 1
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM web_admin_credentials")) == 1
        )
    engine.dispose()
    command.check(config)

    command.downgrade(config, "0005_web_auth_sessions")
    engine = create_database_engine(database_url)
    assert "support_tier" not in {
        column["name"] for column in inspect(engine).get_columns("runtime_definitions")
    }
    assert "expected_prerelease_ordinal" not in {
        column["name"]
        for column in inspect(engine).get_columns("runtime_execution_profiles")
    }
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM managed_instances")) == 1
        assert connection.scalar(text("SELECT COUNT(*) FROM web_sessions")) == 1
        assert (
            connection.scalar(text("SELECT COUNT(*) FROM web_admin_credentials")) == 1
        )
    engine.dispose()
    command.upgrade(config, "head")
    command.check(config)


def test_phase5_history_and_support_policy_refuse_lossy_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-15T12:00:00.000000Z"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('00000000-0000-4000-8000-000000000001', "
                "'kodi-19-5', '19.5', 'legacy')"
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
                    '00000000-0000-4000-8000-000000000002',
                    '00000000-0000-4000-8000-000000000001', 1, 'test', 'test',
                    :digest, 'test', :digest, :timestamp, 'not_approved', 0
                )
                """
            ),
            {"digest": "sha256:" + ("a" * 64), "timestamp": timestamp},
        )
        connection.execute(
            text(
                """
                INSERT INTO runtime_validation_evidence (
                    id, runtime_revision_id, contract_version, result,
                    started_at, completed_at, evidence_reference, recorded_at
                ) VALUES (
                    '00000000-0000-4000-8000-000000000003',
                    '00000000-0000-4000-8000-000000000002',
                    'kodi-runtime-v1', 'passed', :timestamp, :timestamp,
                    'validation://evidence', :timestamp
                )
                """
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="validation evidence"):
        command.downgrade(config, "0005_web_auth_sessions")

    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM runtime_validation_evidence"))
    engine.dispose()
    with pytest.raises(RuntimeError, match="support policy"):
        command.downgrade(config, "0005_web_auth_sessions")


def test_0006_populated_state_upgrades_with_null_fixture_assignment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0006_runtime_matrix_contract")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runtime_definitions "
                "(id, slug, kodi_version, support_tier) VALUES "
                "('10000000-0000-4000-8000-000000000001', "
                "'kodi-21-3-fixture-test', '21.3', 'current')"
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
                    '10000000-0000-4000-8000-000000000002',
                    '10000000-0000-4000-8000-000000000001', 1, 'test', 'test',
                    :digest, 'manifest', :digest, '2026-09-15T00:00:00Z',
                    'not_approved', 0
                )
                """
            ),
            {"digest": "sha256:" + ("a" * 64)},
        )
        connection.execute(
            text(
                "INSERT INTO targets "
                "(id, name, target_type, kodi_version, availability, health, "
                "connection_state) VALUES "
                "('10000000-0000-4000-8000-000000000003', 'fixture migration', "
                "'managed', '21.3', 'unavailable', 'warning', 'disconnected')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO managed_instances "
                "(target_id, instance_type, runtime_revision_id, runtime_state, "
                "start_on_host_boot) VALUES "
                "('10000000-0000-4000-8000-000000000003', 'disposable', "
                "'10000000-0000-4000-8000-000000000002', 'stopped', 0)"
            )
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT fixture_definition_id FROM managed_instances")
            )
            is None
        )
    engine.dispose()
    command.check(config)


def test_fixture_migration_empty_round_trip_and_populated_downgrade_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0006_runtime_matrix_contract")
    command.upgrade(config, "head")
    command.downgrade(config, "0006_runtime_matrix_contract")
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO fixture_definitions "
                "(id, slug, fixture_version, size_category, content_category, "
                "content_digest, compatible_kodi_version, description) VALUES "
                "('35d6854d-6cfc-5247-97df-7f2023c6ae9d', 'small-mixed-v1', "
                "1, 'small', 'mixed', :digest, '21.3', 'fixture')"
            ),
            {"digest": "sha256:" + ("a" * 64)},
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="populated fixture"):
        command.downgrade(config, "0006_runtime_matrix_contract")


def _seed_0014_instance_with_notes(connection: Connection, *, notes: str) -> None:
    execute = connection.execute
    timestamp = "2026-09-16T00:00:00.000000Z"
    digest = "sha256:" + "a" * 64
    execute(
        text(
            "INSERT INTO runtime_definitions "
            "(id, slug, kodi_version, support_tier) VALUES "
            "('89800000-0000-4000-8000-000000000001', 'instance-delete', "
            "'21.3', 'current')"
        )
    )
    execute(
        text(
            "INSERT INTO runtime_revisions (id, runtime_definition_id, "
            "revision_number, source_kind, source_reference, content_digest, "
            "manifest_reference, manifest_digest, built_at, approval_state, "
            "requires_trusted_build_confirmation) VALUES "
            "('89800000-0000-4000-8000-000000000002', "
            "'89800000-0000-4000-8000-000000000001', 1, 'test', 'test', "
            ":digest, 'test', :digest, :timestamp, 'not_approved', 0)"
        ),
        {"digest": digest, "timestamp": timestamp},
    )
    execute(
        text(
            "INSERT INTO targets (id, name, target_type, kodi_version, "
            "availability, health, connection_state) VALUES "
            "('89800000-0000-4000-8000-000000000003', 'delete source', "
            "'managed', '21.3', 'unavailable', 'warning', 'disconnected')"
        )
    )
    execute(
        text(
            "INSERT INTO managed_instances (target_id, instance_type, "
            "runtime_revision_id, runtime_state, start_on_host_boot, notes) VALUES "
            "('89800000-0000-4000-8000-000000000003', 'disposable', "
            "'89800000-0000-4000-8000-000000000002', 'stopped', 0, :notes)"
        ),
        {"notes": notes},
    )


@pytest.mark.parametrize("notes", ["short 🐍 note", "🐍" * 4096])
def test_0015_preserves_bounded_notes_exactly_and_backfills_snapshot_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, notes: str
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0014_snapshot_deletion")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T00:00:00.000000Z"
    digest = "sha256:" + "b" * 64
    with engine.begin() as connection:
        _seed_0014_instance_with_notes(connection, notes=notes)
        connection.execute(
            text(
                "INSERT INTO snapshots (id, source_managed_instance_id, "
                "runtime_revision_id, captured_at, prior_runtime_state, "
                "manifest_version, archive_digest, manifest_digest, archive_size, "
                "member_count, state, validation_at, validation_result) VALUES "
                "('89800000-0000-4000-8000-000000000004', "
                "'89800000-0000-4000-8000-000000000003', "
                "'89800000-0000-4000-8000-000000000002', :timestamp, 'stopped', "
                "1, :digest, :digest, 10, 1, 'available', :timestamp, 'passed')"
            ),
            {"digest": digest, "timestamp": timestamp},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT notes FROM managed_instances")) == notes
        snapshot = connection.execute(
            text(
                "SELECT source_managed_instance_id, "
                "original_source_managed_instance_id, source_binding "
                "FROM snapshots"
            )
        ).one()
    assert snapshot == (
        "89800000-0000-4000-8000-000000000003",
        "89800000-0000-4000-8000-000000000003",
        "attached",
    )
    engine.dispose()


def test_0015_refuses_oversized_existing_notes_without_rewriting_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0014_snapshot_deletion")
    oversized = "🐍" * 4097
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        _seed_0014_instance_with_notes(connection, notes=oversized)
    engine.dispose()

    with pytest.raises(RuntimeError, match="exceed 4096 Unicode code points"):
        command.upgrade(config, "head")

    engine = create_database_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT notes FROM managed_instances")) == oversized
        )
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0014_snapshot_deletion"
        )
        assert (
            "managed_instance_delete_operations"
            not in inspect(connection).get_table_names()
        )
    engine.dispose()


def test_0015_downgrade_refuses_detached_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0014_snapshot_deletion")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T00:00:00.000000Z"
    digest = "sha256:" + "b" * 64
    with engine.begin() as connection:
        _seed_0014_instance_with_notes(connection, notes="bounded")
        connection.execute(
            text(
                "INSERT INTO snapshots (id, source_managed_instance_id, "
                "runtime_revision_id, captured_at, prior_runtime_state, "
                "manifest_version, archive_digest, manifest_digest, archive_size, "
                "member_count, state, validation_at, validation_result) VALUES "
                "('89800000-0000-4000-8000-000000000004', "
                "'89800000-0000-4000-8000-000000000003', "
                "'89800000-0000-4000-8000-000000000002', :timestamp, 'stopped', 1, "
                ":digest, :digest, 10, 1, 'available', :timestamp, 'passed')"
            ),
            {"digest": digest, "timestamp": timestamp},
        )
    engine.dispose()
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE snapshots SET source_managed_instance_id=NULL, "
                "source_binding='deleted_source', source_instance_name='source', "
                "source_deleted_at=:timestamp"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="detached snapshots"):
        command.downgrade(config, "0014_snapshot_deletion")


def test_0015_downgrade_refuses_populated_instance_delete_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0014_snapshot_deletion")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T00:00:00.000000Z"
    with engine.begin() as connection:
        _seed_0014_instance_with_notes(connection, notes="bounded")
    engine.dispose()
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO managed_instance_delete_operations "
                "(id, original_managed_instance_id, source_instance_name, "
                "runtime_revision_id, preserve_snapshots, snapshot_count, "
                "aggregate_snapshot_bytes, completed_snapshot_count, "
                "notes_at_deletion, "
                "requested_at, completed_at, state, phase, reconciliation_required) "
                "VALUES ('89800000-0000-4000-8000-000000000005', "
                "'89800000-0000-4000-8000-000000000003', 'delete source', "
                "'89800000-0000-4000-8000-000000000002', 1, 0, 0, 0, 'bounded', "
                ":timestamp, :timestamp, 'succeeded', 'succeeded', 0)"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="populated instance deletion history"):
        command.downgrade(config, "0014_snapshot_deletion")


def test_0015_relaxed_terminal_history_retains_original_instance_uuid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0014_snapshot_deletion")
    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T00:00:00.000000Z"
    with engine.begin() as connection:
        _seed_0014_instance_with_notes(connection, notes="bounded")
        connection.execute(
            text(
                "INSERT INTO reset_operations "
                "(id, managed_instance_id, requested_at, completed_at, state, phase, "
                "reconciliation_required) VALUES "
                "('89800000-0000-4000-8000-000000000006', "
                "'89800000-0000-4000-8000-000000000003', :timestamp, :timestamp, "
                "'succeeded', 'succeeded', 0)"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM managed_instances WHERE target_id="
                "'89800000-0000-4000-8000-000000000003'"
            )
        )
        connection.execute(
            text("DELETE FROM targets WHERE id='89800000-0000-4000-8000-000000000003'")
        )
        retained = connection.scalar(
            text("SELECT managed_instance_id FROM reset_operations")
        )
    assert retained == "89800000-0000-4000-8000-000000000003"
    engine.dispose()

    with pytest.raises(RuntimeError, match="operation history"):
        command.downgrade(config, "0014_snapshot_deletion")


def test_0016_lifecycle_history_schema_and_guarded_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, database_url = migration_setup(monkeypatch, tmp_path)
    command.upgrade(config, "0015_instance_deletion")
    engine = create_database_engine(database_url)
    assert (
        "managed_instance_lifecycle_operations" not in inspect(engine).get_table_names()
    )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    indexes = {
        index["name"]: index
        for index in inspector.get_indexes("managed_instance_lifecycle_operations")
    }
    assert indexes["uq_lifecycle_operation_running_instance"]["unique"] == 1
    assert indexes["ix_lifecycle_operation_original_requested"]["column_names"] == [
        "original_managed_instance_id",
        "requested_at",
    ]
    foreign_key = inspector.get_foreign_keys("managed_instance_lifecycle_operations")[0]
    assert foreign_key["referred_table"] == "managed_instances"
    assert foreign_key["options"] == {"ondelete": "SET NULL"}
    engine.dispose()
    command.check(config)
    command.downgrade(config, "0015_instance_deletion")
    command.upgrade(config, "head")

    engine = create_database_engine(database_url)
    timestamp = "2026-09-16T00:00:00.000000Z"
    with engine.begin() as connection:
        _seed_0014_instance_with_notes(connection, notes="bounded")
        connection.execute(
            text(
                "INSERT INTO managed_instance_lifecycle_operations "
                "(id, original_managed_instance_id, managed_instance_id, "
                "instance_name, kind, requested_at, started_at, completed_at, "
                "prior_state, resulting_state, state, reconciliation_required) "
                "VALUES ('89900000-0000-4000-8000-000000000001', "
                "'89800000-0000-4000-8000-000000000003', "
                "'89800000-0000-4000-8000-000000000003', 'source', 'start', "
                ":timestamp, :timestamp, :timestamp, 'stopped', "
                "'running_not_ready', 'succeeded', 0)"
            ),
            {"timestamp": timestamp},
        )
    engine.dispose()
    with pytest.raises(RuntimeError, match="populated lifecycle operation history"):
        command.downgrade(config, "0015_instance_deletion")
