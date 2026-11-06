"""Version conflicts preserve protected lifecycle requests and actionable reasons."""

from typing import Never

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.errors import RemoteBrokerError
from kodi_lab.broker.managed_capacity import ManagedCapacityError
from kodi_lab.storage.models import ManagedInstanceLifecycleOperationRow
from tests.web.conftest import INSTANCE_ID, ORIGIN, FakeBroker, login


@pytest.mark.parametrize(
    ("code", "http_status"),
    [
        ("managed_version_slot_conflict", 409),
        ("managed_version_slot_pending", 409),
        ("managed_version_slot_identity_unavailable", 503),
    ],
)
def test_version_refusal_has_exact_reason_and_inspectable_history(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    http_status: int,
) -> None:
    csrf = login(client)
    message = ManagedCapacityError(code).slot_message
    assert message is not None

    def refuse(*, managed_instance_id: str) -> Never:
        assert managed_instance_id == str(INSTANCE_ID)
        raise RemoteBrokerError(code, message, request_id=None)

    monkeypatch.setattr(broker, "provision_managed_container", refuse)
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/provision",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == http_status
    assert response.json()["error"] == {"code": code, "message": message}
    assert broker.container is None
    assert not any(name in {"start", "stop", "deprovision"} for name, _ in broker.calls)
    with database() as session:
        operation = session.scalar(select(ManagedInstanceLifecycleOperationRow))
        assert operation is not None and operation.state == "failed"
        assert operation.original_managed_instance_id == str(INSTANCE_ID)
    assert client.get(f"/instances/{INSTANCE_ID}/history").status_code == 200
