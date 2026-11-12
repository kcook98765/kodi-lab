"""A late renewal must never write through to a successor viewer's lease."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, get_ident
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.novnc_control import HumanViewerOwnership, timestamp
from kodi_lab.storage.models import NovncSessionRow
from kodi_lab.viewer_diagnostics import LOGGER

from .conftest import INSTANCE_ID
from .test_novnc_control import BINDING, OWNER
from .view_attempt_fixture import view_attempt


@pytest.mark.parametrize("successor_owner", [OWNER, "d" * 64])
def test_delayed_renewal_cannot_shorten_or_extend_successor(
    database: sessionmaker[Session],
    successor_owner: str,
) -> None:
    ownership = HumanViewerOwnership(database)
    old_attempt, successor = view_attempt(), view_attempt()
    broker = str(uuid4())
    ownership.create(INSTANCE_ID, old_attempt, OWNER, BINDING, "interactive")
    ownership.claim(INSTANCE_ID, old_attempt, OWNER, BINDING, "interactive", broker)
    paused, release = Event(), Event()

    class DelayedFlush(Session):
        def flush(self, objects: Sequence[Any] | None = None) -> None:
            # Hold the old implementation after it checked the old row and
            # before its ORM UPDATE. A conditional renewal may linearize first.
            if any(isinstance(row, NovncSessionRow) for row in self.dirty):
                paused.set()
                assert release.wait(4)
            super().flush(objects)

    renewing = HumanViewerOwnership(
        sessionmaker(bind=database.kw["bind"], class_=DelayedFlush)
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(renewing.renew, INSTANCE_ID, old_attempt, OWNER)
        try:
            deadline = time.monotonic() + 3
            while not paused.is_set() and not future.done():
                assert time.monotonic() < deadline
                paused.wait(0.005)
            ownership.revoke(INSTANCE_ID, old_attempt, OWNER)
            ownership.finish(INSTANCE_ID, old_attempt, broker, True)
            ownership.create(
                INSTANCE_ID, successor, successor_owner, BINDING, "view-only"
            )
            ownership.claim(
                INSTANCE_ID, successor, successor_owner, BINDING, "view-only", broker
            )
            with database() as db:
                row = db.get(NovncSessionRow, str(INSTANCE_ID))
                assert row
                successor_expiry = row.expires_at
        finally:
            release.set()
        future.result(timeout=3)
    with database() as db:
        row = db.get(NovncSessionRow, str(INSTANCE_ID))
        assert row and row.attempt == str(successor)
        assert row.expires_at == successor_expiry


def test_expiry_while_waiting_for_writer_does_not_revive_control(
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(logging.getLogger(LOGGER), "disabled", False)
    monkeypatch.setattr(logging.getLogger(LOGGER), "propagate", True)
    caplog.set_level(logging.INFO, logger=LOGGER)
    ownership = HumanViewerOwnership(database)
    attempt, broker = view_attempt(), str(uuid4())
    ownership.create(INSTANCE_ID, attempt, OWNER, BINDING, "interactive")
    ownership.claim(INSTANCE_ID, attempt, OWNER, BINDING, "interactive", broker)
    waiting = Event()
    worker: list[int] = []
    engine = database.kw["bind"]

    def before_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if worker and get_ident() == worker[0] and statement == "BEGIN IMMEDIATE":
            waiting.set()

    def renew() -> bool:
        worker.append(get_ident())
        return ownership.renew(INSTANCE_ID, attempt, OWNER)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with database.begin() as db:
                row = db.get(NovncSessionRow, str(INSTANCE_ID))
                assert row
                expires = datetime.now(UTC) + timedelta(milliseconds=200)
                row.expires_at = timestamp(expires)
                db.flush()  # This transaction holds the writer until after expiry.
                future = pool.submit(renew)
                assert waiting.wait(2)
                expires = datetime.now(UTC) + timedelta(milliseconds=200)
                row.expires_at = timestamp(expires)
                db.flush()
                while datetime.now(UTC) <= expires:
                    time.sleep(0.01)
                assert not future.done()
            assert future.result(timeout=3) is False
        with database() as db:
            row = db.get(NovncSessionRow, str(INSTANCE_ID))
            assert row and row.expires_at == timestamp(expires)
        records = [json.loads(r.message) for r in caplog.records if r.name == LOGGER]
        evidence = next(r for r in records if r["event"] == "ownership_renewed")
        assert evidence["lock_ms"] >= 100
        assert evidence["accepted"] is False
        assert evidence["phase"] == "ownership_commit"
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
