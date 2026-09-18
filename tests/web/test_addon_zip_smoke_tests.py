from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_zip_smoke_tests import (
    AddonZipSmokeTestLease,
    AddonZipSmokeTestService,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.config import Settings
from kodi_lab.domain import AddonZipInstallationConfidence, PythonBytecodePolicy
from kodi_lab.kodi.addons import KodiAddonInventoryRecord, KodiAddonInventoryResult
from kodi_lab.kodi.readiness import KodiApplicationIdentity
from kodi_lab.storage.models import (
    AddonZipArtifactRow,
    AddonZipInstallationEvidenceRow,
    AddonZipInstallationOperationRow,
    AddonZipSmokeTestOperationRow,
    RuntimeExecutionProfileRow,
)
from kodi_lab.web.app import create_app

from .conftest import (
    CONTAINER_ID,
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

ARTIFACT_ID = UUID("f8270000-0000-4000-8000-000000000001")
INSTALL_ID = UUID("f8270000-0000-4000-8000-000000000002")
ADDON_ID = "plugin.video.uploaded"
AT = datetime.fromisoformat("2026-09-17T22:30:00+00:00")


class RecordingSmokeWorker:
    def __init__(self, calls: list[UUID], lease: AddonZipSmokeTestLease) -> None:
        self._calls = calls
        self._lease = lease

    def run(self) -> None:
        self._calls.append(self._lease.operation_id)


def seed_zip(database: sessionmaker[Session], broker: FakeBroker) -> None:
    with database.begin() as session:
        profile = session.get(RuntimeExecutionProfileRow, str(REVISION_ID))
        assert profile is not None
        profile.python_bytecode_policy = PythonBytecodePolicy.SUPPRESS
        session.add(
            AddonZipArtifactRow(
                id=str(ARTIFACT_ID),
                logical_label="Uploaded smoke",
                sha256_digest="sha256:" + "3" * 64,
                compressed_size=100,
                total_uncompressed_bytes=200,
                member_count=2,
                addon_id=ADDON_ID,
                addon_name="Uploaded smoke",
                addon_version="1.2.3",
                extension_classification="python_video_plugin",
                dependencies_json="[]",
                payload_manifest_digest="sha256:" + "4" * 64,
                imported_at=AT.isoformat(),
                origin="uploaded_zip",
                validation_state="accepted",
            )
        )
        session.flush()
        session.add(
            AddonZipInstallationOperationRow(
                id=str(INSTALL_ID),
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                artifact_id=str(ARTIFACT_ID),
                addon_id=ADDON_ID,
                addon_version="1.2.3",
                runtime_revision_id=str(REVISION_ID),
                requested_at=AT.isoformat(),
                state="succeeded",
                phase="finalize",
                expected_container_id=CONTAINER_ID,
                application_started=True,
                stopped_readback_matched=True,
                final_readback_matched=True,
                recognition_enabled=False,
                started_at=AT.isoformat(),
                heartbeat_at=AT.isoformat(),
                completed_at=AT.isoformat(),
                reconciliation_required=False,
            )
        )
        session.flush()
        session.add(
            AddonZipInstallationEvidenceRow(
                managed_instance_id=str(INSTANCE_ID),
                original_managed_instance_id=str(INSTANCE_ID),
                artifact_id=str(ARTIFACT_ID),
                installation_operation_id=str(INSTALL_ID),
                runtime_revision_id=str(REVISION_ID),
                trusted_container_id=CONTAINER_ID,
                addon_id=ADDON_ID,
                addon_version="1.2.3",
                payload_manifest_digest="sha256:" + "4" * 64,
                verified_at=AT.isoformat(),
                confidence="verified_at_last_operation",
                result="installed_recognized_disabled",
            )
        )
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    broker.addon_inventory_result = KodiAddonInventoryResult(
        identity=KodiApplicationIdentity("Kodi", 21, 3, "stable"),
        items=(
            KodiAddonInventoryRecord(
                addon_id=ADDON_ID,
                name="Uploaded smoke",
                version="1.2.3",
                enabled=True,
                kodi_type="xbmc.python.pluginsource",
            ),
        ),
    )


def smoke_client(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    calls: list[UUID],
) -> TestClient:
    auth = WebAuthenticationService(database)
    auth.set_password(PASSWORD)

    def factory(lease: AddonZipSmokeTestLease) -> AddonZipSmokeTestService:
        return cast(AddonZipSmokeTestService, RecordingSmokeWorker(calls, lease))

    return TestClient(
        create_app(
            settings=Settings(data_root=data_root),
            session_factory=database,
            broker=broker,
            readiness=ReadinessTaskRegistry(max_workers=1),
            auth=auth,
            coordinator_factory=coordinator_factory,
            addon_inventory_adapter_factory=(
                lambda _observation, _profile: FakeAddonInventoryAdapter(broker)
            ),
            addon_zip_smoke_test_service_factory=factory,
        ),
        base_url="http://127.0.0.1",
    )


def test_zip_smoke_post_requires_auth_origin_csrf_and_strict_empty_body(
    database: sessionmaker[Session], broker: FakeBroker, data_root: Path
) -> None:
    seed_zip(database, broker)
    calls: list[UUID] = []
    path = f"/api/instances/{INSTANCE_ID}/addon-zips/{ARTIFACT_ID}/smoke-test"
    with smoke_client(database, broker, data_root, calls) as client:
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
            {"addon_id": "plugin.video.attacker"},
            {"uri": "plugin://attacker/"},
            {"route": "secret"},
            {"timeout": 999},
            {"method": "System.Reboot"},
            {"media": "files"},
            {"properties": ["file"]},
        ):
            assert (
                client.post(
                    path,
                    json=body,
                    headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
                ).status_code
                == 422
            )
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
        "plugin://",
        "returned_items",
        "labels",
        "artwork",
        "raw_json",
        "kodi_error_text",
        "container_id",
        "payload_manifest_digest",
    ):
        assert forbidden not in serialized


def test_zip_smoke_ui_is_advisory_and_has_exact_network_warning(
    database: sessionmaker[Session], broker: FakeBroker, data_root: Path
) -> None:
    seed_zip(database, broker)
    with smoke_client(database, broker, data_root, []) as client:
        login(client)
        page = client.get(f"/instances/{INSTANCE_ID}")
    assert ">Run smoke test<" in page.text
    assert (
        "Plugin execution may contact external services; the managed runtime "
        "network is not egress-isolated."
    ) in page.text
    assert ">Working<" not in page.text
    assert ">Healthy<" not in page.text
    assert ">Fully tested<" not in page.text


def test_zip_smoke_ui_names_stale_installation_evidence(
    database: sessionmaker[Session], broker: FakeBroker, data_root: Path
) -> None:
    seed_zip(database, broker)
    with database.begin() as session:
        evidence = session.get(
            AddonZipInstallationEvidenceRow, (str(INSTANCE_ID), ADDON_ID)
        )
        assert evidence is not None
        evidence.confidence = AddonZipInstallationConfidence.STALE
        session.add(
            AddonZipSmokeTestOperationRow(
                id="f8270000-0000-4000-8000-000000000003",
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                artifact_id=str(ARTIFACT_ID),
                installation_operation_id=str(INSTALL_ID),
                addon_id=ADDON_ID,
                addon_version="1.2.3",
                runtime_revision_id=str(REVISION_ID),
                requested_at=AT.isoformat(),
                state="failed",
                phase="finalize",
                overall_result=None,
                execution_result="not_attempted",
                post_execution_provenance="not_checked",
                invocation_dispatched=False,
                completed_at=AT.isoformat(),
                failure_reason="zip_installation_evidence_stale",
                reconciliation_required=False,
            )
        )
    with smoke_client(database, broker, data_root, []) as client:
        login(client)
        page = client.get(f"/instances/{INSTANCE_ID}")
    assert "Installation evidence stale" in page.text
    assert ">Run smoke test<" not in page.text
