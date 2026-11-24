from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.config import Settings
from kodi_lab.storage.models import (
    WebSessionRow,
)
from kodi_lab.web.app import create_app

from .conftest import (
    ORIGIN,
    PASSWORD,
    FakeBroker,
    login,
)


def test_login_cookie_is_opaque_and_hardened(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    response = client.post(
        "/login",
        data={"password": PASSWORD},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "kodi_lab_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Secure" in cookie
    raw_token = client.cookies.get("kodi_lab_session")
    assert raw_token is not None
    with database() as session:
        stored = session.scalar(select(WebSessionRow))
        assert stored is not None
        assert stored.token_digest != raw_token


def test_invalid_login_is_generic_and_origin_is_required(client: TestClient) -> None:
    rejected = client.post("/login", data={"password": "wrong"}, headers=ORIGIN)
    assert rejected.status_code == 401
    assert "Authentication failed." in rejected.text
    assert "wrong" not in rejected.text
    assert client.post("/login", data={"password": PASSWORD}).status_code == 403
    assert (
        client.post(
            "/login",
            data={"password": PASSWORD},
            headers={"Referer": "https://127.0.0.1/login"},
            follow_redirects=False,
        ).status_code
        == 303
    )


def test_login_rotates_existing_session(client: TestClient) -> None:
    login(client)
    first = client.cookies.get("kodi_lab_session")
    response = client.post(
        "/login",
        data={"password": PASSWORD},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    second = client.cookies.get("kodi_lab_session")
    assert first is not None and second is not None and first != second


def test_logout_requires_csrf_and_invalidates_cookie(client: TestClient) -> None:
    csrf = login(client)
    assert client.post("/logout", data={}, headers=ORIGIN).status_code == 403
    response = client.post(
        "/logout",
        data={"csrf_token": csrf},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/api/external-targets").status_code == 401


def test_cors_is_not_enabled(client: TestClient) -> None:
    login(client)
    response = client.options(
        "/api/external-targets",
        headers={
            "Origin": "http://attacker.invalid",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in response.headers


def test_unapproved_host_header_is_rejected(client: TestClient) -> None:
    assert client.get("/login", headers={"Host": "attacker.invalid"}).status_code == 400


def test_unconfigured_credentials_have_no_browser_bootstrap_route(
    database: sessionmaker[Session], broker: FakeBroker
) -> None:
    app = create_app(
        settings=Settings(),
        session_factory=database,
        broker=broker,
    )
    with TestClient(app, base_url="https://127.0.0.1") as test_client:
        page = test_client.get("/login")
        assert page.status_code == 200
        assert "kodi-lab-admin set-password" in page.text
        response = test_client.post(
            "/login", data={"password": "anything"}, headers=ORIGIN
        )
        assert response.status_code == 401
        assert test_client.post("/signup", headers=ORIGIN).status_code == 404


@pytest.fixture(autouse=True)
def development_admin_view(request: pytest.FixtureRequest) -> None:
    """Existing full-administration contracts run in Development view."""
    if "client" in request.fixturenames:
        client: TestClient = request.getfixturevalue("client")
        client.cookies.set("kodi_lab_admin_view", "development")
