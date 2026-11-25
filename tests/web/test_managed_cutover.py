"""The application routes Web actions through the restricted managed client."""

from collections.abc import Mapping

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kodi_lab.managed.broker import BrokerClient
from kodi_lab.managed.protocol import OperationReply
from tests.web.conftest import ORIGIN, login


def test_managed_page_replaces_slot_receipts_and_uses_session_authority(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, dict[str, str]]] = []

    def request(
        self: BrokerClient, actor: str, name: str, args: Mapping[str, str]
    ) -> OperationReply:
        calls.append((actor, name, dict(args)))
        return {"result": {"state": "ready"}, "elapsed_seconds": 0.1}

    monkeypatch.setattr(BrokerClient, "request", request)
    url = "/api/managed-kodi/omega/actions"
    operation = {"operation": "managed_start"}
    assert client.post(url, json=operation).status_code == 401
    csrf = login(client)
    assert client.post(url, json=operation).status_code == 403
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert (
        client.post(url, json={"operation": "docker"}, headers=headers).status_code
        == 404
    )
    response = client.post(url, json=operation, headers=headers)
    assert response.json()["result"] == {"state": "ready"}
    assert calls == [("admin", "managed_start", {})]
    page = client.get("/managed-kodi")
    assert "Take Control" in page.text
    assert "img-src 'self' blob: data:" in page.headers["content-security-policy"]
    overview = client.get("/kodi")
    assert overview.status_code == 200
    assert "Open Managed Kodi" in overview.text
    assert "img-src 'self' blob:;" in overview.headers["content-security-policy"]
    assert "Legacy Current Managed" not in page.text
    assert client.get("/managed-kodi/omega/development").status_code == 404
    assert client.get("/api/managed-kodi/requests/old").status_code == 404
    nexus = "/api/managed-kodi/nexus/actions"
    assert client.post(nexus, json=operation, headers=headers).status_code == 200
    assert calls[-1] == ("admin", "managed_start", {"generation": "nexus"})
    assert (
        client.post(
            nexus,
            json={**operation, "arguments": {"generation": "matrix"}},
            headers=headers,
        ).status_code
        == 400
    )
    assert (
        'data-managed-kodi="nexus"' in client.get("/managed-kodi?generation=nexus").text
    )


def test_managed_view_rejects_cross_origin_before_contacting_broker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def rejected(*args: object, **kwargs: object) -> OperationReply:
        pytest.fail("The broker received an unauthorized viewer request")

    monkeypatch.setattr(BrokerClient, "request", rejected)
    login(client)
    with (
        pytest.raises(WebSocketDisconnect) as denial,
        client.websocket_connect(
            "wss://127.0.0.1/api/managed-kodi/omega/view",
            headers={"Origin": "https://unrelated.example"},
            subprotocols=["novnc"],
        ),
    ):
        pass
    assert denial.value.code == 4403


@pytest.mark.parametrize("phase", ["starting", "stopping"])
def test_managed_status_renders_lifecycle_without_waiting_for_snapshots(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    def request(
        self: BrokerClient, actor: str, name: str, args: Mapping[str, str]
    ) -> OperationReply:
        assert (actor, name, args) == (
            "admin",
            "managed_status",
            {"generation": "nexus"},
        )
        return {"result": {"state": phase, "owner": "agent"}, "elapsed_seconds": 0.1}

    monkeypatch.setattr(BrokerClient, "request", request)
    login(client)
    response = client.get("/api/managed-kodi/nexus")
    assert response.status_code == 200
    assert response.json() == {"profile": {"state": phase, "owner": "agent"}}
