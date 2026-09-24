from __future__ import annotations

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
from kodi_lab.mcp.transport import MCP_PROTOCOL_VERSION
from kodi_lab.production_profile_lifecycle import (
    ProductionLifecycleTransition,
    ProductionProfileLifecycle,
)
from kodi_lab.production_profile_mcp_inspection import (
    ProductionProfileMcpInspection,
    ProductionProfileMcpInspectionError,
)
from kodi_lab.production_profile_web_client import ProductionProfileWebClient

from .conftest import PASSWORD, FakeExternalKodi

BASE_URL = "http://127.0.0.1"


class _RecordHandler(logging.Handler):
    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__()
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


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
        for item in invocations:
            assert any(
                f"tool={item.tool_name} " in record.getMessage()
                and f"protocol_request_id={item.request_id} " in record.getMessage()
                for record in mcp_audit_records
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
        assert "tool=target_status " in audit_messages[0]
        assert "protocol_request_id=other-client " in audit_messages[0]
        assert "outcome=target_not_found_or_not_authorized" in audit_messages[0]
        assert "outcome=success" not in audit_messages[0]
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
        assert all("tool=target_list " in message for message in audit_messages)
        assert all(
            f"protocol_request_id={request_id} " in message
            for message in audit_messages
        )
        assert all("outcome=success" in message for message in audit_messages)
        assert token not in "\n".join(audit_messages)
    web.logout()
