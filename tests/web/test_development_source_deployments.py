from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.config import Settings
from kodi_lab.web.app import create_app

from .conftest import (
    INSTANCE_ID,
    ORIGIN,
    PASSWORD,
    FakeBroker,
    coordinator_factory,
    inventory_entry,
    login,
)
from .test_development_sources import (
    SECRET_LOCATOR_A,
    SECRET_LOCATOR_B,
    SOURCE_A,
    _seed_sources,
)


def _write_source(root: Path, locator: str, addon_id: str, name: str) -> None:
    source = root.joinpath(*locator.split("/"))
    source.mkdir(parents=True)
    (source / "addon.xml").write_text(
        f'<addon id="{addon_id}" name="{name}" version="1.2.3">'
        '<extension point="xbmc.python.pluginsource"/></addon>'
    )


@pytest.fixture
def deployment_client(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    tmp_path: Path,
) -> Iterator[TestClient]:
    development_root = tmp_path / "development"
    _write_source(
        development_root, SECRET_LOCATOR_A, "plugin.video.alpha", "Alpha Video"
    )
    _write_source(development_root, SECRET_LOCATOR_B, "service.beta", "Beta Service")
    _seed_sources(database)
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED)
    auth = WebAuthenticationService(database)
    auth.set_password(PASSWORD)
    app = create_app(
        settings=Settings(
            data_root=data_root,
            development_root=development_root,
        ),
        session_factory=database,
        broker=broker,
        readiness=ReadinessTaskRegistry(max_workers=1),
        auth=auth,
        coordinator_factory=coordinator_factory,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def test_manual_copy_sync_requires_auth_origin_csrf_and_strict_empty_body(
    deployment_client: TestClient, broker: FakeBroker
) -> None:
    path = f"/api/instances/{INSTANCE_ID}/development-sources/{SOURCE_A}/copy-sync"
    assert deployment_client.post(path, json={}).status_code == 401
    csrf = login(deployment_client)
    assert deployment_client.post(path, json={}, headers=ORIGIN).status_code == 403
    assert (
        deployment_client.post(
            path,
            json={},
            headers={
                "Origin": "http://attacker.invalid",
                "X-Kodi-Lab-CSRF": csrf,
            },
        ).status_code
        == 403
    )
    assert (
        deployment_client.post(
            path,
            json={"destination": "/tmp/caller-selected"},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 422
    )

    response = deployment_client.post(
        path,
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 202
    document = response.json()
    assert set(document) == {"operation", "status_url"}
    operation_fields = set(document["operation"])
    assert (
        not {
            "destination",
            "container_id",
            "source_manifest_digest",
            "uid",
            "gid",
        }
        & operation_fields
    )
    serialized = response.text
    for forbidden in (SECRET_LOCATOR_A,):
        assert forbidden not in serialized

    status_document: dict[str, object] | None = None
    for _ in range(100):
        status = deployment_client.get(document["status_url"])
        assert status.status_code == 200
        status_document = status.json()
        if status_document["state"] == "succeeded":
            break
        time.sleep(0.01)
    assert status_document is not None
    assert status_document["state"] == "succeeded"
    assert status_document["result_classification"] == "initial_deployment"
    assert any(call[0] == "copy_sync_development_source" for call in broker.calls)

    page = deployment_client.get(f"/instances/{INSTANCE_ID}")
    assert "Last copy verified at" in page.text
    assert ">Copy/Sync<" in page.text
    assert SECRET_LOCATOR_A not in page.text


def test_running_instance_hides_control_and_post_revalidates_stopped_state(
    deployment_client: TestClient, broker: FakeBroker
) -> None:
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    csrf = login(deployment_client)
    page = deployment_client.get(f"/instances/{INSTANCE_ID}")
    assert ">Copy/Sync<" not in page.text
    path = f"/api/instances/{INSTANCE_ID}/development-sources/{SOURCE_A}/copy-sync"
    rejected = deployment_client.post(
        path,
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert rejected.status_code == 409
    assert not any(call[0] == "copy_sync_development_source" for call in broker.calls)
