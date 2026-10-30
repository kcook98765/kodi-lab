"""Real SQLite policy/lease boundaries with four adapter-owned profile fixtures.

Kodi, acquisition eligibility and Docker execution are separate contracts: the
adapter changes only modeled container state. Selection never receives a profile
write/delete method. Existing runtime qualification tests cover the catalog gate.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from kodi_lab.application.development_sessions import (
    DevelopmentSessionService,
    RetainedPolicyRequest,
    RetainedSelectionRequest,
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
from kodi_lab.runtime.recipes import load_installation_recipe, recipe_target_id
from kodi_lab.storage.development_sessions import (
    DevelopmentSessionConflict,
    require_workspace_session_admission,
)
from kodi_lab.storage.models import (
    DevelopmentSessionRow,
    DevelopmentSourceDeploymentRow,
    ManagedAgentLifecycleRequestRow,
    ManagedBridgeTrustRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedInstanceUseRow,
    ManagedProvisionReservationRow,
    ManagedRemoteGuardRow,
    ManagedResourcePolicyRow,
    ManagedStartReservationRow,
    NovncSessionRow,
    RuntimeContainerArtifactRow,
    RuntimeDefinitionRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
    TargetRow,
)
from tests.application import test_mcp_workspace_development as fixture


def environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    provisioned: bool = True,
    database_path: Path | None = None,
) -> Any:
    env = fixture._environment(tmp_path, database_path=database_path)
    ids = {
        g: recipe_target_id(load_installation_recipe(g))
        for g in ("matrix", "nexus", "omega", "piers")
    }
    revisions: dict[UUID, UUID] = {}
    records: list[ManagedContainerRecord] = []
    profiles: dict[UUID, Path] = {}
    with env.sessions.begin() as session:
        original_revision = session.get(
            RuntimeRevisionRow, str(fixture.RUNTIME_REVISION_ID)
        )
        original_profile = session.get(
            RuntimeExecutionProfileRow, str(fixture.RUNTIME_REVISION_ID)
        )
        assert original_revision and original_profile
        for i, (g, tid) in enumerate(ids.items(), 1):
            definition, revision = uuid4(), uuid4()
            version = {
                "matrix": "19.4",
                "nexus": "20.5",
                "omega": "21.3",
                "piers": "22.0-rc.1",
            }[g]
            image = "sha256:" + str(i) * 64
            session.add(
                RuntimeDefinitionRow(
                    id=str(definition),
                    slug="retained-" + g,
                    kodi_version=version,
                    support_tier="preview" if g == "piers" else "current",
                )
            )
            session.flush()
            attrs = {
                c.name: getattr(original_revision, c.name)
                for c in RuntimeRevisionRow.__table__.columns
            }
            attrs.update(
                id=str(revision),
                runtime_definition_id=str(definition),
                content_digest=image,
            )
            session.add(RuntimeRevisionRow(**attrs))
            session.add(
                TargetRow(
                    id=str(tid),
                    name=g,
                    target_type="managed",
                    kodi_version=version,
                    availability="unavailable",
                    health="warning",
                    connection_state="disconnected",
                )
            )
            session.flush()
            session.add(
                ManagedInstanceRow(
                    target_id=str(tid),
                    instance_type="disposable",
                    runtime_revision_id=str(revision),
                    runtime_state="stopped",
                    start_on_host_boot=False,
                )
            )
            session.add(
                RuntimeContainerArtifactRow(
                    runtime_revision_id=str(revision), image_id=image
                )
            )
            attrs = {
                c.name: getattr(original_profile, c.name)
                for c in RuntimeExecutionProfileRow.__table__.columns
            }
            attrs.update(
                runtime_revision_id=str(revision),
                expected_kodi_version="22.0" if g == "piers" else version,
                expected_version_tag="rc" if g == "piers" else "stable",
                expected_prerelease_ordinal=1 if g == "piers" else None,
            )
            session.add(RuntimeExecutionProfileRow(**attrs))
            revisions[tid] = revision
            if provisioned:
                records.append(
                    ManagedContainerRecord(
                        str(tid),
                        str(i) * 64,
                        image,
                        ContainerRuntimeState.STOPPED,
                        ContainerHealthState.HEALTHY,
                        str(revision),
                    )
                )
            p = tmp_path / "profiles" / g / "settings.xml"
            p.parent.mkdir(parents=True)
            p.write_bytes((g + " private settings\n").encode())
            profiles[tid] = p
        session.merge(ManagedResourcePolicyRow(id=1, max_running=4, idle_minutes=60))

    def inventory() -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[ManagedContainerInventoryEntry(**asdict(r)) for r in records]
        )

    service = DevelopmentSessionService(
        env.sessions, inventory, data_root=tmp_path / "data"
    )
    monkeypatch.setattr(
        service, "_selection_rate", SimpleNamespace(allow=lambda actor: True)
    )
    monkeypatch.setattr(
        service,
        "_runtime",
        lambda session, tid, rid: (
            (_ for _ in ()).throw(AssertionError("wrong exact pin"))
            if revisions[tid] != rid
            else None
        ),
    )
    service.configure_retained(
        RetainedPolicyRequest(
            enabled=True, expected_generation=0, omega_target_id=ids["omega"]
        )
    )
    admission = ManagedCapacityAdmission(env.sessions, lambda: tuple(records))

    def choose(
        g: str, *, request_id: UUID | None = None, actor: str = "web_admin"
    ) -> Any:
        p = service.selection_preview(ids[g], env.workspace_id)
        request = RetainedSelectionRequest(
            request_id=request_id or uuid4(),
            expected_generation=p.latest_generation,
            target_id=ids[g],
            workspace_id=env.workspace_id,
            identity=p.identity,
        )
        return service.select_retained(request, actor=actor), request

    def lease(tid: UUID) -> None:
        with env.sessions.begin() as session:
            session.add(
                ManagedInstanceMutationLeaseRow(
                    managed_instance_id=str(tid),
                    operation_kind="lifecycle",
                    operation_id=str(uuid4()),
                    lease_token_digest="a" * 64,
                    lease_expires_at=(
                        datetime.now(UTC) + timedelta(minutes=5)
                    ).isoformat(),
                )
            )

    return SimpleNamespace(
        env=env,
        ids=ids,
        revisions=revisions,
        records=records,
        profiles=profiles,
        service=service,
        admission=admission,
        choose=choose,
        lease=lease,
        inventory=inventory,
    )


def test_selected_profile_absence_blocks_reset_reprovision_without_rebinding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Model post-deprovision inventory only; never run reset or remove a profile."""
    s = environment(tmp_path, monkeypatch)
    selected, _request = s.choose("omega")
    before = {k: p.read_bytes() for k, p in s.profiles.items()}
    s.records[:] = [
        r for r in s.records if r.managed_instance_id != str(s.ids["omega"])
    ]

    def dispatch() -> Any:
        pytest.fail("The stale selected container must block before provisioning")

    with pytest.raises(
        ManagedCapacityError, match="development_session_identity_changed"
    ):
        s.admission.run_provision(
            instance_id=str(s.ids["omega"]),
            revision_id=str(s.revisions[s.ids["omega"]]),
            dispatch=dispatch,
        )
    assert s.service.status() == selected
    assert len(s.records) == 3
    assert {k: p.read_bytes() for k, p in s.profiles.items()} == before
    with s.env.sessions() as session:
        assert session.scalar(select(ManagedProvisionReservationRow)) is None


def test_four_stopped_profiles_switch_without_mutating_bytes_or_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = environment(tmp_path, monkeypatch)
    before = {k: p.read_bytes() for k, p in s.profiles.items()}
    with s.env.sessions.begin() as session:
        session.add(
            ManagedBridgeTrustRow(
                target_id=str(s.ids["omega"]),
                runtime_revision_id=str(s.revisions[s.ids["omega"]]),
                container_id=s.records[2].container_id,
                bridge_id=str(uuid4()),
                bridge_port=8765,
                secret_reference=str(uuid4()),
                identity_json="{}",
                state="active",
                generation=2,
                prepared_at=fixture.NOW_TEXT,
                established_at=fixture.NOW_TEXT,
            )
        )
    with s.env.sessions() as session:
        trust = session.execute(select(ManagedBridgeTrustRow.__table__)).all()
        old_baselines = session.execute(
            select(DevelopmentSourceDeploymentRow.__table__)
        ).all()
    seen = []
    for g in ("omega", "nexus", "matrix", "omega"):
        current, request = s.choose(g)
        seen.append(current.generation)
        assert current.phase == "bound" and current.container_id == next(
            r.container_id for r in s.records if r.managed_instance_id == str(s.ids[g])
        )
        assert s.service.select_retained(request) == current
        tid = s.ids[g]
        s.lease(tid)
        index = next(
            i for i, r in enumerate(s.records) if r.managed_instance_id == str(tid)
        )

        def start(
            index: int = index, tid: UUID = tid, current: Any = current
        ) -> StartManagedContainerRecord:
            s.records[index] = replace(
                s.records[index], runtime_state=ContainerRuntimeState.RUNNING
            )
            return StartManagedContainerRecord(
                str(tid), current.container_id, ContainerRuntimeState.RUNNING, True
            )

        s.admission.run_start(
            instance_id=str(tid),
            container_id=current.container_id,
            revision_id=str(current.runtime_revision_id),
            dispatch=start,
        )
        assert (
            sum(r.runtime_state is ContainerRuntimeState.RUNNING for r in s.records)
            == 1
        )
        s.records[index] = replace(
            s.records[index], runtime_state=ContainerRuntimeState.STOPPED
        )
        s.admission.confirmed_stop(
            instance_id=str(tid),
            container_id=current.container_id,
            revision_id=str(current.runtime_revision_id),
        )
        with s.env.sessions.begin() as session:
            session.execute(
                delete(ManagedInstanceMutationLeaseRow).where(
                    ManagedInstanceMutationLeaseRow.managed_instance_id == str(tid)
                )
            )
        assert {k: p.read_bytes() for k, p in s.profiles.items()} == before
        with s.env.sessions() as session:
            assert (
                len(
                    session.scalars(
                        select(DevelopmentSessionRow).where(
                            DevelopmentSessionRow.phase != "closed"
                        )
                    ).all()
                )
                == 1
            )
            assert (
                session.execute(select(DevelopmentSourceDeploymentRow.__table__)).all()
                == old_baselines
            )
            assert (
                session.execute(select(ManagedBridgeTrustRow.__table__)).all() == trust
            )
    assert seen == [1, 2, 3, 4] and len(s.records) == 4
    assert s.service.status().target_id == s.ids["omega"]


def test_no_selection_never_falls_back_to_unrestricted_runtime_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = environment(tmp_path, monkeypatch)
    from kodi_lab.storage.retained_development import require_retained_selection

    with s.env.sessions() as session:
        with pytest.raises(
            DevelopmentSessionConflict, match="retained_selection_required"
        ):
            require_retained_selection(session, str(s.ids["omega"]))
        with pytest.raises(
            DevelopmentSessionConflict, match="retained_selection_required"
        ):
            require_workspace_session_admission(
                session,
                target_id=str(s.ids["omega"]),
                revision_id=str(s.revisions[s.ids["omega"]]),
                workspace_id=str(s.env.workspace_id),
                source_id=str(fixture.SOURCE_ID),
            )


def test_selection_identity_requester_replay_and_generation_are_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = environment(tmp_path, monkeypatch)
    first, request = s.choose("omega")
    second, _ = s.choose("nexus")
    replay = s.service.select_retained(request)
    assert replay.generation == first.generation and replay.phase == "closed"
    assert s.service.status().generation == second.generation
    with pytest.raises(DevelopmentSessionConflict, match="request_conflict"):
        s.service.select_retained(
            request.model_copy(update={"target_id": s.ids["matrix"]})
        )
    with pytest.raises(DevelopmentSessionConflict, match="generation_conflict"):
        s.service.select_retained(request, actor="mcp:" + str(uuid4()))

    def revoked(_session: Any) -> None:
        raise PermissionError("revoked")

    with pytest.raises(PermissionError, match="revoked"):
        s.service.select_retained(request, authorize=revoked)
    preview = s.service.selection_preview(s.ids["omega"], s.env.workspace_id)
    s.records[2] = replace(s.records[2], container_id="f" * 64)
    with pytest.raises(DevelopmentSessionConflict, match="identity_changed"):
        s.service.select_retained(
            RetainedSelectionRequest(
                request_id=uuid4(),
                expected_generation=second.generation,
                target_id=s.ids["omega"],
                workspace_id=s.env.workspace_id,
                identity=preview.identity,
            )
        )
    assert s.service.status().generation == second.generation


@pytest.mark.parametrize(
    "state",
    [
        ContainerRuntimeState.RUNNING,
        ContainerRuntimeState.UNKNOWN,
        ContainerRuntimeState.TRANSITIONAL,
    ],
)
def test_active_or_unknown_inventory_never_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: ContainerRuntimeState
) -> None:
    s = environment(tmp_path, monkeypatch)
    s.choose("omega")
    s.records[2] = replace(s.records[2], runtime_state=state)
    with pytest.raises(DevelopmentSessionConflict, match="runtime_active"):
        s.choose("nexus")
    assert s.service.status().target_id == s.ids["omega"]


def test_unrelated_or_fifth_profile_is_never_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = environment(tmp_path, monkeypatch)
    s.choose("omega")
    s.records.append(
        replace(
            s.records[0],
            managed_instance_id=str(fixture.TARGET_ID),
            container_id="e" * 64,
        )
    )
    with pytest.raises(DevelopmentSessionConflict, match="inventory_requires_review"):
        s.service.selection_preview(s.ids["nexus"], s.env.workspace_id)
    with pytest.raises((DevelopmentSessionConflict, ManagedCapacityError)):
        s.admission.run_provision(
            instance_id=str(fixture.TARGET_ID),
            revision_id=str(fixture.RUNTIME_REVISION_ID),
            dispatch=lambda: pytest.fail("must not provision"),
        )


def test_four_stopped_provisions_reserve_without_selecting_or_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = environment(tmp_path, monkeypatch, provisioned=False)
    for i, (_g, tid) in enumerate(s.ids.items(), 1):

        def dispatch(tid: UUID = tid, i: int = i) -> ProvisionedManagedContainerRecord:
            r = ManagedContainerRecord(
                str(tid),
                str(i) * 64,
                "sha256:" + str(i) * 64,
                ContainerRuntimeState.STOPPED,
                ContainerHealthState.HEALTHY,
                str(s.revisions[tid]),
            )
            s.records.append(r)
            return ProvisionedManagedContainerRecord(
                r.managed_instance_id,
                str(r.runtime_revision_id),
                r.container_id,
                r.image_id,
                r.runtime_state,
                True,
            )

        s.admission.run_provision(
            instance_id=str(tid), revision_id=str(s.revisions[tid]), dispatch=dispatch
        )
        assert s.service.status() is None
    assert len(s.records) == 4 and all(
        r.runtime_state is ContainerRuntimeState.STOPPED for r in s.records
    )
    s.choose("omega")
    with pytest.raises(DevelopmentSessionConflict, match="profiles_still_present"):
        s.service.configure_retained(
            RetainedPolicyRequest(enabled=False, expected_generation=1)
        )


def test_start_reservation_serializes_selection_and_other_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = environment(tmp_path, monkeypatch)
    s.choose("omega")
    tid = s.ids["omega"]
    s.lease(tid)
    entered, finish = Event(), Event()

    def dispatch() -> StartManagedContainerRecord:
        entered.set()
        assert finish.wait(5)
        s.records[2] = replace(
            s.records[2], runtime_state=ContainerRuntimeState.RUNNING
        )
        return StartManagedContainerRecord(
            str(tid), s.records[2].container_id, ContainerRuntimeState.RUNNING, True
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(
            s.admission.run_start,
            instance_id=str(tid),
            container_id=s.records[2].container_id,
            revision_id=str(s.revisions[tid]),
            dispatch=dispatch,
        )
        try:
            assert entered.wait(5)
            with pytest.raises(DevelopmentSessionConflict, match="busy"):
                s.choose("nexus")
            with pytest.raises(IntegrityError):
                s.lease(s.ids["nexus"])
        finally:
            finish.set()
        future.result()
    assert sum(r.runtime_state is ContainerRuntimeState.RUNNING for r in s.records) == 1


def test_uncertain_start_survives_restart_and_only_exact_stop_frees_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = environment(tmp_path, monkeypatch)
    s.choose("omega")
    tid = s.ids["omega"]
    s.lease(tid)
    r = s.records[2]

    def uncertain() -> Any:
        raise TimeoutError("lost Docker reply")

    with pytest.raises(TimeoutError):
        s.admission.run_start(
            instance_id=str(tid),
            container_id=r.container_id,
            revision_id=str(s.revisions[tid]),
            dispatch=uncertain,
        )
    restart = DevelopmentSessionService(
        s.env.sessions, s.inventory, data_root=tmp_path / "data"
    )
    monkeypatch.setattr(restart, "_runtime", s.service._runtime)
    preview = restart.selection_preview(s.ids["nexus"], s.env.workspace_id)
    request = RetainedSelectionRequest(
        request_id=uuid4(),
        expected_generation=1,
        target_id=s.ids["nexus"],
        workspace_id=s.env.workspace_id,
        identity=preview.identity,
    )
    with pytest.raises(DevelopmentSessionConflict, match="busy"):
        restart.select_retained(request)
    s.admission.confirmed_stop(
        instance_id=str(tid), container_id="f" * 64, revision_id=str(s.revisions[tid])
    )
    with s.env.sessions() as session:
        assert session.get(ManagedStartReservationRow, str(tid)).phase == "uncertain"
    s.admission.confirmed_stop(
        instance_id=str(tid),
        container_id=r.container_id,
        revision_id=str(s.revisions[tid]),
    )
    with s.env.sessions() as session:
        assert session.get(ManagedStartReservationRow, str(tid)) is None


def test_pending_use_hold_and_unselected_restart_stage_are_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = environment(tmp_path, monkeypatch)
    s.choose("omega")
    with s.env.sessions.begin() as session:
        session.add(
            ManagedInstanceUseRow(
                managed_instance_id=str(s.ids["omega"]),
                actor_key="mcp:" + str(uuid4()),
                last_activity_at=datetime.now(UTC).isoformat(),
                hold_until=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                released_at=None,
            )
        )
    with pytest.raises(DevelopmentSessionConflict, match="use_active"):
        s.choose("nexus")
    from kodi_lab.domain import ManagedInstanceMutationKind
    from kodi_lab.storage.repositories import ManagedInstanceMutationLeaseRepository

    with (
        s.env.sessions.begin() as session,
        pytest.raises(DevelopmentSessionConflict, match="selection_required"),
    ):
        ManagedInstanceMutationLeaseRepository(session).reserve(
            s.ids["nexus"],
            kind=ManagedInstanceMutationKind.ADDON_ZIP_INSTALLATION,
            operation_id=uuid4(),
        )


@pytest.mark.parametrize("state", ["pending", "dispatched", "ambiguous"])
def test_lifecycle_intent_before_lease_prevents_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    s = environment(tmp_path, monkeypatch)
    s.choose("omega")
    with s.env.sessions.begin() as session:
        session.add(
            ManagedAgentLifecycleRequestRow(
                id=str(uuid4()),
                client_id=str(s.env.client_id),
                request_id=str(uuid4()),
                original_instance_id=str(s.ids["omega"]),
                managed_instance_id=str(s.ids["omega"]),
                action="ensure_ready",
                hold_minutes=15,
                state=state,
                created_at=fixture.NOW_TEXT,
                updated_at=fixture.NOW_TEXT,
            )
        )
    with pytest.raises(DevelopmentSessionConflict, match="lifecycle_pending"):
        s.choose("nexus")


@pytest.mark.parametrize(
    "state", ["pending", "active", "revoking", "blocked", "remote"]
)
def test_expired_input_ownership_still_prevents_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    s = environment(tmp_path, monkeypatch)
    s.choose("omega")
    with s.env.sessions.begin() as session:
        if state == "remote":
            session.add(
                ManagedRemoteGuardRow(
                    instance_id=str(s.ids["omega"]),
                    invocation_id=str(uuid4()),
                    process_id=str(uuid4()),
                    state="reserved",
                )
            )
        else:
            session.add(
                NovncSessionRow(
                    instance_id=str(s.ids["omega"]),
                    attempt=str(uuid4()),
                    owner="a" * 64,
                    mode="view-only",
                    state=state,
                    identity_json="{}",
                    expires_at=fixture.NOW_TEXT,
                    absolute_expires_at=fixture.NOW_TEXT,
                )
            )
    with pytest.raises(DevelopmentSessionConflict, match="input_requires_review"):
        s.choose("nexus")


def test_uncertain_provision_reserves_capacity_and_prevents_any_start_or_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = environment(tmp_path, monkeypatch)
    s.choose("omega")
    with s.env.sessions.begin() as session:
        session.add(
            ManagedProvisionReservationRow(
                managed_instance_id=str(s.ids["nexus"]),
                runtime_revision_id=str(s.revisions[s.ids["nexus"]]),
                operation_id=str(uuid4()),
                phase="uncertain",
                session_generation=None,
                created_at=fixture.NOW_TEXT,
            )
        )
    from kodi_lab.application.managed_resources import ManagedResourceService

    resources = ManagedResourceService(
        s.env.sessions,
        SimpleNamespace(managed_container_inventory=s.inventory),
        cast(Any, None),
    )
    assert resources.capacity().limit == 1
    assert resources.capacity().used == 1 and not resources.capacity().available
    with pytest.raises(DevelopmentSessionConflict, match="busy"):
        s.choose("matrix")
    from kodi_lab.storage.development_sessions import require_session_admission

    with (
        s.env.sessions() as session,
        pytest.raises(DevelopmentSessionConflict, match="provision_pending"),
    ):
        require_session_admission(
            session,
            s.records,
            target_id=str(s.ids["omega"]),
            revision_id=str(s.revisions[s.ids["omega"]]),
            container_id=s.records[2].container_id,
            starting=True,
        )


def test_only_selected_start_admits_and_other_running_inventory_is_never_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = environment(tmp_path, monkeypatch)
    s.choose("omega")
    from kodi_lab.storage.development_sessions import require_session_admission

    with s.env.sessions() as session, pytest.raises(DevelopmentSessionConflict):
        require_session_admission(
            session,
            s.records,
            target_id=str(s.ids["nexus"]),
            revision_id=str(s.revisions[s.ids["nexus"]]),
            container_id=s.records[1].container_id,
            starting=True,
        )

    s.records[1] = replace(s.records[1], runtime_state=ContainerRuntimeState.RUNNING)
    with (
        s.env.sessions() as session,
        pytest.raises(DevelopmentSessionConflict, match="other_runtime_active"),
    ):
        require_session_admission(
            session,
            s.records,
            target_id=str(s.ids["omega"]),
            revision_id=str(s.revisions[s.ids["omega"]]),
            container_id=s.records[2].container_id,
            starting=True,
        )


def test_selected_preparation_binds_only_its_new_container_with_other_profiles_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = environment(tmp_path, monkeypatch)
    old_profiles = {t: p.read_bytes() for t, p in s.profiles.items()}
    nexus = s.records.pop(1)
    current, _ = s.choose("nexus")
    assert current.phase == "reserved" and current.container_id is None

    def create() -> ProvisionedManagedContainerRecord:
        s.records.append(nexus)
        return ProvisionedManagedContainerRecord(
            nexus.managed_instance_id,
            str(nexus.runtime_revision_id),
            nexus.container_id,
            nexus.image_id,
            ContainerRuntimeState.STOPPED,
            True,
        )

    s.admission.run_provision(
        instance_id=nexus.managed_instance_id,
        revision_id=str(nexus.runtime_revision_id),
        dispatch=create,
    )
    assert s.service.status().phase == "bound"
    assert s.service.status().container_id == nexus.container_id
    assert (
        len(s.records) == 4
        and {t: p.read_bytes() for t, p in s.profiles.items()} == old_profiles
    )


def test_lost_unselected_provision_never_retries_or_releases_its_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = environment(tmp_path, monkeypatch, provisioned=False)
    s.choose("omega")

    def lost() -> Any:
        raise TimeoutError("lost create reply")

    with pytest.raises(TimeoutError):
        s.admission.run_provision(
            instance_id=str(s.ids["nexus"]),
            revision_id=str(s.revisions[s.ids["nexus"]]),
            dispatch=lost,
        )
    with s.env.sessions() as session:
        row = session.get(ManagedProvisionReservationRow, str(s.ids["nexus"]))
        assert row.phase == "uncertain" and row.session_generation is None
        operation_id = row.operation_id
    with pytest.raises(ManagedCapacityError, match="requires_reconciliation"):
        s.admission.run_provision(
            instance_id=str(s.ids["nexus"]),
            revision_id=str(s.revisions[s.ids["nexus"]]),
            dispatch=lambda: pytest.fail("never repeat uncertain create"),
        )
    with s.env.sessions() as session:
        assert (
            session.get(
                ManagedProvisionReservationRow, str(s.ids["nexus"])
            ).operation_id
            == operation_id
        )
    with pytest.raises(DevelopmentSessionConflict, match="busy"):
        s.choose("matrix")


def test_unselected_restart_fails_before_staging_or_lifecycle_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.lifecycle_operations import (
        ManagedInstanceLifecycleOperationCoordinator,
    )
    from kodi_lab.domain import (
        ManagedInstanceLifecycleKind,
        ManagedInstanceSemanticState,
    )
    from kodi_lab.storage.models import ManagedInstanceLifecycleOperationRow

    s = environment(tmp_path, monkeypatch)
    s.choose("omega")
    coordinator = ManagedInstanceLifecycleOperationCoordinator(s.env.sessions)
    with pytest.raises(DevelopmentSessionConflict, match="selection_required"):
        coordinator.begin(
            s.ids["nexus"],
            instance_name="Nexus",
            kind=ManagedInstanceLifecycleKind.START,
            prior_state=ManagedInstanceSemanticState.STOPPED,
        )
    with s.env.sessions() as session:
        assert session.scalar(select(ManagedInstanceLifecycleOperationRow)) is None
        assert session.scalar(select(ManagedInstanceMutationLeaseRow)) is None


def test_concurrent_web_and_mcp_selection_share_one_generation_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = environment(tmp_path, monkeypatch)
    s.choose("omega")
    requests = []
    for g in ("nexus", "matrix"):
        p = s.service.selection_preview(s.ids[g], s.env.workspace_id)
        requests.append(
            RetainedSelectionRequest(
                request_id=uuid4(),
                expected_generation=p.latest_generation,
                target_id=p.target_id,
                workspace_id=p.workspace_id,
                identity=p.identity,
            )
        )

    def select_one(pair: tuple[RetainedSelectionRequest, str]) -> str:
        try:
            s.service.select_retained(pair[0], actor=pair[1])
        except DevelopmentSessionConflict as error:
            return error.code
        return "selected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                select_one,
                zip(
                    requests, ("web_admin", "mcp:" + str(s.env.client_id)), strict=True
                ),
            )
        )
    assert sorted(results) == ["development_session_generation_conflict", "selected"]
    assert s.service.status().generation == 2
    assert len(s.records) == 4
