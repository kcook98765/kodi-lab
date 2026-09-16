from __future__ import annotations

import time
from threading import Event
from typing import cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reset_operations import DurableResetActivityObserver
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.broker.errors import BrokerUnavailableError
from kodi_lab.config import Settings
from kodi_lab.domain import InstanceType
from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.kodi.readiness import (
    JsonRpcState,
    ReadinessFailureReason,
    ReadinessResult,
    UsabilityState,
)
from kodi_lab.storage.models import ManagedInstanceRow, WebSessionRow
from kodi_lab.storage.repositories import FixtureDefinitionRepository
from kodi_lab.web.app import create_app

from .conftest import (
    CONTAINER_ID,
    INSTANCE_ID,
    ORIGIN,
    PASSWORD,
    REVISION_ID,
    FakeBroker,
    coordinator_factory,
    inventory_entry,
    login,
)


def test_protected_routes_require_authentication(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/api/instances").status_code == 401
    assert client.get(f"/api/instances/{INSTANCE_ID}/reset-status").status_code == 401
    assert client.get(f"/api/instances/{INSTANCE_ID}/snapshots").status_code == 401
    assert (
        client.post(f"/api/instances/{INSTANCE_ID}/snapshots", json={}).status_code
        == 401
    )


def _assign_v2(database: sessionmaker[Session]) -> None:
    fixture = load_packaged_fixture("small-mixed-v2").definition
    with database.begin() as session:
        FixtureDefinitionRepository(session).create(fixture)
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert instance is not None
        instance.fixture_definition_id = str(fixture.id)


def test_login_cookie_is_opaque_and_hardened(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    response = client.post(
        "/login",
        data={"password": PASSWORD},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "kodi_lab_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie
    raw_token = client.cookies.get("kodi_lab_session")
    assert raw_token is not None
    with database() as session:
        stored = session.scalar(select(WebSessionRow))
        assert stored is not None
        assert stored.token_digest != raw_token


def test_invalid_login_is_generic_and_origin_is_required(client: TestClient) -> None:
    rejected = client.post("/login", data={"password": "wrong"}, headers=ORIGIN)
    assert rejected.status_code == 401
    assert "Authentication failed." in rejected.text
    assert "wrong" not in rejected.text
    assert client.post("/login", data={"password": PASSWORD}).status_code == 403
    assert (
        client.post(
            "/login",
            data={"password": PASSWORD},
            headers={"Referer": "http://127.0.0.1/login"},
            follow_redirects=False,
        ).status_code
        == 303
    )


def test_login_rotates_existing_session(client: TestClient) -> None:
    login(client)
    first = client.cookies.get("kodi_lab_session")
    response = client.post(
        "/login",
        data={"password": PASSWORD},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    second = client.cookies.get("kodi_lab_session")
    assert first is not None and second is not None and first != second


def test_main_page_autoescapes_and_has_local_assets(client: TestClient) -> None:
    login(client)
    page = client.get("/")
    assert "&lt;script&gt;not markup&lt;/script&gt;" in page.text
    assert "https://" not in page.text
    assert "Content-Security-Policy" in page.headers
    assert "Fixture" in page.text
    assert "None" in page.text
    assert (
        "Reset recreates this exact runtime revision; it does not upgrade Kodi."
        in page.text
    )


def test_authenticated_reset_status_presents_fixture_exact_pin_and_safe_state(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
) -> None:
    _assign_v2(database)
    login(client)
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    response = client.get(f"/api/instances/{INSTANCE_ID}/reset-status")
    assert response.status_code == 200
    body = response.json()
    assert body["eligibility"] == "supported"
    assert body["fixture"] == {
        "assignment": "assigned",
        "compatibility": "compatible",
        "slug": "small-mixed-v2",
        "display_name": "Small Mixed",
        "fixture_version": 2,
        "size_category": "small",
        "content_category": "mixed",
        "compatible_kodi_release": "21.3",
        "baseline_note": (
            "Populated Kodi 21.3 reset baseline proven for this fixture version."
        ),
    }
    assert body["pinned_runtime_revision_id"] == str(REVISION_ID)
    assert body["pinned_runtime_revision_number"] == 2
    assert body["pinned_kodi_release"] == "21.3"
    assert body["container_state"] == "running"
    serialized = response.text
    for forbidden in ("container_id", "image_id", "filesystem", "evidence_reference"):
        assert forbidden not in serialized
    page = client.get("/")
    assert "Small Mixed v2 (small, mixed)" in page.text
    assert "Reset is available for this disposable instance." in page.text
    assert "does not upgrade Kodi" in page.text


def test_reset_activity_failure_and_reconciliation_are_read_only(
    client: TestClient,
) -> None:
    login(client)
    registry = cast(FastAPI, client.app).state.kodi_lab.reset_activity
    active = registry.begin(INSTANCE_ID)
    registry.transition(INSTANCE_ID, active.generation, "provisioning")
    current = client.get(f"/api/instances/{INSTANCE_ID}/reset-status").json()
    assert current["eligibility_reason"] == "reset_in_progress"
    assert current["activity_state"] == "provisioning"
    assert "provisioning" in client.get("/").text
    registry.fail(
        INSTANCE_ID,
        active.generation,
        failure_phase="final_stop",
        failure_reason="The fresh container could not be safely stopped.",
        reconciliation_required=True,
    )
    failed = client.get(f"/api/instances/{INSTANCE_ID}/reset-status").json()
    assert failed["eligibility_reason"] == "reconciliation_required"
    assert failed["reconciliation_required"] is True
    assert failed["failure_phase"] == "final_stop"
    failed_page = client.get("/").text
    assert "Reconciliation is required" in failed_page
    assert "The fresh container could not be safely stopped." in failed_page


def test_persistent_reset_status_is_unsupported_and_reset_requires_confirmation(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    csrf = login(client)
    confirmation = client.get(f"/instances/{INSTANCE_ID}/reset")
    assert confirmation.status_code == 200
    with database.begin() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert instance is not None
        instance.instance_type = InstanceType.PERSISTENT
    status_response = client.get(f"/api/instances/{INSTANCE_ID}/reset-status")
    assert status_response.json()["eligibility_reason"] == "persistent_instance"
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/reset",
        json={"confirmed": True},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 409


def test_reset_confirmation_and_authenticated_csrf_protected_queue(
    client: TestClient,
) -> None:
    assert (
        client.get(
            f"/instances/{INSTANCE_ID}/reset", follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/api/instances/{INSTANCE_ID}/reset", json={"confirmed": True}
        ).status_code
        == 401
    )
    csrf = login(client)
    confirmation = client.get(f"/instances/{INSTANCE_ID}/reset")
    assert confirmation.status_code == 200
    for expected in (
        "Kodi 21.3",
        "revision 2",
        "discards changes",
        "will not be upgraded",
        "provisioned and stopped",
        "Fixture",
    ):
        assert expected in confirmation.text
    route = f"/api/instances/{INSTANCE_ID}/reset"
    assert (
        client.post(route, json={"confirmed": True}, headers=ORIGIN).status_code == 403
    )
    assert (
        client.post(
            route,
            json={"confirmed": True},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": "x" * 43},
        ).status_code
        == 403
    )
    assert (
        client.post(
            route,
            json={"confirmed": True},
            headers={"Origin": "http://attacker.invalid", "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 403
    )
    injected = client.post(
        route,
        json={
            "confirmed": True,
            "runtime_revision_id": str(uuid4()),
            "fixture_id": str(uuid4()),
            "container_id": "a" * 64,
            "image": "attacker",
            "command": "id",
        },
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert injected.status_code == 422
    accepted = client.post(
        route,
        json={"confirmed": True},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert accepted.status_code == 202
    body = accepted.json()
    assert body["created"] is True
    assert body["operation"]["managed_instance_id"] == str(INSTANCE_ID)
    assert "container" not in accepted.text
    assert "revision" not in accepted.text


class BlockingResetService:
    def __init__(
        self,
        observer: DurableResetActivityObserver,
        started: Event,
        release: Event,
        calls: list[UUID],
    ) -> None:
        self._observer = observer
        self._started = started
        self._release = release
        self._calls = calls

    def reset(self, instance_id: UUID) -> object:
        self._calls.append(instance_id)
        activity = self._observer.begin(instance_id)
        self._started.set()
        assert self._release.wait(5)
        self._observer.succeed(instance_id, activity.generation)
        return object()


def test_duplicate_reset_returns_existing_and_lifecycle_is_blocked(
    database: sessionmaker[Session], broker: FakeBroker
) -> None:
    auth = WebAuthenticationService(database)
    auth.set_password(PASSWORD)
    started, release = Event(), Event()
    calls: list[UUID] = []
    app = create_app(
        settings=Settings(),
        session_factory=database,
        broker=broker,
        auth=auth,
        reset_service_factory=lambda observer: BlockingResetService(
            observer, started, release, calls
        ),
    )
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        csrf = login(test_client)
        headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
        route = f"/api/instances/{INSTANCE_ID}/reset"
        first = test_client.post(route, json={"confirmed": True}, headers=headers)
        assert first.status_code == 202
        assert started.wait(2)
        second = test_client.post(route, json={"confirmed": True}, headers=headers)
        assert second.status_code == 202
        assert second.json()["created"] is False
        assert (
            second.json()["operation"]["operation_id"]
            == first.json()["operation"]["operation_id"]
        )
        for action in ("provision", "start", "stop", "deprovision"):
            lifecycle = test_client.post(
                f"/api/instances/{INSTANCE_ID}/{action}", json={}, headers=headers
            )
            assert lifecycle.status_code == 409
            assert lifecycle.json()["error"]["code"] == "reset_operation_conflict"
        status_body = test_client.get(
            f"/api/instances/{INSTANCE_ID}/reset-status"
        ).json()
        assert status_body["operation"]["state"] == "running"
        assert (
            not {
                "lease_token_digest",
                "lease_expires_at",
                "token",
            }
            & status_body["operation"].keys()
        )
        release.set()
        for _ in range(100):
            status_body = test_client.get(
                f"/api/instances/{INSTANCE_ID}/reset-status"
            ).json()
            if status_body["operation"]["state"] == "succeeded":
                break
            time.sleep(0.01)
        assert (
            test_client.post(
                f"/api/instances/{INSTANCE_ID}/provision",
                json={},
                headers=headers,
            ).status_code
            == 200
        )
    assert calls == [INSTANCE_ID]


def test_status_models_unprovisioned_stopped_running_and_broker_failure(
    client: TestClient, broker: FakeBroker
) -> None:
    login(client)
    unprovisioned = client.get("/api/instances").json()[0]
    assert unprovisioned["provision_state"] == "unprovisioned"
    assert unprovisioned["allowed_actions"] == ["provision"]

    broker.container = inventory_entry(ContainerRuntimeState.STOPPED)
    stopped = client.get(f"/api/instances/{INSTANCE_ID}").json()
    assert stopped["container_state"] == "stopped"
    assert stopped["process_state"] == "not_running"
    assert stopped["allowed_actions"] == ["start", "deprovision"]

    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    broker.process_state = KodiProcessState.STARTING
    running = client.get(f"/api/instances/{INSTANCE_ID}").json()
    assert running["container_state"] == "running"
    assert running["process_state"] == "starting"
    assert running["jsonrpc_state"] == "not_checked"

    broker.inventory_error = BrokerUnavailableError("secret internal path")
    unavailable = client.get(f"/api/instances/{INSTANCE_ID}").json()
    assert unavailable["failure_code"] == "broker_unavailable"
    assert unavailable["failure_detail"] == "The runtime service is unavailable."
    assert "secret internal path" not in str(unavailable)


def test_status_list_handles_no_managed_instances(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    login(client)
    with database.begin() as session:
        row = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert row is not None
        session.delete(row)
    assert client.get("/api/instances").json() == []


def test_status_exposes_bounded_readiness_failure_states(
    client: TestClient, broker: FakeBroker
) -> None:
    login(client)
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    registry = cast(FastAPI, client.app).state.kodi_lab.readiness

    timeout = ReadinessResult(
        container_state=ContainerRuntimeState.RUNNING,
        process_state=KodiProcessState.RUNNING,
        jsonrpc_state=JsonRpcState.UNAVAILABLE,
        usability_state=UsabilityState.NOT_USABLE,
        failure_reason=ReadinessFailureReason.READINESS_TIMEOUT,
        last_observed_failure=ReadinessFailureReason.JSONRPC_UNREACHABLE,
        identity=None,
        elapsed_seconds=90,
        attempts=90,
    )
    registry.start(INSTANCE_ID, lambda _cancelled: timeout)
    for _ in range(50):
        timed_out = client.get(f"/api/instances/{INSTANCE_ID}").json()
        if timed_out["readiness_state"] == "timed_out":
            break
        time.sleep(0.01)
    assert timed_out["jsonrpc_state"] == "unavailable"
    assert timed_out["failure_code"] == "readiness_timeout"
    assert "jsonrpc unreachable" in timed_out["failure_detail"]

    protocol = ReadinessResult(
        container_state=ContainerRuntimeState.RUNNING,
        process_state=KodiProcessState.RUNNING,
        jsonrpc_state=JsonRpcState.PROTOCOL_ERROR,
        usability_state=UsabilityState.NOT_USABLE,
        failure_reason=ReadinessFailureReason.JSONRPC_INVALID_RESPONSE,
        last_observed_failure=ReadinessFailureReason.JSONRPC_INVALID_RESPONSE,
        identity=None,
        elapsed_seconds=1,
        attempts=1,
    )
    registry.start(INSTANCE_ID, lambda _cancelled: protocol)
    for _ in range(50):
        failed = client.get(f"/api/instances/{INSTANCE_ID}").json()
        if failed["readiness_state"] == "failed":
            break
        time.sleep(0.01)
    assert failed["jsonrpc_state"] == "protocol_error"
    assert failed["failure_code"] == "jsonrpc_invalid_response"


def test_lifecycle_uses_only_fresh_trusted_preconditions(
    client: TestClient, broker: FakeBroker
) -> None:
    csrf = login(client)
    mutation_headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    provisioned = client.post(
        f"/api/instances/{INSTANCE_ID}/provision",
        json={},
        headers=mutation_headers,
    )
    assert provisioned.status_code == 200, provisioned.text
    assert broker.calls[-1] == (
        "provision",
        {"managed_instance_id": str(INSTANCE_ID)},
    )

    started = client.post(
        f"/api/instances/{INSTANCE_ID}/start", json={}, headers=mutation_headers
    )
    assert started.status_code == 200
    assert str(broker.calls[-1][0]) == "start"
    assert broker.calls[-1][1]["expected_container_id"] == CONTAINER_ID
    assert broker.calls[-1][1]["expected_runtime_state"] == "stopped"
    for _ in range(50):
        current = client.get(f"/api/instances/{INSTANCE_ID}").json()
        if current["usability_state"] == "usable":
            break
        time.sleep(0.01)
    assert current["jsonrpc_state"] == "responsive"

    stopped = client.post(
        f"/api/instances/{INSTANCE_ID}/stop", json={}, headers=mutation_headers
    )
    assert stopped.status_code == 200
    assert str(broker.calls[-1][0]) == "stop"
    assert broker.calls[-1][1]["expected_runtime_state"] == "running"
    assert stopped.json()["readiness_state"] == "cancelled"

    removed = client.post(
        f"/api/instances/{INSTANCE_ID}/deprovision",
        json={},
        headers=mutation_headers,
    )
    assert removed.status_code == 200
    assert str(broker.calls[-1][0]) == "deprovision"
    assert broker.calls[-1][1]["expected_runtime_revision_id"] == str(REVISION_ID)
    assert broker.calls[-1][1]["expected_container_id"] == CONTAINER_ID
    assert removed.json()["readiness_state"] == "idle"


def test_start_without_provisioning_is_a_bounded_conflict(client: TestClient) -> None:
    csrf = login(client)
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/start",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "lifecycle_conflict",
            "message": "managed runtime is not provisioned",
        }
    }


def test_mutations_reject_csrf_cross_origin_and_browser_docker_fields(
    client: TestClient, broker: FakeBroker
) -> None:
    csrf = login(client)
    route = f"/api/instances/{INSTANCE_ID}/provision"
    assert client.post(route, json={}, headers=ORIGIN).status_code == 403
    assert (
        client.post(
            route,
            json={},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": "x" * 43},
        ).status_code
        == 403
    )
    assert (
        client.post(
            route,
            json={},
            headers={
                "Origin": "http://attacker.invalid",
                "X-Kodi-Lab-CSRF": csrf,
            },
        ).status_code
        == 403
    )
    injected = client.post(
        route,
        json={
            "container_id": "f" * 64,
            "runtime_revision_id": str(uuid4()),
            "image": "attacker",
            "network": "host",
            "command": "id",
        },
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert injected.status_code == 422
    assert broker.calls == []


def test_unknown_and_invalid_instance_identifiers(client: TestClient) -> None:
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    assert client.get("/api/instances/not-a-uuid").status_code == 422
    response = client.post(
        f"/api/instances/{uuid4()}/provision", json={}, headers=headers
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "managed_instance_not_found"


def test_logout_requires_csrf_and_invalidates_cookie(client: TestClient) -> None:
    csrf = login(client)
    assert client.post("/logout", data={}, headers=ORIGIN).status_code == 403
    response = client.post(
        "/logout",
        data={"csrf_token": csrf},
        headers=ORIGIN,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/api/instances").status_code == 401


def test_cors_is_not_enabled(client: TestClient) -> None:
    login(client)
    response = client.options(
        "/api/instances",
        headers={
            "Origin": "http://attacker.invalid",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in response.headers


def test_unapproved_host_header_is_rejected(client: TestClient) -> None:
    assert client.get("/login", headers={"Host": "attacker.invalid"}).status_code == 400


def test_unconfigured_credentials_have_no_browser_bootstrap_route(
    database: sessionmaker[Session], broker: FakeBroker
) -> None:
    app = create_app(
        settings=Settings(),
        session_factory=database,
        broker=broker,
        coordinator_factory=coordinator_factory,
    )
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        page = test_client.get("/login")
        assert page.status_code == 200
        assert "kodi-lab-admin set-password" in page.text
        response = test_client.post(
            "/login", data={"password": "anything"}, headers=ORIGIN
        )
        assert response.status_code == 401
        assert test_client.post("/signup", headers=ORIGIN).status_code == 404


def test_snapshot_create_requires_csrf_origin_and_identity_only_body(
    client: TestClient, broker: FakeBroker
) -> None:
    csrf = login(client)
    route = f"/api/instances/{INSTANCE_ID}/snapshots"
    assert client.post(route, json={}).status_code == 403
    assert (
        client.post(
            route,
            json={},
            headers={
                "Origin": "http://attacker.invalid",
                "X-Kodi-Lab-CSRF": csrf,
            },
        ).status_code
        == 403
    )
    injected = client.post(
        route,
        json={
            "label": "safe",
            "container_id": CONTAINER_ID,
            "runtime_revision_id": str(REVISION_ID),
            "fixture": "attacker",
            "path": "/tmp/attacker",
            "archive": "bytes",
            "snapshot_id": str(uuid4()),
            "tar_flags": ["--absolute-names"],
        },
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert injected.status_code == 422
    assert broker.calls == []


def test_snapshot_create_returns_202_and_exposes_only_safe_status(
    client: TestClient,
) -> None:
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/snapshots",
        json={"label": "  safe label  "},
        headers=headers,
    )
    assert response.status_code == 202
    document = response.json()
    assert set(document) == {"snapshot_id", "operation"}
    assert document["operation"]["state"] in {"queued", "running", "failed"}
    forbidden = {"path", "members", "archive", "manifest", "container_id"}
    serialized = str(document).lower()
    assert all(value not in serialized for value in forbidden)
    operation_id = document["operation"]["operation_id"]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status_response = client.get(
            f"/api/instances/{INSTANCE_ID}/snapshot-create-operations/{operation_id}"
        )
        assert status_response.status_code == 200
        if status_response.json()["state"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    listing = client.get(f"/api/instances/{INSTANCE_ID}/snapshots")
    assert listing.status_code == 200
    item = listing.json()[0]
    assert item["label"] == "safe label"
    assert item["runtime_revision_id"] == str(REVISION_ID)
    assert not forbidden.intersection(item)


def test_snapshot_html_escapes_labels_and_no_content_routes_exist(
    client: TestClient,
) -> None:
    csrf = login(client)
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/snapshots",
        json={"label": "<script>alert(1)<script>"},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 202
    snapshot_id = response.json()["snapshot_id"]
    page = client.get(f"/instances/{INSTANCE_ID}/snapshots")
    assert page.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;script&gt;" in page.text
    assert "<script>alert(1)<script>" not in page.text
    for suffix in ("download", "restore", "delete", "manifest"):
        assert client.get(f"/api/snapshots/{snapshot_id}/{suffix}").status_code == 404
        assert (
            client.post(
                f"/api/snapshots/{snapshot_id}/{suffix}",
                json={},
                headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
            ).status_code
            == 404
        )
