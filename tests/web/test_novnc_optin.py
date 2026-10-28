from __future__ import annotations

import os
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.novnc import DisplayLane, ViewBinding
from kodi_lab.broker.socket_path import SocketPathConfig
from kodi_lab.config import Settings
from kodi_lab.web.app import create_app
from kodi_lab.web.novnc_optin import build_viewer, resolve_binding

from . import conftest
from .conftest import INSTANCE_ID, ORIGIN


def test_optin_defaults_to_disabled_and_allows_only_one_instance() -> None:
    assert Settings().experimental_novnc_instances == ()
    with pytest.raises(ValidationError):
        Settings(experimental_novnc_instances=(uuid4(), uuid4()))


def test_private_lookup_returns_exact_binding_without_admitting_input(
    tmp_path: Path,
) -> None:
    path = tmp_path / "display.sock"
    config = SocketPathConfig(
        runtime_directory=tmp_path,
        socket_path=path,
        directory_owner_uid=os.getuid(),
        directory_group_gid=os.getgid(),
        socket_owner_uid=os.getuid(),
        socket_group_gid=os.getgid(),
        directory_mode=0o700,
    )
    target = uuid4()
    binding = ViewBinding(
        "a" * 64, "sha256:" + "b" * 64, "one-incarnation", (str(uuid4()), 2, None)
    )
    looked_up = []

    def lookup(instance: Any) -> ViewBinding:
        assert instance == target
        looked_up.append(instance)
        return binding

    lane = DisplayLane(
        config,
        os.getuid(),
        lambda _: pytest.fail("Lookup must not create a worker"),
        resolve=lookup,
    )
    stop = Event()
    lane.start(stop)
    try:
        assert resolve_binding(path, target) == binding
        assert looked_up == [target] and not lane.active and not lane.results
        # A lookup denied by the resolver closes without worker/input admission.
        with pytest.raises(ValueError):
            resolve_binding(path, uuid4())
    finally:
        stop.set()
        lane.close()
    assert not path.exists()


def test_web_allowlist_rejects_another_target_before_private_broker_lookup(
    database: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    viewer = build_viewer(
        Settings(
            runtime_root=tmp_path,
            experimental_novnc_instances=(INSTANCE_ID,),
        ),
        database,
    )
    assert viewer.require_https and viewer.enabled_instances == frozenset({INSTANCE_ID})
    with pytest.raises(ValueError, match="not enabled"):
        viewer.authority(uuid4())
    viewer.close()


@pytest.fixture
def opted_client(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> TestClient:
    create = create_app

    def configured(**kwargs: Any) -> Any:
        kwargs["settings"].experimental_novnc_instances = (INSTANCE_ID,)
        return create(**kwargs)

    monkeypatch.setattr(conftest, "create_app", configured)
    return request.getfixturevalue("client")  # type: ignore[no-any-return]


def test_normal_app_optin_requires_https_admin_and_instance_allowlist(
    opted_client: TestClient,
) -> None:
    client = opted_client
    page = f"/instances/{INSTANCE_ID}/novnc-phase1"
    assert client.get(page).status_code == 403
    assert (
        client.get("https://127.0.0.1" + page, follow_redirects=False).status_code
        == 401
    )
    assert (
        client.post("/login", data={"password": "wrong"}, headers=ORIGIN).status_code
        == 403
    )
    response = client.post(
        "https://127.0.0.1/login",
        data={"password": conftest.PASSWORD},
        headers={"Origin": "https://127.0.0.1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "secure" in response.headers["set-cookie"].lower()
    assert client.get("https://127.0.0.1" + page).status_code == 200
    assert (
        client.get(
            "https://127.0.0.1/instances/" + str(uuid4()) + "/novnc-phase1"
        ).status_code
        == 404
    )
    # The internal binding resolver has no public endpoint or MCP capability.
    assert (
        client.get(
            "https://127.0.0.1/api/instances/" + str(INSTANCE_ID) + "/novnc-resolve"
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/instances/{INSTANCE_ID}/novnc-start",
            json={"attempt": str(uuid4()), "rate": 2},
            headers=ORIGIN,
        ).status_code
        == 403
    )


def test_broker_rejects_unlisted_or_unqualified_catalog_before_docker(
    database: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.broker.novnc_service import HumanDisplayService

    monkeypatch.setattr(
        "docker.APIClient", lambda **k: pytest.fail("unqualified Docker access")
    )
    service = HumanDisplayService(
        Settings(experimental_novnc_instances=(INSTANCE_ID,)), database
    )
    with pytest.raises(ValueError, match="not enabled"):
        service.resolve(uuid4())
    with pytest.raises(ValueError, match="not qualified"):
        service.resolve(INSTANCE_ID)


def test_unconfirmed_admission_is_reaped_only_after_native_proof(
    database: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.broker.novnc_service import HumanDisplayService

    from .conftest import REVISION_ID

    service = HumanDisplayService(Settings(runtime_root=tmp_path), database)
    binding = ViewBinding(
        "a" * 64, "sha256:" + "b" * 64, "start", (str(REVISION_ID), 0, None)
    )
    attempt = uuid4()
    service.ownership.create(INSTANCE_ID, attempt, "c" * 64, binding, "interactive")
    service.ownership.claim(
        INSTANCE_ID, attempt, "c" * 64, binding, "interactive", service.broker_id
    )
    lane = DisplayLane(
        SocketPathConfig(
            runtime_directory=tmp_path,
            socket_path=tmp_path / "unused.sock",
            directory_owner_uid=os.getuid(),
            directory_group_gid=os.getgid(),
            socket_owner_uid=os.getuid(),
            socket_group_gid=os.getgid(),
        ),
        os.getuid(),
        lambda _: pytest.fail("no admission"),
    )
    monkeypatch.setattr(service, "proof", lambda *a: False)
    service.reconcile(lane)
    assert service.ownership.status(INSTANCE_ID) is not None
    monkeypatch.setattr(service, "proof", lambda *a: True)
    lane.active.add(INSTANCE_ID)
    service.reconcile(lane)
    assert service.ownership.status(INSTANCE_ID) is not None
    lane.active.clear()
    service.reconcile(lane)
    assert service.ownership.status(INSTANCE_ID) is None
