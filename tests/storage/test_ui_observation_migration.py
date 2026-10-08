"""Closed capability extension preserves all existing authority triggers."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path]:
    root = tmp_path / "state"
    (root / "database").mkdir(parents=True)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(root))
    monkeypatch.delenv("KODI_LAB_RUNTIME_ROOT", raising=False)
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option(
        "script_location", str(Path(__file__).parents[2] / "migrations")
    )
    database = root / "database/kodi-lab.db"
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config, database


def _triggers(database: Path) -> dict[str, str]:
    with sqlite3.connect(database) as connection:
        return dict(
            connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
            )
        )


def _review(connection: sqlite3.Connection, capabilities: str, suffix: int) -> None:
    connection.execute(
        "INSERT INTO reviewed_bridge_builds (id,semantic_fingerprint,zip_sha256,"
        "addon_id,addon_version,protocol_version,capabilities_json,"
        "supported_kodi_versions_json,source_reference,reviewed_by,reviewed_at,state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,'active')",
        (
            f"00000000-0000-4000-8000-{suffix:012d}",
            str(suffix) * 64,
            hex(suffix + 9)[2:] * 64,
            "service.kodi_lab_bridge",
            "1.1.15",
            "1",
            capabilities,
            '["21.3"]',
            "explicit test review",
            "local_administrator",
            "2026-10-07T12:00:00.000000Z",
        ),
    )


def test_upgrade_preserves_triggers_and_reviewed_rows_and_rejects_unknown_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, database = _config(tmp_path, monkeypatch)
    command.upgrade(config, "0067_managed_runtime_reconstruction")
    with sqlite3.connect(database) as connection:
        _review(connection, '["screenshot","workspace_deployment"]', 1)
    original = _triggers(database)
    assert original
    command.upgrade(config, "head")
    assert _triggers(database) == original
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM reviewed_bridge_builds"
        ).fetchone() == (1,)
        _review(connection, '["screenshot","ui_observation","workspace_deployment"]', 2)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE reviewed_bridge_builds SET source_reference='changed'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _review(connection, '["screenshot","arbitrary_execution"]', 3)
    with pytest.raises(RuntimeError, match="cannot be downgraded"):
        command.downgrade(config, "0067_managed_runtime_reconstruction")
    assert _triggers(database) == original


def test_empty_state_round_trip_retains_all_triggers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, database = _config(tmp_path, monkeypatch)
    command.upgrade(config, "0067_managed_runtime_reconstruction")
    original = _triggers(database)
    command.upgrade(config, "head")
    command.downgrade(config, "0067_managed_runtime_reconstruction")
    assert _triggers(database) == original
    command.upgrade(config, "head")
    assert _triggers(database) == original
