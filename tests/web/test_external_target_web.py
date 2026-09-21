from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
from kodi_lab.kodi.external import ExternalKodiProbeResult

from .conftest import INSTANCE_ID, ORIGIN, login

PROTOCOL = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {
        "name": "external-target-non-exposure-test",
        "version": "1",
    },
}


class SuccessfulExternalVerifier:
    def probe(
        self, endpoint: Any, _credential: Any, configured: frozenset[str]
    ) -> ExternalKodiProbeResult:
        return ExternalKodiProbeResult(
            addresses=(endpoint.address,),
            kodi_name="Kodi",
            kodi_version="21.3",
            protocol_version="13.0.0",
            verified_capabilities=(
                ("target_inspect",) if "target_inspect" in configured else ()
            ),
        )


def _headers(csrf: str) -> dict[str, str]:
    return {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}


def _registration() -> dict[str, object]:
    return {
        "logical_name": "Disposable external proof",
        "protocol_kind": "jsonrpc_http",
        "host": "192.168.50.30",
        "port": 8080,
        "authentication_mode": "none",
        "configured_capabilities": [
            "target_inspect",
            "addon_inventory",
            "semantic_remote",
        ],
        "username": None,
        "password": None,
    }


def _mcp_call(
    client: TestClient, token: str, name: str, arguments: dict[str, object]
) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL,
            "Mcp-Method": "tools/call",
            "Mcp-Name": name,
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments, "_meta": META},
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    return cast(dict[str, Any], response.json())


def test_external_target_web_flow_and_safe_projection(client: TestClient) -> None:
    assert client.get("/api/external-targets").status_code == 401
    assert (
        client.get(
            "/api/external-targets", headers={"Authorization": "Bearer not-web"}
        ).status_code
        == 401
    )
    csrf = login(client)
    assert client.post("/api/external-targets", json=_registration()).status_code == 403
    assert (
        client.post(
            "/api/external-targets",
            json=_registration(),
            headers={"X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 403
    )
    created_response = client.post(
        "/api/external-targets", json=_registration(), headers=_headers(csrf)
    )
    assert created_response.status_code == 201
    created = created_response.json()
    target_id = created["target_id"]
    assert created["administrative_state"] == "disabled"
    assert created["verification_state"] == "unverified"
    assert "password" not in created
    assert (
        client.post(
            f"/api/external-targets/{target_id}/enabled",
            json={"enabled": True},
            headers=_headers(csrf),
        ).json()["error"]["code"]
        == "verification_required"
    )

    app = cast(FastAPI, client.app)
    service = cast(
        ExternalTargetAdministrationService, app.state.kodi_lab.external_targets
    )
    service._verifier = SuccessfulExternalVerifier()  # type: ignore[assignment]
    probe = client.post(
        f"/api/external-targets/{target_id}/probe",
        json={},
        headers=_headers(csrf),
    )
    assert probe.status_code == 200
    observed = probe.json()
    assert observed["verification_state"] == "unverified"
    assert observed["kodi_name"] == "Kodi"
    assert observed["verified_capabilities"] == ["target_inspect"]

    forged = client.post(
        f"/api/external-targets/{target_id}/confirm",
        json={"confirmed": True, "observation_id": str(uuid4())},
        headers=_headers(csrf),
    )
    assert forged.status_code == 409
    assert (
        client.post(
            f"/api/external-targets/{target_id}/confirm",
            json={
                "confirmed": False,
                "observation_id": observed["pending_observation_id"],
            },
            headers=_headers(csrf),
        ).status_code
        == 422
    )
    confirmed = client.post(
        f"/api/external-targets/{target_id}/confirm",
        json={
            "confirmed": True,
            "observation_id": observed["pending_observation_id"],
        },
        headers=_headers(csrf),
    )
    assert confirmed.status_code == 200
    enabled = client.post(
        f"/api/external-targets/{target_id}/enabled",
        json={"enabled": True},
        headers=_headers(csrf),
    )
    assert enabled.json()["administrative_state"] == "enabled"
    status = client.get(f"/api/external-targets/{target_id}").json()
    assert status["host"] == "192.168.50.30"
    assert status["authentication_mode"] == "none"
    assert "secret_reference" not in status

    client.post(
        f"/api/external-targets/{target_id}/enabled",
        json={"enabled": False},
        headers=_headers(csrf),
    )
    changed = client.put(
        f"/api/external-targets/{target_id}/endpoint",
        json={
            "protocol_kind": "jsonrpc_http",
            "host": "192.168.50.31",
            "port": 8080,
        },
        headers=_headers(csrf),
    ).json()
    assert changed["verification_state"] == "needs_reverification"
    rotated = client.post(
        f"/api/external-targets/{target_id}/credentials",
        json={"authentication_mode": "none", "username": None, "password": None},
        headers=_headers(csrf),
    ).json()
    assert rotated["verification_state"] == "needs_reverification"
    deactivated = client.post(
        f"/api/external-targets/{target_id}/deactivate",
        json={"confirmed": True},
        headers=_headers(csrf),
    ).json()
    assert deactivated["administrative_state"] == "deactivated"
    events = client.get(f"/api/external-targets/{target_id}/events").json()
    assert events[-1]["event_kind"] == "deactivated"


def test_external_target_web_rejects_ssrf_and_invalid_identity(
    client: TestClient,
) -> None:
    csrf = login(client)
    for host in ("127.0.0.1", "::1", "169.254.169.254", "8.8.8.8", "10.250.0.8"):
        payload = _registration()
        payload["host"] = host
        response = client.post(
            "/api/external-targets", json=payload, headers=_headers(csrf)
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "endpoint_not_allowed"
    assert client.get("/api/external-targets/not-a-uuid").status_code == 422
    assert (
        client.get(
            "/api/external-targets", headers={"host": "attacker.example"}
        ).status_code
        == 400
    )


def test_mcp_cannot_list_or_use_registered_external_target(
    client: TestClient,
) -> None:
    csrf = login(client)
    created = client.post(
        "/api/external-targets",
        json=_registration(),
        headers=_headers(csrf),
    ).json()
    target_id = created["target_id"]
    app = cast(FastAPI, client.app)
    administration = app.state.kodi_lab.mcp_administration
    principal = administration.create_client(
        display_name="External non-exposure",
        preset_id=FULL_LAB_CONTROL_PRESET_ID,
    )
    administration.assign_target_preset(INSTANCE_ID, FULL_LAB_CONTROL_PRESET_ID)
    administration.grant_target(principal.id, INSTANCE_ID)
    token = administration.issue_credential(principal.id).token

    listed = _mcp_call(client, token, "target_list", {})
    assert [
        item["target_id"] for item in listed["result"]["structuredContent"]["targets"]
    ] == [str(INSTANCE_ID)]
    attempts = (
        ("target_status", {"target_id": target_id}),
        ("addon_list", {"target_id": target_id}),
        (
            "remote_action",
            {
                "request_id": str(uuid4()),
                "target_id": target_id,
                "action": "left",
            },
        ),
    )
    for name, arguments in attempts:
        result = _mcp_call(client, token, name, arguments)["result"]
        assert result["isError"] is True
