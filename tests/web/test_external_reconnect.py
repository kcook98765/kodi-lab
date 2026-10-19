"""Guided reconnect routes remain Web-only, explicit and secret-safe."""

import subprocess
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.storage.models import ExternalReconnectOperationRow
from tests.web.test_external_target_web import SuccessfulExternalVerifier

from .conftest import ORIGIN, login


def _target(client: TestClient, route: str, csrf: str) -> UUID:
    context = cast(FastAPI, client.app).state.kodi_lab
    service: ExternalTargetAdministrationService = (
        context.production_targets
        if route == "production-targets"
        else context.external_targets
    )
    service._verifier = cast(Any, SuccessfulExternalVerifier())
    response = client.post(
        f"/api/{route}",
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        json={
            "logical_name": "Retained Kodi",
            "protocol_kind": "jsonrpc_http",
            "host": "192.168.50.30",
            "port": 8080,
            "authentication_mode": "none",
            "configured_capabilities": ["target_inspect", "screenshot"],
        },
    )
    assert response.status_code == 201
    target_id = UUID(response.json()["target_id"])
    probe = service.probe(target_id)
    assert probe.pending_observation_id is not None
    service.confirm(target_id, probe.pending_observation_id)
    if route == "production-targets":
        service.confirm_production_designation(target_id, probe.pending_observation_id)
    service.set_enabled(target_id, enabled=True)
    return target_id


@pytest.mark.parametrize("route", ["production-targets", "external-targets"])
def test_reconnect_page_and_consent_are_scoped_to_retained_registration(
    client: TestClient,
    route: str,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert client.get(f"/reconnect-kodi/{uuid4()}").status_code == 401
    csrf = login(client)
    target_id = _target(client, route, csrf)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    page = client.get(f"/reconnect-kodi/{target_id}")
    assert page.status_code == 200
    assert "Reconnect Retained Kodi" in page.text
    assert f'data-target-id="{target_id}"' in page.text
    assert 'value="8765"' in page.text and "data-js-gate disabled" in page.text
    assert 'name="password"' not in page.text
    assert f'href="/reconnect-kodi/{target_id}"' in client.get(f"/{route}").text
    operation_id = uuid4()
    path = f"/api/external-reconnect/{target_id}"
    assert (
        client.post(path, json={"operation_id": str(operation_id)}).status_code == 403
    )
    assert (
        client.post(
            path,
            headers={"Origin": "http://evil", "X-Kodi-Lab-CSRF": csrf},
            json={"operation_id": str(operation_id)},
        ).status_code
        == 403
    )
    invalid = client.post(
        path, headers=headers, json={"operation_id": str(operation_id), "code": "7KQ4"}
    )
    assert invalid.status_code == 422 and "7KQ4" not in invalid.text
    calls: list[int] = []
    context = cast(FastAPI, client.app).state.kodi_lab
    monkeypatch.setattr(
        context.external_bridges._client,
        "bootstrap_otp",
        lambda *_args, **_kwargs: calls.append(1),
    )
    first = client.post(path, headers=headers, json={"operation_id": str(operation_id)})
    assert first.status_code == 200
    assert first.json()["code"] == "transport_consent_required"
    assert first.json()["target_id"] == str(target_id)
    assert first.json()["transport"]["bridge_endpoint"] == "http://192.168.50.30:8765"
    resumed = client.post(f"{path}/{operation_id}/resume", headers=headers, json={})
    assert (
        resumed.status_code == 200
        and resumed.json()["code"] == "transport_consent_required"
    )
    assert calls == []
    assert (
        client.post(
            f"{path}/{operation_id}/replace-pairing",
            headers=headers,
            json={"confirmed": False},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{path}/{operation_id}/cancel", headers=headers, json={"confirmed": False}
        ).status_code
        == 422
    )
    cancelled = client.post(
        f"{path}/{operation_id}/cancel", headers=headers, json={"confirmed": True}
    )
    assert cancelled.status_code == 200 and cancelled.json()["code"] == "cancelled"
    with database() as session:
        rows = session.scalars(select(ExternalReconnectOperationRow)).all()
        assert len(rows) == 1 and rows[0].target_id == str(target_id)
    assert calls == []


def test_reconnect_browser_consent_recovery_and_secrets() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("reconnect_kodi_browser_harness.js")),
            str(root / "src/kodi_lab/web/static/reconnect-kodi.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
