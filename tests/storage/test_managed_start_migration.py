"""Additive ordinary Start authority preserves history and database backstops."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from kodi_lab.storage.engine import create_database_engine
from tests.storage.test_migrations import migration_setup
from tests.storage.test_migrations import migration_templates as migration_templates

OLD = "0090_verified_noop_deployment"
NEW = "0091_managed_start_authorization"


def test_upgrade_preserves_existing_tables_and_empty_downgrade_restores_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    engine = create_database_engine(url)
    with engine.connect() as c:
        before = {
            name: c.execute(text(f'SELECT * FROM "{name}"')).all()
            for name in inspect(c).get_table_names()
            if name != "alembic_version"
        }
        old_trigger = c.scalar(
            text("SELECT sql FROM sqlite_master WHERE name='retained_mutation_insert'")
        )
    command.upgrade(config, NEW)
    with engine.connect() as c:
        assert "managed_start_authorizations" in inspect(c).get_table_names()
        for name, rows in before.items():
            assert c.execute(text(f'SELECT * FROM "{name}"')).all() == rows
        trigger = c.scalar(
            text("SELECT sql FROM sqlite_master WHERE name='retained_mutation_insert'")
        )
        assert "managed_start_authorizations" in trigger
        assert "retained selection required" in trigger
        assert c.scalar(text("PRAGMA integrity_check")) == "ok"
        assert c.execute(text("PRAGMA foreign_key_check")).all() == []
    command.downgrade(config, OLD)
    with engine.connect() as c:
        assert "managed_start_authorizations" not in inspect(c).get_table_names()
        assert (
            c.scalar(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE name='retained_mutation_insert'"
                )
            )
            == old_trigger
        )
    command.upgrade(config, NEW)
    engine.dispose()


def test_authorization_requires_operation_and_used_history_cannot_be_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, url = migration_setup(monkeypatch, tmp_path, start_revision=OLD)
    command.upgrade(config, NEW)
    engine = create_database_engine(url)
    operation = str(uuid4())
    values = dict(
        operation=operation,
        target=str(uuid4()),
        revision=str(uuid4()),
        image="sha256:" + "a" * 64,
        container="b" * 64,
        digest="c" * 64,
        now="2026-11-03T00:00:00.000000Z",
    )
    insert = text(
        "INSERT INTO managed_start_authorizations "
        "(operation_id,target_id,runtime_revision_id,image_id,container_id,"
        "runtime_state_revision,lease_digest,proof_digest,created_at) "
        "VALUES (:operation,:target,:revision,:image,:container,1,:digest,:digest,:now)"
    )
    with engine.begin() as c, pytest.raises(IntegrityError):
        c.execute(insert, values)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO managed_instance_lifecycle_operations "
                "(id,original_managed_instance_id,instance_name,kind,requested_at,started_at,"
                "prior_state,state,reconciliation_required) "
                "VALUES (:operation,:target,'Fixture','start',:now,:now,"
                "'stopped','running',0)"
            ),
            values,
        )
        c.execute(insert, values)
    with pytest.raises(RuntimeError, match="cannot be discarded"):
        command.downgrade(config, OLD)
    with engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM managed_start_authorizations")) == 1
        assert c.scalar(text("SELECT version_num FROM alembic_version")) == NEW
    # Normal terminal receipt pruning cascades, rather than leaving orphan proof.
    with engine.begin() as c:
        c.execute(
            text(
                "DELETE FROM managed_instance_lifecycle_operations WHERE id=:operation"
            ),
            values,
        )
        assert c.scalar(text("SELECT count(*) FROM managed_start_authorizations")) == 0
    engine.dispose()
