"""Queued stopped copy retains its profile; unclaimed refusal cannot clear work."""

from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_deployments import (
    DevelopmentSourceDeploymentConflictError,
    DevelopmentSourceDeploymentLeaseLostError,
    DevelopmentSourceDeploymentService,
)
from kodi_lab.domain import DevelopmentSourceDeploymentOperationState
from kodi_lab.storage.models import (
    DevelopmentSourceDeploymentOperationRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    RuntimeRevisionRow,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceDeploymentOperationRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
)
from tests.application import test_development_source_deployments as fixtures
from tests.application.test_development_source_deployments import factory as factory
from tests.storage.test_migrations import migration_templates as migration_templates


def test_queue_persists_the_observed_stopped_profile_before_worker_claim(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    broker = fixtures.FakeBroker()
    coordinator = fixtures._coordinator(factory, tmp_path, broker, fixtures.Clock())
    status = coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    operation = coordinator.get(status.operation_id)
    assert (
        operation is not None
        and operation.expected_container_id == fixtures.CONTAINER_ID
    )
    assert operation.state is DevelopmentSourceDeploymentOperationState.QUEUED
    assert not broker.calls


def test_profile_drift_refuses_copy_and_releases_only_unclaimed_work(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = fixtures.FakeBroker()
    coordinator = fixtures._coordinator(factory, tmp_path, broker, fixtures.Clock())
    original = fixtures.CONTAINER_ID
    status = coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    monkeypatch.setattr(fixtures, "CONTAINER_ID", "f" * 64)
    with pytest.raises(DevelopmentSourceDeploymentConflictError, match="profile"):
        coordinator.acquire(status.operation_id)
    operation = coordinator.get(status.operation_id)
    assert (
        operation is not None
        and operation.state is DevelopmentSourceDeploymentOperationState.FAILED
    )
    assert (
        operation.expected_container_id == original
        and not operation.reconciliation_required
    )
    assert not broker.calls
    with factory() as session:
        assert (
            ManagedInstanceMutationLeaseRepository(session).current(
                fixtures.INSTANCE_ID
            )
            is None
        )
        assert GlobalHeavyOperationLeaseRepository(session).current() is None


def test_repository_acquire_cannot_overwrite_a_queued_profile_binding(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    coordinator = fixtures._coordinator(
        factory, tmp_path, fixtures.FakeBroker(), fixtures.Clock()
    )
    status = coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    with factory.begin() as session:
        accepted = DevelopmentSourceDeploymentOperationRepository(session).acquire(
            status.operation_id,
            token_digest="1" * 64,
            expected_container_id="f" * 64,
            now=fixtures.NOW,
            lease_expires_at=fixtures.NOW + timedelta(minutes=1),
        )
        assert not accepted
    operation = coordinator.get(status.operation_id)
    assert (
        operation is not None
        and operation.state is DevelopmentSourceDeploymentOperationState.QUEUED
    )
    assert operation.expected_container_id == fixtures.CONTAINER_ID


def test_legacy_nullable_queue_still_gets_its_existing_claim_time_binding(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    broker = fixtures.FakeBroker()
    coordinator = fixtures._coordinator(factory, tmp_path, broker, fixtures.Clock())
    status = coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    with factory.begin() as session:
        row = session.get(
            DevelopmentSourceDeploymentOperationRow, str(status.operation_id)
        )
        assert row is not None
        row.expected_container_id = None
    lease = coordinator.acquire(status.operation_id)
    assert lease is not None and lease.expected_container_id == fixtures.CONTAINER_ID
    DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    operation = coordinator.get(status.operation_id)
    assert (
        operation is not None
        and operation.state is DevelopmentSourceDeploymentOperationState.SUCCEEDED
    )


def test_trusted_queue_precondition_is_atomic_before_operation_and_lease_reservation(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    broker = fixtures.FakeBroker()
    coordinator = fixtures._coordinator(factory, tmp_path, broker, fixtures.Clock())

    def changed(session: Session) -> None:
        instance = session.get(ManagedInstanceRow, str(fixtures.INSTANCE_ID))
        assert instance is not None
        instance.notes = "must roll back"
        raise DevelopmentSourceDeploymentConflictError("selected session changed")

    with pytest.raises(
        DevelopmentSourceDeploymentConflictError, match="selected session changed"
    ):
        coordinator.queue(
            fixtures.INSTANCE_ID, fixtures.SOURCE_ID, precondition=changed
        )
    with factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(
                    DevelopmentSourceDeploymentOperationRow
                )
            )
            == 0
        )
        assert session.scalar(select(ManagedInstanceMutationLeaseRow)) is None
        assert session.scalar(select(GlobalHeavyOperationLeaseRow)) is None
        instance = session.get(ManagedInstanceRow, str(fixtures.INSTANCE_ID))
        assert instance is not None and instance.notes is None
    assert not broker.calls


def test_claimed_work_is_never_terminalized_by_another_acquire_after_profile_drift(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = fixtures.FakeBroker()
    coordinator = fixtures._coordinator(factory, tmp_path, broker, fixtures.Clock())
    status = coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    lease = coordinator.acquire(status.operation_id)
    assert lease is not None
    monkeypatch.setattr(fixtures, "CONTAINER_ID", "f" * 64)
    with pytest.raises(DevelopmentSourceDeploymentLeaseLostError):
        coordinator.acquire(status.operation_id)
    operation = coordinator.get(status.operation_id)
    assert (
        operation is not None
        and operation.state is DevelopmentSourceDeploymentOperationState.RUNNING
    )
    assert operation.lease_token_digest == lease.token_digest
    with factory() as session:
        mutation = ManagedInstanceMutationLeaseRepository(session).current(
            fixtures.INSTANCE_ID
        )
        heavy = GlobalHeavyOperationLeaseRepository(session).current()
        assert mutation is not None and heavy is not None
        assert (
            mutation.lease_token_digest
            == heavy.lease_token_digest
            == lease.token_digest
        )
    assert not broker.calls


@pytest.mark.parametrize("foreign", [False, True])
def test_malformed_or_foreign_lease_keeps_the_queued_fence_during_profile_drift(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    foreign: bool,
) -> None:
    coordinator = fixtures._coordinator(
        factory, tmp_path, fixtures.FakeBroker(), fixtures.Clock()
    )
    status = coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    with factory.begin() as session:
        heavy = session.scalar(select(GlobalHeavyOperationLeaseRow))
        assert heavy is not None
        if foreign:
            heavy.operation_id = str(uuid4())
        else:
            heavy.lease_token_digest = "a" * 64
            heavy.lease_expires_at = "2026-09-16T23:00:00.000000Z"
    monkeypatch.setattr(fixtures, "CONTAINER_ID", "f" * 64)
    with pytest.raises(DevelopmentSourceDeploymentLeaseLostError):
        coordinator.acquire(status.operation_id)
    operation = coordinator.get(status.operation_id)
    assert (
        operation is not None
        and operation.state is DevelopmentSourceDeploymentOperationState.QUEUED
    )
    with factory() as session:
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(fixtures.INSTANCE_ID))
            is not None
        )
        assert session.scalar(select(GlobalHeavyOperationLeaseRow)) is not None


def test_runtime_retarget_before_claim_is_a_known_unclaimed_refusal(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    broker = fixtures.FakeBroker()
    coordinator = fixtures._coordinator(factory, tmp_path, broker, fixtures.Clock())
    status = coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    with factory.begin() as session:
        previous = session.get(RuntimeRevisionRow, str(fixtures.REVISION_ID))
        instance = session.get(ManagedInstanceRow, str(fixtures.INSTANCE_ID))
        assert previous is not None and instance is not None
        values = {
            column.name: getattr(previous, column.name)
            for column in previous.__table__.columns
        }
        values.update(id=str(uuid4()), revision_number=2)
        session.add(RuntimeRevisionRow(**values))
        session.flush()
        instance.runtime_revision_id = values["id"]
    with pytest.raises(DevelopmentSourceDeploymentConflictError, match="scope"):
        coordinator.acquire(status.operation_id)
    operation = coordinator.get(status.operation_id)
    assert (
        operation is not None
        and operation.state is DevelopmentSourceDeploymentOperationState.FAILED
    )
    assert not broker.calls


@pytest.mark.parametrize("after_commit", [False, True])
def test_refusal_commit_cancellation_preserves_the_authoritative_marker(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_commit: bool,
) -> None:
    broker = fixtures.FakeBroker()
    coordinator = fixtures._coordinator(factory, tmp_path, broker, fixtures.Clock())
    status = coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    monkeypatch.setattr(fixtures, "CONTAINER_ID", "f" * 64)
    original = Session.commit
    armed = True

    def commit(session: Session) -> None:
        nonlocal armed
        row = session.get(
            DevelopmentSourceDeploymentOperationRow, str(status.operation_id)
        )
        if (
            armed
            and row is not None
            and row.state is DevelopmentSourceDeploymentOperationState.FAILED
        ):
            armed = False
            if after_commit:
                original(session)
            raise KeyboardInterrupt("fixture interrupted commit acknowledgement")
        original(session)

    monkeypatch.setattr(Session, "commit", commit)
    with pytest.raises(KeyboardInterrupt):
        coordinator.acquire(status.operation_id)
    operation = coordinator.get(status.operation_id)
    assert operation is not None
    assert operation.state is (
        DevelopmentSourceDeploymentOperationState.FAILED
        if after_commit
        else DevelopmentSourceDeploymentOperationState.QUEUED
    )
    with factory() as session:
        mutation = ManagedInstanceMutationLeaseRepository(session).current(
            fixtures.INSTANCE_ID
        )
        heavy = GlobalHeavyOperationLeaseRepository(session).current()
        assert (mutation is None) == (heavy is None) == after_commit
    assert not broker.calls


def test_queue_pin_and_claim_work_on_actual_upgraded_schema(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic.script import ScriptDirectory
    from sqlalchemy import Table, text

    from kodi_lab.storage.engine import create_database_engine, create_session_factory
    from kodi_lab.storage.models import (
        DevelopmentSourceRow,
        ManagedInstanceDevelopmentSourceRow,
        RuntimeDefinitionRow,
        RuntimeExecutionProfileRow,
        TargetRow,
    )
    from tests.storage.test_migrations import migration_setup

    config, url = migration_setup(
        monkeypatch, tmp_path / "migration", start_revision="head"
    )
    expected_head = ScriptDirectory.from_config(config).get_current_head()
    assert expected_head is not None
    engine = create_database_engine(url)
    migrated = create_session_factory(engine)
    # Copy fixture data only, in FK order. Keep the real migrated schema intact.
    rows = (
        RuntimeDefinitionRow,
        RuntimeRevisionRow,
        RuntimeExecutionProfileRow,
        TargetRow,
        ManagedInstanceRow,
        DevelopmentSourceRow,
        ManagedInstanceDevelopmentSourceRow,
    )
    with factory() as source, migrated.begin() as destination:
        for model in rows:
            table = cast(Table, model.__table__)
            for values in source.execute(select(table)).mappings():
                destination.execute(table.insert().values(**dict(values)))
    coordinator = fixtures._coordinator(
        migrated, tmp_path, fixtures.FakeBroker(), fixtures.Clock()
    )
    status = coordinator.queue(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    operation = coordinator.get(status.operation_id)
    assert (
        operation is not None
        and operation.expected_container_id == fixtures.CONTAINER_ID
    )
    lease = coordinator.acquire(status.operation_id)
    assert lease is not None and lease.expected_container_id == fixtures.CONTAINER_ID
    with migrated() as session:
        assert session.scalar(text("PRAGMA integrity_check")) == "ok"
        assert not session.execute(text("PRAGMA foreign_key_check")).all()
        assert (
            session.scalar(text("SELECT version_num FROM alembic_version"))
            == expected_head
        )
    engine.dispose()
