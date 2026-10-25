"""Bridge status UI keeps preparation, trust and capability evidence distinct."""

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from tests.web.conftest import INSTANCE_ID, login


def test_reviewed_bridge_exposes_explicit_status_refresh(client: TestClient) -> None:
    login(client)
    page = client.get(f"/instances/{INSTANCE_ID}").text
    assert "data-reviewed-bridge-install disabled" in page
    assert "data-reviewed-bridge-refresh>Refresh status" in page
    assert "data-reviewed-bridge-status" in page


def test_reviewed_bridge_browser_bounds_and_uncertain_request_safety() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("reviewed_bridge_browser.js")),
            str(root / "src/kodi_lab/web/static/reviewed-bridge.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
