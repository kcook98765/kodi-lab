"""Version requests retain profiles and never retry uncertain children."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any, Never
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.managed_versions import AuthorizeVersion, ManagedVersionError
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.mcp.authorization import McpAuthorizationError
from kodi_lab.storage.models import ManagedInstanceRow, ManagedVersionOperationRow
from kodi_lab.storage.repositories import ManagedInstanceLifecycleOperationRepository
from tests.application.test_create_kodi_installation import factory as factory
from tests.application.test_create_kodi_installation import schema as schema
from tests.managed_versions_support import Environment, ProcessLoss


@pytest.fixture
def env(factory: sessionmaker[Session], tmp_path: Path) -> Iterator[Environment]:
    value = Environment(factory, tmp_path)
    yield value
    value.close()


def test_empty_start_registers_cached_pin_once_and_reuses_running_profile(
    env: Environment,
) -> None:
    target = env.prepare()
    request = uuid4()
    first = env.invoke(request_id=request)
    assert first.receipt_state == "complete", first
    env.wait(target)
    replay = env.invoke(request_id=request)
    assert replay.replayed and replay.operation_id == first.operation_id
    assert replay.status.native_ready and replay.status.state == "running"
    assert replay.status.bridge_readiness == "blocked"
    assert replay.status.agent_readiness == "not_assessed"
    assert replay.hold_until == first.hold_until is None
    again = env.invoke()
    assert again.code == "already_running" and env.broker.starts == 1
    assert env.broker.provisions == 1 and len(env.broker.records) == 1
    assert env.broker.launches == []


def test_stop_start_preserves_exact_profile_and_old_receipts_do_not_restart(
    env: Environment,
) -> None:
    target = env.prepare()
    request = uuid4()
    started = env.invoke(request_id=request)
    env.wait(target)
    original = env.broker.records[0]
    profile = env.root / "profiles" / str(target) / "settings.xml"
    profile.write_bytes(b"private settings and installed addons\n")
    stopped = env.invoke("stop")
    assert stopped.stop_confirmed and stopped.receipt_state == "complete", stopped
    assert env.broker.records[0].container_id == original.container_id
    assert profile.read_bytes() == b"private settings and installed addons\n"
    replay = env.invoke(request_id=request)
    assert replay.replayed and replay.operation_id == started.operation_id
    assert replay.status.state == "stopped" and env.broker.starts == 1
    resumed = env.invoke()
    assert resumed.receipt_state == "complete", resumed
    assert env.broker.starts == 2 and env.broker.provisions == 1
    assert env.broker.records[0].container_id == original.container_id
    assert profile.read_bytes() == b"private settings and installed addons\n"


@pytest.mark.parametrize("cached", [False, True])
def test_missing_cached_image_refuses_without_registration_or_download(
    env: Environment, cached: bool
) -> None:
    target = env.prepare(cached=cached)
    env.broker.image_present = False
    result = env.invoke()
    assert (
        result.receipt_state == "blocked"
        and result.code == "approved_cached_runtime_required"
    ), result
    assert env.broker.provisions == env.broker.starts == 0 and env.broker.launches == []
    with env.factory() as session:
        assert session.get(ManagedInstanceRow, str(target)) is None


def test_ungranted_target_reveals_only_public_supported_version_names(
    env: Environment,
) -> None:
    env.prepare(grant=False)
    values = env.service.status(env.principal.client_id, env.authorize)
    assert all(
        not value.authorized and value.code == "not_authorized"
        for value in values.versions
    )
    assert all(
        value.request_id is None and value.operation_id is None
        for value in values.versions
    )
    with pytest.raises(McpAuthorizationError):
        env.invoke()
    assert env.broker.provisions == 0
    with env.factory() as session:
        assert session.scalar(select(ManagedVersionOperationRow)) is None


def test_request_uuid_cannot_change_version_or_action(env: Environment) -> None:
    target = env.prepare()
    env.prepare("nexus")
    request = uuid4()
    env.invoke(request_id=request)
    env.wait(target)
    with pytest.raises(ManagedVersionError, match="request_conflict"):
        env.invoke("stop", request_id=request)
    with pytest.raises(ManagedVersionError, match="request_conflict"):
        env.invoke(version="nexus", request_id=request)
    assert env.broker.provisions == env.broker.starts == 1


@pytest.mark.parametrize("fault", ["lost_provision_response", "uncertain_provision"])
def test_uncertain_provision_never_retries_after_new_service_or_new_request(
    env: Environment, fault: str
) -> None:
    env.prepare()
    env.broker.fault = fault
    request = uuid4()
    first = env.invoke(request_id=request)
    assert first.receipt_state == "uncertain" and not first.status.native_ready, first
    env.broker.fault = ""
    env.service = env.new_service()
    replay = env.invoke(request_id=request)
    assert replay.replayed and replay.receipt_state == "uncertain", replay
    fresh = env.invoke()
    assert (
        fresh.receipt_state == "blocked"
        and fresh.code == "managed_version_operation_pending"
    )
    assert env.broker.provisions == 1 and env.broker.starts == 0


def test_process_loss_after_child_claim_is_observed_without_dispatch(
    env: Environment,
) -> None:
    env.prepare()
    env.broker.fault = "process_loss"
    request = uuid4()
    with pytest.raises(ProcessLoss):
        env.invoke(request_id=request)
    env.broker.fault = ""
    env.service = env.new_service()
    replay = env.invoke(request_id=request)
    assert replay.replayed and replay.receipt_state in {"provisioning", "uncertain"}, (
        replay
    )
    assert env.broker.provisions == env.broker.starts == 0


def test_one_running_version_blocks_another_without_eviction(env: Environment) -> None:
    omega = env.prepare()
    env.prepare("nexus")
    env.invoke()
    env.wait(omega)
    original = env.broker.records[0]
    blocked = env.invoke(version="nexus")
    assert (
        blocked.receipt_state == "blocked" and blocked.code == "other_version_running"
    )
    assert env.broker.records == [original] and env.broker.stops == 0
    assert env.invoke("stop").stop_confirmed
    started = env.invoke(version="nexus")
    assert started.receipt_state == "complete", started
    assert (
        len(env.broker.records) == 2
        and sum(r.runtime_state == "running" for r in env.broker.records) == 1
    )


def test_concurrent_version_requests_cannot_dispatch_second_managed_version(
    env: Environment,
) -> None:
    omega = env.prepare()
    env.prepare("nexus")
    env.broker.entered, env.broker.finish = Event(), Event()
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending = worker.submit(env.invoke)
            assert env.broker.entered.wait(5)
            blocked = env.invoke(version="nexus")
            assert blocked.receipt_state == "blocked", blocked
            env.broker.finish.set()
            assert pending.result(timeout=5).receipt_state == "complete"
        assert env.broker.starts == 1
        assert env.broker.records[0].managed_instance_id == str(omega)
    finally:
        env.broker.finish.set()


def test_profile_identity_change_never_yields_old_stop_confirmation(
    env: Environment,
) -> None:
    target = env.prepare()
    env.invoke()
    env.wait(target)
    request = uuid4()
    assert env.invoke("stop", request_id=request).stop_confirmed
    env.broker.records[0] = replace(
        env.broker.records[0],
        container_id="e" * 64,
        runtime_state=ContainerRuntimeState.STOPPED,
    )
    replay = env.invoke("stop", request_id=request)
    assert replay.replayed and not replay.stop_confirmed


@pytest.mark.parametrize("version", ["matrix", "nexus", "omega", "piers"])
def test_each_supported_approved_cached_version_can_start_and_stop(
    env: Environment, version: str
) -> None:
    from typing import cast

    from kodi_lab.application.managed_versions import Version

    target = env.prepare(cast(Version, version))
    started = env.invoke(version=cast(Version, version))
    assert started.receipt_state == "complete", started
    env.wait(target)
    status = env.service.status(env.principal.client_id, env.authorize)
    selected = next(value for value in status.versions if value.version == version)
    assert selected.native_ready and selected.bridge_readiness == "blocked", selected
    assert env.invoke("stop", version=cast(Version, version)).stop_confirmed


def test_other_client_hold_blocks_stop_and_release_is_not_stop_proof(
    env: Environment,
) -> None:
    target = env.prepare()
    env.invoke()
    env.wait(target)
    first = env.principal
    second = env.actor("Other version actor")
    env.admin.grant_target(second.client_id, target)
    env.principal = second
    env.invoke()
    # Version Start no longer reserves running use. Make this hold deliberate.
    from datetime import timedelta

    from kodi_lab.storage.managed_activity import timestamp
    from kodi_lab.storage.models import ManagedInstanceUseRow

    with env.factory.begin() as session:
        use = session.get(
            ManagedInstanceUseRow, (str(target), f"mcp:{second.client_id}")
        )
        assert use is not None
        use.hold_until = timestamp(env.service._clock() + timedelta(minutes=15))
    env.principal = first
    request = uuid4()
    blocked = env.invoke("stop", request_id=request)
    assert blocked.receipt_state == "blocked" and not blocked.stop_confirmed
    assert blocked.code == "keep_awake" and env.broker.stops == 0
    env.principal = second
    assert env.invoke("stop").stop_confirmed
    env.principal = first
    replay = env.invoke("stop", request_id=request)
    assert replay.replayed and not replay.stop_confirmed
    assert env.broker.stops == 1


def test_web_hold_cannot_be_cleared_by_version_stop(env: Environment) -> None:
    from kodi_lab.storage.models import ManagedInstanceUseRow

    target = env.prepare()
    env.invoke()
    env.wait(target)
    env.resources.keep_awake(target, minutes=60)
    with env.factory() as session:
        hold = session.get(ManagedInstanceUseRow, (str(target), "web"))
        assert hold is not None
        original = hold.hold_until
    stopped = env.invoke("stop")
    assert not stopped.stop_confirmed and stopped.code == "keep_awake"
    with env.factory() as session:
        hold = session.get(ManagedInstanceUseRow, (str(target), "web"))
        assert hold is not None and hold.hold_until == original


def test_revocation_between_claim_and_dispatch_prevents_runtime_creation(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = env.prepare()
    original = env.service._before_dispatch

    def revoke(identity: str, phase: str, authorize: AuthorizeVersion) -> None:
        env.admin.remove_target_grant(env.principal.client_id, target)
        original(identity, phase, authorize)

    monkeypatch.setattr(env.service, "_before_dispatch", revoke)
    with pytest.raises(McpAuthorizationError):
        env.invoke()
    assert env.broker.provisions == env.broker.starts == 0
    with env.factory() as session:
        row = session.scalar(select(ManagedVersionOperationRow))
        assert row is not None and row.phase == "blocked"


def test_native_failure_is_not_ready_and_bridge_failure_does_not_restart(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from kodi_lab.kodi.readiness import UsabilityState
    from tests.application.test_managed_start import NativeReady

    target = env.prepare()
    failed = replace(
        NativeReady().wait_until_usable(), usability_state=UsabilityState.NOT_USABLE
    )
    monkeypatch.setattr(
        env.lifecycle,
        "_coordinator_factory",
        lambda *_: SimpleNamespace(wait_until_usable=lambda *_a, **_k: failed),
    )
    request = uuid4()
    env.invoke(request_id=request)
    env.wait(target)
    replay = env.invoke(request_id=request)
    assert not replay.status.native_ready and replay.status.state == "blocked"
    assert replay.status.bridge_readiness == "blocked"
    env.invoke()
    assert env.broker.starts == env.broker.provisions == 1


def test_revoked_runtime_start_eligibility_does_not_prevent_exact_stop(
    env: Environment,
) -> None:
    from kodi_lab.application.runtime_catalog import RuntimeCatalogAdministrationService

    target = env.prepare()
    env.invoke()
    env.wait(target)
    revision = UUID(str(env.broker.records[0].runtime_revision_id))
    RuntimeCatalogAdministrationService(env.factory).revoke_revision_approval(revision)
    stopped = env.invoke("stop")
    assert stopped.stop_confirmed and env.broker.stops == 1, stopped
    blocked = env.invoke()
    assert blocked.receipt_state == "blocked" and env.broker.starts == 1


def test_parent_receipts_protect_child_history_from_pruning(env: Environment) -> None:
    target = env.prepare()
    started = env.invoke()
    env.wait(target)
    stopped = env.invoke("stop")
    with env.factory.begin() as session:
        assert (
            ManagedInstanceLifecycleOperationRepository(session).prune_terminal(
                target, retain=0
            )
            == 0
        )
        for child in (
            started.provision_operation_id,
            started.lifecycle_operation_id,
            stopped.lifecycle_operation_id,
        ):
            assert child is not None
            assert (
                ManagedInstanceLifecycleOperationRepository(session).get(child)
                is not None
            )


def test_status_polling_and_request_replay_do_not_extend_holds(
    env: Environment,
) -> None:
    from datetime import timedelta

    from kodi_lab.storage.managed_activity import timestamp
    from kodi_lab.storage.models import ManagedInstanceUseRow

    target = env.prepare()
    request = uuid4()
    started = env.invoke(request_id=request)
    env.wait(target)
    # Model a receipt saved by pre-upgrade Start; keep its original hold evidence.
    legacy_hold = env.service._clock() + timedelta(minutes=15)
    with env.factory.begin() as session:
        receipt = session.get(ManagedVersionOperationRow, str(started.operation_id))
        use = session.get(
            ManagedInstanceUseRow, (str(target), f"mcp:{env.principal.client_id}")
        )
        assert receipt is not None and use is not None
        receipt.hold_until = use.hold_until = timestamp(legacy_hold)
    with env.factory() as session:
        before = session.execute(select(ManagedInstanceUseRow.__table__)).all()
    for _ in range(3):
        env.service.status(env.principal.client_id, env.authorize)
        assert env.invoke(request_id=request).hold_until == legacy_hold
    with env.factory() as session:
        assert session.execute(select(ManagedInstanceUseRow.__table__)).all() == before
    assert env.invoke("stop").stop_confirmed
    # Current-use protection ends, while the old immutable receipt survives.
    replay = env.invoke(request_id=request)
    assert replay.replayed and replay.hold_until == legacy_hold
    assert replay.status.state == "stopped"


def test_accepted_intent_resumes_after_lost_response_before_any_child(
    env: Environment,
) -> None:
    env.prepare()
    request = uuid4()
    saved, replayed = env.service._accept(
        env.principal.client_id,
        request,
        env.service.slot("omega"),
        "start",
        env.authorize,
    )
    assert saved.phase == "accepted" and not replayed
    env.service = env.new_service()
    resumed = env.invoke(request_id=request)
    assert resumed.replayed and resumed.operation_id == UUID(saved.id)
    assert resumed.receipt_state == "complete"
    assert env.broker.provisions == env.broker.starts == 1


def test_same_request_concurrency_is_busy_then_replays_without_another_start(
    env: Environment,
) -> None:
    from kodi_lab.application.managed_agent_lifecycle import ManagedAgentLifecycleError

    env.prepare()
    request = uuid4()
    env.broker.entered, env.broker.finish = Event(), Event()
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending = worker.submit(env.invoke, request_id=request)
            assert env.broker.entered.wait(5)
            with pytest.raises(ManagedAgentLifecycleError, match="target_busy"):
                env.invoke(request_id=request)
            env.broker.finish.set()
            original = pending.result(timeout=5)
        replay = env.invoke(request_id=request)
        assert replay.replayed and replay.operation_id == original.operation_id
        assert env.broker.provisions == env.broker.starts == 1
    finally:
        env.broker.finish.set()


def test_lost_stop_response_is_not_stop_confirmation_or_automatic_retry(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.broker.errors import ClientTransportError

    target = env.prepare()
    env.invoke()
    env.wait(target)
    stop = env.broker.stop_managed_container

    def lost(**kwargs: Any) -> Never:
        stop(**kwargs)
        raise ClientTransportError("fake lost Stop reply")

    monkeypatch.setattr(env.broker, "stop_managed_container", lost)
    request = uuid4()
    first = env.invoke("stop", request_id=request)
    assert first.receipt_state == "uncertain" and not first.stop_confirmed
    replay = env.invoke("stop", request_id=request)
    assert replay.replayed and not replay.stop_confirmed
    assert env.broker.stops == 1


def test_ordinary_web_start_and_version_start_share_one_broker_slot(
    env: Environment,
) -> None:
    from kodi_lab.broker.managed_capacity import ManagedCapacityError

    env.prepare()
    nexus = env.prepare("nexus")
    env.invoke(version="nexus")
    env.wait(nexus)
    assert env.invoke("stop", version="nexus").stop_confirmed
    env.broker.entered, env.broker.finish = Event(), Event()
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending = worker.submit(env.invoke)
            assert env.broker.entered.wait(5)
            with pytest.raises(ManagedCapacityError, match="managed_capacity_full"):
                env.lifecycle.start_managed(nexus)
            env.broker.finish.set()
            assert pending.result(timeout=5).receipt_state == "complete"
        assert sum(r.runtime_state == "running" for r in env.broker.records) == 1
        assert env.broker.starts == 2
    finally:
        env.broker.finish.set()


def test_current_kodi_workspace_source_bindings_and_git_bytes_survive(
    env: Environment, tmp_path: Path
) -> None:
    from kodi_lab.application.development_sessions import (
        DevelopmentSessionService,
        RetainedPolicyRequest,
        RetainedSelectionRequest,
    )
    from kodi_lab.config import Settings
    from kodi_lab.storage.models import (
        AgentWorkspaceRow,
        DevelopmentSessionRow,
        DevelopmentSourceRow,
        McpAgentWorkspaceBindingRow,
        McpClientDevelopmentSourceGrantRow,
    )
    from tests.application import test_mcp_workspace_development as workspace_fixture

    project = tmp_path / "independent-project"
    database = Path(env.factory.kw["bind"].url.database)
    workspace = workspace_fixture._environment(project, database_path=database)
    git_head = project / "sources/plugin.video.slice4/.git/HEAD"
    git_head.parent.mkdir()
    git_head.write_bytes(b"ref: refs/heads/user-work\n")
    target = env.prepare()
    env.invoke()
    env.wait(target)
    assert env.invoke("stop").stop_confirmed
    sessions = DevelopmentSessionService(
        env.factory,
        env.broker.managed_container_inventory,
        data_root=Settings().data_root,
    )
    sessions.configure_retained(
        RetainedPolicyRequest(
            enabled=True, expected_generation=0, omega_target_id=target
        )
    )
    preview = sessions.selection_preview(target, workspace.workspace_id)
    sessions.select_retained(
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
        DevelopmentSessionRow,
        McpAgentWorkspaceBindingRow,
        McpClientDevelopmentSourceGrantRow,
    )
    with env.factory() as session:
        before = [session.execute(select(model.__table__)).all() for model in models]
    files = {
        str(path.relative_to(project)): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }
    env.invoke()
    env.wait(target)
    assert env.invoke("stop").stop_confirmed
    with env.factory() as session:
        assert [
            session.execute(select(model.__table__)).all() for model in models
        ] == before
    assert {
        str(path.relative_to(project)): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    } == files


@pytest.mark.parametrize("mode", ["view-only", "interactive"])
def test_version_stop_protects_existing_human_viewer(
    env: Environment, mode: str
) -> None:
    from kodi_lab.application.novnc_control import HumanViewerOwnership
    from kodi_lab.broker.novnc import ViewBinding
    from kodi_lab.storage.models import NovncSessionRow

    target = env.prepare()
    env.invoke()
    env.wait(target)
    record = env.broker.records[0]
    with env.factory() as session:
        instance = session.get(ManagedInstanceRow, str(target))
        assert instance is not None
        epoch = instance.runtime_state_revision
    attempt = uuid4()
    HumanViewerOwnership(env.factory).create(
        target,
        attempt,
        "f" * 64,
        ViewBinding(
            record.container_id,
            record.image_id,
            "fixture",
            (str(record.runtime_revision_id), epoch),
        ),
        mode,
    )
    result = env.invoke("stop")
    assert not result.stop_confirmed and result.code == "active_viewer_or_input", result
    assert env.broker.stops == 0
    with env.factory() as session:
        viewer = session.get(NovncSessionRow, str(target))
        assert viewer is not None and viewer.attempt == str(attempt)


def test_disabled_owner_cannot_leave_an_undispatched_intent_locking_the_slot(
    env: Environment,
) -> None:
    target = env.prepare()
    request = uuid4()
    saved, _ = env.service._accept(
        env.principal.client_id,
        request,
        env.service.slot("omega"),
        "start",
        env.authorize,
    )
    original = env.principal
    env.admin.set_client_enabled(original.client_id, False)
    env.principal = env.actor("Successor authorized actor")
    env.admin.grant_target(env.principal.client_id, target)
    result = env.invoke()
    assert result.receipt_state == "complete", result
    assert env.broker.provisions == env.broker.starts == 1
    with env.factory() as session:
        old = session.get(ManagedVersionOperationRow, saved.id)
        assert old is not None and old.phase == "blocked"
        assert old.code == "client_authority_revoked" and old.request_id == str(request)


def test_atomic_registration_uses_the_coordinator_data_root(
    env: Environment, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = env.prepare()
    # Existing services retain their configured state root. A different default
    # must not redirect the registration service's provenance-key lookup.
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", str(tmp_path / "unrelated-root"))
    row, _ = env.service._accept(
        env.principal.client_id,
        uuid4(),
        env.service.slot("omega"),
        "start",
        env.authorize,
    )
    assert row.phase == "accepted", row.code
    with env.factory() as session:
        assert session.get(ManagedInstanceRow, str(target)) is not None


@pytest.mark.parametrize("legacy_target", [False, True])
def test_conflicting_profiles_are_reported_without_adoption_or_removal(
    env: Environment, legacy_target: bool
) -> None:
    from kodi_lab.storage.repositories import TargetRepository
    from tests.storage.test_repositories import make_target

    target = env.prepare()
    env.invoke()
    env.wait(target)
    assert env.invoke("stop").stop_confirmed
    original = env.broker.records[0]
    other = target
    if legacy_target:
        other = uuid4()
        with env.factory.begin() as session:
            TargetRepository(session).create(
                replace(make_target("Historical Omega"), id=other, kodi_version="21.3")
            )
            instance = session.get(ManagedInstanceRow, str(target))
            assert instance is not None
            values = {
                column.key: getattr(instance, column.key)
                for column in ManagedInstanceRow.__table__.columns
            }
            values["target_id"] = str(other)
            session.add(ManagedInstanceRow(**values))
    env.broker.records.append(
        replace(original, managed_instance_id=str(other), container_id="d" * 64)
    )
    before = list(env.broker.records)
    profile = env.root / "profiles" / str(target) / "settings.xml"
    profile.write_bytes(b"preserved existing profile")
    result = env.invoke()
    assert result.receipt_state == "blocked", result
    assert result.code == (
        "canonical_slot_requires_review"
        if legacy_target
        else "managed_version_slot_conflict"
    )
    assert env.broker.records == before
    assert env.broker.starts == env.broker.stops == env.broker.provisions == 1
    assert profile.read_bytes() == b"preserved existing profile"


def test_empty_stop_is_confirmed_without_creating_a_profile(env: Environment) -> None:
    target = env.prepare(cached=False)
    request = uuid4()
    first = env.invoke("stop", request_id=request)
    assert first.stop_confirmed and first.code == "already_stopped", first
    replay = env.invoke("stop", request_id=request)
    assert replay.stop_confirmed and replay.replayed
    assert replay.operation_id == first.operation_id
    assert env.broker.provisions == env.broker.starts == env.broker.stops == 0
    with env.factory() as session:
        assert session.get(ManagedInstanceRow, str(target)) is None
