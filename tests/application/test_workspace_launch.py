"""Direct launch receipts use real workspace, deployment and mutation coordination."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from kodi_lab.application.browser_remote_coordination import (
    acquire_browser_remote,
    release_browser_remote,
)
from kodi_lab.application.novnc_control import HumanViewerOwnership
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ReconciliationAction,
    StaleOperationReconciliationDispatcher,
)
from kodi_lab.application.workspace_launch import (
    WorkspaceLaunchError,
    WorkspaceLaunchService,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.development import parse_captured_addon_xml
from kodi_lab.development.deployment import expected_destination_digest
from kodi_lab.kodi.launch import (
    BridgeLaunchIntent,
    BridgeLaunchReceipt,
    ManagedLaunchRuntimeBinding,
)
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    ManagedInstanceMutationLeaseRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceLaunchRow,
    NovncSessionRow,
)
from kodi_lab.workspace import capture_workspace_deployment_tree
from tests.application import test_mcp_workspace_development as fixtures


class Bridge:
    def __init__(self) -> None:
        self.calls: list[bool] = []
        self.lost = False
        self.pending = False
        self.last: BridgeLaunchReceipt | None = None

    def binding(self, observed: Any, kodi_version: str) -> ManagedLaunchRuntimeBinding:
        return ManagedLaunchRuntimeBinding(
            target_id=observed.managed_instance_id,
            runtime_revision_id=observed.runtime_revision_id,
            container_id=observed.container_id,
            generation=1,
        )

    def call(
        self,
        observed: Any,
        kodi_version: str,
        intent: BridgeLaunchIntent,
        *,
        status_only: bool = False,
    ) -> BridgeLaunchReceipt | None:
        self.calls.append(status_only)
        if status_only:
            return self.last
        self.last = BridgeLaunchReceipt(
            launch_id=intent.launch_id,
            intent=intent,
            state="pending" if self.pending else "accepted",
            accepted_at=None if self.pending else fixtures.NOW_TEXT,
        )
        if self.lost:
            raise RuntimeError("fixture response lost after resident acceptance")
        return self.last


def environment(
    tmp_path: Path,
) -> tuple[fixtures.Environment, Bridge, WorkspaceLaunchService]:
    env = fixtures._environment(tmp_path)
    fixtures._deploy_workspace(env)
    # Use the production source/ownership digest domains, not equal placeholders.
    manifest = capture_workspace_deployment_tree(tmp_path / "data", env.workspace_id)
    with env.sessions.begin() as session:
        workspace = session.get(AgentWorkspaceRow, str(env.workspace_id))
        deployment = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert workspace is not None and deployment is not None
        deployment.source_manifest_digest = workspace.current_tree_digest
        deployment.verified_destination_digest = expected_destination_digest(
            manifest, runtime_uid=1000, runtime_gid=1000
        )
    bridge = Bridge()
    service = WorkspaceLaunchService(
        env.sessions,
        env.development,
        bridge,
        data_root=tmp_path / "data",
        clock=lambda: fixtures.NOW + timedelta(minutes=1),
    )
    return env, bridge, service


def launch(
    env: fixtures.Environment,
    service: WorkspaceLaunchService,
    *,
    request_id: UUID | None = None,
    ordinal: int = 0,
) -> Any:
    return service.launch(
        client_id=env.client_id,
        request_id=request_id or uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=fixtures.TARGET_ID,
        extension_ordinal=ordinal,
        authorize=lambda _session: None,
    )


def test_launch_acceptance_is_separate_from_smoke_completion_and_replays_once(
    tmp_path: Path,
) -> None:
    env, bridge, service = environment(tmp_path)
    request_id = uuid4()
    first = launch(env, service, request_id=request_id)
    assert first.state == "accepted" and first.dispatch_accepted
    assert first.ui_acceptance == first.script_completion == "not_observed"
    assert launch(env, service, request_id=request_id).replayed
    assert bridge.calls == [False] and env.smoke.count == 0
    with env.sessions() as session:
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(fixtures.TARGET_ID))
            is None
        )


def test_lost_response_recovers_only_through_resident_status_without_dispatch(
    tmp_path: Path,
) -> None:
    env, bridge, service = environment(tmp_path)
    bridge.lost = True
    request_id = uuid4()
    first = launch(env, service, request_id=request_id)
    assert first.state == "ambiguous"
    status = ManagedInstanceReconciliationService(env.sessions).status(
        fixtures.TARGET_ID
    )
    assert status.required and ReconciliationAction.STOP not in status.blocked_actions
    replay = launch(env, service, request_id=request_id)
    assert replay.state == "accepted" and replay.replayed
    assert bridge.calls == [False, True]
    assert (
        not ManagedInstanceReconciliationService(env.sessions)
        .status(fixtures.TARGET_ID)
        .required
    )


def test_pending_resident_dispatch_remains_unknown_until_exact_original_stop(
    tmp_path: Path,
) -> None:
    env, bridge, service = environment(tmp_path)
    bridge.pending = True
    request_id = uuid4()
    assert launch(env, service, request_id=request_id).state == "ambiguous"
    assert launch(env, service, request_id=request_id).state == "ambiguous"
    assert bridge.calls == [False, True]
    env.broker.state = ContainerRuntimeState.STOPPED
    result = launch(env, service, request_id=request_id)
    assert result.state == "contained" and not result.dispatch_accepted
    assert bridge.calls == [False, True] and env.broker.start_count == 1


@pytest.mark.parametrize("phase", ["claimed", "dispatch_marker", "accepted_response"])
def test_crash_receipt_recovery_never_reexecutes_and_expired_typed_lease_sweeps(
    tmp_path: Path, phase: str
) -> None:
    env, bridge, service = environment(tmp_path)

    class Crash(BaseException):
        pass

    def fail(selected: str) -> None:
        if selected == phase:
            raise Crash()

    service._failure = fail
    request_id = uuid4()
    with pytest.raises(Crash):
        launch(env, service, request_id=request_id)
    StaleOperationReconciliationDispatcher(
        env.sessions, clock=lambda: fixtures.NOW + timedelta(hours=1)
    ).sweep_instance(fixtures.TARGET_ID)
    service._failure = lambda _: None
    result = launch(env, service, request_id=request_id)
    assert result.state == (
        "failed"
        if phase == "claimed"
        else "accepted"
        if phase == "accepted_response"
        else "ambiguous"
    )
    assert bridge.calls.count(False) == (1 if phase == "accepted_response" else 0)


@pytest.mark.parametrize(
    ("ordinal", "extensions", "code"),
    [
        (
            2,
            '<extension point="xbmc.python.script" library="main.py"/>',
            "launch_entry_not_declared",
        ),
        (
            0,
            '<extension point="xbmc.service" library="main.py"/>',
            "launch_unsupported_extension",
        ),
        (
            0,
            '<extension point="xbmc.python.script" library="../main.py"/>',
            "launch_unsafe_library",
        ),
    ],
)
def test_manifest_selection_rejects_unsupported_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ordinal: int,
    extensions: str,
    code: str,
) -> None:
    env, bridge, service = environment(tmp_path)
    descriptor = parse_captured_addon_xml(
        "workspace",
        (
            '<addon id="plugin.video.slice4" name="Slice 4" version="1.0.0">'
            + extensions
            + "</addon>"
        ).encode(),
    )
    monkeypatch.setattr(env.development, "_workspace_descriptor", lambda _: descriptor)
    result = launch(env, service, ordinal=ordinal)
    assert result.state == "failed" and result.code == code
    assert bridge.calls == []


def test_stale_deployment_and_revoked_authority_never_dispatch(tmp_path: Path) -> None:
    env, bridge, service = environment(tmp_path)
    with pytest.raises(PermissionError):
        service.launch(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=fixtures.TARGET_ID,
            extension_ordinal=0,
            authorize=lambda _: (_ for _ in ()).throw(PermissionError()),
        )
    with pytest.raises(WorkspaceLaunchError, match="workspace_generation_conflict"):
        service.launch(
            client_id=env.client_id,
            request_id=uuid4(),
            workspace_id=env.workspace_id,
            expected_generation=1,
            target_id=fixtures.TARGET_ID,
            extension_ordinal=0,
            authorize=lambda _: None,
        )
    assert bridge.calls == []
    with env.sessions() as session:
        assert session.scalar(select(McpWorkspaceLaunchRow)) is None


def test_replay_cannot_change_entry_point_or_target(tmp_path: Path) -> None:
    env, bridge, service = environment(tmp_path)
    request_id = uuid4()
    launch(env, service, request_id=request_id)
    with pytest.raises(WorkspaceLaunchError, match="request_id_conflict"):
        launch(env, service, request_id=request_id, ordinal=1)
    assert bridge.calls == [False]


def test_accepted_receipt_survives_lost_commit_ack_without_redispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, bridge, service = environment(tmp_path)
    finish = service._finish
    lost = True

    def finalize(
        operation_id: str, state: str, code: str | None = None
    ) -> McpWorkspaceLaunchRow:
        nonlocal lost
        result = finish(operation_id, state, code)
        if lost:
            lost = False
            raise RuntimeError("fixture accepted commit acknowledgement lost")
        return result

    monkeypatch.setattr(service, "_finish", finalize)
    result = launch(env, service)
    assert result.state == "accepted" and result.dispatch_accepted
    assert bridge.calls == [False]


def test_fresh_launch_after_same_generation_redeploy_uses_latest_exact_deployment(
    tmp_path: Path,
) -> None:
    env, bridge, service = environment(tmp_path)
    first_request = uuid4()
    first = launch(env, service, request_id=first_request)
    with env.sessions.begin() as session:
        previous = session.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert previous is not None
        values = {
            column.name: getattr(previous, column.name)
            for column in previous.__table__.columns
        }
        values.update(
            id=str(uuid4()),
            request_id=str(uuid4()),
            requested_at=(fixtures.NOW + timedelta(minutes=1))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        )
        session.add(McpWorkspaceDeploymentOperationRow(**values))
    service._clock = lambda: fixtures.NOW + timedelta(minutes=2)
    second = launch(env, service)
    assert second.state == "accepted" and second.launch_id != first.launch_id
    assert launch(env, service, request_id=first_request).launch_id == first.launch_id
    assert bridge.calls == [False, False]


@pytest.mark.parametrize(
    "owner_state", ["pending", "active", "revoking", "blocked", "remote"]
)
def test_input_owner_refuses_launch_without_sql_error_receipt_or_dispatch(
    tmp_path: Path, owner_state: str
) -> None:
    env, bridge, service = environment(tmp_path)
    ownership = HumanViewerOwnership(env.sessions)
    attempt, broker = uuid4(), str(uuid4())
    handle = None
    if owner_state == "remote":
        handle = acquire_browser_remote(env.sessions, fixtures.TARGET_ID)
    else:
        ownership.create(
            fixtures.TARGET_ID,
            attempt,
            "c" * 64,
            ViewBinding(fixtures.CONTAINER_ID, fixtures.IMAGE_ID, "start", ()),
            "interactive",
        )
        with env.sessions.begin() as session:
            row = session.get(NovncSessionRow, str(fixtures.TARGET_ID))
            assert row is not None
            row.state, row.broker_id = owner_state, broker
            if owner_state == "blocked":
                row.expires_at = fixtures.NOW_TEXT
    try:
        with pytest.raises(WorkspaceLaunchError) as refused:
            launch(env, service)
        assert refused.value.code == "launch_conflict"
        cause = refused.value.__cause__
        assert isinstance(cause, IntegrityError)
        if owner_state == "remote":
            assert cause.statement == "browser remote coordination conflict"
            assert str(cause.orig) == "conflict"
        else:
            assert str(cause.orig) == "managed input conflict"
            status = ownership.status(fixtures.TARGET_ID)
            assert status is not None and status.attempt == attempt
            assert status.state == owner_state and status.mode == "interactive"
        assert bridge.calls == []
        with env.sessions() as session:
            assert session.scalar(select(McpWorkspaceLaunchRow)) is None
            assert (
                session.get(ManagedInstanceMutationLeaseRow, str(fixtures.TARGET_ID))
                is None
            )
    finally:
        if handle is not None:
            release_browser_remote(handle)
        else:
            ownership.finish(fixtures.TARGET_ID, attempt, broker, True)
    # Authoritative release does not replay the refused input. A fresh explicit
    # launch succeeds through the same coordinator with no retained queue.
    assert bridge.calls == []
    assert launch(env, service).state == "accepted"
    assert bridge.calls == [False]
