"""Authenticated prerequisite reads must not converge runtime state."""

from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.web.conftest import INSTANCE_ID, login


def test_capability_prerequisites_require_web_auth(client: TestClient) -> None:
    assert (
        client.get(f"/api/instances/{INSTANCE_ID}/capability-prerequisites").status_code
        == 401
    )
    response = client.get(
        f"/instances/{INSTANCE_ID}/capability-prerequisites", follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_authenticated_metadata_does_not_probe_converge_or_mutate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    context = cast(FastAPI, client.app).state.kodi_lab

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("read invoked runtime/Bridge machinery")

    for obj, name in (
        (context.status, "get"),
        (context.status, "get_observation"),
        (context.managed_bridge_trust, "prepare"),
        (context.managed_bridge_trust.broker, "managed_container_inventory"),
    ):
        monkeypatch.setattr(obj, name, forbidden)
    result = client.get(f"/api/instances/{INSTANCE_ID}/capability-prerequisites")
    assert result.status_code == 200
    payload = result.json()
    assert payload["assessment_scope"] == "stored_prerequisites"
    assert payload["native_readiness"] == payload["prompt_state"] == "unknown"
    assert payload["trust_state"] == "no_record"
    page = client.get(f"/instances/{INSTANCE_ID}/capability-prerequisites")
    assert page.status_code == 200 and "does not prove installation" in page.text
    assert (
        "native fallback" in page.text
        and "current availability is unknown" in page.text
    )
    assert (
        client.get(f"/api/instances/{uuid4()}/capability-prerequisites").status_code
        == 404
    )
