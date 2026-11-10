"""Recent completed agent work never reserves ordinary retained Start/Stop."""

from datetime import timedelta
from typing import Any, cast
from uuid import UUID, uuid4

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
from kodi_lab.application.managed_resources import ResourcePolicyInput
from kodi_lab.domain import ManagedInstanceMutationKind
from kodi_lab.storage.managed_activity import (
    record_activity,
    record_operation_activity,
    timestamp,
)
from kodi_lab.storage.models import (
    ManagedInstanceUseRow,
    ManagedVersionOperationRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceLaunchRow,
)
from tests.managed_versions_support import Broker
from tests.web.test_development_session import source_and_workspace
from tests.web.test_managed_slots import act, setup, slot, wait
from tests.web.test_managed_versions_mcp import broker as broker


def completed_work(session: Session, context: Any, target: UUID, work: str) -> None:
    """Exercise real activity attribution from saved terminal operation receipts."""
    actor, operation = uuid4(), uuid4()
    now = timestamp(context.managed_versions._clock())
    record = context.managed_versions._inventory().containers[0]
    common = dict(
        id=str(operation),
        client_id=str(actor),
        request_id=str(uuid4()),
        workspace_id=str(uuid4()),
        development_source_id=str(uuid4()),
        target_id=str(target),
        runtime_revision_id=record.runtime_revision_id,
        generation=0,
        addon_id="script.lifecycle-fixture",
        addon_version="0.1.0",
        phase="finalize",
        requested_at=now,
        started_at=now,
        completed_at=now,
    )
    if work == "deployment":
        session.add(
            McpWorkspaceDeploymentOperationRow(
                **common,
                state="succeeded",
                mutation_started=True,
                target_stopped=True,
                sync_started=True,
                sync_completed=True,
                target_started=True,
                inventory_classification="matched",
                addon_enabled=True,
                result_classification="deployed",
                source_manifest_digest="sha256:" + "a" * 64,
                verified_destination_digest="sha256:" + "a" * 64,
            )
        )
        kind = ManagedInstanceMutationKind.MCP_WORKSPACE_DEPLOYMENT
    elif work == "launch":
        session.add(
            McpWorkspaceLaunchRow(
                **common,
                deployment_id=str(uuid4()),
                ordinal=0,
                tree_digest="a" * 64,
                bridge_generation=1,
                runtime_state_revision=0,
                container_id=record.container_id,
                state="accepted",
            )
        )
        kind = ManagedInstanceMutationKind.MCP_WORKSPACE_LAUNCH
    else:
        record_activity(
            session, target, actor=f"mcp:{actor}", now=context.managed_versions._clock()
        )
        return
    session.flush()
    record_operation_activity(
        session,
        target,
        kind=kind,
        operation_id=operation,
        now=context.managed_versions._clock(),
    )


@pytest.mark.parametrize("work", ["deployment", "launch", "history"])
@pytest.mark.parametrize("idle_minutes", [None, 60])
def test_completed_agent_work_web_stop_and_immediate_restart_preserve_retained_state(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    work: str,
    idle_minutes: int | None,
) -> None:
    csrf = setup(client, database)
    context = cast(FastAPI, client.app).state.kodi_lab
    context.managed_resources.configure(
        ResourcePolicyInput(max_running=1, idle_minutes=idle_minutes)
    )
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    target = UUID(broker.records[0].managed_instance_id)
    # Bind a real workspace so Current Managed Kodi and the cards observe one profile.
    source, creation = source_and_workspace(client)
    assert act(client, csrf, "stop").json()["phase"] == "complete"
    service = DevelopmentSessionService(
        database,
        broker.managed_container_inventory,
        data_root=context.settings.data_root,
    )
    service.configure_retained(
        RetainedPolicyRequest(
            enabled=True,
            expected_generation=0,
            omega_target_id=target,
        )
    )
    preview = service.selection_preview(target, creation.workspace_id)
    service.select_retained(
        RetainedSelectionRequest(
            request_id=uuid4(),
            expected_generation=preview.latest_generation,
            target_id=target,
            workspace_id=creation.workspace_id,
            identity=preview.identity,
        )
    )
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    original = broker.records[0]
    profile = broker.root / str(target)
    (profile / "settings.xml").write_bytes(b"retained settings")
    addon = profile / "addons" / "script.lifecycle-fixture"
    addon.mkdir(parents=True)
    (addon / "addon.xml").write_bytes(b"retained enabled addon fixture")
    workspace = context.agent_workspaces.get(creation.workspace_id)
    with database.begin() as session:
        completed_work(session, context, target, work)
    models = (McpWorkspaceDeploymentOperationRow, McpWorkspaceLaunchRow)
    with database() as session:
        receipts = [session.execute(select(m.__table__)).all() for m in models]
        uses = session.scalars(select(ManagedInstanceUseRow)).all()
        assert all(use.hold_until is None for use in uses)
    assert slot(client)["stop"]
    assert act(client, csrf, "stop").json()["phase"] == "complete"
    stopped = slot(client)
    current = client.get("/api/development-session").json()
    assert stopped["state"] == "stopped" and stopped["start"]
    assert not stopped["needs_review"] and stopped["start_reason"] is None
    assert current["runtime"]["container_state"] == "stopped"
    assert "resume" in current["summary"]["actions"]
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    assert slot(client)["state"] == "running" and slot(client)["stop"]
    assert (
        client.get("/api/development-session").json()["runtime"]["container_state"]
        == "running"
    )
    assert broker.records[0].container_id == original.container_id
    assert (profile / "settings.xml").read_bytes() == b"retained settings"
    assert (addon / "addon.xml").read_bytes() == b"retained enabled addon fixture"
    assert (source / "main.py").read_text() == "pass\n"
    assert context.agent_workspaces.get(workspace.id) == workspace
    with database() as session:
        assert [session.execute(select(m.__table__)).all() for m in models] == receipts
    assert act(client, csrf, "stop").json()["phase"] == "complete"


@pytest.mark.parametrize("minutes", [-1, 15])
def test_stale_hold_on_proven_stopped_profile_allows_restart_without_rearming_hold(
    client: TestClient,
    database: sessionmaker[Session],
    broker: Broker,
    minutes: int,
) -> None:
    csrf = setup(client, database)
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    assert act(client, csrf, "stop").json()["phase"] == "complete"
    context = cast(FastAPI, client.app).state.kodi_lab
    target = broker.records[0].managed_instance_id
    actor = "mcp:" + str(uuid4())
    with database.begin() as session:
        session.add(
            ManagedInstanceUseRow(
                managed_instance_id=target,
                actor_key=actor,
                last_activity_at=timestamp(context.managed_versions._clock()),
                hold_until=timestamp(
                    context.managed_versions._clock() + timedelta(minutes=minutes)
                ),
            )
        )
    assert slot(client)["start"] and not slot(client)["needs_review"]
    # Reset retains its stronger protection even on a stopped profile.
    assert not slot(client)["reset"]
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    assert slot(client)["stop"]
    assert act(client, csrf, "stop").json()["phase"] == "complete"
    with database() as session:
        use = session.get(ManagedInstanceUseRow, (target, actor))
        assert use is not None and use.hold_until is None
        assert all(
            row.hold_until is None
            for row in session.scalars(select(ManagedVersionOperationRow))
        )


def test_agent_start_web_stop_and_immediate_web_restart_keep_original_receipt(
    client: TestClient, database: sessionmaker[Session], broker: Broker
) -> None:
    from tests.web.conftest import login
    from tests.web.test_managed_versions_mcp import authority, call

    token, _, _ = authority(client, database)
    csrf = login(client)
    request = dict(version="omega", request_id=str(uuid4()))
    started = call(client, token, "managed_start", request)["structuredContent"]
    assert started["receipt_state"] == "complete" and started["hold_until"] is None
    wait(client)
    assert slot(client)["stop"]
    assert act(client, csrf, "stop").json()["phase"] == "complete"
    assert slot(client)["start"] and not slot(client)["needs_review"]
    replay = call(client, token, "managed_start", request)["structuredContent"]
    assert replay["replayed"] and replay["operation_id"] == started["operation_id"]
    assert replay["status"]["state"] == "stopped" and broker.starts == 1
    assert act(client, csrf, "start").json()["phase"] == "complete"
    wait(client)
    assert broker.starts == 2 and broker.provisions == 1
