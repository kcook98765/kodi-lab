"""Durable MCP semantic-remote replay and race behavior."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.browser_remote import (
    BrowserRemoteError,
    BrowserRemoteResult,
    RemoteActorContext,
    ScreenshotRefreshResult,
)
from kodi_lab.application.mcp_remote_actions import (
    McpRemoteActionReceiptError,
    McpRemoteActionReceiptService,
    McpRemoteActionResult,
)
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import McpRemoteActionReceiptRow

CLIENT_ID = UUID("21000000-0000-4000-8000-000000000001")
CREDENTIAL_ID = UUID("22000000-0000-4000-8000-000000000001")
TARGET_ID = UUID("23000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


@dataclass
class FakeRemote:
    calls: list[tuple[UUID, BrowserRemoteAction, RemoteActorContext, UUID]] = field(
        default_factory=list
    )
    entered: Event | None = None
    release: Event | None = None
    error: BrowserRemoteError | None = None

    def invoke(
        self,
        target_id: UUID,
        action: BrowserRemoteAction,
        *,
        actor: RemoteActorContext | None = None,
        invocation_id: UUID | None = None,
        before_dispatch: Callable[[], None] | None = None,
    ) -> BrowserRemoteResult:
        if before_dispatch is not None:
            before_dispatch()
        assert actor is not None and invocation_id is not None
        self.calls.append((target_id, action, actor, invocation_id))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(timeout=5)
        if self.error is not None:
            raise self.error
        return BrowserRemoteResult(
            invocation_id=invocation_id,
            managed_instance_id=target_id,
            action=action,
            result="succeeded",
            completed_at=NOW,
            screenshot_refresh=ScreenshotRefreshResult(result="not_requested"),
        )


@dataclass
class FakeLimiter:
    calls: list[tuple[str, UUID]] = field(default_factory=list)
    denied: bool = False

    def require_allowed(self, actor_key: str, target_id: UUID) -> None:
        self.calls.append((actor_key, target_id))
        if self.denied:
            raise BrowserRemoteError("visual_rate_limited", "limited")


@pytest.fixture
def receipts(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'receipts.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def service(
    receipts: sessionmaker[Session],
    remote: FakeRemote,
    limiter: FakeLimiter,
    *,
    max_per_client: int = 10_000,
    max_global: int = 100_000,
) -> McpRemoteActionReceiptService:
    return McpRemoteActionReceiptService(
        receipts,
        remote,
        limiter,
        clock=lambda: NOW,
        max_per_client=max_per_client,
        max_global=max_global,
    )


def invoke(
    receipt_service: McpRemoteActionReceiptService,
    request_id: UUID,
    *,
    action: BrowserRemoteAction = BrowserRemoteAction.LEFT,
    target_id: UUID = TARGET_ID,
    credential_id: UUID = CREDENTIAL_ID,
) -> McpRemoteActionResult:
    return receipt_service.invoke(
        client_id=CLIENT_ID,
        credential_id=credential_id,
        target_id=target_id,
        request_id=request_id,
        action=action,
    )


def test_completed_replay_survives_credential_rotation_without_rate_or_dispatch(
    receipts: sessionmaker[Session],
) -> None:
    remote = FakeRemote()
    limiter = FakeLimiter()
    receipt_service = service(receipts, remote, limiter)
    request_id = uuid4()

    first = invoke(receipt_service, request_id)
    replay = invoke(receipt_service, request_id, credential_id=uuid4())

    assert replay == first
    assert len(remote.calls) == len(limiter.calls) == 1
    assert remote.calls[0][2] == RemoteActorContext.mcp_client(CLIENT_ID)


def test_same_client_request_id_with_changed_parameters_is_a_stable_conflict(
    receipts: sessionmaker[Session],
) -> None:
    remote = FakeRemote()
    receipt_service = service(receipts, remote, FakeLimiter())
    request_id = uuid4()
    invoke(receipt_service, request_id)

    with pytest.raises(McpRemoteActionReceiptError, match="request_id_conflict"):
        invoke(receipt_service, request_id, action=BrowserRemoteAction.RIGHT)
    with pytest.raises(McpRemoteActionReceiptError, match="request_id_conflict"):
        invoke(receipt_service, request_id, target_id=uuid4())
    assert len(remote.calls) == 1


def test_ambiguous_dispatch_and_restart_pending_never_redispatch(
    receipts: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = FakeRemote()
    limiter = FakeLimiter()
    first_service = service(receipts, remote, limiter)
    monkeypatch.setattr(
        first_service,
        "_finalize_success",
        lambda *_args: (_ for _ in ()).throw(SQLAlchemyError("lost update")),
    )
    request_id = uuid4()

    with pytest.raises(McpRemoteActionReceiptError, match="remote_action_ambiguous"):
        invoke(first_service, request_id)
    assert len(remote.calls) == 1
    with receipts() as session:
        row = session.query(McpRemoteActionReceiptRow).one()
        assert row.state == "pending" and row.final_result is None

    restarted = service(receipts, FakeRemote(), limiter)
    with pytest.raises(McpRemoteActionReceiptError, match="remote_action_ambiguous"):
        invoke(restarted, request_id)
    assert len(remote.calls) == 1
    assert len(limiter.calls) == 1


def test_explicit_ambiguous_result_is_durable_and_not_redispatched(
    receipts: sessionmaker[Session],
) -> None:
    remote = FakeRemote(
        error=BrowserRemoteError("remote_action_ambiguous", "uncertain")
    )
    receipt_service = service(receipts, remote, FakeLimiter())
    request_id = uuid4()
    for _ in range(2):
        with pytest.raises(
            McpRemoteActionReceiptError, match="remote_action_ambiguous"
        ):
            invoke(receipt_service, request_id)
    assert len(remote.calls) == 1


def test_stable_remote_rejection_is_replayed_without_redispatch(
    receipts: sessionmaker[Session],
) -> None:
    remote = FakeRemote(
        error=BrowserRemoteError("remote_no_active_player", "no player")
    )
    receipt_service = service(receipts, remote, FakeLimiter())
    request_id = uuid4()
    for _ in range(2):
        with pytest.raises(
            McpRemoteActionReceiptError, match="remote_no_active_player"
        ):
            invoke(
                receipt_service,
                request_id,
                action=BrowserRemoteAction.PLAY_PAUSE,
            )
    assert len(remote.calls) == 1


def test_simultaneous_identical_request_has_one_dispatch(
    receipts: sessionmaker[Session],
) -> None:
    entered = Event()
    release = Event()
    remote = FakeRemote(entered=entered, release=release)
    receipt_service = service(receipts, remote, FakeLimiter())
    request_id = uuid4()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, receipt_service, request_id)
        assert entered.wait(timeout=5)
        second = pool.submit(invoke, receipt_service, request_id)
        with pytest.raises(
            McpRemoteActionReceiptError, match="remote_action_ambiguous"
        ):
            second.result(timeout=5)
        release.set()
        assert first.result(timeout=5).outcome == "succeeded"
    assert len(remote.calls) == 1


def test_simultaneous_changed_action_conflicts_without_second_dispatch(
    receipts: sessionmaker[Session],
) -> None:
    entered = Event()
    release = Event()
    remote = FakeRemote(entered=entered, release=release)
    receipt_service = service(receipts, remote, FakeLimiter())
    request_id = uuid4()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, receipt_service, request_id)
        assert entered.wait(timeout=5)
        second = pool.submit(
            invoke,
            receipt_service,
            request_id,
            action=BrowserRemoteAction.RIGHT,
        )
        with pytest.raises(McpRemoteActionReceiptError, match="request_id_conflict"):
            second.result(timeout=5)
        release.set()
        assert first.result(timeout=5).outcome == "succeeded"
    assert len(remote.calls) == 1


def test_rate_rejection_is_completed_and_capacity_is_bounded(
    receipts: sessionmaker[Session],
) -> None:
    remote = FakeRemote()
    limiter = FakeLimiter(denied=True)
    receipt_service = service(receipts, remote, limiter, max_per_client=1, max_global=1)
    request_id = uuid4()
    for _ in range(2):
        with pytest.raises(McpRemoteActionReceiptError, match="rate_limited"):
            invoke(receipt_service, request_id)
    with pytest.raises(
        McpRemoteActionReceiptError, match="remote_action_receipt_capacity"
    ):
        invoke(receipt_service, uuid4())
    assert len(limiter.calls) == 1
    assert remote.calls == []


def test_retention_cleanup_allows_reuse_only_after_policy_window(
    receipts: sessionmaker[Session],
) -> None:
    now = NOW
    remote = FakeRemote()
    limiter = FakeLimiter()
    receipt_service = McpRemoteActionReceiptService(
        receipts,
        remote,
        limiter,
        clock=lambda: now,
        retention=timedelta(days=30),
    )
    request_id = uuid4()
    invoke(receipt_service, request_id)
    now += timedelta(days=30, microseconds=1)
    invoke(receipt_service, request_id)
    assert len(remote.calls) == 2
