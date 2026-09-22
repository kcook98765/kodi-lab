from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.mcp_administration import READ_ONLY_INSPECTION_PRESET_ID
from kodi_lab.application.production_inspection import PRODUCTION_INSPECTION_PRESET_ID
from kodi_lab.kodi.external import ExternalKodiProbeResult

from .conftest import ORIGIN, login


class SuccessfulProductionVerifier:
    def probe(
        self, endpoint: Any, _credential: Any, configured: frozenset[str]
    ) -> ExternalKodiProbeResult:
        return ExternalKodiProbeResult(
            addresses=(endpoint.address,),
            kodi_name="Kodi",
            kodi_version="21.3",
            protocol_version="13.0.0",
            verified_capabilities=tuple(sorted(configured)),
        )


def _headers(csrf: str) -> dict[str, str]:
    return {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}


def _registration() -> dict[str, object]:
    return {
        "logical_name": "Production Kodi",
        "protocol_kind": "jsonrpc_http",
        "host": "192.168.50.40",
        "port": 8080,
        "authentication_mode": "none",
        "configured_capabilities": ["target_inspect", "addon_inventory"],
        "username": None,
        "password": None,
    }


def test_production_web_flow_is_separate_two_step_and_web_only(
    client: TestClient,
) -> None:
    assert client.get("/api/production-targets").status_code == 401
    csrf = login(client)
    forged_class = _registration()
    forged_class["target_class"] = "external_development"
    assert (
        client.post(
            "/api/production-targets",
            json=forged_class,
            headers=_headers(csrf),
        ).status_code
        == 422
    )
    assert (
        client.post("/api/production-targets", json=_registration()).status_code == 403
    )
    created_response = client.post(
        "/api/production-targets",
        json=_registration(),
        headers=_headers(csrf),
    )
    assert created_response.status_code == 201
    created = created_response.json()
    target_id = created["target_id"]
    assert created["target_class"] == "external_production"
    assert created["production_policy"]["lifecycle_state"] == (
        "registered_disabled_unverified"
    )
    assert client.get(f"/api/external-targets/{target_id}").status_code == 404
    assert target_id in {
        item["target_id"] for item in client.get("/api/mcp/target-policies").json()
    }
    wrong_policy = client.post(
        f"/api/mcp/target-policies/{target_id}",
        json={"preset_id": str(READ_ONLY_INSPECTION_PRESET_ID)},
        headers=_headers(csrf),
    )
    assert wrong_policy.status_code == 409
    assigned_policy = client.post(
        f"/api/mcp/target-policies/{target_id}",
        json={"preset_id": str(PRODUCTION_INSPECTION_PRESET_ID)},
        headers=_headers(csrf),
    )
    assert assigned_policy.status_code == 200

    app = cast(FastAPI, client.app)
    service = cast(
        ExternalTargetAdministrationService, app.state.kodi_lab.production_targets
    )
    service._verifier = SuccessfulProductionVerifier()  # type: ignore[assignment]
    observed = client.post(
        f"/api/production-targets/{target_id}/probe",
        json={},
        headers=_headers(csrf),
    ).json()
    observation_id = observed["pending_observation_id"]
    assert observed["production_policy"]["designation_state"] == (
        "pending_native_identity"
    )

    native = client.post(
        f"/api/production-targets/{target_id}/confirm-native",
        json={"confirmed": True, "observation_id": observation_id},
        headers=_headers(csrf),
    )
    assert native.status_code == 200
    assert native.json()["production_policy"]["designation_state"] == (
        "pending_confirmation"
    )
    denied = client.post(
        f"/api/production-targets/{target_id}/enabled",
        json={"enabled": True},
        headers=_headers(csrf),
    )
    assert denied.json()["error"]["code"] == "production_designation_required"

    bad_wording = client.post(
        f"/api/production-targets/{target_id}/confirm-designation",
        json={
            "confirmed": True,
            "observation_id": observation_id,
            "acknowledgement": "generic confirmation",
        },
        headers=_headers(csrf),
    )
    assert bad_wording.status_code == 422
    designated = client.post(
        f"/api/production-targets/{target_id}/confirm-designation",
        json={
            "confirmed": True,
            "observation_id": observation_id,
            "acknowledgement": (
                "I confirm this is a production Kodi target and mutations may "
                "affect real user state."
            ),
        },
        headers=_headers(csrf),
    )
    assert designated.status_code == 200
    enabled = client.post(
        f"/api/production-targets/{target_id}/enabled",
        json={"enabled": True},
        headers=_headers(csrf),
    ).json()
    assert enabled["production_policy"]["lifecycle_state"] == "enabled_read_only"

    created_client = client.post(
        "/api/mcp/clients",
        json={
            "display_name": "Production reader",
            "preset_id": str(PRODUCTION_INSPECTION_PRESET_ID),
        },
        headers=_headers(csrf),
    )
    assert created_client.status_code == 201
    client_id = created_client.json()["id"]
    granted = client.post(
        f"/api/mcp/clients/{client_id}/targets/{target_id}",
        headers=_headers(csrf),
    )
    assert granted.status_code == 200
    scope_url = (
        f"/api/mcp/clients/{client_id}/targets/{target_id}/production-inspection-scopes"
    )
    assert client.post(scope_url, json={"identity_generation": 1}).status_code == 403
    scope = client.post(
        scope_url,
        json={"identity_generation": 1},
        headers=_headers(csrf),
    )
    assert scope.status_code == 201
    scope_body = scope.json()
    assert scope_body["effective_state"] == "active"
    assert set(scope_body) == {
        "scope_id",
        "client_id",
        "target_id",
        "identity_generation",
        "current_identity_generation",
        "scope",
        "state",
        "effective_state",
        "created_at",
        "created_by",
        "revoked_at",
        "revoked_by",
    }
    assert "192.168.50.40" not in scope.text
    revoked = client.post(
        f"/api/mcp/production-inspection-scopes/{scope_body['scope_id']}/revoke",
        headers=_headers(csrf),
    )
    assert revoked.status_code == 200
    assert revoked.json()["effective_state"] == "revoked"


def test_production_mutation_controls_are_csrf_protected_and_epoch_monotonic(
    client: TestClient,
) -> None:
    csrf = login(client)
    initial = client.get("/api/production-targets/mutation-control").json()
    assert initial["production_mutations_blocked"] is True
    assert initial["policy_epoch"] == 1
    assert (
        client.post(
            "/api/production-targets/mutation-control", json={"blocked": False}
        ).status_code
        == 403
    )
    cleared = client.post(
        "/api/production-targets/mutation-control",
        json={"blocked": False},
        headers=_headers(csrf),
    ).json()
    assert cleared["production_mutations_blocked"] is False
    assert cleared["policy_epoch"] == 2
