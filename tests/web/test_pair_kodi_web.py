from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.external_onboarding import (
    ExternalKodiOnboardingError,
    ExternalKodiOnboardingResult,
)

from .conftest import ORIGIN, login

_WEB_ROOT = Path(__file__).resolve().parents[2] / "src" / "kodi_lab" / "web"


def _headers(csrf: str) -> dict[str, str]:
    return {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}


def _payload(**overrides: object) -> dict[str, object]:
    return {
        "name": "Living room Kodi",
        "address": "192.168.50.135",
        "code": "7KQ4",
        **overrides,
    }


def _result() -> ExternalKodiOnboardingResult:
    return ExternalKodiOnboardingResult(
        target_id=uuid4(),
        target_name="Living room Kodi",
        kodi_version="20.5",
        bridge_id=uuid4(),
        bridge_version="1.1.13",
        bridge_fingerprint="a" * 64,
    )


def test_pair_kodi_entry_and_simple_form(client: TestClient) -> None:
    assert client.get("/pair-kodi").status_code == 401
    login(client)
    development = client.get("/development")
    targets = client.get("/external-targets")
    page = client.get("/pair-kodi")
    assert page.status_code == 200
    assert 'href="/pair-kodi">Pair Kodi' in development.text
    assert 'href="/pair-kodi">Pair Kodi' in targets.text
    assert 'name="name"' in page.text
    assert 'name="address"' in page.text
    assert 'name="code"' in page.text
    assert 'pattern="[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}"' in page.text
    assert 'value="8080"' in page.text
    assert 'value="8765"' in page.text
    assert "Advanced ports" in page.text
    assert 'name="username"' in page.text
    assert 'name="password" type="password"' in page.text
    assert "Legacy pairing" in page.text
    assert "Paired" in page.text
    assert "external_development" not in page.text
    assert "production" not in page.text.lower()
    assert "long token" not in page.text.lower()
    assert "One-time pairing token" not in page.text


def test_pair_kodi_api_uses_only_application_service_and_redacts_secrets(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    expected = _result()

    def onboard(**payload: object) -> ExternalKodiOnboardingResult:
        calls.append(payload)
        return expected

    service = cast(FastAPI, client.app).state.kodi_lab.external_onboarding
    monkeypatch.setattr(service, "onboard", onboard)
    route = "/api/external-targets/onboard-otp"
    assert client.post(route, json=_payload()).status_code == 401
    csrf = login(client)
    assert client.post(route, json=_payload()).status_code == 403
    response = client.post(
        route,
        json=_payload(username="kodi", password="private-password"),
        headers=_headers(csrf),
    )
    assert response.status_code == 201
    assert response.json()["target_id"] == str(expected.target_id)
    assert calls == [
        {
            **_payload(username="kodi", password="private-password"),
            "kodi_port": 8080,
            "bridge_port": 8765,
        }
    ]
    assert "private-password" not in response.text
    assert "7KQ4" not in response.text
    assert "credential" not in response.text
    override = client.post(
        route,
        json=_payload(kodi_port=8081, bridge_port=8766),
        headers=_headers(csrf),
    )
    assert override.status_code == 201
    assert calls[-1]["kodi_port"] == 8081
    assert calls[-1]["bridge_port"] == 8766
    assert calls[-1]["username"] is None
    assert calls[-1]["password"] is None


@pytest.mark.parametrize("bad_code", ["0O1I", "AB3", "AB3EE", "ab3e"])
def test_pair_kodi_invalid_code_never_registers_target(
    client: TestClient, bad_code: str
) -> None:
    csrf = login(client)
    response = client.post(
        "/api/external-targets/onboard-otp",
        json=_payload(code=bad_code),
        headers=_headers(csrf),
    )
    assert response.status_code == 422
    assert client.get("/api/external-targets").json() == []


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("otp_invalid", "Code not accepted. Check the code shown in Kodi."),
        ("otp_expired", "Code expired. Generate a new code in Kodi."),
        (
            "otp_locked",
            "Pairing temporarily locked. Generate a new code in Kodi "
            "after the cooldown.",
        ),
        ("kodi_unreachable", "Kodi could not be reached at this address."),
        ("invalid_kodi_credentials", "Enter both the Kodi username and password."),
        (
            "bridge_unreachable",
            "Kodi was reached, but the Kodi Lab bridge could not be reached.",
        ),
        ("bridge_already_paired", "This Kodi Lab bridge is already paired."),
        (
            "bootstrap_version_unsupported",
            "Update the Kodi Lab bridge before using code pairing.",
        ),
        (
            "bridge_identity_mismatch",
            "The bridge identity changed. Pairing was stopped.",
        ),
        (
            "target_already_exists",
            "A Kodi with this name already exists. Choose another name.",
        ),
    ],
)
def test_pair_kodi_bounded_error_messages(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    message: str,
) -> None:
    service = cast(FastAPI, client.app).state.kodi_lab.external_onboarding

    def reject(**_payload: object) -> ExternalKodiOnboardingResult:
        raise ExternalKodiOnboardingError(code)

    monkeypatch.setattr(service, "onboard", reject)
    csrf = login(client)
    response = client.post(
        "/api/external-targets/onboard-otp",
        json=_payload(),
        headers=_headers(csrf),
    )
    assert response.json()["error"] == {"code": code, "message": message}
    assert "7KQ4" not in response.text


def test_pair_kodi_resume_is_csrf_protected_and_never_needs_code(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_id = uuid4()
    calls: list[object] = []
    service = cast(FastAPI, client.app).state.kodi_lab.external_onboarding

    def uncertain(**_payload: object) -> ExternalKodiOnboardingResult:
        raise ExternalKodiOnboardingError("pairing_uncertain", target_id=target_id)

    def resume(requested: object) -> ExternalKodiOnboardingResult:
        calls.append(requested)
        return _result()

    monkeypatch.setattr(service, "onboard", uncertain)
    monkeypatch.setattr(service, "resume", resume)
    csrf = login(client)
    failed = client.post(
        "/api/external-targets/onboard-otp",
        json=_payload(),
        headers=_headers(csrf),
    )
    assert failed.status_code == 409
    assert failed.json()["error"]["target_id"] == str(target_id)
    route = f"/api/external-targets/onboarding/{target_id}/resume"
    assert client.post(route).status_code == 403
    restored = client.post(route, headers=_headers(csrf))
    assert restored.status_code == 200
    assert restored.json()["paired"] is True
    assert calls == [target_id]
    assert "7KQ4" not in restored.text


def test_pair_kodi_browser_submits_once_and_only_renders_user_fields() -> None:
    script = (_WEB_ROOT / "static" / "pair-kodi.js").read_text()
    assert 'request("/api/external-targets/onboard-otp"' in script
    assert "if (busy) return" in script
    assert "request(`/api/external-targets/onboarding/${pending.id}/resume`" in script
    assert 'credentials: "same-origin"' in script
    assert '"X-Kodi-Lab-CSRF"' in script
    assert "response.json()" in script
    assert 'codeInput.value = ""' in script
    assert 'passwordInput.value = ""' in script
    assert "textContent = result.target_name" in script
    assert "textContent = result.kodi_version" in script
    assert "innerHTML" not in script
    assert "bootstrap_otp" not in script
    assert "console." not in script
    assert "sessionStorage.setItem(pendingKey, JSON.stringify(pending))" in script
    assert (
        "password:"
        not in script.split("sessionStorage.setItem", 1)[1].split("\n", 1)[0]
    )


def test_pair_kodi_browser_behavior_without_network_or_kodi() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for the dependency-free browser harness")
    subprocess.run(
        [
            node,
            str(Path(__file__).with_name("pair_kodi_browser_harness.js")),
            str(_WEB_ROOT / "static" / "pair-kodi.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
