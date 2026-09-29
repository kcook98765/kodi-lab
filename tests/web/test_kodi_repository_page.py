"""The stable repository status is authenticated Web-only presentation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from kodi_lab.repository.stable import generate, sha256
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
