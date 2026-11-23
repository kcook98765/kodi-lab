"""The review forms retain normal administrator, CSRF and origin boundaries."""

import time
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.development.addition_reviews import AddedFile, SourceAdditionProof
from kodi_lab.storage.source_addition_reviews import addition_review_scope
from tests.web.conftest import INSTANCE_ID, ORIGIN, FakeBroker, login
from tests.web.test_development_source_deployments import (
    deployment_client as deployment_client,
)
from tests.web.test_development_sources import SOURCE_A

PATH = "resources/generated.xml"
START = f"/instances/{INSTANCE_ID}/development-sources/{SOURCE_A}/review-additions"


def test_review_form_requires_admin_origin_csrf_and_bounded_inputs(
    deployment_client: TestClient, broker: FakeBroker
) -> None:
    assert deployment_client.get(START).status_code == 401
    assert deployment_client.post(START, data={"paths": PATH}).status_code == 401
    csrf = login(deployment_client)
    page = deployment_client.get(START)
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "Inspect these added files" in page.text
    assert (
        deployment_client.post(START, data={"paths": PATH}, headers=ORIGIN).status_code
        == 422
    )
    assert (
        deployment_client.post(
            START, data={"paths": PATH, "csrf_token": "wrong"}, headers=ORIGIN
        ).status_code
        == 403
    )
    assert (
        deployment_client.post(
            START,
            data={"paths": PATH, "csrf_token": csrf},
            headers={"Origin": "https://attacker.invalid"},
        ).status_code
        == 403
    )
    assert (
        deployment_client.post(
            START,
            data={"paths": PATH, "csrf_token": csrf, "force": "true"},
            headers=ORIGIN,
        ).status_code
        == 422
    )
    assert (
        deployment_client.get(f"/source-addition-reviews/{uuid4()}").status_code == 404
    )
    assert (
        deployment_client.post(
            START,
            data={
                "paths": "\n".join(f"file-{i}" for i in range(33)),
                "csrf_token": csrf,
            },
            headers=ORIGIN,
        ).status_code
        == 422
    )
    assert not any(call[0] == "copy_sync_development_source" for call in broker.calls)


def test_exact_metadata_per_file_approval_receipt_and_repost(
    deployment_client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csrf = login(deployment_client)
    copied = deployment_client.post(
        f"/api/instances/{INSTANCE_ID}/development-sources/{SOURCE_A}/copy-sync",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert copied.status_code == 202
    for _ in range(100):
        status = deployment_client.get(copied.json()["status_url"]).json()
        if status["state"] == "succeeded":
            break
        time.sleep(0.01)
    assert status["state"] == "succeeded"

    def review(**kwargs: Any) -> SourceAdditionProof:
        with database() as session:
            scope = addition_review_scope(
                session,
                str(INSTANCE_ID),
                str(SOURCE_A),
                kwargs["expected_container_id"],
            )
        return SourceAdditionProof(
            scope_digest=scope,
            source_digest="sha256:" + "1" * 64,
            destination_digest="sha256:" + "3" * 64,
            container_digest="sha256:" + "4" * 64,
            trusted_destination_digest="sha256:" + "2" * 64,
            additions=(
                AddedFile(
                    path=PATH, sha256="a" * 64, size=123, mode=0o644, uid=1000, gid=1000
                ),
            ),
        )

    monkeypatch.setattr(broker, "review_source_additions", review, raising=False)
    preview = deployment_client.post(
        START,
        data={"paths": PATH, "csrf_token": csrf},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert preview.status_code == 303
    location = preview.headers["location"]
    page = deployment_client.get(location)
    assert "a" * 64 in page.text and "123 bytes" in page.text and "0644" in page.text
    assert (
        'name="approved_paths"' in page.text
        and "Approve these files and copy source" in page.text
    )
    approve = location + "/approve"
    form = {"csrf_token": csrf, "review_ack": "yes", "approved_paths": [PATH]}
    assert (
        deployment_client.post(
            approve, data={**form, "csrf_token": "bad"}, headers=ORIGIN
        ).status_code
        == 403
    )
    assert (
        deployment_client.post(
            approve, data=form, headers={"Origin": "https://attacker.invalid"}
        ).status_code
        == 403
    )
    assert (
        deployment_client.post(
            approve, data={"csrf_token": csrf, "review_ack": "yes"}, headers=ORIGIN
        ).status_code
        == 422
    )
    assert (
        deployment_client.post(
            approve,
            data={**form, "approved_paths": ["resources/other.xml"]},
            headers=ORIGIN,
        ).status_code
        == 409
    )
    result = deployment_client.post(
        approve, data=form, headers=ORIGIN, follow_redirects=False
    )
    assert result.status_code == 303
    saved = deployment_client.get(location)
    assert 'name="approved_paths"' not in saved.text
    assert "Saved copy state" in saved.text
    assert (
        deployment_client.post(
            approve, data=form, headers=ORIGIN, follow_redirects=False
        ).status_code
        == 303
    )
    assert sum(call[0] == "copy_sync_development_source" for call in broker.calls) <= 2
