from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import zlib
from pathlib import Path
from uuid import uuid4

import pytest
from mcp_types import ImageContent

from scripts.real_agent_trial import (
    answer_is_complete,
    bounded_model_tool_payload,
    created_workspace_id,
    credential_from_file,
    development_action_allowed,
    endpoint_identity,
    records_mutation_dispatch,
    remote_action_allowed,
    safe_arguments,
    safe_mcp_error,
    safe_tool_result,
    visual_tool_payload,
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
    assert safe_mcp_error(
        {"error": "permission_denied", "reason": "target_unavailable"}
    ) == {
        "code": "permission_denied",
        "reason": "target_unavailable",
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


def test_screenshot_argument_scope_and_visual_delivery(tmp_path: Path) -> None:
    target_id = str(uuid4())
    assert safe_arguments("screenshot_capture", {"target_id": target_id}) == {
        "target_id": target_id
    }
    with pytest.raises(ValueError):
        safe_arguments("screenshot_capture", {"target_id": target_id, "path": "/tmp/x"})
    raw_scanline = b"\0\xff\0\0"

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + tag
            + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw_scanline))
        + chunk(b"IEND", b"")
    )
    encoded = base64.b64encode(png).decode()
    meta = {
        "target_id": target_id,
        "capture_id": str(uuid4()),
        "width": 1,
        "height": 1,
        "mime_type": "image/png",
        "byte_size": len(png),
        "host_path": "/private/secret",
    }
    path = tmp_path / "capture.png"
    content, evidence = visual_tool_payload(
        [ImageContent(data=encoded, mime_type="image/png")], meta, path
    )
    assert path.read_bytes() == png
    assert evidence["sha256"] == hashlib.sha256(png).hexdigest()
    assert content[1]["image_url"]["url"] == "data:image/png;base64," + encoded
    assert "/private/secret" not in content[0]["text"]
    assert "/private/secret" not in json.dumps(
        safe_tool_result("screenshot_capture", meta)
    )


def test_visual_delivery_rejects_missing_or_invalid_image(tmp_path: Path) -> None:
    meta = {"width": 1, "height": 1, "mime_type": "image/png", "byte_size": 100}
    with pytest.raises(ValueError, match="screenshot_invalid"):
        visual_tool_payload([], meta, tmp_path / "missing.png")
    with pytest.raises(ValueError, match="screenshot_invalid"):
        visual_tool_payload(
            [
                ImageContent(
                    data=base64.b64encode(b"not png").decode(), mime_type="image/png"
                )
            ],
            meta,
            tmp_path / "bad.png",
        )
    assert not (tmp_path / "bad.png").exists()


def test_development_calls_bind_one_source_target_and_created_workspace() -> None:
    source_id, target_id, workspace_id = (str(uuid4()) for _ in range(3))
    request_id = str(uuid4())
    create = safe_arguments(
        "workspace_create", {"source_id": source_id, "request_id": request_id}
    )
    assert development_action_allowed(
        "workspace_create",
        create,
        source_id=source_id,
        target_id=target_id,
        workspace_id=None,
        dispatches={},
        mutation_limit=1,
    )
    assert (
        created_workspace_id(
            {
                "workspace_id": workspace_id,
                "source_id": source_id,
                "host_path": "/private",
            },
            source_id,
        )
        == workspace_id
    )
    assert (
        created_workspace_id(
            {"workspace_id": workspace_id, "source_id": str(uuid4())}, source_id
        )
        is None
    )
    deploy = safe_arguments(
        "workspace_deploy",
        {
            "request_id": request_id,
            "workspace_id": workspace_id,
            "expected_generation": 0,
            "target_id": target_id,
            "deployment_mode": "direct",
        },
    )
    assert development_action_allowed(
        "workspace_deploy",
        deploy,
        source_id=source_id,
        target_id=target_id,
        workspace_id=workspace_id,
        dispatches={},
        mutation_limit=1,
    )
    assert not development_action_allowed(
        "workspace_deploy",
        {**deploy, "target_id": str(uuid4())},
        source_id=source_id,
        target_id=target_id,
        workspace_id=workspace_id,
        dispatches={},
        mutation_limit=1,
    )
    assert not development_action_allowed(
        "workspace_deploy",
        {**deploy, "deployment_mode": "repository"},
        source_id=source_id,
        target_id=target_id,
        workspace_id=workspace_id,
        dispatches={},
        mutation_limit=1,
    )
    assert not development_action_allowed(
        "workspace_deploy",
        deploy,
        source_id=source_id,
        target_id=target_id,
        workspace_id=workspace_id,
        dispatches={"workspace_deploy": 1},
        mutation_limit=1,
    )
    assert not development_action_allowed(
        "workspace_create",
        create,
        source_id=source_id,
        target_id=target_id,
        workspace_id=workspace_id,
        dispatches={},
        mutation_limit=1,
    )
    assert not development_action_allowed(
        "workspace_source_scan",
        {"workspace_id": str(uuid4())},
        source_id=source_id,
        target_id=target_id,
        workspace_id=workspace_id,
        dispatches={},
        mutation_limit=1,
    )


def test_development_arguments_and_evidence_reject_unrelated_data() -> None:
    workspace_id = str(uuid4())
    assert (
        safe_arguments(
            "workspace_read",
            {"workspace_id": workspace_id, "relative_path": "addon.xml"},
        )["relative_path"]
        == "addon.xml"
    )
    with pytest.raises(ValueError):
        safe_arguments(
            "workspace_read",
            {"workspace_id": workspace_id, "relative_path": "../secret"},
        )
    with pytest.raises(ValueError):
        safe_arguments(
            "workspace_execute",
            {
                "workspace_id": workspace_id,
                "target_id": str(uuid4()),
                "expected_generation": 0,
                "request_id": str(uuid4()),
                "extra": "unsafe",
            },
        )
    secret = "/private/host-secret"
    result = safe_tool_result(
        "workspace_deploy",
        {
            "state": "succeeded",
            "deployment_mode": "direct",
            "target_id": str(uuid4()),
            "host_path": secret,
        },
    )
    assert result["state"] == "succeeded"
    assert secret not in json.dumps(result)
    assert "text" not in safe_tool_result(
        "workspace_read", {"workspace_id": workspace_id, "text": secret}
    )


def test_development_dispatch_evidence_marks_server_mutations() -> None:
    for name in (
        "workspace_create",
        "workspace_validate",
        "workspace_deploy",
        "workspace_execute",
        "remote_action",
    ):
        assert records_mutation_dispatch(name)
    for name in ("workspace_status", "workspace_tree", "target_status"):
        assert not records_mutation_dispatch(name)
