"""Focused ordinary controls DOM and semantic/mobile contracts (no native runtime)."""

import subprocess
from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient

from tests.web.conftest import login


def test_managed_slots_dom_controller() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("managed_slots_controller.cjs")),
            str(root / "src/kodi_lab/web/static/managed-slots.js"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


class Semantics(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.descriptions: set[str] = set()
        self.buttons: list[dict[str, str | None]] = []
        self.dialogs: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id"):
            assert attributes["id"] not in self.ids
            self.ids.add(str(attributes["id"]))
        for name in ("aria-describedby", "aria-labelledby"):
            self.descriptions.update(str(attributes.get(name) or "").split())
        if tag == "button" and "data-slot-action" in attributes:
            self.buttons.append(attributes)
        if tag == "dialog":
            self.dialogs.append(attributes)


def test_four_slots_have_accessible_confirmation_controls_and_mobile_layout(
    client: TestClient,
) -> None:
    login(client)
    page = client.get("/kodi")
    parser = Semantics()
    parser.feed(page.text)
    assert parser.descriptions <= parser.ids
    assert len(parser.buttons) == 12 and all("disabled" in b for b in parser.buttons)
    assert len(parser.dialogs) == 1
    assert 'type="checkbox" required data-slot-confirm-check' in page.text
    assert "<noscript>" in page.text
    assert "<summary>Advanced</summary>" in page.text
    css = client.get("/static/managed-slots.css").text
    assert "minmax(min(100%,20rem),1fr)" in css
    assert "@media(max-width:40rem)" in css
    assert "min-height:44px" in css and ":focus-visible" in css
