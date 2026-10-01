from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from scripts.real_agent_trial import (
    answer_is_complete,
    bounded_model_tool_payload,
    credential_from_file,
    endpoint_identity,
    remote_action_allowed,
    safe_arguments,
    safe_mcp_error,
    safe_tool_result,
)


def test_endpoint_identity_rejects_embedded_secrets() -> None:
    assert endpoint_identity("http://spark.home.arpa:8000/v1/") == (
        "http://spark.home.arpa:8000/v1"
    )
    for endpoint in (
        "http://user:secret@spark.home.arpa:8000/v1",
        "http://spark.home.arpa:8000/v1?token=secret",
        "http://spark.home.arpa:8000/v1#secret",
    ):
        with pytest.raises(ValueError):
            endpoint_identity(endpoint)


def test_credential_file_requires_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "trial.json"
    token = "a" * 43
    path.write_text(json.dumps({"token": token}))
    os.chmod(path, 0o600)
    assert credential_from_file(path) == token
    os.chmod(path, 0o644)
    with pytest.raises(ValueError, match="permissions_invalid"):
        credential_from_file(path)


def test_tool_result_is_bounded_and_drops_unknown_fields() -> None:
    secret = "unrelated-host-secret"
    summary = safe_tool_result(
        "target_list",
        {
            "targets": [
                {
                    "target_id": "mini-id",
                    "display_name": "mini",
                    "host_path": f"/private/{secret}",
                    "password": secret,
                }
            ]
        },
    )
    assert summary["targets"] == [{"target_id": "mini-id", "display_name": "mini"}]
    assert secret not in json.dumps(summary)
    addons = safe_tool_result(
        "addon_list",
        {
            "target_id": "mini-id",
            "total_count": 1,
            "addons": [
                {
                    "addon_id": "service.kodi_lab_bridge",
                    "addon_type": "xbmc.service",
                    "enabled": True,
                    "version": "1.1.13",
                    "raw_path": f"/private/{secret}",
                }
            ],
        },
    )
    assert addons["bridge_on_page"] == {
        "addon_id": "service.kodi_lab_bridge",
        "version": "1.1.13",
        "enabled": True,
    }
    assert secret not in json.dumps(addons)


def test_mcp_error_keeps_safe_codes_only() -> None:
    assert safe_mcp_error(
        {
            "error": {
                "code": "permission_denied",
                "reason": "target_unavailable",
                "exception": "/private/secret",
            }
        }
    ) == {
        "code": "permission_denied",
        "reason": "target_unavailable",
    }
    assert safe_mcp_error({"error": {"code": "/private/secret"}}) == {
        "code": "unknown_mcp_error"
    }


def test_tool_arguments_and_empty_model_answer_fail_closed() -> None:
    assert safe_arguments("target_status", {"target_id": "mini-id"}) == {
        "target_id": "mini-id"
    }
    with pytest.raises(ValueError):
        safe_arguments("target_status", {"target_id": "mini-id", "action": "home"})
    with pytest.raises(ValueError):
        safe_arguments("remote_action", {})
    assert not answer_is_complete(
        {"finish_reason": "length", "message": {"content": "partial"}}
    )
    assert not answer_is_complete({"finish_reason": "stop", "message": {"content": ""}})
    assert answer_is_complete({"finish_reason": "stop", "message": {"content": "done"}})
    assert json.loads(bounded_model_tool_payload({"items": [1, 2]})) == {
        "items": [1, 2]
    }
    assert json.loads(
        bounded_model_tool_payload({"large": "x" * 100}, max_chars=10)
    ) == {"error": {"code": "tool_result_too_large"}}


def test_remote_action_is_canonical_and_limited_to_trial_target() -> None:
    target_id = str(uuid4())
    arguments = {"target_id": target_id, "request_id": str(uuid4()), "action": "home"}
    assert safe_arguments("remote_action", arguments) == arguments
    assert remote_action_allowed(
        arguments,
        trial_target_id=target_id,
        allowed_actions={"home"},
        dispatched_count=0,
        max_dispatches=1,
    )
    assert not remote_action_allowed(
        arguments,
        trial_target_id=target_id,
        allowed_actions={"home"},
        dispatched_count=1,
        max_dispatches=1,
    )
    assert not remote_action_allowed(
        arguments,
        trial_target_id=str(uuid4()),
        allowed_actions={"home"},
        dispatched_count=0,
        max_dispatches=1,
    )
    assert not remote_action_allowed(
        arguments,
        trial_target_id=target_id,
        allowed_actions=set(),
        dispatched_count=0,
        max_dispatches=0,
    )
    with pytest.raises(ValueError):
        safe_arguments("remote_action", {**arguments, "action": "open_settings"})
    with pytest.raises(ValueError):
        safe_arguments("remote_action", {**arguments, "request_id": "not-a-uuid"})


def test_remote_receipt_record_drops_unrelated_fields() -> None:
    result = safe_tool_result(
        "remote_action",
        {
            "request_id": "one",
            "target_id": "mini",
            "action": "home",
            "outcome": "succeeded",
            "host_path": "/private/secret",
        },
    )
    assert result == {
        "request_id": "one",
        "target_id": "mini",
        "action": "home",
        "outcome": "succeeded",
    }
