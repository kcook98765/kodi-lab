"""Exact, read-only authority checks cannot revive or replace durable fences."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import novnc_control
from kodi_lab.application.novnc_control import HumanViewerOwnership, timestamp
from kodi_lab.storage.models import NovncSessionRow

from .conftest import INSTANCE_ID
from .test_novnc_control import BINDING, OWNER, mutation
from .view_attempt_fixture import view_attempt


@pytest.mark.parametrize("successor_owner", [OWNER, "d" * 64])
def test_delayed_old_check_cannot_authorize_or_mutate_successor(
    database: sessionmaker[Session],
    successor_owner: str,
) -> None:
    ownership = HumanViewerOwnership(database)
    old_attempt, successor = view_attempt(), view_attempt()
    broker = str(uuid4())
    ownership.create(INSTANCE_ID, old_attempt, OWNER, BINDING, "interactive")
    ownership.claim(INSTANCE_ID, old_attempt, OWNER, BINDING, "interactive", broker)
    paused, release = Event(), Event()

    class DelayedRead(Session):
        def get(self, *args: Any, **kwargs: Any) -> Any:
            paused.set()
            assert release.wait(4)
            return super().get(*args, **kwargs)

    checking = HumanViewerOwnership(
        sessionmaker(bind=database.kw["bind"], class_=DelayedRead)
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            checking.authorized, INSTANCE_ID, old_attempt, OWNER, BINDING, "interactive"
        )
        try:
            assert paused.wait(2)
            ownership.revoke(INSTANCE_ID, old_attempt, OWNER)
            ownership.finish(INSTANCE_ID, old_attempt, broker, True)
            ownership.create(
                INSTANCE_ID, successor, successor_owner, BINDING, "view-only"
            )
            ownership.claim(
                INSTANCE_ID, successor, successor_owner, BINDING, "view-only", broker
            )
        finally:
            release.set()
        assert future.result(timeout=3) is False
    # Late heartbeat/cleanup from the old connection cannot affect its successor.
    ownership.revoke(INSTANCE_ID, old_attempt, OWNER)
    ownership.finish(INSTANCE_ID, old_attempt, broker, True)
    assert not ownership.valid(INSTANCE_ID, old_attempt, broker)
    assert ownership.valid(INSTANCE_ID, successor, broker)
    with database() as db:
        row = db.get(NovncSessionRow, str(INSTANCE_ID))
        assert row and row.expires_at == row.absolute_expires_at
        assert row.attempt == str(successor) and row.owner == successor_owner


def test_authority_checks_cannot_revive_expired_or_revoked_control(
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ownership = HumanViewerOwnership(database)
    other_process = HumanViewerOwnership(sessionmaker(bind=database.kw["bind"]))
    attempt, broker = view_attempt(), str(uuid4())
    deadline = ownership.create(INSTANCE_ID, attempt, OWNER, BINDING, "interactive")
    assert not ownership.authorized(INSTANCE_ID, attempt, OWNER, BINDING, "interactive")
    ownership.claim(INSTANCE_ID, attempt, OWNER, BINDING, "interactive", broker)
    assert other_process.authorized(INSTANCE_ID, attempt, OWNER, BINDING, "interactive")
    for binding, mode in [
        (replace(BINDING, started="replacement"), "interactive"),
        (BINDING, "view-only"),
    ]:
        assert not other_process.authorized(INSTANCE_ID, attempt, OWNER, binding, mode)
    with monkeypatch.context() as patch:
        patch.setattr(
            novnc_control,
            "timestamp",
            lambda value=None: deadline if value is None else timestamp(value),
        )
        assert not other_process.valid(INSTANCE_ID, attempt, broker)
        assert not ownership.authorized(
            INSTANCE_ID, attempt, OWNER, BINDING, "interactive"
        )
        ownership.expire_pending()
        with pytest.raises(IntegrityError):
            ownership.create(
                INSTANCE_ID,
                view_attempt(),
                OWNER,
                BINDING,
                "interactive",
                timestamp(datetime.now(UTC) + timedelta(seconds=700)),
            )
        with pytest.raises(IntegrityError):
            mutation(database)
    # Revocation is seen by independent readers even after an accepted heartbeat.
    ownership.revoke(INSTANCE_ID, attempt, OWNER)
    assert not other_process.valid(INSTANCE_ID, attempt, broker)
    assert not other_process.authorized(
        INSTANCE_ID, attempt, OWNER, BINDING, "interactive"
    )
    assert ownership.status(INSTANCE_ID) is not None
    with pytest.raises(IntegrityError):
        mutation(database)


def test_claim_is_not_visible_or_acknowledged_before_durable_commit(
    database: sessionmaker[Session],
) -> None:
    ownership = HumanViewerOwnership(database)
    attempt, broker = view_attempt(), str(uuid4())
    ownership.create(INSTANCE_ID, attempt, OWNER, BINDING, "interactive")
    committing, release = Event(), Event()
    engine = database.kw["bind"]

    def pause_commit(conn: Any) -> None:
        committing.set()
        assert release.wait(4)

    event.listen(engine, "commit", pause_commit)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                ownership.claim,
                INSTANCE_ID,
                attempt,
                OWNER,
                BINDING,
                "interactive",
                broker,
            )
            try:
                assert committing.wait(2)
                assert not future.done()
                assert not ownership.valid(INSTANCE_ID, attempt, broker)
                assert not ownership.authorized(
                    INSTANCE_ID, attempt, OWNER, BINDING, "interactive"
                )
            finally:
                release.set()
            future.result(timeout=3)
    finally:
        event.remove(engine, "commit", pause_commit)
    assert ownership.valid(INSTANCE_ID, attempt, broker)


def test_broker_restart_retains_expired_worker_fence_until_exact_proof(
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    ownership = HumanViewerOwnership(database)
    attempt, broker = view_attempt(), str(uuid4())
    ownership.create(INSTANCE_ID, attempt, OWNER, BINDING, "interactive")
    ownership.claim(INSTANCE_ID, attempt, OWNER, BINDING, "interactive", broker)
    ownership.started(INSTANCE_ID, attempt, broker, "old-worker")
    with database.begin() as db:
        row = db.get(NovncSessionRow, str(INSTANCE_ID))
        assert row
        row.expires_at = row.absolute_expires_at = timestamp(
            datetime.now(UTC) - timedelta(seconds=1)
        )
    seen: list[tuple[str, str | None, bool]] = []
    proven = False

    def proof(container: str, worker: str | None, interactive: bool) -> bool:
        seen.append((container, worker, interactive))
        return proven

    replacement = HumanViewerOwnership(sessionmaker(bind=database.kw["bind"]))
    restarted_broker = str(uuid4())
    with monkeypatch.context() as patch:
        clock = iter([0.0, 7.0])
        patch.setattr(
            novnc_control, "time", SimpleNamespace(monotonic=lambda: next(clock))
        )
        replacement.recover(restarted_broker, proof)
    assert replacement.status(INSTANCE_ID).state == "blocked"  # type: ignore[union-attr]
    assert not replacement.valid(INSTANCE_ID, attempt, broker)
    with pytest.raises(IntegrityError):
        replacement.create(INSTANCE_ID, view_attempt(), OWNER, BINDING, "interactive")
    with pytest.raises(IntegrityError):
        mutation(database)
    proven = True
    replacement.recover(restarted_broker, proof)
    assert seen == [(BINDING.container, "old-worker", True)] * 2
    assert ownership.status(INSTANCE_ID) is None
    mutation(database)


@pytest.mark.parametrize("ending", ["revoke", "web-crash"])
def test_live_connection_loss_or_cross_process_revocation_retains_input_fence(
    database: sessionmaker[Session],
    ending: str,
) -> None:
    import socket
    import struct
    from threading import Thread

    from kodi_lab.broker.novnc import DockerViewTunnel, ViewRequest
    from tests.broker.test_novnc_transport import DockerDouble

    ownership = HumanViewerOwnership(database)
    broker_owner = HumanViewerOwnership(sessionmaker(bind=database.kw["bind"]))
    attempt, broker_id = view_attempt(), str(uuid4())
    ownership.create(INSTANCE_ID, attempt, OWNER, BINDING, "interactive")
    ownership.claim(INSTANCE_ID, attempt, OWNER, BINDING, "interactive", broker_id)
    native = DockerDouble()
    tunnel = DockerViewTunnel(
        BINDING,
        ViewRequest(INSTANCE_ID, OWNER, 2, attempt, "interactive"),
        lambda: broker_owner.valid(INSTANCE_ID, attempt, broker_id),
        client=native,
        proof=lambda _: False,
    )
    web, broker = socket.socketpair()
    stopped = Event()
    errors: list[Exception] = []

    def run() -> None:
        try:
            tunnel.run(broker, stopped)
        except Exception as error:
            errors.append(error)
        finally:
            broker_owner.finish(
                INSTANCE_ID, attempt, broker_id, tunnel.result.terminated
            )

    worker = Thread(target=run)
    worker.start()
    try:
        native.helper.settimeout(1)
        nonce = b"n" * 32
        native.helper.sendall(
            struct.pack("!B3sI", 1, b"\0\0\0", 40) + b"KLVNCARM" + nonce
        )
        assert native.helper.recv(37) == struct.pack("!BI", 4, 32) + nonce
        # A live, previously authorized heartbeat and input reach this worker.
        heartbeat = struct.pack("!BI", 2, 0)
        frame = struct.pack("!BI", 1, 1) + b"x"
        web.sendall(heartbeat + frame)
        assert native.helper.recv(6) == frame
        if ending == "revoke":
            ownership.revoke(INSTANCE_ID, attempt, OWNER)
            # Even a newer heartbeat cannot override durable revocation.
            web.sendall(heartbeat + frame)
        else:
            # Web vanished without running its durable revoke/end cleanup.
            web.shutdown(socket.SHUT_WR)
        worker.join(2)
        assert not worker.is_alive() and not errors
        assert tunnel.result.bytes_up == 1
        assert tunnel.result.reason in (
            {"input_revoked", "revoked"} if ending == "revoke" else {"disconnect"}
        )
        assert ownership.status(INSTANCE_ID).state == "blocked"  # type: ignore[union-attr]
        with pytest.raises(IntegrityError):
            mutation(database)
        with pytest.raises(IntegrityError):
            ownership.create(INSTANCE_ID, view_attempt(), OWNER, BINDING, "interactive")
    finally:
        stopped.set()
        web.close()
        worker.join(3)
        broker.close()
    assert not worker.is_alive()
