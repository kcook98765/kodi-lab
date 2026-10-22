from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.mcp_administration import McpAuthenticationService
from kodi_lab.web.native_zip_installations import (
    NativeZipBodyLimitMiddleware,
    native_zip_router,
)
from tests.application.test_addon_zip_installations import factory as factory_fixture
from tests.application.test_native_zip_installations import (
    Fixture,
)
from tests.application.test_native_zip_installations import (
    native as native_fixture,
)
from tests.development.test_repository_zip import REPO_ID, repository_zip

native = native_fixture
factory = factory_fixture


@pytest.fixture
def client(native: Fixture) -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(NativeZipBodyLimitMiddleware)
    app.include_router(
        native_zip_router(
            native.service,
            McpAuthenticationService(native.sessions, clock=native.clock),
        )
    )
    with TestClient(app, client=("10.250.0.2", 1234)) as result:
        yield result


def auth(native: Fixture) -> dict[str, str]:
    return {"Authorization": "Bearer " + native.token}


def test_bearer_and_install_permission_required(
    client: TestClient, native: Fixture
) -> None:
    path = "/api/native-zip-installations"
    body = native.request.model_dump(mode="json")
    assert client.post(path, json=body).status_code == 401
    assert (
        client.post(
            path, json={**body, "path": "/tmp/arbitrary"}, headers=auth(native)
        ).status_code
        == 422
    )
    native.admin.remove_target_grant(
        native.principal.client_id, native.request.target_id
    )
    assert client.post(path, json=body, headers=auth(native)).status_code == 403
    assert native.broker.calls == []


def test_native_http_listing_ranges_status_and_exact_peer(
    client: TestClient, native: Fixture
) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/native-zip-installations" in paths
    assert not any(path.startswith("/native-zip-packages/") for path in paths)
    response = client.post(
        "/api/native-zip-installations",
        json=native.request.model_dump(mode="json"),
        headers=auth(native),
    )
    assert response.status_code == 202
    state = response.json()
    source = state["source_url"]
    # The fixture's configured public origin resolves through TestClient only.
    listing = client.get(source)
    filename = f"{REPO_ID}-1.0.0.zip"
    assert (
        listing.status_code == 200
        and f'<a href="{filename}">{filename}</a>' in listing.text
    )
    package = source + filename
    for _ in range(6):
        assert client.head(package).status_code == 200
    response = client.get(package, headers={"Range": "bytes=3-15"})
    assert response.status_code == 206 and response.content == repository_zip()[3:16]
    assert response.headers["content-range"] == f"bytes 3-15/{len(repository_zip())}"
    assert client.get(package, headers={"Range": "bytes=999999999-"}).status_code == 416
    assert client.get(source + "other.zip").status_code == 404
    assert client.get(package + "?path=/etc/passwd").status_code == 404
    with TestClient(client.app, client=("10.250.0.11", 1234)) as other:
        assert (
            other.get(package, headers={"X-Forwarded-For": "10.250.0.2"}).status_code
            == 404
        )
    result = client.post(
        f"/api/native-zip-installations/{state['operation_id']}/verify",
        headers=auth(native),
    )
    assert result.status_code == 200 and result.json()["state"] == "unverified"
    assert (
        client.get(
            f"/api/native-zip-installations/{state['operation_id']}",
            headers=auth(native),
        ).status_code
        == 200
    )


def test_disabled_client_revokes_delivery_and_api_access(
    client: TestClient, native: Fixture
) -> None:
    response = client.post(
        "/api/native-zip-installations",
        json=native.request.model_dump(mode="json"),
        headers=auth(native),
    )
    state = response.json()
    native.admin.set_client_enabled(native.principal.client_id, False)
    assert client.get(state["source_url"]).status_code == 404
    assert (
        client.get(
            f"/api/native-zip-installations/{state['operation_id']}",
            headers=auth(native),
        ).status_code
        == 401
    )


def test_forwarded_peer_cannot_authorize_package_delivery(
    client: TestClient, native: Fixture
) -> None:
    from typing import cast

    from starlette.types import ASGIApp
    from uvicorn._types import ASGI3Application
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    state = client.post(
        "/api/native-zip-installations",
        json=native.request.model_dump(mode="json"),
        headers=auth(native),
    ).json()
    package = state["source_url"] + f"{REPO_ID}-1.0.0.zip"
    # Exercise the actual server middleware: by route admission the client
    # address has already been rewritten to the otherwise authorized peer.
    # Starlette and Uvicorn describe the same ASGI contract with distinct types.
    proxied = ProxyHeadersMiddleware(
        cast(ASGI3Application, client.app), trusted_hosts=["127.0.0.1"]
    )
    with TestClient(cast(ASGIApp, proxied), client=("127.0.0.1", 1234)) as proxy:
        for path in (state["source_url"], package):
            assert (
                proxy.get(path, headers={"X-Forwarded-For": "10.250.0.2"}).status_code
                == 404
            )
    for header in ("Forwarded", "X-Real-IP"):
        assert client.get(package, headers={header: "10.250.0.2"}).status_code == 404


@pytest.mark.parametrize(
    "method,headers",
    [
        ("HEAD", {}),
        ("GET", {"Range": "bytes=99999999-"}),
        ("GET", {"Range": "bytes=invalid"}),
    ],
)
def test_head_and_rejected_ranges_do_not_supply_get_evidence(
    client: TestClient, native: Fixture, method: str, headers: dict[str, str]
) -> None:
    from uuid import UUID

    from kodi_lab.storage.models import NativeZipInstallationRow
    from tests.application.test_native_zip_installations import recognized

    state = client.post(
        "/api/native-zip-installations",
        json=native.request.model_dump(mode="json"),
        headers=auth(native),
    ).json()
    client.request(
        method, state["source_url"] + f"{REPO_ID}-1.0.0.zip", headers=headers
    )
    with native.sessions() as session:
        row = session.get(NativeZipInstallationRow, state["operation_id"])
        assert row is not None and row.zip_get_requested_at is None
    native.inventory.items = (recognized(),)
    native.broker.destination = "exact_payload_present"
    assert (
        native.service.verify(native.principal, UUID(state["operation_id"])).state
        == "unverified"
    )


def test_preparation_timeout_quarantines_workers_until_exit(
    client: TestClient, native: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    import kodi_lab.web.native_zip_installations as implementation

    state = client.post(
        "/api/native-zip-installations",
        json=native.request.model_dump(mode="json"),
        headers=auth(native),
    ).json()
    prepare = native.service.package
    release = threading.Event()
    entered = 0

    def blocked(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal entered
        entered += 1
        assert release.wait(timeout=5)
        return prepare(*args, **kwargs)

    monkeypatch.setattr(native.service, "package", blocked)
    monkeypatch.setattr(implementation, "PREPARATION_TIMEOUT_SECONDS", 0.01)
    try:
        for _ in range(4):
            assert client.get(state["source_url"]).status_code == 504
        assert client.get(state["source_url"]).status_code == 429
        assert entered == 4
    finally:
        release.set()
    monkeypatch.setattr(implementation, "PREPARATION_TIMEOUT_SECONDS", 15)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if client.get(state["source_url"]).status_code == 200:
            break
        time.sleep(0.01)
    else:
        pytest.fail("delivery workers did not release bounded slots")


def test_native_api_body_is_bounded_before_authentication(client: TestClient) -> None:
    assert (
        client.post("/api/native-zip-installations", content=b"x" * 4097).status_code
        == 413
    )

    def chunks() -> Iterator[bytes]:
        yield b"x" * 2048
        yield b"x" * 2049

    assert (
        client.post("/api/native-zip-installations", content=chunks()).status_code
        == 413
    )


def test_expiry_at_final_response_admission_rejects_bytes(
    client: TestClient, native: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from kodi_lab.application.native_zip_installations import DELIVERY_LIFETIME

    state = client.post(
        "/api/native-zip-installations",
        json=native.request.model_dump(mode="json"),
        headers=auth(native),
    ).json()
    admit = native.service.admit_delivery

    def expire(operation_id, *, record_get=False):  # type: ignore[no-untyped-def]
        native.clock.now += DELIVERY_LIFETIME + timedelta(seconds=1)
        native.service.sweep()
        return admit(operation_id, record_get=record_get)

    monkeypatch.setattr(native.service, "admit_delivery", expire)
    response = client.get(state["source_url"] + f"{REPO_ID}-1.0.0.zip")
    assert response.status_code == 404 and response.content != repository_zip()


@pytest.mark.parametrize(
    "method,suffix", [("GET", ""), ("HEAD", ""), ("HEAD", f"{REPO_ID}-1.0.0.zip")]
)
def test_listing_and_head_sends_are_timed_and_release_capacity(
    client: TestClient,
    native: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    suffix: str,
) -> None:
    import asyncio

    from starlette.types import Message, Receive, Scope, Send

    import kodi_lab.web.native_zip_installations as implementation

    state = client.post(
        "/api/native-zip-installations",
        json=native.request.model_dump(mode="json"),
        headers=auth(native),
    ).json()
    monkeypatch.setattr(implementation, "SEND_TIMEOUT_SECONDS", 0.01)

    async def stalled(scope: Scope, receive: Receive, send: Send) -> None:
        async def slow_send(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] == 200:
                await asyncio.sleep(5)
            await send(message)

        await client.app(scope, receive, slow_send)

    with TestClient(
        stalled, client=("10.250.0.2", 4321), raise_server_exceptions=False
    ) as slow:
        for _ in range(5):
            assert slow.request(method, state["source_url"] + suffix).status_code == 500
    monkeypatch.setattr(implementation, "SEND_TIMEOUT_SECONDS", 15)
    assert client.get(state["source_url"]).status_code == 200
