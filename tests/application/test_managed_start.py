"""Ordinary Start crosses real SQLite authority/reservations with fake Docker."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
    ManagedContainerRecord,
    StartManagedContainerRecord,
)
from kodi_lab.broker.managed_capacity import ManagedCapacityAdmission
from kodi_lab.broker.operations import (
    DeprovisionManagedContainerResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    ProvisionManagedContainerResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.kodi.readiness import (
    JsonRpcState,
    KodiApplicationIdentity,
    ReadinessResult,
    UsabilityState,
)
from kodi_lab.runtime.install import bootstrap_recipe_instance
from kodi_lab.runtime.recipes import load_reviewed_recipe, recipe_target_id
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    DevelopmentSourceRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedStartAuthorizationRow,
    RuntimeRecipeInstallationRow,
)
from tests.runtime.test_recipe_installation import _ready
from tests.runtime.test_recipe_installation import factory as factory


class PausedDispatch(BaseException):
    """Model process exit after claim, before handing the request to the broker."""


class NativeReady:
    def wait_until_usable(self, *_args: Any, **_kwargs: Any) -> ReadinessResult:
        return ReadinessResult(
            ContainerRuntimeState.RUNNING,
            KodiProcessState.RUNNING,
            JsonRpcState.RESPONSIVE,
            UsabilityState.USABLE,
            None,
            None,
            KodiApplicationIdentity("Kodi", 21, 3, "stable"),
            0,
            1,
        )


class Broker:
    def __init__(
        self, sessions: sessionmaker[Session], record: ManagedContainerRecord
    ) -> None:
        self.records = [record]
        self.admission = ManagedCapacityAdmission(sessions, lambda: tuple(self.records))
        self.starts = self.stops = 0
        self.pause = False
        self.proof: SecretStr | None = None
        self.entered: Event | None = None
        self.finish: Event | None = None

    def provision_managed_container(
        self, **_kwargs: Any
    ) -> ProvisionManagedContainerResult:
        pytest.fail("Start must not provision")

    def deprovision_managed_container(
        self, **_kwargs: Any
    ) -> DeprovisionManagedContainerResult:
        pytest.fail("Start must not replace a profile")

    def managed_container_inventory(
        self, **_kwargs: Any
    ) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                ManagedContainerInventoryEntry(
                    **{
                        key: getattr(r, key)
                        for key in ManagedContainerInventoryEntry.model_fields
                    }
                )
                for r in self.records
            ]
        )

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        r = next(
            r for r in self.records if r.managed_instance_id == managed_instance_id
        )
        assert r.container_id == expected_container_id
        return ObserveManagedKodiRuntimeResult(
            managed_instance_id=managed_instance_id,
            container_id=r.container_id,
            runtime_revision_id=str(r.runtime_revision_id),
            runtime_state=r.runtime_state,
            process_state=KodiProcessState.RUNNING
            if r.runtime_state == "running"
            else KodiProcessState.NOT_RUNNING,
            ipv4_address="10.250.0.10",
            jsonrpc_port=8080,
        )

    def start_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
        ordinary_start_proof: SecretStr | None = None,
    ) -> StartManagedContainerResult:
        self.proof = ordinary_start_proof
        if self.pause:
            raise PausedDispatch()
        r = next(
            r for r in self.records if r.managed_instance_id == managed_instance_id
        )
        result = self.admission.run_start(
            instance_id=managed_instance_id,
            container_id=expected_container_id,
            revision_id=str(r.runtime_revision_id),
            ordinary_start_proof=ordinary_start_proof.get_secret_value()
            if ordinary_start_proof
            else None,
            dispatch=lambda: self.dispatch(managed_instance_id),
        )
        return StartManagedContainerResult(
            **{k: getattr(result, k) for k in StartManagedContainerResult.model_fields}
        )

    def dispatch(self, target: str) -> StartManagedContainerRecord:
        self.starts += 1
        if self.entered is not None and self.finish is not None:
            self.entered.set()
            assert self.finish.wait(5)
        r = next(r for r in self.records if r.managed_instance_id == target)
        self.records[self.records.index(r)] = replace(
            r, runtime_state=ContainerRuntimeState.RUNNING
        )
        return StartManagedContainerRecord(
            target, r.container_id, ContainerRuntimeState.RUNNING, True
        )

    def stop_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StopManagedContainerResult:
        self.stops += 1
        r = next(
            r for r in self.records if r.managed_instance_id == managed_instance_id
        )
        assert r.container_id == expected_container_id
        self.records[self.records.index(r)] = replace(
            r, runtime_state=ContainerRuntimeState.STOPPED
        )
        self.admission.confirmed_stop(
            instance_id=managed_instance_id,
            container_id=expected_container_id,
            revision_id=str(r.runtime_revision_id),
        )
        return StopManagedContainerResult(
            managed_instance_id=managed_instance_id,
            container_id=expected_container_id,
            runtime_state=ContainerRuntimeState.STOPPED,
            stop_performed=True,
        )


class Environment:
    def __init__(self, sessions: sessionmaker[Session], tmp_path: Path) -> None:
        self.sessions = sessions
        operation = _ready(sessions, generation="omega")
        reviewed = load_reviewed_recipe("omega")
        self.target = recipe_target_id(reviewed)
        with sessions() as session:
            installation = session.get(RuntimeRecipeInstallationRow, str(operation))
            assert installation is not None and installation.runtime_revision_id
            self.revision = installation.runtime_revision_id
            self.image = str(installation.image_id)
        bootstrap_recipe_instance(sessions, reviewed, UUID(self.revision))
        with sessions.begin() as session:
            session.add(
                DevelopmentSessionPolicyRow(
                    id=1,
                    retain_profiles=True,
                    retained_omega_target_id=str(self.target),
                )
            )
        self.broker = Broker(
            sessions,
            ManagedContainerRecord(
                managed_instance_id=str(self.target),
                runtime_revision_id=self.revision,
                image_id=self.image,
                container_id="a" * 64,
                runtime_state=ContainerRuntimeState.STOPPED,
                health_state=ContainerHealthState.HEALTHY,
            ),
        )
        self.readiness = ReadinessTaskRegistry()
        self.status = ManagedRuntimeStatusService(sessions, self.broker, self.readiness)
        self.lifecycle = ManagedRuntimeLifecycleService(
            sessions,
            self.broker,
            self.readiness,
            self.status,
            coordinator_factory=lambda *_: NativeReady(),
        )
        self.profile = tmp_path / "retained-profile"
        self.profile.mkdir()
        (self.profile / "settings.xml").write_text("owner settings")

    def mint(self) -> str:
        self.broker.pause = True
        try:
            with pytest.raises(PausedDispatch):
                self.lifecycle.start_managed(self.target)
        finally:
            self.broker.pause = False
        assert self.broker.proof is not None
        return self.broker.proof.get_secret_value()


@pytest.fixture
def env(factory: sessionmaker[Session], tmp_path: Path) -> Iterator[Environment]:
    value = Environment(factory, tmp_path)
    try:
        yield value
    finally:
        value.readiness.close()


def test_no_workspace_start_stop_start_preserves_identity_and_profile(
    env: Environment,
) -> None:
    before = env.broker.records[0]
    contents = (env.profile / "settings.xml").read_bytes()
    env.lifecycle.start_managed(env.target)
    env.lifecycle.stop(env.target)
    env.lifecycle.start_managed(env.target)
    after = env.broker.records[0]
    assert after.container_id == before.container_id
    assert after.runtime_revision_id == before.runtime_revision_id
    assert after.image_id == before.image_id
    assert (env.profile / "settings.xml").read_bytes() == contents
    assert env.broker.starts == 2 and env.broker.stops == 1
    with env.sessions() as session:
        for model in (AgentWorkspaceRow, DevelopmentSourceRow, DevelopmentSessionRow):
            assert session.scalars(select(model)).all() == []
        authorizations = session.scalars(select(ManagedStartAuthorizationRow)).all()
        assert len(authorizations) == 2
        assert all(row.consumed_at for row in authorizations)
        assert session.scalars(select(ManagedInstanceMutationLeaseRow)).all() == []


def test_development_start_stays_strict_and_does_not_mint_proof(
    env: Environment,
) -> None:
    from kodi_lab.storage.development_sessions import DevelopmentSessionConflict

    with pytest.raises(DevelopmentSessionConflict, match="retained_selection_required"):
        env.lifecycle.start(env.target)
    with env.sessions() as session:
        assert session.scalars(select(ManagedStartAuthorizationRow)).all() == []
        assert session.scalars(select(ManagedInstanceLifecycleOperationRow)).all() == []
    assert env.broker.starts == 0


def test_claim_failure_rolls_back_authorization_operation_and_lease(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.lifecycle_operations import (
        LifecycleOperationConflictError,
    )
    from kodi_lab.storage.repositories import ManagedInstanceMutationLeaseRepository

    monkeypatch.setattr(
        ManagedInstanceMutationLeaseRepository, "claim", lambda *_a, **_kw: False
    )
    with pytest.raises(LifecycleOperationConflictError):
        env.lifecycle.start_managed(env.target)
    with env.sessions() as session:
        for model in (
            ManagedStartAuthorizationRow,
            ManagedInstanceLifecycleOperationRow,
            ManagedInstanceMutationLeaseRow,
        ):
            assert session.scalars(select(model)).all() == []
    assert env.broker.starts == 0


def test_database_backstop_still_denies_a_generic_start_without_selection(
    env: Environment,
) -> None:
    from datetime import UTC, datetime

    from sqlalchemy.exc import IntegrityError

    from kodi_lab.domain import (
        ManagedInstanceLifecycleKind,
        ManagedInstanceLifecycleOperationState,
        ManagedInstanceMutationKind,
        ManagedInstanceSemanticState,
    )
    from kodi_lab.storage.managed_activity import timestamp

    operation = str(uuid4())
    now = timestamp(datetime.now(UTC))
    with env.sessions.begin() as session:
        session.add(
            ManagedInstanceLifecycleOperationRow(
                id=operation,
                original_managed_instance_id=str(env.target),
                managed_instance_id=str(env.target),
                instance_name="Generic Start",
                kind=ManagedInstanceLifecycleKind.START,
                requested_at=now,
                started_at=now,
                prior_state=ManagedInstanceSemanticState.STOPPED,
                state=ManagedInstanceLifecycleOperationState.RUNNING,
            )
        )
    with (
        env.sessions.begin() as session,
        pytest.raises(IntegrityError, match="retained selection required"),
    ):
        session.add(
            ManagedInstanceMutationLeaseRow(
                managed_instance_id=str(env.target),
                operation_id=operation,
                operation_kind=ManagedInstanceMutationKind.LIFECYCLE,
            )
        )
        session.flush()
    assert env.broker.starts == 0


def test_lost_broker_response_is_not_success_even_when_kodi_is_running(
    env: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.reconciliation import (
        ManagedInstanceReconciliationRequiredError,
    )
    from kodi_lab.application.runtime_lifecycle import LifecycleDispatchUnconfirmedError
    from kodi_lab.broker.errors import ClientTransportError
    from kodi_lab.domain import ManagedInstanceLifecycleOperationState

    dispatch = env.broker.start_managed_container

    def lost(**kwargs: Any) -> StartManagedContainerResult:
        dispatch(**kwargs)
        raise ClientTransportError("response lost")

    monkeypatch.setattr(env.broker, "start_managed_container", lost)
    with pytest.raises(LifecycleDispatchUnconfirmedError):
        env.lifecycle.start_managed(env.target)
    assert env.broker.records[0].runtime_state is ContainerRuntimeState.RUNNING
    with env.sessions() as session:
        operation = session.scalars(select(ManagedInstanceLifecycleOperationRow)).one()
        assert (
            operation.state
            is ManagedInstanceLifecycleOperationState.RECONCILIATION_REQUIRED
        )
        assert operation.failure_reason == "broker_runtime_dispatch_unconfirmed"
        assert session.scalars(select(ManagedStartAuthorizationRow)).one().consumed_at
    with pytest.raises(ManagedInstanceReconciliationRequiredError):
        env.lifecycle.start_managed(env.target)
    assert env.broker.starts == 1


def test_already_running_does_not_replace_existing_readiness_or_dispatch_again(
    env: Environment,
) -> None:
    entered, finish = Event(), Event()

    class WaitingNative(NativeReady):
        def wait_until_usable(self, *_args: Any, **_kwargs: Any) -> ReadinessResult:
            entered.set()
            assert finish.wait(5)
            return super().wait_until_usable()

    env.lifecycle._coordinator_factory = lambda *_: WaitingNative()
    try:
        env.lifecycle.start_managed(env.target)
        assert entered.wait(5)
        generation = env.readiness.snapshot(env.target).generation
        env.lifecycle.start_managed(env.target)
        assert env.readiness.snapshot(env.target).generation == generation
        assert env.broker.starts == 1
        with env.sessions() as session:
            assert len(session.scalars(select(ManagedStartAuthorizationRow)).all()) == 1
    finally:
        finish.set()
