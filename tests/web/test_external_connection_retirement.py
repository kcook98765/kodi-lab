from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.external_bridges import ExternalBridgeServiceError
from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.mcp_administration import READ_ONLY_INSPECTION_PRESET_ID
from kodi_lab.application.production_inspection import PRODUCTION_INSPECTION_PRESET_ID
from kodi_lab.storage.models import ExternalKodiTargetRow, ExternalSecretRetirementRow
from kodi_lab.storage.secrets import SecretStoreError
from tests.web.test_external_target_web import SuccessfulExternalVerifier

from .conftest import ORIGIN, login


def _register(client: TestClient, route: str, csrf: str) -> UUID:
    response = client.post(
        f"/api/{route}",
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        json={
            "logical_name": "Living Room",
            "protocol_kind": "jsonrpc_http",
            "host": "192.168.50.30",
            "port": 8080,
            "authentication_mode": "username_password",
            "username": "kodi",
            "password": "fixture-password",
            "configured_capabilities": ["target_inspect"],
        },
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["target_id"])


@pytest.mark.parametrize("route", ["external-targets", "production-targets"])
@pytest.mark.parametrize("already_deactivated", [False, True])
def test_retirement_preserves_exact_replacement_and_audit_history(
    client: TestClient, route: str, already_deactivated: bool
) -> None:
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    context = cast(FastAPI, client.app).state.kodi_lab
    service: ExternalTargetAdministrationService = (
        context.external_targets
        if route == "external-targets"
        else context.production_targets
    )
    service._verifier = SuccessfulExternalVerifier()  # type: ignore[assignment]
    old_id = _register(client, route, csrf)
    replacement_id = _register(client, route, csrf)
    for target_id in (old_id, replacement_id):
        observed = service.probe(target_id)
        assert observed.pending_observation_id is not None
        service.confirm(target_id, observed.pending_observation_id)
        if route == "production-targets":
            service.confirm_production_designation(
                target_id, observed.pending_observation_id
            )
        service.set_enabled(target_id, enabled=True)
    replacement = service.get(replacement_id)
    preset_id = (
        READ_ONLY_INSPECTION_PRESET_ID
        if route == "external-targets"
        else PRODUCTION_INSPECTION_PRESET_ID
    )
    agent = context.mcp_administration.create_client(
        display_name="Reader", preset_id=preset_id
    )
    context.mcp_administration.grant_target(agent.id, old_id)
    context.mcp_administration.grant_target(agent.id, replacement_id)
    with service._session_factory() as session:
        row = session.get(ExternalKodiTargetRow, str(old_id))
        assert row is not None and row.secret_reference is not None
        reference = UUID(row.secret_reference)
    if already_deactivated:
        service.deactivate(old_id)
    path = f"/api/{route}/{old_id}/retire"
    assert client.post(path, json={"confirmed": True}).status_code == 403
    assert (
        client.post(path, json={"confirmed": False}, headers=headers).status_code == 422
    )
    assert (
        client.post(path, json={"confirmed": True}, headers=headers).status_code == 200
    )
    retired = service.get(old_id)
    assert retired.administrative_state.value == "deactivated"
    assert retired.target_id == old_id
    assert service.get(replacement_id) == replacement
    assert context.mcp_administration.get_client(agent.id).target_grants == (
        replacement_id,
    )
    with pytest.raises(SecretStoreError):
        service._credential_store.read(old_id, reference)
    assert {item["target_id"] for item in client.get(f"/api/{route}").json()} == {
        str(replacement_id)
    }
    assert {
        item["target_id"]
        for item in client.get(f"/api/{route}?include_retired=true").json()
    } == {str(old_id), str(replacement_id)}
    assert str(old_id) not in client.get(f"/{route}").text
    history = client.get(f"/{route}?include_retired=true").text
    assert str(old_id) in history and str(replacement_id) in history
    assert "Retired" in history and "Retry credential cleanup" in history
    policies = client.get("/api/mcp/target-policies").json()
    assert str(old_id) not in {item["target_id"] for item in policies}
    assert str(replacement_id) in {item["target_id"] for item in policies}
    assert client.get(f"/api/{route}/{old_id}/events").status_code == 200
    assert "deactivated" in {item.event_kind for item in service.events(old_id)}
    assert (
        client.post(
            f"/api/{route}/{old_id}/enabled", json={"enabled": True}, headers=headers
        ).status_code
        == 409
    )
    assert (
        client.post(path, json={"confirmed": True}, headers=headers).status_code == 200
    )
    assert service.get(old_id).deactivated_at == retired.deactivated_at
    if route == "production-targets":
        assert str(old_id) not in client.get("/kodi").text
        assert str(old_id) not in client.get("/").text


@pytest.mark.parametrize(
    "route,wrong",
    [
        ("external-targets", "production-targets"),
        ("production-targets", "external-targets"),
    ],
)
def test_retirement_checks_class_before_revoking_any_authority(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, route: str, wrong: str
) -> None:
    csrf = login(client)
    target_id = _register(client, route, csrf)
    context = cast(FastAPI, client.app).state.kodi_lab
    calls: list[UUID] = []
    monkeypatch.setattr(
        context.external_bridges, "revoke", lambda requested: calls.append(requested)
    )
    response = client.post(
        f"/api/{wrong}/{target_id}/retire",
        json={"confirmed": True},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 404
    assert calls == []


@pytest.mark.parametrize("route", ["external-targets", "production-targets"])
def test_bridge_cleanup_failure_still_retires_native_access_and_can_retry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    csrf = login(client)
    target_id = _register(client, route, csrf)
    context: Any = cast(FastAPI, client.app).state.kodi_lab
    service: ExternalTargetAdministrationService = (
        context.external_targets
        if route == "external-targets"
        else context.production_targets
    )

    def fail(_target_id: UUID) -> None:
        raise ExternalBridgeServiceError("bridge_secret_retirement_failed")

    with monkeypatch.context() as patch:
        patch.setattr(context.external_bridges, "revoke", fail)
        response = client.post(
            f"/api/{route}/{target_id}/retire",
            json={"confirmed": True},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        )
    assert response.status_code == 503
    assert service.get(target_id).administrative_state.value == "deactivated"
    assert service.list_targets() == []
    assert (
        client.post(
            f"/api/{route}/{target_id}/retire",
            json={"confirmed": True},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 200
    )


@pytest.mark.parametrize("route", ["external-targets", "production-targets"])
def test_native_cleanup_failure_still_revokes_bridge_and_preserves_retry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    target_id = _register(client, route, csrf)
    context = cast(FastAPI, client.app).state.kodi_lab
    service: ExternalTargetAdministrationService = (
        context.external_targets
        if route == "external-targets"
        else context.production_targets
    )
    revoke = context.external_bridges.revoke
    calls: list[UUID] = []

    def observed_revoke(requested: UUID) -> Any:
        calls.append(requested)
        return revoke(requested)

    def fail(_reference: UUID) -> None:
        raise SecretStoreError("fixture storage temporarily unavailable")

    monkeypatch.setattr(context.external_bridges, "revoke", observed_revoke)
    with monkeypatch.context() as patch:
        patch.setattr(service._credential_store, "retire", fail)
        response = client.post(
            f"/api/{route}/{target_id}/retire",
            json={"confirmed": True},
            headers=headers,
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "connection_secret_retirement_failed"
    assert calls == [target_id]
    assert service.get(target_id).administrative_state.value == "deactivated"
    with service._session_factory() as session:
        assert session.query(ExternalSecretRetirementRow).count() == 1
    assert (
        client.post(
            f"/api/{route}/{target_id}/retire",
            json={"confirmed": True},
            headers=headers,
        ).status_code
        == 200
    )
    with service._session_factory() as session:
        assert session.query(ExternalSecretRetirementRow).count() == 0


@pytest.mark.parametrize(
    "route,script",
    [
        ("external-targets", "external-targets.js"),
        ("production-targets", "production-targets.js"),
    ],
)
def test_browser_retirement_confirms_exact_registration_and_does_not_guess_by_name(
    route: str, script: str
) -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("external_retirement_browser_harness.js")),
            str(root / "src/kodi_lab/web/static" / script),
            route,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
