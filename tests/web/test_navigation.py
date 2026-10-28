"""Shared navigation keeps existing administration reachable and authenticated."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from tests.web.conftest import INSTANCE_ID, login


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.destinations: set[str] = set()
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            self.destinations.add(str(attributes["href"]))
        if attributes.get("id"):
            self.ids.add(str(attributes["id"]))


@pytest.mark.parametrize("path", ["/kodi", "/development", "/settings"])
def test_landing_pages_keep_existing_authentication(
    client: TestClient, path: str
) -> None:
    assert client.get(path).status_code == 401
    login(client)
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors" in response.headers["content-security-policy"]
    assert client.post(path).status_code == 405


@pytest.mark.parametrize(
    ("path", "active"),
    [
        ("/", "/"),
        ("/kodi", "/kodi"),
        ("/add-kodi", "/kodi"),
        ("/production-targets", "/kodi"),
        ("/mcp-clients", "/mcp-clients"),
        ("/development", "/development"),
        ("/external-targets", "/development"),
        ("/pair-kodi", "/development"),
        ("/runtimes", "/settings"),
        ("/development-sources", "/development"),
        ("/agent-workspaces", "/development"),
        ("/addon-zips", "/development"),
        ("/addon-repositories", "/development"),
        ("/snapshots/preserved", "/development"),
        ("/history/instances", "/development"),
        (f"/instances/{INSTANCE_ID}", "/settings"),
        (f"/instances/{INSTANCE_ID}/history", "/development"),
        (f"/instances/{INSTANCE_ID}/snapshots", "/development"),
        ("/settings", "/settings"),
        ("/kodi-repository", "/settings"),
    ],
)
def test_shared_navigation_and_sign_out_on_existing_pages(
    client: TestClient, path: str, active: str
) -> None:
    csrf = login(client)
    response = client.get(path)
    assert response.status_code == 200
    html = response.text
    navigation = re.search(r'<nav id="admin-navigation".*?</nav>', html, re.S)
    assert navigation is not None
    assert re.findall(r'<a href="([^"]+)"', navigation.group()) == [
        "/",
        "/kodi",
        "/mcp-clients",
        "/development",
        "/settings",
    ]
    assert re.findall(r'<a href="([^"]+)" aria-current="page"', html) == [active]
    assert 'aria-controls="admin-navigation"' in html
    assert 'href="#main-content"' in html
    assert '<main id="main-content" tabindex="-1"' in html
    assert 'action="/logout"' in navigation.group()
    assert f'name="csrf_token" value="{csrf}"' in navigation.group()
    assert html.count("navigation.js") == 1
    assert html.count("<main ") == 1


def test_grouped_links_resolve_including_fragments(client: TestClient) -> None:
    login(client)
    destinations: set[str] = set()
    for path in ["/", "/kodi", "/development", "/settings"]:
        links = Links()
        links.feed(client.get(path).text)
        destinations.update(links.destinations)
    assert {
        "/add-kodi",
        "/production-targets",
        "/external-targets",
        "/mcp-clients",
        "/runtimes",
        "/kodi-repository",
        "/development-sources",
        "/agent-workspaces",
        "/addon-zips",
        "/addon-repositories",
        "/snapshots/preserved",
        "/history/instances",
        "/production-targets#reviewed-bridge-builds",
    } <= destinations
    for destination in sorted(destinations):
        url = urlsplit(destination)
        assert not url.netloc
        response = client.get(url.path or "/")
        assert response.status_code == 200, destination
        if url.fragment:
            links = Links()
            links.feed(response.text)
            assert url.fragment in links.ids, destination


@pytest.fixture(autouse=True)
def development_admin_view(request: pytest.FixtureRequest) -> None:
    """Existing full-administration contracts run in Development view."""
    if "client" in request.fixturenames:
        client: TestClient = request.getfixturevalue("client")
        client.cookies.set("kodi_lab_admin_view", "development")
