"""The stable repository status is authenticated Web-only presentation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

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
