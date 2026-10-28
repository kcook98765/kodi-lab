"""Durable human-only ownership, cross-process writers and handoff failures."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Thread
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.browser_remote_coordination import (
    BrowserRemoteCoordinationConflict,
    acquire_browser_remote,
    release_browser_remote,
)
from kodi_lab.application.live_display import LiveDisplaySessionRegistry
from kodi_lab.application.novnc_control import (
    HumanControlBusyError,
    HumanViewerOwnership,
    timestamp,
)
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.domain import ManagedInstanceMutationKind
from kodi_lab.storage.models import ManagedInstanceMutationLeaseRow, NovncSessionRow
from kodi_lab.storage.repositories import ManagedInstanceMutationLeaseRepository
from kodi_lab.web.novnc import ChangeMode, PrototypeViewer, StartView

from .conftest import INSTANCE_ID, REVISION_ID

OWNER = "c" * 64
BINDING = ViewBinding("a" * 64, "sha256:" + "b" * 64, "start", (str(REVISION_ID), 1))


def test_late_busy_control_refusal_cannot_promise_an_untouched_attachment(
    database: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ownership = HumanViewerOwnership(database)
    viewer = PrototypeViewer(
        tmp_path / "unused.sock",
        lambda _: BINDING,
        ownership=ownership,
        registry=LiveDisplaySessionRegistry(),
    )
    item = viewer.start(INSTANCE_ID, OWNER, StartView(attempt=uuid4()))

    def released_then_busy(_instance: object) -> None:
        assert ownership.status(INSTANCE_ID) is None
        mutation(database)

    monkeypatch.setattr(ownership, "wait_released", released_then_busy)
    with pytest.raises(ValueError) as error:
        viewer.handoff(
            INSTANCE_ID,
            OWNER,
            ChangeMode(attempt=item.attempt, next_attempt=uuid4()),
            "interactive",
        )
    assert not isinstance(error.value, HumanControlBusyError)
    assert item.session.cancelled.is_set()
    assert ownership.status(INSTANCE_ID) is None


def create(ownership: HumanViewerOwnership, mode: str = "interactive") -> object:
    attempt = uuid4()
    ownership.create(INSTANCE_ID, attempt, OWNER, BINDING, mode)
    return attempt


def mutation(database: sessionmaker[Session]) -> None:
    with database.begin() as db:
        db.add(
            ManagedInstanceMutationLeaseRow(
                managed_instance_id=str(INSTANCE_ID),
                operation_kind=ManagedInstanceMutationKind.MCP_ADDON_SETTINGS,
                operation_id=str(uuid4()),
            )
        )


def test_control_excludes_existing_web_mcp_remote_and_all_mutations_until_proof(
    database: sessionmaker[Session],
) -> None:
    ownership = HumanViewerOwnership(database)
    attempt = uuid4()
    ownership.create(INSTANCE_ID, attempt, OWNER, BINDING, "interactive")
    broker = str(uuid4())
    ownership.claim(INSTANCE_ID, attempt, OWNER, BINDING, "interactive", broker)
    assert ownership.valid(INSTANCE_ID, attempt, broker)
    ownership.revoke(INSTANCE_ID, attempt, OWNER)
    assert not ownership.valid(INSTANCE_ID, attempt, broker)
    with pytest.raises(BrowserRemoteCoordinationConflict):
        acquire_browser_remote(database, INSTANCE_ID)
    with pytest.raises(IntegrityError):
        mutation(database)
    ownership.finish(INSTANCE_ID, attempt, broker, False)
    assert ownership.status(INSTANCE_ID).state == "blocked"  # type: ignore[union-attr]
    with pytest.raises(IntegrityError):
        mutation(database)
    ownership.finish(INSTANCE_ID, attempt, broker, True)
    handle = acquire_browser_remote(database, INSTANCE_ID)
    release_browser_remote(handle)
    mutation(database)


def test_inflight_remote_and_mutation_both_refuse_control(
    database: sessionmaker[Session],
) -> None:
    ownership = HumanViewerOwnership(database)
    handle = acquire_browser_remote(database, INSTANCE_ID)
    try:
        with pytest.raises(IntegrityError):
            create(ownership)
        with pytest.raises(IntegrityError):
            mutation(database)
    finally:
        release_browser_remote(handle)
    mutation(database)
    with pytest.raises(IntegrityError):
        create(ownership)


def test_view_only_allows_remote_and_cancellation_cannot_be_claimed(
    database: sessionmaker[Session],
) -> None:
    ownership = HumanViewerOwnership(database)
    attempt = uuid4()
    ownership.create(INSTANCE_ID, attempt, OWNER, BINDING, "view-only")
    handle = acquire_browser_remote(database, INSTANCE_ID)
    release_browser_remote(handle)
    ownership.revoke(INSTANCE_ID, attempt, OWNER)
    with pytest.raises(ValueError):
        ownership.claim(INSTANCE_ID, attempt, OWNER, BINDING, "view-only", str(uuid4()))
    mutation(database)


def test_expiry_and_wrong_owner_never_renew_or_free_claimed_fence(
    database: sessionmaker[Session],
) -> None:
    ownership = HumanViewerOwnership(database)
    attempt = uuid4()
    broker = str(uuid4())
    deadline = ownership.create(INSTANCE_ID, attempt, OWNER, BINDING, "interactive")
    for owner, binding, mode in [
        ("d" * 64, BINDING, "interactive"),
        (
            OWNER,
            ViewBinding("d" * 64, BINDING.image, "start", BINDING.identity),
            "interactive",
        ),
        (OWNER, BINDING, "view-only"),
    ]:
        with pytest.raises(ValueError):
            ownership.claim(INSTANCE_ID, attempt, owner, binding, mode, broker)
    ownership.claim(INSTANCE_ID, attempt, OWNER, BINDING, "interactive", broker)
    assert not ownership.renew(INSTANCE_ID, attempt, "d" * 64)
    with database.begin() as db:
        row = db.get(NovncSessionRow, str(INSTANCE_ID))
        assert row
        row.expires_at = timestamp(datetime.now(UTC) - timedelta(seconds=1))
    assert not ownership.valid(INSTANCE_ID, attempt, broker)
    assert not ownership.renew(INSTANCE_ID, attempt, OWNER)
    ownership.revoke(INSTANCE_ID, attempt, "d" * 64)
    assert ownership.status(INSTANCE_ID).deadline == deadline  # type: ignore[union-attr]
    with pytest.raises(IntegrityError):
        mutation(database)


@pytest.mark.parametrize("agent_operation", ["remote", "mutation"])
def test_controller_and_agent_concurrent_admission_have_one_winner(
    database: sessionmaker[Session],
    agent_operation: str,
) -> None:
    ownership = HumanViewerOwnership(database)
    barrier = Barrier(2)
    handles = []

    def control() -> str:
        barrier.wait(timeout=2)
        try:
            create(ownership)
            return "control"
        except IntegrityError:
            return "control_excluded"

    def remote() -> str:
        barrier.wait(timeout=2)
        try:
            if agent_operation == "mutation":
                with database.begin() as db:
                    ManagedInstanceMutationLeaseRepository(db).reserve(
                        INSTANCE_ID,
                        kind=ManagedInstanceMutationKind.MCP_ADDON_SETTINGS,
                        operation_id=uuid4(),
                    )
            else:
                handles.append(acquire_browser_remote(database, INSTANCE_ID))
            return "agent"
        except (BrowserRemoteCoordinationConflict, IntegrityError):
            return "agent_excluded"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(control), pool.submit(remote)]
        outcomes = {future.result(timeout=4) for future in futures}
    assert outcomes in ({"control", "agent_excluded"}, {"agent", "control_excluded"})
    for handle in handles:
        release_browser_remote(handle)


def test_verified_handoff_carries_hard_deadline_and_invalidates_old_ticket(
    database: sessionmaker[Session],
) -> None:
    ownership = HumanViewerOwnership(database)
    viewer = PrototypeViewer(
        Path("/unused"),
        lambda _: BINDING,
        registry=LiveDisplaySessionRegistry(),
        ownership=ownership,
    )
    first = viewer.start(INSTANCE_ID, OWNER, StartView(attempt=uuid4()))
    second = viewer.handoff(
        INSTANCE_ID,
        OWNER,
        ChangeMode(attempt=first.attempt, next_attempt=uuid4()),
        "interactive",
    )
    assert second.mode == "interactive" and second.deadline == first.deadline
    with pytest.raises(ValueError):
        viewer.consume(INSTANCE_ID, OWNER, first.ticket)
    viewer.stop(OWNER, second.attempt)
    assert ownership.status(INSTANCE_ID) is None
    viewer.close()


def test_recovery_retains_unknown_workers_and_clears_only_proven_exact_binding(
    database: sessionmaker[Session],
) -> None:
    ownership = HumanViewerOwnership(database)
    attempt = uuid4()
    broker = str(uuid4())
    ownership.create(INSTANCE_ID, attempt, OWNER, BINDING, "interactive")
    ownership.claim(INSTANCE_ID, attempt, OWNER, BINDING, "interactive", broker)
    # A false proof retains exclusion. Use a short monotonic clock jump to avoid
    # a real six-second sleep in the failure path.
    seen = []

    def proof(container: str, exec_id: str | None, interactive: bool) -> bool:
        seen.append(container)
        return True

    ownership.recover(str(uuid4()), proof)
    assert seen == [BINDING.container] and ownership.status(INSTANCE_ID) is None


def test_real_release_waits_for_broker_proof(database: sessionmaker[Session]) -> None:
    ownership = HumanViewerOwnership(database)
    attempt = uuid4()
    broker = str(uuid4())
    ownership.create(INSTANCE_ID, attempt, OWNER, BINDING, "interactive")
    ownership.claim(INSTANCE_ID, attempt, OWNER, BINDING, "interactive", broker)
    ownership.revoke(INSTANCE_ID, attempt, OWNER)
    with pytest.raises(ValueError, match="unproven"):
        ownership.wait_released(INSTANCE_ID, timeout=0)
    ownership.finish(INSTANCE_ID, attempt, broker, True)
    ownership.wait_released(INSTANCE_ID, timeout=0)
    with database.begin() as db:
        db.execute(delete(ManagedInstanceMutationLeaseRow))


def test_revoke_racing_with_broker_claim_retains_claimed_fence(
    database: sessionmaker[Session],
) -> None:
    from threading import Event

    from sqlalchemy import event

    ownership = HumanViewerOwnership(database)
    attempt = uuid4()
    broker = str(uuid4())
    ownership.create(INSTANCE_ID, attempt, OWNER, BINDING, "interactive")
    entered = Event()
    release = Event()

    def intercept(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: object,
    ) -> None:
        if statement.startswith("DELETE FROM novnc_sessions"):
            entered.set()
            assert release.wait(2)

    engine = database.kw["bind"]
    event.listen(engine, "before_cursor_execute", intercept)
    worker = Thread(target=lambda: ownership.revoke(INSTANCE_ID, attempt, OWNER))
    try:
        worker.start()
        assert entered.wait(2)
        ownership.claim(INSTANCE_ID, attempt, OWNER, BINDING, "interactive", broker)
    finally:
        release.set()
        worker.join(2)
        event.remove(engine, "before_cursor_execute", intercept)
    row = ownership.status(INSTANCE_ID)
    assert row is not None and row.state == "revoking"
    with pytest.raises(IntegrityError):
        mutation(database)


def test_ambiguous_remote_requires_exact_native_stop_before_human_control(
    database: sessionmaker[Session],
) -> None:
    from kodi_lab.application.browser_remote_coordination import (
        mark_remote_dispatch,
        retire_stopped_remote,
    )

    ownership = HumanViewerOwnership(database)
    handle = acquire_browser_remote(database, INSTANCE_ID)
    mark_remote_dispatch(handle, BINDING.container)
    release_browser_remote(handle, unresolved=True)
    with pytest.raises(IntegrityError):
        create(ownership)
    retire_stopped_remote(database, INSTANCE_ID, "d" * 64)
    with pytest.raises(IntegrityError):
        create(ownership)
    retire_stopped_remote(database, INSTANCE_ID, BINDING.container)
    create(ownership)


def test_stop_fence_rejects_live_dispatcher_and_is_available_after_process_loss(
    database: sessionmaker[Session],
) -> None:
    from kodi_lab.application.browser_remote_coordination import stop_dispatch_fence

    handle = acquire_browser_remote(database, INSTANCE_ID)
    try:
        with (
            pytest.raises(BrowserRemoteCoordinationConflict),
            stop_dispatch_fence(database, INSTANCE_ID),
        ):
            pass
    finally:
        release_browser_remote(handle, unresolved=True)
    # Kernel ownership is gone; the durable unknown-dispatch fence is retained
    # until the existing Stop operation obtains its exact stopped receipt.
    with stop_dispatch_fence(database, INSTANCE_ID), pytest.raises(IntegrityError):
        create(HumanViewerOwnership(database))


def test_default_app_native_stop_recovers_orphan_human_ownership(
    database: sessionmaker[Session], client: object, broker: object
) -> None:
    from fastapi.testclient import TestClient

    from kodi_lab.broker.docker_inventory import ContainerRuntimeState

    from .conftest import ORIGIN, FakeBroker, inventory_entry, login

    assert isinstance(client, TestClient) and isinstance(broker, FakeBroker)
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    ownership = HumanViewerOwnership(database)
    attempt = uuid4()
    broker_id = str(uuid4())
    ownership.create(INSTANCE_ID, attempt, OWNER, BINDING, "interactive")
    ownership.claim(INSTANCE_ID, attempt, OWNER, BINDING, "interactive", broker_id)
    csrf = login(client)
    # The ordinary app has no VNC routes or VNC service. Existing native Stop
    # still retires the exact input incarnation and clears durable ownership.
    reply = client.post(
        f"/api/instances/{INSTANCE_ID}/stop",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert reply.status_code == 200, reply.text
    assert ownership.status(INSTANCE_ID) is None


def test_refused_mutation_does_not_revoke_exclusive_human_viewer(
    database: sessionmaker[Session],
) -> None:
    import time

    from kodi_lab.application.live_display import (
        LIVE_DISPLAY_REGISTRY,
        LiveDisplaySession,
    )
    from kodi_lab.storage.repositories import ManagedInstanceMutationLeaseRepository

    ownership = HumanViewerOwnership(database)
    create(ownership)
    display = LiveDisplaySession(
        session_id=uuid4(),
        instance_id=INSTANCE_ID,
        runtime_revision_id=REVISION_ID,
        container_id=BINDING.container,
        started_at=datetime.now(UTC),
        started_monotonic=time.monotonic(),
        owner=OWNER,
    )
    LIVE_DISPLAY_REGISTRY.acquire(display)
    try:
        with pytest.raises(IntegrityError), database.begin() as db:
            ManagedInstanceMutationLeaseRepository(db).reserve(
                INSTANCE_ID,
                kind=ManagedInstanceMutationKind.LIFECYCLE,
                operation_id=uuid4(),
            )
        assert not display.cancelled.is_set()
    finally:
        LIVE_DISPLAY_REGISTRY.release(display)
