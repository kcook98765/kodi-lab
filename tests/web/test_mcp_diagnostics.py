"""Bounded MCP tool errors retain outer codes without leaking service details."""

from __future__ import annotations

import pytest
from mcp_types import TextContent

from kodi_lab.mcp.transport import _tool_error_result


@pytest.mark.parametrize(
    ("outer", "reason"),
    [
        ("execution_unsupported", "dependency_missing"),
        ("execution_unsupported", "dependency_disabled"),
        ("execution_unsupported", "dependency_incompatible"),
        ("execution_unsupported", "dependency_version_unverified"),
        ("execution_unsupported", "dependency_declaration_mismatch"),
        ("development_source_scan_unsafe", "source_identity_changed"),
        ("workspace_capture_rejected", "workspace_generation_conflict"),
    ],
)
def test_allowlisted_reason_keeps_outer_error(outer: str, reason: str) -> None:
    result = _tool_error_result(outer, reason=reason)

    assert result.is_error
    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text == f"{outer}: {reason}"
    assert result.structured_content is None


@pytest.mark.parametrize(
    ("outer", "reason"),
    [
        ("execution_unsupported", None),
        ("execution_unsupported", "/private/host/path"),
        ("execution_unsupported", "source_identity_changed"),
        ("permission_denied", "dependency_missing"),
    ],
)
def test_unknown_or_unauthorized_reason_stays_generic(
    outer: str, reason: str | None
) -> None:
    result = _tool_error_result(outer, reason=reason)

    assert result.is_error
    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text == outer
    assert result.structured_content is None
