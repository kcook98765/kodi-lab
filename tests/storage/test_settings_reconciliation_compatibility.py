"""Historical readers remain usable without hiding missing current safety storage."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import CheckConstraint, MetaData, Table, text

from kodi_lab.application.reconciliation import (
    CoordinationCorruptionError,
    ManagedInstanceReconciliationService,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from tests.storage.test_migrations import migration_setup
from tests.storage.test_migrations import migration_templates as migration_templates


@pytest.mark.parametrize(
    "revision",
    ["0078_workspace_deploy_recovery", "0085_session_source_copy_admissions"],
)
def test_known_pre_settings_recovery_still_reads_its_actual_historical_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str
) -> None:
    _config, url = migration_setup(monkeypatch, tmp_path, start_revision=revision)
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    assert not ManagedInstanceReconciliationService(sessions).status(uuid4()).blocking
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == revision
        )
        assert (
            connection.scalar(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE name='mcp_addon_settings_operations'"
                )
            )
            is None
        )
    engine.dispose()


@pytest.mark.parametrize("case", ["current", "unknown", "forged_old_stamp"])
def test_missing_settings_storage_never_becomes_an_empty_current_safety_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0085_session_source_copy_admissions"
    )
    if case != "unknown":
        command.upgrade(config, "0086_addon_settings")
    engine = create_database_engine(url)
    with engine.begin() as connection:
        if case != "unknown":
            connection.execute(text("DROP TABLE mcp_addon_settings_operations"))
        if case == "unknown":
            connection.execute(
                text("UPDATE alembic_version SET version_num='0087_unknown'")
            )
        elif case == "forged_old_stamp":
            connection.execute(
                text(
                    "UPDATE alembic_version SET "
                    "version_num='0085_session_source_copy_admissions'"
                )
            )
    with pytest.raises(CoordinationCorruptionError, match="storage is unavailable"):
        ManagedInstanceReconciliationService(create_session_factory(engine)).status(
            uuid4()
        )
    engine.dispose()


@pytest.mark.parametrize("mode", ["tautology", "permissive_or"])
def test_old_stamp_with_weakened_named_lease_check_is_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0085_session_source_copy_admissions"
    )
    engine = create_database_engine(url)
    lease = Table("managed_instance_mutation_leases", MetaData(), autoload_with=engine)
    constraint = next(
        item
        for item in lease.constraints
        if isinstance(item, CheckConstraint)
        and "managed_instance_mutation_kind" in str(item.name)
    )
    original = str(constraint.sqltext)
    constraint.sqltext = text("1" if mode == "tautology" else f"({original}) OR 1")
    # This exact isolated migration fixture is empty. Observation below must
    # recognize corruption; it must not test admissibility with a write probe.
    with engine.begin() as connection:
        lease.drop(connection)
        lease.create(connection)
    with pytest.raises(CoordinationCorruptionError, match="storage is unavailable"):
        ManagedInstanceReconciliationService(create_session_factory(engine)).status(
            uuid4()
        )
    engine.dispose()


@pytest.mark.parametrize("mode", ["no_stamp", "multiple_stamps", "missing_lease"])
def test_incomplete_historical_identity_never_hides_settings_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _config, url = migration_setup(
        monkeypatch, tmp_path, start_revision="0085_session_source_copy_admissions"
    )
    engine = create_database_engine(url)
    with engine.begin() as connection:
        if mode == "no_stamp":
            connection.execute(text("DELETE FROM alembic_version"))
        elif mode == "multiple_stamps":
            connection.execute(
                text(
                    "INSERT INTO alembic_version(version_num) "
                    "VALUES ('unknown_other_head')"
                )
            )
        else:
            connection.execute(text("DROP TABLE managed_instance_mutation_leases"))
    with pytest.raises(CoordinationCorruptionError, match="storage is unavailable"):
        ManagedInstanceReconciliationService(create_session_factory(engine)).status(
            uuid4()
        )
    engine.dispose()
