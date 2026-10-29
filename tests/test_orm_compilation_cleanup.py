"""Real SQL and live-worker contracts for test-only compilation reclamation."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text

from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import ProductionMutationControlRow
from tests.efficiency import CurrentSchemaTemplate, reclaim_orm_compilations
from tests.test_test_efficiency import run_probe


def test_reclamation_preserves_private_rows_and_can_compile_again(
    tmp_path: Path, current_schema: CurrentSchemaTemplate
) -> None:
    engines = []
    factories = []
    for name, blocked in (("first", True), ("second", False)):
        path = tmp_path / f"{name}.db"
        current_schema.clone(path)
        engine = create_database_engine(f"sqlite:///{path}")
        engines.append(engine)
        factory = create_session_factory(engine)
        factories.append(factory)
        with factory.begin() as session:
            session.add(
                ProductionMutationControlRow(
                    id=1,
                    production_mutations_blocked=blocked,
                    policy_epoch=1,
                    updated_by="fixture",
                    updated_at="2026-10-29T00:00:00.000000Z",
                )
            )
    try:
        mapper = inspect(ProductionMutationControlRow)
        cache = mapper.__dict__["_compiled_cache"]
        assert all(any(key[0] is engine.dialect for key in cache) for engine in engines)
        assert reclaim_orm_compilations() >= 2
        assert not cache
        for factory, expected in zip(factories, (True, False), strict=True):
            with factory() as session:
                row = session.get(ProductionMutationControlRow, 1)
                assert row is not None
                assert row.production_mutations_blocked is expected
                assert row.policy_epoch == 1
        with factories[0].begin() as session:
            row = session.get(ProductionMutationControlRow, 1)
            assert row is not None
            row.policy_epoch = 2
        assert cache  # A real ORM flush refills the same cache inside this case.
        with factories[1]() as session:
            assert (
                session.scalar(select(ProductionMutationControlRow.policy_epoch)) == 1
            )
        for engine in engines:
            with engine.connect() as connection:
                assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
                assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        for engine in engines:
            engine.dispose()


def test_unknown_cache_representation_fails_visibly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapper = inspect(ProductionMutationControlRow)
    monkeypatch.setitem(mapper.__dict__, "_compiled_cache", object())
    with pytest.raises(RuntimeError, match="unsupported ORM compilation cache"):
        reclaim_orm_compilations()


@pytest.mark.parametrize("kind", ["thread", "process"])
def test_protocol_retains_plans_until_owned_worker_is_finished(
    tmp_path: Path, kind: str
) -> None:
    worker_start = (
        "stop = threading.Event()\n"
        "worker = threading.Thread(target=stop.wait, args=(10,))\nworker.start()"
        if kind == "thread"
        else "worker = subprocess.Popen("
        "[sys.executable, '-c', 'import time; time.sleep(10)'])"
    )
    worker_stop = (
        "stop.set()\n        worker.join(timeout=5)\n"
        "        assert not worker.is_alive()"
        if kind == "thread"
        else "worker.terminate()\n        worker.wait(timeout=5)"
    )
    document = run_probe(
        tmp_path,
        """
import threading, subprocess, sys
from sqlalchemy import inspect
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import ProductionMutationControlRow
worker = stop = None

def test_first(tmp_path, current_schema):
    global worker, stop
    path = tmp_path / "database.db"
    current_schema.clone(path)
    engine = create_database_engine(f"sqlite:///{path}")
    try:
        with create_session_factory(engine).begin() as session:
            session.add(ProductionMutationControlRow(
                id=1, production_mutations_blocked=True, policy_epoch=1,
                updated_by="fixture", updated_at="2026-10-29T00:00:00.000000Z"))
    finally:
        engine.dispose()
    """
        + worker_start.replace("\n", "\n    ")
        + """

def test_second():
    try:
        assert inspect(ProductionMutationControlRow).__dict__["_compiled_cache"]
    finally:
        """
        + worker_stop
        + "\n",
    )
    cases = document["tests"]
    assert isinstance(cases, dict)
    assert cases["test_probe.py::test_first"]["orm_cache"] == {
        "outcome": "retained_workers_or_unknown"
    }
    assert cases["test_probe.py::test_second"]["orm_cache"]["outcome"] == "reclaimed"
    assert cases["test_probe.py::test_second"]["orm_cache"]["entries"] >= 1
    metrics = document["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["orm_cache_retained_workers_or_unknown"] >= 1
