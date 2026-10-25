"""Final replacement requires Web admin, exact consent, scope and shared admission."""

import html
import re
from dataclasses import asdict
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kodi_lab.broker.docker_inventory import (
    ManagedContainerRecord,
    ProvisionedManagedContainerRecord,
)
from kodi_lab.broker.errors import ClientTransportError
from kodi_lab.broker.managed_capacity import ManagedCapacityAdmission
from kodi_lab.broker.operations import ProvisionManagedContainerResult
from tests.web.conftest import ORIGIN, FakeBroker
from tests.web.test_session_replacement_reviews import prepare as prepare_review


def prepare(
    client: TestClient,
    broker: FakeBroker,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    env = prepare_review(client, broker, database, monkeypatch)
    saved = client.post(
        "/api/development-session/replacement-review",
        json=env["request"],
        headers=env["headers"],
    )
    assert saved.status_code == 200, saved.text
    review = saved.json()
    original = broker.provision_managed_container

    def records() -> tuple[ManagedContainerRecord, ...]:
        item = broker.container
        return (
            ()
            if item is None
            else (
                ManagedContainerRecord(
                    item.managed_instance_id,
                    item.container_id,
                    item.image_id,
                    item.runtime_state,
                    item.health_state,
                    item.runtime_revision_id,
                ),
            )
        )

    admission = ManagedCapacityAdmission(database, records)

    def provision(*, managed_instance_id: str) -> ProvisionManagedContainerResult:
        def create() -> ProvisionedManagedContainerRecord:
            assert broker.container is None
            result = original(managed_instance_id=managed_instance_id)
            result = result.model_copy(update={"container_id": "f" * 64})
            assert broker.container is not None
            broker.container = broker.container.model_copy(
                update={"container_id": result.container_id}
            )
            return ProvisionedManagedContainerRecord(
                result.managed_instance_id,
                result.runtime_revision_id,
                result.container_id,
                result.image_id,
                result.runtime_state,
                result.created,
            )

        result = admission.run_provision(
            instance_id=managed_instance_id,
            revision_id=env["request"]["next_runtime_revision_id"],
            dispatch=create,
        )
        return ProvisionManagedContainerResult.model_validate(asdict(result))

    monkeypatch.setattr(broker, "provision_managed_container", provision)
    operation = dict(
        request_id=str(uuid4()),
        expected_snapshot_sha256=review["snapshot_sha256"],
        confirm_profile_disposal=True,
    )
    return {**env, "review": review, "operation": operation}


def test_replacement_actions_require_auth_csrf_and_strict_final_consent(
    client: TestClient,
    broker: FakeBroker,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = f"/api/development-session/replacement/{uuid4()}"
    assert client.get(path).status_code == 401
    for action in ("execute", "continue", "release"):
        assert (
            client.post(path + "/" + action, json={}, headers=ORIGIN).status_code == 401
        )
    env = prepare(client, broker, database, monkeypatch)
    path = f"/api/development-session/replacement/{env['review']['review_id']}"
    assert (
        client.post(
            path + "/execute", json=env["operation"], headers=ORIGIN
        ).status_code
        == 403
    )
    for change in (
        {"confirm_profile_disposal": 1},
        {"confirm_profile_disposal": False},
        {"container_id": "a" * 64},
        {"path": "/profile"},
    ):
        assert (
            client.post(
                path + "/execute",
                json={**env["operation"], **change},
                headers=env["headers"],
            ).status_code
            == 422
        )
    assert (
        client.post(
            path + "/execute",
            json={**env["operation"], "expected_snapshot_sha256": "e" * 64},
            headers=env["headers"],
        ).status_code
        == 409
    )
    assert client.get(path).json() is None
    assert not broker.calls


def test_json_replay_status_preserve_exact_scope_and_private_evidence(
    client: TestClient,
    broker: FakeBroker,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = prepare(client, broker, database, monkeypatch)
    review_id = env["review"]["review_id"]
    path = f"/api/development-session/replacement/{review_id}"
    result = client.post(
        path + "/execute", json=env["operation"], headers=env["headers"]
    )
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["phase"] == "successor_bound" and data["successor"]["generation"] == 2
    assert data["successor"]["container_id"] == "f" * 64
    assert (
        data["deployment_baseline"] == "not_inherited"
        and data["live_runtime_state"] == "not_observed"
    )
    count = len(broker.calls)
    assert (
        client.post(
            path + "/execute", json=env["operation"], headers=env["headers"]
        ).json()
        == data
    )
    assert client.get(path).json() == data and len(broker.calls) == count
    assert "untrusted log" not in result.text
    page = client.get(f"/development-session/replacement/{review_id}")
    assert (
        page.status_code == 200
        and "Independent source, native and Bridge setup" in page.text
    )
    assert page.headers["cache-control"] == "no-store"
    assert (
        client.post(
            f"/api/development-session/replacement-review/{review_id}/cancel",
            json={
                "expected_snapshot_sha256": env["review"]["snapshot_sha256"],
                "confirm_cancel": True,
            },
            headers=env["headers"],
        ).status_code
        == 409
    )
    client.cookies.clear()
    assert client.get(path).status_code == 401
    assert (
        client.post(
            path + "/release", json=env["operation"], headers=env["headers"]
        ).status_code
        == 401
    )
    assert len(broker.calls) == count


def test_final_form_binds_nonce_origin_digest_and_explicit_profile_data_loss(
    client: TestClient,
    broker: FakeBroker,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = prepare(client, broker, database, monkeypatch)
    review_id = env["review"]["review_id"]
    page = client.get(f"/development-session/replacement/{review_id}")
    assert "Permanently remove this exact stopped disposable profile" in page.text
    assert "Native crash reports remain uncollected" in page.text

    def hidden(name: str) -> str:
        found = re.search(r'name="' + name + r'" value="([^"]*)"', page.text)
        assert found is not None
        return html.unescape(found.group(1))

    form = dict(
        csrf_token=env["csrf"],
        request_id=hidden("request_id"),
        expected_snapshot_sha256=hidden("expected_snapshot_sha256"),
        confirmation_ack="yes",
    )
    path = f"/development-session/replacement/{review_id}/execute"
    assert client.post(path, data=form).status_code == 403
    assert (
        client.post(
            path, data={**form, "confirmation_ack": "no"}, headers=ORIGIN
        ).status_code
        == 422
    )
    assert (
        client.post(
            path, data={**form, "csrf_token": "invalid"}, headers=ORIGIN
        ).status_code
        == 403
    )
    result = client.post(path, data=form, headers=ORIGIN, follow_redirects=False)
    assert result.status_code == 303, result.text
    count = len(broker.calls)
    assert (
        client.post(path, data=form, headers=ORIGIN, follow_redirects=False).headers[
            "location"
        ]
        == result.headers["location"]
    )
    assert len(broker.calls) == count


def test_unknown_retirement_stays_inspectable_and_no_empty_inventory_escape_exists(
    client: TestClient,
    broker: FakeBroker,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = prepare(client, broker, database, monkeypatch)

    def lost(**kwargs: object) -> Any:
        broker.calls.append(("deprovision", dict(kwargs)))
        broker.container = None
        raise ClientTransportError("fixture lost removal reply")

    monkeypatch.setattr(broker, "deprovision_managed_container", lost)
    review_id = env["review"]["review_id"]
    path = f"/api/development-session/replacement/{review_id}"
    result = client.post(
        path + "/execute", json=env["operation"], headers=env["headers"]
    )
    assert (
        result.status_code == 200 and result.json()["phase"] == "retirement_unconfirmed"
    )
    for action in ("continue", "release"):
        refused = client.post(
            path + "/" + action, json=env["operation"], headers=env["headers"]
        )
        assert refused.status_code == 409 and "retirement_unconfirmed" in refused.text
    assert (
        client.post(
            path + "/execute", json=env["operation"], headers=env["headers"]
        ).json()
        == result.json()
    )
    page = client.get(f"/development-session/replacement/{review_id}")
    assert "Do not resubmit or discard" in page.text
    assert client.get("/api/development-session").json()["current"]["generation"] == 1
    assert [kind for kind, _kwargs in broker.calls] == ["deprovision"]
