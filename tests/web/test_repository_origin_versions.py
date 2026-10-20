"""Home installation instructions follow the verified active repository version."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.config import Settings
from kodi_lab.repository.stable import generate, sha256
from kodi_lab.web.app import create_app
from tests.repository.test_stable import BASE, candidate
from tests.web.conftest import FakeBroker, login
from tests.web.test_kodi_repository_page import HealthyRepository


def test_same_stale_literal_origin_reports_reachability_despite_internal_health(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = candidate(tmp_path)
    root = tmp_path / "repository"
    generate(bundle, root, BASE)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", BASE)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(root))
    monkeypatch.setattr("kodi_lab.web.app.httpx.AsyncClient", HealthyRepository)

    async def unreachable(_base: str, _checksum: str) -> str:
        assert _base == BASE
        return "unreachable"

    monkeypatch.setattr(
        "kodi_lab.repository.address.probe_advertised_origin", unreachable
    )
    login(client)
    page = client.get("/kodi-repository")
    assert page.status_code == 200
    assert "is unreachable from Lab" in page.text
    assert "DHCP reservation" in page.text
    assert "Repository address mismatch" not in page.text


def test_repository_and_add_kodi_show_current_versioned_bootstrap(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = candidate(tmp_path)
    root = tmp_path / "repository"
    generate(bundle, root, BASE, version="1.0.1")
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", BASE)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(root))
    monkeypatch.setenv(
        "KODI_LAB_REPOSITORY_BUNDLE_SHA256",
        sha256((bundle / "BUNDLE.json").read_bytes()),
    )
    monkeypatch.setattr("kodi_lab.web.app.httpx.AsyncClient", HealthyRepository)
    login(client)
    for route in ("/kodi-repository", "/add-kodi"):
        page = client.get(route)
        assert page.status_code == 200
        assert "repository.kodi-lab-1.0.1.zip" in page.text
        assert "repository.kodi-lab-1.0.0.zip" not in page.text
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", "http://192.168.50.11:18120")
    assert f"{BASE}/install/" not in client.get("/add-kodi").text
    assert "repository-source-url" not in client.get("/add-kodi").text


def test_pending_origin_migration_blocks_web_initialization(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository-pending"
    root.mkdir()
    (root / "ORIGIN-MIGRATION.json").write_text("pending")
    monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(root))
    with pytest.raises(ValueError, match="origin migration is incomplete"):
        create_app(
            settings=Settings(data_root=data_root),
            session_factory=database,
            broker=broker,
        )
