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

from .conftest import INSTANCE_ID, ORIGIN, FakeBroker, login, seed_external_target
from .test_kodi_views import household as household
from .test_runtime_installations import Flow, seed_installation


def navigation(html: str) -> list[str]:
    nav = re.search(r'<nav id="admin-navigation".*?</nav>', html, re.S)
    assert nav is not None
    return re.findall(r'<a href="([^"]+)"', nav.group())


NAVIGATION = [
    "/kodi",
    "/managed-kodi",
    "/production-targets",
    "/external-targets",
    "/development",
    "/mcp-clients",
    "/settings",
]


@pytest.mark.parametrize("cookie", [None, "standard", "development", "invalid"])
def test_unified_navigation_ignores_legacy_cookie(
    client: TestClient, cookie: str | None
) -> None:
    login(client)
    if cookie is not None:
        client.cookies.set(ADMIN_VIEW_COOKIE, cookie)
    for path in ["/", "/kodi", "/mcp-clients", "/settings", "/add-kodi"]:
        page = client.get(path).text
        assert navigation(page) == NAVIGATION
        assert "data-admin-view-form" not in page
        assert "Choose Development" not in page
        assert "data-standard-view" not in page
    page = client.get("/kodi").text
    assert 'id="managed-summary-heading"' in page
    assert 'id="external_development-summary-heading"' in page
    assert "data-managed-slots" not in page
    assert 'id="managed-kodi-heading"' in client.get("/managed-kodi").text
    assert "Create custom preset" in client.get("/mcp-clients").text
    assert 'href="/agent-workspaces"' in client.get("/development").text
    assert 'data-mode-controls disabled aria-describedby="mode-help">' in (
        client.get("/add-kodi").text
    )


@pytest.mark.parametrize(
    "path",
    ["/runtimes", "/development", "/external-targets", f"/instances/{INSTANCE_ID}"],
)
def test_unified_navigation_preserves_authenticated_deep_links(
    client: TestClient, path: str
) -> None:
    assert client.get(path, follow_redirects=False).status_code in (401, 303)
    login(client)
    response = client.get(path)
    assert response.status_code == 200
    assert "Choose Development" not in response.text
    assert 'aria-current="page"' in response.text


@pytest.mark.parametrize("mode", ["standard", "development"])
def test_legacy_submission_auth_csrf_origin_and_no_authorization_state(
    client: TestClient, database: sessionmaker[Session], broker: FakeBroker, mode: str
) -> None:
    assert client.post("/admin-view", headers=ORIGIN).status_code == 401
    csrf = login(client)
    assert client.post("/admin-view", headers=ORIGIN).status_code == 403
    body = {"csrf_token": csrf, "mode": mode, "return_to": "/kodi?x=1#end"}
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
    assert "Max-Age=0" in cookie
    assert not writes
    assert broker.calls == before
    assert ADMIN_VIEW_COOKIE not in client.cookies
    assert "data-admin-view-form" not in client.get("/kodi").text
    assert client.get("/api/runtime-installations").status_code == 200
    # An untrusted preference cannot grant any mode beyond presentation.
    client.cookies.set(ADMIN_VIEW_COOKIE, "invalid")
    assert len(navigation(client.get("/kodi").text)) == 7


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
    assert development == initial["installations"]
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
    seed_external_target(database)
    csrf = login(client)
    presets = client.get("/api/mcp/presets").json()
    for name in ["Production Inspection", "Read-only Inspection"]:
        response = client.post(
            "/api/mcp/clients",
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
            json={
                "display_name": name + " agent",
                "preset_id": next(
                    p["id"] for p in presets if p["display_name"] == name
                ),
            },
        )
        assert response.status_code == 201
    routes: dict[str, dict[str, str]] = {}
    for mode in ["missing", "standard", "development", "invalid"]:
        if mode == "missing":
            client.cookies.pop(ADMIN_VIEW_COOKIE, None)
        else:
            client.cookies.set(ADMIN_VIEW_COOKIE, mode)
        routes[mode] = {
            path: client.get(path).text
            for path in [
                "/",
                "/kodi",
                "/managed-kodi",
                "/managed-instances",
                "/production-targets",
                "/external-targets",
                "/create-kodi",
                "/development-session",
                "/add-kodi",
                "/add-kodi?mode=development",
                "/mcp-clients",
                "/development",
                "/settings",
                "/runtimes",
            ]
        }
        assert "Living room" in routes[mode]["/production-targets"]
        assert str(household) in routes[mode]["/production-targets"]
        assert str(household) not in routes[mode]["/kodi"]
        assert str(household) not in routes[mode]["/managed-kodi"]
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
        apis = {
            path: client.get(path).json()
            for path in [
                "/api/managed-kodi/slots",
                "/api/development-session",
                "/api/instances",
            ]
        }
        (root / "fixture-apis.json").write_text(json.dumps(apis))


def test_older_template_contexts_cannot_restore_mode_filtering() -> None:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from starlette.requests import Request

    from kodi_lab.web.admin_views import admin_view_context

    renderer = Environment(
        loader=FileSystemLoader(
            Path(__file__).parents[2] / "src/kodi_lab/web/templates"
        ),
        autoescape=select_autoescape(),
    )
    rendered = []
    for context in [
        {},
        {"admin_view_enabled": True, "admin_view_mode": "standard"},
        {"admin_view_enabled": True, "admin_view_mode": "development"},
    ]:
        rendered.append(
            renderer.get_template("navigation.html").render(
                shared_navigation_enabled=True,
                nav_section="kodi",
                csrf_token="fixture",
                **context,
            )
        )
    assert len(set(rendered)) == 1
    assert navigation(rendered[0]) == NAVIGATION
    contexts = [
        admin_view_context(
            Request(
                {
                    "type": "http",
                    "headers": [(b"cookie", f"kodi_lab_admin_view={value}".encode())],
                }
            )
        )
        for value in ["standard", "development", "invalid", ""]
    ]
    assert all(context == contexts[0] for context in contexts)
