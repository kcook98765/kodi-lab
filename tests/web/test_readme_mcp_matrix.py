"""Keep the public tool matrix complete against both offline wire catalogs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kodi_lab.application.mcp_administration import READ_ONLY_INSPECTION_PRESET_ID
from kodi_lab.mcp.registry import build_mcp_tool_registry

from .test_mcp import _administration, _legacy_post, _post

README = Path(__file__).resolve().parents[2] / "README.md"


def _matrix_names() -> list[str]:
    readme = README.read_text()
    start = "<!-- mcp-tool-matrix:start -->"
    end = "<!-- mcp-tool-matrix:end -->"
    assert readme.count(start) == readme.count(end) == 1
    matrix = readme.split(start, 1)[1].split(end, 1)[0]
    rows = re.findall(r"^\| `([a-z_]+)` \| (.+) \|$", matrix, re.MULTILINE)
    assert rows, "README must contain MCP matrix rows"
    for name, cells in rows:
        columns = cells.split(" | ")
        assert len(columns) == 5, f"{name}: expected purpose, three classes, authority"
        assert all(columns), f"{name}: empty matrix cell"
        assert set(columns[1:4]) <= {"Yes", "Conditional", "Off", "No", "N/A"}
    return [name for name, _ in rows]


def test_readme_matrix_covers_each_registered_tool_exactly_once() -> None:
    names = _matrix_names()
    assert len(names) == len(set(names)), "duplicate MCP tool row"
    assert set(names) == set(build_mcp_tool_registry().names())


@pytest.mark.parametrize("legacy", [False, True], ids=["modern", "legacy"])
def test_readme_matrix_matches_complete_authenticated_wire_catalog(
    client: TestClient, legacy: bool
) -> None:
    administration = _administration(client)
    owner = administration.create_client(
        display_name="Offline documentation inventory",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
        trusted_managed_lab=True,
    )
    token = administration.issue_credential(owner.id).token
    post = _legacy_post if legacy else _post
    response = post(client, token, "tools/list")
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names == list(build_mcp_tool_registry().names())
    assert set(names) == set(_matrix_names())
    assert all(tool["inputSchema"] and tool["outputSchema"] for tool in tools)

    # Registration and discovery are distinct from exact target authorization.
    administration.set_managed_lab_role(owner.id, False)
    limited = post(client, token, "tools/list")
    assert limited.status_code == 200
    limited_names = {tool["name"] for tool in limited.json()["result"]["tools"]}
    assert limited_names < set(names)
    assert "target_status" in limited_names
    assert "project_create" not in limited_names
    assert "managed_reset" not in limited_names
