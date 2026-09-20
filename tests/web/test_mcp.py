from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
)
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    McpCredentialRow,
    McpSecurityEventRow,
    TargetRow,
)

from .conftest import INSTANCE_ID, ORIGIN, REVISION_ID, login

PROTOCOL = "2026-07-28"
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


def _authority(client: TestClient) -> tuple[UUID, str]:
    administration = _administration(client)
    principal = administration.create_client(
        display_name="Modern wire client",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
    )
    administration.assign_target_preset(INSTANCE_ID, READ_ONLY_INSPECTION_PRESET_ID)
    administration.grant_target(principal.id, INSTANCE_ID)
    issued = administration.issue_credential(principal.id)
    return principal.id, issued.token


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


def test_modern_wire_is_stateless_and_exposes_only_read_only_tools(
    client: TestClient,
) -> None:
    _client_id, token = _authority(client)

    discover = _post(client, token, "server/discover", request_id=10)
    assert discover.status_code == 200
    assert discover.headers.get("mcp-session-id") is None
    assert discover.json()["result"]["supportedVersions"] == [PROTOCOL]
    assert set(discover.json()["result"]["capabilities"]) == {"tools"}

    listed = _post(client, token, "tools/list", request_id=11)
    assert listed.status_code == 200
    assert listed.headers.get("mcp-session-id") is None
    listed_result = listed.json()["result"]
    assert listed_result["ttlMs"] == 0
    assert listed_result["cacheScope"] == "private"
    tools = listed_result["tools"]
    assert [item["name"] for item in tools] == ["target_list", "target_status"]
    assert all(item["_meta"]["kodiLabMcpSchemaVersion"] == 1 for item in tools)

    target_list = _post(
        client,
        token,
        "tools/call",
        params={"name": "target_list", "arguments": {}},
        request_id=12,
    )
    assert target_list.status_code == 200
    listed_content = target_list.json()["result"]["structuredContent"]
    assert listed_content["schema_version"] == 1
    assert [item["target_id"] for item in listed_content["targets"]] == [
        str(INSTANCE_ID)
    ]

    status = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "target_status",
            "arguments": {"target_id": str(INSTANCE_ID)},
        },
        request_id=13,
    )
    assert status.status_code == 200
    status_content = status.json()["result"]["structuredContent"]
    assert status_content["target"]["target_id"] == str(INSTANCE_ID)
    assert "container_id" not in str(status_content)
    assert "jsonrpc_port" not in str(status_content)


def test_pinned_official_sdk_uses_only_modern_stateless_requests(
    client: TestClient,
) -> None:
    _client_id, token = _authority(client)
    methods: list[str] = []

    async def capture(request: httpx2.Request) -> None:
        body = json.loads(request.content)
        methods.append(cast(str, body["method"]))

    async def exercise() -> None:
        transport = httpx2.ASGITransport(app=client.app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {token}"},
            event_hooks={"request": [capture]},
        ) as http:
            discovery_streams = streamable_http_client(
                "http://127.0.0.1/mcp",
                http_client=http,
                terminate_on_close=False,
            )
            async with Client(discovery_streams, mode="auto", cache=None) as probe:
                assert probe.session.protocol_version == PROTOCOL
                await probe.list_tools()
            streams = streamable_http_client(
                "http://127.0.0.1/mcp",
                http_client=http,
                terminate_on_close=False,
            )
            async with Client(streams, mode=PROTOCOL, cache=None) as official:
                assert official.session.protocol_version == PROTOCOL
                listed = await official.list_tools()
                assert [tool.name for tool in listed.tools] == [
                    "target_list",
                    "target_status",
                ]
                target_list = await official.call_tool("target_list", {})
                assert target_list.is_error is False
                status = await official.call_tool(
                    "target_status", {"target_id": str(INSTANCE_ID)}
                )
                assert status.is_error is False

    asyncio.run(exercise())
    assert methods == [
        "server/discover",
        "tools/list",
        "tools/list",
        "tools/call",
        "tools/call",
    ]
    assert "initialize" not in methods
    assert "notifications/initialized" not in methods


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
            "MCP-Protocol-Version": "2025-11-25",
            "Authorization": f"Bearer {token}",
        },
        json=valid_body,
    )
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["code"] != 0
    assert unsupported.json()["id"] == 1

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


def test_web_mcp_administration_is_separate_show_once_and_csrf_protected(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    assert client.get("/mcp-clients", follow_redirects=False).status_code == 401
    assert client.get("/api/mcp/clients").status_code == 401
    csrf = login(client)
    presets = client.get("/api/mcp/presets").json()
    read_only = next(
        item for item in presets if item["display_name"] == "Read-only Inspection"
    )

    no_csrf = client.post(
        "/api/mcp/clients",
        headers=ORIGIN,
        json={"display_name": "Web client", "preset_id": read_only["id"]},
    )
    assert no_csrf.status_code == 403
    mutation_headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    bad_origin = client.post(
        "/api/mcp/clients",
        headers={"Origin": "https://attacker.example", "X-Kodi-Lab-CSRF": csrf},
        json={"display_name": "Web client", "preset_id": read_only["id"]},
    )
    assert bad_origin.status_code == 403
    assert (
        client.get("/api/mcp/clients", headers={"Host": "attacker.example"}).status_code
        == 400
    )
    created = client.post(
        "/api/mcp/clients",
        headers=mutation_headers,
        json={"display_name": "Web client", "preset_id": read_only["id"]},
    )
    assert created.status_code == 201
    client_id = created.json()["id"]
    issue = client.post(
        f"/api/mcp/clients/{client_id}/credentials",
        headers=mutation_headers,
        json={"expiration_seconds": 3600},
    )
    assert issue.status_code == 201
    issued_payload = issue.json()
    token = issued_payload.pop("token")
    credential_id = issued_payload["credential"]["id"]
    assert len(base64.urlsafe_b64decode(token + "=")) == 32

    renamed = client.post(
        f"/api/mcp/clients/{client_id}/rename",
        headers=mutation_headers,
        json={"display_name": "Renamed Web client"},
    )
    assert renamed.json()["display_name"] == "Renamed Web client"
    assigned = client.post(
        f"/api/mcp/clients/{client_id}/preset",
        headers=mutation_headers,
        json={"preset_id": read_only["id"]},
    )
    assert assigned.json()["preset"]["id"] == read_only["id"]
    policy = client.post(
        f"/api/mcp/target-policies/{INSTANCE_ID}",
        headers=mutation_headers,
        json={"preset_id": read_only["id"]},
    )
    assert policy.json()["preset"]["id"] == read_only["id"]
    granted = client.post(
        f"/api/mcp/clients/{client_id}/targets/{INSTANCE_ID}",
        headers=mutation_headers,
    )
    assert granted.json()["target_grants"] == [str(INSTANCE_ID)]
    removed = client.delete(
        f"/api/mcp/clients/{client_id}/targets/{INSTANCE_ID}",
        headers=mutation_headers,
    )
    assert removed.json()["target_grants"] == []
    rotated = client.post(
        f"/api/mcp/clients/{client_id}/credentials/{credential_id}/rotate",
        headers=mutation_headers,
        json={"expiration_seconds": 3600},
    )
    assert rotated.status_code == 200
    rotated_token = rotated.json()["token"]
    assert rotated_token != token
    rotated_id = rotated.json()["credential"]["id"]
    revoked = client.post(
        f"/api/mcp/clients/{client_id}/credentials/{rotated_id}/revoke",
        headers=mutation_headers,
    )
    assert revoked.json()["status"] == "revoked"
    disabled = client.post(
        f"/api/mcp/clients/{client_id}/enabled",
        headers=mutation_headers,
        json={"enabled": False},
    )
    assert disabled.json()["enabled"] is False

    detail_text = client.get(f"/api/mcp/clients/{client_id}").text
    page_text = client.get("/mcp-clients").text
    assert token not in detail_text
    assert token not in page_text
    assert rotated_token not in detail_text
    assert rotated_token not in page_text
    assert "token_digest" not in detail_text
    assert "token_digest" not in page_text
    assert "Non-expiring" in page_text
    with database() as session:
        persisted = session.scalar(
            select(McpCredentialRow).where(McpCredentialRow.client_id == client_id)
        )
        assert persisted is not None
        assert persisted.token_digest not in detail_text
        event_kinds = set(session.scalars(select(McpSecurityEventRow.event_kind)))
    assert {
        "client_created",
        "client_renamed",
        "client_preset_assigned",
        "credential_issued",
        "credential_rotated",
        "credential_revoked",
        "client_disabled",
        "target_preset_assigned",
        "target_granted",
        "target_grant_removed",
    }.issubset(event_kinds)

    # A valid Web cookie is not an MCP bearer credential.
    cookie_only = _post(client, "not-a-token", "tools/list")
    assert cookie_only.status_code == 401
    client.cookies.clear()
    assert (
        client.get(
            "/api/mcp/clients", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 401
    )


def test_target_authority_recomputes_grants_and_preserves_uuid_privacy(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    administration = _administration(client)
    mcp_client = administration.create_client(
        display_name="Reevaluation client",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
    )
    administration.assign_target_preset(INSTANCE_ID, READ_ONLY_INSPECTION_PRESET_ID)
    token = administration.issue_credential(mcp_client.id).token

    before = _post(
        client,
        token,
        "tools/call",
        params={"name": "target_list", "arguments": {}},
    ).json()["result"]["structuredContent"]
    assert before["targets"] == []

    earlier_id = UUID("00000000-0000-4000-8000-000000000100")
    with database.begin() as session:
        session.add(
            TargetRow(
                id=str(earlier_id),
                name="Earlier lexical target",
                target_type="managed",
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.flush()
        session.add(
            ManagedInstanceRow(
                target_id=str(earlier_id),
                instance_type="disposable",
                runtime_revision_id=str(REVISION_ID),
                runtime_state="stopped",
                start_on_host_boot=False,
                notes=None,
            )
        )
    administration.assign_target_preset(earlier_id, READ_ONLY_INSPECTION_PRESET_ID)
    administration.grant_target(mcp_client.id, earlier_id)
    administration.grant_target(mcp_client.id, INSTANCE_ID)
    after = _post(
        client,
        token,
        "tools/call",
        params={"name": "target_list", "arguments": {"limit": 1}},
    ).json()["result"]["structuredContent"]
    assert len(after["targets"]) == 1
    assert after["targets"][0]["target_id"] == str(earlier_id)
    assert after["next_cursor"] == str(earlier_id)
    second_page = _post(
        client,
        token,
        "tools/call",
        params={
            "name": "target_list",
            "arguments": {"limit": 1, "cursor": after["next_cursor"]},
        },
    ).json()["result"]["structuredContent"]
    assert [item["target_id"] for item in second_page["targets"]] == [str(INSTANCE_ID)]
    assert second_page["next_cursor"] is None

    administration.remove_target_grant(mcp_client.id, INSTANCE_ID)
    errors = []
    for target_id in (INSTANCE_ID, uuid4()):
        response = _post(
            client,
            token,
            "tools/call",
            params={
                "name": "target_status",
                "arguments": {"target_id": str(target_id)},
            },
        )
        assert response.status_code == 200
        errors.append(response.json()["result"])
    assert [item["isError"] for item in errors] == [True, True]
    assert all(
        "target_not_found_or_not_authorized" in item["content"][0]["text"]
        for item in errors
    )
