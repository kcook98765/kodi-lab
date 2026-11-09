"""Focused ordinary controls DOM and semantic/mobile contracts (no native runtime)."""

import subprocess
from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient

from tests.web.conftest import login

STOP_GUIDANCE = (
    "Finish playback in Kodi first. Kodi Lab checks playback and active use before "
    "stopping, and may refuse the request. Your settings and add-ons are kept."
)
RESET_GUIDANCE = (
    "If Kodi is running, finish playback first. Reset checks for playback and active "
    "use when you confirm, and may be refused."
)


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
        self.advanced: list[bool] = []
        self.action_locations: dict[str, list[bool]] = {}
        self.field_locations: dict[str, list[bool]] = {}
        self.action_descriptions: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "details":
            self.advanced.append("data-slot-options" in attributes)
        if attributes.get("id"):
            assert attributes["id"] not in self.ids
            self.ids.add(str(attributes["id"]))
        if "data-slot-reason" in attributes:
            self.action_descriptions[str(attributes["id"])] = attributes
        for name in ("aria-describedby", "aria-labelledby"):
            self.descriptions.update(str(attributes.get(name) or "").split())
        if tag == "button" and "data-slot-action" in attributes:
            self.buttons.append(attributes)
            action = str(attributes["data-slot-action"])
            self.action_locations.setdefault(action, []).append(any(self.advanced))
        if tag == "dialog":
            self.dialogs.append(attributes)
        for key in attributes:
            if key.startswith("data-slot-"):
                self.field_locations.setdefault(key, []).append(any(self.advanced))

    def handle_endtag(self, tag: str) -> None:
        if tag == "details":
            self.advanced.pop()


def test_four_slots_have_accessible_confirmation_controls_and_mobile_layout(
    client: TestClient,
) -> None:
    login(client)
    page = client.get("/managed-kodi")
    assert "<h1>Managed Kodi</h1>" in page.text
    heading = page.text.split('<h2 id="managed-kodi-heading">', 1)[1].split("</h2>", 1)[
        0
    ]
    assert "<span>Kodi versions</span>" in heading
    assert "Managed Kodi" not in heading
    parser = Semantics()
    parser.feed(page.text)
    assert parser.descriptions <= parser.ids
    for button in parser.buttons:
        action = button["data-slot-action"]
        description = parser.action_descriptions[str(button["aria-describedby"])]
        assert description["data-slot-reason"] == action
        expected = {"start": "", "stop": STOP_GUIDANCE, "reset": RESET_GUIDANCE}
        assert description["data-slot-available-description"] == expected[str(action)]
    assert len(parser.buttons) == 12 and all("disabled" in b for b in parser.buttons)
    assert len(parser.dialogs) == 1
    assert 'type="checkbox" required data-slot-confirm-check' in page.text
    assert "Reset permanently discards this version\u2019s Kodi settings" in page.text
    assert "I understand that this Kodi profile will be discarded." in page.text
    assert "<noscript>" in page.text
    assert page.text.count("<summary>Advanced options") == 4
    assert "Open Screen" not in page.text and "View Screen" in page.text
    assert parser.action_locations == {
        "start": [False] * 4,
        "stop": [False] * 4,
        "reset": [True] * 4,
    }
    for field in ["preparation", "native", "technical", "advanced", "history"]:
        assert parser.field_locations["data-slot-" + field] == [True] * 4
    for field in ["state", "message", "clean", "recovery", "screen", "install"]:
        assert parser.field_locations["data-slot-" + field] == [False] * 4
    for field in ["request-message", "request-recovery", "resume"]:
        assert parser.field_locations["data-slot-" + field] == [False]
    assert "Stop keeps settings and add-ons" in page.text
    assert "Choosing a different retained version keeps its saved profile" in page.text
    css = client.get("/static/managed-slots.css").text
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in css
    assert "@media(max-width:40rem)" in css
    assert "min-height:44px" in css and ":focus-visible" in css
