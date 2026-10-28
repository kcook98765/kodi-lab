"""Selected-session observation through real launch/smoke coordinators.

The framebuffer/Bridge transports are fixtures; durable operations, leases,
selection, registry and controller exclusion use the production implementations.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.live_display import LiveDisplaySessionRegistry
from kodi_lab.application.novnc import CatalogViewAuthority
from kodi_lab.application.novnc_control import HumanViewerOwnership
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.domain import ManagedInstanceMutationKind
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    DevelopmentSourceRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceLaunchRow,
    RuntimeContainerArtifactRow,
)
from kodi_lab.web.novnc import ChangeMode, PrototypeViewer, StartView
from tests.application import test_mcp_workspace_development as fixture
from tests.application.test_workspace_launch import environment, launch


def selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    env, bridge, service = environment(tmp_path)
    with env.sessions.begin() as db:
        db.add(
            RuntimeContainerArtifactRow(
                runtime_revision_id=str(fixture.RUNTIME_REVISION_ID),
                image_id=fixture.IMAGE_ID,
            )
        )
        workspace = db.get(AgentWorkspaceRow, str(env.workspace_id))
        assert workspace is not None
        db.add(
            DevelopmentSessionRow(
                generation=1,
                request_id=str(uuid4()),
                target_id=str(fixture.TARGET_ID),
                runtime_revision_id=str(fixture.RUNTIME_REVISION_ID),
                workspace_id=str(env.workspace_id),
                source_id=workspace.development_source_id,
                container_id=fixture.CONTAINER_ID,
                phase="bound",
                created_at=fixture.NOW_TEXT,
                updated_at=fixture.NOW_TEXT,
            )
        )
        db.flush()
        db.add(DevelopmentSessionPolicyRow(id=1, active_generation=1))
        deployment = db.scalar(select(McpWorkspaceDeploymentOperationRow))
        assert deployment is not None
        deployment.original_container_id = fixture.CONTAINER_ID

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "Clock":
            return cls.fromtimestamp(
                (fixture.NOW + timedelta(minutes=1)).timestamp(), tz=fixture.NOW.tzinfo
            )

    monkeypatch.setattr("kodi_lab.application.novnc.datetime", Clock)
    registry = LiveDisplaySessionRegistry()
    monkeypatch.setattr(
        "kodi_lab.application.live_display.LIVE_DISPLAY_REGISTRY", registry
    )
    authority = CatalogViewAuthority(
        env.sessions,
        {
            fixture.TARGET_ID: ViewBinding(
                fixture.CONTAINER_ID, fixture.IMAGE_ID, "start", ()
            )
        },
    )
    viewer = PrototypeViewer(
        tmp_path / "unused.sock",
        authority.resolve,
        registry=registry,
        ownership=HumanViewerOwnership(env.sessions),
    )
    item = viewer.start(fixture.TARGET_ID, "c" * 64, StartView(attempt=uuid4()))
    authority.pins[fixture.TARGET_ID] = item.binding
    return env, bridge, service, authority, viewer, item


@pytest.mark.parametrize("kind", ["launch", "smoke"])
def test_selected_view_only_survives_real_coordinator_and_refused_take_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    env, bridge, service, authority, viewer, item = selected(tmp_path, monkeypatch)
    observed = []

    def inspect(phase: str) -> None:
        observed.append(phase)
        assert not item.session.cancelled.is_set()
        assert authority.resolve(fixture.TARGET_ID) == item.binding
        with pytest.raises(ValueError, match="operation conflict"):
            authority.resolve(fixture.TARGET_ID, mode="interactive")
        with pytest.raises(ValueError, match="agent operation active"):
            viewer.handoff(
                fixture.TARGET_ID,
                item.owner,
                ChangeMode(attempt=item.attempt, next_attempt=uuid4()),
                "interactive",
            )
        assert not item.session.cancelled.is_set()

    if kind == "launch":
        service._failure = inspect
        result = launch(env, service)
        assert result.state == "accepted" and bridge.calls == [False]
    else:
        env.development._failure = inspect
        fixture._execute_workspace(env)
        assert env.smoke.count == 1
    assert observed
    assert authority.resolve(fixture.TARGET_ID) == item.binding
    assert not item.session.cancelled.is_set()
    viewer.end(item)


@pytest.mark.parametrize(
    "drift",
    ["generation", "workspace", "source", "session", "epoch", "image", "container"],
)
def test_selected_identity_drift_revokes_original_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    env, _, _, authority, viewer, item = selected(tmp_path, monkeypatch)
    with env.sessions.begin() as db:
        current = db.get(DevelopmentSessionRow, 1)
        workspace = db.get(AgentWorkspaceRow, str(env.workspace_id))
        instance = db.get(ManagedInstanceRow, str(fixture.TARGET_ID))
        assert current is not None and workspace is not None and instance is not None
        if drift == "generation":
            workspace.generation += 1
        elif drift == "workspace":
            other = AgentWorkspaceRow(
                **{
                    column.name: getattr(workspace, column.name)
                    for column in AgentWorkspaceRow.__table__.columns
                }
            )
            other.id = str(uuid4())
            other.baseline_artifact_id = str(uuid4())
            db.add(other)
            db.flush()
            current.workspace_id = other.id
        elif drift == "source":
            source = db.get(DevelopmentSourceRow, current.source_id)
            assert source is not None
            other_source = DevelopmentSourceRow(
                **{
                    column.name: getattr(source, column.name)
                    for column in DevelopmentSourceRow.__table__.columns
                }
            )
            other_source.id = str(uuid4())
            other_source.logical_name = "Other"
            other_source.addon_id = "plugin.video.other"
            other_source.relative_locator = "plugin.video.other"
            db.add(other_source)
            db.flush()
            current.source_id = other_source.id
        elif drift == "session":
            current.phase = "closed"
            policy = db.get(DevelopmentSessionPolicyRow, 1)
            assert policy is not None
            policy.active_generation = None
        elif drift == "epoch":
            instance.runtime_state_revision += 1
        elif drift == "container":
            current.container_id = "d" * 64
        else:
            from kodi_lab.storage.models import RuntimeContainerArtifactRow

            artifact = db.get(
                RuntimeContainerArtifactRow, str(fixture.RUNTIME_REVISION_ID)
            )
            assert artifact is not None
            artifact.image_id = "sha256:" + "d" * 64
    with pytest.raises(ValueError):
        authority.resolve(fixture.TARGET_ID)
    viewer.end(item)


@pytest.mark.parametrize(
    "defect",
    [
        "expired",
        "ambiguous",
        "wrong_workspace",
        "wrong_container",
        "unknown",
        "unclaimed",
        "heartbeat",
        "missing_lease",
    ],
)
def test_unproven_operation_never_preserves_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    env, _, service, authority, viewer, item = selected(tmp_path, monkeypatch)

    def inspect(phase: str) -> None:
        if phase != "claimed":
            return
        with env.sessions.begin() as db:
            lease = db.get(ManagedInstanceMutationLeaseRow, str(fixture.TARGET_ID))
            assert lease is not None
            operation = db.get(McpWorkspaceLaunchRow, lease.operation_id)
            assert operation is not None
            if defect == "expired":
                lease.lease_expires_at = fixture.NOW_TEXT
            elif defect == "unclaimed":
                lease.lease_token_digest = None
            elif defect == "heartbeat":
                lease.heartbeat_at = None
            elif defect == "missing_lease":
                db.delete(lease)
            elif defect == "ambiguous":
                operation.state = "ambiguous"
            elif defect == "wrong_workspace":
                operation.workspace_id = str(uuid4())
            elif defect == "wrong_container":
                operation.container_id = "d" * 64
            else:
                lease.operation_id = str(uuid4())
        with pytest.raises(ValueError, match="view operation"):
            authority.resolve(fixture.TARGET_ID)
        raise RuntimeError("stop after verifying fail-closed authority")

    service._failure = inspect
    with pytest.raises(RuntimeError, match="stop after verifying"):
        launch(env, service)
    viewer.end(item)


def test_negative_diagnostic_completed_between_checks_still_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.mcp_workspace_development import WorkspaceExecutionKind

    env, _, _, authority, viewer, item = selected(tmp_path, monkeypatch)
    operation, created = env.development._claim_execution(
        env.client_id,
        uuid4(),
        env.workspace_id,
        0,
        fixture.TARGET_ID,
        WorkspaceExecutionKind.KODI_RPC_NEGATIVE_DIAGNOSTIC,
        digest="e" * 64,
    )
    assert created
    env.development._finish_execution(operation.id, "failed", "not_dispatched")
    with env.sessions() as db:
        assert db.get(ManagedInstanceMutationLeaseRow, str(fixture.TARGET_ID)) is None
    assert authority.resolve(fixture.TARGET_ID) == item.binding
    assert item.session.cancelled.is_set()
    viewer.end(item)


@pytest.mark.parametrize("kind", ["launch", "smoke"])
def test_real_uncertainty_latches_cancellation_after_lease_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from kodi_lab.application.mcp_workspace_development import (
        McpWorkspaceDevelopmentError,
    )

    env, bridge, service, authority, viewer, item = selected(tmp_path, monkeypatch)
    if kind == "launch":
        bridge.pending = True
        assert launch(env, service).state == "ambiguous"
    else:

        def interrupted(_addon: str) -> Any:
            raise TimeoutError("smoke dispatch result unknown")

        monkeypatch.setattr(env.smoke, "invoke", interrupted)
        with pytest.raises(McpWorkspaceDevelopmentError):
            fixture._execute_workspace(env)
    with env.sessions() as db:
        assert db.get(ManagedInstanceMutationLeaseRow, str(fixture.TARGET_ID)) is None
    assert item.session.cancelled.is_set()
    with pytest.raises(ValueError, match="reconciliation"):
        authority.resolve(fixture.TARGET_ID)
    assert viewer.ownership is not None
    with pytest.raises(ValueError, match="reconciliation"):
        viewer.ownership.require_control_available(fixture.TARGET_ID)
    viewer.end(item)


@pytest.mark.parametrize("mode", [None, "view-only", "interactive"])
@pytest.mark.parametrize(
    "kind",
    [
        ManagedInstanceMutationKind.MCP_WORKSPACE_LAUNCH,
        ManagedInstanceMutationKind.MCP_WORKSPACE_EXECUTION,
        ManagedInstanceMutationKind.LIFECYCLE,
        ManagedInstanceMutationKind.MCP_WORKSPACE_DEPLOYMENT,
    ],
)
def test_registry_keeps_jpeg_and_invalidating_mutation_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Any,
    kind: ManagedInstanceMutationKind,
) -> None:
    env, _, _, _, viewer, item = selected(tmp_path, monkeypatch)
    item.session.novnc_mode = mode
    with env.sessions() as db:
        viewer.registry.cancel_for_mutation(fixture.TARGET_ID, kind, db, uuid4())
    assert item.session.cancelled.is_set()
    viewer.end(item)
