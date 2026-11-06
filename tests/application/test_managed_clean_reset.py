"""Clean Reset preserves independent development data and durable destruction proof."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.managed_clean_reset import (
    ManagedCleanResetService,
    ManagedResetInput,
    ManagedResetResult,
)
from kodi_lab.application.managed_versions import Version
from kodi_lab.application.mcp_administration import FULL_LAB_CONTROL_PRESET_ID
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.managed_versions_support import Environment


@pytest.fixture
def env(factory: sessionmaker[Session], tmp_path: Path) -> Iterator[Environment]:
    value = Environment(factory, tmp_path)
    value.preset = value.admin.create_custom_preset(
        display_name="Explicit clean Reset",
        template_preset_id=FULL_LAB_CONTROL_PRESET_ID,
        permissions=frozenset({"managed.versions", "managed.reset"}),
    )
    value.principal = value.actor("Reset actor")
    yield value
    value.close()


def reset(
    env: Environment,
    request: UUID | None = None,
    *,
    restart: bool = False,
    version: Version = "omega",
) -> ManagedResetResult:
    return ManagedCleanResetService(env.service).invoke(
        env.principal.client_id,
        ManagedResetInput(
            version=version, request_id=request or uuid4(), restart=restart
        ),
        lambda session, target: env.authorization.recheck_managed_versions(
            session, env.principal, "managed_reset", target
        ),
    )


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_clean_reset_replaces_exact_profile_without_fixtures(
    env: Environment, running: bool, restart: bool
) -> None:
    target = env.prepare()
    env.invoke()
    env.wait(target)
    if not running:
        assert env.invoke("stop").stop_confirmed
    original = env.broker.records[0]
    profile = env.root / "profiles" / str(target) / "settings.xml"
    profile.write_bytes(b"old settings and installed addons")
    request = uuid4()
    result = reset(env, request, restart=restart)
    assert result.phase == "complete", result
    assert result.retirement == result.replacement == "confirmed", result
    assert result.profile_bound_state == "invalidated"
    assert result.start == ("accepted" if restart else "not_requested")
    assert env.broker.records[0].container_id != original.container_id
    assert env.broker.records[0].runtime_revision_id == original.runtime_revision_id
    assert env.broker.records[0].runtime_state == ("running" if restart else "stopped")
    assert profile.read_bytes() == b"clean fixture Kodi profile\n"
    assert env.broker.removals == 1 and env.broker.provisions == 2
    replay = reset(env, request, restart=restart)
    assert replay.replayed and replay.operation_id == result.operation_id
    assert env.broker.removals == 1 and env.broker.provisions == 2
    if not restart:
        assert not replay.status.native_ready
    assert replay.status.bridge_readiness == "blocked"


def test_empty_reset_prepares_a_stopped_clean_slot(env: Environment) -> None:
    env.prepare()
    result = reset(env)
    assert result.phase == "complete", result
    assert result.retirement == "not_required" and result.replacement == "confirmed"
    assert result.retirement_operation_id is None
    assert env.broker.starts == env.broker.removals == 0
    assert env.broker.provisions == 1


@pytest.mark.parametrize("scope", ["client", "target", "versions", "grant"])
def test_destructive_permission_and_version_authority_are_both_required(
    env: Environment, scope: str
) -> None:
    from sqlalchemy import select

    from kodi_lab.mcp.authorization import McpAuthorizationError
    from kodi_lab.storage.models import ManagedCleanResetRow

    target = env.prepare()
    if scope == "grant":
        env.admin.remove_target_grant(env.principal.client_id, target)
    else:
        limited = env.admin.create_custom_preset(
            display_name="Limited lifecycle",
            template_preset_id=FULL_LAB_CONTROL_PRESET_ID,
            permissions=frozenset(
                {"managed.reset"} if scope == "versions" else {"managed.versions"}
            ),
        )
        if scope == "target":
            env.admin.assign_target_preset(target, limited.id)
        else:
            env.admin.assign_client_preset(env.principal.client_id, limited.id)
    with pytest.raises(McpAuthorizationError):
        reset(env)
    assert env.broker.removals == env.broker.provisions == 0
    with env.factory() as session:
        assert session.scalar(select(ManagedCleanResetRow)) is None


@pytest.mark.parametrize(
    "blocker", ["recent_user", "hold", "view-only", "interactive", "input"]
)
def test_other_users_viewers_and_input_prevent_any_destruction(
    env: Environment, blocker: str
) -> None:
    from datetime import timedelta

    from kodi_lab.application.browser_remote_coordination import (
        acquire_browser_remote,
        release_browser_remote,
    )
    from kodi_lab.application.managed_versions import ManagedVersionError
    from kodi_lab.application.novnc_control import HumanViewerOwnership
    from kodi_lab.broker.novnc import ViewBinding
    from kodi_lab.storage.managed_activity import timestamp
    from kodi_lab.storage.models import ManagedInstanceRow, ManagedInstanceUseRow

    target = env.prepare()
    env.invoke()
    env.wait(target)
    handle = None
    if blocker in {"recent_user", "hold"}:
        with env.factory.begin() as session:
            session.add(
                ManagedInstanceUseRow(
                    managed_instance_id=str(target),
                    actor_key="mcp:" + str(uuid4()),
                    last_activity_at=timestamp(env.service._clock()),
                    hold_until=timestamp(env.service._clock() + timedelta(minutes=10))
                    if blocker == "hold"
                    else None,
                )
            )
    elif blocker == "input":
        handle = acquire_browser_remote(env.factory, target)
    else:
        record = env.broker.records[0]
        with env.factory() as session:
            instance = session.get(ManagedInstanceRow, str(target))
            assert instance is not None
            epoch = instance.runtime_state_revision
        HumanViewerOwnership(env.factory).create(
            target,
            uuid4(),
            "f" * 64,
            ViewBinding(
                record.container_id,
                record.image_id,
                "fixture",
                (str(record.runtime_revision_id), epoch),
            ),
            blocker,
        )
    try:
        with pytest.raises(ManagedVersionError):
            reset(env)
    finally:
        if handle is not None:
            release_browser_remote(handle)
    assert env.broker.stops == env.broker.removals == 0
    assert env.broker.provisions == 1


@pytest.mark.parametrize("missing", ["approval", "image", "cached_record"])
def test_missing_runtime_approval_or_cache_never_stops_or_removes(
    env: Environment, missing: str
) -> None:
    from sqlalchemy import select

    from kodi_lab.application.managed_versions import ManagedVersionError
    from kodi_lab.application.runtime_catalog import RuntimeCatalogAdministrationService
    from kodi_lab.domain import RuntimeRecipeInstallationState
    from kodi_lab.storage.models import RuntimeRecipeInstallationRow

    target = env.prepare()
    env.invoke()
    env.wait(target)
    if missing == "approval":
        RuntimeCatalogAdministrationService(env.factory).revoke_revision_approval(
            UUID(str(env.broker.records[0].runtime_revision_id))
        )
    elif missing == "image":
        env.broker.image_present = False
    else:
        with env.factory.begin() as session:
            row = session.scalar(select(RuntimeRecipeInstallationRow))
            assert row is not None
            row.state = RuntimeRecipeInstallationState.FAILED
            row.runtime_revision_id = None
            row.failure_code = "runtime_image_unavailable"
    with pytest.raises(ManagedVersionError):
        reset(env)
    assert env.broker.stops == env.broker.removals == 0


@pytest.mark.parametrize("checkpoint", ["stop", "remove"])
def test_lost_response_after_confirmed_child_continues_without_repeating_it(
    env: Environment, checkpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    from kodi_lab.storage.models import ManagedCleanResetRow
    from tests.managed_versions_support import ProcessLoss

    target = env.prepare()
    env.invoke()
    env.wait(target)
    request = uuid4()
    service = ManagedCleanResetService(env.service)
    next_method = "_remove" if checkpoint == "stop" else "_provision"
    original = getattr(service, next_method)

    def loss(*_args: object) -> None:
        raise ProcessLoss()

    monkeypatch.setattr(service, next_method, loss)

    def authorize(session: Session, selected: UUID) -> None:
        env.authorization.recheck_managed_versions(
            session, env.principal, "managed_reset", selected
        )

    with pytest.raises(ProcessLoss):
        service.invoke(
            env.principal.client_id,
            ManagedResetInput(version="omega", request_id=request),
            authorize,
        )
    with env.factory() as session:
        parent = session.scalar(select(ManagedCleanResetRow))
        assert parent is not None and parent.phase == (
            "stopped" if checkpoint == "stop" else "retired"
        )
    monkeypatch.setattr(service, next_method, original)
    result = reset(env, request)
    assert result.phase == "complete" and result.replayed, result
    assert env.broker.stops == env.broker.removals == 1
    assert env.broker.provisions == 2


@pytest.mark.parametrize("child", ["stop", "remove", "provision"])
def test_uncertain_children_never_repeat_removal_or_create_a_second_profile(
    env: Environment, child: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.managed_versions import ManagedVersionError
    from kodi_lab.broker.errors import ClientTransportError

    target = env.prepare()
    env.invoke()
    env.wait(target)
    if child == "stop":
        original = env.broker.stop_managed_container

        def lost(**kwargs: Any) -> Any:
            original(**kwargs)
            raise ClientTransportError("fake lost stop reply")

        monkeypatch.setattr(env.broker, "stop_managed_container", lost)
    else:
        env.broker.fault = (
            "lost_remove_response" if child == "remove" else "uncertain_provision"
        )
    request = uuid4()
    first = reset(env, request)
    assert first.phase == "uncertain" and first.replacement == "not_confirmed", first
    assert first.retirement == (
        "confirmed" if child == "provision" else "not_confirmed"
    )
    counts = (env.broker.stops, env.broker.removals, env.broker.provisions)
    env.broker.fault = ""
    replay = reset(env, request)
    assert replay.phase == "uncertain" and replay.replayed, replay
    assert (env.broker.stops, env.broker.removals, env.broker.provisions) == counts
    with pytest.raises(ManagedVersionError, match="managed_reset_pending"):
        reset(env)
    blocked = env.invoke()
    assert (
        blocked.receipt_state == "blocked" and blocked.code == "managed_reset_pending"
    )
    if child == "stop":
        assert env.broker.removals == 0
    if child == "remove":
        assert env.broker.provisions == 1


def test_known_replacement_failure_recovers_without_a_second_retirement(
    env: Environment,
) -> None:
    target = env.prepare()
    env.invoke()
    env.wait(target)
    assert env.invoke("stop").stop_confirmed
    env.broker.fault = "safe_provision_failure"
    request = uuid4()
    first = reset(env, request)
    assert first.retirement == "confirmed" and first.replacement == "not_confirmed", (
        first
    )
    replay = reset(env, request)
    assert replay.phase == "blocked" and replay.code == "replacement_failed", replay
    assert env.broker.removals == 1 and env.broker.records == []
    env.broker.fault = ""
    recovered = reset(env)
    assert recovered.phase == "complete" and recovered.retirement == "not_required", (
        recovered
    )
    assert env.broker.removals == 1 and env.broker.provisions == 2
    assert reset(env, request).phase == "blocked"


def test_request_binding_rejects_restart_changes_and_start_reinterpretation(
    env: Environment,
) -> None:
    from kodi_lab.application.managed_versions import ManagedVersionError

    env.prepare()
    request = uuid4()
    reset(env, request)
    with pytest.raises(ManagedVersionError, match="request_conflict"):
        reset(env, request, restart=True)
    with pytest.raises(ManagedVersionError, match="request_conflict"):
        env.invoke(request_id=request)
    assert env.broker.removals == 0 and env.broker.provisions == 1


@pytest.mark.parametrize("drift", ["container", "image", "revision"])
def test_identity_drift_before_removal_fails_closed(
    env: Environment, drift: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace
    from typing import Any

    target = env.prepare()
    env.invoke()
    env.wait(target)
    assert env.invoke("stop").stop_confirmed
    original = ManagedCleanResetService._remove

    def changed(service: ManagedCleanResetService, row: Any, authorize: Any) -> None:
        key, value = {
            "container": ("container_id", "e" * 64),
            "image": ("image_id", "sha256:" + "e" * 64),
            "revision": ("runtime_revision_id", str(uuid4())),
        }[drift]
        values: dict[str, Any] = {key: value}
        env.broker.records[0] = replace(env.broker.records[0], **values)
        original(service, row, authorize)

    monkeypatch.setattr(ManagedCleanResetService, "_remove", changed)
    result = reset(env)
    assert result.phase == "blocked" and result.retirement == "not_confirmed", result
    assert env.broker.removals == 0 and env.broker.provisions == 1


def test_permission_is_rechecked_immediately_before_removal(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.mcp.authorization import McpAuthorizationError

    target = env.prepare()
    env.invoke()
    env.wait(target)
    assert env.invoke("stop").stop_confirmed
    original = ManagedCleanResetService._before_dispatch

    def revoke(
        service: ManagedCleanResetService, identity: str, authorize: Any
    ) -> None:
        env.admin.remove_target_grant(env.principal.client_id, target)
        original(service, identity, authorize)

    monkeypatch.setattr(ManagedCleanResetService, "_before_dispatch", revoke)
    request = uuid4()
    with pytest.raises(McpAuthorizationError):
        reset(env, request)
    assert env.broker.removals == 0 and len(env.broker.records) == 1
    monkeypatch.setattr(ManagedCleanResetService, "_before_dispatch", original)
    env.admin.grant_target(env.principal.client_id, target)
    replay = reset(env, request)
    assert replay.phase == "blocked" and replay.retirement == "not_confirmed", replay
    assert reset(env).phase == "complete"
    assert env.broker.removals == 1


def test_reset_preserves_workspace_source_git_evidence_and_invalidates_old_profile(
    env: Environment, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from sqlalchemy import select

    from kodi_lab.application.development_sessions import (
        DevelopmentSessionService,
        RetainedPolicyRequest,
        RetainedSelectionRequest,
    )
    from kodi_lab.config import Settings
    from kodi_lab.storage.managed_activity import timestamp
    from kodi_lab.storage.models import (
        AgentWorkspaceRow,
        DevelopmentSessionPolicyRow,
        DevelopmentSessionRow,
        DevelopmentSourceRow,
        ManagedBridgeTrustRow,
        ManagedScreenshotRow,
        McpAgentWorkspaceBindingRow,
        McpClientDevelopmentSourceGrantRow,
    )
    from tests.application import test_mcp_workspace_development as workspaces

    project = tmp_path / "independent-work"
    workspace = workspaces._environment(
        project, database_path=Path(env.factory.kw["bind"].url.database)
    )
    git = project / "sources/plugin.video.slice4/.git/HEAD"
    git.parent.mkdir()
    git.write_text("ref: refs/heads/agent-work\n")
    retained = project / "retained-diagnostics.txt"
    retained.write_text("selected historical evidence\n")
    target = env.prepare()
    env.invoke()
    env.wait(target)
    assert env.invoke("stop").stop_confirmed
    record = env.broker.records[0]
    selection = DevelopmentSessionService(
        env.factory,
        env.broker.managed_container_inventory,
        data_root=Settings().data_root,
    )
    selection.configure_retained(
        RetainedPolicyRequest(
            enabled=True, expected_generation=0, omega_target_id=target
        )
    )
    preview = selection.selection_preview(target, workspace.workspace_id)
    selection.select_retained(
        RetainedSelectionRequest(
            request_id=uuid4(),
            expected_generation=preview.latest_generation,
            target_id=target,
            workspace_id=workspace.workspace_id,
            identity=preview.identity,
        )
    )
    models = (
        AgentWorkspaceRow,
        DevelopmentSourceRow,
        McpAgentWorkspaceBindingRow,
        McpClientDevelopmentSourceGrantRow,
    )
    secret = uuid4()
    retired: list[UUID] = []
    env.factory.configure(
        info={"managed_bridge_secrets": SimpleNamespace(retire=retired.append)}
    )
    now = timestamp(env.service._clock())
    with env.factory.begin() as session:
        before = [session.execute(select(model.__table__)).all() for model in models]
        current = session.scalar(
            select(DevelopmentSessionRow).where(DevelopmentSessionRow.phase == "bound")
        )
        assert current is not None
        generation = current.generation
        session.add(
            ManagedBridgeTrustRow(
                target_id=str(target),
                runtime_revision_id=str(record.runtime_revision_id),
                container_id=record.container_id,
                bridge_id=str(uuid4()),
                bridge_port=8765,
                secret_reference=str(secret),
                identity_json="{}",
                state="active",
                generation=1,
                prepared_at=now,
                established_at=now,
            )
        )
        session.add(
            ManagedScreenshotRow(
                managed_instance_id=str(target),
                capture_id=str(uuid4()),
                captured_at=now,
                mime_type="image/png",
                width=2,
                height=2,
                byte_size=100,
                last_attempt_at=now,
            )
        )
    files = {
        str(p.relative_to(project)): p.read_bytes()
        for p in project.rglob("*")
        if p.is_file()
    }
    result = reset(env)
    assert result.phase == "complete" and result.profile_bound_state == "invalidated", (
        result
    )
    with env.factory() as session:
        assert [
            session.execute(select(model.__table__)).all() for model in models
        ] == before
        trust = session.get(ManagedBridgeTrustRow, str(target))
        assert (
            trust is not None
            and trust.state == "invalidated"
            and trust.secret_reference is None
        )
        assert trust.container_id == record.container_id and trust.generation > 1
        screen = session.get(ManagedScreenshotRow, str(target))
        assert (
            screen is not None
            and screen.capture_id is None
            and screen.last_error_code == "profile_replaced"
        )
        old = session.get(DevelopmentSessionRow, generation)
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        assert (
            old is not None
            and old.phase == "closed"
            and old.container_id == record.container_id
        )
        assert old.workspace_id == str(workspace.workspace_id)
        assert policy is not None and policy.active_generation is None
    assert str(secret) in [str(value) for value in retired]
    assert {
        str(p.relative_to(project)): p.read_bytes()
        for p in project.rglob("*")
        if p.is_file()
    } == files
    assert env.invoke().receipt_state == "complete"


def test_pending_reset_fences_web_start_mutation_input_and_viewer_admission(
    env: Environment,
) -> None:
    from sqlalchemy.exc import IntegrityError

    from kodi_lab.application.browser_remote_coordination import (
        BrowserRemoteCoordinationConflict,
        acquire_browser_remote,
    )
    from kodi_lab.application.lifecycle_operations import (
        LifecycleOperationConflictError,
    )
    from kodi_lab.application.novnc_control import HumanViewerOwnership
    from kodi_lab.broker.novnc import ViewBinding
    from kodi_lab.storage.models import ManagedInstanceRow

    target = env.prepare()
    env.invoke()
    env.wait(target)
    assert env.invoke("stop").stop_confirmed
    service = ManagedCleanResetService(env.service)
    operation = ManagedResetInput(version="omega", request_id=uuid4())

    def authorize(session: Session, selected: UUID) -> None:
        env.authorization.recheck_managed_versions(
            session, env.principal, "managed_reset", selected
        )

    parent, _ = service._accept(env.principal.client_id, operation, authorize)
    from kodi_lab.application.managed_resources import ManagedResourceProtected

    with pytest.raises(ManagedResourceProtected, match="managed_reset_pending"):
        env.resources.keep_awake(target, minutes=5)
    with pytest.raises(LifecycleOperationConflictError):
        env.lifecycle.start_managed(target)
    with pytest.raises(BrowserRemoteCoordinationConflict):
        acquire_browser_remote(env.factory, target)
    record = env.broker.records[0]
    with env.factory() as session:
        instance = session.get(ManagedInstanceRow, str(target))
        assert instance is not None
        epoch = instance.runtime_state_revision
    with pytest.raises(IntegrityError):
        HumanViewerOwnership(env.factory).create(
            target,
            uuid4(),
            "f" * 64,
            ViewBinding(
                record.container_id,
                record.image_id,
                "fixture",
                (str(record.runtime_revision_id), epoch),
            ),
            "view-only",
        )
    blocked = env.invoke()
    assert (
        blocked.code == "managed_reset_pending" and blocked.receipt_state == "blocked"
    )
    status = env.service.status(env.principal.client_id, env.authorize)
    omega = next(v for v in status.versions if v.version == "omega")
    assert omega.operation_id == UUID(parent.id) and not omega.native_ready
    assert reset(env, operation.request_id).phase == "complete"


def test_inflight_reset_and_start_share_the_existing_lock_and_mutation_lease(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from kodi_lab.application.lifecycle_operations import (
        LifecycleOperationConflictError,
    )
    from kodi_lab.application.managed_agent_lifecycle import ManagedAgentLifecycleError

    target = env.prepare()
    env.invoke()
    env.wait(target)
    assert env.invoke("stop").stop_confirmed
    entered, finish = Event(), Event()
    original = env.broker.deprovision_managed_container

    def paused(**kwargs: Any) -> Any:
        entered.set()
        assert finish.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(env.broker, "deprovision_managed_container", paused)
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(reset, env)
            assert entered.wait(5)
            with pytest.raises(ManagedAgentLifecycleError, match="target_busy"):
                env.invoke()
            with pytest.raises(LifecycleOperationConflictError):
                env.lifecycle.start_managed(target)
            finish.set()
            result = future.result(timeout=10)
            assert result.phase == "complete", result
    finally:
        finish.set()
    assert env.broker.removals == 1 and env.broker.provisions == 2
    assert env.broker.starts == 1 and env.broker.records[0].runtime_state == "stopped"


def test_late_screenshot_cannot_publish_old_pixels_during_or_after_reset(
    env: Environment,
) -> None:
    from kodi_lab.application.screenshots import (
        ManagedScreenshotService,
        ScreenshotIdentityChangedError,
    )
    from kodi_lab.storage.repositories import ManagedRuntimeProjectionRepository

    target = env.prepare()
    env.invoke()
    env.wait(target)
    with env.factory() as session:
        token = ManagedRuntimeProjectionRepository(session).observation_token(target)
    assert token is not None
    operation = ManagedResetInput(version="omega", request_id=uuid4())

    def authorize(session: Session, selected: UUID) -> None:
        env.authorization.recheck_managed_versions(
            session, env.principal, "managed_reset", selected
        )

    ManagedCleanResetService(env.service)._accept(
        env.principal.client_id, operation, authorize
    )
    screenshots = ManagedScreenshotService(
        env.factory,
        cast(Any, env.broker),
        env.status,
        env.service._reconciliation,
        data_root=env.root,
    )

    def publish() -> None:
        screenshots._publish_metadata(
            instance_id=target,
            capture_id=uuid4(),
            captured_at=env.service._clock(),
            width=2,
            height=2,
            byte_size=100,
            expected_token=token,
        )

    with pytest.raises(ScreenshotIdentityChangedError):
        publish()
    assert reset(env, operation.request_id).phase == "complete"
    with pytest.raises(ScreenshotIdentityChangedError):
        publish()
    assert not screenshots.get_status(target).latest_capture_available


def test_retirement_receipt_never_proves_a_later_replacement_is_current(
    env: Environment,
) -> None:
    env.prepare()
    request = uuid4()
    first = reset(env, request)
    assert first.replacement_current
    second = reset(env)
    assert second.phase == "complete" and second.replacement_current
    old = reset(env, request)
    assert (
        old.replayed and old.replacement == "confirmed" and not old.replacement_current
    )
    assert env.broker.removals == 1 and env.broker.provisions == 2


def test_clean_reset_does_not_apply_or_temporarily_clear_an_assigned_fixture(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.fixtures.package import load_packaged_fixture
    from kodi_lab.storage.models import ManagedInstanceRow
    from kodi_lab.storage.repositories import FixtureDefinitionRepository

    target = env.prepare()
    env.invoke()
    env.wait(target)
    assert env.invoke("stop").stop_confirmed
    fixture = load_packaged_fixture("small-mixed-v5").definition
    with env.factory.begin() as session:
        FixtureDefinitionRepository(session).create(fixture)
        instance = session.get(ManagedInstanceRow, str(target))
        assert instance is not None
        instance.fixture_definition_id = str(fixture.id)

    def forbidden(**_kwargs: Any) -> Any:
        pytest.fail("Clean Reset must never apply or initialize assigned fixtures")

    monkeypatch.setattr(env.broker, "apply_assigned_fixture", forbidden, raising=False)
    result = reset(env)
    assert result.phase == "complete" and result.replacement_current, result
    # Only the initial preparation started Kodi; Reset leaves it stopped.
    assert env.broker.starts == 1
    with env.factory() as session:
        instance = session.get(ManagedInstanceRow, str(target))
        assert instance is not None and instance.fixture_definition_id == str(
            fixture.id
        )


@pytest.mark.parametrize("version", ["matrix", "nexus", "omega", "piers"])
def test_four_cached_versions_reset_and_restart_with_their_own_native_identity(
    env: Environment, version: Version
) -> None:
    target = env.prepare(version)
    env.invoke(version=version)
    env.wait(target)
    request = uuid4()
    result = reset(env, request, version=version, restart=True)
    assert result.phase == "complete" and result.retirement == "confirmed", result
    env.wait(target)
    result = reset(env, request, version=version, restart=True)
    assert result.status.native_ready and result.status.version == version, result
    assert result.status.bridge_readiness == "blocked"
    assert env.broker.removals == 1 and env.broker.provisions == env.broker.starts == 2


def test_restart_cannot_evict_another_running_version(env: Environment) -> None:
    nexus = env.prepare("nexus")
    env.invoke(version="nexus")
    env.wait(nexus)
    omega = env.prepare()
    result = reset(env, restart=True)
    assert (
        result.phase == "blocked"
        and result.code == "other_version_running"
        and result.replacement_current
    ), result
    assert result.start == "not_confirmed" and env.broker.stops == 0
    assert sum(record.runtime_state == "running" for record in env.broker.records) == 1
    assert (
        next(
            r for r in env.broker.records if r.managed_instance_id == str(nexus)
        ).runtime_state
        == "running"
    )
    assert (
        next(
            r for r in env.broker.records if r.managed_instance_id == str(omega)
        ).runtime_state
        == "stopped"
    )
    assert env.invoke("stop", version="nexus").stop_confirmed
    assert env.invoke().receipt_state == "complete"
    assert env.broker.provisions == 2


def test_retirement_uses_the_configured_secret_root_and_preserves_an_unrelated_root(
    env: Environment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.storage.managed_activity import timestamp
    from kodi_lab.storage.models import ManagedBridgeTrustRow
    from kodi_lab.storage.secrets import ExternalBridgeSecretStore, SecretStoreError

    target = env.prepare()
    env.invoke()
    env.wait(target)
    assert env.invoke("stop").stop_confirmed
    record = env.broker.records[0]
    configured = env.lifecycle._data_root / "secrets/external-bridges"
    default_root = tmp_path / "unrelated-default"
    store = ExternalBridgeSecretStore(configured, master_key=b"x" * 32, key_version=1)
    unrelated = ExternalBridgeSecretStore(
        default_root / "secrets/external-bridges", master_key=b"y" * 32, key_version=1
    )
    reference = store.write(target, "request_authentication", b"a" * 32)
    unrelated.write(target, "request_authentication", b"b" * 32, reference=reference)
    with env.factory.begin() as session:
        session.add(
            ManagedBridgeTrustRow(
                target_id=str(target),
                runtime_revision_id=str(record.runtime_revision_id),
                container_id=record.container_id,
                bridge_port=8765,
                secret_reference=str(reference),
                state="prepared",
                generation=1,
                prepared_at=timestamp(env.service._clock()),
            )
        )
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(default_root))
    result = reset(env)
    assert result.phase == "complete", result
    with pytest.raises(SecretStoreError):
        store.read(target, reference, "request_authentication")
    assert unrelated.read(target, reference, "request_authentication") == b"b" * 32


def test_reset_of_another_retained_version_preserves_current_kodi_and_its_profile(
    env: Environment, tmp_path: Path
) -> None:
    from sqlalchemy import select

    from kodi_lab.application.development_sessions import (
        DevelopmentSessionService,
        RetainedPolicyRequest,
        RetainedSelectionRequest,
    )
    from kodi_lab.config import Settings
    from kodi_lab.storage.models import (
        DevelopmentSessionPolicyRow,
        DevelopmentSessionRow,
    )
    from tests.application import test_mcp_workspace_development as workspaces

    workspace = workspaces._environment(
        tmp_path / "persistent-work",
        database_path=Path(env.factory.kw["bind"].url.database),
    )
    omega = env.prepare()
    env.invoke()
    env.wait(omega)
    assert env.invoke("stop").stop_confirmed
    nexus = env.prepare("nexus")
    env.invoke(version="nexus")
    env.wait(nexus)
    assert env.invoke("stop", version="nexus").stop_confirmed
    original = next(
        record
        for record in env.broker.records
        if record.managed_instance_id == str(omega)
    )
    profile = env.root / "profiles" / str(omega) / "settings.xml"
    profile.write_bytes(b"selected Omega user settings")
    selection = DevelopmentSessionService(
        env.factory,
        env.broker.managed_container_inventory,
        data_root=Settings().data_root,
    )
    selection.configure_retained(
        RetainedPolicyRequest(
            enabled=True, expected_generation=0, omega_target_id=omega
        )
    )
    preview = selection.selection_preview(omega, workspace.workspace_id)
    selection.select_retained(
        RetainedSelectionRequest(
            request_id=uuid4(),
            expected_generation=preview.latest_generation,
            target_id=omega,
            workspace_id=workspace.workspace_id,
            identity=preview.identity,
        )
    )
    with env.factory() as session:
        before = [
            session.execute(select(model.__table__)).all()
            for model in (DevelopmentSessionPolicyRow, DevelopmentSessionRow)
        ]
    result = reset(env, version="nexus")
    assert result.phase == "complete" and result.retirement == "confirmed", result
    assert (
        next(
            record
            for record in env.broker.records
            if record.managed_instance_id == str(omega)
        )
        == original
    )
    assert profile.read_bytes() == b"selected Omega user settings"
    with env.factory() as session:
        assert [
            session.execute(select(model.__table__)).all()
            for model in (DevelopmentSessionPolicyRow, DevelopmentSessionRow)
        ] == before


def test_reset_children_remain_inspectable_after_normal_history_pruning(
    env: Environment,
) -> None:

    from kodi_lab.storage.repositories import (
        ManagedInstanceLifecycleOperationRepository,
    )

    target = env.prepare()
    env.invoke()
    env.wait(target)
    result = reset(env, restart=True)
    assert result.phase == "complete", result
    children = (
        result.stop_operation_id,
        result.retirement_operation_id,
        result.provision_operation_id,
        result.start_operation_id,
    )
    assert all(child is not None for child in children)
    with env.factory.begin() as session:
        repository = ManagedInstanceLifecycleOperationRepository(session)
        assert repository.prune_terminal(target, retain=0) == 0
        for child in children:
            assert child is not None and repository.get(child) is not None
