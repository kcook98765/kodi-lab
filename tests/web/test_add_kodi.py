import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from tests.web.conftest import login

WEB_ROOT = Path(__file__).resolve().parents[2] / "src/kodi_lab/web"


def test_add_kodi_requires_auth_and_preserves_legacy_page(client: TestClient) -> None:
    assert client.get("/add-kodi").status_code == 401
    csrf = login(client)
    response = client.get("/add-kodi")
    assert response.status_code == 200
    assert "<h1>Add Kodi</h1>" in response.text
    assert "Friendly Kodi name (optional)" in response.text
    home_name = response.text.split('id="kodi-name"', 1)[1].split(">", 1)[0]
    assert "required" not in home_name
    assert f'data-csrf-token="{csrf}"' in response.text
    assert 'data-recovery-namespace="' in response.text
    assert 'name="password" type="password"' in response.text
    assert "data-native-fields hidden disabled" in response.text
    assert 'method="post" action="/add-kodi" data-connect-form' in response.text
    assert "data-js-gate disabled" in response.text
    assert "data-manual-recovery" in response.text
    assert 'type="checkbox" required data-consent' in response.text
    assert 'type="checkbox" required data-consent checked' not in response.text
    assert 'name="kodi_use" value="home" checked' in response.text
    assert "unencrypted HTTP" in response.text
    assert "does not grant access to an MCP agent" in response.text
    assert "frame-ancestors" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/pair-kodi").status_code == 200
    assert 'href="/add-kodi"' in client.get("/kodi").text


def test_add_kodi_recovery_namespace_changes_on_new_sign_in(client: TestClient) -> None:
    import re

    def namespace() -> str:
        page = client.get("/add-kodi").text
        match = re.search(r'data-recovery-namespace="([0-9a-f]{64})"', page)
        assert match is not None
        return match.group(1)

    login(client)
    first = namespace()
    assert namespace() == first
    login(client)
    assert namespace() != first


def test_legacy_entry_template_needs_no_dashboard_data() -> None:
    template = Environment(
        loader=FileSystemLoader(WEB_ROOT / "templates")
    ).get_template("index.html")
    html = template.render(url_for=lambda *_args, **_kwargs: "/static")
    assert 'href="/kodi"' in html
    assert 'href="/managed-instances"' in html
    assert 'id="instances"' in html and 'id="managed-details"' in html
    assert "data-instance-id" not in html


def test_add_kodi_browser_recovery_and_secret_handling() -> None:
    node = shutil.which("node")
    assert node is not None, "Node is required for the Add Kodi behavior checks"
    subprocess.run(
        [
            node,
            str(Path(__file__).with_name("add_kodi_browser_harness.js")),
            str(WEB_ROOT / "static/add-kodi.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
