"""Viewer feedback and explicit recovery without live Kodi operations."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[2]


def test_viewer_feedback_and_explicit_recovery_in_node() -> None:
    result = subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("novnc_feedback_browser.cjs")),
            str(ROOT / "src/kodi_lab/web/static/novnc-phase1.js"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FEEDBACK PASS" in result.stdout


@pytest.mark.skipif(not os.getenv("KODI_LAB_BROWSER_TOOLS"), reason="Chromium opt-in")
def test_rendered_viewer_feedback_accessibility_and_recovery_in_chromium(
    tmp_path: Path,
) -> None:
    templates = Environment(
        loader=FileSystemLoader(ROOT / "src/kodi_lab/web/templates"), autoescape=True
    )
    templates.globals["viewer_clock_ms"] = lambda: 1_794_355_200_000
    html = tmp_path / "viewer.html"
    html.write_text(
        templates.get_template("novnc.html").render(
            instance="fixture", csrf_token="fixture", viewer_binding="a" * 64
        )
    )
    result = subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("novnc_feedback_chromium.cjs")),
            str(ROOT),
            str(html),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CHROMIUM FEEDBACK PASS" in result.stdout
