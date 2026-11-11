"""Protected Create intent and passive browser status; all execution is forbidden."""

from collections.abc import Iterator
from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.create_kodi import CreateKodiService
from kodi_lab.application.create_kodi_installation import CreateKodiInstallationService
from kodi_lab.application.create_kodi_provisioning import CreateKodiProvisioningService
from kodi_lab.application.create_kodi_readiness import CreateKodiReadinessService
from kodi_lab.application.create_kodi_registration import CreateKodiRegistrationService
from kodi_lab.application.create_kodi_start import CreateKodiStartService
from kodi_lab.application.managed_bridge_trust import ManagedBridgeTrustService
from kodi_lab.application.managed_kodi_prerequisites import (
    DataStorageObservation,
    ManagedKodiPrerequisiteService,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.broker.docker_inventory import ContainerHealthState, ContainerRuntimeState
from kodi_lab.broker.operations import ManagedContainerInventoryEntry
from kodi_lab.domain import InstanceRuntimeState, TargetType
from kodi_lab.runtime.recipes import load_installation_recipe, recipe_target_id
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.storage.models import (
    CreateKodiOperationRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedResourcePolicyRow,
    ManagedStartAuthorizationRow,
    NovncSessionRow,
    RuntimeRecipeInstallationRow,
)
from kodi_lab.storage.repositories import TargetRepository
from kodi_lab.web.app import create_app
from kodi_lab.web.create_kodi import BASE
from tests.storage.test_repositories import make_target
from tests.web.conftest import (
    CONTAINER_ID,
    IMAGE_ID,
    INSTANCE_ID,
    ORIGIN,
    REVISION_ID,
    FakeBroker,
    login,
)
from tests.web.test_runtime_installations import Flow, seed_installation


def approval(
    generation: str = "omega", request_id: UUID | None = None
) -> dict[str, Any]:
    reviewed = load_installation_recipe(generation)
    return dict(
        request_id=str(request_id or uuid4()),
        generation=generation,
        recipe_id=reviewed.recipe.recipe_id,
        recipe_revision=reviewed.recipe.recipe_revision,
        recipe_digest=reviewed.digest,
        owner_approved=True,
        start_after_setup=True,
    )


@pytest.fixture(autouse=True)
def no_execution(
    client: TestClient, broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Flow]:
    flow = Flow()
    flow.attach(broker)
    broker.container = None

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail(
            "Create acceptance/status must never execute a Kodi stage or native probe"
        )

    for cls, methods in (
        (RuntimeInstallationService, ("start", "_reconcile")),
        (
            ManagedRuntimeLifecycleService,
            ("provision", "start", "start_managed", "stop"),
        ),
        (ManagedRuntimeStatusService, ("get", "get_observation", "list_statuses")),
        (CreateKodiInstallationService, ("install",)),
        (CreateKodiRegistrationService, ("register", "confirm")),
        (CreateKodiProvisioningService, ("provision",)),
        (CreateKodiStartService, ("start",)),
        (CreateKodiReadinessService, ("inspect", "advance")),
        (ReadinessTaskRegistry, ("start",)),
        (ManagedBridgeTrustService, ("prepare_background",)),
    ):
        for method in methods:
            monkeypatch.setattr(cls, method, forbidden)
    monkeypatch.setattr(broker, "observe_managed_kodi_runtime", forbidden)
    yield flow
    assert flow.launches == []
    assert not any(
        name in {"provision", "start", "stop", "deprovision"}
        for name, _ in broker.calls
    )


@pytest.fixture
def headers(client: TestClient) -> dict[str, str]:
    return {**ORIGIN, "X-Kodi-Lab-CSRF": login(client)}


def submit(
    client: TestClient, headers: dict[str, str], payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    response = client.post(BASE, json=payload or approval(), headers=headers)
    assert response.status_code == 202, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Location"] == response.json()["status_url"]
    return cast(dict[str, Any], response.json())


def test_authentication_precedes_approval_body_and_bearer_is_not_admin(
    client: TestClient,
) -> None:
    assert client.post(BASE, content=b"not JSON").status_code == 401
    assert (
        client.post(
            BASE, json=approval(), headers={"Authorization": "Bearer not-a-web-session"}
        ).status_code
        == 401
    )
    assert client.get(f"{BASE}/{uuid4()}").status_code == 401
    assert client.get(f"{BASE}/by-target/{uuid4()}").status_code == 401
    target = recipe_target_id(load_installation_recipe("omega"))
    for target_id in (target, uuid4()):
        assert (
            client.get(f"{BASE}/by-target/{target_id}?missing=empty").status_code == 401
        )
        assert (
            client.get(
                f"{BASE}/by-target/{target_id}?missing=empty",
                headers={"Authorization": "Bearer not-a-web-session"},
            ).status_code
            == 401
        )


@pytest.mark.parametrize("generation", ["matrix", "nexus", "omega"])
def test_expected_absence_requires_opt_in_and_a_canonical_target(
    client: TestClient, headers: dict[str, str], generation: str
) -> None:
    target = recipe_target_id(load_installation_recipe(generation))
    path = f"{BASE}/by-target/{target}"
    assert client.get(path).status_code == 404
    empty = client.get(path, params={"missing": "empty"})
    assert empty.status_code == 204 and empty.content == b""
    assert empty.headers["Cache-Control"] == "no-store"
    for unrelated in (INSTANCE_ID, uuid4()):
        unknown = client.get(f"{BASE}/by-target/{unrelated}?missing=empty")
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "create_operation_not_found"
    assert client.get(f"{BASE}/{uuid4()}?missing=empty").status_code == 404
    assert client.get(path, params={"missing": "anything-else"}).status_code == 422


def test_opted_in_discovery_preserves_saved_operation_and_approval_identity(
    client: TestClient,
    headers: dict[str, str],
    database: sessionmaker[Session],
) -> None:
    payload = approval()
    accepted = submit(client, headers, payload)
    original = CreateKodiRepository(database).get(UUID(accepted["operation_id"]))
    assert original
    path = f"{BASE}/by-target/{original.target_id}"
    status = client.get(accepted["status_url"]).json()
    assert client.get(path).json() == status
    for _ in range(3):
        assert client.get(path, params={"missing": "empty"}).json() == status
    replay = client.post(BASE, json=payload, headers=headers)
    assert replay.status_code == 200
    assert replay.json()["operation_id"] == str(original.id)
    assert CreateKodiRepository(database).get(original.id) == original
    with database() as session:
        assert len(session.scalars(select(CreateKodiOperationRow)).all()) == 1


def test_origin_csrf_and_referer_rules(
    client: TestClient, headers: dict[str, str], database: sessionmaker[Session]
) -> None:
    body = approval()
    for invalid in (
        {},
        ORIGIN,
        {**headers, "Origin": "https://evil.invalid"},
        {**headers, "X-Kodi-Lab-CSRF": "wrong"},
        {
            "Referer": "https://evil.invalid/page",
            "X-Kodi-Lab-CSRF": headers["X-Kodi-Lab-CSRF"],
        },
    ):
        assert client.post(BASE, json=body, headers=invalid).status_code == 403
    with database() as session:
        assert session.scalars(select(CreateKodiOperationRow)).all() == []
    valid = {
        "Referer": "https://127.0.0.1/kodi",
        "X-Kodi-Lab-CSRF": headers["X-Kodi-Lab-CSRF"],
    }
    submit(client, valid, body)


@pytest.mark.parametrize("generation", ["matrix", "nexus", "omega"])
def test_approval_uses_snapshot_recipe_and_records_only_parent(
    client: TestClient,
    headers: dict[str, str],
    database: sessionmaker[Session],
    generation: str,
) -> None:
    snapshot = client.get(
        "/api/create-kodi/prerequisites", params={"generation": generation}
    ).json()
    body = {
        key: snapshot[key]
        for key in ("generation", "recipe_id", "recipe_revision", "recipe_digest")
    }
    body.update(request_id=str(uuid4()), owner_approved=True)
    result = submit(client, headers, body)
    assert (result["stage"], result["checkpoint"], result["version"]) == (
        "installation",
        "accepted",
        0,
    )
    assert result["message"] == "Create approved. Setup has not started."
    assert result["phase"] == "Preparing Kodi" and result["status"] == "waiting"
    assert result["kodi_version"] == snapshot["version"] and not result["complete"]
    assert result["instance"]["target_id"] == snapshot["target_id"]
    assert result["instance"]["state"] == "not_created"
    with database() as session:
        rows = session.scalars(select(CreateKodiOperationRow)).all()
        assert len(rows) == 1 and rows[0].request_id == body["request_id"]
        assert rows[0].approved_at and rows[0].scope == "create_native_v1"
        assert rows[0].installation_operation_id is None
        assert (
            rows[0].provision_operation_id is None
            and rows[0].start_operation_id is None
        )
        for model in (
            ManagedInstanceLifecycleOperationRow,
            ManagedStartAuthorizationRow,
            RuntimeRecipeInstallationRow,
            ManagedInstanceMutationLeaseRow,
            NovncSessionRow,
            ManagedResourcePolicyRow,
        ):
            assert session.scalars(select(model)).all() == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", "unreviewed"),
        ("generation", "OMEGA"),
        ("request_id", "not-a-uuid"),
        ("owner_approved", "true"),
        ("owner_approved", 1),
        ("start_after_setup", "false"),
        ("preview_approved", 1),
        ("recipe_revision", True),
        ("recipe_digest", "bad"),
        ("container_id", "a" * 64),
        ("force", True),
        ("expected_version", 0),
    ],
)
def test_strict_body_rejects_unreviewed_or_caller_authority(
    client: TestClient, headers: dict[str, str], field: str, value: Any
) -> None:
    body = approval()
    body[field] = value
    response = client.post(BASE, json=body, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "create_invalid_request"


def test_explicit_approval_and_request_uuid_are_required(
    client: TestClient, headers: dict[str, str]
) -> None:
    body = approval()
    body["owner_approved"] = False
    response = client.post(BASE, json=body, headers=headers)
    assert (
        response.status_code == 422
        and response.json()["error"]["code"] == "create_approval_required"
    )
    for key in (
        "owner_approved",
        "request_id",
        "recipe_id",
        "recipe_revision",
        "recipe_digest",
    ):
        missing = approval()
        missing.pop(key)
        assert client.post(BASE, json=missing, headers=headers).status_code == 422


def test_json_boundary_is_bounded_and_does_not_echo_payload(
    client: TestClient, headers: dict[str, str]
) -> None:
    assert client.post(BASE, content=b"{}", headers=headers).status_code == 415
    json_headers = {**headers, "Content-Type": "application/json"}
    assert (
        client.post(BASE, content=b"x" * 4097, headers=json_headers).status_code == 413
    )
    response = client.post(
        BASE, content=b'{"private":"super-secret", bad JSON}', headers=json_headers
    )
    assert response.status_code == 422 and "super-secret" not in response.text


def test_replay_is_same_parent_despite_backend_loss_and_current_recipe_change(
    client: TestClient,
    headers: dict[str, str],
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = approval()
    initial = submit(client, headers, body)
    repo = CreateKodiRepository(database)
    before = repo.get(UUID(initial["operation_id"]))

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("/private/path and broker credential")

    monkeypatch.setattr(ManagedKodiPrerequisiteService, "snapshot", unavailable)
    monkeypatch.setattr(
        "kodi_lab.web.create_kodi.load_installation_recipe", unavailable
    )
    replay = client.post(BASE, json=body, headers=headers)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert replay.json()["operation_id"] == initial["operation_id"]
    assert repo.get(UUID(initial["operation_id"])) == before


@pytest.mark.parametrize(
    "field", ["recipe_id", "recipe_revision", "recipe_digest", "generation"]
)
def test_changed_request_inputs_conflict_with_saved_parent(
    client: TestClient, headers: dict[str, str], field: str
) -> None:
    body = approval()
    initial = submit(client, headers, body)
    changed = dict(body)
    changed[field] = {
        "recipe_id": "other",
        "recipe_revision": 999,
        "recipe_digest": "sha256:" + "f" * 64,
        "generation": "nexus",
    }[field]
    conflict = client.post(BASE, json=changed, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "create_request_conflict"
    assert conflict.json()["operation_id"] == initial["operation_id"]
    assert conflict.json()["status_url"] == initial["status_url"]


def test_identical_acceptance_becoming_visible_at_target_lookup_is_replay(
    client: TestClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    body = approval()
    original = submit(client, headers, body)
    # Model another process committing between the two independent read lookups.
    monkeypatch.setattr(CreateKodiService, "for_request", lambda *_args: None)
    response = client.post(BASE, json=body, headers=headers)
    assert response.status_code == 200 and response.json()["replayed"]
    assert response.json()["operation_id"] == original["operation_id"]


def recovery_row(database: sessionmaker[Session], target_id: UUID) -> None:
    from datetime import UTC, datetime

    from kodi_lab.domain import ManagedInstanceSemanticState
    from kodi_lab.storage.managed_activity import timestamp

    now = timestamp(datetime.now(UTC))
    with database.begin() as session:
        session.add(
            ManagedInstanceLifecycleOperationRow(
                id=str(uuid4()),
                original_managed_instance_id=str(target_id),
                managed_instance_id=str(target_id),
                instance_name="Saved Kodi",
                kind="stop",
                requested_at=now,
                started_at=now,
                completed_at=now,
                prior_state=ManagedInstanceSemanticState.RUNNING_NOT_READY,
                resulting_state="ambiguous",
                state="reconciliation_required",
                failure_reason="private_broker_proof_and_path",
                reconciliation_required=True,
            )
        )


def test_recovery_required_is_reported_without_resolving_it(
    client: TestClient, headers: dict[str, str], database: sessionmaker[Session]
) -> None:
    recovery_row(database, INSTANCE_ID)
    response = client.post(BASE, json=approval(), headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "create_recovery_required"
    assert "private_broker" not in response.text
    with database() as session:
        assert session.scalars(select(CreateKodiOperationRow)).all() == []


def test_completed_history_retains_completion_while_current_kodi_requires_review(
    client: TestClient, headers: dict[str, str], database: sessionmaker[Session]
) -> None:
    accepted = submit(client, headers)
    target = completed_parent(database, accepted)
    recovery_row(database, target)
    response = client.get(accepted["status_url"])
    assert response.status_code == 200
    assert response.json()["complete"]
    assert response.json()["instance"]["state"] == "needs_attention"
    assert "private_broker" not in response.text


def test_canonical_competitor_and_stale_recipe_are_explicit(
    client: TestClient, headers: dict[str, str]
) -> None:
    stale = approval()
    stale["recipe_revision"] += 1
    response = client.post(BASE, json=stale, headers=headers)
    assert (
        response.status_code == 409
        and response.json()["error"]["code"] == "create_recipe_changed"
    )
    initial = submit(client, headers)
    conflict = client.post(BASE, json=approval(), headers=headers)
    assert (
        conflict.status_code == 409
        and conflict.json()["error"]["code"] == "create_target_conflict"
    )
    assert (
        client.get(conflict.json()["status_url"]).json()["operation_id"]
        == initial["operation_id"]
    )


@pytest.mark.parametrize(
    "blocker",
    [
        "backend",
        "capacity",
        "active_installation",
        "failed_installation",
        "lease",
        "storage",
    ],
)
def test_new_intent_refreshes_observations_without_claiming_or_repairing(
    client: TestClient,
    headers: dict[str, str],
    database: sessionmaker[Session],
    broker: FakeBroker,
    no_execution: Flow,
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
) -> None:
    if blocker == "backend":

        def fail(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("private broker credential")

        monkeypatch.setattr(ManagedKodiPrerequisiteService, "snapshot", fail)
    elif blocker == "capacity":
        broker.container = ManagedContainerInventoryEntry(
            managed_instance_id=str(INSTANCE_ID),
            container_id=CONTAINER_ID,
            image_id=IMAGE_ID,
            runtime_revision_id=str(REVISION_ID),
            runtime_state=ContainerRuntimeState.RUNNING,
            health_state=ContainerHealthState.HEALTHY,
        )
    elif blocker == "active_installation":
        no_execution.operation_id, no_execution.generation = str(uuid4()), "omega"
    elif blocker == "failed_installation":
        seed_installation(database, "omega")
    elif blocker == "lease":
        with database.begin() as session:
            session.add(
                ManagedInstanceMutationLeaseRow(
                    managed_instance_id=str(INSTANCE_ID),
                    operation_id=str(uuid4()),
                    operation_kind="lifecycle",
                )
            )
    else:
        monkeypatch.setattr(
            "kodi_lab.application.managed_kodi_prerequisites.read_data_storage",
            lambda _root: DataStorageObservation(0, True),
        )
    response = client.post(BASE, json=approval(), headers=headers)
    assert response.status_code == (503 if blocker == "backend" else 409), response.text
    assert "private broker credential" not in response.text
    with database() as session:
        assert session.scalars(select(CreateKodiOperationRow)).all() == []


def test_unknown_host_measurements_do_not_become_installation_authority_or_blockers(
    client: TestClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unknown(_root: Any) -> Any:
        raise NotImplementedError("unmeasurable Windows/VM storage")

    monkeypatch.setattr(
        "kodi_lab.application.managed_kodi_prerequisites.read_data_storage", unknown
    )
    result = submit(client, headers)
    assert result["checkpoint"] == "accepted" and result["version"] == 0


def test_reconnect_unknown_and_passive_gets_preserve_all_identity_and_settings(
    client: TestClient,
    headers: dict[str, str],
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = submit(client, headers)
    repo = CreateKodiRepository(database)
    original = repo.get(UUID(result["operation_id"]))
    assert original

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("GET attempted a parent transition or prerequisite probe")

    for name in (
        "plan",
        "claim_dispatch",
        "advance",
        "claim_readiness_probe",
        "note_uncertain",
        "resolve_installation_result",
    ):
        monkeypatch.setattr(CreateKodiRepository, name, forbidden)
    monkeypatch.setattr(ManagedKodiPrerequisiteService, "snapshot", forbidden)
    statements: list[str] = []
    engine = database.kw["bind"]

    def record(_c: Any, _cur: Any, sql: str, *_args: Any) -> None:
        statements.append(sql.lstrip())

    event.listen(engine, "before_cursor_execute", record)
    try:
        for _ in range(3):
            for url in (result["status_url"], f"{BASE}/by-target/{original.target_id}"):
                response = client.get(url)
                assert response.status_code == 200 and response.json()[
                    "operation_id"
                ] == str(original.id)
                assert response.headers["Cache-Control"] == "no-store"
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert not [
        sql
        for sql in statements
        if sql.split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
    ]
    assert (
        repo.get(original.id) == original
        and repo.for_request(original.request_id) == original
    )
    assert client.get(f"{BASE}/{uuid4()}").status_code == 404
    assert client.get(f"{BASE}/by-target/{uuid4()}").status_code == 404
    assert client.get(f"{BASE}/not-a-uuid").status_code == 422


@pytest.mark.parametrize("outcome", ["refused", "failed", "uncertain"])
def test_attention_codes_never_echo_internal_reason_or_trigger_recovery(
    client: TestClient,
    headers: dict[str, str],
    database: sessionmaker[Session],
    outcome: str,
) -> None:
    accepted = submit(client, headers)
    with database.begin() as session:
        row = session.get(CreateKodiOperationRow, accepted["operation_id"])
        assert row
        row.checkpoint, row.outcome = "needs_attention", outcome
        row.reason, row.completed_at = "private_lease_digest_and_secret", row.updated_at
        if outcome != "refused":
            row.dispatched_at = row.updated_at
            row.installation_operation_id = str(uuid4())
    response = client.get(accepted["status_url"])
    body = response.json()
    assert body["status"] == "needs_attention" and not body["complete"]
    assert (
        body["reason_code"]
        == {
            "refused": "create_action_blocked",
            "failed": "create_failed",
            "uncertain": "create_outcome_unconfirmed",
        }[outcome]
    )
    for hidden in (
        "private_lease_digest",
        "installation_operation_id",
        "start_operation_id",
        "proof",
        "container_id",
        "runtime_revision_id",
        "image_id",
        "credential",
        "/home/",
    ):
        assert hidden not in response.text


def completed_parent(database: sessionmaker[Session], accepted: dict[str, Any]) -> UUID:
    from kodi_lab.storage.models import ManagedBridgePreparationRow

    with database.begin() as session:
        row = session.get(CreateKodiOperationRow, accepted["operation_id"])
        assert row
        row.stage, row.checkpoint, row.outcome = "readiness", "complete", "succeeded"
        row.completed_at, row.dispatched_at = row.updated_at, row.updated_at
        (
            row.installation_operation_id,
            row.provision_operation_id,
            row.start_operation_id,
        ) = map(str, (uuid4(), uuid4(), uuid4()))
        row.runtime_revision_id, row.image_id, row.container_id = (
            str(REVISION_ID),
            IMAGE_ID,
            CONTAINER_ID,
        )
        row.runtime_state_revision = 4
        if row.bridge_required:
            session.add(
                ManagedBridgePreparationRow(
                    id=str(uuid4()),
                    request_id=str(uuid4()),
                    create_operation_id=row.id,
                    target_id=row.target_id,
                    runtime_revision_id=row.runtime_revision_id,
                    container_id=row.container_id,
                    image_id=row.image_id,
                    runtime_state_revision=4,
                    keep_running=row.start_after_setup,
                    phase="complete",
                    checkpoint="complete",
                    version=4,
                    attempt=0,
                    created_at=row.updated_at,
                    updated_at=row.updated_at,
                )
            )
        target_id = UUID(row.target_id)
        target = replace(
            make_target("Created Kodi"), id=target_id, target_type=TargetType.MANAGED
        )
        TargetRepository(session).create(target)
        session.add(
            ManagedInstanceRow(
                target_id=str(target_id),
                instance_type="disposable",
                runtime_revision_id=str(REVISION_ID),
                runtime_state=InstanceRuntimeState.RUNNING,
            )
        )
    return target_id


def test_historical_status_tracks_stop_and_replacement_without_repair(
    client: TestClient,
    headers: dict[str, str],
    database: sessionmaker[Session],
    broker: FakeBroker,
) -> None:
    accepted = submit(client, headers)
    target = completed_parent(database, accepted)
    broker.container = ManagedContainerInventoryEntry(
        managed_instance_id=str(target),
        container_id=CONTAINER_ID,
        image_id=IMAGE_ID,
        runtime_revision_id=str(REVISION_ID),
        runtime_state=ContainerRuntimeState.RUNNING,
        health_state=ContainerHealthState.HEALTHY,
    )
    repo = CreateKodiRepository(database)
    original = repo.get(UUID(accepted["operation_id"]))
    response = client.get(accepted["status_url"]).json()
    assert response["complete"] and response["instance"]["state"] == "running"
    broker.container = broker.container.model_copy(
        update={"runtime_state": ContainerRuntimeState.STOPPED}
    )
    with database.begin() as session:
        instance = session.get(ManagedInstanceRow, str(target))
        assert instance
        instance.runtime_state, instance.runtime_state_revision = (
            InstanceRuntimeState.STOPPED,
            8,
        )
    stopped = client.get(accepted["status_url"]).json()
    assert stopped["complete"] and stopped["instance"]["state"] == "stopped"
    broker.container = broker.container.model_copy(update={"container_id": "f" * 64})
    changed = client.get(accepted["status_url"]).json()
    assert changed["complete"] and changed["instance"]["state"] == "changed"
    broker.inventory_error = OSError("private Docker path")
    unknown = client.get(accepted["status_url"])
    assert (
        unknown.json()["instance"]["state"] == "unknown"
        and "private Docker" not in unknown.text
    )
    assert repo.get(UUID(accepted["operation_id"])) == original


def test_application_reopen_reuses_admin_session_and_same_parent_without_approval(
    client: TestClient,
    headers: dict[str, str],
    database: sessionmaker[Session],
    broker: FakeBroker,
) -> None:
    accepted = submit(client, headers)
    context = cast(FastAPI, client.app).state.kodi_lab
    reopened_app = create_app(
        settings=context.settings,
        session_factory=database,
        broker=broker,
        auth=WebAuthenticationService(database),
    )
    with TestClient(reopened_app, base_url="https://127.0.0.1") as reopened:
        reopened.cookies.update(client.cookies)
        response = reopened.get(accepted["status_url"])
        assert response.status_code == 200
        assert response.json() == {
            key: value for key, value in accepted.items() if key != "replayed"
        }


@pytest.mark.parametrize("generation", ["matrix", "nexus", "omega", "piers"])
def test_install_only_approval_is_allowed_when_running_capacity_is_full(
    client: TestClient,
    headers: dict[str, str],
    broker: FakeBroker,
    generation: str,
) -> None:
    broker.container = ManagedContainerInventoryEntry(
        managed_instance_id=str(INSTANCE_ID),
        container_id=CONTAINER_ID,
        image_id=IMAGE_ID,
        runtime_revision_id=str(REVISION_ID),
        runtime_state=ContainerRuntimeState.RUNNING,
        health_state=ContainerHealthState.HEALTHY,
    )
    payload = approval(generation) | {
        "start_after_setup": False,
        "preview_approved": generation == "piers",
    }
    original = submit(client, headers, payload)
    replay = client.post(BASE, json=payload, headers=headers)
    assert replay.status_code == 200
    assert original["operation_id"] == replay.json()["operation_id"]
    assert (
        client.post(
            BASE, json=payload | {"start_after_setup": True}, headers=headers
        ).status_code
        == 409
    )


def test_preview_setup_requires_explicit_consent_and_four_slot_policy_is_accepted(
    client: TestClient,
    headers: dict[str, str],
    database: sessionmaker[Session],
) -> None:
    assert client.post(BASE, json=approval("piers"), headers=headers).status_code == 422
    with database.begin() as session:
        session.add(ManagedResourcePolicyRow(id=1, max_running=4, idle_minutes=60))
    accepted = submit(client, headers)
    assert accepted["generation"] == "omega"
