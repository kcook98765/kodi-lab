"""Authenticated one-step HTTP progression through the real fixed adapters."""

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.create_kodi_runner import CreateKodiRunner
from kodi_lab.application.managed_bridge_trust import ManagedBridgeTrustService
from kodi_lab.auth import passwords as password_module
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.errors import ClientTransportError
from kodi_lab.config import Settings
from kodi_lab.domain import InstanceRuntimeState
from kodi_lab.domain import ManagedInstanceLifecycleOperationState as LifecycleState
from kodi_lab.domain.create_kodi import CreateOperation
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedStartAuthorizationRow,
    NovncSessionRow,
    RuntimeRecipeInstallationRow,
)
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
from kodi_lab.web.create_kodi import BASE
from kodi_lab.web.novnc import PrototypeViewer
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import finish
from tests.application.test_create_kodi_installation import schema as schema
from tests.create_kodi_api_support import WebEnvironment
from tests.runtime.test_recipe_installation import _ready
from tests.web.conftest import ORIGIN, PASSWORD, login
from tests.web.test_create_kodi_api import approval

type Web = tuple[WebEnvironment, TestClient, dict[str, str]]


@pytest.fixture
def web(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> Iterator[Web]:
    monkeypatch.setattr(
        password_module,
        "PasswordHasher",
        lambda **kwargs: PasswordHasher(
            time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=8, **kwargs
        ),
    )
    WebAuthenticationService(factory).set_password(PASSWORD)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("ordinary Create must not prepare Bridge or start screen viewing")

    monkeypatch.setattr(ManagedBridgeTrustService, "prepare", forbidden)
    monkeypatch.setattr(ManagedBridgeTrustService, "prepare_background", forbidden)
    monkeypatch.setattr(PrototypeViewer, "start", forbidden)
    environment = WebEnvironment(factory, Settings().data_root)
    with environment.client() as client:
        yield environment, client, {**ORIGIN, "X-Kodi-Lab-CSRF": login(client)}


def accept(web: Web) -> dict[str, Any]:
    env, client, headers = web
    result = client.post(BASE, json=approval(), headers=headers)
    assert result.status_code == 202, result.text
    body: dict[str, Any] = result.json()
    env.broker.parent = UUID(body["operation_id"])
    assert body["version"] == 0 and body["next_action"] == "continue"
    return body


def step(web: Web, status: dict[str, Any]) -> dict[str, Any]:
    _, client, headers = web
    response = client.post(
        status["status_url"] + "/continue",
        json={"version": status["version"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Location"] == status["status_url"]
    body: dict[str, Any] = response.json()
    assert body["operation_id"] == status["operation_id"]
    return body


def reach(web: Web, stage: str, checkpoint: str) -> dict[str, Any]:
    env, client, _ = web
    assert env.broker.parent
    status = client.get(f"{BASE}/{env.broker.parent}").json()
    for _ in range(20):
        if (status["stage"], status["checkpoint"]) == (stage, checkpoint):
            return dict(status)
        assert status["status"] != "needs_attention", status
        status = step(web, status)
    pytest.fail("fixed Create progression did not reach boundary")


def counts(env: WebEnvironment) -> tuple[int, int, int, int]:
    return (
        len(env.broker.launches),
        env.broker.provisions,
        env.broker.starts,
        len(env.transport.methods),
    )


def test_admin_csrf_origin_referer_and_host_before_execution(web: Web) -> None:
    env, client, headers = web
    status = accept(web)
    path = status["status_url"] + "/continue"
    cookies = dict(client.cookies)
    client.cookies.clear()
    assert client.post(path, content=b"not JSON").status_code == 401
    assert (
        client.post(
            path, json={"version": 0}, headers={"Authorization": "Bearer read-only-mcp"}
        ).status_code
        == 401
    )
    client.cookies.update(cookies)
    for invalid in (
        {},
        ORIGIN,
        {**headers, "X-Kodi-Lab-CSRF": "wrong"},
        {**headers, "Origin": "https://evil.invalid"},
        {
            "Referer": "https://evil.invalid/",
            "X-Kodi-Lab-CSRF": headers["X-Kodi-Lab-CSRF"],
        },
    ):
        assert (
            client.post(path, json={"version": 0}, headers=invalid).status_code == 403
        )
    assert (
        client.post(
            path, json={"version": 0}, headers={**headers, "Host": "evil.invalid"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "http://127.0.0.1" + path, json={"version": 0}, headers=headers
        ).status_code
        == 403
    )
    assert client.get(status["status_url"]).json()["version"] == 0
    assert counts(env) == (0, 0, 0, 0)
    response = client.post(
        path,
        json={"version": 0},
        headers={
            "Referer": "https://127.0.0.1/kodi",
            "X-Kodi-Lab-CSRF": headers["X-Kodi-Lab-CSRF"],
        },
    )
    assert response.status_code == 200 and response.json()["checkpoint"] == "planned"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"version": "0"},
        {"version": True},
        {"version": -1},
        {"version": 0.0},
        {"version": None},
        {"version": 0, "stage": "startup"},
        {"version": 0, "child_id": str(uuid4())},
        {"version": 0, "force": True},
        {"version": 0, "proof": "private-proof"},
        {"version": 0, "retry": True},
        {"version": 0, "container_id": "a" * 64},
    ],
)
def test_only_exact_strict_version_is_accepted(web: Web, body: dict[str, Any]) -> None:
    env, client, headers = web
    status = accept(web)
    response = client.post(
        status["status_url"] + "/continue", json=body, headers=headers
    )
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"
    assert "private-proof" not in response.text
    assert client.get(status["status_url"]).json()["version"] == 0
    assert counts(env) == (0, 0, 0, 0)


def test_bounded_body_unknown_parent_and_stale_version(web: Web) -> None:
    env, client, headers = web
    status = accept(web)
    path = status["status_url"] + "/continue"
    json_headers = {**headers, "Content-Type": "application/json"}
    for content, expected in ((b"x" * 4097, 413), (b'{"secret":"private", bad}', 422)):
        response = client.post(path, content=content, headers=json_headers)
        assert response.status_code == expected and "private" not in response.text
    assert client.post(path, content=b"{}", headers=headers).status_code == 415
    assert (
        client.post(
            path + "?force=true", json={"version": 0}, headers=headers
        ).status_code
        == 422
    )
    unknown = client.post(
        f"{BASE}/{uuid4()}/continue", json={"version": 0}, headers=headers
    )
    assert unknown.status_code == 404
    changed = step(web, status)
    stale = client.post(path, json={"version": 0}, headers=headers)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "create_stale_version"
    assert stale.json()["status_url"] == status["status_url"]
    assert client.get(status["status_url"]).json() == changed
    assert counts(env) == (0, 0, 0, 0)


def test_incomplete_stream_times_out_before_runner_execution(web: Web) -> None:
    env, client, headers = web
    status = accept(web)
    status.pop("replayed")  # Approval-only envelope field is absent from status.

    async def request() -> httpx.Response:
        async def incomplete() -> Any:
            yield b'{"version":'
            await asyncio.Event().wait()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=env.app),
            base_url="https://127.0.0.1",
            cookies=dict(client.cookies),
        ) as other:
            return await other.post(
                status["status_url"] + "/continue",
                content=incomplete(),
                headers={**headers, "Content-Type": "application/json"},
            )

    response = asyncio.run(request())
    assert response.status_code == 408
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get(status["status_url"]).json() == status
    assert counts(env) == (0, 0, 0, 0)


@pytest.mark.parametrize("cached", [False, True])
def test_one_post_one_runner_call_and_full_approved_progression(
    web: Web,
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    cached: bool,
) -> None:
    env, client, _ = web
    if cached:
        _ready(factory, generation="omega")
    original = CreateKodiRunner.continue_once
    calls = 0

    def once(
        self: CreateKodiRunner, operation_id: UUID, *, version: int
    ) -> CreateOperation:
        nonlocal calls
        calls += 1
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()  # Synchronous work belongs to a worker thread.
        return original(self, operation_id, version=version)

    monkeypatch.setattr(CreateKodiRunner, "continue_once", once)
    status = accept(web)
    for index in range(20):
        previous = calls
        status = step(web, status)
        assert calls == previous + 1
        if index == 0:
            assert status["checkpoint"] == "planned" and counts(env) == (0, 0, 0, 0)
        if status["complete"]:
            break
    assert status["complete"] and status["next_action"] == "complete"
    assert len(env.broker.launches) == (0 if cached else 1)
    assert env.broker.provisions == env.broker.starts == 1
    assert set(env.transport.methods) == {"JSONRPC.Ping", "Application.GetProperties"}
    assert client.get(status["status_url"]).json() == status
    assert (
        client.get(f"{BASE}/by-target/{status['instance']['target_id']}").json()
        == status
    )
    with factory() as session:
        for model in (NovncSessionRow, AgentWorkspaceRow, DevelopmentSessionRow):
            assert session.scalars(select(model)).all() == []
    serialized = str(status)
    op = env.repo.get(UUID(status["operation_id"]))
    assert op
    for private in (
        op.container_id,
        op.image_id,
        str(op.start_operation_id),
        str(op.provision_operation_id),
    ):
        assert private and private not in serialized


def test_active_install_worker_waits_without_launch_and_passive_get(
    web: Web, factory: sessionmaker[Session]
) -> None:
    env, client, _ = web
    status = accept(web)
    env.broker.mode = "pending"
    status = step(web, step(web, status))
    assert status["checkpoint"] == "dispatched" and status["next_action"] == "wait"
    assert status["installation_result_available"] is False
    before = counts(env)
    assert client.get(status["status_url"]).json() == status
    assert counts(env) == before
    assert step(web, status) == status and counts(env) == before
    op = env.repo.get(UUID(status["operation_id"]))
    assert op and op.installation_operation_id
    finish(factory, op.installation_operation_id)
    observed = client.get(status["status_url"]).json()
    assert observed == {**status, "installation_result_available": True}
    assert env.repo.get(op.id) == op and counts(env) == before
    assert step(web, status) == observed, "receipt cannot bypass live worker checks"
    assert env.repo.get(op.id) == op and counts(env) == before
    env.broker.workers[str(op.installation_operation_id)] = "exited"
    env.broker.operation_id = env.broker.generation = None
    confirmed = step(web, status)
    assert confirmed["checkpoint"] == "confirmed" and len(env.broker.launches) == 1


@pytest.mark.parametrize("receipt", ["active", "ready", "failed", "mismatched"])
def test_installation_receipt_hint_is_exact_and_read_only(
    web: Web,
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    receipt: str,
) -> None:
    env, client, _ = web
    status = accept(web)
    env.broker.mode = "pending"
    status = step(web, step(web, status))
    op = env.repo.get(UUID(status["operation_id"]))
    assert op and op.installation_operation_id
    if receipt in {"ready", "mismatched"}:
        finish(factory, op.installation_operation_id)
    elif receipt == "failed":
        with factory.begin() as session:
            RuntimeRecipeInstallationRepository(session).fail(
                op.installation_operation_id,
                code="installation_failed",
                now=datetime.now(UTC),
            )
    if receipt == "mismatched":
        with factory.begin() as session:
            child = session.get(
                RuntimeRecipeInstallationRow, str(op.installation_operation_id)
            )
            assert child
            child.recipe_digest = "sha256:" + "f" * 64

    before = counts(env)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("receipt GET must not inspect the worker or run a continuation")

    monkeypatch.setattr(CreateKodiRunner, "continue_once", forbidden)
    monkeypatch.setattr(env.broker, "observe_reviewed_runtime_installation", forbidden)
    statements: list[str] = []
    engine = factory.kw["bind"]

    def record(_c: Any, _cur: Any, sql: str, *_args: Any) -> None:
        statements.append(sql.lstrip())

    event.listen(engine, "before_cursor_execute", record)
    try:
        for _ in range(3):
            for url in (status["status_url"], f"{BASE}/by-target/{op.target_id}"):
                response = client.get(url)
                assert response.status_code == 200
                assert response.json() == {
                    **status,
                    "installation_result_available": receipt in {"ready", "failed"},
                }
                assert response.headers["Cache-Control"] == "no-store"
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert not [
        sql
        for sql in statements
        if sql.split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
    ]
    assert env.repo.get(op.id) == op and counts(env) == before


@pytest.mark.parametrize("stage", ["installation", "provisioning", "startup"])
def test_lost_child_response_preserves_original_identity_and_fences_mutations(
    web: Web, stage: str
) -> None:
    env, client, _ = web
    accept(web)
    status = reach(web, stage, "planned")
    if stage == "installation":
        env.broker.mode = "no_response"
    elif stage == "provisioning":
        env.broker.fault = "lost_response"
    else:
        env.broker.start_fault = "lost_response"
    fenced = step(web, status)
    assert (
        fenced["status"] == "needs_attention"
        and fenced["next_action"] == "needs_attention"
    )
    original = env.repo.get(UUID(status["operation_id"]))
    assert original and original.child_id
    before = counts(env)
    assert client.get(status["status_url"]).json() == fenced
    assert step(web, fenced) == fenced
    assert env.repo.get(original.id) == original and counts(env) == before


@pytest.mark.parametrize("stage", ["installation", "provisioning", "startup"])
def test_lost_http_result_requires_status_then_new_version_without_repeating_child(
    web: Web, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    env, client, headers = web
    accept(web)
    status = reach(web, stage, "planned")
    original = CreateKodiRunner.continue_once

    def missing(
        self: CreateKodiRunner, operation_id: UUID, *, version: int
    ) -> CreateOperation:
        original(self, operation_id, version=version)
        raise OSError("private broker proof /private/path response lost")

    with monkeypatch.context() as patch:
        patch.setattr(CreateKodiRunner, "continue_once", missing)
        response = client.post(
            status["status_url"] + "/continue",
            json={"version": status["version"]},
            headers=headers,
        )
    assert response.status_code == 503 and "private" not in response.text
    assert response.json()["status_url"] == status["status_url"]
    before = counts(env)
    saved = client.get(status["status_url"]).json()
    assert saved["checkpoint"] == "confirmed" and counts(env) == before
    stale = client.post(
        status["status_url"] + "/continue",
        json={"version": status["version"]},
        headers=headers,
    )
    assert stale.status_code == 409 and counts(env) == before
    advanced = step(web, saved)
    assert advanced["checkpoint"] == "accepted" and counts(env) == before


def test_blocked_install_does_not_block_event_loop_or_start_another_launch(
    web: Web,
) -> None:
    env, client, headers = web
    status = step(web, accept(web))
    env.broker.mode = "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        request = pool.submit(
            client.post,
            status["status_url"] + "/continue",
            json={"version": status["version"]},
            headers=headers,
        )
        try:
            assert env.broker.entered.wait(3)
            lookup = pool.submit(client.get, status["status_url"])
            pending = lookup.result(timeout=2).json()
            assert (
                pending["checkpoint"] == "dispatched"
                and pending["next_action"] == "wait"
            )
            assert len(env.broker.launches) == 1
        finally:
            env.broker.release.set()
        assert request.result(timeout=5).status_code == 200


@pytest.mark.parametrize("stage", ["provisioning", "startup"])
@pytest.mark.parametrize("fault", ["expired", "missing", "terminal"])
def test_active_lifecycle_waits_then_unsafe_ownership_is_fenced(
    web: Web,
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    fault: str,
) -> None:
    env, client, headers = web
    accept(web)
    planned = reach(web, stage, "planned")
    entered, release = Event(), Event()

    def held_dispatch(**_kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        raise ClientTransportError("fake unavailable dispatch response")

    monkeypatch.setattr(
        env.broker,
        "provision_managed_container"
        if stage == "provisioning"
        else "start_managed_container",
        held_dispatch,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(
            client.post,
            planned["status_url"] + "/continue",
            json={"version": planned["version"]},
            headers=headers,
        )
        try:
            assert entered.wait(3)
            pending = client.get(planned["status_url"]).json()
            assert pending["checkpoint"] == "dispatched"
            assert pending["next_action"] == "wait"
            before = counts(env)
            assert step(web, pending) == pending and counts(env) == before
            op = env.repo.get(UUID(pending["operation_id"]))
            assert op and op.child_id
            with factory.begin() as session:
                if fault == "expired":
                    lease = session.get(
                        ManagedInstanceMutationLeaseRow, str(op.target_id)
                    )
                    assert lease
                    lease.lease_expires_at = timestamp(
                        datetime.now(UTC) - timedelta(seconds=1)
                    )
                else:
                    child = session.get(
                        ManagedInstanceLifecycleOperationRow, str(op.child_id)
                    )
                    assert child
                    if fault == "missing":
                        auth = session.get(
                            ManagedStartAuthorizationRow, str(op.child_id)
                        )
                        if auth:
                            session.delete(auth)
                            session.flush()
                        session.delete(child)
                    else:
                        child.state, child.completed_at, child.resulting_state = (
                            LifecycleState.SUCCEEDED,
                            child.started_at,
                            "stopped",
                        )
            fenced = step(web, pending)
            assert fenced["status"] == "needs_attention"
            assert fenced["next_action"] == "needs_attention" and counts(env) == before
            assert step(web, fenced) == fenced and counts(env) == before
        finally:
            release.set()
        assert request.result(timeout=5).status_code == 200


def test_native_recheck_and_completed_replay_after_stop_are_readonly(
    web: Web, factory: sessionmaker[Session]
) -> None:
    env, client, _ = web
    accept(web)
    status = reach(web, "readiness", "planned")
    env.transport.mode = "invalid"
    waiting = step(web, status)
    assert (
        waiting["checkpoint"] == "dispatched"
        and waiting["next_action"] == "check_again"
    )
    before = counts(env)
    assert client.get(status["status_url"]).json() == waiting and counts(env) == before
    env.transport.mode = "ready"
    completed = step(web, waiting)
    assert completed["complete"] and counts(env)[:3] == before[:3]
    env.broker.records[0] = replace(
        env.broker.records[0], runtime_state=ContainerRuntimeState.STOPPED
    )
    with factory.begin() as session:
        instance = session.get(ManagedInstanceRow, completed["instance"]["target_id"])
        assert instance
        instance.runtime_state = InstanceRuntimeState.STOPPED
        instance.runtime_state_revision += 2
    before = counts(env)
    history = client.get(status["status_url"]).json()
    assert history["complete"] and history["instance"]["state"] == "stopped"
    assert step(web, history) == history and counts(env) == before


def test_reconnect_application_reconstruction_and_passive_gets(
    web: Web, factory: sessionmaker[Session]
) -> None:
    env, client, headers = web
    status = step(web, accept(web))
    saved = env.repo.get(UUID(status["operation_id"]))
    restarted = WebEnvironment(
        factory, Settings().data_root, operation_id=UUID(status["operation_id"])
    )
    with restarted.client() as other:
        other.cookies.update(dict(client.cookies))
        assert other.get(status["status_url"]).json() == status
        assert (
            other.get(f"{BASE}/by-target/{status['instance']['target_id']}").json()
            == status
        )
        assert restarted.repo.get(UUID(status["operation_id"])) == saved
        assert counts(restarted) == (0, 0, 0, 0)
        confirmed = step((restarted, other, headers), status)
        assert confirmed["checkpoint"] == "confirmed"
        assert len(restarted.broker.launches) == 1
