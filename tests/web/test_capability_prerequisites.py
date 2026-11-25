"""Reviewing setup metadata never dispatches a managed runtime operation."""

import pytest
from fastapi.testclient import TestClient

from kodi_lab.managed.broker import BrokerClient
from tests.web.conftest import FakeBroker, login
from tests.web.test_runtime_installations import Flow


def test_runtime_metadata_requires_web_auth(client: TestClient) -> None:
    assert client.get("/api/runtime-installations").status_code == 401
    response = client.get("/runtimes", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_authenticated_metadata_does_not_prepare_or_start_managed_kodi(
    client: TestClient, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Metadata read contacted the managed broker")

    monkeypatch.setattr(BrokerClient, "request", forbidden)
    flow = Flow()
    flow.attach(broker)
    login(client)
    result = client.get("/api/runtime-installations")
    assert result.status_code == 200
    entries = result.json()["installations"]
    assert {entry["generation"] for entry in entries} == {
        "matrix",
        "nexus",
        "omega",
        "piers",
    }
    assert all(entry["status"] == "not_installed" for entry in entries)
    page = client.get("/runtimes")
    assert page.status_code == 200 and "recorded runtime eligibility" in page.text
    assert not flow.launches
