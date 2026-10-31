from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.application.mcp_administration import READ_ONLY_INSPECTION_PRESET_ID
from kodi_lab.application.production_inspection import PRODUCTION_INSPECTION_PRESET_ID
from kodi_lab.application.production_transient_approvals import ProductionApprovalRead
from kodi_lab.kodi.external import ExternalKodiProbeResult
from kodi_lab.kodi.remote import BrowserRemoteAction

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
        "bridge_trust_revision",
        "current_identity_generation",
        "current_bridge_trust_revision",
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


def test_screenshot_capability_amendment_is_explicit_web_only_and_closed(
    client: TestClient,
) -> None:
    csrf = login(client)
    created = client.post(
        "/api/production-targets",
        json=_registration(),
        headers=_headers(csrf),
    ).json()
    target_id = created["target_id"]
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
    client.post(
        f"/api/production-targets/{target_id}/confirm-native",
        json={"confirmed": True, "observation_id": observation_id},
        headers=_headers(csrf),
    )
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
    ).json()
    enabled = client.post(
        f"/api/production-targets/{target_id}/enabled",
        json={"enabled": True},
        headers=_headers(csrf),
    ).json()
    add_page = client.get("/production-targets")
    assert "Add screenshot to approved maximum" in add_page.text
    assert "does not establish screenshot authority" in add_page.text
    revision = enabled["production_policy"]["policy_revision"]
    payload = {
        "operation": "add",
        "capability": "screenshot",
        "expected_configured_capabilities": [
            "target_inspect",
            "addon_inventory",
        ],
        "expected_policy_revision": revision,
        "confirmed": True,
        "acknowledgement": (
            "I confirm this exact screenshot capability amendment and its displayed "
            "consequences."
        ),
    }
    url = f"/api/production-targets/{target_id}/capability-amendments"
    assert client.post(url, json=payload, headers=ORIGIN).status_code == 403
    assert (
        client.post(
            url,
            json=payload,
            headers={"Origin": "http://evil.invalid", "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 403
    )
    client.cookies.clear()
    assert client.post(url, json=payload, headers=_headers(csrf)).status_code == 401
    csrf = login(client)
    assert (
        client.post(
            url,
            json={**payload, "confirmed": False},
            headers=_headers(csrf),
        ).status_code
        == 422
    )
    for rejected in (
        "workspace_deployment",
        "fixed_execution",
        "semantic_remote",
        "arbitrary_capability",
    ):
        response = client.post(
            url,
            json={**payload, "capability": rejected},
            headers=_headers(csrf),
        )
        assert response.status_code == 422

    development_target = client.post(
        "/api/external-targets",
        json={**_registration(), "logical_name": "Development Kodi"},
        headers=_headers(csrf),
    ).json()["target_id"]
    assert (
        client.post(
            f"/api/production-targets/{development_target}/capability-amendments",
            json=payload,
            headers=_headers(csrf),
        ).status_code
        == 404
    )
    managed_target = client.get("/api/instances").json()[0]["managed_instance_id"]
    assert (
        client.post(
            f"/api/production-targets/{managed_target}/capability-amendments",
            json=payload,
            headers=_headers(csrf),
        ).status_code
        == 404
    )

    amended = client.post(url, json=payload, headers=_headers(csrf))
    assert amended.status_code == 201
    body = amended.json()
    assert body["policy_revision"] == revision + 1
    assert body["production_policy_revision_binding"] == revision + 1
    assert body["native_verified_capabilities"] == [
        "addon_inventory",
        "target_inspect",
    ]
    assert body["event_kind"] == "production_capability_screenshot_added"
    assert designated["production_policy"]["identity_generation"] == 1
    after = client.get(f"/api/production-targets/{target_id}").json()
    assert set(after["configured_capabilities"]) == {
        "addon_inventory",
        "screenshot",
        "target_inspect",
    }
    assert set(after["verified_capabilities"]) == {
        "addon_inventory",
        "target_inspect",
    }
    bridge = client.get(f"/api/production-targets/{target_id}/bridge").json()
    assert bridge["pairing_state"] == "unpaired"
    assert bridge["production_bridge_trust_revision"] is None
    assert bridge["verified_capabilities"] == []
    page = client.get("/production-targets")
    assert "operator-approved maximum" in page.text
    assert "cannot revive automatically" in page.text
    assert all(
        "capability-amendments" not in getattr(route, "path", "")
        for route in app.routes
        if getattr(route, "path", "").startswith("/mcp")
    )


def test_production_approval_queue_and_decisions_are_web_only_and_protected(
    client: TestClient,
) -> None:
    approval_id = UUID("53000000-0000-4000-8000-000000000001")
    now = datetime(2026, 9, 22, 12, tzinfo=UTC)
    record = ProductionApprovalRead(
        approval_id=approval_id,
        target_id=UUID("53000000-0000-4000-8000-000000000002"),
        target_name="Living room",
        client_id=UUID("53000000-0000-4000-8000-000000000003"),
        client_name="Production remote",
        request_id=UUID("53000000-0000-4000-8000-000000000004"),
        action=BrowserRemoteAction.PLAY_PAUSE,
        state="pending",
        terminal_reason=None,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        trust_summary="native 1; bridge 2; policy 3",
        global_mutations_blocked=False,
        target_mutations_suspended=False,
    )

    class ApprovalAdministration:
        def __init__(self) -> None:
            self.calls: list[tuple[str, BrowserRemoteAction | None]] = []

        def list_queue(self) -> list[ProductionApprovalRead]:
            return [record]

        def approve(
            self, _approval_id: UUID, *, action: BrowserRemoteAction
        ) -> ProductionApprovalRead:
            self.calls.append(("approve", action))
            return record.model_copy(update={"state": "approved"})

        def deny(self, _approval_id: UUID) -> ProductionApprovalRead:
            self.calls.append(("deny", None))
            return record.model_copy(
                update={"state": "denied", "terminal_reason": "administrator_denied"}
            )

        def cancel(self, _approval_id: UUID) -> ProductionApprovalRead:
            self.calls.append(("cancel", None))
            return record.model_copy(
                update={
                    "state": "cancelled",
                    "terminal_reason": "administrator_cancelled",
                }
            )

    app = cast(FastAPI, client.app)
    administration = ApprovalAdministration()
    object.__setattr__(
        app.state.kodi_lab, "production_transient_approvals", administration
    )
    queue_url = "/api/production-operation-approvals"
    decision_url = f"{queue_url}/{approval_id}"
    assert client.get(queue_url).status_code == 401
    csrf = login(client)
    assert (
        client.get(queue_url, headers={"Host": "attacker.invalid"}).status_code == 400
    )
    queue = client.get(queue_url)
    assert queue.status_code == 200
    assert queue.json()[0]["action"] == "play_pause"
    page = client.get("/production-targets").text
    primary = page.split('<details class="navigation-group"', 1)[0]
    assert f'data-approval-id="{approval_id}"' in primary
    assert 'data-approval-action-kind="approve"' in primary
    assert "data-global-block" in primary
    if evidence := os.getenv("KODI_VIEW_BROWSER_EVIDENCE"):
        root = Path(evidence)
        root.mkdir(parents=True, exist_ok=True)
        (root / "production-pending-approval.html").write_text(page)
    assert (
        client.post(
            f"{decision_url}/approve",
            json={"confirmed": True, "action": "play_pause"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{decision_url}/approve",
            json={"confirmed": False, "action": "play_pause"},
            headers=_headers(csrf),
        ).status_code
        == 422
    )
    approved = client.post(
        f"{decision_url}/approve",
        json={"confirmed": True, "action": "play_pause"},
        headers=_headers(csrf),
    )
    assert approved.status_code == 200
    assert approved.json()["action"] == "play_pause"
    assert (
        client.post(
            f"{decision_url}/deny",
            json={"confirmed": True},
            headers=_headers(csrf),
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"{decision_url}/cancel",
            json={"confirmed": True},
            headers=_headers(csrf),
        ).status_code
        == 200
    )
    assert administration.calls == [
        ("approve", BrowserRemoteAction.PLAY_PAUSE),
        ("deny", None),
        ("cancel", None),
    ]
