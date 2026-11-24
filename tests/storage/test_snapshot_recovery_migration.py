from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, select

from kodi_lab.application.snapshot_recovery import SnapshotRecoveryService
from kodi_lab.application.snapshot_recovery_maintenance import seal_epoch
from kodi_lab.broker.snapshot_recovery import RecoveryStepExecutor
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import SnapshotRecoveryEpochRow
from tests.migration_support import migration_config
from tests.snapshot_recovery_support import Case
from tests.snapshot_recovery_support import factory as factory
from tests.snapshot_recovery_support import recovery_case as recovery_case

OLD, NEW = "0018_source_addition_reviews", "0019_snapshot_recovery"


def _replace(
    connection: sqlite3.Connection,
    table: str,
    key: str,
    value: str,
    changes: dict[str, Any],
) -> None:
    cursor = connection.execute(f'SELECT * FROM "{table}" WHERE "{key}"=?', (value,))
    row = cursor.fetchone()
    assert row is not None and cursor.description is not None
    data = dict(zip((c[0] for c in cursor.description), row, strict=True))
    data.update(changes)
    names = ",".join('"' + name + '"' for name in data)
    placeholders = ",".join("?" for _ in data)
    connection.execute(
        f'INSERT OR REPLACE INTO "{table}" ({names}) VALUES ({placeholders})',
        tuple(data.values()),
    )


def test_populated_predecessor_preservation_constraints_and_downgrade(
    recovery_case: Case,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = recovery_case
    database = tmp_path / "migrated.db"
    config = migration_config(database)
    command.upgrade(config, OLD)
    engine = create_engine(f"sqlite:///{database}")
    existing = set(inspect(engine).get_table_names())
    with case.sessions() as source, engine.begin() as destination:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing:
                continue
            for row in source.execute(select(table)).mappings():
                query = table.select()
                for key in table.primary_key.columns:
                    query = query.where(key == row[key.name])
                if destination.execute(query).first() is None:
                    destination.execute(table.insert().values(**dict(row)))
                else:
                    update = table.update()
                    for key in table.primary_key.columns:
                        update = update.where(key == row[key.name])
                    destination.execute(update.values(**dict(row)))
    with sqlite3.connect(database) as connection:
        before_tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'"
            )
        ]
        before = {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
            for table in before_tables
        }
        before_triggers = dict(
            connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
            )
        )
    command.upgrade(config, NEW)
    with sqlite3.connect(database) as connection:
        assert {
            table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
            for table in before_tables
        } == before
        after_triggers = dict(
            connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
            )
        )
        assert all(after_triggers[k] == v for k, v in before_triggers.items())
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    # Unused migration can reverse and reapply, preserving predecessor bytes.
    command.downgrade(config, OLD)
    command.upgrade(config, NEW)
    sessions = create_session_factory(engine)
    epoch_id = seal_epoch(sessions, case.review, case.seal)
    incident = case.review.normalized()["receipt"]["id"]
    with sqlite3.connect(database) as connection:
        for sql in (
            "UPDATE snapshot_recovery_epochs SET active=2",
            "UPDATE snapshot_recovery_epochs SET active=NULL",
            "UPDATE snapshot_recovery_epochs SET state='closed'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)
        with pytest.raises(sqlite3.IntegrityError, match="replacement refused"):
            _replace(
                connection,
                "snapshot_recovery_epochs",
                "id",
                epoch_id,
                {"id": str(uuid4())},
            )
        for sql in (
            "UPDATE snapshot_restore_operations SET phase='failed' WHERE id=?",
            "DELETE FROM snapshot_restore_operations WHERE id=?",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(sql, (incident,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM snapshot_recovery_epochs WHERE id=?", (epoch_id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE snapshot_recovery_epochs SET evidence_json='{}'")
        with pytest.raises(sqlite3.IntegrityError, match="replacement refused"):
            connection.execute(
                "INSERT OR REPLACE INTO snapshot_restore_operations "
                "SELECT * FROM snapshot_restore_operations WHERE id=?",
                (incident,),
            )
    with sessions.begin() as session, case.sessions() as source_session:
        boundary = session.get(SnapshotRecoveryEpochRow, epoch_id)
        original_boundary = source_session.get(SnapshotRecoveryEpochRow, case.epoch_id)
        assert boundary is not None and original_boundary is not None
        boundary.web_owner, boundary.broker_owner = (
            original_boundary.web_owner,
            original_boundary.broker_owner,
        )
        boundary.state = "serving"
        owner = boundary.broker_owner
        assert owner is not None
    case.sessions, case.epoch_id = sessions, epoch_id
    case.executor = RecoveryStepExecutor(
        sessions, case.provider, case.root, os.getgid(), epoch_id, owner
    )
    case.service = SnapshotRecoveryService(sessions, case, case.root, os.getgid())
    case.service.owner_uid = os.getuid()
    claimed = case.claim()
    real_step = case.snapshot_recovery_step

    def constrained_step(
        recovery_id: str, step: str, attempt_id: str, wire: str
    ) -> object:
        with sqlite3.connect(database) as connection:
            for changes in (
                {"id": str(uuid4()), "request_id": str(uuid4())},
                {"id": str(uuid4())},
                {"request_id": str(uuid4())},
            ):
                with pytest.raises(sqlite3.IntegrityError, match="replacement refused"):
                    _replace(
                        connection,
                        "snapshot_recovery_dispatches",
                        "id",
                        attempt_id,
                        changes,
                    )
            # Competing requests/claims are tested as uncommitted constraint
            # fixtures, never admitted operations or historical dispatch facts.
            for conflict in ("request", "incident"):
                connection.execute("SAVEPOINT competing_claim")
                other = str(uuid4())
                _replace(
                    connection,
                    "snapshot_recoveries",
                    "id",
                    recovery_id,
                    {
                        "id": other,
                        "incident_id": str(uuid4())
                        if conflict == "request"
                        else claimed["incident_id"],
                        "state": "preview",
                        "request_id": None,
                        "consumed_at": None,
                        "lease_digest": None,
                        "container_id": None,
                        "successor_generation": None,
                    },
                )
                source = connection.execute(
                    "SELECT request_id,consumed_at,lease_digest "
                    "FROM snapshot_recoveries WHERE id=?",
                    (recovery_id,),
                ).fetchone()
                assert source is not None
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE OR REPLACE snapshot_recoveries SET state='claimed',"
                        "request_id=?,consumed_at=?,lease_digest=? WHERE id=?",
                        (
                            source[0] if conflict == "request" else str(uuid4()),
                            source[1],
                            source[2],
                            other,
                        ),
                    )
                connection.execute("ROLLBACK TO competing_claim")
                connection.execute("RELEASE competing_claim")
        return real_step(recovery_id, step, attempt_id, wire)

    monkeypatch.setattr(case, "snapshot_recovery_step", constrained_step)
    case.service.execute(claimed["recovery_id"])
    assert case.service.status(claimed["recovery_id"])["state"] == "succeeded"
    assert case.provider.calls == ["provision", "apply", "verify"]
    with sqlite3.connect(database) as connection:
        event = connection.execute(
            "SELECT id FROM snapshot_recovery_events LIMIT 1"
        ).fetchone()
        assert event is not None
        with pytest.raises(sqlite3.IntegrityError, match="replacement refused"):
            _replace(
                connection,
                "snapshot_recovery_events",
                "id",
                event[0],
                {"id": str(uuid4())},
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM development_sessions WHERE generation=55")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE development_sessions SET container_id=? WHERE generation=55",
                ("f" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE snapshot_recovery_dispatches SET state='prepared'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM snapshot_recovery_resolutions")
    with pytest.raises(RuntimeError, match="refusing lossy downgrade"):
        command.downgrade(config, OLD)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (NEW,)
        stored = connection.execute(
            "SELECT evidence_json FROM snapshot_recovery_epochs WHERE id=?", (epoch_id,)
        ).fetchone()
        assert (
            stored is not None
            and json.loads(stored[0])["historical_dispatch"] == "not_recorded"
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    engine.dispose()


def test_downgrade_refuses_new_kind_lease_before_any_destructive_ddl(
    tmp_path: Path,
) -> None:
    database = tmp_path / "new-kind-lease.db"
    config = migration_config(database)
    command.upgrade(config, NEW)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO global_heavy_operation_lease"
            "(singleton_id,operation_kind,operation_id) "
            "VALUES(1,'snapshot_recovery',?)",
            (str(uuid4()),),
        )
    before = database.read_bytes()
    with pytest.raises(RuntimeError, match="lease exists"):
        command.downgrade(config, OLD)
    assert database.read_bytes() == before


def test_unexpected_predecessor_vocabulary_refuses_without_writes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "unexpected-predecessor.db"
    config = migration_config(database)
    command.upgrade(config, OLD)
    # Deliberately malformed, isolated predecessor. No application migration is
    # permitted to normalize/adopt this unexpected predicate.
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_schema SET sql=replace(sql, ? , ?) "
            "WHERE name='global_heavy_operation_lease'",
            ("('snapshot_create'", "('unexpected_kind','snapshot_create'"),
        )
        version = connection.execute("PRAGMA schema_version").fetchone()
        assert version is not None
        connection.execute(f"PRAGMA schema_version={int(version[0]) + 1}")
    before = database.read_bytes()
    with pytest.raises(RuntimeError, match="Unexpected frozen lease vocabulary"):
        command.upgrade(config, NEW)
    assert database.read_bytes() == before
