from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kodi_lab.application.live_display import (
    LiveDisplayError,
    LiveDisplaySessionRegistry,
)
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.web.app import create_app
from kodi_lab.web.novnc import PrototypeViewer, StartView
from kodi_lab.web.novnc_protocol import incoming_limit

from . import conftest
from .conftest import INSTANCE_ID, ORIGIN, login


def viewer() -> PrototypeViewer:
    binding = ViewBinding(
        "a" * 64, "sha256:" + "b" * 64, "start-1", (str(uuid4()), 1, 1)
    )
    return PrototypeViewer(
        Path("/tmp/not-a-real-view-socket"),
        lambda _: binding,
        registry=LiveDisplaySessionRegistry(),
    )


def test_attachment_is_exact_owner_instance_and_one_use() -> None:
    service = viewer()
    start = service.start(INSTANCE_ID, "owner", StartView(attempt=uuid4()))
    with pytest.raises(ValueError):
        service.consume(INSTANCE_ID, "wrong", start.ticket)
    with pytest.raises(ValueError):
        service.consume(uuid4(), "owner", start.ticket)
    ticket = start.ticket
    assert service.consume(INSTANCE_ID, "owner", ticket) is start
    with pytest.raises(ValueError):
        service.consume(INSTANCE_ID, "owner", ticket)
    service.end(start)


def test_stop_before_start_lifecycle_cancellation_and_capacity() -> None:
    service = viewer()
    attempt = uuid4()
    service.stop("owner", attempt)
    with pytest.raises(ValueError):
        service.start(INSTANCE_ID, "owner", StartView(attempt=attempt))
    item = service.start(INSTANCE_ID, "owner", StartView(attempt=uuid4()))
    with pytest.raises(LiveDisplayError):
        service.start(INSTANCE_ID, "other", StartView(attempt=uuid4()))
    service.registry.cancel(INSTANCE_ID)
    assert item.session.cancelled.is_set()
    service.end(item)
    first = service.start(INSTANCE_ID, "owner", StartView(attempt=uuid4()))
    second = service.start(uuid4(), "owner", StartView(attempt=uuid4()))
    with pytest.raises(LiveDisplayError):
        service.start(uuid4(), "owner", StartView(attempt=uuid4()))
    service.close()
    assert first.session.cancelled.is_set() and second.session.cancelled.is_set()
    assert service.registry.active_count() == 0


def test_expired_pending_ticket_releases_capacity_and_cannot_attach() -> None:
    service = viewer()
    item = service.start(INSTANCE_ID, "owner", StartView(attempt=uuid4()))
    item.expires = 0
    with pytest.raises(ValueError):
        service.consume(INSTANCE_ID, "owner", item.ticket)
    assert service.registry.active_count() == 0


def test_existing_default_application_has_no_novnc_routes(client: TestClient) -> None:
    assert client.get(f"/instances/{INSTANCE_ID}/novnc-phase1").status_code == 404
    assert (
        client.post(f"/api/instances/{INSTANCE_ID}/novnc-start", json={}).status_code
        == 404
    )


def test_wire_limits_do_not_expand_jpeg_or_other_websockets() -> None:
    route = f"/api/instances/{INSTANCE_ID}/novnc"
    assert incoming_limit(route) == 65536
    for path in (
        route + "?token=x",
        route + "/",
        route.replace("novnc", "display"),
        "/mcp",
    ):
        assert incoming_limit(path) == 256


@pytest.fixture
def enabled_client(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> TestClient:
    create = create_app

    def configured(**kwargs: Any) -> Any:
        kwargs["novnc_viewer"] = viewer()
        return create(**kwargs)

    monkeypatch.setattr(conftest, "create_app", configured)
    return request.getfixturevalue("client")  # type: ignore[no-any-return]


def test_enabled_routes_require_admin_csrf_origin_and_closed_schema(
    enabled_client: TestClient,
) -> None:
    client = enabled_client
    path = f"/api/instances/{INSTANCE_ID}/novnc-start"
    body = {"attempt": str(uuid4()), "rate": 10}
    assert client.post(path, json=body, headers=ORIGIN).status_code in (401, 403)
    csrf = login(client)
    assert client.post(path, json=body, headers=ORIGIN).status_code == 403
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert (
        client.post(path, json={**body, "input": True}, headers=headers).status_code
        == 422
    )
    assert (
        client.post(
            path, json=body, headers={**headers, "Origin": "http://evil.test"}
        ).status_code
        == 403
    )
    reply = client.post(path, json=body, headers=headers)
    assert reply.status_code == 200 and reply.json()["mode"] == "view-only"
    assert "ticket" in reply.json()
    assert client.post(path, json=body, headers=headers).status_code == 409
    page = client.get(f"/instances/{INSTANCE_ID}/novnc-phase1")
    assert page.status_code == 200 and "No input or audio" in page.text


def test_websocket_rejects_missing_ticket_wrong_origin_and_query(
    enabled_client: TestClient,
) -> None:
    from starlette.websockets import WebSocketDisconnect

    login(enabled_client)
    path = f"/api/instances/{INSTANCE_ID}/novnc"
    for url, origin, protocols in [
        (path, "http://127.0.0.1", []),
        (path, "http://evil.test", ["novnc", "view.fake"]),
        (path + "?token=bad", "http://127.0.0.1", ["novnc", "view.fake"]),
        (path, "http://127.0.0.1", ["novnc", "view.fake"]),
    ]:
        with (
            pytest.raises(WebSocketDisconnect) as error,
            enabled_client.websocket_connect(
                url,
                headers={"Origin": origin, "Host": "127.0.0.1"},
                subprotocols=protocols,
            ),
        ):
            pass
        assert error.value.code == 4403
