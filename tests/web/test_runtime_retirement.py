from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kodi_lab.managed import runtime_retirement as retirement
from kodi_lab.managed.broker import Broker, BrokerClient
from kodi_lab.managed.controller import Controller
from kodi_lab.managed.protocol import FIELDS, OperationReply, Refusal
from kodi_lab.managed.runtime import Runtime
from tests.web.conftest import ORIGIN, login

SESSION = "b" * 64
ASSETS = retirement.Assets(engine="test-engine", feed="a" * 64, network="b" * 64)
IMAGE = "sha256:" + "d" * 64


@pytest.fixture
def target(tmp_path: Path) -> Controller:
    root = tmp_path / "managed"
    root.mkdir(mode=0o700)
    image = tmp_path / "reviewed"
    image.write_text(IMAGE)
    controller = Controller(root, reviewed_image=image)
    slot = "c" * 32
    profile = root / "volumes" / slot
    profile.mkdir()
    (profile / "payload").write_text("working data")
    controller.omega.pointer.write_text(slot)
    controller.omega.image_pin.write_text(IMAGE)
    path = controller.snapshots.path("retained")
    path.write_bytes(b"snapshot")
    path.with_suffix(".json").write_text(json.dumps({"image": IMAGE}))
    return controller


def test_retirement_retries_preserves_recovery_and_reenables(
    target: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = target.runtime_retirement
    runtime = target.omega
    current = ASSETS
    calls: list[retirement.Assets] = []

    def observed(native: Runtime, engine: str | None) -> retirement.Assets:
        assert native is runtime and engine in {None, ASSETS.engine}
        return current

    def sweep(native: Runtime, assets: retirement.Assets) -> None:
        nonlocal current
        assert native is runtime and assets == ASSETS
        calls.append(assets)
        if len(calls) == 1:
            current = assets.model_copy(update={"feed": None})
            raise subprocess.TimeoutExpired("network rm", 4)
        current = assets.model_copy(update={"network": None, "feed": None})

    monkeypatch.setattr(retirement, "observe_assets", observed)
    monkeypatch.setattr(retirement, "retire_services", sweep)
    review = service.review("omega", SESSION)
    assert review["eligible"] == ["Private feed helper", "Private runtime network"]
    assert review["snapshots"] == [{"name": "retained", "bytes": 8}]
    assert not runtime.retired()
    current = ASSETS.model_copy(update={"network": "e" * 64})
    with pytest.raises(Refusal, match="managed_retirement_assets_changed"):
        service.execute(review["token"], SESSION, "omega", "retire")
    assert not runtime.retired() and not calls
    current = ASSETS
    assert (
        service.execute(review["token"], SESSION, "omega", "retire")["state"]
        == "pending"
    )
    for action, args in (
        ("managed_start", {}),
        ("managed_prepare", {}),
        ("managed_reset", {}),
        ("snapshot_load", {"name": "retained"}),
        ("managed_public_repository_select", {"repository": "fixture"}),
        ("workspace_deploy", {"addon": "script.fixture"}),
    ):
        with pytest.raises(Refusal, match="managed_runtime_retired"):
            target.perform(action, args, actor="admin")
    resume = service.review("omega", SESSION)
    assert resume["state"] == "pending"
    assert resume["eligible"] == ["Private runtime network"]
    done = service.execute(resume["token"], SESSION, "omega", "retire")
    assert done["state"] == "retired"
    assert service.execute(resume["token"], SESSION, "omega", "retire") == done
    assert (runtime.profile() / "payload").read_text() == "working data"
    assert target.snapshots.path("retained").read_bytes() == b"snapshot"
    assert runtime.image == IMAGE
    enable = service.review("omega", SESSION)
    assert enable["action"] == "reactivate"
    assert (
        service.execute(enable["token"], SESSION, "omega", "reactivate")["state"]
        == "available"
    )
    assert not runtime.retired() and calls == [ASSETS, ASSETS]
    with pytest.raises(Refusal, match="managed_retirement_identity"):
        service.execute(review["token"], SESSION, "omega", "retire")
    assert service.review("omega", SESSION)["action"] == "retire"


def test_recheck_lease_shell_identity_and_expiry(
    target: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = target.runtime_retirement
    monkeypatch.setattr(retirement, "observe_assets", lambda *_: ASSETS)
    review = service.review("omega", SESSION)
    target.claim(str(uuid4()))
    assert service.review("omega", SESSION)["token"] is None
    with pytest.raises(Refusal, match="managed_retirement_identity"):
        service.execute(review["token"], SESSION, "omega", "retire")
    target.lease_path.unlink()
    with pytest.raises(Refusal, match="storage_maintenance_preview_expired"):
        service.execute(review["token"], "c" * 64, "omega", "retire")
    with pytest.raises(Refusal, match="storage_maintenance_preview_expired"):
        service.execute(review["token"], SESSION, "omega", "reactivate")
    profile = target.omega.profile()
    profile.rename(profile.with_name("held"))
    profile.mkdir()
    with pytest.raises(Refusal, match="managed_retirement_identity"):
        service.execute(review["token"], SESSION, "omega", "retire")
    assert not target.omega.retired()


def test_engine_and_exact_ids_are_rechecked_before_removal(
    target: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    commands_seen = iter((ASSETS, ASSETS.model_copy(update={"feed": "f" * 64})))

    def command(args: list[str], **_: object) -> None:
        commands.append(args)

    monkeypatch.setattr(subprocess, "run", command)
    monkeypatch.setattr(retirement, "observe_assets", lambda *_: next(commands_seen))
    with pytest.raises(Refusal, match="managed_retirement_assets_changed"):
        retirement.retire_services(target.omega, ASSETS)
    assert len(commands) == 1
    assert commands[0][-5:] == ["container", "stop", "--time", "2", ASSETS.feed]


def test_retirement_requests_are_only_available_to_admin(
    target: Controller, tmp_path: Path
) -> None:
    assert retirement.OPERATIONS.isdisjoint(FIELDS)
    with Broker(tmp_path / "admin.sock", target, os.getuid()) as broker:
        for name in retirement.OPERATIONS:
            with pytest.raises(Refusal, match="storage_maintenance_admin_required"):
                broker.dispatch({"actor": str(uuid4()), "name": name, "args": {}})


def test_web_retirement_requires_signed_session_confirmation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[str, str, Mapping[str, str]]] = []

    def broker_call(
        _self: BrokerClient, actor: str, name: str, args: Mapping[str, str]
    ) -> OperationReply:
        commands.append((actor, name, args))
        if name == "runtime_retirement_review":
            return {
                "result": {
                    "state": "available",
                    "token": "exact-preview",
                    "action": "retire",
                    "image": IMAGE,
                    "working_profile": True,
                },
                "elapsed_seconds": 0,
            }
        return {"result": {"state": "retired"}, "elapsed_seconds": 0}

    monkeypatch.setattr(BrokerClient, "request", broker_call)
    url = "/storage-maintenance/runtime/omega"
    assert client.get(url).status_code == 401
    csrf = login(client)
    response = client.get(url)
    assert response.status_code == 200 and "keeping the profile" in response.text
    data = {"action": "retire", "token": "exact-preview", "csrf_token": csrf}
    assert client.post(url, data=data, headers=ORIGIN).status_code == 422
    data["confirmed"] = "yes"
    assert (
        client.post(
            url, data={**data, "csrf_token": "wrong"}, headers=ORIGIN
        ).status_code
        == 403
    )
    assert len(commands) == 1
    result = client.post(url, data=data, headers=ORIGIN)
    assert result.status_code == 200 and "remain available for recovery" in result.text
    assert commands[-1][:2] == ("admin", "runtime_retirement_execute")


def test_restore_to_new_engine_and_volume_can_reenable_without_removing_services(
    target: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = ASSETS

    def observed(_native: Runtime, engine: str | None) -> retirement.Assets:
        assert engine in {None, current.engine}
        return current

    def interrupted(*_args: object) -> None:
        raise OSError("interrupted before removal")

    service = target.runtime_retirement
    monkeypatch.setattr(retirement, "observe_assets", observed)
    monkeypatch.setattr(retirement, "retire_services", interrupted)
    view = service.review("omega", SESSION)
    assert (
        service.execute(view["token"], SESSION, "omega", "retire")["state"] == "pending"
    )
    profile = target.omega.profile()
    profile.rename(profile.with_name("old-profile"))
    profile.mkdir()
    (profile / "payload").write_text("restored working data")
    current = retirement.Assets(engine="restored-engine", feed=None, network=None)
    restored = service.review("omega", SESSION)
    assert restored["action"] == "reactivate"
    assert (
        service.execute(restored["token"], SESSION, "omega", "reactivate")["state"]
        == "available"
    )
    assert (profile / "payload").read_text() == "restored working data"
    assert not target.omega.retired()


def test_unprepared_runtime_can_reenable_after_reviewed_image_replacement(
    target: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = target.omega
    native.pointer.unlink()
    service = target.runtime_retirement
    absent = ASSETS.model_copy(update={"feed": None, "network": None})
    monkeypatch.setattr(retirement, "observe_assets", lambda *_: absent)
    monkeypatch.setattr(retirement, "retire_services", lambda *_: None)
    review = service.review("omega", SESSION)
    assert (
        service.execute(review["token"], SESSION, "omega", "retire")["state"]
        == "retired"
    )
    assert native.reviewed_image
    native.reviewed_image.write_text("sha256:" + "e" * 64)
    review = service.review("omega", SESSION)
    assert review["action"] == "reactivate"
    assert (
        service.execute(review["token"], SESSION, "omega", "reactivate")["state"]
        == "available"
    )
    assert not native.retired()


def test_feed_helper_with_sibling_attachment_is_protected(
    target: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = target.omega
    attachments = {
        native.network_name: {"NetworkID": ASSETS.network},
        "unrelated": {"NetworkID": "f" * 64},
    }
    settings = {
        "Id": ASSETS.feed,
        "HostConfig": {"NetworkMode": ASSETS.network},
        "State": {"Running": True},
        "NetworkSettings": {"Networks": attachments},
    }
    monkeypatch.setattr(
        retirement, "docker_read", lambda *_, **__: json.dumps([settings])
    )
    assert ASSETS.feed and ASSETS.network
    with pytest.raises(Refusal, match="managed_retirement_assets_changed"):
        retirement.require_feed_network(native, ASSETS.feed, ASSETS.network, 1)
    del attachments["unrelated"]
    retirement.require_feed_network(native, ASSETS.feed, ASSETS.network, 1)


@pytest.mark.parametrize("dependency", ["network", "helper", "unrelated"])
def test_inactive_container_dependencies_are_read_back(
    target: Controller, monkeypatch: pytest.MonkeyPatch, dependency: str
) -> None:
    native = target.omega
    assert ASSETS.feed and ASSETS.network
    other = "d" * 64
    mode = {
        "network": native.network_name,
        "helper": "container:" + ASSETS.feed,
        "unrelated": "none",
    }[dependency]
    replies = iter(
        (f"{other}\n{ASSETS.feed}", f'{other}\t"{mode}"\t""\t""\tnull\tnull\tnull')
    )
    monkeypatch.setattr(retirement, "docker_read", lambda *_, **__: next(replies))
    if dependency == "unrelated":
        retirement.require_no_service_dependents(native, ASSETS.feed, ASSETS.network, 1)
    else:
        with pytest.raises(Refusal, match="managed_retirement_assets_changed"):
            retirement.require_no_service_dependents(
                native, ASSETS.feed, ASSETS.network, 1
            )


def test_reenable_fsync_failure_fences_native_writes(
    target: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.managed import runtime

    service = target.runtime_retirement
    absent = ASSETS.model_copy(update={"feed": None, "network": None})
    monkeypatch.setattr(retirement, "observe_assets", lambda *_: absent)
    review = service.review("omega", SESSION)
    assert (
        service.execute(review["token"], SESSION, "omega", "retire")["state"]
        == "retired"
    )
    review = service.review("omega", SESSION)

    def unavailable(_path: Path) -> None:
        raise OSError("injected fsync error")

    with monkeypatch.context() as fault:
        fault.setattr(runtime, "sync_dir", unavailable)
        fault.setattr(retirement, "sync_dir", unavailable)
        with pytest.raises(OSError, match="fsync"):
            service.execute(review["token"], SESSION, "omega", "reactivate")
        with pytest.raises(OSError, match="fsync"):
            target.omega.require_not_retired()
        with pytest.raises(OSError, match="fsync"):
            service.execute(review["token"], SESSION, "omega", "reactivate")
    assert (
        service.execute(review["token"], SESSION, "omega", "reactivate")["state"]
        == "available"
    )
    target.omega.require_not_retired()
