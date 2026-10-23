"""View preferences affect presentation only, with isolated browser fixtures."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.web.admin_views import ADMIN_VIEW_COOKIE, admin_view_return_path

from .conftest import INSTANCE_ID, ORIGIN, FakeBroker, login
from .test_kodi_views import household as household
from .test_runtime_installations import Flow, seed_installation


def navigation(html: str) -> list[str]:
    nav = re.search(r'<nav id="admin-navigation".*?</nav>', html, re.S)
    assert nav is not None
    return re.findall(r'<a href="([^"]+)"', nav.group())


def test_first_use_standard_and_full_development(client: TestClient) -> None:
    login(client)
    assert ADMIN_VIEW_COOKIE not in client.cookies
    for path in ["/", "/kodi", "/mcp-clients", "/settings", "/add-kodi"]:
        page = client.get(path).text
        assert navigation(page) == ["/", "/kodi", "/mcp-clients", "/settings"]
        assert 'value="standard" aria-pressed="true"' in page
    assert "hidden data-managed-deep-link" in client.get("/").text
    assert 'id="development-kodi-heading"' not in client.get("/kodi").text
    assert "Create custom preset" not in client.get("/mcp-clients").text
    assert 'href="/development"' in client.get("/settings").text
    assert "workspaces" in client.get("/settings").text.lower()
    assert 'data-mode-controls disabled aria-describedby="mode-help" hidden' in (
        client.get("/add-kodi").text
    )
    client.cookies.set(ADMIN_VIEW_COOKIE, "development")
    assert len(navigation(client.get("/").text)) == 5
    assert 'id="managed-details"' in client.get("/").text
    assert 'id="development-kodi-heading"' in client.get("/kodi").text
    assert "Create custom preset" in client.get("/mcp-clients").text
    assert "Preserved snapshots" in client.get("/settings").text
    assert " data-standard-view" not in client.get("/add-kodi").text


@pytest.mark.parametrize(
    "path",
    ["/runtimes", "/development", "/external-targets", f"/instances/{INSTANCE_ID}"],
)
def test_standard_preserves_authenticated_deep_links(
    client: TestClient, path: str
) -> None:
    assert client.get(path, follow_redirects=False).status_code in (401, 303)
    login(client)
    response = client.get(path)
    assert response.status_code == 200
    assert "This is a Development administration page" in response.text
    assert "Choose Development" in response.text


def test_preference_auth_csrf_origin_persistence_and_no_state_writes(
    client: TestClient, database: sessionmaker[Session], broker: FakeBroker
) -> None:
    assert client.post("/admin-view", headers=ORIGIN).status_code == 401
    csrf = login(client)
    assert client.post("/admin-view", headers=ORIGIN).status_code == 403
    body = {"csrf_token": csrf, "mode": "development", "return_to": "/kodi?x=1#end"}
    assert client.post("/admin-view", data=body).status_code == 403
    assert (
        client.post(
            "/admin-view", headers={"Origin": "http://evil.invalid"}, data=body
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/admin-view", headers=ORIGIN, data={**body, "mode": "admin"}
        ).status_code
        == 400
    )
    writes: list[str] = []

    def record(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|ALTER)\b", statement, re.I):
            writes.append(statement)

    bind = database.kw["bind"]
    before = list(broker.calls)
    event.listen(bind, "before_cursor_execute", record)
    try:
        changed = client.post(
            "/admin-view", headers=ORIGIN, data=body, follow_redirects=False
        )
    finally:
        event.remove(bind, "before_cursor_execute", record)
    assert changed.status_code == 303
    assert changed.headers["location"] == "/kodi?x=1#end"
    cookie = changed.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Max-Age=31536000" in cookie
    assert not writes
    assert broker.calls == before
    assert client.cookies.get(ADMIN_VIEW_COOKIE) == "development"
    assert 'value="development" aria-pressed="true"' in client.get("/kodi").text
    assert client.get("/api/runtime-installations").status_code == 200
    # An untrusted preference cannot grant any mode beyond presentation.
    client.cookies.set(ADMIN_VIEW_COOKIE, "invalid")
    assert len(navigation(client.get("/kodi").text)) == 4


@pytest.mark.parametrize(
    "value", ["https://evil.invalid", "//evil.invalid", "/\\evil", "/\n"]
)
def test_view_return_path_rejects_external_redirects(value: str) -> None:
    assert admin_view_return_path(value) == "/"


def test_runtime_actions_work_in_both_views(
    client: TestClient, broker: FakeBroker
) -> None:
    flow = Flow()
    flow.attach(broker)
    csrf = login(client)
    initial = client.get("/api/runtime-installations").json()
    client.cookies.set(ADMIN_VIEW_COOKIE, "development")
    development = client.get("/api/runtime-installations").json()["installations"]
    assert development[:-1] == initial["installations"]
    assert development[-1]["generation"] == "piers"
    client.cookies.set(ADMIN_VIEW_COOKIE, "standard")
    accepted = client.post(
        "/api/runtime-installations/matrix", headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    )
    assert accepted.status_code == 202
    assert len(flow.launches) == 1
    assert not any(name == "start_managed_container" for name, _ in broker.calls)


def test_export_isolated_browser_fixtures(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    household: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real controllers render fixtures; no preview database/real broker changes."""
    flow = Flow()
    flow.attach(broker)
    monkeypatch.setattr(
        RuntimeInstallationService, "_eligible_ready", lambda _s, _r: True
    )
    monkeypatch.setattr(
        "kodi_lab.application.runtime_installations.recipe_target_id",
        lambda _r: INSTANCE_ID,
    )
    seed_installation(database, "omega", ready=True)
    login(client)
    routes: dict[str, dict[str, str]] = {}
    for mode in ["standard", "development"]:
        client.cookies.set(ADMIN_VIEW_COOKIE, mode)
        routes[mode] = {
            path: client.get(path).text
            for path in [
                "/",
                "/kodi",
                "/add-kodi",
                "/add-kodi?mode=development",
                "/mcp-clients",
                "/settings",
                "/runtimes",
            ]
        }
        assert "Living room" in routes[mode]["/"]
        assert str(household) in routes[mode]["/kodi"]
    installations = client.get("/api/runtime-installations").json()
    assert [i["action"] for i in installations["installations"]] == [
        "install",
        "install",
        "open",
        "install",
    ]
    if destination := os.getenv("KODI_ADMIN_VIEW_EVIDENCE"):
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=True)
        (root / "fixture-routes.json").write_text(json.dumps(routes))
        (root / "fixture-installations.json").write_text(json.dumps(installations))
