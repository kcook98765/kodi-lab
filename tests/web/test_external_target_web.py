from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.external_onboarding import (
    ExternalKodiOnboardingError,
    ExternalKodiOnboardingResult,
)
from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
from kodi_lab.application.mcp_workspace_development import ExternalBaselineResult
from kodi_lab.kodi.external import ExternalKodiProbeResult
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.storage.secrets import ExternalBridgeSecretStore

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


def test_simple_otp_onboarding_api_is_admin_only_and_redacted(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = uuid4()
    calls: list[dict[str, object]] = []

    class FakeOnboarding:
        def onboard(self, **payload: object) -> ExternalKodiOnboardingResult:
            calls.append(payload)
            return ExternalKodiOnboardingResult(
                target_id=target_id,
                target_name="Windows Kodi test",
                kodi_version="20.5",
                bridge_id=uuid4(),
                bridge_version="1.1.13",
                bridge_fingerprint="a" * 64,
            )

        def resume(self, requested: object) -> ExternalKodiOnboardingResult:
            assert requested == target_id
            raise ExternalKodiOnboardingError(
                "pairing_commit_failed", target_id=target_id
            )

    onboarding = cast(FastAPI, client.app).state.kodi_lab.external_onboarding
    fake = FakeOnboarding()
    monkeypatch.setattr(onboarding, "onboard", fake.onboard)
    monkeypatch.setattr(onboarding, "resume", fake.resume)
    payload = {
        "name": "Windows Kodi test",
        "address": "192.168.50.135",
        "code": "7KQ4",
    }
    route = "/api/external-targets/onboard-otp"
    assert client.post(route, json=payload).status_code == 401
    csrf = login(client)
    assert client.post(route, json=payload).status_code == 403
    response = client.post(route, json=payload, headers=_headers(csrf))
    assert response.status_code == 201
    assert response.json()["target_id"] == str(target_id)
    assert calls == [
        {
            **payload,
            "kodi_port": 8080,
            "bridge_port": 8765,
            "username": None,
            "password": None,
        }
    ]
    assert "credential" not in response.text and "secret" not in response.text
    resumed = client.post(
        f"/api/external-targets/onboarding/{target_id}/resume",
        headers=_headers(csrf),
    )
    assert resumed.status_code == 409
    assert resumed.json()["error"] == {
        "code": "pairing_commit_failed",
        "message": "Pairing could not finish. Resume pairing to check its state.",
        "target_id": str(target_id),
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


def test_external_baseline_establishment_is_web_csrf_only(
    client: TestClient, monkeypatch: Any
) -> None:
    target_id = uuid4()
    source_id = uuid4()
    bridge_id = uuid4()
    route = f"/api/external-targets/{target_id}/development-baselines"
    assert (
        client.post(
            route, json={"confirmed": True, "source_id": str(source_id)}
        ).status_code
        == 401
    )
    csrf = login(client)
    assert (
        client.post(
            route,
            json={"confirmed": True, "source_id": str(source_id)},
            headers=ORIGIN,
        ).status_code
        == 403
    )
    app = cast(FastAPI, client.app)
    development = app.state.kodi_lab.mcp_workspace_development
    monkeypatch.setattr(
        development,
        "establish_external_baseline",
        lambda *, target_id, source_id: ExternalBaselineResult(
            target_id=target_id,
            source_id=source_id,
            bridge_id=bridge_id,
            addon_id="plugin.video.fixture",
            addon_version="1.0.0",
            manifest_digest="sha256:" + "a" * 64,
            file_count=2,
            total_bytes=64,
            established_at=datetime(2026, 9, 21, tzinfo=UTC),
        ),
    )
    response = client.post(
        route,
        json={"confirmed": True, "source_id": str(source_id)},
        headers=_headers(csrf),
    )
    assert response.status_code == 201
    assert response.json()["bridge_id"] == str(bridge_id)

    reconciliation = (
        f"/api/external-targets/{target_id}/deployments/{uuid4()}/reconcile"
    )
    assert client.post(reconciliation, json={"confirmed": True}).status_code == 403
    assert (
        client.post(
            reconciliation,
            json={"confirmed": True, "retry": True},
            headers=_headers(csrf),
        ).status_code
        == 422
    )
    assert (
        client.post(
            reconciliation,
            json={"confirmed": True},
            headers=_headers(csrf),
        ).status_code
        == 404
    )


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


def test_bridge_pairing_administration_is_web_only_show_once_and_csrf_protected(
    client: TestClient,
    tmp_path: Path,
) -> None:
    payload = _registration()
    payload["configured_capabilities"] = ["target_inspect", "screenshot"]
    assert (
        client.post(
            f"/api/external-targets/{uuid4()}/bridge/pairing-token",
            json={"confirmed": True, "bridge_port": 8765},
        ).status_code
        == 401
    )
    csrf = login(client)
    created = client.post(
        "/api/external-targets", json=payload, headers=_headers(csrf)
    ).json()
    target_id = created["target_id"]
    app = cast(FastAPI, client.app)
    app.state.kodi_lab.external_bridges._secrets = ExternalBridgeSecretStore(
        tmp_path / "web-bridge-secrets", master_key=b"b" * 32, key_version=1
    )
    service = cast(
        ExternalTargetAdministrationService, app.state.kodi_lab.external_targets
    )
    service._verifier = SuccessfulExternalVerifier()  # type: ignore[assignment]
    observed = client.post(
        f"/api/external-targets/{target_id}/probe",
        json={},
        headers=_headers(csrf),
    ).json()
    client.post(
        f"/api/external-targets/{target_id}/confirm",
        json={
            "confirmed": True,
            "observation_id": observed["pending_observation_id"],
        },
        headers=_headers(csrf),
    )
    client.post(
        f"/api/external-targets/{target_id}/enabled",
        json={"enabled": True},
        headers=_headers(csrf),
    )
    url = f"/api/external-targets/{target_id}/bridge/pairing-token"
    body = {"confirmed": True, "bridge_port": 8765}
    assert client.post(url, json=body).status_code == 403
    assert (
        client.post(url, json=body, headers={"X-Kodi-Lab-CSRF": csrf}).status_code
        == 403
    )
    assert (
        client.post(
            url,
            json={"confirmed": False, "bridge_port": 8765},
            headers=_headers(csrf),
        ).status_code
        == 422
    )
    assert (
        client.post(
            url,
            json={**body, "secret": "caller-owned"},
            headers=_headers(csrf),
        ).status_code
        == 422
    )
    generated = client.post(url, json=body, headers=_headers(csrf))
    assert generated.status_code == 201
    assert generated.headers["content-type"] == "application/json"
    generated_payload = generated.json()
    assert set(generated_payload) == {"target_id", "token", "expires_at"}
    token = generated_payload["token"]
    assert len(token) == 43
    status = client.get(f"/api/external-targets/{target_id}/bridge").json()
    assert status["pairing_token_pending"] is True
    assert "token" not in status
    page = client.get("/external-targets")
    assert page.status_code == 200
    assert token not in page.text

    revoke = f"/api/external-targets/{target_id}/bridge/revoke"
    assert (
        client.post(
            revoke, json={"confirmed": False}, headers=_headers(csrf)
        ).status_code
        == 422
    )
    assert client.post(revoke, json={"confirmed": True}).status_code == 403
    revoked = client.post(revoke, json={"confirmed": True}, headers=_headers(csrf))
    assert revoked.status_code == 200
    assert revoked.json()["pairing_state"] == "revoked"

    names = set(build_mcp_tool_registry().names())
    assert not names.intersection(
        {
            "bridge_pair",
            "bridge_pairing_token",
            "bridge_revoke",
            "bridge_set_endpoint",
        }
    )
