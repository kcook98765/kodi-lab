from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_enablements import (
    DevelopmentSourceEnablementService,
    DevelopmentSourceEnablementStatus,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.config import Settings
from kodi_lab.domain import (
    DevelopmentSourceEnablementOperationState,
    DevelopmentSourceEnablementResult,
)
from kodi_lab.kodi.addons import KodiAddonInventoryRecord, KodiAddonInventoryResult
from kodi_lab.kodi.readiness import KodiApplicationIdentity
from kodi_lab.storage.models import (
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    DevelopmentSourceEnablementOperationRow,
    RuntimeExecutionProfileRow,
)
from kodi_lab.web.app import create_app

from .conftest import (
    INSTANCE_ID,
    ORIGIN,
    PASSWORD,
    REVISION_ID,
    FakeAddonInventoryAdapter,
    FakeBroker,
    coordinator_factory,
    inventory_entry,
    login,
)
from .test_development_sources import SECRET_LOCATOR_A, SOURCE_A, _seed_sources

AT = "2026-09-17T16:00:00.000000Z"
DEPLOYMENT_OPERATION_ID = "dd220000-0000-4000-8000-000000000001"
ENABLEMENT_OPERATION_ID = "dd220000-0000-4000-8000-000000000002"
AT_DATETIME = datetime.fromisoformat(AT.replace("Z", "+00:00"))


class FakeEnablementService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []
        self.start_authority: list[bool] = []

    def enable(
        self,
        instance_id: object,
        source_id: object,
        *,
        allow_service_start: bool = False,
    ) -> DevelopmentSourceEnablementStatus:
        self.calls.append((instance_id, source_id))
        self.start_authority.append(allow_service_start)
        return DevelopmentSourceEnablementStatus(
            operation_id=UUID(ENABLEMENT_OPERATION_ID),
            original_managed_instance_id=INSTANCE_ID,
            managed_instance_id=INSTANCE_ID,
            development_source_id=SOURCE_A,
            addon_id="plugin.video.alpha",
            addon_version="1.2.3",
            state=DevelopmentSourceEnablementOperationState.SUCCEEDED,
            result_classification=DevelopmentSourceEnablementResult.ENABLED,
            prior_enabled=False,
            result_enabled=True,
            enable_performed=True,
            service_start_authorized=allow_service_start,
            reconciliation_required=False,
            requested_at=AT_DATETIME,
            completed_at=AT_DATETIME,
        )


def _seed_copied_source(database: sessionmaker[Session]) -> None:
    _seed_sources(database)
    with database.begin() as session:
        session.execute(
            update(RuntimeExecutionProfileRow).values(python_bytecode_policy="suppress")
        )
        session.add(
            DevelopmentSourceDeploymentOperationRow(
                id=DEPLOYMENT_OPERATION_ID,
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_A),
                source_logical_name="Alpha source",
                addon_id="plugin.video.alpha",
                runtime_revision_id=str(REVISION_ID),
                request_kind="copy_sync",
                requested_at=AT,
                state="succeeded",
                phase="finalize",
                result_classification="initial_deployment",
                new_count=1,
                changed_count=0,
                unchanged_count=0,
                destination_only_count=0,
                transferred_bytes=10,
                expected_container_id="a" * 64,
                started_at=AT,
                heartbeat_at=AT,
                completed_at=AT,
                reconciliation_required=False,
            )
        )
        session.add(
            DevelopmentSourceDeploymentRow(
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_A),
                addon_id="plugin.video.alpha",
                mode="copy",
                last_successful_operation_id=DEPLOYMENT_OPERATION_ID,
                source_manifest_digest="sha256:" + "1" * 64,
                verified_destination_digest="sha256:" + "2" * 64,
                runtime_revision_id=str(REVISION_ID),
                trusted_container_id="a" * 64,
                observed_source_name="Alpha Video",
                observed_source_version="1.2.3",
                verified_at=AT,
                confidence="verified_at_last_operation",
            )
        )


def _write_source(development_root: Path) -> None:
    source = development_root.joinpath(*SECRET_LOCATOR_A.split("/"))
    source.mkdir(parents=True)
    (source / "addon.xml").write_text(
        '<addon id="plugin.video.alpha" name="Alpha Video" version="1.2.3">'
        '<extension point="xbmc.python.pluginsource"/></addon>',
        encoding="utf-8",
    )


def _client(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    development_root: Path,
    service: FakeEnablementService,
) -> TestClient:
    auth = WebAuthenticationService(database)
    auth.set_password(PASSWORD)
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
            development_source_enablement_service=cast(
                DevelopmentSourceEnablementService, service
            ),
        ),
        base_url="http://127.0.0.1",
    )


def test_enable_endpoint_requires_auth_origin_csrf_and_strict_empty_body(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    tmp_path: Path,
) -> None:
    _seed_copied_source(database)
    development_root = tmp_path / "development"
    _write_source(development_root)
    service = FakeEnablementService()
    path = f"/api/instances/{INSTANCE_ID}/development-sources/{SOURCE_A}/enable"
    with _client(database, broker, data_root, development_root, service) as client:
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
        for forbidden_body in (
            {"addon_id": "plugin.video.attacker"},
            {"enabled": False},
            {"toggle": True},
            {"install_dependencies": True},
            {"repository_url": "https://attacker.invalid/repo"},
        ):
            assert (
                client.post(
                    path,
                    json=forbidden_body,
                    headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
                ).status_code
                == 422
            )
        response = client.post(
            path,
            json={},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        )
    assert response.status_code == 200
    assert response.json()["result_classification"] == "enabled"
    assert service.calls == [(INSTANCE_ID, SOURCE_A)]
    for forbidden in (
        SECRET_LOCATOR_A,
        "path",
        "digest",
        "endpoint",
        "repository",
        "raw_jsonrpc",
    ):
        assert forbidden not in response.text


def test_instance_ui_offers_enable_only_for_advisory_eligible_disabled_source(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    tmp_path: Path,
) -> None:
    _seed_copied_source(database)
    development_root = tmp_path / "development"
    _write_source(development_root)
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    broker.addon_inventory_result = KodiAddonInventoryResult(
        identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
        items=(
            KodiAddonInventoryRecord(
                addon_id="plugin.video.alpha",
                name="Alpha Video",
                version="1.2.3",
                enabled=False,
                kodi_type="xbmc.python.pluginsource",
            ),
        ),
    )
    service = FakeEnablementService()
    with _client(database, broker, data_root, development_root, service) as client:
        login(client)
        page = client.get(f"/instances/{INSTANCE_ID}")
    assert "Recognized, disabled" in page.text
    assert ">Enable<" in page.text
    assert "Installed" not in page.text
    for forbidden in (
        ">Disable<",
        ">Toggle<",
        ">Install<",
        "install_dependencies",
        "repository_url",
    ):
        assert forbidden not in page.text


@pytest.mark.parametrize(
    ("container_state", "enabled", "stale", "expected_enable", "expected_state"),
    [
        (
            ContainerRuntimeState.RUNNING,
            False,
            False,
            True,
            "Dependency blocked at last attempt",
        ),
        (ContainerRuntimeState.RUNNING, True, False, False, "Recognized, enabled"),
        (ContainerRuntimeState.STOPPED, False, False, False, "Copied, Kodi stopped"),
        (ContainerRuntimeState.RUNNING, False, True, False, "Provenance stale"),
    ],
)
def test_historical_dependency_failure_allows_fresh_preflight_only_when_eligible(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    tmp_path: Path,
    container_state: ContainerRuntimeState,
    enabled: bool,
    stale: bool,
    expected_enable: bool,
    expected_state: str,
) -> None:
    _seed_copied_source(database)
    development_root = tmp_path / "development"
    _write_source(development_root)
    with database.begin() as session:
        session.add(
            DevelopmentSourceEnablementOperationRow(
                id=ENABLEMENT_OPERATION_ID,
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                development_source_id=str(SOURCE_A),
                source_logical_name="Alpha source",
                addon_id="plugin.video.alpha",
                addon_version="1.2.3",
                runtime_revision_id=str(REVISION_ID),
                requested_at=AT,
                started_at=AT,
                completed_at=AT,
                state="failed",
                phase="finalize",
                result_classification="dependency_blocked",
                result_enabled=False,
                enable_performed=False,
                blocked_dependency_id="script.module.example",
                blocked_dependency_reason="dependency_missing",
                failure_reason="dependency_missing",
                reconciliation_required=False,
            )
        )
        if stale:
            session.execute(
                update(DevelopmentSourceDeploymentRow).values(confidence="stale")
            )
    broker.container = inventory_entry(container_state)
    broker.addon_inventory_result = KodiAddonInventoryResult(
        identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
        items=(
            KodiAddonInventoryRecord(
                "plugin.video.alpha",
                "Alpha Video",
                "1.2.3",
                enabled,
                "xbmc.python.pluginsource",
            ),
        ),
    )
    service = FakeEnablementService()
    with _client(database, broker, data_root, development_root, service) as client:
        login(client)
        page = client.get(f"/instances/{INSTANCE_ID}")
    assert expected_state in page.text
    assert ('class="development-source-enable"' in page.text) is expected_enable
    assert service.calls == []
    with database() as session:
        original = session.get(
            DevelopmentSourceEnablementOperationRow, ENABLEMENT_OPERATION_ID
        )
        assert original is not None
        assert original.state is DevelopmentSourceEnablementOperationState.FAILED
        assert original.failure_reason == "dependency_missing"


def test_service_start_opt_in_is_strict_and_authenticated(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    tmp_path: Path,
) -> None:
    _seed_copied_source(database)
    development_root = tmp_path / "development"
    _write_source(development_root)
    service = FakeEnablementService()
    path = f"/api/instances/{INSTANCE_ID}/development-sources/{SOURCE_A}/enable"
    with _client(database, broker, data_root, development_root, service) as client:
        assert client.post(path, json={"allow_service_start": True}).status_code == 401
        csrf = login(client)
        for invalid in (1, "true", None):
            response = client.post(
                path,
                json={"allow_service_start": invalid},
                headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
            )
            assert response.status_code == 422
        assert (
            client.post(
                path, json={"allow_service_start": True}, headers=ORIGIN
            ).status_code
            == 403
        )
        response = client.post(
            path,
            json={"allow_service_start": True},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        )
        assert response.status_code == 200
        assert response.json()["service_start_authorized"] is True
        assert response.json()["service_execution"] == "not_observed"
    assert service.start_authority == [True]


def test_service_enable_ui_requires_startup_choice_for_disabled_copied_service(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    tmp_path: Path,
) -> None:
    from sqlalchemy import update

    from kodi_lab.storage.models import DevelopmentSourceRow

    _seed_copied_source(database)
    development_root = tmp_path / "development"
    _write_source(development_root)
    with database.begin() as session:
        session.execute(
            update(DevelopmentSourceRow)
            .where(DevelopmentSourceRow.id == str(SOURCE_A))
            .values(extension_types_json='["xbmc.service"]')
        )
        session.execute(
            update(RuntimeExecutionProfileRow).values(python_bytecode_policy="suppress")
        )
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    broker.addon_inventory_result = KodiAddonInventoryResult(
        identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
        items=(
            KodiAddonInventoryRecord(
                addon_id="plugin.video.alpha",
                name="Alpha Video",
                version="1.2.3",
                enabled=False,
                kodi_type="xbmc.service",
            ),
        ),
    )
    with _client(
        database, broker, data_root, development_root, FakeEnablementService()
    ) as client:
        login(client)
        response = client.get(f"/instances/{INSTANCE_ID}")
    assert response.status_code == 200
    assert 'class="development-source-service-start"' in response.text
    assert "Enable service" in response.text
    assert "Check its logs to verify execution" in response.text
