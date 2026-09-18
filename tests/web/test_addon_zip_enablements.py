from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.addon_zip_enablements import (
    AddonZipEnablementService,
    AddonZipEnablementStatus,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.config import Settings
from kodi_lab.domain import (
    AddonZipEnablementOperationState,
    AddonZipEnablementResult,
    AddonZipPayloadVerificationOutcome,
)
from kodi_lab.web.app import create_app

from .conftest import (
    INSTANCE_ID,
    ORIGIN,
    PASSWORD,
    FakeBroker,
    coordinator_factory,
    login,
)

ARTIFACT_ID = UUID("e8260000-0000-4000-8000-000000000001")
OPERATION_ID = UUID("e8260000-0000-4000-8000-000000000002")
INSTALLATION_ID = UUID("e8260000-0000-4000-8000-000000000003")
AT = datetime.fromisoformat("2026-09-17T21:30:00+00:00")


class FakeZipEnablementService:
    def __init__(self, result: AddonZipEnablementResult) -> None:
        self.result = result
        self.calls: list[tuple[UUID, UUID]] = []

    def enable(self, instance_id: UUID, artifact_id: UUID) -> AddonZipEnablementStatus:
        self.calls.append((instance_id, artifact_id))
        already = self.result is AddonZipEnablementResult.ALREADY_ENABLED
        return AddonZipEnablementStatus(
            operation_id=OPERATION_ID,
            original_managed_instance_id=INSTANCE_ID,
            managed_instance_id=INSTANCE_ID,
            artifact_id=ARTIFACT_ID,
            installation_operation_id=INSTALLATION_ID,
            addon_id="plugin.video.uploaded",
            addon_version="1.2.3",
            state=AddonZipEnablementOperationState.SUCCEEDED,
            result=self.result,
            payload_verification=AddonZipPayloadVerificationOutcome.MATCHED,
            prior_enabled=already,
            result_enabled=True,
            enable_performed=not already,
            reconciliation_required=False,
            requested_at=AT,
            completed_at=AT,
        )


def client_for(
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
    service: FakeZipEnablementService,
) -> TestClient:
    auth = WebAuthenticationService(database)
    auth.set_password(PASSWORD)
    return TestClient(
        create_app(
            settings=Settings(data_root=data_root),
            session_factory=database,
            broker=broker,
            readiness=ReadinessTaskRegistry(max_workers=1),
            auth=auth,
            coordinator_factory=coordinator_factory,
            addon_zip_enablement_service=cast(AddonZipEnablementService, service),
        ),
        base_url="http://127.0.0.1",
    )


def test_endpoint_has_only_uuid_authority_and_strict_empty_body(
    database: sessionmaker[Session], broker: FakeBroker, data_root: Path
) -> None:
    service = FakeZipEnablementService(AddonZipEnablementResult.ENABLED)
    path = f"/api/instances/{INSTANCE_ID}/addon-zips/{ARTIFACT_ID}/enable"
    with client_for(database, broker, data_root, service) as client:
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
            {"enable": True},
            {"disable": True},
            {"toggle": True},
            {"install_dependencies": True},
            {"path": "/tmp/attacker"},
        ):
            assert (
                client.post(
                    path,
                    json=body,
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
    assert response.json()["result"] == "enabled"
    assert service.calls == [(INSTANCE_ID, ARTIFACT_ID)]
    for forbidden in (
        "path",
        "digest",
        "container",
        "endpoint",
        "repository_url",
        "raw_jsonrpc",
        "error text",
    ):
        assert forbidden not in response.text


def test_already_enabled_result_is_typed_and_reports_no_mutation(
    database: sessionmaker[Session], broker: FakeBroker, data_root: Path
) -> None:
    service = FakeZipEnablementService(AddonZipEnablementResult.ALREADY_ENABLED)
    path = f"/api/instances/{INSTANCE_ID}/addon-zips/{ARTIFACT_ID}/enable"
    with client_for(database, broker, data_root, service) as client:
        csrf = login(client)
        response = client.post(
            path,
            json={},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        )
    assert response.status_code == 200
    assert response.json()["result"] == "already_enabled"
    assert response.json()["enable_performed"] is False
