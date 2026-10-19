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
        ("execution_unsupported", "script_launch_not_supported"),
        ("execution_unsupported", "multiple_executable_extensions"),
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


@pytest.mark.parametrize(
    ("reason", "detail"),
    [
        ("client_retained_workspace_limit", "per client: 4"),
        ("global_retained_workspace_limit", "MCP bindings: 32"),
        ("client_workspace_creation_in_progress", "creation limit per client: 1"),
        ("workspace_creation_busy", "global-heavy operation"),
    ],
)
def test_workspace_quota_has_bounded_reason_and_supported_recovery(
    reason: str, detail: str
) -> None:
    result = _tool_error_result(
        "workspace_quota_exceeded", tool_name="workspace_create", reason=reason
    )
    assert result.is_error and result.structured_content is None
    assert isinstance(result.content[0], TextContent)
    text = result.content[0].text
    assert text.startswith(f"workspace_quota_exceeded: {reason}\n")
    assert detail in text and "next_action:" in text
    if reason.endswith("workspace_limit"):
        assert "Stopping Kodi does not release" in text
        assert "Do not delete or abandon unrelated projects" in text
    else:
        assert "retry the same request_id" in text


def test_workspace_quota_never_echoes_untrusted_service_detail() -> None:
    result = _tool_error_result(
        "workspace_quota_exceeded",
        tool_name="workspace_create",
        reason="/private/path?token=SECRET",
    )
    assert isinstance(result.content[0], TextContent)
    assert "SECRET" not in result.content[0].text
    assert "next_action:" in result.content[0].text
