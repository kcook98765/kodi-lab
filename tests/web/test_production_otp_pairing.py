"""Production Web pairing against the current bridge's OTP-only endpoint."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.external_bridges import ExternalBridgeService
from kodi_lab.application.external_targets import ExternalTargetAdministrationService
from kodi_lab.kodi.bridge import ExternalBridgeClient, ExternalBridgeError
from kodi_lab.kodi.external import ExternalEndpointPolicy
from tests.kodi.test_bridge_otp_bootstrap import _server
from tests.kodi.test_narrow_bridge_addon import _load

from .conftest import ORIGIN, login
from .test_production_targets import SuccessfulProductionVerifier


def _headers(csrf: str) -> dict[str, str]:
    return {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}


def _ready_target(client: TestClient, csrf: str) -> str:
    headers = _headers(csrf)
    created = client.post(
        "/api/production-targets",
        json={
            "logical_name": "Disposable production Kodi",
            "protocol_kind": "jsonrpc_http",
            "host": "127.0.0.1",
            "port": 8080,
            "authentication_mode": "none",
            "configured_capabilities": ["target_inspect", "screenshot"],
        },
        headers=headers,
    )
    assert created.status_code == 201
    target_id = str(created.json()["target_id"])
    observed = client.post(
        f"/api/production-targets/{target_id}/probe", json={}, headers=headers
    )
    assert observed.status_code == 200
    observation_id = observed.json()["pending_observation_id"]
    assert observation_id
    native = client.post(
        f"/api/production-targets/{target_id}/confirm-native",
        json={"confirmed": True, "observation_id": observation_id},
        headers=headers,
    )
    assert native.status_code == 200
    designation = client.post(
        f"/api/production-targets/{target_id}/confirm-designation",
        json={
            "confirmed": True,
            "observation_id": observation_id,
            "acknowledgement": (
                "I confirm this is a production Kodi target and mutations may "
                "affect real user state."
            ),
        },
        headers=headers,
    )
    assert designation.status_code == 200
    enabled = client.post(
        f"/api/production-targets/{target_id}/enabled",
        json={"enabled": True},
        headers=headers,
    )
    assert enabled.status_code == 200
    return target_id


@pytest.fixture
def ready_web(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, dict[str, Any], int, str, str]]:
    bridge = _load(
        monkeypatch,
        state_path=tmp_path / "bridge-state.json",
        onboarding_path=tmp_path / "bridge-onboarding.json",
    )
    addon = SimpleNamespace(getSettingBool=lambda name: name == "enabled")
    monkeypatch.setattr(sys.modules["xbmcaddon"], "Addon", lambda _id: addon)
    bridge["_load_state"]()
    bridge["_onboarding_store"]().initialize(paired=False)
    app = cast(FastAPI, client.app)
    targets = cast(
        ExternalTargetAdministrationService, app.state.kodi_lab.production_targets
    )
    targets._verifier = SuccessfulProductionVerifier()  # type: ignore[assignment]
    targets._endpoint_policy = ExternalEndpointPolicy(
        allowed_ports=(8080,), test_loopback_enabled=True
    )
    bridges = cast(ExternalBridgeService, app.state.kodi_lab.external_bridges)
    bridges._client = ExternalBridgeClient()

    with _server(bridge) as port:
        bridges._endpoint_policy = ExternalEndpointPolicy(
            allowed_ports=(8765, port), test_loopback_enabled=True
        )
        monkeypatch.setattr(
            app.state.kodi_lab.settings, "external_bridge_allowed_ports", (8765, port)
        )
        csrf = login(client)
        target_id = _ready_target(client, csrf)
        headers = _headers(csrf)
        reviewed = client.post(
            "/api/production-bridge-builds",
            json={
                "semantic_fingerprint": bridge["BUILD_FINGERPRINT"],
                "zip_sha256": "a" * 64,
                "addon_id": "service.kodi_lab_bridge",
                "addon_version": "1.1.27",
                "protocol_version": "1",
                "capabilities": [
                    "screenshot",
                    "workspace_deployment",
                    "ui_observation",
                    "log_read",
                ],
                "supported_kodi_versions": ["21.3"],
                "source_reference": "controlled bridge 1.1.27 fixture",
            },
            headers=headers,
        )
        assert reviewed.status_code == 201
        acknowledged = client.post(
            f"/api/production-targets/{target_id}/bridge/http-acknowledgements",
            json={
                "confirmed": True,
                "bridge_port": port,
                "acknowledgement": (
                    "I acknowledge that private-LAN HTTP protects authenticity "
                    "and integrity but does not provide screenshot confidentiality."
                ),
            },
            headers=headers,
        )
        assert acknowledged.status_code == 201
        yield client, bridge, port, target_id, csrf


def _pair(client: TestClient, target_id: str, csrf: str, port: int, code: str) -> Any:
    return client.post(
        f"/api/production-targets/{target_id}/bridge/pair",
        json={"confirmed": True, "bridge_port": port, "code": code},
        headers=_headers(csrf),
    )


def test_reconnect_explains_stale_acknowledgement_without_consuming_code(
    ready_web: tuple[TestClient, dict[str, Any], int, str, str],
) -> None:
    client, bridge, port, target_id, csrf = ready_web
    original_code = bridge["_onboarding_store"]().status(paired=False)["code"]
    assert _pair(client, target_id, csrf, port, original_code).status_code == 200
    confirmed = client.post(
        f"/api/production-targets/{target_id}/bridge/confirm",
        json={
            "confirmed": True,
            "acknowledgement": (
                "I confirm this bridge is installed on the production Kodi target and "
                "the displayed bridge identity and package provenance match the "
                "reviewed package."
            ),
        },
        headers=_headers(csrf),
    )
    assert confirmed.status_code == 200
    revoked = client.post(
        f"/api/production-targets/{target_id}/bridge/revoke",
        json={"confirmed": True},
        headers=_headers(csrf),
    )
    assert revoked.status_code == 200
    # Simulate the operator's explicit Kodi-side reset, never an automatic retry.
    assert bridge["_reset_pairing"]() is True
    bridge["_onboarding_store"]().initialize(paired=False)
    bridge["_onboarding_store"]().generate_new_code(paired=False)
    code = bridge["_onboarding_store"]().status(paired=False)["code"]
    response = _pair(client, target_id, csrf, port, code)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "transport_acknowledgement_required"
    assert "current HTTP connection risks" in response.json()["error"]["message"]
    assert "code was not sent" in response.json()["error"]["message"]
    assert bridge["_onboarding_store"]().status(paired=False)["code"] == code
    assert (
        "First acknowledge the current HTTP risks"
        in client.get("/production-targets").text
    )
    assert f'<option value="{port}" selected>' in client.get("/production-targets").text
    renewed = client.post(
        f"/api/production-targets/{target_id}/bridge/http-acknowledgements",
        json={
            "confirmed": True,
            "bridge_port": port,
            "acknowledgement": (
                "I acknowledge that private-LAN HTTP protects authenticity "
                "and integrity but does not provide screenshot confidentiality."
            ),
        },
        headers=_headers(csrf),
    )
    assert renewed.status_code == 201
    assert _pair(client, target_id, csrf, port, code).status_code == 200


def test_fresh_production_web_pairing_uses_otp_not_rejected_pair_bridge(
    ready_web: tuple[TestClient, dict[str, Any], int, str, str],
) -> None:
    client, bridge, port, target_id, csrf = ready_web
    url = f"/api/production-targets/{target_id}/bridge/pair"
    code = bridge["_onboarding_store"]().status(paired=False)["code"]
    assert isinstance(code, str) and len(code) == 4
    assert (
        client.post(
            url, json={"confirmed": True, "bridge_port": port, "code": code}
        ).status_code
        == 403
    )
    assert (
        client.post(
            url,
            json={"confirmed": True, "bridge_port": port, "code": code},
            headers=ORIGIN,
        ).status_code
        == 403
    )
    assert (
        client.post(
            url,
            json={"confirmed": True, "bridge_port": port, "code": code},
            headers={"Origin": "http://untrusted.invalid", "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 403
    )
    malformed = _pair(client, target_id, csrf, port, "AB0D")
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "otp_invalid_format"
    pair = _pair(client, target_id, csrf, port, code)
    assert pair.status_code == 200, pair.json()
    assert pair.json()["pairing_state"] == "pairing_pending"
    assert pair.json()["observed_addon_version"] == "1.1.27"
    assert "credential" not in pair.text and code not in pair.text
    page = client.get("/production-targets")
    assert "Provisional bridge identity is recorded" in page.text
    assert "Confirm installed bridge identity" in page.text
    repeated = _pair(client, target_id, csrf, port, code)
    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "pairing_in_progress"
    confirmation = {
        "confirmed": True,
        "acknowledgement": (
            "I confirm this bridge is installed on the production Kodi target and "
            "the displayed bridge identity and package provenance match the "
            "reviewed package."
        ),
    }
    confirm_url = f"/api/production-targets/{target_id}/bridge/confirm"
    assert (
        client.post(
            confirm_url, json={"confirmed": True}, headers=_headers(csrf)
        ).status_code
        == 422
    )
    paired = client.post(confirm_url, json=confirmation, headers=_headers(csrf))
    assert paired.status_code == 200, paired.json()
    assert paired.json()["pairing_state"] == "paired"
    assert paired.json()["production_identity_generation"] == 1
    assert paired.json()["reviewed_build_id"]
    assert (
        client.post(confirm_url, json=confirmation, headers=_headers(csrf)).json()
        == paired.json()
    )
    assert (
        client.get(f"/api/production-targets/{target_id}").json()["target_class"]
        == "external_production"
    )
    assert (
        client.post(
            f"/api/production-targets/{target_id}/bridge/pairing-token",
            json={"confirmed": True, "bridge_port": port},
            headers=_headers(csrf),
        ).status_code
        == 404
    )
    client.cookies.clear()
    assert client.get(f"/api/production-targets/{target_id}/bridge").status_code == 401
    assert _pair(client, target_id, csrf, port, code).status_code == 401


def test_production_web_shows_wrong_code_lockout_and_expiry(
    ready_web: tuple[TestClient, dict[str, Any], int, str, str], tmp_path: Path
) -> None:
    client, bridge, port, target_id, csrf = ready_web
    code = bridge["_onboarding_store"]().status(paired=False)["code"]
    assert isinstance(code, str)
    wrong = "2222" if code != "2222" else "3333"
    for _ in range(5):
        response = _pair(client, target_id, csrf, port, wrong)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "wrong_code"
        assert code not in response.text
    locked = _pair(client, target_id, csrf, port, code)
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "pairing_temporarily_locked"
    assert "locked" in locked.json()["error"]["message"].lower()
    assert (
        client.get(f"/api/production-targets/{target_id}/bridge").json()[
            "pairing_state"
        ]
        == "unpaired"
    )
    state_path = tmp_path / "bridge-onboarding.json"
    state = json.loads(state_path.read_text())
    state["bridge_failures"] = []
    state["source_failures"] = {}
    state["failed_attempts"] = 0
    now = time.time()
    state["created_at"] = now - 301
    state["expires_at"] = now - 1
    state["last_generated_at"] = now - 301
    state_path.write_text(json.dumps(state))
    expired = _pair(client, target_id, csrf, port, code)
    assert expired.status_code == 410
    assert expired.json()["error"]["code"] == "pairing_code_expired"
    assert "expired" in expired.json()["error"]["message"].lower()


def test_production_web_refresh_recovers_lost_final_response(
    ready_web: tuple[TestClient, dict[str, Any], int, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, bridge, port, target_id, csrf = ready_web
    code = bridge["_onboarding_store"]().status(paired=False)["code"]
    assert isinstance(code, str)
    assert _pair(client, target_id, csrf, port, code).status_code == 200
    app = cast(FastAPI, client.app)
    service = cast(ExternalBridgeService, app.state.kodi_lab.external_bridges)
    original = service._client.commit_pairing
    calls = 0

    def lost(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        if calls == 1:
            raise ExternalBridgeError("bridge_unreachable")

    monkeypatch.setattr(service._client, "commit_pairing", lost)
    url = f"/api/production-targets/{target_id}/bridge/confirm"
    confirmation = {
        "confirmed": True,
        "acknowledgement": (
            "I confirm this bridge is installed on the production Kodi target and "
            "the displayed bridge identity and package provenance match the "
            "reviewed package."
        ),
    }
    uncertain = client.post(url, json=confirmation, headers=_headers(csrf))
    assert uncertain.status_code == 503
    assert uncertain.json()["error"]["code"] == "bridge_unreachable"
    assert (
        "Provisional bridge identity is recorded"
        in client.get("/production-targets").text
    )
    recovered = client.post(url, json=confirmation, headers=_headers(csrf))
    assert recovered.status_code == 200
    assert recovered.json()["pairing_state"] == "paired"
    assert calls == 2
    assert (
        client.post(url, json=confirmation, headers=_headers(csrf)).json()
        == recovered.json()
    )
