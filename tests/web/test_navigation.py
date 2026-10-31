"""Shared navigation keeps existing administration reachable and authenticated."""

from __future__ import annotations

import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from tests.web.conftest import INSTANCE_ID, login


def test_login_enters_kodi_and_legacy_entry_has_no_script_fallbacks(
    client: TestClient,
) -> None:
    assert (
        client.get("/managed-instances", follow_redirects=False).headers["location"]
        == "/login"
    )
    login(client)
    assert client.get("/login", follow_redirects=False).headers["location"] == "/kodi"
    for fragment in ("", "#instances", "#managed-details"):
        response = client.get(f"/{fragment}")
        assert response.status_code == 200
        links = Links()
        links.feed(response.text)
        assert {"/kodi", "/managed-instances"} <= links.destinations
        assert {"instances", "managed-details"} <= links.ids
        assert "app.js" not in response.text
        assert "kodi-lab-poll-ms" not in response.text


def test_navigation_compatibility_and_keyboard_dom() -> None:
    node = shutil.which("node")
    assert node is not None, "Node is required for navigation behavior checks"
    subprocess.run(
        [node, str(Path(__file__).with_name("navigation_harness.cjs"))],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


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
    response = client.get(path, follow_redirects=False)
    assert response.status_code == (303 if path == "/kodi" else 401)
    if path == "/kodi":
        assert response.headers["location"] == "/login"
    login(client)
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors" in response.headers["content-security-policy"]
    assert client.post(path).status_code == 405


@pytest.mark.parametrize(
    ("path", "active"),
    [
        ("/", "/kodi"),
        ("/managed-instances", "/settings"),
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
        "/managed-instances",
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
