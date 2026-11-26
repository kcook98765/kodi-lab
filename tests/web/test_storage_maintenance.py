from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kodi_lab.managed import controller as managed
from kodi_lab.managed import storage_maintenance
from kodi_lab.managed.broker import Broker, BrokerClient
from kodi_lab.managed.controller import Controller
from kodi_lab.managed.protocol import FIELDS, Document, OperationReply, Refusal
from kodi_lab.managed.runtime import Runtime
from tests.experiments.test_managed_feed_retirement import book, path_for, publish
from tests.web.conftest import ORIGIN, login

SESSION = "a" * 64


@pytest.fixture
def target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Controller:
    root = tmp_path / "managed"
    root.mkdir(mode=0o700)
    controller = Controller(root)

    def idle(runtime: Runtime, engine: str | None) -> bool:
        return runtime.generation == "omega" and engine == "fixture-engine"

    monkeypatch.setattr(storage_maintenance, "runtime_idle", idle)
    monkeypatch.setattr(managed, "runtime_idle", idle)
    return controller


def preview(target: Controller) -> Document:
    return dict(target.storage_maintenance.review("omega", SESSION)["items"][0])


def test_preview_is_read_only_and_exact_retry_survives_restart(
    target: Controller,
) -> None:
    old = publish(target, "1.0.0")
    latest = publish(target, "2.0.0")
    tracker = book(target)
    previous = tracker.record.read_bytes()
    item = preview(target)
    token = item["token"]
    assert token and item["artifact"]["digest"] == old.digest
    assert item["estimated_bytes"] > 0
    assert tracker.record.read_bytes() == previous
    assert not target.storage_maintenance.history_file.exists()
    assert path_for(target, old).read_bytes() == old.data

    result = target.storage_maintenance.execute(token, SESSION, "omega")
    assert result["state"] == "removed"
    assert result["estimated_bytes"] == item["estimated_bytes"]
    assert type(result["available_delta"]) is int
    assert not path_for(target, old).exists()
    assert path_for(target, latest).read_bytes() == latest.data
    assert target.packages.get(old.digest).data == old.data

    restarted = Controller(target.omega.root)
    assert restarted.storage_maintenance.execute(token, SESSION, "omega") == result
    assert len(restarted.storage_maintenance.history()) == 1


def test_hold_appearing_after_review_stops_confirmed_cleanup(
    target: Controller,
) -> None:
    old = publish(target, "1.0.0")
    publish(target, "2.0.0")
    token = preview(target)["token"]
    book(target).hold.write_text("protect this feed")
    result = target.storage_maintenance.execute(token, SESSION, "omega")
    assert result["state"] == "needs_review"
    assert path_for(target, old).exists()


def test_preview_expiry_and_different_session_block_mutation(
    target: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = publish(target, "1.0.0")
    publish(target, "2.0.0")
    view = preview(target)
    with pytest.raises(Refusal, match="storage_maintenance_preview_expired"):
        target.storage_maintenance.execute(view["token"], "c" * 64, "omega")
    with pytest.raises(Refusal, match="storage_maintenance_preview_expired"):
        target.storage_maintenance.execute(view["token"], SESSION, "nexus")
    monkeypatch.setattr(
        storage_maintenance, "time", SimpleNamespace(time=lambda: view["expires"] + 1)
    )
    with pytest.raises(Refusal, match="storage_maintenance_preview_expired"):
        target.storage_maintenance.execute(view["token"], SESSION, "omega")
    assert path_for(target, old).exists()
    assert not target.storage_maintenance.history()


def test_lost_result_keeps_removal_retryable_through_lifecycle_retirement(
    target: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = publish(target, "1.0.0")
    publish(target, "2.0.0")
    service = target.storage_maintenance
    token = preview(target)["token"]
    save = service.save

    def fail_receipt(history: list[storage_maintenance.Outcome]) -> None:
        if history[0].state == "removed":
            raise OSError("result storage unavailable")
        save(history)

    with monkeypatch.context() as faults:
        faults.setattr(service, "save", fail_receipt)
        result = service.execute(token, SESSION, "omega")
    assert result["state"] == "pending"
    assert not path_for(target, old).exists()
    assert book(target).read().pending[0].phase == "removing"
    target.resume_feed_cleanup()
    assert book(target).read().pending[0].phase == "removing"
    assert service.execute(token, SESSION, "omega")["state"] == "removed"
    assert not book(target).read().pending


def test_automatic_pause_does_not_remove_or_prevent_explicit_review(
    target: Controller,
) -> None:
    old = publish(target, "1.0.0")
    publish(target, "2.0.0")
    assert target.storage_maintenance.automatic()
    assert target.storage_maintenance.policy(False) == {"automatic": False}
    target.resume_feed_cleanup()
    assert path_for(target, old).exists()
    assert preview(target)["token"]
    assert target.storage_maintenance.policy(True) == {"automatic": True}
    target.resume_feed_cleanup()
    assert not path_for(target, old).exists()


def test_privileged_broker_requests_are_outside_managed_agent_protocol(
    target: Controller, tmp_path: Path
) -> None:
    operations = storage_maintenance.OPERATIONS
    assert operations.isdisjoint(FIELDS.keys())
    with Broker(tmp_path / "admin.sock", target, os.getuid()) as broker:
        for name in operations:
            with pytest.raises(Refusal, match="storage_maintenance_admin_required"):
                broker.dispatch({"actor": str(uuid4()), "name": name, "args": {}})


def test_web_confirmation_origin_csrf_and_exact_broker_scope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, dict[str, str]]] = []

    def request(
        _client: BrokerClient, actor: str, name: str, args: Mapping[str, str]
    ) -> OperationReply:
        calls.append((actor, name, dict(args)))
        artifact = {"addon": "script.fixture", "version": "1.0.0", "digest": "b" * 64}
        if name == "storage_review":
            result = {
                "generation": "omega",
                "automatic": True,
                "count": 1,
                "history": [],
                "items": [
                    {
                        "artifact": artifact,
                        "token": "private-preview",
                        "estimated_bytes": 100,
                        "reason": "Verified duplicate",
                    }
                ],
            }
        else:
            result = {
                "generation": "omega",
                "artifact": artifact,
                "state": "removed",
                "reason": "Verified result",
                "estimated_bytes": 100,
                "available_delta": -5,
            }
        return {"result": result, "elapsed_seconds": 0}

    monkeypatch.setattr(BrokerClient, "request", request)
    endpoint = "/storage-maintenance/cleanup"
    values = {"generation": "omega", "token": "private-preview", "confirmed": "yes"}
    assert client.get("/storage-maintenance/review").status_code == 401
    assert client.post(endpoint, data=values).status_code == 401
    csrf = login(client)
    page = client.get("/storage-maintenance/review")
    assert page.status_code == 200
    assert "Confirm removal of this feed copy" in page.text
    assert "Verified duplicate" in page.text
    assert len(calls) == 1
    body = {**values, "csrf_token": csrf}
    assert (
        client.post(
            endpoint, data=body, headers={"Origin": "https://elsewhere.example"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            endpoint, data={**body, "csrf_token": "bad"}, headers=ORIGIN
        ).status_code
        == 403
    )
    assert (
        client.post(
            endpoint, data={**body, "confirmed": "no"}, headers=ORIGIN
        ).status_code
        == 422
    )
    assert len(calls) == 1
    response = client.post(endpoint, data=body, headers=ORIGIN)
    assert response.status_code == 200
    assert "Estimated reclaimable file bytes 100 B" in response.text
    assert "Observed net available-space change -5 B" in response.text
    assert calls[-1] == (
        "admin",
        "storage_execute",
        {
            "session": hashlib.sha256(csrf.encode()).hexdigest(),
            "generation": "omega",
            "token": "private-preview",
        },
    )


def test_same_digest_replacement_requires_a_new_preview(target: Controller) -> None:
    old = publish(target, "1.0.0")
    publish(target, "2.0.0")
    item = preview(target)
    path = path_for(target, old)
    parked = path.with_name("parked-fixture")
    path.rename(parked)
    path.write_bytes(old.data)
    assert path.stat().st_ino != parked.stat().st_ino
    result = target.storage_maintenance.execute(item["token"], SESSION, "omega")
    assert result["state"] == "skipped"
    assert path.read_bytes() == old.data


def test_partial_retry_preserves_a_same_digest_replacement(
    target: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = publish(target, "1.0.0")
    publish(target, "2.0.0")
    item = preview(target)
    path = path_for(target, old)
    checksum = path.with_suffix(".zip.sha256")
    unlink = Path.unlink

    def fail(path: Path, missing_ok: bool = False) -> None:
        if path == checksum:
            raise OSError("checksum removal interrupted")
        unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", fail)
        first = target.storage_maintenance.execute(item["token"], SESSION, "omega")
    assert first["state"] == "pending" and not path.exists()
    path.write_bytes(old.data)
    resumed = target.storage_maintenance.execute(item["token"], SESSION, "omega")
    assert resumed["state"] == "needs_review"
    target.resume_feed_cleanup()
    assert path.read_bytes() == old.data
    assert checksum.exists()


def test_pending_authorizations_are_deduplicated_and_can_expire(
    target: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = publish(target, "1.0.0")
    publish(target, "2.0.0")
    first = preview(target)
    second = preview(target)
    service = target.storage_maintenance
    with monkeypatch.context() as blocked:
        blocked.setattr(storage_maintenance, "runtime_idle", lambda *_: False)
        assert service.execute(first["token"], SESSION, "omega")["state"] == "pending"
        with pytest.raises(Refusal, match="storage_maintenance_already_pending"):
            service.execute(second["token"], SESSION, "omega")
    assert len(service.history()) == 1 and path_for(target, old).exists()
    monkeypatch.setattr(
        storage_maintenance, "time", SimpleNamespace(time=lambda: first["expires"] + 1)
    )
    assert service.execute(first["token"], SESSION, "omega")["state"] == "needs_review"
    fresh = service.review("omega", "d" * 64)["items"][0]
    assert service.execute(fresh["token"], "d" * 64, "omega")["state"] == "removed"


def test_bounded_review_can_page_past_protected_copies(target: Controller) -> None:
    packages = [publish(target, f"{version}.0.0") for version in range(1, 7)]
    catalog = target.omega.root / "catalog.json"
    catalog.write_text(json.dumps([pkg.digest for pkg in packages[:4]]))
    service = target.storage_maintenance
    first = service.review("omega", SESSION, 1)
    assert first["next_page"] == 2
    assert not any(row["token"] for row in first["items"])
    second = service.review("omega", SESSION, 2)
    assert second["items"][0]["artifact"]["digest"] == packages[4].digest
    assert second["items"][0]["token"]


def test_automatic_cleanup_control_keeps_scope_and_reverse_action_visible(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = True

    def request(
        _client: BrokerClient, actor: str, name: str, args: Mapping[str, str]
    ) -> OperationReply:
        nonlocal state
        assert actor == "admin"
        if name == "storage_policy":
            state = args["enabled"] == "true"
        else:
            assert name == "storage_review"
        return {"result": {"automatic": state, "items": []}, "elapsed_seconds": 0}

    monkeypatch.setattr(BrokerClient, "request", request)
    csrf = login(client)
    review = client.get("/storage-maintenance/review?generation=matrix")
    assert "This setting applies to all managed Kodi versions" in review.text
    assert "Pause automatic feed cleanup" in review.text
    data = {"generation": "matrix", "csrf_token": csrf}
    pause = client.post(
        "/storage-maintenance/policy",
        data={**data, "enabled": "false"},
        headers=ORIGIN,
    )
    assert pause.status_code == 200
    assert "Automatic feed cleanup is paused" in pause.text
    assert "Resume automatic feed cleanup" in pause.text
    assert (
        "Automatic feed cleanup is paused"
        in client.get("/storage-maintenance/review?generation=omega").text
    )
    resume = client.post(
        "/storage-maintenance/policy",
        data={**data, "enabled": "true"},
        headers=ORIGIN,
    )
    assert resume.status_code == 200
    assert "Pause automatic feed cleanup" in resume.text
    assert state
