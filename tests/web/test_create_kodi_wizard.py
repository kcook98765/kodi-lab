"""Passive guided page and focused browser controller behavior."""

import json
import re
import subprocess
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tests.web.conftest import ORIGIN, login
from tests.web.test_create_kodi_api import no_execution as no_execution


def test_favicon_is_a_small_public_icon_with_normal_host_controls(
    client: TestClient,
) -> None:
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200
    assert icon.headers["content-type"] == "image/vnd.microsoft.icon"
    assert icon.headers["x-content-type-options"] == "nosniff"
    assert len(icon.content) < 1024
    with Image.open(BytesIO(icon.content)) as decoded:
        assert decoded.format == "ICO" and decoded.size == (16, 16)
    assert 'rel="icon"' in client.get("/login").text
    assert (
        client.get("/favicon.ico", headers={"Host": "evil.invalid"}).status_code == 400
    )


@pytest.mark.parametrize("generation", ["matrix", "nexus", "omega", "piers"])
def test_guided_page_observes_all_four_reviewed_versions(
    client: TestClient, generation: str
) -> None:
    assert client.get("/create-kodi", follow_redirects=False).status_code == 303
    login(client)
    response = client.get("/create-kodi", params={"generation": generation})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    config = re.search(r'id="create-kodi-config">(.*?)</script>', response.text, re.S)
    assert config
    payload = json.loads(config[1])
    assert payload["generation"] == generation
    assert payload["recipe_digest"].startswith("sha256:")
    assert set(payload["canonical_targets"]) == {"matrix", "nexus", "omega", "piers"}
    assert "data-create-approve disabled" in response.text
    assert "data-kodi-prerequisites data-generation=" in response.text
    assert 'aria-live="polite"' in response.text
    assert "data-create-screen hidden" in response.text
    assert 'value="piers"' in response.text
    assert "data-create-start-after-setup>" in response.text
    assert "data-create-preview-approved" in response.text
    assert "create-kodi.js" in response.text and "create-kodi.css" in response.text
    assert client.post("/create-kodi", headers=ORIGIN).status_code == 405


def test_normal_navigation_and_preview_choice(client: TestClient) -> None:
    login(client)
    assert 'href="/create-kodi?generation=piers"' in client.get("/kodi").text
    assert client.get("/create-kodi?generation=piers").status_code == 200
    assert (
        client.get("/create-kodi", headers={"Host": "evil.invalid"}).status_code == 400
    )


def test_browser_controller_preserves_identity_and_stops_after_unknown_outcomes() -> (
    None
):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("create_kodi_controller.cjs")),
            str(root / "src/kodi_lab/web/static/create-kodi.js"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
