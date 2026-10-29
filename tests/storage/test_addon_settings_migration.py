"""Real 0085->0086 preserves data and adds only bounded, value-free coordination."""

import re
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.domain import ManagedInstanceMutationKind
from kodi_lab.storage.engine import create_database_engine
from tests.storage.test_migrations import migration_setup
from tests.storage.test_migrations import migration_templates as migration_templates

PRIOR = "0085_session_source_copy_admissions"
CURRENT = "0086_addon_settings"


def test_real_transition_preserves_tables_triggers_and_closed_lease_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PRIOR)
    engine = create_database_engine(url)
    with engine.begin() as connection:
        # Existing real data and an independent trigger survive the lease-table
        # reconstruction; no ORM create_all or revision stamping is involved.
        connection.execute(
            text("CREATE TABLE preservation_probe (id INTEGER PRIMARY KEY, value TEXT)")
        )
        connection.execute(text("INSERT INTO preservation_probe VALUES (1,'retained')"))
        connection.execute(
            text(
                "CREATE TRIGGER preservation_trigger "
                "AFTER INSERT ON preservation_probe BEGIN SELECT 1; END"
            )
        )
        before = {
            name: connection.execute(
                text(f'SELECT * FROM "{name}" ORDER BY rowid')
            ).all()
            for name in inspect(engine).get_table_names()
            if name != "alembic_version"
        }
        prior_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE name='managed_instance_mutation_leases'"
            )
        )
    command.upgrade(config, CURRENT)
    with engine.connect() as connection:
        for name, rows in before.items():
            assert (
                connection.execute(text(f'SELECT * FROM "{name}" ORDER BY rowid')).all()
                == rows
            )
        assert (
            connection.scalar(
                text("SELECT name FROM sqlite_master WHERE name='preservation_trigger'")
            )
            == "preservation_trigger"
        )
        current_sql = connection.scalar(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE name='managed_instance_mutation_leases'"
            )
        )
        assert (
            "'mcp_addon_settings'" in current_sql
            and "'mcp_addon_settings'" not in prior_sql
        )
        assert "'reconstruction'" not in prior_sql
        match = re.search(r"operation_kind IN \(([^)]+)\)", current_sql)
        assert match is not None
        assert set(re.findall(r"'([^']+)'", match.group(1))) == {
            kind.value for kind in ManagedInstanceMutationKind
        }

        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert not connection.execute(text("PRAGMA foreign_key_check")).all()
    columns = {
        col["name"]
        for col in inspect(engine).get_columns("mcp_addon_settings_operations")
    }
    assert not columns & {
        "settings",
        "values",
        "payload",
        "parameter_fingerprint",
        "plaintext",
    }
    assert {
        "results_json",
        "session_generation",
        "container_id",
        "bridge_generation",
    } <= columns
    command.downgrade(config, PRIOR)
    assert "mcp_addon_settings_operations" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT value FROM preservation_probe"))
            == "retained"
        )
    engine.dispose()


def test_real_receipt_constraints_and_populated_downgrade_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=PRIOR)
    command.upgrade(config, CURRENT)
    engine = create_database_engine(url)
    row: dict[str, object] = {
        name: str(uuid4())
        for name in (
            "id",
            "client_id",
            "request_id",
            "workspace_id",
            "development_source_id",
            "target_id",
            "deployment_id",
            "runtime_revision_id",
        )
    }
    row.update(
        container_id="a" * 64,
        addon_id="plugin.video.test",
        addon_version="1.0.0",
        tree_digest="a" * 64,
        state="pending",
        requested_at="2026-10-26T00:00:00.000000Z",
        generation=9,
        session_generation=6,
        runtime_state_revision=1,
        bridge_generation=11,
    )
    names = ",".join(row)
    sql = text(
        "INSERT INTO mcp_addon_settings_operations ("
        + names
        + ") VALUES ("
        + ",".join(":" + k for k in row)
        + ")"
    )
    with engine.begin() as connection:
        connection.execute(sql, row)
    for changes in (
        {"id": str(uuid4())},
        {"id": str(uuid4()), "request_id": str(uuid4()), "session_generation": 0},
        {"id": str(uuid4()), "request_id": str(uuid4()), "state": "retrying"},
        {"id": str(uuid4()), "request_id": str(uuid4()), "container_id": "unknown"},
    ):
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(sql, {**row, **changes})
    with pytest.raises(RuntimeError, match="history must be preserved"):
        command.downgrade(config, PRIOR)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM mcp_addon_settings_operations")
            )
            == 1
        )
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == CURRENT
        )
    engine.dispose()


def test_populated_predecessor_preserves_lease_then_runs_real_settings_coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from sqlalchemy import select

    from kodi_lab.application.reconciliation import (
        ManagedInstanceReconciliationService,
        StaleOperationReconciliationDispatcher,
    )
    from kodi_lab.storage.models import (
        ManagedInstanceMutationLeaseRow,
        McpAddonSettingsOperationRow,
    )
    from kodi_lab.storage.repositories import ManagedInstanceMutationLeaseRepository
    from tests.application import test_mcp_workspace_development as fixtures
    from tests.application.test_addon_settings import fixture, request

    config, url = migration_setup(
        monkeypatch, tmp_path / "migration", start_revision=PRIOR
    )
    command.upgrade(config, "head")
    engine = create_database_engine(url)
    fixture_root = tmp_path / "fixture"
    env = fixtures._environment(
        fixture_root, database_path=Path(str(engine.url.database))
    )
    lease_id = uuid4()
    with env.sessions.begin() as session:
        leases = ManagedInstanceMutationLeaseRepository(session)
        leases.reserve(
            fixtures.TARGET_ID,
            kind=ManagedInstanceMutationKind.LIFECYCLE,
            operation_id=lease_id,
        )
        assert leases.claim(
            fixtures.TARGET_ID,
            operation_id=lease_id,
            token_digest="c" * 64,
            now=fixtures.NOW,
            expires_at=fixtures.NOW + timedelta(minutes=15),
        )
    command.downgrade(config, PRIOR)
    with engine.connect() as connection:
        before = connection.execute(
            text("SELECT * FROM managed_instance_mutation_leases")
        ).all()
    command.upgrade(config, CURRENT)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT * FROM managed_instance_mutation_leases")
            ).all()
            == before
        )
    command.upgrade(config, "head")
    with env.sessions.begin() as session:
        assert ManagedInstanceMutationLeaseRepository(session).release(
            fixtures.TARGET_ID, operation_id=lease_id, token_digest="c" * 64
        )
    env, bridge, service = fixture(fixture_root, seeded=env)
    result = service.set(
        client_id=env.client_id, request=request(), authorize=lambda *_: None
    )
    assert result.state == "completed" and bridge.count == 1
    with env.sessions.begin() as session:
        row = session.get(McpAddonSettingsOperationRow, str(result.operation_id))
        assert row is not None
        row.state = "dispatching"
        row.completed_at = None
        leases = ManagedInstanceMutationLeaseRepository(session)
        leases.reserve(
            fixtures.TARGET_ID,
            kind=ManagedInstanceMutationKind.MCP_ADDON_SETTINGS,
            operation_id=result.operation_id,
        )
        assert leases.claim(
            fixtures.TARGET_ID,
            operation_id=result.operation_id,
            token_digest="d" * 64,
            now=fixtures.NOW,
            expires_at=fixtures.NOW + timedelta(seconds=1),
        )
    StaleOperationReconciliationDispatcher(
        env.sessions, clock=lambda: fixtures.NOW + timedelta(hours=1)
    ).sweep_instance(fixtures.TARGET_ID)
    status = ManagedInstanceReconciliationService(env.sessions).status(
        fixtures.TARGET_ID
    )
    assert status.blocking and status.operation_state == "ambiguous"
    with env.sessions() as session:
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(fixtures.TARGET_ID))
            is None
        )
        assert session.scalar(select(McpAddonSettingsOperationRow)) is not None
    engine.dispose()


@pytest.mark.parametrize(
    "kind",
    [
        ManagedInstanceMutationKind.MCP_ADDON_SETTINGS,
        ManagedInstanceMutationKind.RECONSTRUCTION,
    ],
)
def test_downgrade_refuses_unrepresentable_lease_even_without_settings_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: ManagedInstanceMutationKind,
) -> None:
    from kodi_lab.storage.repositories import ManagedInstanceMutationLeaseRepository
    from tests.application import test_mcp_workspace_development as fixtures

    config, url = migration_setup(
        monkeypatch, tmp_path / "migration", start_revision=PRIOR
    )
    command.upgrade(config, "head")
    engine = create_database_engine(url)
    env = fixtures._environment(
        tmp_path / "fixture", database_path=Path(str(engine.url.database))
    )
    with env.sessions.begin() as session:
        ManagedInstanceMutationLeaseRepository(session).reserve(
            fixtures.TARGET_ID, kind=kind, operation_id=uuid4()
        )
    command.downgrade(config, CURRENT)
    with pytest.raises(RuntimeError, match="history must be preserved"):
        command.downgrade(config, PRIOR)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM mcp_addon_settings_operations")
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT operation_kind FROM managed_instance_mutation_leases")
            )
            == kind.value
        )
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == CURRENT
        )
    engine.dispose()
