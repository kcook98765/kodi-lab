from __future__ import annotations

import base64
import inspect
import json
import logging
from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.production_inspection import PRODUCTION_INSPECTION_PRESET_ID
from kodi_lab.kodi.external import NativeExternalKodiVerifier
from kodi_lab.mcp import transport as transport_module
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.mcp.transport import (
    MCP_PROTOCOL_VERSION,
    mcp_audit_request_correlation,
)
from kodi_lab.production_profile_lifecycle import (
    ProductionLifecycleTransition,
    ProductionProfileLifecycle,
)
from kodi_lab.production_profile_mcp_inspection import (
    ProductionProfileMcpInspection,
    ProductionProfileMcpInspectionError,
    inspect_mcp_screenshot_image,
)
from kodi_lab.production_profile_web_client import ProductionProfileWebClient
from tests.kodi.fake_external_bridge import fake_png

from .conftest import PASSWORD, FakeExternalKodi

BASE_URL = "http://127.0.0.1"


class _RecordHandler(logging.Handler):
    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__()
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


def test_mcp_screenshot_image_diagnostics_are_strict_and_bounded() -> None:
    payload = fake_png(width=2, height=2)
    result: dict[str, object] = {
        "content": [
            {
                "type": "image",
                "mimeType": "image/png",
                "data": base64.b64encode(payload).decode("ascii"),
            }
        ]
    }
    metadata = inspect_mcp_screenshot_image(result)
    assert metadata["decoded_byte_length"] == len(payload)
    assert cast(int, metadata["encoded_character_length"]) > len(payload)
    assert metadata["format"] == "png"
    assert metadata["width"] == 2
    assert metadata["height"] == 2
    assert metadata["color_mode"] == "rgb"
    assert "data" not in metadata

    failures: tuple[tuple[dict[str, object], str], ...] = (
        ({"content": []}, "screenshot_payload_missing"),
        (
            {"content": [{"type": "text", "mimeType": "image/png", "data": ""}]},
            "screenshot_payload_type_invalid",
        ),
        (
            {"content": [{"type": "image", "mimeType": "image/png", "data": "%%%"}]},
            "screenshot_base64_invalid",
        ),
        (
            {"content": [{"type": "image", "mimeType": "image/png", "data": ""}]},
            "screenshot_decoded_empty",
        ),
        (
            {
                "content": [
                    {
                        "type": "image",
                        "mimeType": "image/png",
                        "data": base64.b64encode(b"not-png").decode("ascii"),
                    }
                ]
            },
            "screenshot_decoder_rejected",
        ),
    )
    for document, code in failures:
        with pytest.raises(ProductionProfileMcpInspectionError, match=code):
            inspect_mcp_screenshot_image(document)


def _audit_payloads(records: list[logging.LogRecord]) -> list[dict[str, object]]:
    return [
        cast(dict[str, object], json.loads(message.partition("mcp_request ")[2]))
        for record in records
        if "mcp_request " in (message := record.getMessage())
    ]


def _tool_service(client: TestClient) -> Any:
    server = cast(FastAPI, client.app).state.kodi_lab.mcp_transport.server
    entry = server.get_request_handler("tools/call")
    assert entry is not None
    return inspect.getclosurevars(entry.handler).nonlocals["tools"]


def _call_tool(
    client: TestClient,
    token: str,
    *,
    request_id: str,
    tool_name: str,
    arguments: dict[str, object] | None = None,
) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Mcp-Method": "tools/call",
    }
    if all(32 <= ord(character) < 127 for character in tool_name):
        headers["Mcp-Name"] = tool_name
    return cast(
        httpx.Response,
        client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments or {},
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": (
                            MCP_PROTOCOL_VERSION
                        ),
                        "io.modelcontextprotocol/clientCapabilities": {},
                        "io.modelcontextprotocol/clientInfo": {
                            "name": "audit-boundary-test",
                            "version": "1",
                        },
                    },
                },
            },
        ),
    )


@pytest.fixture
def mcp_audit_records() -> Iterator[list[logging.LogRecord]]:
    records: list[logging.LogRecord] = []
    handler = _RecordHandler(records)
    logger = logging.getLogger("kodi_lab.mcp")
    prior_level = logger.level
    prior_disabled = logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.disabled = False
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)
        logger.disabled = prior_disabled


class _FailSecondToolCall:
    def __init__(self, client: TestClient) -> None:
        self.cookies = client.cookies
        self._client = client
        self.tool_names: list[str] = []

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        document = kwargs.get("json")
        if isinstance(document, dict) and document.get("method") == "tools/call":
            params = document.get("params")
            if isinstance(params, dict) and isinstance(params.get("name"), str):
                self.tool_names.append(cast(str, params["name"]))
                if params["name"] == "target_status":
                    return httpx.Response(503)
        return cast(httpx.Response, self._client.post(url, **kwargs))

    def close(self) -> None:
        pass


def _ready_target(
    client: TestClient, external_kodi: FakeExternalKodi
) -> tuple[ProductionProfileWebClient, ProductionLifecycleTransition]:
    external_kodi.kodi_major = 19
    external_kodi.kodi_minor = 5
    app = cast(FastAPI, client.app)
    service = cast(
        ExternalTargetAdministrationService, app.state.kodi_lab.production_targets
    )
    service._verifier = NativeExternalKodiVerifier(
        transport=httpx.MockTransport(external_kodi.handle)
    )
    web = ProductionProfileWebClient(client=cast(Any, client), base_url=BASE_URL)
    web.login(PASSWORD)
    lifecycle = ProductionProfileLifecycle(web)
    registered = lifecycle.register_external_production(
        username="fixture-kodi-user", password="fixture-kodi-password"
    )
    observed = lifecycle.observe_native(registered.target_id)
    assert observed.observation_id is not None
    lifecycle.confirm_native_identity(registered.target_id, observed.observation_id)
    lifecycle.designate_production(registered.target_id, observed.observation_id)
    ready = lifecycle.enable_production_read(registered.target_id)
    return web, ready


def test_exact_web_authority_and_three_tool_transport(
    client: TestClient,
    database: sessionmaker[Session],
    external_kodi: FakeExternalKodi,
    mcp_audit_records: list[logging.LogRecord],
) -> None:
    web, ready = _ready_target(client, external_kodi)
    target_id = ready.target_id
    with ProductionProfileMcpInspection(
        web, mcp_client=cast(Any, client)
    ) as inspection:
        authority = inspection.establish_authority(
            target_id=target_id, identity_generation=1
        )
        plaintext_token = inspection._token
        assert plaintext_token is not None
        assert authority.target_id == target_id
        protocol = inspection.inspect_catalog()
        assert protocol.tool_names == ("addon_list", "target_list", "target_status")
        assert not [
            record
            for record in mcp_audit_records
            if "mcp_request " in record.getMessage()
        ]
        invocations = inspection.invoke_exact_three()
        assert [item.tool_name for item in invocations] == [
            "target_list",
            "target_status",
            "addon_list",
        ]
        assert len({item.request_id for item in invocations}) == 3
        payloads = _audit_payloads(mcp_audit_records)
        for item in invocations:
            assert any(
                payload["tool"] == item.tool_name
                and payload["protocol_request"]
                == mcp_audit_request_correlation(item.request_id)
                for payload in payloads
            )
        audit_messages = [
            record.getMessage()
            for record in mcp_audit_records
            if "mcp_request " in record.getMessage()
        ]
        assert len(audit_messages) == 3
        assert plaintext_token not in "\n".join(audit_messages)
        assert "Authorization" not in "\n".join(audit_messages)
        assert "Bearer" not in "\n".join(audit_messages)
        serialized = json.dumps(dict(inspection.evidence), default=str)
        assert "Authorization" not in serialized
        assert "Bearer" not in serialized

    with database() as session:
        assert (
            session.execute(text("SELECT COUNT(*) FROM mcp_clients")).scalar_one() == 1
        )
        credential = session.execute(
            text("SELECT token_digest,last_used_at FROM mcp_credentials")
        ).one()
        assert len(credential.token_digest) == 64
        assert credential.last_used_at is not None
        assert (
            session.execute(
                text(
                    "SELECT COUNT(*) FROM mcp_production_target_scopes "
                    "WHERE scope='inspect' AND state='active'"
                )
            ).scalar_one()
            == 1
        )
        assert (
            session.execute(
                text(
                    "SELECT COUNT(*) FROM mcp_production_target_scopes "
                    "WHERE scope<>'inspect'"
                )
            ).scalar_one()
            == 0
        )
    assert external_kodi.mutations == 0
    assert external_kodi.methods[-7:] == [
        "JSONRPC.Ping",
        "JSONRPC.Version",
        "Application.GetProperties",
        "JSONRPC.Ping",
        "JSONRPC.Version",
        "Application.GetProperties",
        "Addons.GetAddons",
    ]
    web.logout()


def test_live_sequence_does_not_retry_or_continue_after_failure(
    client: TestClient, external_kodi: FakeExternalKodi
) -> None:
    web, ready = _ready_target(client, external_kodi)
    transport = _FailSecondToolCall(client)
    with ProductionProfileMcpInspection(
        web, mcp_client=cast(Any, transport)
    ) as inspection:
        inspection.establish_authority(
            target_id=ready.target_id,
            identity_generation=1,
        )
        inspection.inspect_catalog()
        with pytest.raises(
            ProductionProfileMcpInspectionError, match="mcp_transport_failed"
        ):
            inspection.invoke_exact_three()
    assert transport.tool_names == ["target_list", "target_status"]
    web.logout()


def test_authority_helpers_retain_web_security_boundary(
    client: TestClient, external_kodi: FakeExternalKodi
) -> None:
    web, ready = _ready_target(client, external_kodi)
    target_id = ready.target_id
    body = {
        "display_name": "Forbidden bypass",
        "preset_id": "11000000-0000-4000-8000-000000000005",
    }
    assert (
        web.post_authenticated(
            "/api/mcp/clients", json=body, csrf_token=None
        ).status_code
        == 403
    )
    assert (
        web.post_authenticated("/api/mcp/clients", json=body, origin=None).status_code
        == 403
    )
    assert client.get("/api/mcp/clients").json() == []
    with ProductionProfileMcpInspection(
        web, mcp_client=cast(Any, client)
    ) as inspection:
        inspection.establish_authority(target_id=target_id, identity_generation=1)
    assert len(client.get("/api/mcp/clients").json()) == 1
    web.logout()


def test_production_credential_failures_do_not_inherit_authority(
    client: TestClient,
    external_kodi: FakeExternalKodi,
    mcp_audit_records: list[logging.LogRecord],
) -> None:
    web, ready = _ready_target(client, external_kodi)
    with ProductionProfileMcpInspection(
        web, mcp_client=cast(Any, client)
    ) as inspection:
        authority = inspection.establish_authority(
            target_id=ready.target_id, identity_generation=1
        )
        request = {
            "jsonrpc": "2.0",
            "id": "credential-negative",
            "method": "tools/list",
            "params": {},
        }
        base_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": "tools/list",
        }
        assert (
            client.post("/mcp", headers=base_headers, json=request).status_code == 401
        )
        assert (
            client.post(
                "/mcp",
                headers={**base_headers, "Authorization": "Bearer " + "A" * 43},
                json=request,
            ).status_code
            == 401
        )

        other = web.post_authenticated(
            "/api/mcp/clients",
            json={
                "display_name": "Other bounded production client",
                "preset_id": str(PRODUCTION_INSPECTION_PRESET_ID),
            },
        ).json()
        issued = web.post_authenticated(
            f"/api/mcp/clients/{other['id']}/credentials",
            json={"expiration_seconds": 3600},
        ).json()
        other_token = issued.pop("token")
        denied = client.post(
            "/mcp",
            headers={
                **base_headers,
                "Authorization": f"Bearer {other_token}",
                "Mcp-Method": "tools/call",
                "Mcp-Name": "target_status",
            },
            json={
                "jsonrpc": "2.0",
                "id": "other-client",
                "method": "tools/call",
                "params": {
                    "name": "target_status",
                    "arguments": {"target_id": str(ready.target_id)},
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": (
                            MCP_PROTOCOL_VERSION
                        ),
                        "io.modelcontextprotocol/clientCapabilities": {},
                        "io.modelcontextprotocol/clientInfo": {
                            "name": "credential-negative-test",
                            "version": "1",
                        },
                    },
                },
            },
        )
        assert denied.status_code == 200
        assert denied.json()["result"]["structuredContent"] == {
            "error": "target_not_found_or_not_authorized"
        }

        revoked = web.post_authenticated(
            f"/api/mcp/clients/{authority.client_id}/credentials/"
            f"{authority.credential_id}/revoke"
        )
        assert revoked.status_code == 200
        with pytest.raises(
            ProductionProfileMcpInspectionError, match="mcp_transport_failed"
        ):
            inspection.inspect_catalog()
        audit_messages = [
            record.getMessage()
            for record in mcp_audit_records
            if "mcp_request " in record.getMessage()
        ]
        assert len(audit_messages) == 1
        payload = _audit_payloads(mcp_audit_records)[0]
        assert payload["tool"] == "target_status"
        assert payload["protocol_request"] == mcp_audit_request_correlation(
            "other-client"
        )
        assert payload["outcome"] == "authorization_denied"
        assert payload["result_code"] == "target_not_found_or_not_authorized"
        assert other_token not in audit_messages[0]
    web.logout()


def test_repeated_read_correlation_id_is_independently_audited(
    client: TestClient,
    external_kodi: FakeExternalKodi,
    mcp_audit_records: list[logging.LogRecord],
) -> None:
    web, ready = _ready_target(client, external_kodi)
    with ProductionProfileMcpInspection(
        web, mcp_client=cast(Any, client)
    ) as inspection:
        inspection.establish_authority(target_id=ready.target_id, identity_generation=1)
        token = inspection._token
        assert token is not None
        request_id = "6bb499af-fad2-43e9-b819-77ea0f3da3da"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "target_list",
        }
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "target_list",
                "arguments": {},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientCapabilities": {},
                    "io.modelcontextprotocol/clientInfo": {
                        "name": "audit-replay-test",
                        "version": "1",
                    },
                },
            },
        }
        first = client.post("/mcp", headers=headers, json=body)
        second = client.post("/mcp", headers=headers, json=body)
        assert first.status_code == second.status_code == 200
        assert first.json()["result"]["isError"] is False
        assert second.json()["result"]["isError"] is False

        audit_messages = [
            record.getMessage()
            for record in mcp_audit_records
            if "mcp_request " in record.getMessage()
        ]
        assert len(audit_messages) == 2
        payloads = _audit_payloads(mcp_audit_records)
        assert all(payload["tool"] == "target_list" for payload in payloads)
        assert all(
            payload["protocol_request"] == mcp_audit_request_correlation(request_id)
            for payload in payloads
        )
        assert all(payload["outcome"] == "success" for payload in payloads)
        assert token not in "\n".join(audit_messages)
    web.logout()


def test_dispatched_tool_calls_emit_exactly_one_terminal_audit(
    client: TestClient,
    external_kodi: FakeExternalKodi,
    mcp_audit_records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web, ready = _ready_target(client, external_kodi)
    with ProductionProfileMcpInspection(
        web, mcp_client=cast(Any, client)
    ) as inspection:
        inspection.establish_authority(target_id=ready.target_id, identity_generation=1)
        token = inspection._token
        assert token is not None
        tools = _tool_service(client)
        original = tools.target_list

        successful = _call_tool(
            client,
            token,
            request_id="terminal-success",
            tool_name="target_list",
        )
        assert successful.json()["result"]["isError"] is False

        denied = _call_tool(
            client,
            token,
            request_id="terminal-denied",
            tool_name="target_status",
            arguments={"target_id": "14000000-0000-4000-8000-000000000099"},
        )
        assert denied.json()["result"]["isError"] is True

        def tool_failure(*_args: object, **_kwargs: object) -> None:
            raise McpAuthorizationError("target_unreachable")

        monkeypatch.setattr(tools, "target_list", tool_failure)
        failed = _call_tool(
            client,
            token,
            request_id="terminal-tool-error",
            tool_name="target_list",
        )
        assert failed.json()["result"]["structuredContent"] == {
            "error": "target_unreachable"
        }

        def unexpected(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected adapter failure")

        monkeypatch.setattr(tools, "target_list", unexpected)
        internal = _call_tool(
            client,
            token,
            request_id="terminal-internal-error",
            tool_name="target_list",
        )
        assert internal.status_code == 200
        assert "error" in internal.json()
        monkeypatch.setattr(tools, "target_list", original)

        payloads = _audit_payloads(mcp_audit_records)
        assert len(payloads) == 4
        assert [payload["outcome"] for payload in payloads] == [
            "success",
            "authorization_denied",
            "tool_error",
            "internal_error",
        ]
        assert [payload["result_code"] for payload in payloads] == [
            "success",
            "target_not_found_or_not_authorized",
            "target_unreachable",
            "internal_error",
        ]
        for request_id, payload in zip(
            (
                "terminal-success",
                "terminal-denied",
                "terminal-tool-error",
                "terminal-internal-error",
            ),
            payloads,
            strict=True,
        ):
            assert payload["protocol_request"] == mcp_audit_request_correlation(
                request_id
            )
    web.logout()


@pytest.mark.parametrize(
    "request_id",
    (
        "line-one\nmcp_request forged=true",
        "carriage\rreturn",
        "tab\tand\x01control",
        'quote"and\\backslash',
        "x" * 32_000,
        "Bearer fake-secret-value",
    ),
)
def test_protocol_request_id_audit_is_hashed_bounded_and_single_line(
    client: TestClient,
    external_kodi: FakeExternalKodi,
    mcp_audit_records: list[logging.LogRecord],
    request_id: str,
) -> None:
    web, ready = _ready_target(client, external_kodi)
    with ProductionProfileMcpInspection(
        web, mcp_client=cast(Any, client)
    ) as inspection:
        inspection.establish_authority(target_id=ready.target_id, identity_generation=1)
        token = inspection._token
        assert token is not None
        response = _call_tool(
            client,
            token,
            request_id=request_id,
            tool_name="target_list",
        )
        assert response.json()["result"]["isError"] is False
        messages = [
            record.getMessage()
            for record in mcp_audit_records
            if "mcp_request " in record.getMessage()
        ]
        assert len(messages) == 1
        assert len(messages[0].splitlines()) == 1
        assert len(messages[0].encode("utf-8")) < 1_024
        assert request_id not in messages[0]
        assert "Bearer fake-secret-value" not in messages[0]
        payload = _audit_payloads(mcp_audit_records)[0]
        assert payload["protocol_request"] == mcp_audit_request_correlation(request_id)
    web.logout()


def test_unrecognized_tool_name_audit_is_hashed_bounded_and_single_line(
    client: TestClient,
    external_kodi: FakeExternalKodi,
    mcp_audit_records: list[logging.LogRecord],
) -> None:
    web, ready = _ready_target(client, external_kodi)
    hostile_name = 'unknown-"\\-Bearer-fake-secret-value-' + ("x" * 8_000)
    with ProductionProfileMcpInspection(
        web, mcp_client=cast(Any, client)
    ) as inspection:
        inspection.establish_authority(target_id=ready.target_id, identity_generation=1)
        token = inspection._token
        assert token is not None
        response = _call_tool(
            client,
            token,
            request_id="hostile-tool-name",
            tool_name=hostile_name,
        )
        assert response.json()["result"]["isError"] is True
        messages = [
            record.getMessage()
            for record in mcp_audit_records
            if "mcp_request " in record.getMessage()
        ]
        assert len(messages) == 1
        assert len(messages[0].splitlines()) == 1
        assert len(messages[0].encode("utf-8")) < 1_024
        assert hostile_name not in messages[0]
        assert "Bearer fake-secret-value" not in messages[0]
        payload = _audit_payloads(mcp_audit_records)[0]
        assert payload["tool"] == "unrecognized"
        assert payload["tool_correlation"] == mcp_audit_request_correlation(
            hostile_name
        )
        assert payload["outcome"] == "authorization_denied"
    web.logout()


def test_audit_logger_failure_does_not_replace_successful_tool_result(
    client: TestClient,
    external_kodi: FakeExternalKodi,
    mcp_audit_records: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web, ready = _ready_target(client, external_kodi)
    with ProductionProfileMcpInspection(
        web, mcp_client=cast(Any, client)
    ) as inspection:
        inspection.establish_authority(target_id=ready.target_id, identity_generation=1)
        token = inspection._token
        assert token is not None

        def fail_audit(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected logging failure")

        monkeypatch.setattr(transport_module, "_audit", fail_audit)
        response = _call_tool(
            client,
            token,
            request_id="audit-logger-failure",
            tool_name="target_list",
        )
        assert response.json()["result"]["isError"] is False
        assert not _audit_payloads(mcp_audit_records)
        assert any(
            record.getMessage() == "mcp_audit_emit_failed"
            for record in mcp_audit_records
        )
    web.logout()
