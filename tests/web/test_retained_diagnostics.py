"""Real Web auth/CSRF and escaped evidence over the durable capture service."""

from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.storage.models import RuntimeContainerArtifactRow
from tests.application.test_retained_diagnostics import snapshot
from tests.web.conftest import (
    CONTAINER_ID,
    IMAGE_ID,
    INSTANCE_ID,
    ORIGIN,
    REVISION_ID,
    FakeBroker,
    inventory_entry,
    login,
)


def prepare(
    client: TestClient,
    broker: FakeBroker,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    root = cast(FastAPI, client.app).state.kodi_lab.settings.data_root
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with database.begin() as session:
        session.add(
            RuntimeContainerArtifactRow(
                runtime_revision_id=str(REVISION_ID), image_id=IMAGE_ID
            )
        )
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED)
    calls = []

    def read(**kwargs: Any) -> Any:
        calls.append(kwargs)
        value = snapshot()
        value.exit_metadata = value.exit_metadata.model_copy(
            update={
                "managed_instance_id": str(INSTANCE_ID),
                "runtime_revision_id": str(REVISION_ID),
                "container_id": CONTAINER_ID,
                "image_id": IMAGE_ID,
            }
        )
        value.artifacts[0].content = '<script>alert("untrusted log")</script>\n'
        return value

    monkeypatch.setattr(broker, "read_stopped_managed_logs", read, raising=False)
    return calls


def body() -> dict[str, Any]:
    return dict(
        request_id=str(uuid4()),
        expected_runtime_revision_id=str(REVISION_ID),
        expected_container_id=CONTAINER_ID,
        retain_redacted_logs=True,
    )


def test_auth_csrf_acknowledgement_and_closed_inputs_precede_all_log_reads(
    client: TestClient,
    broker: FakeBroker,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = prepare(client, broker, database, monkeypatch)
    path = f"/api/instances/{INSTANCE_ID}/diagnostic-captures"
    data = body()
    assert client.post(path, json=data, headers=ORIGIN).status_code == 401
    assert client.get("/api/diagnostic-captures").status_code == 401
    assert client.get("/diagnostics", follow_redirects=False).status_code == 303
    csrf = login(client)
    assert client.post(path, json=data, headers=ORIGIN).status_code == 403
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert (
        client.post(
            path, json={**data, "retain_redacted_logs": False}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            path, json={**data, "path": "/private"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            path,
            json={**data, "deployment_operation_id": str(uuid4())},
            headers=headers,
        ).status_code
        == 409
    )
    assert not calls


def test_captured_body_is_private_escaped_persistent_and_replay_safe(
    client: TestClient,
    broker: FakeBroker,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = prepare(client, broker, database, monkeypatch)
    csrf = login(client)
    data = body()
    path = f"/api/instances/{INSTANCE_ID}/diagnostic-captures"
    result = client.post(path, json=data, headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf})
    assert result.status_code == 200
    capture = result.json()
    assert (
        capture["state"] == "retained"
        and capture["authorizes_profile_disposal"] is False
    )
    assert "untrusted log" not in result.text and len(calls) == 1
    capture_id = capture["capture_id"]
    broker.container = None
    assert (
        client.post(
            path, json=data, headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
        ).json()["capture_id"]
        == capture_id
    )
    assert len(calls) == 1
    page = client.get(f"/diagnostics/{capture_id}")
    assert page.status_code == 200 and "&lt;script&gt;" in page.text
    assert "<script>alert(" not in page.text
    evidence = client.get(f"/api/diagnostic-captures/{capture_id}/evidence")
    assert (
        evidence.status_code == 200 and evidence.headers["cache-control"] == "no-store"
    )
    assert evidence.json()["logs"]["crash_reports"] == "not_collected"
    assert evidence.json()["logs"]["cross_file_consistency"] == "not_established"
    assert not any(
        c[0] in {"start", "stop", "provision", "deprovision"} for c in broker.calls
    )
    assert "Saving evidence never starts Kodi" in client.get("/diagnostics").text


def test_forms_bind_nonce_origin_csrf_and_reviewed_discard(
    client: TestClient,
    broker: FakeBroker,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = prepare(client, broker, database, monkeypatch)
    csrf = login(client)
    target = f"/instances/{INSTANCE_ID}/diagnostics"
    page = client.get(target)
    assert page.status_code == 200 and 'name="request_id"' in page.text
    data = {k: str(v) for k, v in body().items() if k != "retain_redacted_logs"}
    data.update(csrf_token=csrf, capture_ack="yes", deployment_operation_id="")
    assert (
        client.post(
            target,
            data=data,
            headers={"Origin": "https://foreign.invalid"},
            follow_redirects=False,
        ).status_code
        == 403
    )
    assert (
        client.post(
            target,
            data={**data, "csrf_token": "bad"},
            headers=ORIGIN,
            follow_redirects=False,
        ).status_code
        == 403
    )
    assert (
        client.post(
            target,
            data={**data, "force": "yes"},
            headers=ORIGIN,
            follow_redirects=False,
        ).status_code
        == 422
    )
    result = client.post(target, data=data, headers=ORIGIN, follow_redirects=False)
    assert result.status_code == 303 and len(calls) == 1
    assert (
        client.post(target, data=data, headers=ORIGIN, follow_redirects=False).headers[
            "location"
        ]
        == result.headers["location"]
    )
    assert len(calls) == 1
    context = cast(FastAPI, client.app).state.kodi_lab
    saved = context.retained_diagnostics.list()[0]
    discard = {
        "csrf_token": csrf,
        "expected_state": "retained",
        "expected_payload_sha256": saved.payload_sha256,
        "discard_ack": "yes",
    }
    path = f"/diagnostics/{saved.capture_id}/discard"
    assert (
        client.post(
            path,
            data={**discard, "expected_payload_sha256": "0" * 64},
            headers=ORIGIN,
            follow_redirects=False,
        ).status_code
        == 409
    )
    assert (
        client.post(
            path, data=discard, headers=ORIGIN, follow_redirects=False
        ).status_code
        == 303
    )
    assert context.retained_diagnostics.status(saved.capture_id).state == "discarded"
    assert (
        client.get(f"/api/diagnostic-captures/{saved.capture_id}/evidence").status_code
        == 409
    )
    assert (
        "historical identity remains"
        in client.get(f"/diagnostics/{saved.capture_id}").text
    )


def test_json_discard_and_known_private_evidence_require_full_web_authority(
    client: TestClient,
    broker: FakeBroker,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = prepare(client, broker, database, monkeypatch)
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    saved = client.post(
        f"/api/instances/{INSTANCE_ID}/diagnostic-captures",
        json=body(),
        headers=headers,
    ).json()
    capture_id = saved["capture_id"]
    discard = {
        "expected_state": "retained",
        "expected_payload_sha256": saved["payload_sha256"],
        "confirm_discard": True,
    }
    path = f"/api/diagnostic-captures/{capture_id}/discard"
    assert client.post(path, json=discard, headers=ORIGIN).status_code == 403
    assert (
        client.post(
            path, json={**discard, "confirm_discard": 1}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            path, json={**discard, "expected_payload_sha256": "0" * 64}, headers=headers
        ).status_code
        == 409
    )
    assert (
        client.get(f"/api/diagnostic-captures/{capture_id}/evidence").status_code == 200
    )
    client.cookies.clear()
    assert (
        client.get(f"/api/diagnostic-captures/{capture_id}/evidence").status_code == 401
    )
    assert client.get(f"/api/diagnostic-captures/{capture_id}").status_code == 401
    assert (
        client.get(f"/diagnostics/{capture_id}", follow_redirects=False).status_code
        == 303
    )
    assert client.post(path, json=discard, headers=headers).status_code == 401
    assert len(calls) == 1
