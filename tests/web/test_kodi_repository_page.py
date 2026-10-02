"""The stable repository status is authenticated Web-only presentation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.application.mcp_workspace_development import McpWorkspaceDevelopmentError
from kodi_lab.repository.development import initialize as initialize_development
from kodi_lab.repository.stable import generate, sha256
from tests.repository.test_development import fixture_zip, phase2_candidate
from tests.repository.test_stable import BASE, candidate
from tests.web.conftest import login


class HealthyRepository:
    def __init__(self, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> HealthyRepository:
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    async def get(self, url: str) -> SimpleNamespace:
        assert url == "http://repository:8110/health"
        return SimpleNamespace(status_code=200, content=b"ok\n")


def test_repository_page_requires_login_and_shows_verified_install(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.get("/kodi-repository", follow_redirects=False).status_code == 303
    bundle = candidate(tmp_path)
    data = tmp_path / "repository-data"
    provenance = generate(bundle, data, BASE)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", BASE)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(data))
    monkeypatch.setenv(
        "KODI_LAB_REPOSITORY_BUNDLE_SHA256",
        sha256((bundle / "BUNDLE.json").read_bytes()),
    )
    monkeypatch.setattr("kodi_lab.web.app.httpx.AsyncClient", HealthyRepository)
    login(client)
    page = client.get("/kodi-repository")
    assert page.status_code == 200
    assert "Serving" in page.text
    assert BASE + "/install/" in page.text
    assert str(provenance["bootstrap_zip_sha256"]) in page.text
    assert "service.kodi_lab_bridge" in page.text
    assert "malicious LAN actor" in page.text


@pytest.fixture
def development_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bundle = phase2_candidate(tmp_path)
    data = tmp_path / "development-repository-data"
    generate(bundle, data, BASE)
    initialize_development(data / "development-data", BASE, bundle)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", BASE)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(data))
    monkeypatch.setenv(
        "KODI_LAB_REPOSITORY_BUNDLE_SHA256",
        sha256((bundle / "BUNDLE.json").read_bytes()),
    )
    return data


def test_authenticated_import_review_and_explicit_approval(
    development_environment: Path,
    client: TestClient,
) -> None:
    body = fixture_zip("1.0.0")
    url = "/api/kodi-repository/development/import"
    assert (
        client.post(
            url, content=body, headers={"Content-Type": "application/zip"}
        ).status_code
        == 401
    )
    token = login(client)
    response = client.post(
        url,
        content=body,
        headers={
            "Origin": "http://127.0.0.1",
            "X-Kodi-Lab-CSRF": token,
            "X-Kodi-Lab-Compatibility": "matrix,nexus,omega",
            "Content-Type": "application/zip",
        },
    )
    assert response.status_code == 200, response.text
    prepared = response.json()
    assert prepared["status"] == "prepared"
    assert prepared["zip_sha256"] == sha256(body)
    page = client.get("/kodi-repository")
    assert prepared["addon_id"] in page.text
    assert prepared["zip_sha256"] in page.text
    approval_url = (
        f"/api/kodi-repository/development/{prepared['publication_id']}/approve"
    )
    headers = {"Origin": "http://127.0.0.1", "X-Kodi-Lab-CSRF": token}
    payload = {
        "addon_id": prepared["addon_id"],
        "version": prepared["version"],
        "zip_sha256": "0" * 64,
    }
    assert client.post(approval_url, json=payload, headers=headers).status_code == 409
    payload["zip_sha256"] = prepared["zip_sha256"]
    approved = client.post(approval_url, json=payload, headers=headers)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "current"
    assert (
        b"plugin.video.kodi-lab-phase2-synthetic"
        in (
            development_environment
            / "development-data"
            / "generations"
            / approved.json()["feed_generation"]
            / "development/addons.xml"
        ).read_bytes()
    )


def test_workspace_repository_approval_route_requires_exact_authenticated_values(
    development_environment: Path,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = uuid4()
    operation_id = uuid4()
    target_id = uuid4()
    endpoint = (
        f"/api/workspaces/{workspace_id}/repository-deployments/{operation_id}/approve"
    )
    payload = {
        "target_id": str(target_id),
        "generation": 2,
        "addon_id": "plugin.video.synthetic",
        "addon_version": "1.0.1",
        "zip_sha256": "a" * 64,
        "expected_manifest_digest": "sha256:" + "b" * 64,
    }
    assert client.post(endpoint, json=payload).status_code == 401
    token = login(client)
    assert client.post(endpoint, json=payload).status_code == 403
    headers = {"Origin": "http://127.0.0.1", "X-Kodi-Lab-CSRF": token}
    assert (
        client.post(
            endpoint, json={**payload, "generation": "2"}, headers=headers
        ).status_code
        == 400
    )
    observed: list[object] = []

    def approved(observed_operation_id: object, **values: object) -> SimpleNamespace:
        observed.append((observed_operation_id, values))
        return SimpleNamespace(model_dump=lambda **_kwargs: {"state": "running"})

    monkeypatch.setattr(
        cast(FastAPI, client.app).state.kodi_lab.mcp_workspace_development,
        "approve_repository_deployment",
        approved,
    )
    result = client.post(endpoint, json=payload, headers=headers)
    assert result.status_code == 200 and result.json() == {"state": "running"}
    assert observed == [
        (
            operation_id,
            {
                "workspace_id": workspace_id,
                "target_id": target_id,
                "generation": 2,
                "addon_id": payload["addon_id"],
                "addon_version": payload["addon_version"],
                "zip_sha256": payload["zip_sha256"],
                "expected_manifest_digest": payload["expected_manifest_digest"],
            },
        )
    ]


def test_existing_install_verification_requires_login_csrf_and_exact_confirmation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_id = uuid4()
    publication_id = uuid4()
    endpoint = (
        f"/api/external-targets/{target_id}/development-repository/"
        f"publications/{publication_id}/verify-existing"
    )
    payload = {
        "target_id": str(target_id),
        "publication_id": str(publication_id),
        "addon_id": "plugin.video.synthetic",
        "addon_version": "1.0.1",
        "zip_sha256": "a" * 64,
        "expected_manifest_digest": "sha256:" + "b" * 64,
        "confirmed_existing": True,
    }
    assert client.post(endpoint, json=payload).status_code == 401
    token = login(client)
    assert client.post(endpoint, json=payload).status_code == 403
    headers = {"Origin": "http://127.0.0.1", "X-Kodi-Lab-CSRF": token}
    for stale in (
        {**payload, "target_id": str(uuid4())},
        {**payload, "publication_id": str(uuid4())},
        {**payload, "confirmed_existing": False},
        {**payload, "extra": "unexpected"},
    ):
        assert client.post(endpoint, json=stale, headers=headers).status_code == 400
    observed: list[object] = []

    def verified(**values: object) -> SimpleNamespace:
        observed.append(values)
        return SimpleNamespace(
            model_dump=lambda **_kwargs: {"origin": "verified_existing"}
        )

    monkeypatch.setattr(
        cast(FastAPI, client.app).state.kodi_lab.mcp_workspace_development,
        "verify_existing_repository_installation",
        verified,
    )
    result = client.post(endpoint, json=payload, headers=headers)
    assert result.status_code == 200 and result.json() == {
        "origin": "verified_existing"
    }
    assert observed == [
        {
            "target_id": target_id,
            "publication_id": publication_id,
            "addon_id": payload["addon_id"],
            "addon_version": payload["addon_version"],
            "zip_sha256": payload["zip_sha256"],
            "expected_manifest_digest": payload["expected_manifest_digest"],
        }
    ]

    def rejected(**_values: object) -> None:
        raise McpWorkspaceDevelopmentError("adoption_installed_state_mismatch")

    monkeypatch.setattr(
        cast(FastAPI, client.app).state.kodi_lab.mcp_workspace_development,
        "verify_existing_repository_installation",
        rejected,
    )
    rejected_response = client.post(endpoint, json=payload, headers=headers)
    assert rejected_response.status_code == 409
    assert rejected_response.json()["detail"] == "adoption_installed_state_mismatch"
