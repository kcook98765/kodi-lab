"""Resource policy upgrades do not enroll existing Kodi and retain uncertain starts."""

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text

from kodi_lab.broker.errors import PostStartVerificationError
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from tests.broker.test_managed_capacity import Environment
from tests.storage.test_migrations import (  # noqa: F401
    migration_setup,
    migration_templates,
)

PREDECESSOR = "0075_external_reconnect"
REVISION = "0076_managed_resource_policy"
TABLES = {
    "managed_resource_policy",
    "managed_idle_states",
    "managed_instance_uses",
    "managed_start_reservations",
    "managed_agent_lifecycle_requests",
}


def test_populated_upgrade_backfills_only_explicitly_unenrolled_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    engine = create_database_engine(url)
    environment = Environment(create_session_factory(engine))
    before = set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        targets = (
            connection.execute(text("SELECT * FROM targets ORDER BY id")).tuples().all()
        )
        leases = (
            connection.execute(
                text(
                    "SELECT * FROM managed_instance_mutation_leases "
                    "ORDER BY managed_instance_id"
                )
            )
            .tuples()
            .all()
        )
        triggers = (
            connection.execute(
                text(
                    "SELECT name,sql FROM sqlite_master "
                    "WHERE type='trigger' ORDER BY name"
                )
            )
            .tuples()
            .all()
        )
    command.upgrade(config, REVISION)
    assert set(inspect(engine).get_table_names()) == before | TABLES
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT id,max_running,idle_minutes FROM managed_resource_policy")
        ).tuples().all() == [(1, 4, 60)]
        assert connection.execute(
            text(
                "SELECT managed_instance_id,enrolled,last_activity_at "
                "FROM managed_idle_states ORDER BY managed_instance_id"
            )
        ).tuples().all() == [
            (identity, 0, None) for identity in sorted(environment.ids)
        ]
        assert (
            connection.execute(text("SELECT * FROM targets ORDER BY id")).tuples().all()
            == targets
        )
        assert (
            connection.execute(
                text(
                    "SELECT * FROM managed_instance_mutation_leases "
                    "ORDER BY managed_instance_id"
                )
            )
            .tuples()
            .all()
            == leases
        )
        assert (
            connection.execute(
                text(
                    "SELECT name,sql FROM sqlite_master "
                    "WHERE type='trigger' ORDER BY name"
                )
            )
            .tuples()
            .all()
            == triggers
        )
        assert connection.execute(text("PRAGMA foreign_key_check")).tuples().all() == []
    command.downgrade(config, PREDECESSOR)
    assert set(inspect(engine).get_table_names()) == before
    engine.dispose()


def test_real_uncertain_admission_on_upgraded_schema_blocks_lossy_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PREDECESSOR)
    engine = create_database_engine(url)
    environment = Environment(create_session_factory(engine))
    command.upgrade(config, REVISION)
    # Current broker admission requires its matched current schema. Empty later
    # migrations downgrade back to 0076, whose real uncertain-start guard must
    # still prevent losing the retained reservation on the way to 0075.
    command.upgrade(config, "head")
    with pytest.raises(PostStartVerificationError):
        environment.start(0, dispatch=PostStartVerificationError("fixture unknown"))
    with pytest.raises(RuntimeError, match="cannot be safely downgraded"):
        command.downgrade(config, PREDECESSOR)
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == REVISION
        )
        assert connection.execute(
            text("SELECT managed_instance_id,phase FROM managed_start_reservations")
        ).tuples().all() == [(environment.ids[0], "uncertain")]
        assert connection.execute(text("PRAGMA foreign_key_check")).tuples().all() == []
    engine.dispose()
