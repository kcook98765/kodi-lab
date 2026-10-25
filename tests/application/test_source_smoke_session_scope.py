"""Existing generic source smoke keeps physical queue scope and typed ownership."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import Table, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_smoke_tests import (
    SMOKE_OPERATION_BOUND,
    DevelopmentSourceSmokeTestConflictError,
    DevelopmentSourceSmokeTestOperationCoordinator,
    DevelopmentSourceSmokeTestService,
)
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationRequiredError,
)
from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.domain import (
    AgentWorkspaceState,
    DevelopmentSourceSmokeTestOperationState,
    ManagedInstanceMutationKind,
)
from kodi_lab.kodi.addons import KodiAddonSmokeResult, KodiAddonSmokeTimeoutError
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    DevelopmentSourceRow,
    DevelopmentSourceSmokeTestOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedStartReservationRow,
    RuntimeRevisionRow,
)
from kodi_lab.storage.repositories import DevelopmentSourceSmokeTestOperationRepository
from tests.application import test_development_source_smoke_tests as fixtures
from tests.application.test_development_source_smoke_tests import factory as factory
from tests.storage.test_migrations import migration_templates as migration_templates


class Clock:
    def __init__(self) -> None:
        self.now = fixtures.NOW

    def __call__(self) -> datetime:
        return self.now


def bind(factory: sessionmaker[Session], *, defect: str = "") -> None:
    with factory.begin() as session:
        source_id = str(fixtures.SOURCE_ID)
        addon_id = fixtures.ADDON_ID
        if defect == "source":
            source = session.get(DevelopmentSourceRow, source_id)
            assert source is not None
            values = {
                column.name: getattr(source, column.name)
                for column in source.__table__.columns
            }
            source_id, addon_id = str(uuid4()), "plugin.video.other"
            values.update(
                id=source_id,
                logical_name="Other source",
                relative_locator="other-source",
                addon_id=addon_id,
            )
            session.add(DevelopmentSourceRow(**values))
            session.flush()
        revision_id = str(fixtures.REVISION_ID)
        if defect == "runtime":
            original = session.get(RuntimeRevisionRow, revision_id)
            assert original is not None
            values = {
                column.name: getattr(original, column.name)
                for column in original.__table__.columns
            }
            revision_id = str(uuid4())
            values.update(id=revision_id, revision_number=2)
            session.add(RuntimeRevisionRow(**values))
            session.flush()
        workspace_id = str(uuid4())
        session.add(
            AgentWorkspaceRow(
                id=workspace_id,
                development_source_id=source_id,
                source_addon_id=addon_id,
                source_logical_name="Example",
                source_observed_name="Example",
                source_observed_version=fixtures.VERSION,
                state=AgentWorkspaceState.READY,
                baseline_artifact_id=str(uuid4()),
                baseline_manifest_digest="sha256:" + "1" * 64,
                current_tree_digest="sha256:" + "1" * 64,
                capture_started_at=fixtures.NOW.isoformat(),
                captured_at=fixtures.NOW.isoformat(),
                created_at=fixtures.NOW.isoformat(),
                updated_at=fixtures.NOW.isoformat(),
            )
        )
        session.flush()
        session.add(
            DevelopmentSessionRow(
                generation=1,
                request_id=str(uuid4()),
                target_id=str(uuid4())
                if defect == "target"
                else str(fixtures.INSTANCE_ID),
                runtime_revision_id=revision_id,
                workspace_id=workspace_id,
                source_id=source_id,
                container_id="f" * 64
                if defect == "container"
                else fixtures.CONTAINER_ID,
                phase="uncertain" if defect == "phase" else "bound",
                created_at=fixtures.NOW.isoformat(),
                updated_at=fixtures.NOW.isoformat(),
            )
        )
        session.flush()
        session.add(DevelopmentSessionPolicyRow(id=1, active_generation=1))
        if defect == "pending":
            session.add(
                ManagedStartReservationRow(
                    managed_instance_id=str(fixtures.INSTANCE_ID),
                    runtime_revision_id=str(fixtures.REVISION_ID),
                    operation_id=str(uuid4()),
                    container_id=fixtures.CONTAINER_ID,
                    phase="uncertain",
                    created_at=fixtures.NOW.isoformat(),
                )
            )


def build(
    factory: sessionmaker[Session], tmp_path: Path, *, seed_session: bool = True
) -> tuple[
    DevelopmentSourceSmokeTestOperationCoordinator,
    DevelopmentSourceSmokeTestService,
    fixtures.FakeSmoke,
    Clock,
]:
    if seed_session:
        bind(factory)
    clock = Clock()
    coordinator = DevelopmentSourceSmokeTestOperationCoordinator(factory, clock=clock)
    queued = coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    smoke = fixtures.FakeSmoke(KodiAddonSmokeResult(1, 12))
    broker = fixtures.FakeBroker()
    service = DevelopmentSourceSmokeTestService(
        factory,
        broker,
        DevelopmentSourceValidator(tmp_path),
        coordinator,
        lease,
        inventory_adapter_factory=lambda _o, _p: fixtures.FakeInventory(
            [fixtures.target()]
        ),
        details_adapter_factory=lambda _o, _p: fixtures.FakeDetails(),
        smoke_adapter_factory=lambda _o, _p: smoke,
    )
    return coordinator, service, smoke, clock


def test_new_selected_queue_persists_pin_and_completes_existing_native_sequence(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    coordinator, service, smoke, _clock = build(factory, tmp_path)
    operation = coordinator.get(service._lease.operation_id)
    assert (
        operation is not None
        and operation.expected_container_id == fixtures.CONTAINER_ID
    )
    result = service.run()
    assert result.state is DevelopmentSourceSmokeTestOperationState.SUCCEEDED
    assert smoke.calls == [fixtures.ADDON_ID]


@pytest.mark.parametrize("defect", ["target", "source", "runtime", "phase", "pending"])
def test_wrong_selected_scope_refuses_before_queue_or_lease(
    factory: sessionmaker[Session],
    defect: str,
) -> None:
    bind(factory, defect=defect)
    coordinator = DevelopmentSourceSmokeTestOperationCoordinator(
        factory, clock=lambda: fixtures.NOW
    )
    with pytest.raises(DevelopmentSessionConflict):
        coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    with factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(DevelopmentSourceSmokeTestOperationRow)
            )
            == 0
        )
        assert session.scalar(select(ManagedInstanceMutationLeaseRow)) is None


def test_changed_queued_scope_preserves_unclaimed_original_fence(
    factory: sessionmaker[Session],
) -> None:
    bind(factory)
    coordinator = DevelopmentSourceSmokeTestOperationCoordinator(
        factory, clock=lambda: fixtures.NOW
    )
    queued = coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    with factory.begin() as session:
        current = session.get(DevelopmentSessionRow, 1)
        assert current is not None
        current.container_id = "f" * 64
    with pytest.raises(DevelopmentSessionConflict):
        coordinator.acquire(queued.operation_id)
    with factory() as session:
        operation = session.get(
            DevelopmentSourceSmokeTestOperationRow, str(queued.operation_id)
        )
        lease = session.get(ManagedInstanceMutationLeaseRow, str(fixtures.INSTANCE_ID))
        assert (
            operation is not None
            and operation.state is DevelopmentSourceSmokeTestOperationState.QUEUED
        )
        assert operation.expected_container_id == fixtures.CONTAINER_ID
        assert lease is not None and lease.lease_token_digest is None


def test_repository_acquire_cannot_clear_or_rebind_a_positive_queue_pin(
    factory: sessionmaker[Session],
) -> None:
    bind(factory)
    coordinator = DevelopmentSourceSmokeTestOperationCoordinator(
        factory, clock=lambda: fixtures.NOW
    )
    queued = coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    for cid in (None, "f" * 64):
        with factory.begin() as session:
            assert not DevelopmentSourceSmokeTestOperationRepository(session).acquire(
                queued.operation_id,
                token_digest="1" * 64,
                expected_container_id=cid,
                now=fixtures.NOW,
                lease_expires_at=fixtures.NOW + SMOKE_OPERATION_BOUND,
            )
    operation = coordinator.get(queued.operation_id)
    assert (
        operation is not None
        and operation.state is DevelopmentSourceSmokeTestOperationState.QUEUED
    )
    assert operation.expected_container_id == fixtures.CONTAINER_ID


@pytest.mark.parametrize(
    "defect", ["foreign", "kind", "token", "acquired", "dispatched", "reconciliation"]
)
def test_acquire_never_claims_foreign_or_malformed_queued_authority(
    factory: sessionmaker[Session],
    defect: str,
) -> None:
    bind(factory)
    coordinator = DevelopmentSourceSmokeTestOperationCoordinator(
        factory, clock=lambda: fixtures.NOW
    )
    queued = coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    with factory.begin() as session:
        row = session.get(ManagedInstanceMutationLeaseRow, str(fixtures.INSTANCE_ID))
        op = session.get(
            DevelopmentSourceSmokeTestOperationRow, str(queued.operation_id)
        )
        assert row is not None and op is not None
        if defect == "foreign":
            row.operation_id = str(uuid4())
        elif defect == "kind":
            row.operation_kind = ManagedInstanceMutationKind.LIFECYCLE
        elif defect == "token":
            row.lease_token_digest = "a" * 64
        elif defect == "acquired":
            row.acquired_at = fixtures.NOW.isoformat()
        elif defect == "dispatched":
            op.invocation_dispatched = True
        else:
            op.reconciliation_required = True
    with pytest.raises(DevelopmentSourceSmokeTestConflictError):
        coordinator.acquire(queued.operation_id)
    operation = coordinator.get(queued.operation_id)
    assert (
        operation is not None
        and operation.state is DevelopmentSourceSmokeTestOperationState.QUEUED
    )


def change(factory: sessionmaker[Session], kind: str) -> None:
    with factory.begin() as session:
        current = session.get(DevelopmentSessionRow, 1)
        policy = session.get(DevelopmentSessionPolicyRow, 1)
        assert current is not None and policy is not None
        if kind == "phase":
            current.phase = "uncertain"
        elif kind == "container":
            current.container_id = "f" * 64
        else:
            current.phase = "closed"
            values = {
                column.name: getattr(current, column.name)
                for column in current.__table__.columns
            }
            values.update(generation=2, request_id=str(uuid4()), phase="bound")
            session.add(DevelopmentSessionRow(**values))
            session.flush()
            policy.active_generation = 2


@pytest.mark.parametrize("boundary", ["observation", "invocation"])
@pytest.mark.parametrize("kind", ["phase", "container", "successor"])
def test_late_selection_change_never_invokes_or_reattributes_native_work(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    kind: str,
) -> None:
    coordinator, service, smoke, _clock = build(factory, tmp_path)
    method = (
        "set_expected_container"
        if boundary == "observation"
        else "mark_invocation_dispatched"
    )
    original = getattr(coordinator, method)

    def changed(*args: Any, **kwargs: Any) -> Any:
        change(factory, kind)
        return original(*args, **kwargs)

    monkeypatch.setattr(coordinator, method, changed)
    result = service.run()
    assert result.state is DevelopmentSourceSmokeTestOperationState.FAILED
    assert result.failure_reason in {
        "development_session_identity_changed",
        "development_session_requires_review",
    }
    operation = coordinator.get(result.operation_id)
    assert operation is not None and not operation.invocation_dispatched
    assert not smoke.calls
    with factory() as session:
        assert session.scalar(select(ManagedInstanceMutationLeaseRow)) is None


@pytest.mark.parametrize("defect", ["foreign", "kind", "token", "expired"])
def test_live_marker_and_cleanup_cannot_borrow_or_clear_other_authority(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    coordinator, service, smoke, _clock = build(factory, tmp_path)
    original = coordinator.mark_invocation_dispatched

    def changed(*args: Any, **kwargs: Any) -> Any:
        with factory.begin() as session:
            row = session.get(
                ManagedInstanceMutationLeaseRow, str(fixtures.INSTANCE_ID)
            )
            assert row is not None
            if defect == "foreign":
                row.operation_id = str(uuid4())
            elif defect == "kind":
                row.operation_kind = ManagedInstanceMutationKind.LIFECYCLE
            elif defect == "token":
                row.lease_token_digest = "f" * 64
            else:
                row.lease_expires_at = (fixtures.NOW - timedelta(seconds=1)).strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                )
        return original(*args, **kwargs)

    monkeypatch.setattr(coordinator, "mark_invocation_dispatched", changed)
    with pytest.raises(DevelopmentSourceSmokeTestConflictError):
        service.run()
    assert not smoke.calls
    with factory() as session:
        row = session.get(
            DevelopmentSourceSmokeTestOperationRow, str(service._lease.operation_id)
        )
        assert (
            row is not None
            and row.state is DevelopmentSourceSmokeTestOperationState.RUNNING
        )
        assert not row.invocation_dispatched
        assert session.scalar(select(ManagedInstanceMutationLeaseRow)) is not None


def test_legacy_nullable_queue_keeps_observed_binding_and_cannot_overwrite_it(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    coordinator, service, smoke, _clock = build(factory, tmp_path, seed_session=False)
    operation = coordinator.get(service._lease.operation_id)
    assert operation is not None and operation.expected_container_id is None
    result = service.run()
    assert (
        result.state is DevelopmentSourceSmokeTestOperationState.SUCCEEDED
        and smoke.calls == [fixtures.ADDON_ID]
    )
    operation = coordinator.get(result.operation_id)
    assert (
        operation is not None
        and operation.expected_container_id == fixtures.CONTAINER_ID
    )


def test_scope_heartbeat_does_not_extend_the_existing_ninety_second_bound(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    coordinator, service, _smoke, clock = build(factory, tmp_path)
    original = coordinator.get(service._lease.operation_id)
    assert original is not None
    expiry = original.lease_expires_at
    assert expiry is not None
    clock.now += timedelta(seconds=5)
    coordinator.set_expected_container(service._lease, fixtures.CONTAINER_ID)
    coordinator.mark_invocation_dispatched(service._lease)
    current = coordinator.get(service._lease.operation_id)
    assert current is not None and current.lease_expires_at == expiry
    with factory() as session:
        lease = session.get(ManagedInstanceMutationLeaseRow, str(fixtures.INSTANCE_ID))
        assert lease is not None and lease.lease_expires_at == expiry.strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )


@pytest.mark.parametrize("after_commit", [False, True])
def test_marker_commit_interruption_preserves_authoritative_dispatch_fence(
    factory: sessionmaker[Session],
    tmp_path: Path,
    after_commit: bool,
) -> None:
    coordinator, service, smoke, _clock = build(factory, tmp_path)
    coordinator.set_expected_container(service._lease, fixtures.CONTAINER_ID)
    armed = False

    def before(session: Session) -> None:
        nonlocal armed
        row = session.get(
            DevelopmentSourceSmokeTestOperationRow, str(service._lease.operation_id)
        )
        if row is not None and row.invocation_dispatched:
            armed = True
            if not after_commit:
                raise KeyboardInterrupt("before marker commit")

    def after(_session: Session) -> None:
        if armed and after_commit:
            raise KeyboardInterrupt("after marker commit")

    event.listen(Session, "before_commit", before)
    event.listen(Session, "after_commit", after)
    try:
        with pytest.raises(KeyboardInterrupt):
            coordinator.mark_invocation_dispatched(service._lease)
    finally:
        event.remove(Session, "before_commit", before)
        event.remove(Session, "after_commit", after)
    operation = coordinator.get(service._lease.operation_id)
    assert operation is not None and operation.invocation_dispatched == after_commit
    assert (
        operation.state is DevelopmentSourceSmokeTestOperationState.RUNNING
        and not smoke.calls
    )
    with factory() as session:
        assert session.scalar(select(ManagedInstanceMutationLeaseRow)) is not None


def test_selected_smoke_completes_on_actual_upgraded_schema(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic.script import ScriptDirectory

    from kodi_lab.storage.engine import create_database_engine, create_session_factory
    from kodi_lab.storage.models import (
        DevelopmentSourceDeploymentOperationRow,
        DevelopmentSourceDeploymentRow,
        ManagedInstanceDevelopmentSourceRow,
        ManagedInstanceRow,
        RuntimeDefinitionRow,
        RuntimeExecutionProfileRow,
        TargetRow,
    )
    from tests.storage.test_migrations import migration_setup

    config, url = migration_setup(
        monkeypatch, tmp_path / "migration", start_revision="head"
    )
    engine = create_database_engine(url)
    migrated = create_session_factory(engine)
    bind(factory)
    models = (
        RuntimeDefinitionRow,
        RuntimeRevisionRow,
        RuntimeExecutionProfileRow,
        TargetRow,
        ManagedInstanceRow,
        DevelopmentSourceRow,
        ManagedInstanceDevelopmentSourceRow,
        DevelopmentSourceDeploymentOperationRow,
        DevelopmentSourceDeploymentRow,
        AgentWorkspaceRow,
        DevelopmentSessionRow,
        DevelopmentSessionPolicyRow,
    )
    with factory() as source, migrated.begin() as destination:
        for model in models:
            table = cast(Table, model.__table__)
            for values in source.execute(select(table)).mappings():
                destination.execute(table.insert().values(**dict(values)))
    # The copied policy is active; skip only reseeding the same fixture rows.
    coordinator, service, smoke, _clock = build(migrated, tmp_path, seed_session=False)
    queued_binding = coordinator.get(service._lease.operation_id)
    assert queued_binding is not None
    assert queued_binding.expected_container_id == fixtures.CONTAINER_ID
    result = service.run()
    assert (
        result.state is DevelopmentSourceSmokeTestOperationState.SUCCEEDED
        and smoke.calls == [fixtures.ADDON_ID]
    )
    with migrated() as session:
        assert (
            session.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(config).get_current_head()
        )
        assert session.scalar(text("PRAGMA integrity_check")) == "ok"
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
    engine.dispose()


def test_preexisting_nullable_queue_binds_only_at_observation_under_current_scope(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    bind(factory)
    coordinator = DevelopmentSourceSmokeTestOperationCoordinator(
        factory, clock=lambda: fixtures.NOW
    )
    queued = coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    with factory.begin() as session:
        row = session.get(
            DevelopmentSourceSmokeTestOperationRow, str(queued.operation_id)
        )
        assert row is not None
        # Data-only fixture for an older queued row that never retained a pin.
        row.expected_container_id = None
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    before = coordinator.get(queued.operation_id)
    assert before is not None and before.expected_container_id is None
    coordinator.set_expected_container(lease, fixtures.CONTAINER_ID)
    after = coordinator.get(queued.operation_id)
    assert after is not None and after.expected_container_id == fixtures.CONTAINER_ID
    with pytest.raises(
        (DevelopmentSessionConflict, DevelopmentSourceSmokeTestConflictError)
    ):
        coordinator.set_expected_container(lease, "f" * 64)
    still = coordinator.get(queued.operation_id)
    assert still is not None and still.expected_container_id == fixtures.CONTAINER_ID


def test_active_session_timeout_keeps_original_dispatched_reconciliation(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    coordinator, service, smoke, _clock = build(factory, tmp_path)
    smoke.outcome = KodiAddonSmokeTimeoutError(10_000)
    result = service.run()
    assert (
        result.state is DevelopmentSourceSmokeTestOperationState.RECONCILIATION_REQUIRED
    )
    assert result.reconciliation_required and smoke.calls == [fixtures.ADDON_ID]
    operation = coordinator.get(result.operation_id)
    assert operation is not None and operation.invocation_dispatched
    assert operation.expected_container_id == fixtures.CONTAINER_ID
    with pytest.raises(ManagedInstanceReconciliationRequiredError):
        coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
