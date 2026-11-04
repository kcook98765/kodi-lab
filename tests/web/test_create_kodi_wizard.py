"""Passive guided page and focused browser controller behavior."""

import json
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.web.conftest import ORIGIN, login
from tests.web.test_create_kodi_api import no_execution as no_execution


@pytest.mark.parametrize("generation", ["matrix", "nexus", "omega"])
def test_guided_page_only_observes_reviewed_stable_versions(
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
    assert set(payload["canonical_targets"]) == {"matrix", "nexus", "omega"}
    assert "data-create-approve disabled" in response.text
    assert "data-kodi-prerequisites data-generation=" in response.text
    assert 'aria-live="polite"' in response.text
    assert "data-create-screen hidden" in response.text
    assert 'value="piers"' not in response.text
    assert "create-kodi.js" in response.text and "create-kodi.css" in response.text
    assert client.post("/create-kodi", headers=ORIGIN).status_code == 405


def test_normal_navigation_and_preview_exclusion(client: TestClient) -> None:
    login(client)
    assert 'href="/create-kodi"' in client.get("/kodi").text
    assert client.get("/create-kodi?generation=piers").status_code == 422
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
