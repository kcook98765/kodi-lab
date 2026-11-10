"""Retained selection policy is separate from ordinary Start authority."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.managed_capacity import ManagedCapacityError
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import (
    DevelopmentSessionRow,
    ManagedInstanceMutationLeaseRow,
    ManagedStartAuthorizationRow,
)
from tests.application.test_managed_start import Broker, NativeReady
from tests.application.test_retained_development import environment


def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    s = environment(tmp_path, monkeypatch)
    # Policy-only synthetic catalog. Real sealed runtime admission is exercised
    # independently by test_managed_start and test_managed_start_authority.
    monkeypatch.setattr(
        "kodi_lab.application.managed_runtime_admission.require_managed_runtime",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        "kodi_lab.application.runtime_lifecycle.require_managed_runtime",
        lambda *_a, **_kw: None,
    )
    broker = Broker(s.env.sessions, s.records[0])
    broker.records = s.records
    readiness = ReadinessTaskRegistry()
    status = ManagedRuntimeStatusService(s.env.sessions, broker, readiness)
    lifecycle = ManagedRuntimeLifecycleService(
        s.env.sessions,
        broker,
        readiness,
        status,
        coordinator_factory=lambda *_: NativeReady(),
    )
    return SimpleNamespace(s=s, broker=broker, readiness=readiness, lifecycle=lifecycle)


@pytest.mark.parametrize(
    "selection",
    [
        "none",
        "same",
        "other",
        "same_incomplete",
        "other_incomplete",
        "same_mismatch",
        "other_mismatch",
    ],
)
def test_selection_context_never_changes_implicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    e = setup(tmp_path, monkeypatch)
    s = e.s
    target = s.ids["omega"]
    try:
        if selection != "none":
            chosen = "nexus" if selection.startswith("other") else "omega"
            selected, _ = s.choose(chosen)
            with s.env.sessions.begin() as session:
                row = session.get(DevelopmentSessionRow, selected.generation)
                if selection.endswith("incomplete"):
                    row.phase = "reserved"
                elif selection.endswith("mismatch"):
                    row.container_id = "f" * 64
        with s.env.sessions() as session:
            before = session.execute(select(DevelopmentSessionRow.__table__)).all()
        profiles = {tid: path.read_bytes() for tid, path in s.profiles.items()}
        if "incomplete" in selection or "mismatch" in selection:
            with pytest.raises(DevelopmentSessionConflict):
                e.lifecycle.start_managed(target)
            assert e.broker.starts == 0
        else:
            e.lifecycle.start_managed(target)
            assert e.broker.starts == 1
        with s.env.sessions() as session:
            assert (
                session.execute(select(DevelopmentSessionRow.__table__)).all() == before
            )
        assert {tid: path.read_bytes() for tid, path in s.profiles.items()} == profiles
    finally:
        e.readiness.close()


@pytest.mark.parametrize(
    "state",
    [
        ContainerRuntimeState.RUNNING,
        ContainerRuntimeState.UNKNOWN,
        ContainerRuntimeState.TRANSITIONAL,
    ],
)
def test_other_kodi_must_be_positively_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: ContainerRuntimeState
) -> None:
    e = setup(tmp_path, monkeypatch)
    try:
        e.s.choose("nexus")
        r = next(
            r for r in e.s.records if r.managed_instance_id == str(e.s.ids["nexus"])
        )
        e.s.records[e.s.records.index(r)] = replace(r, runtime_state=state)
        with pytest.raises(DevelopmentSessionConflict, match="other_runtime_active"):
            e.lifecycle.start_managed(e.s.ids["omega"])
        assert e.broker.starts == 0 and e.broker.stops == 0
    finally:
        e.readiness.close()


def test_selection_cannot_race_past_start_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = setup(tmp_path, monkeypatch)
    s = e.s
    e.broker.entered, e.broker.finish = Event(), Event()
    try:
        preview = s.service.selection_preview(s.ids["nexus"], s.env.workspace_id)
        from kodi_lab.application.development_sessions import RetainedSelectionRequest

        request = RetainedSelectionRequest(
            request_id=uuid4(),
            expected_generation=preview.latest_generation,
            target_id=s.ids["nexus"],
            workspace_id=s.env.workspace_id,
            identity=preview.identity,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(e.lifecycle.start_managed, s.ids["omega"])
            assert e.broker.entered.wait(5)
            with pytest.raises(DevelopmentSessionConflict, match="busy"):
                s.service.select_retained(request)
            e.broker.finish.set()
            first.result(timeout=5)
        assert s.service.status() is None
    finally:
        e.broker.finish.set()
        e.readiness.close()


def test_missing_container_never_provisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = setup(tmp_path, monkeypatch)
    try:
        e.broker.records[:] = [
            r
            for r in e.broker.records
            if r.managed_instance_id != str(e.s.ids["omega"])
        ]
        with pytest.raises(DevelopmentSessionConflict, match="identity_changed"):
            e.lifecycle.start_managed(e.s.ids["omega"])
        assert e.broker.starts == 0
        with e.s.env.sessions() as session:
            assert session.scalars(select(ManagedStartAuthorizationRow)).all() == []
            assert session.scalars(select(ManagedInstanceMutationLeaseRow)).all() == []
    finally:
        e.readiness.close()


def test_selected_development_request_cannot_borrow_ordinary_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e = setup(tmp_path, monkeypatch)
    try:
        e.s.choose("omega")
        e.broker.pause = True
        from tests.application.test_managed_start import PausedDispatch

        with pytest.raises(PausedDispatch):
            e.lifecycle.start_managed(e.s.ids["omega"])
        e.broker.pause = False
        proof = e.broker.proof
        assert proof is not None
        # Strict selection alone would pass here; possession of the current
        # ordinary lease must still not authorize this generic broker request.
        with pytest.raises(ManagedCapacityError, match="authority_unavailable"):
            e.broker.start_managed_container(
                managed_instance_id=str(e.s.ids["omega"]),
                expected_container_id=next(
                    r.container_id
                    for r in e.s.records
                    if r.managed_instance_id == str(e.s.ids["omega"])
                ),
                expected_runtime_state=ContainerRuntimeState.STOPPED,
            )
        assert e.broker.starts == 0
    finally:
        e.readiness.close()


@pytest.mark.parametrize(
    "blocker", ["lease", "reservation", "agent_intent", "reconciliation"]
)
def test_existing_resource_and_recovery_holds_are_never_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    from datetime import UTC, datetime

    from kodi_lab.domain import (
        ManagedInstanceLifecycleKind,
        ManagedInstanceLifecycleOperationState,
        ManagedInstanceSemanticState,
    )
    from kodi_lab.storage.managed_activity import timestamp
    from kodi_lab.storage.models import (
        ManagedAgentLifecycleRequestRow,
        ManagedInstanceLifecycleOperationRow,
        ManagedStartReservationRow,
    )

    e = setup(tmp_path, monkeypatch)
    s = e.s
    try:
        s.choose("nexus")
        other = str(s.ids["nexus"])
        now = timestamp(datetime.now(UTC))
        if blocker == "lease":
            s.lease(s.ids["nexus"])
        else:
            with s.env.sessions.begin() as session:
                if blocker == "reservation":
                    r = next(r for r in s.records if r.managed_instance_id == other)
                    session.add(
                        ManagedStartReservationRow(
                            managed_instance_id=other,
                            container_id=r.container_id,
                            runtime_revision_id=r.runtime_revision_id,
                            operation_id=str(uuid4()),
                            phase="uncertain",
                            created_at=now,
                        )
                    )
                elif blocker == "agent_intent":
                    session.add(
                        ManagedAgentLifecycleRequestRow(
                            id=str(uuid4()),
                            request_id=str(uuid4()),
                            original_instance_id=other,
                            managed_instance_id=other,
                            action="ensure_ready",
                            hold_minutes=15,
                            state="pending",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    session.add(
                        ManagedInstanceLifecycleOperationRow(
                            id=str(uuid4()),
                            original_managed_instance_id=other,
                            managed_instance_id=other,
                            instance_name="Other Kodi",
                            kind=ManagedInstanceLifecycleKind.START,
                            requested_at=now,
                            started_at=now,
                            completed_at=now,
                            prior_state=ManagedInstanceSemanticState.STOPPED,
                            state=ManagedInstanceLifecycleOperationState.RECONCILIATION_REQUIRED,
                            resulting_state="ambiguous",
                            failure_reason="broker_runtime_dispatch_unconfirmed",
                            reconciliation_required=True,
                        )
                    )
        tracked = (
            ManagedInstanceMutationLeaseRow,
            ManagedStartReservationRow,
            ManagedAgentLifecycleRequestRow,
            ManagedInstanceLifecycleOperationRow,
        )
        with s.env.sessions() as session:
            before = {
                model.__tablename__: session.execute(select(model.__table__)).all()
                for model in tracked
            }
        with pytest.raises(DevelopmentSessionConflict):
            e.lifecycle.start_managed(s.ids["omega"])
        with s.env.sessions() as session:
            assert {
                model.__tablename__: session.execute(select(model.__table__)).all()
                for model in tracked
            } == before
            assert session.scalars(select(ManagedStartAuthorizationRow)).all() == []
        assert e.broker.starts == 0
    finally:
        e.readiness.close()


@pytest.mark.parametrize("hold", [False, True])
def test_another_stopped_profiles_history_or_hold_does_not_reserve_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hold: bool
) -> None:
    from datetime import UTC, datetime, timedelta

    from kodi_lab.storage.managed_activity import timestamp
    from kodi_lab.storage.models import ManagedInstanceUseRow, ManagedResourcePolicyRow

    e = setup(tmp_path, monkeypatch)
    s = e.s
    try:
        s.choose("nexus")
        with s.env.sessions.begin() as session:
            policy = session.get(ManagedResourcePolicyRow, 1)
            assert policy is not None
            policy.idle_minutes = None
            session.add(
                ManagedInstanceUseRow(
                    managed_instance_id=str(s.ids["nexus"]),
                    actor_key=f"mcp:{uuid4()}",
                    last_activity_at=timestamp(datetime.now(UTC)),
                    hold_until=timestamp(datetime.now(UTC) + timedelta(minutes=15))
                    if hold
                    else None,
                )
            )
        query = select(ManagedInstanceUseRow.__table__).where(
            ManagedInstanceUseRow.managed_instance_id == str(s.ids["nexus"])
        )
        with s.env.sessions() as session:
            before = session.execute(query).all()
        e.lifecycle.start_managed(s.ids["omega"])
        assert e.broker.starts == 1 and e.broker.stops == 0
        with s.env.sessions() as session:
            assert session.execute(query).all() == before
    finally:
        e.readiness.close()
