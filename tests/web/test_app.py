from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.reset_operations import DurableResetActivityObserver
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.broker.errors import (
    BrokerUnavailableError,
    RuntimeRevisionPreconditionClientError,
)
from kodi_lab.broker.operations import ReconstructionDestinationResult
from kodi_lab.config import Settings
from kodi_lab.domain import (
    InstanceRuntimeState,
    InstanceType,
    Snapshot,
    SnapshotSourceBinding,
    SnapshotState,
    SnapshotValidationResult,
)
from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.kodi.readiness import (
    JsonRpcState,
    ReadinessFailureReason,
    ReadinessResult,
    UsabilityState,
)
from kodi_lab.storage.models import (
    ManagedInstanceDeleteOperationRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ResetOperationRow,
    SnapshotDeleteOperationRow,
    SnapshotRow,
    TargetRow,
    WebSessionRow,
)
from kodi_lab.storage.repositories import (
    FixtureDefinitionRepository,
    SnapshotRepository,
)
from kodi_lab.web.app import create_app

from .conftest import (
    CONTAINER_ID,
    IMAGE_ID,
    INSTANCE_ID,
    ORIGIN,
    PASSWORD,
    REVISION_ID,
    UNRELATED_INSTANCE_ID,
    FakeBroker,
    coordinator_factory,
    guard_targeted_status_isolation,
    inventory_entry,
    login,
)


def test_protected_routes_require_authentication(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/api/instances").status_code == 401
    assert client.get(f"/api/instances/{INSTANCE_ID}/reset-status").status_code == 401
    assert (
        client.get(
            f"/api/instances/{INSTANCE_ID}/reconstruction-eligibility"
        ).status_code
        == 401
    )
    assert (
        client.post(
            f"/api/instances/{INSTANCE_ID}/reconstruct",
            json={"confirmed": True, "observation_token": "a" * 64},
        ).status_code
        == 401
    )
    assert client.get(f"/api/instances/{INSTANCE_ID}/snapshots").status_code == 401
    assert client.get(f"/api/instances/{INSTANCE_ID}/reconciliation").status_code == 401
    assert client.get(f"/api/snapshots/{uuid4()}/reconciliation").status_code == 401
    assert client.get("/snapshots/preserved", follow_redirects=False).status_code == 303
    assert client.get("/api/snapshots/preserved").status_code == 401
    assert (
        client.post(
            f"/api/instances/{INSTANCE_ID}/delete",
            json={"confirmed": True, "preserve_snapshots": True},
        ).status_code
        == 401
    )
    assert (
        client.post(f"/api/instances/{INSTANCE_ID}/snapshots", json={}).status_code
        == 401
    )
    snapshot_id = uuid4()
    assert (
        client.get(
            f"/instances/{INSTANCE_ID}/snapshots/{snapshot_id}/restore",
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/api/instances/{INSTANCE_ID}/snapshots/{snapshot_id}/restore",
            json={"confirmed": True},
        ).status_code
        == 401
    )
    assert (
        client.get(
            f"/instances/{INSTANCE_ID}/snapshots/{snapshot_id}/delete",
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/api/instances/{INSTANCE_ID}/snapshots/{snapshot_id}/delete",
            json={"confirmed": True},
        ).status_code
        == 401
    )


def test_read_only_reconciliation_warning_is_bounded_and_blocks_mutation(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
) -> None:
    csrf = login(client)
    with database.begin() as session:
        session.add(
            ResetOperationRow(
                id=str(uuid4()),
                managed_instance_id=str(INSTANCE_ID),
                requested_at="2026-09-16T00:00:00.000000Z",
                started_at="2026-09-16T00:00:00.000000Z",
                heartbeat_at="2026-09-16T00:00:01.000000Z",
                completed_at="2026-09-16T00:00:01.000000Z",
                state="reconciliation_required",
                phase="deprovisioning",
                failure_reason=(
                    "raw broker failure container deadbeef path /srv/private"
                ),
                reconciliation_required=True,
            )
        )

    response = client.get(f"/api/instances/{INSTANCE_ID}/reconciliation")
    assert response.status_code == 200
    document = response.json()
    assert document["operation_kind"] == "reset"
    assert document["last_phase"] == "deprovisioning"
    assert document["observed_state"] == "unprovisioned"
    assert "provision" in document["blocked_actions"]
    serialized = response.text
    assert "deadbeef" not in serialized
    assert "/srv/private" not in serialized
    assert "lease" not in serialized

    page = client.get("/")
    assert page.status_code == 200
    assert "Operator reconciliation required" in page.text
    assert "Automatic destructive work will not resume" in page.text
    assert 'aria-disabled="true">Delete blocked' in page.text
    assert f'href="/instances/{INSTANCE_ID}/reset"' not in page.text
    assert "raw broker failure" not in page.text
    assert "Finalize" not in page.text
    assert "Acknowledge" not in page.text

    blocked = client.post(
        f"/api/instances/{INSTANCE_ID}/provision",
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == (
        "managed_instance_reconciliation_required"
    )
    assert "lease" not in blocked.text
    assert not any(call[0] == "provision" for call in broker.calls)
    assert (
        client.post(f"/api/instances/{INSTANCE_ID}/reconciliation", json={}).status_code
        == 405
    )


def _assign_v2(database: sessionmaker[Session]) -> None:
    fixture = load_packaged_fixture("small-mixed-v2").definition
    with database.begin() as session:
        FixtureDefinitionRepository(session).create(fixture)
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert instance is not None
        instance.fixture_definition_id = str(fixture.id)


def _seed_available_snapshot(database: sessionmaker[Session]) -> UUID:
    snapshot_id = uuid4()
    with database.begin() as session:
        SnapshotRepository(session).create(
            Snapshot(
                id=snapshot_id,
                source_managed_instance_id=INSTANCE_ID,
                runtime_revision_id=REVISION_ID,
                captured_fixture_definition_id=None,
                captured_at=datetime(2026, 9, 16, tzinfo=UTC),
                prior_runtime_state=InstanceRuntimeState.STOPPED,
                label="safe restore",
                manifest_version=1,
                archive_digest="sha256:" + "1" * 64,
                manifest_digest="sha256:" + "2" * 64,
                archive_size=10240,
                member_count=3,
                state=SnapshotState.AVAILABLE,
                validation_at=datetime(2026, 9, 16, tzinfo=UTC),
                validation_result=SnapshotValidationResult.PASSED,
            )
        )
    return snapshot_id


def _seed_snapshot_artifact(data_root: Path, snapshot_id: UUID) -> None:
    snapshots_root = data_root / "snapshots"
    snapshots_root.mkdir(parents=True, mode=0o750)
    snapshots_root.chmod(0o750)
    snapshot_root = snapshots_root / str(snapshot_id)
    snapshot_root.mkdir(mode=0o750)
    (snapshot_root / "manifest.json").write_text("{}", encoding="utf-8")
    (snapshot_root / "state.tar").write_bytes(b"test archive")
    snapshot_root.chmod(0o750)


def _wait_for_instance_delete(
    client: TestClient, operation_id: str
) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/instance-delete-operations/{operation_id}")
        assert response.status_code == 200
        document = response.json()
        if document["state"] not in {"queued", "running"}:
            return cast(dict[str, object], document)
        time.sleep(0.01)
    raise AssertionError("instance deletion did not complete")


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


def test_reconstruction_is_admin_confirmed_fenced_and_readable(
    client: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_revision = uuid4()
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED).model_copy(
        update={"runtime_revision_id": str(old_revision)}
    )
    monkeypatch.setattr(
        broker,
        "reconstruction_destination",
        lambda *, managed_instance_id: ReconstructionDestinationResult(
            managed_instance_id=managed_instance_id,
            desired_runtime_revision_id=str(REVISION_ID),
            desired_image_id=IMAGE_ID,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        client.app.state.kodi_lab.reconstruction_executor,
        "submit",
        lambda _operation_id: None,
    )
    csrf = login(client)
    path = f"/api/instances/{INSTANCE_ID}/reconstruct"
    eligibility = client.get(
        f"/api/instances/{INSTANCE_ID}/reconstruction-eligibility"
    ).json()
    assert eligibility["eligible"]
    token = eligibility["observation_token"]
    page = client.get(f"/instances/{INSTANCE_ID}/reconstruct")
    assert page.status_code == 200
    for warning in (
        "discards the current container writable layer",
        "does not automatically create a safety snapshot",
        "persisted approved runtime pin",
        "Preserve files externally",
    ):
        assert warning in page.text
    assert CONTAINER_ID not in page.text
    request = {"confirmed": True, "observation_token": token}
    assert client.post(path, json=request, headers=ORIGIN).status_code == 403
    assert (
        client.post(
            path,
            json=request,
            headers={"Origin": "http://attacker.invalid", "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 403
    )
    assert (
        client.post(
            path,
            json=request | {"container_id": CONTAINER_ID},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 422
    )
    broker.container = broker.container.model_copy(update={"container_id": "f" * 64})
    assert (
        client.post(
            path, json=request, headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
        ).status_code
        == 409
    )
    assert broker.calls == []
    broker.container = broker.container.model_copy(
        update={"container_id": CONTAINER_ID}
    )
    accepted = client.post(
        path, json=request, headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    )
    assert accepted.status_code == 202
    status_url = accepted.json()["status_url"]
    assert client.get(status_url).json()["state"] == "queued"
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert 'data-reconstruction-state="queued"' in dashboard.text
    assert "Runtime reconstruction" in dashboard.text
    assert broker.calls == []


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


def test_targeted_web_status_isolates_persistent_neighbor(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login(client)
    observed = guard_targeted_status_isolation(database, broker, client, monkeypatch)
    response = client.get(f"/api/instances/{INSTANCE_ID}")
    assert response.status_code == 200
    assert response.json()["managed_instance_id"] == str(INSTANCE_ID)
    assert response.json()["usability_state"] == "usable"
    assert observed == [str(INSTANCE_ID)]
    with database() as session:
        own = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        own_target = session.get(TargetRow, str(INSTANCE_ID))
        other = session.get(ManagedInstanceRow, str(UNRELATED_INSTANCE_ID))
        other_target = session.get(TargetRow, str(UNRELATED_INSTANCE_ID))
        assert own is not None and own_target is not None
        assert other is not None and other_target is not None
        assert own.runtime_state_revision == other.runtime_state_revision == 0
        assert own.runtime_state.value == "running"
        assert (
            own_target.availability.value,
            own_target.health.value,
            own_target.connection_state.value,
        ) == ("available", "healthy", "connected")
        assert other.runtime_state.value == "stopped"
        assert (
            other_target.availability.value,
            other_target.health.value,
            other_target.connection_state.value,
        ) == ("unavailable", "error", "disconnected")
        assert (
            session.scalar(
                select(func.count()).select_from(ManagedInstanceLifecycleOperationRow)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(ManagedInstanceMutationLeaseRow)
            )
            == 0
        )


def test_running_instance_recovers_readiness_in_fresh_web_process(
    client: TestClient, broker: FakeBroker
) -> None:
    login(client)
    broker.container = inventory_entry(ContainerRuntimeState.RUNNING)
    broker.process_state = KodiProcessState.RUNNING
    first = client.get(f"/api/instances/{INSTANCE_ID}").json()
    assert first["readiness_state"] in {"checking", "usable"}
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        current = client.get(f"/api/instances/{INSTANCE_ID}").json()
        if current["readiness_state"] == "usable":
            break
        time.sleep(0.01)
    assert current["readiness_state"] == "usable"
    assert current["usability_state"] == "usable"


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
    client: TestClient, broker: FakeBroker, database: sessionmaker[Session]
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
    with database() as session:
        operations = list(
            session.scalars(
                select(ManagedInstanceLifecycleOperationRow).where(
                    ManagedInstanceLifecycleOperationRow.original_managed_instance_id
                    == str(INSTANCE_ID)
                )
            )
        )
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None
    by_kind = {operation.kind.value: operation for operation in operations}
    assert set(by_kind) == {"provision", "start", "stop", "deprovision"}
    assert (
        by_kind["provision"].prior_state.value,
        by_kind["provision"].resulting_state,
    ) == (
        "unprovisioned",
        "stopped",
    )
    assert by_kind["start"].prior_state.value == "stopped"
    assert by_kind["start"].resulting_state in {
        "running_not_ready",
        "running_usable",
    }
    assert by_kind["stop"].resulting_state == "stopped"
    assert (
        by_kind["deprovision"].prior_state.value,
        by_kind["deprovision"].resulting_state,
    ) == (
        "stopped",
        "unprovisioned",
    )
    assert all(operation.state.value == "succeeded" for operation in operations)


def test_start_without_provisioning_is_a_bounded_conflict(
    client: TestClient, database: sessionmaker[Session]
) -> None:
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
    with database() as session:
        operation = session.scalar(select(ManagedInstanceLifecycleOperationRow))
    assert operation is not None
    assert operation.kind.value == "start"
    assert operation.prior_state.value == "unprovisioned"
    assert operation.resulting_state == "unprovisioned"
    assert operation.state.value == "failed"
    assert operation.failure_reason == "managed runtime is not provisioned"


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


def test_remote_container_host_origin_and_mcp_policy(
    database: sessionmaker[Session], broker: FakeBroker
) -> None:
    auth = WebAuthenticationService(database)
    auth.set_password(PASSWORD)
    app = create_app(
        settings=Settings(web_bind_host="0.0.0.0", web_allowed_hosts=("lab.example",)),
        session_factory=database,
        broker=broker,
        coordinator_factory=coordinator_factory,
        auth=auth,
    )
    with TestClient(app, base_url="http://lab.example:18100") as test_client:
        assert test_client.get("/login").status_code == 200
        assert (
            test_client.get("/login", headers={"Host": "attacker.invalid"}).status_code
            == 400
        )
        assert (
            test_client.post(
                "/login",
                data={"password": PASSWORD},
                headers={"Origin": "http://attacker.invalid"},
            ).status_code
            == 403
        )
        assert (
            test_client.post(
                "/login",
                data={"password": PASSWORD},
                headers={"Origin": "http://lab.example:18100"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert test_client.get("/pair-kodi").status_code == 200
        assert (
            test_client.post(
                "/mcp",
                headers={
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/list",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            ).status_code
            == 401
        )


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


def test_snapshot_create_rejects_known_stopped_revision_mismatch(
    client: TestClient, broker: FakeBroker
) -> None:
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED).model_copy(
        update={"runtime_revision_id": str(uuid4())}
    )
    csrf = login(client)
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/snapshots",
        json={"label": "must not queue"},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "snapshot_create_conflict"
    assert client.get(f"/api/instances/{INSTANCE_ID}/snapshots").json() == []
    page = client.get(f"/instances/{INSTANCE_ID}/snapshots/new")
    assert page.status_code == 200
    assert 'id="confirm-snapshot"' in page.text
    assert "disabled" in page.text


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


def test_restore_confirmation_is_authenticated_safe_and_destructive(
    client: TestClient,
    database: sessionmaker[Session],
) -> None:
    login(client)
    snapshot_id = _seed_available_snapshot(database)
    page = client.get(f"/instances/{INSTANCE_ID}/snapshots/{snapshot_id}/restore")
    assert page.status_code == 200
    assert "permanently discards" in page.text
    assert "No automatic backup is created" in page.text
    assert "Exact match" in page.text
    assert "archive" not in page.text.lower()
    assert "/var/" not in page.text


def test_restore_post_requires_csrf_origin_and_rejects_extra_authority(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
) -> None:
    csrf = login(client)
    snapshot_id = _seed_available_snapshot(database)
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED)
    route = f"/api/instances/{INSTANCE_ID}/snapshots/{snapshot_id}/restore"
    assert client.post(route, json={"confirmed": True}).status_code == 403
    assert (
        client.post(
            route,
            json={"confirmed": True},
            headers={
                "Origin": "http://attacker.invalid",
                "X-Kodi-Lab-CSRF": csrf,
            },
        ).status_code
        == 403
    )
    response = client.post(
        route,
        json={
            "confirmed": True,
            "runtime_revision_id": str(REVISION_ID),
            "container_id": CONTAINER_ID,
            "fixture": None,
            "path": "/tmp/attacker",
            "archive": "bytes",
        },
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 422


def test_restore_post_returns_202_with_bounded_status(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
) -> None:
    csrf = login(client)
    snapshot_id = _seed_available_snapshot(database)
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED)
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/snapshots/{snapshot_id}/restore",
        json={"confirmed": True},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 202
    document = response.json()
    assert document["snapshot_id"] == str(snapshot_id)
    assert set(document) == {"snapshot_id", "operation"}
    assert not {"path", "archive", "manifest", "members", "container_id"}.intersection(
        document["operation"]
    )
    operation_id = document["operation"]["operation_id"]
    status_response = client.get(
        f"/api/instances/{INSTANCE_ID}/snapshot-restore-operations/{operation_id}"
    )
    assert status_response.status_code == 200
    assert status_response.json()["snapshot_id"] == str(snapshot_id)
    assert (
        client.post(
            f"/api/snapshots/{snapshot_id}/delete",
            json={},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 404
    )


def test_delete_confirmation_is_authenticated_safe_and_permanent(
    client: TestClient,
    database: sessionmaker[Session],
) -> None:
    login(client)
    snapshot_id = _seed_available_snapshot(database)
    page = client.get(f"/instances/{INSTANCE_ID}/snapshots/{snapshot_id}/delete")
    assert page.status_code == 200
    assert "Deletion is permanent" in page.text
    assert "can no longer be restored" in page.text
    assert "does not alter the currently running or stopped Kodi instance" in page.text
    assert "manifest.json" not in page.text
    assert "/var/" not in page.text


def test_delete_post_requires_csrf_origin_and_rejects_extra_authority(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
) -> None:
    csrf = login(client)
    snapshot_id = _seed_available_snapshot(database)
    route = f"/api/instances/{INSTANCE_ID}/snapshots/{snapshot_id}/delete"
    assert client.post(route, json={"confirmed": True}).status_code == 403
    assert (
        client.post(
            route,
            json={"confirmed": True},
            headers={
                "Origin": "http://attacker.invalid",
                "X-Kodi-Lab-CSRF": csrf,
            },
        ).status_code
        == 403
    )
    response = client.post(
        route,
        json={
            "confirmed": True,
            "path": "/tmp/attacker",
            "archive": "state.tar",
            "recursive": True,
            "wildcard": "*",
        },
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 422
    assert not any(call[0] == "delete_snapshot" for call in broker.calls)


def test_delete_returns_202_removes_snapshot_and_retains_safe_history(
    client: TestClient,
    database: sessionmaker[Session],
    broker: FakeBroker,
    data_root: Path,
) -> None:
    csrf = login(client)
    snapshot_id = _seed_available_snapshot(database)
    _seed_snapshot_artifact(data_root, snapshot_id)
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/snapshots/{snapshot_id}/delete",
        json={"confirmed": True},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 202
    document = response.json()
    assert document["snapshot_id"] == str(snapshot_id)
    assert set(document) == {"snapshot_id", "operation"}
    operation_id = document["operation"]["operation_id"]

    deadline = time.monotonic() + 2
    status_document: dict[str, object] = {}
    while time.monotonic() < deadline:
        status_response = client.get(
            f"/api/instances/{INSTANCE_ID}/snapshot-delete-operations/{operation_id}"
        )
        assert status_response.status_code == 200
        status_document = status_response.json()
        if status_document["state"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    assert status_document["state"] == "succeeded"
    assert status_document["snapshot_id"] == str(snapshot_id)
    assert not {
        "path",
        "archive",
        "manifest",
        "members",
        "container_id",
    }.intersection(status_document)
    assert not (data_root / "snapshots" / str(snapshot_id)).exists()
    assert (data_root / "snapshots").is_dir()
    assert broker.container is None
    with database() as session:
        assert session.get(SnapshotRow, str(snapshot_id)) is None
    listing = client.get(f"/api/instances/{INSTANCE_ID}/snapshots")
    assert listing.status_code == 200
    assert all(item["snapshot_id"] != str(snapshot_id) for item in listing.json())
    repeated = client.post(
        f"/api/instances/{INSTANCE_ID}/snapshots/{snapshot_id}/delete",
        json={"confirmed": True},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert repeated.status_code == 404


def test_deleting_snapshot_list_offers_no_restore_or_second_delete(
    client: TestClient,
    database: sessionmaker[Session],
) -> None:
    login(client)
    snapshot_id = _seed_available_snapshot(database)
    with database.begin() as session:
        row = session.get(SnapshotRow, str(snapshot_id))
        assert row is not None
        row.state = SnapshotState.DELETING
    page = client.get(f"/instances/{INSTANCE_ID}/snapshots")
    assert page.status_code == 200
    assert "Deletion is pending" in page.text
    assert f"snapshots/{snapshot_id}/restore" not in page.text
    assert f"snapshots/{snapshot_id}/delete" not in page.text
    assert (
        client.get(
            f"/instances/{INSTANCE_ID}/snapshots/{snapshot_id}/delete"
        ).status_code
        == 409
    )


def test_no_bulk_delete_or_retention_controls(
    client: TestClient,
) -> None:
    csrf = login(client)
    headers = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}
    for route in (
        f"/api/instances/{INSTANCE_ID}/snapshots/delete-all",
        f"/api/instances/{INSTANCE_ID}/snapshots/bulk-delete",
        f"/api/instances/{INSTANCE_ID}/snapshots/retention",
    ):
        assert client.post(route, json={}, headers=headers).status_code == 404


def test_instance_delete_confirmation_has_explicit_unselected_policy(
    client: TestClient,
    database: sessionmaker[Session],
) -> None:
    login(client)
    _seed_available_snapshot(database)
    with database.begin() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert instance is not None
        instance.notes = "potentially sensitive note"

    response = client.get(f"/instances/{INSTANCE_ID}/delete")

    assert response.status_code == 200
    assert "Preserve snapshots" in response.text
    assert "Permanently delete snapshots" in response.text
    assert "not restorable in v1" in response.text
    assert "Snapshots</dt><dd>1" in response.text
    assert "Notes present</dt><dd>Yes" in response.text
    assert "potentially sensitive note" not in response.text
    assert 'name="snapshot-policy" value="preserve" checked' not in response.text
    assert 'name="snapshot-policy" value="delete" checked' not in response.text


def test_matching_stopped_instance_keeps_delete_available(
    client: TestClient, broker: FakeBroker
) -> None:
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED)
    csrf = login(client)
    dashboard = client.get("/").text
    card = dashboard.split(f'data-instance-id="{INSTANCE_ID}"', 1)[1].split(
        "</article>", 1
    )[0]
    assert re.search(r'data-delete-action="link"[^>]*href=', card)
    assert not re.search(r'data-delete-action="link"[^>]*hidden', card)
    page = client.get(f"/instances/{INSTANCE_ID}/delete")
    assert page.status_code == 200
    assert 'name="snapshot-policy" value="preserve"' in page.text
    accepted = client.post(
        f"/api/instances/{INSTANCE_ID}/delete",
        json={"confirmed": True, "preserve_snapshots": True},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert accepted.status_code == 202
    assert (
        _wait_for_instance_delete(client, accepted.json()["operation_id"])["state"]
        == "succeeded"
    )
    assert any(action == "deprovision" for action, _ in broker.calls)


def test_known_stopped_revision_mismatch_blocks_delete_before_queue(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
) -> None:
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED).model_copy(
        update={"runtime_revision_id": str(uuid4())}
    )
    csrf = login(client)
    dashboard = client.get("/").text
    card = dashboard.split(f'data-instance-id="{INSTANCE_ID}"', 1)[1].split(
        "</article>", 1
    )[0]
    assert re.search(r'data-delete-action="link"[^>]*hidden', card)
    assert re.search(r'data-delete-action="blocked"[^>]*aria-disabled="true"', card)
    assert "Managed runtime differs from the selected pin." in card

    page = client.get(f"/instances/{INSTANCE_ID}/delete")
    assert page.status_code == 200
    assert "Managed runtime differs from the selected pin." in page.text
    assert 'name="snapshot-policy"' not in page.text
    assert re.search(r'id="confirm-instance-delete"[^>]*disabled', page.text)

    with database.begin() as session:
        session.add(
            TargetRow(
                id=str(UNRELATED_INSTANCE_ID),
                name="unrelated managed instance",
                target_type="managed",
                kodi_version="21.3",
                availability="unavailable",
                health="error",
                connection_state="disconnected",
            )
        )
        session.flush()
        session.add(
            ManagedInstanceRow(
                target_id=str(UNRELATED_INSTANCE_ID),
                instance_type="disposable",
                runtime_revision_id=str(REVISION_ID),
                runtime_state="stopped",
                start_on_host_boot=False,
            )
        )
    rejected = client.post(
        f"/api/instances/{INSTANCE_ID}/delete",
        json={"confirmed": True, "preserve_snapshots": True},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "instance_delete_conflict"
    assert "Managed runtime differs from the selected pin." in rejected.text
    with database() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ManagedInstanceDeleteOperationRow)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(ManagedInstanceMutationLeaseRow)
            )
            == 0
        )
        neighbor = session.get(ManagedInstanceRow, str(UNRELATED_INSTANCE_ID))
        assert neighbor is not None
        assert neighbor.runtime_state.value == "stopped"
        assert neighbor.runtime_state_revision == 0
    assert broker.calls == []


def test_delete_status_to_dispatch_revision_race_stays_fail_closed(
    client: TestClient,
    broker: FakeBroker,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker.container = inventory_entry(ContainerRuntimeState.STOPPED)
    csrf = login(client)
    assert 'data-delete-action="link"' in client.get("/").text
    original_inventory = broker.managed_container_inventory
    inventory_calls = 0

    def change_after_status(*, managed_instance_id: str | None = None) -> object:
        nonlocal inventory_calls
        result = original_inventory(managed_instance_id=managed_instance_id)
        inventory_calls += 1
        if inventory_calls == 1:
            broker.container = inventory_entry(
                ContainerRuntimeState.STOPPED
            ).model_copy(update={"runtime_revision_id": str(uuid4())})
        return result

    def reject_deprovision(**kwargs: object) -> None:
        assert broker.container is not None
        assert (
            broker.container.runtime_revision_id
            != kwargs["expected_runtime_revision_id"]
        )
        raise RuntimeRevisionPreconditionClientError(
            "runtime_revision_precondition_failed",
            "The managed runtime revision differs from trusted state.",
            request_id=None,
        )

    monkeypatch.setattr(broker, "managed_container_inventory", change_after_status)
    monkeypatch.setattr(broker, "deprovision_managed_container", reject_deprovision)
    accepted = client.post(
        f"/api/instances/{INSTANCE_ID}/delete",
        json={"confirmed": True, "preserve_snapshots": True},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert accepted.status_code == 202
    operation = _wait_for_instance_delete(client, accepted.json()["operation_id"])
    assert operation["state"] == "reconciliation_required"
    assert inventory_calls >= 2
    assert broker.calls == []
    with database() as session:
        assert session.get(ManagedInstanceRow, str(INSTANCE_ID)) is not None


def test_instance_delete_post_is_strict_and_origin_and_csrf_protected(
    client: TestClient,
) -> None:
    csrf = login(client)
    route = f"/api/instances/{INSTANCE_ID}/delete"
    authorized = {**ORIGIN, "X-Kodi-Lab-CSRF": csrf}

    assert client.post(route, json={}, headers=authorized).status_code == 422
    assert (
        client.post(route, json={"confirmed": True}, headers=authorized).status_code
        == 422
    )
    assert (
        client.post(
            route, json={"preserve_snapshots": True}, headers=authorized
        ).status_code
        == 422
    )
    assert (
        client.post(
            route,
            json={"confirmed": True, "preserve_snapshots": "true"},
            headers=authorized,
        ).status_code
        == 422
    )
    assert (
        client.post(
            route,
            json={
                "confirmed": True,
                "preserve_snapshots": True,
                "snapshot_ids": [],
            },
            headers=authorized,
        ).status_code
        == 422
    )
    assert (
        client.post(
            route,
            json={"confirmed": True, "preserve_snapshots": True},
            headers={"X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 403
    )
    assert (
        client.post(
            route,
            json={"confirmed": True, "preserve_snapshots": True},
            headers=ORIGIN,
        ).status_code
        == 403
    )


def test_instance_delete_preserve_reloads_snapshots_and_exposes_metadata_only(
    client: TestClient,
    database: sessionmaker[Session],
) -> None:
    csrf = login(client)
    first_id = _seed_available_snapshot(database)
    confirmation = client.get(f"/instances/{INSTANCE_ID}/delete")
    assert confirmation.status_code == 200
    assert "Snapshots</dt><dd>1" in confirmation.text

    second_id = _seed_available_snapshot(database)
    response = client.post(
        f"/api/instances/{INSTANCE_ID}/delete",
        json={"confirmed": True, "preserve_snapshots": True},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 202
    operation_id = response.json()["operation_id"]
    status_document = _wait_for_instance_delete(client, operation_id)
    assert status_document["state"] == "succeeded"
    assert status_document["snapshot_count"] == 2
    assert status_document["completed_snapshot_count"] == 2

    listing_response = client.get("/api/snapshots/preserved")
    assert listing_response.status_code == 200
    listing = listing_response.json()
    assert {item["snapshot_id"] for item in listing} == {
        str(first_id),
        str(second_id),
    }
    for item in listing:
        assert item["source_binding"] == "deleted_source"
        assert item["original_source_managed_instance_id"] == str(INSTANCE_ID)
        assert not {
            "path",
            "archive",
            "manifest",
            "members",
            "notes",
            "container_id",
        }.intersection(item)
    page = client.get("/snapshots/preserved")
    assert page.status_code == 200
    assert "Restore" not in page.text
    assert "Delete" in page.text
    assert client.get(f"/api/instances/{INSTANCE_ID}").status_code == 404

    with database() as session:
        assert session.get(ManagedInstanceRow, str(INSTANCE_ID)) is None
        assert all(
            row.source_managed_instance_id is None
            for row in session.scalars(select(SnapshotRow)).all()
        )
        operation = session.get(ManagedInstanceDeleteOperationRow, operation_id)
        assert operation is not None
        assert operation.state == "succeeded"


def test_preserved_snapshot_delete_is_artifact_first_and_keeps_history(
    client: TestClient,
    database: sessionmaker[Session],
    data_root: Path,
) -> None:
    csrf = login(client)
    snapshot_id = _seed_available_snapshot(database)
    _seed_snapshot_artifact(data_root, snapshot_id)
    with database.begin() as session:
        snapshot = session.get(SnapshotRow, str(snapshot_id))
        assert snapshot is not None
        snapshot.source_binding = SnapshotSourceBinding.DELETED_SOURCE
        snapshot.source_instance_name = "Deleted source"
        snapshot.source_deleted_at = "2026-09-16T01:00:00.000000Z"
        snapshot.source_managed_instance_id = None

    page = client.get(f"/snapshots/preserved/{snapshot_id}/delete")
    assert page.status_code == 200
    response = client.post(
        f"/api/snapshots/preserved/{snapshot_id}/delete",
        json={"confirmed": True},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 202
    operation_id = response.json()["operation"]["operation_id"]

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with database() as session:
            operation = session.get(SnapshotDeleteOperationRow, operation_id)
            assert operation is not None
            if operation.state not in {"queued", "running"}:
                break
        time.sleep(0.01)
    assert operation is not None
    assert operation.state == "succeeded"
    assert operation.managed_instance_id == str(INSTANCE_ID)
    assert not (data_root / "snapshots" / str(snapshot_id)).exists()
    with database() as session:
        assert session.get(SnapshotRow, str(snapshot_id)) is None


def test_attached_snapshot_cannot_use_preserved_delete_route(
    client: TestClient,
    database: sessionmaker[Session],
) -> None:
    csrf = login(client)
    snapshot_id = _seed_available_snapshot(database)

    assert client.get(f"/snapshots/preserved/{snapshot_id}/delete").status_code == 404
    response = client.post(
        f"/api/snapshots/preserved/{snapshot_id}/delete",
        json={"confirmed": True},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 409
