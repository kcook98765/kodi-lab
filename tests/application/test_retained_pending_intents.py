"""A refused pre-dispatch intent must not become permanent session ownership."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.managed_agent_lifecycle import (
    ManagedAgentLifecycleError,
    ManagedAgentLifecycleService,
)
from kodi_lab.application.managed_resources import ManagedResourceService
from kodi_lab.broker.docker_inventory import ProvisionedManagedContainerRecord
from kodi_lab.broker.operations import ProvisionManagedContainerResult
from kodi_lab.domain import ManagedInstanceLifecycleKind, ManagedInstanceSemanticState
from kodi_lab.storage.models import (
    ManagedAgentLifecycleRequestRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceUseRow,
    ManagedProvisionReservationRow,
    ManagedStartReservationRow,
)
from tests.application.test_retained_bridge_lifecycle import setup


@pytest.mark.parametrize("conflict", ["start", "provision", "container", "image"])
def test_post_claim_refusal_is_terminal_without_clearing_conflicting_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    s = setup(tmp_path, monkeypatch)
    resources = ManagedResourceService(s.env.sessions, s.broker, s.lifecycle)
    agent = ManagedAgentLifecycleService(
        s.env.sessions,
        resources,
        s.lifecycle,
        s.status,
        lock_root=tmp_path / "agent-locks",
    )
    claimed, proceed = Event(), Event()
    original_claim = agent._claim
    request_id = uuid4()

    def pause(*args: Any, **kwargs: Any) -> Any:
        value = original_claim(*args, **kwargs)
        claimed.set()
        assert proceed.wait(5)
        return value

    monkeypatch.setattr(agent, "_claim", pause)
    web_entered, web_finish = Event(), Event()
    s.broker.start_entered, s.broker.start_finish = web_entered, web_finish

    def provision(**kwargs: Any) -> ProvisionManagedContainerResult:
        r = s.broker.record(kwargs["managed_instance_id"])

        def dispatch() -> ProvisionedManagedContainerRecord:
            web_entered.set()
            assert web_finish.wait(5)
            return ProvisionedManagedContainerRecord(
                r.managed_instance_id,
                str(r.runtime_revision_id),
                r.container_id,
                r.image_id,
                r.runtime_state,
                False,
            )

        out = s.admission.run_provision(
            instance_id=r.managed_instance_id,
            revision_id=str(r.runtime_revision_id),
            dispatch=dispatch,
        )
        return ProvisionManagedContainerResult(
            **{k: getattr(out, k) for k in ProvisionManagedContainerResult.model_fields}
        )

    monkeypatch.setattr(
        s.broker, "provision_managed_container", provision, raising=False
    )
    before_record = s.records[2]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ensured = pool.submit(
                agent.ensure_ready,
                client_id=s.env.client_id,
                request_id=request_id,
                target_id=s.ids["omega"],
                authorize=lambda _: None,
            )
            web = None
            try:
                assert claimed.wait(5)
                if conflict == "start":
                    web = pool.submit(s.lifecycle.start, s.ids["omega"])
                elif conflict == "provision":
                    web = pool.submit(s.lifecycle.provision, s.ids["nexus"])
                else:
                    s.records[2] = (
                        replace(before_record, container_id="f" * 64)
                        if conflict == "container"
                        else replace(before_record, image_id="sha256:" + "f" * 64)
                    )
                if web is not None:
                    assert web_entered.wait(5)
                    model = (
                        ManagedStartReservationRow
                        if conflict == "start"
                        else ManagedProvisionReservationRow
                    )
                    target = s.ids["omega"] if conflict == "start" else s.ids["nexus"]
                    with s.env.sessions() as session:
                        reservation_id = session.get(model, str(target)).operation_id
                proceed.set()
                with pytest.raises(ManagedAgentLifecycleError):
                    ensured.result(timeout=5)
                with s.env.sessions() as session:
                    intent = session.scalar(select(ManagedAgentLifecycleRequestRow))
                    assert intent is not None and intent.state == "failed"
                    assert intent.lifecycle_operation_id is None
                    assert (
                        session.get(ManagedInstanceLifecycleOperationRow, intent.id)
                        is None
                    )
                    assert (
                        session.get(
                            ManagedInstanceUseRow,
                            (str(s.ids["omega"]), "mcp:" + str(s.env.client_id)),
                        )
                        is None
                    )
                    if web is not None:
                        assert (
                            session.get(model, str(target)).operation_id
                            == reservation_id
                        )
            finally:
                proceed.set()
                web_finish.set()
            if web is not None:
                web.result(timeout=5)
        if conflict == "start":
            with s.readiness._lock:
                future = s.readiness._entries[s.ids["omega"]].future
            assert future is not None
            future.result(timeout=5)
            s.lifecycle.stop(s.ids["omega"])
        else:
            s.records[2] = before_record
        starts = s.broker.starts
        replay = agent.ensure_ready(
            client_id=s.env.client_id,
            request_id=request_id,
            target_id=s.ids["omega"],
            authorize=lambda _: None,
        )
        assert replay.replayed and replay.receipt_state == "failed" and not replay.ready
        assert s.broker.starts == starts
        s.choose("nexus")  # The refused request no longer owns the session forever.
        assert s.service.status().target_id == s.ids["nexus"] and len(s.records) == 4
    finally:
        proceed.set()
        web_finish.set()
        s.readiness.close()


@pytest.mark.parametrize("state", ["pending", "dispatched", "ambiguous"])
def test_refusal_cleanup_never_settles_a_committed_child_or_ambiguous_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    s = setup(tmp_path, monkeypatch)
    agent = ManagedAgentLifecycleService(
        s.env.sessions,
        ManagedResourceService(s.env.sessions, s.broker, s.lifecycle),
        s.lifecycle,
        s.status,
        lock_root=tmp_path / "agent-locks",
    )
    row, _ = agent._claim(
        s.env.client_id, uuid4(), s.ids["omega"], "ensure_ready", 15, lambda _: None
    )

    def claimed(session: Any, handle: Any) -> None:
        if state != "pending":
            value = session.get(ManagedAgentLifecycleRequestRow, row.id)
            value.state, value.lifecycle_operation_id = state, str(handle.operation_id)

    handle = s.lifecycle._operations.begin(
        s.ids["omega"],
        instance_name="Committed child fixture",
        kind=ManagedInstanceLifecycleKind.START,
        prior_state=ManagedInstanceSemanticState.STOPPED,
        operation_id=UUID(row.id),
        on_claim=claimed,
    )
    try:
        agent._refuse_undispatched(row, "development_session_start_pending")
        with s.env.sessions() as session:
            assert session.get(ManagedAgentLifecycleRequestRow, row.id).state == state
            assert (
                session.get(ManagedInstanceLifecycleOperationRow, row.id).state
                == "running"
            )
            assert session.get(
                ManagedInstanceMutationLeaseRow, str(s.ids["omega"])
            ).operation_id == str(handle.operation_id)
    finally:
        s.readiness.close()
