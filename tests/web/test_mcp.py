from __future__ import annotations

import inspect
from typing import Any, cast
from uuid import UUID, uuid4

import jsonschema  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import (
    FULL_LAB_CONTROL_PRESET_ID,
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
)
from kodi_lab.kodi.external import ExternalProbeError
from kodi_lab.mcp.transport import (
    BoundedTokenBucketLimiter,
    ModernMcpAuthMiddleware,
    build_mcp_http_transport,
)
from kodi_lab.storage.models import (
    McpCredentialRow,
)

from .conftest import (
    EXTERNAL_TARGET_ID,
    FakeExternalKodi,
    seed_external_target,
)

PROTOCOL = "2026-07-28"
LEGACY_PROTOCOL = "2025-11-25"
META = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {
        "name": "kodi-lab-test",
        "version": "1",
    },
}


def _administration(client: TestClient) -> McpAdministrationService:
    app = cast(FastAPI, client.app)
    return cast(McpAdministrationService, app.state.kodi_lab.mcp_administration)


def _assert_tool_error(result: dict[str, Any], code: str) -> None:
    assert result["isError"] is True
    assert result["content"][0]["text"] == code
    assert "structuredContent" not in result


def _strict_tool_result(result: dict[str, Any], output_schema: dict[str, Any]) -> None:
    """Apply the advertised schema as a client that validates every supplied value."""
    if "structuredContent" in result:
        jsonschema.validate(result["structuredContent"], output_schema)
    else:
        assert result["isError"] is True


def _authority(client: TestClient) -> tuple[UUID, str]:
    administration = _administration(client)
    principal = administration.create_client(
        display_name="Modern wire client", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    return principal.id, administration.issue_credential(principal.id).token


def _post(
    client: TestClient,
    token: str,
    method: str,
    *,
    params: dict[str, Any] | None = None,
    request_id: int = 1,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    materialized = {} if params is None else dict(params)
    materialized["_meta"] = META
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL,
        "Mcp-Method": method,
    }
    if method == "tools/call" and isinstance(materialized.get("name"), str):
        headers["Mcp-Name"] = cast(str, materialized["name"])
    if extra_headers:
        headers.update(extra_headers)
    return client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": materialized,
        },
        follow_redirects=False,
    )


def _legacy_post(
    client: TestClient,
    token: str,
    method: str,
    *,
    params: dict[str, Any] | None = None,
    request_id: int = 1,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": LEGACY_PROTOCOL,
        "Mcp-Method": method,
    }
    if method == "tools/call" and isinstance((params or {}).get("name"), str):
        headers["Mcp-Name"] = cast(str, (params or {})["name"])
    if extra_headers:
        headers.update(extra_headers)
    return client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {} if params is None else params,
        },
        follow_redirects=False,
    )


def test_external_target_list_is_unprobed_even_when_previously_verified_offline(
    client: TestClient,
    database: sessionmaker[Session],
    external_kodi: FakeExternalKodi,
) -> None:
    seed_external_target(database)
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="External list freshness", preset_id=READ_ONLY_INSPECTION_PRESET_ID
    )
    administration.assign_target_preset(
        EXTERNAL_TARGET_ID, READ_ONLY_INSPECTION_PRESET_ID
    )
    administration.grant_target(mcp_client.id, EXTERNAL_TARGET_ID)
    token = administration.issue_credential(mcp_client.id).token

    def offline() -> None:
        raise ExternalProbeError("target_unreachable")

    external_kodi.on_identity = offline
    listed = _post(
        client,
        token,
        "tools/call",
        params={"name": "target_list", "arguments": {}},
    ).json()["result"]["structuredContent"]["targets"]
    external = next(
        item for item in listed if item["target_id"] == str(EXTERNAL_TARGET_ID)
    )
    assert external_kodi.methods == []
    assert external["kodi_version"] == "21.3"
    assert external["capabilities"] == [
        "target_status",
        "system_info",
        "addon_list",
        "remote_action",
        "inspect_ui_state",
        "log_read",
        "log_scan",
    ]
    assert external["process_state"] == "unknown"
    assert external["jsonrpc_state"] == "not_checked"
    assert external["usability_state"] == "not_checked"
    assert external["readiness_state"] == "not_checked"

    status = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "target_status",
            "arguments": {"target_id": str(EXTERNAL_TARGET_ID)},
        },
    ).json()["result"]
    _assert_tool_error(status, "target_unreachable")
    assert external_kodi.methods == ["JSONRPC.Ping", "Application.GetProperties"]


def test_external_target_modern_legacy_status_inventory_and_replay_parity(
    client: TestClient,
    database: sessionmaker[Session],
    external_kodi: FakeExternalKodi,
) -> None:
    seed_external_target(database)
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="External HTTP parity",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    administration.assign_target_preset(EXTERNAL_TARGET_ID, FULL_LAB_CONTROL_PRESET_ID)
    administration.grant_target(mcp_client.id, EXTERNAL_TARGET_ID)
    token = administration.issue_credential(mcp_client.id).token

    for tool in ("target_status", "addon_list"):
        params = {
            "name": tool,
            "arguments": {"target_id": str(EXTERNAL_TARGET_ID)},
        }
        modern = _post(client, token, "tools/call", params=params).json()["result"]
        legacy = _legacy_post(client, token, "tools/call", params=params).json()[
            "result"
        ]
        assert modern["isError"] is legacy["isError"] is False
        modern_content = dict(modern["structuredContent"])
        legacy_content = dict(legacy["structuredContent"])
        modern_content.pop("observed_at", None)
        legacy_content.pop("observed_at", None)
        assert modern_content == legacy_content

    arguments = {
        "request_id": str(uuid4()),
        "target_id": str(EXTERNAL_TARGET_ID),
        "action": "home",
    }
    modern = _post(
        client,
        token,
        "tools/call",
        params={"name": "remote_action", "arguments": arguments},
    ).json()["result"]
    legacy = _legacy_post(
        client,
        token,
        "tools/call",
        params={"name": "remote_action", "arguments": arguments},
    ).json()["result"]
    assert modern["isError"] is legacy["isError"] is False
    assert modern["structuredContent"] == legacy["structuredContent"]
    assert external_kodi.mutations == 1


def test_external_remote_rechecks_revocation_after_identity_before_dispatch(
    client: TestClient,
    database: sessionmaker[Session],
    external_kodi: FakeExternalKodi,
) -> None:
    seed_external_target(database)
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="External pre-dispatch recheck",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    administration.assign_target_preset(EXTERNAL_TARGET_ID, FULL_LAB_CONTROL_PRESET_ID)
    administration.grant_target(mcp_client.id, EXTERNAL_TARGET_ID)
    token = administration.issue_credential(mcp_client.id).token

    def revoke() -> None:
        administration.remove_target_grant(mcp_client.id, EXTERNAL_TARGET_ID)

    external_kodi.on_identity = revoke
    arguments = {
        "request_id": str(uuid4()),
        "target_id": str(EXTERNAL_TARGET_ID),
        "action": "home",
    }

    first = _post(
        client,
        token,
        "tools/call",
        params={"name": "remote_action", "arguments": arguments},
    ).json()["result"]
    replay = _post(
        client,
        token,
        "tools/call",
        params={"name": "remote_action", "arguments": arguments},
    ).json()["result"]
    assert first["isError"] is replay["isError"] is True
    _assert_tool_error(first, "target_not_found_or_not_authorized")
    _assert_tool_error(replay, "target_not_found_or_not_authorized")
    assert external_kodi.mutations == 0


def test_mcp_raw_protocol_and_authentication_fail_closed(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    client_id, token = _authority(client)
    valid_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {"_meta": META},
    }
    modern = {
        "MCP-Protocol-Version": PROTOCOL,
        "Mcp-Method": "tools/list",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    assert client.get("/mcp", follow_redirects=False).status_code == 405
    missing_version = client.post(
        "/mcp", headers={"Authorization": f"Bearer {token}"}, json=valid_body
    )
    assert missing_version.status_code == 400
    unsupported = client.post(
        "/mcp",
        headers={
            **modern,
            "MCP-Protocol-Version": "2025-06-18",
            "Authorization": f"Bearer {token}",
        },
        json=valid_body,
    )
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["code"] != 0
    assert unsupported.json()["id"] == 1
    assert unsupported.json()["error"]["data"]["supported"] == [
        PROTOCOL,
    ]

    failures = [
        client.post("/mcp", headers=modern, json=valid_body),
        client.post(
            "/mcp", headers={**modern, "Authorization": "Basic no"}, json=valid_body
        ),
        client.post(
            "/mcp",
            headers={**modern, "Authorization": "Bearer invalid"},
            json=valid_body,
        ),
        client.post(
            f"/mcp?access_token={token}",
            headers={**modern, "Authorization": f"Bearer {token}"},
            json=valid_body,
        ),
    ]
    assert {(item.status_code, item.json()["error"]) for item in failures} == {
        (401, "authentication_failed")
    }

    malformed_json = client.post(
        "/mcp",
        headers={**modern, "Authorization": f"Bearer {token}"},
        content=b"{",
    )
    assert malformed_json.status_code == 400
    wrong_content_type = client.post(
        "/mcp",
        headers={
            **modern,
            "Content-Type": "text/plain",
            "Authorization": f"Bearer {token}",
        },
        content=b"{}",
    )
    assert wrong_content_type.status_code == 415
    mismatch = client.post(
        "/mcp",
        headers={
            **modern,
            "Mcp-Method": "tools/call",
            "Authorization": f"Bearer {token}",
        },
        json=valid_body,
    )
    assert mismatch.status_code == 400
    session_header = client.post(
        "/mcp",
        headers={
            **modern,
            "Mcp-Session-Id": "not-a-session",
            "Authorization": f"Bearer {token}",
        },
        json=valid_body,
    )
    assert session_header.status_code == 400
    invalid_method = _post(client, token, "debug/anything")
    assert invalid_method.status_code == 404
    initialize = _post(client, token, "initialize")
    assert initialize.status_code == 404
    subscription = _post(client, token, "subscriptions/listen")
    assert subscription.status_code == 404
    assert subscription.json()["error"]["code"] == -32601
    invalid_params = _post(
        client,
        token,
        "tools/call",
        params={"name": "target_status", "arguments": {}},
    )
    assert invalid_params.status_code == 200
    assert invalid_params.json()["result"]["isError"] is True
    too_large = client.post(
        "/mcp",
        headers={**modern, "Authorization": f"Bearer {token}"},
        content=b" " * (64 * 1024 + 1),
    )
    assert too_large.status_code == 413
    invalid_origin = client.post(
        "/mcp",
        headers={
            **modern,
            "Authorization": f"Bearer {token}",
            "Origin": "https://attacker.example",
        },
        json=valid_body,
    )
    assert invalid_origin.status_code == 403

    administration = _administration(client)
    credential_id = administration.get_client(client_id).credentials[0].id
    administration.revoke_credential(client_id, credential_id)
    revoked = client.post(
        "/mcp",
        headers={**modern, "Authorization": f"Bearer {token}"},
        json=valid_body,
    )
    assert revoked.status_code == 401
    assert revoked.json() == {"error": "authentication_failed"}

    expired = administration.issue_credential(client_id)
    with database.begin() as session:
        row = session.get(McpCredentialRow, str(expired.credential.id))
        assert row is not None
        row.created_at = "2000-01-01T00:00:00.000000Z"
        row.expires_at = "2000-01-01T00:00:00.000001Z"
    expired_response = client.post(
        "/mcp",
        headers={**modern, "Authorization": f"Bearer {expired.token}"},
        json=valid_body,
    )
    assert expired_response.status_code == 401
    assert expired_response.json() == {"error": "authentication_failed"}

    disabled = administration.issue_credential(client_id)
    administration.set_client_enabled(client_id, False)
    disabled_response = client.post(
        "/mcp",
        headers={**modern, "Authorization": f"Bearer {disabled.token}"},
        json=valid_body,
    )
    assert disabled_response.status_code == 401
    assert disabled_response.json() == {"error": "authentication_failed"}


def test_legacy_http_malformed_input_and_downgrade_matrix(
    client: TestClient,
) -> None:
    _client_id, token = _authority(client)
    initialize_body = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "initialize",
        "params": {
            "protocolVersion": LEGACY_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "legacy-matrix", "version": "1"},
        },
    }
    initialized = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json=initialize_body,
    )
    assert initialized.status_code == 200
    assert initialized.headers.get("mcp-session-id") is None
    assert initialized.json()["result"]["protocolVersion"] == LEGACY_PROTOCOL

    listed = _legacy_post(client, token, "tools/list", request_id=11)
    assert listed.status_code == 200
    assert listed.headers.get("mcp-session-id") is None
    assert [item["name"] for item in listed.json()["result"]["tools"]] == [
        "addon_list",
        "inspect_ui_state",
        "log_read",
        "log_scan",
        "screenshot_capture",
        "screenshot_retrieve",
        "system_info",
        "target_list",
        "target_status",
    ]

    legacy_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": LEGACY_PROTOCOL,
    }
    malformed = client.post("/mcp", headers=legacy_headers, content=b"{")
    assert malformed.status_code == 400
    invalid_method = _legacy_post(client, token, "debug/anything")
    assert invalid_method.status_code == 404
    invalid_params = _legacy_post(
        client,
        token,
        "tools/call",
        params={"name": "target_status", "arguments": {}},
    )
    assert invalid_params.status_code == 200
    assert invalid_params.json()["result"]["isError"] is True
    session_header = _legacy_post(
        client,
        token,
        "tools/list",
        extra_headers={"Mcp-Session-Id": "caller-state"},
    )
    assert session_header.status_code == 400
    mismatch = _legacy_post(
        client,
        token,
        "tools/list",
        extra_headers={"Mcp-Method": "tools/call"},
    )
    assert mismatch.status_code == 400

    # An explicit era never falls through to the other era's method set.
    legacy_discover = _legacy_post(client, token, "server/discover")
    assert legacy_discover.status_code == 404
    modern_initialize = _post(
        client,
        token,
        "initialize",
        params=cast(dict[str, Any], initialize_body["params"]),
    )
    assert modern_initialize.status_code == 404
    wrong_headerless_initialize = dict(initialize_body)
    wrong_headerless_initialize["params"] = {
        **cast(dict[str, Any], initialize_body["params"]),
        "protocolVersion": "2025-06-18",
    }
    wrong_version = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json=wrong_headerless_initialize,
    )
    assert wrong_version.status_code == 400
    assert wrong_version.json()["error"]["data"] == {
        "supported": [LEGACY_PROTOCOL],
        "requested": "2025-06-18",
    }

    unauthenticated = _legacy_post(client, "invalid", "tools/list")
    assert unauthenticated.status_code == 401
    oversized = client.post(
        "/mcp",
        headers=legacy_headers,
        content=b" " * (64 * 1024 + 1),
    )
    assert oversized.status_code == 413


def test_modern_and_legacy_share_one_authenticated_rate_bucket(
    client: TestClient,
) -> None:
    _client_id, token = _authority(client)
    app = cast(FastAPI, client.app)
    middleware = app.state.kodi_lab.mcp_transport.app
    assert isinstance(middleware, ModernMcpAuthMiddleware)
    middleware.authenticated = BoundedTokenBucketLimiter(
        rate=1.0,
        burst=2,
        maximum_buckets=4,
        idle_seconds=60,
        clock=lambda: 100.0,
    )
    assert _post(client, token, "tools/list", request_id=1).status_code == 200
    assert _legacy_post(client, token, "tools/list", request_id=2).status_code == 200
    limited = _legacy_post(client, token, "tools/list", request_id=3)
    assert limited.status_code == 429
    assert limited.json() == {"error": "rate_limited"}


def test_mcp_transport_host_allowlist_rejects_dns_rebinding_values(
    client: TestClient,
    database: sessionmaker[Session],
) -> None:
    _client_id, token = _authority(client)
    context = cast(FastAPI, client.app).state.kodi_lab
    call_entry = context.mcp_transport.server.get_request_handler("tools/call")
    list_entry = context.mcp_transport.server.get_request_handler("tools/list")
    assert call_entry is not None and list_entry is not None
    call_closure = inspect.getclosurevars(call_entry.handler).nonlocals
    list_closure = inspect.getclosurevars(list_entry.handler).nonlocals
    transport = build_mcp_http_transport(
        authentication=McpAuthenticationService(database),
        authorization=list_closure["authorization"],
        managed=call_closure["managed"],
        tools=call_closure["tools"],
        additional_allowed_hosts=(
            "10.251.0.10",
            "10.251.0.10:*",
            "lab.example",
            "lab.example:*",
        ),
        additional_allowed_origins=("http://lab.example", "http://lab.example:*"),
    )
    sdk_app = cast(Any, transport.app)._app
    with TestClient(sdk_app, base_url="https://127.0.0.1") as direct:
        for index, host in enumerate(
            (
                "127.0.0.1",
                "127.0.0.1:8180",
                "localhost",
                "localhost:8180",
                "10.251.0.10",
                "10.251.0.10:8180",
                "lab.example:18100",
            ),
            start=1,
        ):
            response = _post(
                direct,
                token,
                "server/discover",
                request_id=10_000 + index,
                extra_headers={"Host": host},
            )
            assert response.status_code == 200, host

        assert (
            _post(
                direct,
                token,
                "server/discover",
                request_id=10_100,
                extra_headers={
                    "Host": "lab.example:18100",
                    "Origin": "http://lab.example:18100",
                },
            ).status_code
            == 200
        )
        assert (
            _post(
                direct,
                token,
                "server/discover",
                request_id=10_101,
                extra_headers={
                    "Host": "lab.example:18100",
                    "Origin": "http://attacker.invalid",
                },
            ).status_code
            == 403
        )

        for index, host in enumerate(
            (
                "192.168.50.10",
                "192.168.50.135",
                "10.251.0.11",
                "8.8.8.8",
                "attacker.invalid",
                "10.251.0.10.evil",
                "[]",
                "*.example",
                "*",
            ),
            start=1,
        ):
            response = _post(
                direct,
                token,
                "server/discover",
                request_id=20_000 + index,
                extra_headers={"Host": host},
            )
            assert response.status_code == 421, host
            assert response.text == "Invalid Host header"


def test_workspace_edit_rate_class_is_two_per_second_with_burst_four(
    client: TestClient,
) -> None:
    context = cast(FastAPI, client.app).state.kodi_lab
    limiter = context.mcp_transport.development_edit_limiter
    now = [100.0]
    cast(Any, limiter)._clock = lambda: now[0]
    assert [limiter.allow("client") for _index in range(4)] == [True] * 4
    assert limiter.allow("client") is False
    now[0] += 0.5
    assert limiter.allow("client") is True
