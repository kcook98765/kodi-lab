"""One durable session serializes with every broker provision/start reservation."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.agent_workspace_cleanup import (
    AgentWorkspaceCleanupError,
    AgentWorkspaceCleanupService,
)
from kodi_lab.application.development_sessions import (
    DevelopmentSessionRequest,
    DevelopmentSessionService,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    ManagedContainerRecord,
    ProvisionedManagedContainerRecord,
    StartManagedContainerRecord,
)
from kodi_lab.broker.managed_capacity import (
    ManagedCapacityAdmission,
    ManagedCapacityError,
)
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
)
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    DevelopmentSessionRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedProvisionReservationRow,
)
from kodi_lab.storage.repositories import TargetRepository
from tests.application import test_mcp_workspace_development as fixture
from tests.storage.test_repositories import make_target


def environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_path: Path | None = None,
) -> Any:
    env = fixture._environment(tmp_path, database_path=database_path)
    records: list[ManagedContainerRecord] = []

    def inventory() -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    managed_instance_id=item.managed_instance_id,
                    container_id=item.container_id,
                    image_id=item.image_id,
                    runtime_state=item.runtime_state,
                    health_state=item.health_state,
                )
                for item in records
            ]
        )

    service = DevelopmentSessionService(
        env.sessions, inventory, data_root=tmp_path / "data"
    )
    # These tests exercise reservation/admission rather than runtime acquisition.
    monkeypatch.setattr(service, "_runtime", lambda *_args: None)
    admission = ManagedCapacityAdmission(
        env.sessions, lambda: tuple(records), clock=lambda: fixture.NOW
    )
    request = DevelopmentSessionRequest(
        request_id=uuid4(),
        expected_generation=0,
        target_id=fixture.TARGET_ID,
        workspace_id=env.workspace_id,
    )

    def dispatch() -> ProvisionedManagedContainerRecord:
        record = ManagedContainerRecord(
            str(fixture.TARGET_ID),
            fixture.CONTAINER_ID,
            fixture.IMAGE_ID,
            ContainerRuntimeState.STOPPED,
            ContainerHealthState.NOT_CONFIGURED,
            str(fixture.RUNTIME_REVISION_ID),
        )
        records[:] = [record]
        return ProvisionedManagedContainerRecord(
            record.managed_instance_id,
            str(record.runtime_revision_id),
            record.container_id,
            record.image_id,
            record.runtime_state,
            True,
        )

    def provision(callback: Any = dispatch) -> Any:
        return admission.run_provision(
            instance_id=str(fixture.TARGET_ID),
            revision_id=str(fixture.RUNTIME_REVISION_ID),
            dispatch=callback,
        )

    return SimpleNamespace(
        env=env,
        records=records,
        service=service,
        admission=admission,
        request=request,
        dispatch=dispatch,
        provision=provision,
        inventory=inventory,
    )


def test_reservation_replay_retains_one_workspace_and_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = environment(tmp_path, monkeypatch)
    reserved = state.service.reserve(state.request)
    assert reserved.phase == "reserved" and reserved.generation == 1
    assert state.service.reserve(state.request) == reserved
    with pytest.raises(DevelopmentSessionConflict, match="request_conflict"):
        state.service.reserve(state.request.model_copy(update={"target_id": uuid4()}))
    with pytest.raises(DevelopmentSessionConflict, match="already_active"):
        state.service.reserve(state.request.model_copy(update={"request_id": uuid4()}))
    assert state.env.workspaces.get(state.env.workspace_id).generation == 0
    closed = state.service.close_absent(1)
    assert closed.phase == "closed" and state.service.status() is None
    assert state.service.reserve(state.request).phase == "closed"
    second = state.service.reserve(
        state.request.model_copy(
            update={"request_id": uuid4(), "expected_generation": 1}
        )
    )
    assert second.generation == 2
    with state.env.sessions() as session:
        assert len(session.scalars(select(DevelopmentSessionRow)).all()) == 2
    with pytest.raises(
        AgentWorkspaceCleanupError, match="workspace_session_history_retained"
    ):
        AgentWorkspaceCleanupService(
            state.env.sessions, data_root=tmp_path / "data"
        ).queue(state.env.workspace_id)


@pytest.mark.parametrize(
    "state_value",
    [
        ContainerRuntimeState.STOPPED,
        ContainerRuntimeState.RUNNING,
        ContainerRuntimeState.UNKNOWN,
    ],
)
def test_existing_resource_is_never_enrolled_or_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state_value: ContainerRuntimeState
) -> None:
    state = environment(tmp_path, monkeypatch)
    state.dispatch()
    state.records[:] = [replace(state.records[0], runtime_state=state_value)]
    before = list(state.records)
    with pytest.raises(DevelopmentSessionConflict, match="existing_resource"):
        state.service.reserve(state.request)
    assert state.records == before and state.service.status() is None


def test_broker_provision_binds_exact_container_before_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = environment(tmp_path, monkeypatch)
    state.service.reserve(state.request)
    result = state.provision()
    bound = state.service.status()
    assert bound.phase == "bound" and bound.container_id == result.container_id
    with pytest.raises(DevelopmentSessionConflict, match="existing_resource"):
        state.service.close_absent(1)
    # Even a stopped unrelated container blocks all session starts/provisioning.
    state.records.append(
        replace(
            state.records[0], managed_instance_id=str(uuid4()), container_id="f" * 64
        )
    )
    with pytest.raises(ManagedCapacityError, match="identity_changed"):
        state.provision()
    assert len(state.records) == 2


def test_all_starts_use_session_identity_while_stop_preserves_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = environment(tmp_path, monkeypatch)
    state.service.reserve(state.request)
    state.provision()
    from datetime import timedelta

    from kodi_lab.storage.managed_activity import timestamp

    with state.env.sessions.begin() as session:
        session.add(
            ManagedInstanceMutationLeaseRow(
                managed_instance_id=str(fixture.TARGET_ID),
                operation_kind="lifecycle",
                operation_id=str(uuid4()),
                lease_token_digest="a" * 64,
                acquired_at=fixture.NOW_TEXT,
                heartbeat_at=fixture.NOW_TEXT,
                lease_expires_at=timestamp(fixture.NOW + timedelta(minutes=15)),
            )
        )
    calls = []

    def start() -> StartManagedContainerRecord:
        calls.append("start")
        state.records[0] = replace(
            state.records[0], runtime_state=ContainerRuntimeState.RUNNING
        )
        return StartManagedContainerRecord(
            str(fixture.TARGET_ID),
            fixture.CONTAINER_ID,
            ContainerRuntimeState.RUNNING,
            True,
        )

    state.admission.run_start(
        instance_id=str(fixture.TARGET_ID),
        container_id=fixture.CONTAINER_ID,
        revision_id=str(fixture.RUNTIME_REVISION_ID),
        dispatch=start,
    )
    state.records[0] = replace(
        state.records[0], runtime_state=ContainerRuntimeState.STOPPED
    )
    state.admission.confirmed_stop(
        instance_id=str(fixture.TARGET_ID),
        container_id=fixture.CONTAINER_ID,
        revision_id=str(fixture.RUNTIME_REVISION_ID),
    )
    assert state.service.status().container_id == fixture.CONTAINER_ID
    assert state.service.status().generation == 1
    assert calls == ["start"]
    with pytest.raises(
        ManagedCapacityError, match="managed_start_identity_unavailable"
    ):
        state.admission.run_start(
            instance_id=str(fixture.TARGET_ID),
            container_id="e" * 64,
            revision_id=str(fixture.RUNTIME_REVISION_ID),
            dispatch=start,
        )
    assert calls == ["start"]


def test_uncertain_provision_survives_restart_and_is_not_freed_by_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = environment(tmp_path, monkeypatch)
    state.service.reserve(state.request)

    def lost() -> Any:
        raise RuntimeError("lost Docker response")

    with pytest.raises(RuntimeError, match="lost Docker"):
        state.provision(lost)
    restarted = DevelopmentSessionService(
        state.env.sessions, state.inventory, data_root=tmp_path / "data"
    )
    resumed = restarted.status()
    assert resumed is not None and resumed.phase == "uncertain"
    admission = ManagedCapacityAdmission(
        state.env.sessions, lambda: tuple(state.records)
    )
    with pytest.raises(ManagedCapacityError, match="requires_reconciliation"):
        admission.run_provision(
            instance_id=str(fixture.TARGET_ID),
            revision_id=str(fixture.RUNTIME_REVISION_ID),
            dispatch=state.dispatch,
        )
    with state.env.sessions() as session:
        retained = session.get(ManagedProvisionReservationRow, str(fixture.TARGET_ID))
        assert retained is not None and retained.phase == "uncertain"
        operation_id = UUID(retained.operation_id)
    state.dispatch()
    with pytest.raises(DevelopmentSessionConflict, match="requires_review"):
        restarted.close_absent(1)
    state.records.clear()
    # Absence cannot prove that an accepted daemon create will not finish late.
    with pytest.raises(DevelopmentSessionConflict, match="requires_review"):
        restarted.close_absent(1)
    with state.env.sessions() as session:
        retained = session.get(ManagedProvisionReservationRow, str(fixture.TARGET_ID))
        assert retained is not None and retained.operation_id == str(operation_id)
    with pytest.raises(ManagedCapacityError, match="requires_reconciliation"):
        state.provision()


def test_missing_container_does_not_silently_allow_an_advanced_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = environment(tmp_path, monkeypatch)
    state.service.reserve(state.request)
    state.provision()
    state.records.clear()
    with pytest.raises(ManagedCapacityError, match="identity_changed"):
        state.provision()
    state.service.close_absent(1)
    assert state.service.status() is None


def test_two_prepare_reservations_have_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = environment(tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                state.service.reserve,
                state.request.model_copy(update={"request_id": uuid4()}),
            )
            for _ in range(2)
        ]
        results = []
        for future in futures:
            try:
                results.append(future.result(timeout=10).generation)
            except DevelopmentSessionConflict:
                results.append("blocked")
    assert sorted(map(str, results)) == ["1", "blocked"]


def test_inflight_advanced_provision_is_not_an_empty_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = environment(tmp_path, monkeypatch)
    entered, proceed = Event(), Event()

    def paused() -> Any:
        entered.set()
        assert proceed.wait(10)
        return state.dispatch()

    with ThreadPoolExecutor(max_workers=1) as pool:
        provisioned = pool.submit(state.provision, paused)
        assert entered.wait(10)
        with pytest.raises(DevelopmentSessionConflict, match="busy"):
            state.service.reserve(state.request)
        proceed.set()
        assert provisioned.result(timeout=10).created
    assert state.service.status() is None


def test_unrelated_advanced_provision_denied_after_session_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = environment(tmp_path, monkeypatch)
    other = make_target("Unrelated preserved target")
    with state.env.sessions.begin() as session:
        TargetRepository(session).create(other)
        session.add(
            ManagedInstanceRow(
                target_id=str(other.id),
                instance_type="disposable",
                runtime_revision_id=str(fixture.RUNTIME_REVISION_ID),
                runtime_state="stopped",
                start_on_host_boot=False,
            )
        )
    state.service.reserve(state.request)
    with pytest.raises(ManagedCapacityError, match="other_target"):
        state.admission.run_provision(
            instance_id=str(other.id),
            revision_id=str(fixture.RUNTIME_REVISION_ID),
            dispatch=state.dispatch,
        )
    assert state.records == []


def test_runtime_and_canonical_identity_validation_are_not_bypassed_in_production(
    tmp_path: Path,
) -> None:
    env = fixture._environment(tmp_path)
    service = DevelopmentSessionService(
        env.sessions,
        lambda: ManagedContainerInventoryResult(containers=[]),
        data_root=tmp_path / "data",
    )
    with pytest.raises(DevelopmentSessionConflict, match="noncanonical_target"):
        service.reserve(
            DevelopmentSessionRequest(
                request_id=uuid4(),
                expected_generation=0,
                target_id=fixture.TARGET_ID,
                workspace_id=env.workspace_id,
            )
        )


def test_container_created_outside_reserved_dispatch_cannot_be_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = environment(tmp_path, monkeypatch)
    state.service.reserve(state.request)

    def unowned_retry() -> ProvisionedManagedContainerRecord:
        return replace(
            cast(ProvisionedManagedContainerRecord, state.dispatch()), created=False
        )

    with pytest.raises(ManagedCapacityError, match="result_unverified"):
        state.provision(unowned_retry)
    assert state.service.status().phase == "uncertain"
    assert len(state.records) == 1


def test_definite_image_preflight_failure_does_not_leave_an_uncertain_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.broker.errors import RuntimeImageUnavailableError

    state = environment(tmp_path, monkeypatch)
    state.service.reserve(state.request)

    def no_image() -> Any:
        raise RuntimeImageUnavailableError("cached image disappeared before create")

    with pytest.raises(RuntimeImageUnavailableError):
        state.provision(no_image)
    assert state.service.status().phase == "reserved"
    with state.env.sessions() as session:
        assert (
            session.get(ManagedProvisionReservationRow, str(fixture.TARGET_ID)) is None
        )
    assert state.provision().created
