"""Native enablement narrows existing authority to the originally selected scope."""

from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import Table, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_enablements import (
    DevelopmentSourceEnablementConflictError,
    DevelopmentSourceEnablementOperationCoordinator,
)
from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.domain import (
    AgentWorkspaceState,
    DevelopmentSourceEnablementOperationState,
    ManagedInstanceMutationKind,
)
from kodi_lab.kodi.addons import KodiAddonEnablementAmbiguousError
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    DevelopmentSourceEnablementOperationRow,
    DevelopmentSourceRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    ManagedProvisionReservationRow,
    ManagedStartReservationRow,
    RuntimeRevisionRow,
    SessionSourceCopyAdmissionRow,
)
from tests.application import test_development_source_enablements as fixtures
from tests.application.test_development_source_enablements import factory as factory
from tests.storage.test_migrations import migration_templates as migration_templates


def bind(factory: sessionmaker[Session], *, defect: str = "") -> None:
    with factory.begin() as session:
        workspace_id = str(uuid4())
        session.add(
            AgentWorkspaceRow(
                id=workspace_id,
                development_source_id=str(fixtures.SOURCE_ID),
                source_addon_id=fixtures.ADDON_ID,
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
        source_id = str(fixtures.SOURCE_ID)
        if defect == "source":
            original_source = session.get(DevelopmentSourceRow, source_id)
            assert original_source is not None
            source_values = {
                column.name: getattr(original_source, column.name)
                for column in original_source.__table__.columns
            }
            source_id = str(uuid4())
            source_values.update(
                id=source_id,
                logical_name="Other source",
                relative_locator="other-source",
                addon_id="plugin.video.other",
            )
            session.add(DevelopmentSourceRow(**source_values))
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
        if defect in {"start", "provision"}:
            reservation: Any
            values = dict(
                managed_instance_id=str(fixtures.INSTANCE_ID),
                runtime_revision_id=str(fixtures.REVISION_ID),
                operation_id=str(uuid4()),
                phase="uncertain",
                created_at=fixtures.NOW.isoformat(),
            )
            if defect == "start":
                reservation = ManagedStartReservationRow(
                    **values, container_id=fixtures.CONTAINER_ID
                )
            else:
                reservation = ManagedProvisionReservationRow(
                    **values, session_generation=1
                )
            session.add(reservation)


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


@pytest.mark.parametrize(
    "defect", ["target", "source", "runtime", "phase", "start", "provision"]
)
def test_wrong_metadata_scope_refuses_before_history_lease_or_native_calls(
    factory: sessionmaker[Session],
    tmp_path: Path,
    defect: str,
) -> None:
    bind(factory, defect=defect)
    broker = fixtures.FakeBroker()
    adapter = fixtures.FakeEnableAdapter()
    service = fixtures._service(
        factory, tmp_path, broker, [fixtures._target()], adapter
    )
    with pytest.raises(DevelopmentSessionConflict):
        service.enable(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    assert not adapter.enable_calls and broker.verify_calls == 0
    with factory() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(
                    DevelopmentSourceEnablementOperationRow
                )
            )
            == 0
        )
        assert session.scalar(select(ManagedInstanceMutationLeaseRow)) is None
        assert session.scalar(select(GlobalHeavyOperationLeaseRow)) is None


def test_current_selected_scope_uses_all_existing_preconditions_and_native_enable(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    bind(factory)
    broker = fixtures.FakeBroker()
    adapter = fixtures.FakeEnableAdapter()
    result = fixtures._service(
        factory, tmp_path, broker, [fixtures._target()], adapter
    ).enable(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    assert result.state is DevelopmentSourceEnablementOperationState.SUCCEEDED
    assert adapter.enable_calls == [fixtures.ADDON_ID] and broker.verify_calls == 1


def test_observed_profile_must_match_the_claimed_selected_profile(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    bind(factory, defect="container")
    broker = fixtures.FakeBroker()
    adapter = fixtures.FakeEnableAdapter()
    result = fixtures._service(
        factory, tmp_path, broker, [fixtures._target()], adapter
    ).enable(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    assert result.state is DevelopmentSourceEnablementOperationState.FAILED
    assert result.failure_reason == "development_session_identity_changed"
    assert not adapter.enable_calls and broker.verify_calls == 0


@pytest.mark.parametrize("boundary", ["runtime", "preflight", "mutation"])
@pytest.mark.parametrize("kind", ["phase", "container", "successor"])
def test_scope_change_before_native_dispatch_cannot_borrow_the_successor(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    kind: str,
) -> None:
    bind(factory)
    broker = fixtures.FakeBroker()
    adapter = fixtures.FakeEnableAdapter()
    service = fixtures._service(
        factory, tmp_path, broker, [fixtures._target()], adapter
    )
    if boundary == "runtime":
        observe = broker.observe_managed_kodi_runtime

        def observe_then_change(**kwargs: Any) -> ObserveManagedKodiRuntimeResult:
            result = observe(**kwargs)
            change(factory, kind)
            return result

        monkeypatch.setattr(broker, "observe_managed_kodi_runtime", observe_then_change)
    else:
        method = (
            "set_preflight_state"
            if boundary == "preflight"
            else "mark_mutation_started"
        )
        original = getattr(service._coordinator, method)

        def changed(*args: Any, **kwargs: Any) -> Any:
            change(factory, kind)
            return original(*args, **kwargs)

        monkeypatch.setattr(service._coordinator, method, changed)
    result = service.enable(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    assert result.state is DevelopmentSourceEnablementOperationState.FAILED
    assert result.failure_reason in {
        "development_session_identity_changed",
        "development_session_requires_review",
    }
    assert not result.enable_performed and not result.reconciliation_required
    assert not adapter.enable_calls
    with factory() as session:
        assert session.scalar(select(ManagedInstanceMutationLeaseRow)) is None
        if kind == "successor":
            policy = session.get(DevelopmentSessionPolicyRow, 1)
            assert policy is not None and policy.active_generation == 2


def test_already_enabled_result_still_requires_the_original_selected_scope(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bind(factory)
    broker = fixtures.FakeBroker()
    adapter = fixtures.FakeEnableAdapter()
    adapter.enabled = True
    service = fixtures._service(
        factory, tmp_path, broker, [fixtures._target(enabled=True)], adapter
    )
    original = service._coordinator.set_preflight_state

    def changed(*args: Any, **kwargs: Any) -> Any:
        change(factory, "successor")
        return original(*args, **kwargs)

    monkeypatch.setattr(service._coordinator, "set_preflight_state", changed)
    result = service.enable(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    assert result.state is DevelopmentSourceEnablementOperationState.FAILED
    assert result.result_enabled is None and not adapter.enable_calls


def test_ambiguous_native_dispatch_retains_existing_reconciliation_semantics(
    factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    bind(factory)
    broker = fixtures.FakeBroker()
    adapter = fixtures.FakeEnableAdapter()
    adapter.enable_error = KodiAddonEnablementAmbiguousError(
        "fixture ambiguous response"
    )
    result = fixtures._service(
        factory, tmp_path, broker, [fixtures._target()], adapter
    ).enable(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    assert (
        result.state
        is DevelopmentSourceEnablementOperationState.RECONCILIATION_REQUIRED
    )
    assert result.reconciliation_required and adapter.enable_calls == [
        fixtures.ADDON_ID
    ]


def test_metadata_only_admission_cannot_supply_a_guided_copy_dispatch_proof(
    factory: sessionmaker[Session],
) -> None:
    from kodi_lab.storage.development_sessions import require_source_session_admission

    bind(factory)
    from kodi_lab.application.session_source_copy import SOURCE_COPY_NAMESPACE

    request_id = uuid4()
    operation_id = str(uuid5(SOURCE_COPY_NAMESPACE, str(request_id)))
    with factory.begin() as session:
        current = session.get(DevelopmentSessionRow, 1)
        assert current is not None
        session.add(
            SessionSourceCopyAdmissionRow(
                request_id=str(request_id),
                operation_id=operation_id,
                session_generation=1,
                target_id=current.target_id,
                runtime_revision_id=current.runtime_revision_id,
                workspace_id=current.workspace_id,
                source_id=current.source_id,
                container_id=fixtures.CONTAINER_ID,
                requested_at=fixtures.NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            )
        )
    with (
        factory() as session,
        pytest.raises(DevelopmentSessionConflict, match="selection_changed"),
    ):
        require_source_session_admission(
            session,
            target_id=str(fixtures.INSTANCE_ID),
            revision_id=str(fixtures.REVISION_ID),
            source_id=str(fixtures.SOURCE_ID),
            container_id=None,
            operation_id=operation_id,
        )


@pytest.mark.parametrize("boundary", ["preflight", "mutation"])
@pytest.mark.parametrize("defect", ["expired", "foreign", "wrong_kind"])
def test_marker_cannot_outlive_or_borrow_another_typed_lease(
    factory: sessionmaker[Session],
    boundary: str,
    defect: str,
) -> None:
    bind(factory)
    clock = fixtures.Clock()
    coordinator = DevelopmentSourceEnablementOperationCoordinator(factory, clock=clock)
    handle = coordinator.begin(
        fixtures.INSTANCE_ID,
        fixtures.SOURCE_ID,
        source_logical_name="Example",
        addon_id=fixtures.ADDON_ID,
        addon_version=fixtures.VERSION,
        runtime_revision_id=fixtures.REVISION_ID,
    )
    with factory.begin() as session:
        lease = session.get(ManagedInstanceMutationLeaseRow, str(fixtures.INSTANCE_ID))
        assert lease is not None
        if defect == "foreign":
            lease.operation_id = str(uuid4())
        elif defect == "wrong_kind":
            lease.operation_kind = ManagedInstanceMutationKind.LIFECYCLE
    if defect == "expired":
        clock.now += timedelta(minutes=11)
    with pytest.raises(
        DevelopmentSourceEnablementConflictError, match="owned and current"
    ):
        if boundary == "preflight":
            coordinator.set_preflight_state(handle, prior_enabled=False)
        else:
            coordinator.mark_mutation_started(handle)
    with factory() as session:
        operation = session.get(
            DevelopmentSourceEnablementOperationRow, str(handle.operation_id)
        )
        lease = session.get(ManagedInstanceMutationLeaseRow, str(fixtures.INSTANCE_ID))
        assert (
            operation is not None
            and operation.state is DevelopmentSourceEnablementOperationState.RUNNING
        )
        assert not operation.enable_performed and operation.prior_enabled is None
        assert lease is not None
        if defect == "foreign":
            assert lease.operation_id != str(handle.operation_id)
        if defect == "wrong_kind":
            assert lease.operation_kind is ManagedInstanceMutationKind.LIFECYCLE


def test_selected_enablement_and_owned_markers_run_on_actual_migrated_head(
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
    expected_head = ScriptDirectory.from_config(config).get_current_head()
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
    # Copy data only; do not create ORM tables over the actual migrated schema.
    with factory() as source, migrated.begin() as destination:
        for model in models:
            table = cast(Table, model.__table__)
            for values in source.execute(select(table)).mappings():
                destination.execute(table.insert().values(**dict(values)))
    broker = fixtures.FakeBroker()
    adapter = fixtures.FakeEnableAdapter()
    result = fixtures._service(
        migrated, tmp_path, broker, [fixtures._target()], adapter
    ).enable(fixtures.INSTANCE_ID, fixtures.SOURCE_ID)
    assert result.state is DevelopmentSourceEnablementOperationState.SUCCEEDED
    assert adapter.enable_calls == [fixtures.ADDON_ID] and broker.verify_calls == 1
    with migrated() as session:
        assert (
            session.scalar(text("SELECT version_num FROM alembic_version"))
            == expected_head
        )
        assert session.scalar(text("PRAGMA integrity_check")) == "ok"
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        assert session.scalar(select(ManagedInstanceMutationLeaseRow)) is None
    engine.dispose()
