from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from .conftest import INSTANCE_ID, ORIGIN, login

ARTIFACT_ID = UUID("00000000-0000-4000-8000-000000000211")
PLAN_ID = UUID("00000000-0000-4000-8000-000000000212")


def test_replacement_prepare_requires_auth_origin_csrf_and_empty_body(
    client: TestClient,
) -> None:
    path = (
        f"/api/instances/{INSTANCE_ID}/repository-artifacts/{ARTIFACT_ID}/"
        "replacement-plans"
    )
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
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    for field in (
        "force",
        "destination",
        "source",
        "incumbent_class",
        "addon_id",
        "version",
        "snapshot_id",
        "container_id",
        "overwrite",
        "delete",
    ):
        response = client.post(path, json={field: True}, headers=headers)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "request_validation_failed"
        assert field not in response.text
    unavailable = client.post(path, json={}, headers=headers)
    assert unavailable.status_code == 422
    assert "path" not in unavailable.text.lower()
    assert "digest" not in unavailable.text.lower()


@pytest.mark.parametrize(
    "body",
    [{}, {"confirmed": False}, {"confirmed": True, "force": True}],
)
def test_approve_and_execute_requires_exact_true_confirmation(
    client: TestClient, body: dict[str, object]
) -> None:
    csrf = login(client)
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/replacement-plans/{PLAN_ID}/approve-and-execute",
        json=body,
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
    assert "force" not in response.text


def test_replacement_status_and_plan_reads_are_private(client: TestClient) -> None:
    operation_id = UUID("00000000-0000-4000-8000-000000000213")
    assert (
        client.get(
            f"/api/instances/{INSTANCE_ID}/replacement-plans/{PLAN_ID}"
        ).status_code
        == 401
    )
    assert (
        client.get(
            f"/api/instances/{INSTANCE_ID}/repository-replacement-operations/"
            f"{operation_id}"
        ).status_code
        == 401
    )
