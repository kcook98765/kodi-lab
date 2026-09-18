from __future__ import annotations

import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_repositories import (
    AddonRepositoryAdministrationService,
    AddonRepositoryFetchCoordinator,
    AddonRepositoryRefreshCoordinator,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.config import Settings
from kodi_lab.development import RepositoryHttpResponse, addon_zip_artifact_directory
from kodi_lab.storage.repositories import AddonZipArtifactRepository
from kodi_lab.web.app import create_app

from .conftest import (
    INSTANCE_ID,
    ORIGIN,
    PASSWORD,
    FakeAddonInventoryAdapter,
    FakeBroker,
    coordinator_factory,
    login,
)


def _zip(addon_id: str = "plugin.video.repositorytest") -> bytes:
    output = BytesIO()
    descriptor = (
        f'<addon id="{addon_id}" name="Repository Test" version="1.2.3">'
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension>"
        '<extension point="xbmc.addon.metadata" /></addon>'
    )
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{addon_id}/addon.xml", descriptor)
        archive.writestr(f"{addon_id}/default.py", "pass\n")
    return output.getvalue()


class FakeTransport:
    def get(
        self, url: str, *, maximum_body: int, require_index: bool = True
    ) -> RepositoryHttpResponse:
        del maximum_body
        if require_index:
            body = (
                b'<addons><addon id="plugin.video.repositorytest" '
                b'name="Repository Test" version="1.2.3">'
                b'<extension point="xbmc.python.pluginsource" library="default.py">'
                b"<provides>video</provides></extension>"
                b'<extension point="xbmc.addon.metadata" /></addon></addons>'
            )
            return RepositoryHttpResponse(200, "application/xml", body, url)
        return RepositoryHttpResponse(200, "application/zip", _zip(), url)


class MismatchTransport(FakeTransport):
    def get(
        self, url: str, *, maximum_body: int, require_index: bool = True
    ) -> RepositoryHttpResponse:
        if require_index:
            return super().get(
                url, maximum_body=maximum_body, require_index=require_index
            )
        return RepositoryHttpResponse(
            200, "application/zip", _zip("plugin.video.wrong"), url
        )


def _wait(client: TestClient, url: str) -> dict[str, object]:
    for _ in range(100):
        response = client.get(url)
        assert response.status_code == 200
        value = cast(dict[str, object], response.json())
        if value["state"] in {"succeeded", "failed"}:
            return value
        time.sleep(0.01)
    raise AssertionError("repository operation did not terminalize")


def test_repository_api_is_closed_private_and_keeps_uploaded_zip_authority_separate(
    database: sessionmaker[Session], broker: FakeBroker, data_root: Path
) -> None:
    data_root.mkdir(mode=0o700)
    auth = WebAuthenticationService(database)
    auth.set_password(PASSWORD)
    transport = FakeTransport()
    refreshes = AddonRepositoryRefreshCoordinator(database, transport=transport)
    fetches = AddonRepositoryFetchCoordinator(
        database, data_root=data_root, transport=transport
    )
    app = create_app(
        settings=Settings(
            data_root=data_root,
            development_root=data_root.parent / "development-root",
        ),
        session_factory=database,
        broker=broker,
        readiness=ReadinessTaskRegistry(max_workers=1),
        auth=auth,
        coordinator_factory=coordinator_factory,
        addon_inventory_adapter_factory=(
            lambda _observation, _profile: FakeAddonInventoryAdapter(broker)
        ),
        addon_repository_refresh_coordinator=refreshes,
        addon_repository_fetch_coordinator=fetches,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        csrf = login(client)
        headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
        create = client.post(
            "/api/addon-repositories",
            json={
                "logical_name": "Safe Repository",
                "index_url": "https://EXAMPLE.com/kodi/addons.xml",
            },
            headers=headers,
        )
        assert create.status_code == 201
        repository = create.json()
        assert repository["canonical_index_url"] == (
            "https://example.com/kodi/addons.xml"
        )
        repository_id = repository["repository_id"]
        assert "digest" not in create.text
        assert "headers" not in create.text

        strict = client.post(
            "/api/addon-repositories",
            json={
                "logical_name": "No",
                "index_url": "https://example.com/addons.xml",
                "force": True,
            },
            headers=headers,
        )
        assert strict.status_code == 422
        assert (
            client.post(
                f"/api/instances/{INSTANCE_ID}/addon-repositories/{repository_id}/attach",
                json={"force": True},
                headers=headers,
            ).status_code
            == 422
        )
        attached = client.post(
            f"/api/instances/{INSTANCE_ID}/addon-repositories/{repository_id}/attach",
            json={},
            headers=headers,
        )
        assert attached.status_code == 200

        accepted = client.post(
            f"/api/addon-repositories/{repository_id}/refresh",
            json={},
            headers=headers,
        )
        assert accepted.status_code == 202
        refreshed = _wait(client, accepted.json()["status_url"])
        assert refreshed["state"] == "succeeded"
        candidates = client.get(
            f"/api/addon-repositories/{repository_id}/candidates"
        ).json()
        assert len(candidates) == 1
        candidate_id = candidates[0]["candidate_id"]
        assert "package" not in str(candidates).lower()
        assert "digest" not in str(candidates).lower()

        accepted = client.post(
            f"/api/addon-repositories/{repository_id}/candidates/{candidate_id}/fetch",
            json={},
            headers=headers,
        )
        assert accepted.status_code == 202
        fetched = _wait(client, accepted.json()["status_url"])
        assert fetched["state"] == "succeeded"
        artifact_id = fetched["artifact_id"]
        assert client.get("/api/addon-zips").json() == []
        generic = client.post(
            f"/api/instances/{INSTANCE_ID}/addon-zips/{artifact_id}/install",
            json={},
            headers=headers,
        )
        assert generic.status_code == 409
        install = client.post(
            f"/api/instances/{INSTANCE_ID}/repository-artifacts/{artifact_id}/install",
            json={},
            headers=headers,
        )
        assert install.status_code == 202


def test_repository_registration_requires_auth_origin_csrf_and_url_policy(
    client: TestClient,
) -> None:
    path = "/api/addon-repositories"
    body = {
        "logical_name": "Repository",
        "index_url": "https://example.com/addons.xml",
    }
    assert client.post(path, json=body).status_code == 401
    csrf = login(client)
    assert client.post(path, json=body, headers=ORIGIN).status_code == 403
    assert (
        client.post(
            path,
            json=body,
            headers={
                "Origin": "http://attacker.invalid",
                "X-Kodi-Lab-CSRF": csrf,
            },
        ).status_code
        == 403
    )
    invalid = client.post(
        path,
        json={"logical_name": "Repository", "index_url": "http://example.com/a"},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "repository_https_required"


def test_rejected_package_publishes_no_partial_artifact(
    database: sessionmaker[Session], data_root: Path
) -> None:
    data_root.mkdir(mode=0o700)
    administration = AddonRepositoryAdministrationService(database)
    repository = administration.register(
        "Mismatch repository", "https://example.com/addons.xml"
    )
    refreshes = AddonRepositoryRefreshCoordinator(database, transport=FakeTransport())
    queued = refreshes.queue(repository.repository_id)
    refresh_lease = refreshes.acquire(queued.operation_id)
    assert refresh_lease is not None
    refreshes.run(refresh_lease)
    candidate = refreshes.candidates(repository.repository_id)[0]

    fetches = AddonRepositoryFetchCoordinator(
        database, data_root=data_root, transport=MismatchTransport()
    )
    queued_fetch = fetches.queue(repository.repository_id, candidate.candidate_id)
    fetch_lease = fetches.acquire(queued_fetch.operation_id)
    assert fetch_lease is not None
    fetches.run(fetch_lease)
    terminal = fetches.get(queued_fetch.operation_id)
    assert terminal.state.value == "failed"
    assert terminal.failure_reason == "repository_package_descriptor_mismatch"
    with database() as session:
        assert AddonZipArtifactRepository(session).get(terminal.artifact_id) is None
    assert not addon_zip_artifact_directory(data_root, terminal.artifact_id).exists()
