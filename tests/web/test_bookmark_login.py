"""Browser page admission retains the API authentication boundary."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.storage.models import WebSessionRow

from .conftest import ORIGIN, PASSWORD, login

BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


@pytest.mark.parametrize("path", ["/managed-kodi", "/mcp-clients", "/development"])
def test_signed_out_bookmark_redirects_to_fixed_login(
    client: TestClient, path: str
) -> None:
    response = client.get(
        path + "?next=https://attacker.invalid", headers=BROWSER, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "set-cookie" not in response.headers
    page = client.get(path, headers=BROWSER)
    assert page.status_code == 200
    assert page.url.path == "/login"
    assert 'name="password"' in page.text


@pytest.mark.parametrize("accept", ["application/json", "*/*", "text/html"])
@pytest.mark.parametrize(
    "path",
    ["/api/runtime-installations", "/api/mcp/clients", "/api/managed-kodi/omega"],
)
def test_api_auth_errors_never_redirect(
    client: TestClient, accept: str, path: str
) -> None:
    response = client.get(path, headers={"Accept": accept}, follow_redirects=False)
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    assert "location" not in response.headers


def test_json_page_client_retains_401_and_mcp_retains_auth_error(
    client: TestClient,
) -> None:
    assert (
        client.get("/managed-kodi", headers={"Accept": "application/json"}).status_code
        == 401
    )
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Accept": "application/json, text/event-stream, text/html",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/list",
        },
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert "location" not in response.headers
    assert (
        client.post("/logout", headers=BROWSER, follow_redirects=False).status_code
        == 403
    )


def test_valid_session_logout_and_revisit(client: TestClient) -> None:
    csrf = login(client)
    assert client.get("/managed-kodi", headers=BROWSER).status_code == 200
    assert client.get("/api/runtime-installations").status_code == 200
    signed_out = client.post(
        "/logout", data={"csrf_token": csrf}, headers=ORIGIN, follow_redirects=False
    )
    assert signed_out.status_code == 303
    assert "Max-Age=0" in signed_out.headers["set-cookie"]
    assert (
        client.get("/managed-kodi", headers=BROWSER, follow_redirects=False).headers[
            "location"
        ]
        == "/login"
    )
    # Login always uses the existing fixed landing page, ignoring return parameters.
    signed_in = client.post(
        "/login?next=//attacker.invalid",
        data={"password": PASSWORD, "return_to": "https://attacker.invalid"},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == "/kodi"


def test_expired_session_bookmark_and_api(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    login(client)
    with database.begin() as session:
        session.execute(
            update(WebSessionRow).values(
                absolute_expires_at="2000-01-01T00:00:00.000000Z"
            )
        )
    response = client.get("/managed-kodi", headers=BROWSER, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/api/runtime-installations", headers=BROWSER).status_code == 401
