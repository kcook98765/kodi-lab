"""Integrated Web/MCP lifecycle and post-Reset authority over isolated runtimes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_sessions import (
    DevelopmentSessionService,
    RetainedPolicyRequest,
    RetainedSelectionRequest,
)
from kodi_lab.application.mcp_workspace_development import BoundedIntervalRateLimiter
from kodi_lab.runtime.recipes import load_installation_recipe, recipe_target_id
from kodi_lab.storage.managed_lifecycle_owner import WEB_ADMIN_OWNER
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionRow,
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceRow,
    McpAgentWorkspaceBindingRow,
    McpClientDevelopmentSourceGrantRow,
)
from kodi_lab.storage.repositories import TargetRepository
from tests.managed_versions_support import Broker, readiness
from tests.runtime.test_recipe_installation import _ready
from tests.storage.test_repositories import make_target
from tests.web.conftest import ORIGIN, login
from tests.web.test_development_session import source_and_workspace
from tests.web.test_managed_slots import act, slot, wait
from tests.web.test_managed_versions_mcp import authority, call
from tests.web.test_managed_versions_mcp import broker as broker
from tests.web.test_mcp import _assert_tool_error

PERMISSIONS = frozenset({"managed.versions", "managed.reset"})


def invoke(
    client: TestClient,
    token: str,
    action: str,
    version: str = "omega",
    request: str | None = None,
) -> dict[str, Any]:
    result = call(
        client,
        token,
        "managed_" + action,
        {}
        if action == "status"
        else dict(version=version, request_id=request or str(uuid4())),
    )
    assert "structuredContent" in result, result
    return cast(dict[str, Any], result["structuredContent"])


def test_wire_status_start_stop_resume_reset_and_sequential_version_switch(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
) -> None:
    token, omega, actor = authority(client, database, permissions=PERMISSIONS)
    context = cast(FastAPI, client.app).state.kodi_lab
    context.managed_versions._lifecycle._coordinator_factory = readiness
    admin = context.mcp_administration
    _ready(database, generation="nexus")
    reviewed = load_installation_recipe("nexus")
    nexus = recipe_target_id(reviewed)
    with database.begin() as session:
        TargetRepository(session).create(
            replace(
                make_target("Nexus Lab"),
                id=nexus,
                kodi_version=reviewed.recipe.kodi_version,
            )
        )
    admin.assign_target_preset(nexus, admin.get_client(actor).preset.id)
    admin.grant_target(actor, nexus)
    status = invoke(client, token, "status")
    assert (
        next(v for v in status["versions"] if v["version"] == "omega")["state"]
        == "empty"
    )
    request = str(uuid4())
    assert (
        invoke(client, token, "start", request=request)["receipt_state"] == "complete"
    )
    wait(client)
    original = broker.records[0].container_id
    profile = broker.root / str(omega) / "settings.xml"
    profile.write_text("retained preferences and addon files")
    assert invoke(client, token, "stop")["stop_confirmed"]
    assert invoke(client, token, "start")["receipt_state"] == "complete"
    wait(client)
    assert (
        broker.records[0].container_id == original
        and profile.read_text() == "retained preferences and addon files"
    )
    assert invoke(client, token, "stop")["stop_confirmed"]
    historical = invoke(client, token, "start", request=request)
    assert historical["replayed"] and historical["status"]["state"] == "stopped"
    assert invoke(client, token, "start", "nexus")["receipt_state"] == "complete"
    wait(client)
    assert sum(r.runtime_state == "running" for r in broker.records) == 1
    assert invoke(client, token, "stop", "nexus")["stop_confirmed"]
    reset = invoke(client, token, "reset")
    assert reset["phase"] == "complete" and reset["retirement"] == "confirmed"
    assert reset["replacement"] == "confirmed" and reset["status"]["state"] == "stopped"
    assert profile.read_text() != "retained preferences and addon files"
    assert invoke(client, token, "start")["receipt_state"] == "complete"
    wait(client)
    current = next(r for r in broker.records if r.managed_instance_id == str(omega))
    assert current.container_id != original
    assert invoke(client, token, "stop")["stop_confirmed"]
    assert len(broker.records) == 2 and broker.provisions == 3 and broker.removals == 1


def test_web_bearer_is_not_admin_and_web_cookie_is_not_mcp_authority(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
) -> None:
    token, _, _ = authority(client, database, permissions=PERMISSIONS)
    csrf = login(client)
    body = dict(
        request_id=str(uuid4()),
        expected_snapshot=slot(client)["snapshot"],
        confirm_reset=True,
    )
    client.cookies.clear()
    denied = client.post(
        "/api/managed-kodi/omega/reset",
        json=body,
        headers={**ORIGIN, "Authorization": "Bearer " + token, "X-Kodi-Lab-CSRF": csrf},
    )
    assert denied.status_code == 401
    current_csrf = login(client)
    denied_mcp = client.post(
        "/mcp",
        json=dict(jsonrpc="2.0", id=1, method="tools/list"),
        headers={
            **ORIGIN,
            "X-Kodi-Lab-CSRF": current_csrf,
            "Accept": "application/json, text/event-stream",
        },
    )
    assert denied_mcp.status_code in {401, 403}
    assert broker.removals == broker.provisions == broker.starts == 0


def test_reserved_web_owner_is_denied_even_with_a_valid_mcp_credential(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, target, actor = authority(client, database, permissions=PERMISSIONS)
    context = cast(FastAPI, client.app).state.kodi_lab
    admin = context.mcp_administration
    # Simulate even an imported, credentialed client colliding with the namespace.
    preset_id = admin.get_client(actor).preset.id
    with monkeypatch.context() as local:
        local.setattr(
            "kodi_lab.application.mcp_administration.uuid4", lambda: WEB_ADMIN_OWNER
        )
        collision = admin.create_client(
            display_name="Imported namespace collision", preset_id=preset_id
        )
    admin.grant_target(collision.id, target)
    credential = admin.issue_credential(collision.id)
    for name, arguments in (
        ("managed_status", {}),
        ("managed_start", dict(version="omega", request_id=str(uuid4()))),
        ("managed_reset", dict(version="omega", request_id=str(uuid4()))),
    ):
        _assert_tool_error(
            call(client, credential.token, name, arguments), "permission_denied"
        )
    assert broker.removals == broker.starts == broker.provisions == 0


def test_web_reset_cannot_cross_an_inflight_mcp_create(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token, _, _ = authority(client, database, permissions=PERMISSIONS)
    csrf = login(client)
    entered, finish = Event(), Event()
    provision = broker.provision_managed_container

    def paused(**kwargs: Any) -> Any:
        entered.set()
        assert finish.wait(10)
        return provision(**kwargs)

    monkeypatch.setattr(broker, "provision_managed_container", paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(invoke, client, token, "start")
        try:
            assert entered.wait(10)
            confirmation = dict(
                request_id=str(uuid4()),
                expected_snapshot=slot(client)["snapshot"],
                confirm_reset=True,
            )
            denied = act(client, csrf, "reset", payload=confirmation)
            assert denied.status_code == 409
            assert broker.removals == broker.provisions == 0
        finally:
            finish.set()
        assert future.result(timeout=10)["receipt_state"] == "complete"
    wait(client)
    stale = act(client, csrf, "reset", payload=confirmation)
    assert stale.status_code == 409 and stale.json()["code"] == "stale_browser"
    assert broker.provisions == broker.starts == 1 and broker.removals == 0


def test_failed_empty_start_recovery_link_resolves_without_an_instance(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
) -> None:
    csrf = login(client)
    cast(
        FastAPI, client.app
    ).state.kodi_lab.managed_versions._rate = BoundedIntervalRateLimiter(0)
    response = act(client, csrf, "start")
    assert response.status_code == 200 and response.json()["phase"] == "blocked", (
        response.text
    )
    with database() as session:
        assert (
            session.get(
                ManagedInstanceRow,
                str(
                    cast(FastAPI, client.app)
                    .state.kodi_lab.managed_versions.slot("omega")
                    .canonical_target_id
                ),
            )
            is None
        )
    recovery = client.get(response.json()["recovery_url"])
    assert recovery.status_code == 200, recovery.text
    assert broker.provisions == broker.starts == 0


def test_reset_preserves_workspace_authority_but_mcp_cannot_reestablish_selection(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
) -> None:
    permissions = PERMISSIONS | {"managed.lifecycle", "workspace.inspect"}
    token, target, actor = authority(client, database, permissions=permissions)
    context = cast(FastAPI, client.app).state.kodi_lab
    source, creation = source_and_workspace(client)
    workspace = context.agent_workspaces.get(creation.workspace_id)
    context.mcp_administration.grant_development_source(
        actor, workspace.development_source_id
    )
    context.mcp_administration.bind_workspace(actor, workspace.id)
    assert invoke(client, token, "start")["receipt_state"] == "complete"
    wait(client)
    assert invoke(client, token, "stop")["stop_confirmed"]
    selection = DevelopmentSessionService(
        database,
        broker.managed_container_inventory,
        data_root=context.settings.data_root,
    )
    selection._selection_rate = BoundedIntervalRateLimiter(0)
    selection.configure_retained(
        RetainedPolicyRequest(
            enabled=True, expected_generation=0, omega_target_id=target
        )
    )
    preview = selection.selection_preview(target, workspace.id)
    selection.select_retained(
        RetainedSelectionRequest(
            request_id=uuid4(),
            expected_generation=preview.latest_generation,
            target_id=target,
            workspace_id=workspace.id,
            identity=preview.identity,
        )
    )
    arguments = dict(target_id=str(target), workspace_id=str(workspace.id))
    assert "structuredContent" in call(
        client, token, "managed_selection_preview", arguments
    )
    with database() as session:
        before = [
            session.execute(select(model.__table__)).all()
            for model in (
                AgentWorkspaceRow,
                McpClientDevelopmentSourceGrantRow,
                McpAgentWorkspaceBindingRow,
            )
        ]
    record = broker.records[0]
    baseline_operation = str(uuid4())
    now = "2026-11-06T20:11:00.000000Z"
    with database.begin() as session:
        session.add(
            ManagedInstanceDevelopmentSourceRow(
                managed_instance_id=str(target),
                development_source_id=str(workspace.development_source_id),
                attached_at=now,
            )
        )
        session.flush()
        session.add(
            DevelopmentSourceDeploymentOperationRow(
                id=baseline_operation,
                original_managed_instance_id=str(target),
                managed_instance_id=str(target),
                development_source_id=str(workspace.development_source_id),
                source_logical_name="Session source",
                addon_id="plugin.video.session-wire",
                runtime_revision_id=str(record.runtime_revision_id),
                request_kind="copy_sync",
                requested_at=now,
                state="succeeded",
                phase="finalize",
                result_classification="initial_deployment",
                new_count=2,
                changed_count=0,
                unchanged_count=0,
                destination_only_count=0,
                transferred_bytes=10,
                expected_container_id=record.container_id,
                started_at=now,
                heartbeat_at=now,
                completed_at=now,
                reconciliation_required=False,
            )
        )
        session.flush()
        session.add(
            DevelopmentSourceDeploymentRow(
                managed_instance_id=str(target),
                development_source_id=str(workspace.development_source_id),
                addon_id="plugin.video.session-wire",
                mode="copy",
                last_successful_operation_id=baseline_operation,
                source_manifest_digest="sha256:" + "3" * 64,
                verified_destination_digest="sha256:" + "4" * 64,
                runtime_revision_id=str(record.runtime_revision_id),
                trusted_container_id=record.container_id,
                observed_source_name="Session",
                observed_source_version="1.0.0",
                verified_at=now,
                confidence="verified_at_last_operation",
            )
        )
    files = {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }
    assert invoke(client, token, "reset")["phase"] == "complete"
    login(client)
    guide = client.get("/managed-kodi/omega/development")
    assert "Stop Kodi before choosing" in guide.text
    assert f"/development-session/retained?target_id={target}" in guide.text
    denied = call(client, token, "managed_selection_preview", arguments)
    _assert_tool_error(denied, "retained_workspace_requires_administrator")
    with database() as session:
        assert [
            session.execute(select(model.__table__)).all()
            for model in (
                AgentWorkspaceRow,
                McpClientDevelopmentSourceGrantRow,
                McpAgentWorkspaceBindingRow,
            )
        ] == before
        old = session.scalar(select(DevelopmentSessionRow))
        assert old is not None and old.phase == "closed"
        baseline = session.get(
            DevelopmentSourceDeploymentRow,
            (str(target), str(workspace.development_source_id)),
        )
        assert baseline is not None and baseline.confidence == "stale"
        assert baseline.trusted_container_id == record.container_id
        assert baseline.last_successful_operation_id == baseline_operation
        evidence = session.get(
            DevelopmentSourceDeploymentOperationRow, baseline_operation
        )
        assert evidence is not None and evidence.state == "succeeded"

    assert files == {
        p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }
    # The existing administrator selection can bind this exact clean replacement.
    preview = selection.selection_preview(target, workspace.id)
    selection.select_retained(
        RetainedSelectionRequest(
            request_id=uuid4(),
            expected_generation=preview.latest_generation,
            target_id=target,
            workspace_id=workspace.id,
            identity=preview.identity,
        )
    )
    assert "structuredContent" in call(
        client, token, "managed_selection_preview", arguments
    )
    assert (
        invoke(client, token, "status")["versions"][2]["agent_readiness"]
        == "not_assessed"
    )


@pytest.mark.parametrize("handoff", ["stop", "release"])
def test_supported_admin_handoff_releases_only_web_hold_then_agent_can_stop(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    handoff: str,
) -> None:
    from kodi_lab.storage.models import ManagedInstanceUseRow

    token, target, actor = authority(client, database, permissions=PERMISSIONS)
    csrf = login(client)
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    original = broker.records[0].container_id
    if handoff == "stop":
        assert act(client, csrf, "stop").json()["phase"] == "complete"
        assert broker.records[0].container_id == original
        assert invoke(client, token, "start")["receipt_state"] == "complete"
        wait(client)
        assert broker.starts == 2 and broker.stops == 1
    else:
        assert invoke(client, token, "start")["receipt_state"] == "complete"
        with database() as session:
            use = session.get(ManagedInstanceUseRow, (str(target), "mcp:" + str(actor)))
            assert use is not None and use.hold_until
            original_hold = use.hold_until
        response = client.post(
            f"/api/managed-resources/{target}/keep-awake",
            json={"minutes": 0},
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        )
        assert response.status_code == 200
        with database() as session:
            use = session.get(ManagedInstanceUseRow, (str(target), "mcp:" + str(actor)))
            assert use is not None and use.hold_until == original_hold
            web = session.get(ManagedInstanceUseRow, (str(target), "web"))
            assert (
                web is not None
                and web.hold_until is None
                and web.released_at is not None
            )
        assert broker.starts == 1 and broker.stops == 0
    assert invoke(client, token, "stop")["stop_confirmed"]
    assert broker.records[0].container_id == original
    assert invoke(client, token, "reset")["replacement"] == "confirmed"
    assert (
        broker.records[0].container_id != original
        and broker.records[0].runtime_state == "stopped"
    )


@pytest.mark.parametrize("action", ["start", "reset"])
def test_web_reuses_exact_existing_canonical_target_with_a_custom_name(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    action: str,
) -> None:
    from kodi_lab.storage.models import TargetRow

    _, target, _ = authority(client, database, permissions=PERMISSIONS)
    with database.begin() as session:
        row = session.get(TargetRow, str(target))
        assert row is not None
        row.name = "My approved Omega"
    csrf = login(client)
    response = act(client, csrf, action)
    assert response.status_code == 200 and response.json()["phase"] == "complete", (
        response.text
    )
    wait(client)
    with database() as session:
        row = session.get(TargetRow, str(target))
        assert row is not None and row.name == "My approved Omega"
    assert broker.records[0].managed_instance_id == str(target)
    assert broker.provisions == 1 and broker.removals == 0


def test_existing_canonical_target_still_requires_exact_version_metadata(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
) -> None:
    from kodi_lab.storage.models import TargetRow

    _, target, _ = authority(client, database, permissions=PERMISSIONS)
    with database.begin() as session:
        row = session.get(TargetRow, str(target))
        assert row is not None
        row.kodi_version = "20.5"
    csrf = login(client)
    response = act(client, csrf, "start")
    assert (
        response.status_code == 409
        and response.json()["code"] == "canonical_slot_requires_review"
    ), response.text
    assert broker.starts == broker.provisions == broker.removals == 0


@pytest.mark.parametrize("version", ["matrix", "nexus", "omega", "piers"])
def test_web_empty_cached_slots_start_stop_and_clean_reset_for_each_version(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    version: str,
) -> None:
    if version == "piers":
        from tests.runtime.test_ostree_installation import ready

        ready(database)
    else:
        _ready(database, generation=version)
    context = cast(FastAPI, client.app).state.kodi_lab
    context.managed_versions._rate = BoundedIntervalRateLimiter(0)
    context.managed_versions._lifecycle._coordinator_factory = readiness
    csrf = login(client)

    def action(name: str) -> dict[str, Any]:
        response = client.post(
            f"/api/managed-kodi/{version}/{name}",
            json=dict(
                request_id=str(uuid4()),
                expected_snapshot=slot(client, version)["snapshot"],
                confirm_reset=name == "reset",
            ),
            headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
        )
        assert response.status_code == 200 and response.json()["phase"] == "complete", (
            response.text
        )
        return cast(dict[str, Any], response.json())

    assert slot(client, version)["state"] == "empty"
    action("start")
    wait(client)
    assert slot(client, version)["native"] == "Ready"
    original = broker.records[0]
    action("stop")
    assert broker.records[0].container_id == original.container_id
    action("reset")
    assert broker.records[0].container_id != original.container_id
    assert broker.records[0].image_id == original.image_id
    assert slot(client, version)["state"] == "stopped"
    assert (
        broker.provisions == 2 and broker.starts == broker.stops == broker.removals == 1
    )


@pytest.mark.parametrize("kind", ["start", "reset"])
def test_durable_parent_fences_development_selection_and_policy_between_children(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    kind: str,
) -> None:
    from kodi_lab.application.managed_clean_reset import (
        ManagedCleanResetService,
        ManagedResetInput,
    )
    from kodi_lab.application.mcp_administration import McpAuthenticationService
    from kodi_lab.mcp.authorization import McpAuthorizationService
    from kodi_lab.mcp.registry import build_mcp_tool_registry
    from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
    from kodi_lab.storage.models import ManagedInstanceMutationLeaseRow

    token, target, actor = authority(client, database, permissions=PERMISSIONS)
    context = cast(FastAPI, client.app).state.kodi_lab
    _source, creation = source_and_workspace(client)
    assert invoke(client, token, "start")["receipt_state"] == "complete"
    wait(client)
    assert invoke(client, token, "stop")["stop_confirmed"]
    selection = DevelopmentSessionService(
        database,
        broker.managed_container_inventory,
        data_root=context.settings.data_root,
    )
    selection._selection_rate = BoundedIntervalRateLimiter(0)
    selection.configure_retained(
        RetainedPolicyRequest(
            enabled=True, expected_generation=0, omega_target_id=target
        )
    )

    def select_request() -> RetainedSelectionRequest:
        preview = selection.selection_preview(target, creation.workspace_id)
        return RetainedSelectionRequest(
            request_id=uuid4(),
            expected_generation=preview.latest_generation,
            target_id=target,
            workspace_id=creation.workspace_id,
            identity=preview.identity,
        )

    original = selection.select_retained(select_request())
    principal = McpAuthenticationService(database).authenticate(token)
    assert principal is not None
    authorization = McpAuthorizationService(database, build_mcp_tool_registry())
    request = uuid4()

    def authorize(session: Session, selected: Any) -> None:
        authorization.recheck_managed_versions(
            session, principal, "managed_" + kind, selected
        )

    if kind == "start":
        context.managed_versions._accept(
            actor, request, context.managed_versions.slot("omega"), "start", authorize
        )
    else:
        ManagedCleanResetService(context.managed_versions)._accept(
            actor, ManagedResetInput(version="omega", request_id=request), authorize
        )
    with database() as session:
        assert session.scalar(select(ManagedInstanceMutationLeaseRow)) is None
    with pytest.raises(DevelopmentSessionConflict, match="development_session_busy"):
        selection.select_retained(select_request())
    with pytest.raises(DevelopmentSessionConflict, match="development_session_busy"):
        selection.configure_retained(
            RetainedPolicyRequest(
                enabled=True,
                expected_generation=original.generation,
                omega_target_id=target,
            )
        )
    current = selection.status()
    assert current is not None and current.generation == original.generation
    result = invoke(client, token, kind, request=str(request))
    assert result["phase" if kind == "reset" else "receipt_state"] == "complete"
    if kind == "start":
        wait(client)
        assert invoke(client, token, "stop")["stop_confirmed"]
    assert selection.select_retained(select_request()).phase == "bound"
