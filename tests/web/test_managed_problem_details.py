"""Sanitized managed support-copy behavior and rendered accessibility contracts."""

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from tests.web.conftest import login
from tests.web.test_managed_slots_dom import Semantics


def test_managed_problem_details_controller() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("managed_problem_details.cjs")),
            str(root / "src/kodi_lab/web/static/managed-slots.js"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_managed_problem_copy_semantics(client: TestClient) -> None:
    login(client)
    page = client.get("/managed-kodi")
    assert page.status_code == 200
    parser = Semantics()
    parser.feed(page.text)
    assert parser.descriptions <= parser.ids
    assert page.text.count('class="managed-problem-details"') == 6
    assert page.text.count('type="button" data-problem-copy') == 6
    assert page.text.count('readonly spellcheck="false" data-problem-text') == 6
    assert page.text.count('aria-atomic="true" data-problem-status') == 6
    assert page.text.count("data-problem-manual hidden") == 6
    assert page.text.count("/managed-slots.js") == 1
    assert "/managed-problem-details.js" not in page.text
    assert "touch and hold the text" in page.text
