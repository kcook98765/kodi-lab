"""Exact action consent replaces the view preference, never administrator authority."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_installations import preview_acknowledgement
from kodi_lab.broker.operations import ReviewedInstallationLaunchResult
from kodi_lab.host_bootstrap import generate_master_key
from kodi_lab.runtime.preview import PreviewAcknowledgement, recorded_acknowledgement
from tests.runtime.test_piers_display import ready_display
from tests.web.conftest import ORIGIN, FakeBroker, login
from tests.web.test_runtime_installations import Flow

PATHS = (
    "/api/runtime-installations/piers",
    "/api/runtime-installations/piers/instances",
)


def approval(action: str) -> dict[str, object]:
    return {
        "action": action,
        "approved": True,
        "acknowledgement": preview_acknowledgement(),
    }


def preview_flow(broker: FakeBroker) -> Flow:
    flow = Flow()
    flow.attach(broker)
    dynamic: Any = broker

    def launch(**kwargs: Any) -> ReviewedInstallationLaunchResult:
        assert (
            kwargs["preview_acknowledgement"].model_dump(mode="json")
            == preview_acknowledgement()
        )
        flow.operation_id, flow.generation = (
            kwargs["operation_id"],
            kwargs["generation"],
        )
        flow.launches.append((flow.generation, flow.operation_id))
        return ReviewedInstallationLaunchResult(
            operation_id=flow.operation_id, generation="piers", newly_accepted=True
        )

    dynamic.launch_reviewed_runtime_installation = launch
    return flow


@pytest.mark.parametrize("mode", ["standard", "development"])
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "flat",
        "false",
        "integer",
        "string",
        "extra",
        "release",
        "digest",
        "revision",
        "unsupported_recipe",
        "old_recipe",
        "tier",
        "nested_extra",
        "wrong_action",
        "unknown_action",
        "malformed",
    ],
)
def test_invalid_or_reused_action_consent_is_denied(
    client: TestClient, broker: FakeBroker, mode: str, path: str, case: str
) -> None:
    from kodi_lab.runtime.recipes import PREVIEW_INSTALLATION_DIGEST

    flow = preview_flow(broker)
    token = login(client)
    client.cookies.set("kodi_lab_admin_view", mode)
    action = "create" if path.endswith("/instances") else "install"
    body: dict[str, Any] = deepcopy(approval(action))
    expected = 422
    if case == "missing":
        body = {}
    elif case == "flat":
        body = preview_acknowledgement()
    elif case == "false":
        body["approved"] = False
    elif case == "integer":
        body["approved"] = 1
    elif case == "string":
        body["approved"] = "true"
    elif case == "extra":
        body["enable_preview"] = True
    elif case == "release":
        body["acknowledgement"]["release"] = "22.0"
    elif case == "digest":
        body["acknowledgement"]["recipe_digest"] = "sha256:" + "f" * 64
    elif case == "revision":
        body["acknowledgement"]["recipe_revision"] = True
    elif case == "unsupported_recipe":
        body["acknowledgement"]["recipe_id"] = "piers-unreviewed"
    elif case == "tier":
        body["acknowledgement"]["support_tier"] = "stable"
    elif case == "nested_extra":
        body["acknowledgement"]["authorize_all_actions"] = True
    elif case == "unknown_action":
        body["action"] = "enable"
    elif case == "old_recipe":
        body["acknowledgement"].update(
            recipe_id="piers-22.0-rc1-r2",
            recipe_revision=2,
            recipe_digest=PREVIEW_INSTALLATION_DIGEST,
        )
        expected = 409
    elif case == "wrong_action":
        body["action"] = "install" if action == "create" else "create"
        expected = 409
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": token}
    result = (
        client.post(
            path, headers={**headers, "Content-Type": "application/json"}, content="{"
        )
        if case == "malformed"
        else client.post(path, headers=headers, json=body)
    )
    assert result.status_code == expected
    assert not flow.launches


@pytest.mark.parametrize("mode", ["standard", "development"])
@pytest.mark.parametrize("path", PATHS)
def test_view_cookie_never_supplies_authentication_or_csrf(
    client: TestClient, broker: FakeBroker, mode: str, path: str
) -> None:
    flow = preview_flow(broker)
    body = approval("create" if path.endswith("/instances") else "install")
    client.cookies.set("kodi_lab_admin_view", mode)
    assert client.post(path, json=body, headers=ORIGIN).status_code == 401
    token = login(client)
    assert client.post(path, json=body, headers=ORIGIN).status_code == 403
    assert (
        client.post(
            path,
            json=body,
            headers={
                **ORIGIN,
                "X-Kodi-Lab-CSRF": token,
                "Origin": "https://evil.invalid",
            },
        ).status_code
        == 403
    )
    assert not flow.launches


@pytest.mark.parametrize("mode", ["standard", "development"])
def test_exact_install_consent_does_not_authorize_a_later_action(
    client: TestClient, database: sessionmaker[Session], broker: FakeBroker, mode: str
) -> None:
    flow = preview_flow(broker)
    token = login(client)
    client.cookies.set("kodi_lab_admin_view", mode)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": token}
    body = approval("install")
    result = client.post(PATHS[0], json=body, headers=headers)
    assert result.status_code == 202
    operation = UUID(result.json()["operation_id"])
    assert recorded_acknowledgement(
        database, operation
    ) == PreviewAcknowledgement.model_validate(preview_acknowledgement())
    assert client.post(PATHS[1], json=body, headers=headers).status_code == 409
    assert client.post(PATHS[0], json={}, headers=headers).status_code == 422
    assert client.post(PATHS[0], json=body, headers=headers).status_code == 409
    for url in ("/runtimes", "/api/runtime-installations", result.json()["status_url"]):
        observed = client.get(url)
        assert observed.status_code == 200
        assert str(operation) in observed.text
    assert len(flow.launches) == 1


@pytest.mark.parametrize("mode", ["standard", "development"])
def test_create_needs_its_own_consent_and_real_sealed_eligibility(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    mode: str,
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root.mkdir(mode=0o700, exist_ok=True)
    data_root.chmod(0o700)
    generate_master_key(data_root)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(data_root))
    preview_flow(broker)
    token = login(client)
    client.cookies.set("kodi_lab_admin_view", mode)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": token}
    assert (
        client.post(PATHS[1], json=approval("create"), headers=headers).status_code
        == 409
    )
    ready_display(
        database
    )  # Existing signed/validated test evidence; no image acquisition.
    assert (
        client.post(PATHS[1], json=approval("install"), headers=headers).status_code
        == 409
    )
    result = client.post(PATHS[1], json=approval("create"), headers=headers)
    assert result.status_code == 200
    assert result.json()["action"] == "open"
    assert client.post(PATHS[1], json={}, headers=headers).status_code == 422
