"""Selected-session runtime work narrows existing authority without owning grants."""

from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from kodi_lab.application.development_sessions import (
    DevelopmentSessionRequest,
    DevelopmentSessionService,
)
from kodi_lab.application.mcp_workspace_development import (
    McpWorkspaceDevelopmentError,
)
from kodi_lab.application.workspace_deployment_preflight import (
    DeploymentPreflightInput,
    WorkspaceDeploymentPreflightService,
)
from kodi_lab.application.workspace_launch import WorkspaceLaunchError
from kodi_lab.broker.development_sources import (
    DevelopmentSourceDeploymentStateError,
    SqliteDevelopmentSourceDeploymentResolver,
)
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedProvisionReservationRow,
    ManagedStartReservationRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    McpWorkspaceLaunchRow,
    RuntimeRevisionRow,
)
from kodi_lab.storage.repositories import DevelopmentSourceRepository
from tests.application import test_mcp_workspace_development as fixtures
from tests.application import test_workspace_launch as launch_fixtures
from tests.storage.test_migrations import (  # noqa: F401
    migration_setup,
    migration_templates,
)


def bind(env: fixtures.Environment, *, mismatch: str = "") -> None:
    workspace_id = env.workspace_id
    if mismatch == "workspace":
        creation = env.workspaces.queue_creation(fixtures.SOURCE_ID)
        lease = env.workspaces.acquire(creation.operation_id)
        assert lease is not None
        env.workspaces.run(lease)
        workspace_id = creation.workspace_id
    with env.sessions.begin() as session:
        source_id = fixtures.SOURCE_ID
        if mismatch == "source":
            source_id = uuid4()
            DevelopmentSourceRepository(session).create(
                replace(
                    fixtures._source(),
                    id=source_id,
                    logical_name="Other source",
                    relative_locator="other-source",
                    addon_id="plugin.video.other",
                )
            )
        session.add(
            DevelopmentSessionRow(
                generation=1,
                request_id=str(uuid4()),
                target_id=str(uuid4() if mismatch == "target" else fixtures.TARGET_ID),
                runtime_revision_id=str(fixtures.RUNTIME_REVISION_ID),
                workspace_id=str(workspace_id),
                source_id=str(source_id),
                container_id="b" * 64
                if mismatch == "container"
                else fixtures.CONTAINER_ID,
                phase="uncertain"
                if mismatch == "phase"
                else "reserved"
                if mismatch == "reserved"
                else "bound",
                created_at=fixtures.NOW_TEXT,
                updated_at=fixtures.NOW_TEXT,
            )
        )
        session.flush()
        session.add(DevelopmentSessionPolicyRow(id=1, active_generation=1))
        if mismatch == "revision":
            old = session.get(RuntimeRevisionRow, str(fixtures.RUNTIME_REVISION_ID))
            instance = session.get(ManagedInstanceRow, str(fixtures.TARGET_ID))
            assert old is not None and instance is not None
            values = {
                column.name: getattr(old, column.name)
                for column in old.__table__.columns
            }
            values.update(id=str(uuid4()), revision_number=2)
            session.add(RuntimeRevisionRow(**values))
            session.flush()
            instance.runtime_revision_id = values["id"]
        if mismatch in {"start", "provision"}:
            values = dict(
                managed_instance_id=str(fixtures.TARGET_ID),
                runtime_revision_id=str(fixtures.RUNTIME_REVISION_ID),
                operation_id=str(uuid4()),
                phase="uncertain",
                created_at=fixtures.NOW_TEXT,
            )
            if mismatch == "start":
                session.add(
                    ManagedStartReservationRow(
                        container_id=fixtures.CONTAINER_ID, **values
                    )
                )
            else:
                session.add(
                    ManagedProvisionReservationRow(session_generation=1, **values)
                )


def action_environment(
    tmp_path: Path, action: str
) -> tuple[fixtures.Environment, Any, Any]:
    if action == "launch":
        env, bridge, service = launch_fixtures.environment(tmp_path)
        return (
            env,
            bridge,
            lambda request: launch_fixtures.launch(env, service, request_id=request),
        )
    env = fixtures._environment(tmp_path)
    if action == "execute":
        fixtures._deploy_workspace(env)
    method = env.development.deploy if action == "deploy" else env.development.execute
    return (
        env,
        None,
        lambda request: method(
            client_id=env.client_id,
            request_id=request,
            workspace_id=env.workspace_id,
            expected_generation=0,
            target_id=fixtures.TARGET_ID,
        ),
    )


def runtime_counts(env: fixtures.Environment, bridge: Any) -> tuple[Any, ...]:
    return (
        env.broker.stop_count,
        env.broker.sync_count,
        env.broker.start_count,
        env.smoke.count,
        None if bridge is None else tuple(bridge.calls),
    )


@pytest.mark.parametrize("action", ["deploy", "execute", "launch"])
@pytest.mark.parametrize(
    "mismatch",
    [
        "workspace",
        "source",
        "target",
        "phase",
        "reserved",
        "revision",
        "container",
        "start",
        "provision",
    ],
)
def test_mismatched_session_never_dispatches_or_leaves_a_mutation_lease(
    tmp_path: Path,
    action: str,
    mismatch: str,
) -> None:
    env, bridge, run = action_environment(tmp_path, action)
    bind(env, mismatch=mismatch)
    before = runtime_counts(env, bridge)
    with pytest.raises(
        (McpWorkspaceDevelopmentError, WorkspaceLaunchError),
        match="development_session_",
    ):
        run(uuid4())
    assert runtime_counts(env, bridge) == before
    with env.sessions() as session:
        assert session.scalar(select(ManagedInstanceMutationLeaseRow)) is None
        model = {
            "deploy": McpWorkspaceDeploymentOperationRow,
            "execute": McpWorkspaceExecutionOperationRow,
            "launch": McpWorkspaceLaunchRow,
        }[action]
        rows: list[Any] = list(session.scalars(select(model)))
        # A live-container mismatch is observed after a durable claim, whose
        # failed receipt must remain inspectable and must release its lease.
        assert all(row.state in {"succeeded", "failed"} for row in rows)
        if mismatch != "container":
            assert not rows or (
                action != "deploy" and all(row.state == "succeeded" for row in rows)
            )


@pytest.mark.parametrize("action", ["deploy", "execute", "launch"])
def test_selected_workspace_succeeds_and_receipt_replay_survives_selection_change(
    tmp_path: Path,
    action: str,
) -> None:
    env, bridge, run = action_environment(tmp_path, action)
    bind(env)
    request_id = uuid4()
    result = run(request_id)
    assert result.state in {"succeeded", "accepted"}
    before = runtime_counts(env, bridge)
    with env.sessions.begin() as session:
        current = session.get(DevelopmentSessionRow, 1)
        assert current is not None
        current.phase = "uncertain"
    replay = run(request_id)
    assert replay.state == result.state and runtime_counts(env, bridge) == before


def test_offline_workspace_edit_remains_available_while_another_workspace_is_selected(
    tmp_path: Path,
) -> None:
    env = fixtures._environment(tmp_path)
    bind(env, mismatch="workspace")
    edit = env.workspaces.replace_text_file(
        env.workspace_id,
        expected_generation=0,
        relative_path="main.py",
        content="print('offline')\n",
    )
    assert edit.generation == 1
    assert (
        env.workspaces.read_file(env.workspace_id, "main.py").content
        == "print('offline')\n"
    )
    assert runtime_counts(env, None)[:4] == (0, 0, 0, 0)


def test_external_development_keeps_its_separate_existing_authority(
    tmp_path: Path,
) -> None:
    env = fixtures._environment(tmp_path)
    fixtures._configure_external_environment(env)
    bind(env, mismatch="workspace")
    deployed = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=fixtures.EXTERNAL_TARGET_ID,
    )
    assert deployed.state == "succeeded"
    executed = env.development.execute(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=fixtures.EXTERNAL_TARGET_ID,
    )
    assert executed.state == "succeeded" and env.broker.stop_count == 0


@pytest.mark.parametrize("action", ["deploy", "execute"])
def test_partial_policy_restore_after_claim_rechecks_before_any_runtime_mutation(
    tmp_path: Path, action: str
) -> None:
    env, bridge, run = action_environment(tmp_path, action)
    bind(env)
    before = runtime_counts(env, bridge)
    name = "_acquire_deployment" if action == "deploy" else "_acquire_execution"
    original = getattr(env.development, name)

    def acquire(*args: Any) -> None:
        original(*args)
        # Simulates unsupported partial DB restoration, not an allowed session
        # transition: ordinary replacement is blocked by the held typed lease.
        with env.sessions.begin() as session:
            current = session.get(DevelopmentSessionRow, 1)
            assert current is not None
            current.phase = "uncertain"

    setattr(env.development, name, acquire)
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="development_session_requires_review"
    ):
        run(uuid4())
    assert runtime_counts(env, bridge) == before


def test_broker_copy_resolver_checks_selected_session_before_copy(
    tmp_path: Path,
) -> None:
    env = fixtures._environment(tmp_path)
    bind(env)
    row, created = env.development._claim_deployment(
        env.client_id, uuid4(), env.workspace_id, 0, fixtures.TARGET_ID
    )
    assert created
    with env.sessions.begin() as session:
        operation = session.get(McpWorkspaceDeploymentOperationRow, row.id)
        assert operation is not None
        operation.state, operation.phase = "running", "sync"
        operation.started_at = fixtures.NOW_TEXT
        operation.mutation_started = operation.target_stopped = (
            operation.sync_started
        ) = True
    resolver = SqliteDevelopmentSourceDeploymentResolver(
        env.sessions, development_root=tmp_path / "sources", data_root=tmp_path / "data"
    )
    parameters: dict[str, Any] = dict(
        managed_instance_id=str(fixtures.TARGET_ID),
        workspace_id=str(env.workspace_id),
        expected_generation=0,
        deployment_operation_id=row.id,
        expected_container_id=fixtures.CONTAINER_ID,
    )
    assert resolver.resolve_workspace_deployment(
        **parameters
    ).managed_instance_id == str(fixtures.TARGET_ID)
    with env.sessions.begin() as session:
        current = session.get(DevelopmentSessionRow, 1)
        assert current is not None
        current.container_id = "b" * 64
    with pytest.raises(
        DevelopmentSourceDeploymentStateError, match="active development session"
    ):
        resolver.resolve_workspace_deployment(**parameters)
    assert env.broker.sync_count == 0


def test_admission_uses_real_upgraded_schema_without_a_policy_backfill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, url = migration_setup(monkeypatch, tmp_path, start_revision="head")
    env = fixtures._environment(
        tmp_path / "case", database_path=Path(url.removeprefix("sqlite:///"))
    )
    with env.sessions() as session:
        assert session.get(DevelopmentSessionPolicyRow, 1) is None
    bind(env, mismatch="workspace")
    with pytest.raises(
        McpWorkspaceDevelopmentError, match="development_session_other_workspace"
    ):
        fixtures._deploy_workspace(env)
    assert env.broker.stop_count == env.broker.sync_count == 0


def test_launch_rechecks_policy_after_claim_before_bridge_dispatch(
    tmp_path: Path,
) -> None:
    env, bridge, service = launch_fixtures.environment(tmp_path)
    bind(env)

    def fail(phase: str) -> None:
        if phase == "claimed":
            with env.sessions.begin() as session:
                current = session.get(DevelopmentSessionRow, 1)
                assert current is not None
                current.phase = "uncertain"

    service._failure = fail
    result = launch_fixtures.launch(env, service)
    assert (
        result.state == "failed"
        and result.code == "development_session_requires_review"
    )
    assert bridge.calls == []


def test_new_session_cannot_adopt_a_workspace_mutation_already_claimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = fixtures._environment(tmp_path)
    row, created = env.development._claim_deployment(
        env.client_id, uuid4(), env.workspace_id, 0, fixtures.TARGET_ID
    )
    assert created and row.state == "pending"
    service = DevelopmentSessionService(
        env.sessions,
        lambda: ManagedContainerInventoryResult(containers=[]),
        data_root=tmp_path / "data",
    )
    # Exercise shared writer/lease admission, independently of cached runtime
    # acquisition. Existing resources are deliberately absent in this test view.
    monkeypatch.setattr(service, "_runtime", lambda *_args: None)
    request = DevelopmentSessionRequest(
        request_id=uuid4(),
        expected_generation=0,
        target_id=fixtures.TARGET_ID,
        workspace_id=env.workspace_id,
    )
    with pytest.raises(DevelopmentSessionConflict, match="development_session_busy"):
        service.reserve(request)
    with env.sessions() as session:
        assert session.get(DevelopmentSessionPolicyRow, 1) is None
    assert env.broker.stop_count == env.broker.sync_count == 0


@pytest.mark.parametrize(
    "mismatch",
    [
        "",
        "inactive",
        "workspace",
        "source",
        "target",
        "phase",
        "reserved",
        "revision",
        "start",
        "provision",
    ],
)
def test_preflight_explains_stored_session_scope_without_dispatch_or_writes(
    tmp_path: Path, mismatch: str
) -> None:
    env = fixtures._environment(tmp_path)
    if mismatch != "inactive":
        bind(env, mismatch=mismatch)
    statements: list[str] = []

    def record(_connection: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        statements.append(sql.lstrip().split()[0].upper())

    engine = env.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        result = WorkspaceDeploymentPreflightService(env.sessions).inspect(
            DeploymentPreflightInput(
                workspace_id=env.workspace_id,
                target_id=fixtures.TARGET_ID,
                expected_generation=0,
            )
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert set(statements) <= {"SELECT", "PRAGMA"}
    check = next(item for item in result.checks if item.name == "session")
    expected = (
        "not_required"
        if mismatch == "inactive"
        else "satisfied"
        if not mismatch
        else "blocked"
    )
    assert check.state == expected
    if expected == "blocked":
        assert result.verdict == "blocked"
        action = next(
            item for item in result.next_actions if item.action == "review_session"
        )
        assert (
            action.authority == "web_administrator"
            and action.status_url == "/development-session"
        )
    assert result.live_bridge_readiness == "unknown" and not result.reservation_created
    assert env.broker.stop_count == env.broker.sync_count == env.smoke.count == 0
