"""Fresh pages enable the layout used by their unconditional sidebar."""

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


@pytest.mark.parametrize("legacy_flag", [None, False, True])
def test_sidebar_layout_does_not_require_retired_context(
    legacy_flag: bool | None,
) -> None:
    templates = Path(__file__).parents[2] / "src/kodi_lab/web/templates"
    renderer = Environment(
        loader=FileSystemLoader(templates), autoescape=select_autoescape()
    )
    context: dict[str, object] = {
        "csrf_token": "fixture",
        "url_for": lambda _name, *, path: "/static" + path,
    }
    if legacy_flag is not None:
        context["shared_navigation_enabled"] = legacy_flag
    html = renderer.get_template("base.html").render(**context)
    assert 'class="admin-layout ' in html
    assert 'class="admin-sidebar"' in html
    assert html.count("/static/navigation.js") == 1
