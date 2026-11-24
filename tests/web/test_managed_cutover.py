"""The application routes Web actions through the restricted managed client."""

from collections.abc import Mapping

import pytest
from fastapi.testclient import TestClient

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
    assert "Legacy Current Managed" not in page.text
    assert client.get("/managed-kodi/omega/development").status_code == 404
    assert client.get("/api/managed-kodi/requests/old").status_code == 404
