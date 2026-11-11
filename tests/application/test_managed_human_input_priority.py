"""Human ownership fences input before allocation and at shared dispatch."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.browser_remote_coordination import (
    HumanControlConflict,
    acquire_browser_remote,
    release_browser_remote,
)
from kodi_lab.application.mcp_remote_actions import (
    McpRemoteActionReceiptError,
    McpRemoteActionResult,
)
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.storage.models import McpRemoteActionReceiptRow, NovncSessionRow
from tests.application.test_mcp_foundation import TARGET_ID
from tests.application.test_mcp_foundation import mcp_database as mcp_database
from tests.application.test_mcp_remote_actions import FakeLimiter, FakeRemote, service


def human(
    sessions: sessionmaker[Session],
    *,
    mode: str = "interactive",
    state: str = "active",
    expired: bool = False,
) -> None:
    with sessions.begin() as db:
        db.add(
            NovncSessionRow(
                instance_id=str(TARGET_ID),
                attempt=str(uuid4()),
                owner="a" * 64,
                mode=mode,
                state=state,
                identity_json="{}",
                expires_at="2000-01-01T00:00:00.000000Z"
                if expired
                else "2099-01-01T00:00:00.000000Z",
                absolute_expires_at="2099-01-01T00:00:00.000000Z",
            )
        )


@pytest.mark.parametrize("state", ["pending", "active", "revoking", "blocked"])
@pytest.mark.parametrize("expired", [False, True])
def test_human_refusals_do_not_allocate_receipts_or_replay_on_release(
    mcp_database: sessionmaker[Session], state: str, expired: bool
) -> None:
    remote, limiter = FakeRemote(), FakeLimiter()
    receipt_service = service(
        mcp_database, remote, limiter, max_per_client=1, max_global=1
    )
    client_id, credential_id = uuid4(), uuid4()

    def invoke(request: UUID) -> McpRemoteActionResult:
        return receipt_service.invoke(
            client_id=client_id,
            credential_id=credential_id,
            target_id=TARGET_ID,
            request_id=request,
            action=BrowserRemoteAction.SELECT,
        )

    human(mcp_database, state=state, expired=expired)
    for _ in range(3):
        with pytest.raises(McpRemoteActionReceiptError, match="human_control_active"):
            invoke(uuid4())
    with mcp_database() as db:
        assert (
            db.scalar(select(func.count()).select_from(McpRemoteActionReceiptRow)) == 0
        )
    assert remote.calls == []
    assert len(limiter.calls) == 3
    with mcp_database.begin() as db:
        db.execute(delete(NovncSessionRow))  # Fixture models verified native release.
    assert remote.calls == []  # Release never dispatches or replays buffered keys.
    request = uuid4()
    result = invoke(request)
    assert len(remote.calls) == 1
    human(mcp_database)
    replay = invoke(request)
    assert replay.invocation_id == result.invocation_id
    assert len(remote.calls) == 1
    assert len(limiter.calls) == 4


def test_view_only_does_not_claim_input_and_refusal_closes_dispatch_lock(
    mcp_database: sessionmaker[Session],
) -> None:
    human(mcp_database)
    for _ in range(2):
        with pytest.raises(HumanControlConflict):
            acquire_browser_remote(mcp_database, TARGET_ID)
    with mcp_database.begin() as db:
        row = db.get(NovncSessionRow, str(TARGET_ID))
        assert row is not None
        row.mode = "view-only"
    handle = acquire_browser_remote(mcp_database, TARGET_ID)
    release_browser_remote(handle)
    remote = FakeRemote()
    service(mcp_database, remote, FakeLimiter()).invoke(
        client_id=uuid4(),
        credential_id=uuid4(),
        target_id=TARGET_ID,
        request_id=uuid4(),
        action=BrowserRemoteAction.SELECT,
    )
    assert len(remote.calls) == 1
