"""Both MCP transports preserve bounded reasons without leaking unknown detail."""

from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.ui_state import ManagedUiObservationService
from kodi_lab.kodi.ui_state_native import (
    MANAGED_BRIDGE_RECOVERY_REASONS,
    UiObservationError,
)
from kodi_lab.storage.models import McpCredentialRow
from tests.web.conftest import (
    INSTANCE_ID,
    FakeBroker,
    guard_targeted_status_isolation,
    login,
)
from tests.web.test_mcp import _administration, _authority, _legacy_post, _post


@pytest.mark.parametrize(
    "reason", [*sorted(MANAGED_BRIDGE_RECOVERY_REASONS), "private-exception-detail"]
)
@pytest.mark.parametrize("tool", ["inspect_ui_state", "log_read"])
def test_source_reason_survives_adapters_and_both_transports_with_allowlisting(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    tool: str,
) -> None:
    path = f"/api/instances/{INSTANCE_ID}/ui-state"
    if tool == "inspect_ui_state":
        assert client.get(path).status_code == 401
        login(client)
    guard_targeted_status_isolation(database, broker, client, monkeypatch)
    _, token = _authority(client)

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise UiObservationError("identity_mismatch", reason=reason)

    monkeypatch.setattr(
        ManagedUiObservationService,
        "inspect" if tool == "inspect_ui_state" else "read_log",
        fail,
    )
    for post in (_post, _legacy_post):
        result = post(
            client,
            token,
            "tools/call",
            params={"name": tool, "arguments": {"target_id": str(INSTANCE_ID)}},
        ).json()["result"]
        assert result["isError"] and "structuredContent" not in result
        text = result["content"][0]["text"]
        if reason in MANAGED_BRIDGE_RECOVERY_REASONS:
            assert text.startswith(f"identity_mismatch: {reason}\nnext_action:")
            assert "Web administrator" in text
            assert "direct workspace deployment" in text
        else:
            assert text == "identity_mismatch"
    if tool == "inspect_ui_state":
        response = client.get(path)
        assert response.status_code == 503
        assert response.json()["detail"] == "identity_mismatch"
        if reason in MANAGED_BRIDGE_RECOVERY_REASONS:
            assert response.json()["reason"] == reason
            assert "Web administrator" in response.json()["next_action"]
            assert response.headers["cache-control"] == "no-store"
        else:
            assert response.json() == {"detail": "identity_mismatch"}


@pytest.mark.parametrize("tool", ["inspect_ui_state", "log_read"])
def test_unauthorized_target_does_not_reach_observation_or_disclose_recovery(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    agent, token = _authority(client)
    _administration(client).remove_target_grant(agent, INSTANCE_ID)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Unauthorized caller reached observation")

    monkeypatch.setattr(
        ManagedUiObservationService,
        "inspect" if tool == "inspect_ui_state" else "read_log",
        forbidden,
    )
    for post in (_post, _legacy_post):
        result = post(
            client,
            token,
            "tools/call",
            params={"name": tool, "arguments": {"target_id": str(INSTANCE_ID)}},
        ).json()["result"]
        assert result["isError"]
        assert "managed_bridge" not in result["content"][0]["text"]


@pytest.mark.parametrize("lost", ["grant", "credential"])
@pytest.mark.parametrize("tool", ["inspect_ui_state", "log_read"])
@pytest.mark.parametrize("transport", ["modern", "legacy"])
def test_authority_lost_during_read_suppresses_recovery_details(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    lost: str,
    tool: str,
    transport: str,
) -> None:
    guard_targeted_status_isolation(database, broker, client, monkeypatch)
    agent, token = _authority(client)
    admin = _administration(client)
    with database() as session:
        credential = session.scalar(
            select(McpCredentialRow).where(McpCredentialRow.client_id == str(agent))
        )
        assert credential is not None
        credential_id = UUID(credential.id)
    reads: list[str] = []

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        reads.append(tool)
        if lost == "grant":
            admin.remove_target_grant(agent, INSTANCE_ID)
        else:
            admin.revoke_credential(agent, credential_id)
        raise UiObservationError(
            "identity_mismatch", reason="managed_bridge_trust_changed"
        )

    monkeypatch.setattr(
        ManagedUiObservationService,
        "inspect" if tool == "inspect_ui_state" else "read_log",
        fail,
    )
    post = _post if transport == "modern" else _legacy_post
    result = post(
        client,
        token,
        "tools/call",
        params={"name": tool, "arguments": {"target_id": str(INSTANCE_ID)}},
    ).json()["result"]
    assert reads == [tool] and result["isError"]
    text = result["content"][0]["text"]
    assert "managed_bridge" not in text and "next_action" not in text
