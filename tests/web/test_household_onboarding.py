from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.household_onboarding import (
    HouseholdKodiOnboardingService,
    HouseholdOnboardingResult,
    HouseholdStartRequest,
)
from kodi_lab.storage.models import HouseholdOnboardingRow, TargetRow
from tests.web.conftest import ORIGIN, login


def test_household_api_auth_csrf_same_origin_and_structured_result(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation_id = uuid4()
    path = "/api/household-onboarding"
    body: dict[str, Any] = {
        "operation_id": str(operation_id),
        "name": "Living room",
        "address": "192.168.50.40",
        "code": "2345",
    }
    assert client.post(path, json=body).status_code == 401
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert client.post(path, json=body).status_code == 403
    assert (
        client.post(
            path, json=body, headers={**headers, "Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    app = cast(FastAPI, client.app)
    service = cast(
        HouseholdKodiOnboardingService, app.state.kodi_lab.household_onboarding
    )
    seen: list[HouseholdStartRequest] = []

    def start(payload: HouseholdStartRequest) -> HouseholdOnboardingResult:
        seen.append(payload)
        return HouseholdOnboardingResult(
            operation_id=payload.operation_id,
            state="requires_action",
            code="transport_consent_required",
            message="Review HTTP risks.",
        )

    monkeypatch.setattr(service, "start", start)
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["state"] == "requires_action"
    assert response.json()["operation_id"] == str(operation_id)
    assert "2345" not in response.text
    assert seen[0].code.get_secret_value() == "2345"
    for action in ("resume", "abandon"):
        endpoint = f"{path}/{operation_id}/{action}"
        assert client.post(endpoint, json={}).status_code == 403
        assert (
            client.post(
                endpoint, json={}, headers={**headers, "Origin": "https://evil.invalid"}
            ).status_code
            == 403
        )
        response = client.post(endpoint, json={}, headers=headers)
        assert response.status_code == 200
        assert response.json()["code"] == "operation_not_found"


@pytest.mark.parametrize(
    "changes",
    [
        {"code": "SECRET-OTP"},
        {"password": "SECRET-PASSWORD", "extra": "SECRET-EXTRA"},
        {"kodi_port": True},
        {"allow_remote_control": True},
    ],
)
def test_invalid_household_request_never_echoes_secrets(
    client: TestClient,
    changes: dict[str, object],
) -> None:
    csrf = login(client)
    response = client.post(
        "/api/household-onboarding",
        json={
            "operation_id": str(uuid4()),
            "name": "Kodi",
            "address": "192.168.50.40",
            "code": "2345",
            **changes,
        },
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    # An invalid OTP reaches bounded service validation; closed-schema/type
    # errors use the route's content-free FastAPI validation handler.
    assert response.status_code in {200, 422}
    assert "SECRET" not in response.text
    assert "2345" not in response.text


def test_resume_decision_requires_strict_explicit_consent(client: TestClient) -> None:
    csrf = login(client)
    response = client.post(
        f"/api/household-onboarding/{uuid4()}/resume",
        json={
            "code": "SECRET-OTP",
            "transport_decision": {
                "disclosure_token": "a" * 64,
                "accept_http_risk": "yes",
            },
        },
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 422
    assert "SECRET" not in response.text


def test_initial_native_storage_failure_returns_household_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    csrf = login(client)
    app = cast(FastAPI, client.app)
    service = cast(
        HouseholdKodiOnboardingService, app.state.kodi_lab.household_onboarding
    )
    monkeypatch.setattr(service._targets._credential_store, "_master_key", None)
    operation_id = uuid4()
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    response = client.post(
        "/api/household-onboarding",
        json={
            "operation_id": str(operation_id),
            "name": "Credential recovery",
            "address": "192.168.50.40",
            "code": "2345",
            "username": "fixture",
            "password": "fixture-sensitive-password",
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["operation_id"] == str(operation_id)
    assert response.json()["state"] == "incomplete"
    assert response.json()["code"] == "secret_unavailable"
    assert response.json()["target_id"] is None
    assert "2345" not in response.text
    assert "fixture-sensitive-password" not in response.text
    with service._sessions() as session:
        operation = session.get(HouseholdOnboardingRow, str(operation_id))
        assert operation is not None and operation.stage == "reserved"
        assert session.get(TargetRow, operation.target_id) is None
    resumed = client.post(
        f"/api/household-onboarding/{operation_id}/resume", json={}, headers=headers
    )
    assert resumed.status_code == 200
    assert resumed.json()["code"] == "registration_retry_required"
    assert resumed.json()["target_id"] is None
