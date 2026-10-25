"""Admin-only exact review forms hold evidence without executing replacement."""

import html
import json
import re
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kodi_lab.application.development_sessions import DevelopmentSessionService
from kodi_lab.storage.models import DevelopmentSessionRow
from tests.web.conftest import CONTAINER_ID, INSTANCE_ID, ORIGIN, FakeBroker, login
from tests.web.test_development_session import source_and_workspace
from tests.web.test_retained_diagnostics import body as capture_body
from tests.web.test_retained_diagnostics import prepare as prepare_diagnostics

API = "/api/development-session/replacement-review"
PAGE = "/development-session/replacement"


def prepare(
    client: TestClient,
    broker: FakeBroker,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    _source, creation = source_and_workspace(client)
    monkeypatch.setattr(DevelopmentSessionService, "_runtime", lambda *_args: None)
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    reserved = client.post(
        "/api/development-session/reserve",
        json=dict(
            request_id=str(uuid4()),
            expected_generation=0,
            target_id=str(INSTANCE_ID),
            workspace_id=str(creation.workspace_id),
        ),
        headers=headers,
    )
    assert reserved.status_code == 200, reserved.text
    calls = prepare_diagnostics(client, broker, database, monkeypatch)
    with database.begin() as session:
        row = session.get(DevelopmentSessionRow, 1)
        row.phase, row.container_id = "bound", CONTAINER_ID
    capture = client.post(
        f"/api/instances/{INSTANCE_ID}/diagnostic-captures",
        json=capture_body(),
        headers=headers,
    )
    assert capture.status_code == 200, capture.text
    offer = client.get(
        "/api/development-session/replacement-offer",
        params={"target_id": str(INSTANCE_ID)},
    )
    assert offer.status_code == 200, offer.text
    request = dict(
        **offer.json(),
        request_id=str(uuid4()),
        confirm_review=True,
        acknowledge_uncollected_crash_reports=True,
        evidence=[
            dict(
                capture_id=capture.json()["capture_id"],
                payload_sha256=capture.json()["payload_sha256"],
            )
        ],
    )
    return dict(
        csrf=csrf, headers=headers, calls=calls, request=request, capture=capture.json()
    )


def test_review_routes_require_admin_and_mutations_require_csrf(
    client: TestClient,
    broker: FakeBroker,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert client.get(PAGE, follow_redirects=False).status_code == 303
    assert client.get(API).status_code == 401
    assert client.get(f"{API}/{uuid4()}").status_code == 401
    assert client.post(API, json={}, headers=ORIGIN).status_code == 401
    env = prepare(client, broker, database, monkeypatch)
    assert client.post(API, json=env["request"], headers=ORIGIN).status_code == 403
    for change in (
        {"confirm_review": 1},
        {"delete_profile": True},
        {"acknowledge_uncollected_crash_reports": False},
    ):
        assert (
            client.post(
                API, json={**env["request"], **change}, headers=env["headers"]
            ).status_code
            == 422
        )
    assert client.get(API).json() is None
    assert len(env["calls"]) == 1 and not broker.calls


def test_json_review_replay_private_status_hold_and_exact_cancel(
    client: TestClient,
    broker: FakeBroker,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = prepare(client, broker, database, monkeypatch)
    result = client.post(API, json=env["request"], headers=env["headers"])
    assert result.status_code == 200, result.text
    review = result.json()
    assert not review["executes_replacement"] and "untrusted log" not in result.text
    assert (
        client.post(API, json=env["request"], headers=env["headers"]).json() == review
    )
    review_id = review["review_id"]
    capture = env["capture"]
    discard = dict(
        expected_state="retained",
        expected_payload_sha256=capture["payload_sha256"],
        confirm_discard=True,
    )
    path = f"/api/diagnostic-captures/{capture['capture_id']}/discard"
    refused = client.post(path, json=discard, headers=env["headers"])
    assert refused.status_code == 409 and "held_for_session_review" in refused.text
    page = client.get(f"{PAGE}/{review_id}")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "No replacement runs automatically" in page.text
    cancel = dict(
        expected_snapshot_sha256=review["snapshot_sha256"], confirm_cancel=True
    )
    assert (
        client.post(
            f"{API}/{review_id}/cancel", json=cancel, headers=ORIGIN
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{API}/{review_id}/cancel",
            json={**cancel, "expected_snapshot_sha256": "f" * 64},
            headers=env["headers"],
        ).status_code
        == 409
    )
    client.cookies.clear()
    assert client.get(f"{API}/{review_id}").status_code == 401
    assert client.get(f"{PAGE}/{review_id}", follow_redirects=False).status_code == 303
    assert (
        client.post(
            f"{API}/{review_id}/cancel", json=cancel, headers=env["headers"]
        ).status_code
        == 401
    )
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert (
        client.post(f"{API}/{review_id}/cancel", json=cancel, headers=headers).json()[
            "state"
        ]
        == "cancelled"
    )
    assert client.post(path, json=discard, headers=headers).status_code == 200
    assert client.get("/api/development-session").json()["current"]["phase"] == "bound"
    assert len(env["calls"]) == 1 and not broker.calls


def test_forms_bind_displayed_scope_nonce_origin_evidence_and_cancel(
    client: TestClient,
    broker: FakeBroker,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = prepare(client, broker, database, monkeypatch)
    page = client.get(PAGE)
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert (
        "untrusted log" not in page.text
        and "Native crash reports have not been collected" in page.text
    )

    def hidden(name: str) -> str:
        found = re.search(r'name="' + name + r'" value="([^"]*)"', page.text)
        assert found is not None
        return html.unescape(found.group(1))

    capture = env["capture"]
    form = dict(
        csrf_token=env["csrf"],
        request_id=hidden("request_id"),
        selection_json=hidden("selection_json"),
        evidence=f"{capture['capture_id']}:{capture['payload_sha256']}",
        review_ack="yes",
        crash_reports_ack="yes",
    )
    assert client.post(PAGE, data=form).status_code == 403
    assert (
        client.post(
            PAGE, data={**form, "csrf_token": "invalid"}, headers=ORIGIN
        ).status_code
        == 403
    )
    for change in (
        {"crash_reports_ack": "no"},
        {"evidence": "malformed"},
        {"evidence": [form["evidence"]] * 2},
        {"selection_json": "{}"},
        {"remove": "yes"},
    ):
        assert (
            client.post(PAGE, data={**form, **change}, headers=ORIGIN).status_code
            == 422
        )
    selection = json.loads(form["selection_json"])
    selection["expected_generation"] += 1
    assert (
        client.post(
            PAGE, data={**form, "selection_json": json.dumps(selection)}, headers=ORIGIN
        ).status_code
        == 409
    )
    result = client.post(PAGE, data=form, headers=ORIGIN, follow_redirects=False)
    assert result.status_code == 303, result.text
    assert (
        client.post(PAGE, data=form, headers=ORIGIN, follow_redirects=False).headers[
            "location"
        ]
        == result.headers["location"]
    )
    review = client.get(API).json()
    cancel_form = dict(
        csrf_token=env["csrf"],
        expected_snapshot_sha256=review["snapshot_sha256"],
        cancel_ack="yes",
    )
    cancel_path = result.headers["location"] + "/cancel"
    assert client.post(cancel_path, data=cancel_form).status_code == 403
    assert (
        client.post(
            cancel_path, data=cancel_form, headers=ORIGIN, follow_redirects=False
        ).status_code
        == 303
    )
    assert "Evidence holds are released" in client.get(result.headers["location"]).text
    assert not broker.calls and len(env["calls"]) == 1
