from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from kodi_lab.storage.engine import create_database_engine


def test_initial_migration_round_trip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project_root = Path(__file__).parents[2]
    data_root = tmp_path / "data"
    (data_root / "database").mkdir(parents=True)
    monkeypatch.chdir(project_root)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(data_root))
    monkeypatch.delenv("KODI_LAB_RUNTIME_ROOT", raising=False)
    config = Config(str(project_root / "alembic.ini"))
    database_url = f"sqlite:///{data_root / 'database' / 'kodi-lab.db'}"

    command.upgrade(config, "head")

    engine = create_database_engine(database_url)
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) == {
        "alembic_version",
        "managed_instances",
        "targets",
    }
    assert inspector.get_pk_constraint("targets")["constrained_columns"] == ["id"]
    assert inspector.get_pk_constraint("managed_instances")["constrained_columns"] == [
        "target_id"
    ]
    foreign_keys = inspector.get_foreign_keys("managed_instances")
    assert foreign_keys[0]["referred_table"] == "targets"
    assert foreign_keys[0]["options"] == {"ondelete": "CASCADE"}
    assert {
        constraint["name"] for constraint in inspector.get_check_constraints("targets")
    } == {
        "connection_state",
        "health_status",
        "target_availability",
        "target_kodi_version_not_blank",
        "target_name_not_blank",
        "target_type",
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints("managed_instances")
    } == {
        "instance_runtime_state",
        "instance_type",
        "managed_instance_runtime_revision_not_blank",
    }
    engine.dispose()

    command.check(config)
    command.downgrade(config, "base")

    engine = create_database_engine(database_url)
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    assert "targets" in inspect(engine).get_table_names()
    engine.dispose()
