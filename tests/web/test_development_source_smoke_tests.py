from __future__ import annotations

import time
from pathlib import Path
from typing import cast
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_smoke_tests import (
    DevelopmentSourceSmokeTestLease,
    DevelopmentSourceSmokeTestService,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.config import Settings
from kodi_lab.domain import DevelopmentSourceDeploymentConfidence
from kodi_lab.kodi.addons import KodiAddonInventoryRecord, KodiAddonInventoryResult
from kodi_lab.kodi.readiness import KodiApplicationIdentity
from kodi_lab.storage.models import DevelopmentSourceDeploymentRow
from kodi_lab.web.app import create_app

from .conftest import (
    INSTANCE_ID,
    ORIGIN,
    PASSWORD,
    FakeAddonInventoryAdapter,
    FakeBroker,
    coordinator_factory,
    inventory_entry,
    login,
)
from .test_development_source_enablements import _seed_copied_source
from .test_development_sources import SECRET_LOCATOR_A, SOURCE_A


class RecordingSmokeWorker:
    def __init__(
        self, calls: list[UUID], lease: DevelopmentSourceSmokeTestLease
    ) -> None:
        self._calls = calls
        self._lease = lease

    def run(self) -> None:
        self._calls.append(self._lease.operation_id)


def write_source(development_root: Path) -> None:
    source = development_root.joinpath(*SECRET_LOCATOR_A.split("/"))
    source.mkdir(parents=True)
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.alpha" name="Alpha Video" version="1.2.3">'
        '<extension point="xbmc.python.pluginsource" library="main.py">'
        "<provides>video</provides></extension>"
        '<extension point="xbmc.addon.metadata"/></addon>',
        encoding="utf-8",
    )
    (source / "main.py").write_text("pass\n", encoding="utf-8")


def smoke_client(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    development_root: Path,
    calls: list[UUID],
) -> TestClient:
    auth = WebAuthenticationService(database)
    auth.set_password(PASSWORD)

    def factory(
        lease: DevelopmentSourceSmokeTestLease,
    ) -> DevelopmentSourceSmokeTestService:
        return cast(
            DevelopmentSourceSmokeTestService, RecordingSmokeWorker(calls, lease)
        )

    return TestClient(
        create_app(
            settings=Settings(data_root=data_root, development_root=development_root),
            session_factory=database,
            broker=broker,
            readiness=ReadinessTaskRegistry(max_workers=1),
            auth=auth,
            coordinator_factory=coordinator_factory,
            addon_inventory_adapter_factory=(
                lambda _observation, _profile: FakeAddonInventoryAdapter(broker)
            ),
            development_source_smoke_test_service_factory=factory,
        ),
        base_url="http://127.0.0.1",
    )


def prepare(
    database: sessionmaker[Session], broker: FakeBroker, development_root: Path
) -> None:
    _seed_copied_source(database)
    write_source(development_root)
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    broker.addon_inventory_result = KodiAddonInventoryResult(
        identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
        items=(
            KodiAddonInventoryRecord(
                addon_id="plugin.video.alpha",
                name="Alpha Video",
                version="1.2.3",
                enabled=True,
                kodi_type="xbmc.python.pluginsource",
            ),
        ),
    )


def test_smoke_post_requires_auth_origin_csrf_and_strict_empty_body(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    tmp_path: Path,
) -> None:
    development_root = tmp_path / "development"
    prepare(database, broker, development_root)
    calls: list[UUID] = []
    path = f"/api/instances/{INSTANCE_ID}/development-sources/{SOURCE_A}/smoke-test"
    with smoke_client(database, broker, data_root, development_root, calls) as client:
        assert client.post(path, json={}).status_code == 401
        csrf = login(client)
        assert client.post(path, json={}, headers=ORIGIN).status_code == 403
        assert (
            client.post(
                path,
                json={},
                headers={
                    "Origin": "http://attacker.invalid",
                    "X-Kodi-Lab-CSRF": csrf,
                },
            ).status_code
            == 403
        )
        for body in (
            {"uri": "plugin://attacker/"},
            {"route": "secret"},
            {"query": "x=1"},
            {"timeout": 999},
            {"method": "System.Reboot"},
            {"media": "files"},
            {"properties": ["file"]},
        ):
            rejected = client.post(
                path,
                json=body,
                headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
            )
            assert rejected.status_code == 422
        accepted = client.post(
            path,
            json={},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        )
        assert accepted.status_code == 202
        document = accepted.json()
        assert set(document) == {"operation", "status_url"}
        status = client.get(document["status_url"])
        assert status.status_code == 200
        assert status.json()["state"] in {"queued", "running"}
        assert (
            client.get(
                document["status_url"].replace(str(INSTANCE_ID), str(UUID(int=999)))
            ).status_code
            == 404
        )
        for _ in range(100):
            if calls:
                break
            time.sleep(0.001)
    assert len(calls) == 1
    serialized = accepted.text + status.text
    for forbidden in (
        SECRET_LOCATOR_A,
        "plugin://",
        "returned_items",
        "labels",
        "artwork",
        "raw_json",
        "kodi_error_text",
        "digest",
        "container_id",
    ):
        assert forbidden not in serialized


def test_ui_shows_only_advisory_eligible_action_and_explicit_network_warning(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    tmp_path: Path,
) -> None:
    development_root = tmp_path / "development"
    prepare(database, broker, development_root)
    calls: list[UUID] = []
    with smoke_client(database, broker, data_root, development_root, calls) as client:
        login(client)
        page = client.get(f"/instances/{INSTANCE_ID}")
    assert ">Run smoke test<" in page.text
    assert "managed network is not egress-isolated" in page.text
    assert "inherits the instance's existing network access" in page.text
    assert "may contact external services" in page.text
    assert (
        "Smoke passed is bounded historical evidence that one fixed "
        "plugin-directory invocation completed and preserved deployed-source "
        "provenance. It is not a general working/health guarantee."
    ) in page.text
    assert ">Working<" not in page.text
    assert ">Fully tested<" not in page.text


def test_ui_hides_smoke_action_when_confidence_or_live_enablement_is_ineligible(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    tmp_path: Path,
) -> None:
    development_root = tmp_path / "development"
    prepare(database, broker, development_root)
    with database.begin() as session:
        session.execute(
            update(DevelopmentSourceDeploymentRow).values(
                confidence=DevelopmentSourceDeploymentConfidence.STALE
            )
        )
    calls: list[UUID] = []
    with smoke_client(database, broker, data_root, development_root, calls) as client:
        login(client)
        page = client.get(f"/instances/{INSTANCE_ID}")
    assert ">Run smoke test<" not in page.text
