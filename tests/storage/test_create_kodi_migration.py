"""One additive migration and parent receipt retention, on isolated databases."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import CheckConstraint, inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import ManagedInstanceLifecycleOperationRepository
from tests.create_kodi_support import FakeChildren, accept, continue_one, service
from tests.storage.test_migrations import migration_setup
from tests.storage.test_migrations import migration_templates as migration_templates

OLD = "0091_managed_start_authorization"
NEW = "0092_create_kodi_operations"


def test_upgrade_is_additive_matches_orm_and_empty_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    engine = create_database_engine(url)
    with engine.connect() as c:
        before = {
            name: c.execute(text(f'SELECT * FROM "{name}"')).all()
            for name in inspect(c).get_table_names()
            if name != "alembic_version"
        }
        triggers = c.execute(
            text("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
        ).all()
    command.upgrade(config, NEW)
    with engine.connect() as c:
        for name, rows in before.items():
            assert c.execute(text(f'SELECT * FROM "{name}"')).all() == rows
        assert (
            c.execute(
                text("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
            ).all()
            == triggers
        )
        assert set(inspect(c).get_table_names()) == {
            *before,
            "alembic_version",
            "create_kodi_operations",
        }
        checks = {
            row["name"]: row["sqltext"]
            for row in inspect(c).get_check_constraints("create_kodi_operations")
        }
        expected = {
            constraint.name: str(constraint.sqltext)
            for constraint in metadata.tables["create_kodi_operations"].constraints
            if isinstance(constraint, CheckConstraint)
        }
        assert checks == expected
        assert c.scalar(text("PRAGMA integrity_check")) == "ok"
        assert c.execute(text("PRAGMA foreign_key_check")).all() == []
    command.downgrade(config, OLD)
    with engine.connect() as c:
        assert "create_kodi_operations" not in inspect(c).get_table_names()
    command.upgrade(config, NEW)
    # Use the production repository against the migrated, not ORM-created, table.
    op = accept(service(url))
    assert service(url).get(op.id) == op
    with pytest.raises(RuntimeError, match="history cannot be discarded"):
        command.downgrade(config, OLD)
    with engine.connect() as c:
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == NEW
        assert c.scalar(text("SELECT count(*) FROM create_kodi_operations")) == 1
    engine.dispose()


@pytest.mark.parametrize(
    "assignments",
    [
        "stage='invented'",
        "checkpoint='retry'",
        "version=-1",
        "scope='force'",
        "checkpoint='confirmed'",
        "outcome='succeeded'",
        "image_id='sha256:bad'",
        "container_id='" + "b" * 64 + "'",
        "recipe_digest='sha256:unreviewed'",
    ],
)
def test_sql_constraints_reject_invalid_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assignments: str,
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, NEW)
    accept(service(url))
    engine = create_database_engine(url)
    with engine.begin() as c, pytest.raises(IntegrityError):
        c.execute(text(f"UPDATE create_kodi_operations SET {assignments}"))
    engine.dispose()


def test_database_uniqueness_and_completed_parent_protect_child_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, NEW)
    app, children = service(url), FakeChildren()
    op = accept(app)
    for _ in range(16):
        op = continue_one(app, op.id, children)
    assert op.checkpoint == "complete"
    engine = create_database_engine(url)
    with engine.begin() as c:
        # Both referenced children precede more than the normal 20 retained receipts.
        for index, child in enumerate(
            [
                op.provision_operation_id,
                op.start_operation_id,
                *[uuid4() for _ in range(25)],
            ]
        ):
            c.execute(
                text(
                    "INSERT INTO managed_instance_lifecycle_operations "
                    "(id,original_managed_instance_id,instance_name,kind,requested_at,started_at,"
                    "completed_at,prior_state,resulting_state,state,"
                    "reconciliation_required) "
                    "VALUES (:id,:target,'Fixture',:kind,:now,:now,:now,"
                    "'stopped','stopped','succeeded',0)"
                ),
                {
                    "id": str(child),
                    "target": str(op.target_id),
                    "kind": "provision" if index == 0 else "start",
                    "now": f"2026-11-03T00:00:{index:02d}.000000Z",
                },
            )
        c.execute(
            text(
                "INSERT INTO managed_start_authorizations "
                "(operation_id,target_id,runtime_revision_id,image_id,container_id,"
                "runtime_state_revision,lease_digest,proof_digest,created_at) "
                "VALUES (:id,:target,:revision,:image,:container,"
                "5,:digest,:digest,:now)"
            ),
            {
                "id": str(op.start_operation_id),
                "target": str(op.target_id),
                "revision": str(op.runtime_revision_id),
                "image": op.image_id,
                "container": op.container_id,
                "digest": "c" * 64,
                "now": op.updated_at,
            },
        )
        # A NULL child in a different accepted parent must not disable all pruning.
    accept(app, generation="nexus")
    with create_session_factory(engine)() as session:
        assert (
            ManagedInstanceLifecycleOperationRepository(session).prune_terminal(
                op.target_id
            )
            == 5
        )
        session.commit()
    with engine.connect() as c:
        assert (
            c.scalar(text("SELECT count(*) FROM managed_instance_lifecycle_operations"))
            == 22
        )
        assert c.scalar(text("SELECT count(*) FROM managed_start_authorizations")) == 1
        original = dict(
            c.execute(
                text("SELECT * FROM create_kodi_operations WHERE id=:id"),
                {"id": str(op.id)},
            )
            .mappings()
            .one()
        )
    # Direct SQL, without admission code, still enforces both uniqueness fences.
    for unique in ("request_id", "target_id"):
        values = original | {
            "id": str(uuid4()),
            "request_id": str(uuid4()),
            "target_id": str(uuid4()),
            "installation_operation_id": None,
            "provision_operation_id": None,
            "start_operation_id": None,
        }
        values[unique] = original[unique]
        with engine.begin() as c, pytest.raises(IntegrityError):
            c.execute(
                metadata.tables["create_kodi_operations"].insert().values(**values)
            )
    assert app.get(op.id) == op
    engine.dispose()
