"""Service-level regressions for Bridge side effects and readiness ownership."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from kodi_lab.application.lifecycle_operations import LifecycleOperationConflictError
from kodi_lab.application.managed_bridge_trust import ManagedBridgeTrustService
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationRequiredError,
)
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.broker.managed_capacity import ManagedCapacityError
from kodi_lab.broker.operations import (
    EstablishManagedBridgeTrustResult,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
    StartManagedContainerResult,
    StopManagedContainerResult,
)
from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
from kodi_lab.storage.models import ManagedBridgeTrustRow
from kodi_lab.storage.secrets import ExternalBridgeSecretStore
from tests.application.test_managed_bridge_trust import Client, Native
from tests.application.test_managed_resources import ImmediateReadiness
from tests.application.test_retained_development import environment


class Broker:
    def __init__(self, s: Any, client: Client) -> None:
        self.s, self.client = s, client
        self.stages = self.starts = self.stops = 0
        self.start_entered: Event | None = None
        self.start_finish: Event | None = None

    def managed_container_inventory(
        self, *, managed_instance_id: str | None = None
    ) -> ManagedContainerInventoryResult:
        return ManagedContainerInventoryResult(
            containers=[
                r
                for r in self.s.inventory().containers
                if managed_instance_id is None
                or r.managed_instance_id == managed_instance_id
            ]
        )

    def record(self, tid: str) -> Any:
        return next(r for r in self.s.records if r.managed_instance_id == tid)

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        r = self.record(managed_instance_id)
        assert r.container_id == expected_container_id
        return ObserveManagedKodiRuntimeResult(
            managed_instance_id=managed_instance_id,
            runtime_revision_id=str(r.runtime_revision_id),
            container_id=r.container_id,
            runtime_state=r.runtime_state,
            process_state=KodiProcessState.RUNNING,
            ipv4_address="10.250.0.10",
            jsonrpc_port=8080,
        )

    def establish_managed_bridge_trust(
        self, **kwargs: Any
    ) -> EstablishManagedBridgeTrustResult:
        self.stages += 1
        tid = UUID(kwargs["managed_instance_id"])
        self.s.profiles[tid].with_name("bridge-stage.txt").write_text(str(self.stages))
        kwargs.pop("install_reviewed", None)
        return EstablishManagedBridgeTrustResult(
            managed_instance_id=str(tid),
            container_id=kwargs["expected_container_id"],
            generation=kwargs["generation"],
            bridge_id=str(self.client.value.bridge_id),
        )

    def stop_managed_container(self, **kwargs: Any) -> StopManagedContainerResult:
        self.stops += 1
        tid = kwargs["managed_instance_id"]
        r = self.record(tid)
        self.s.records[self.s.records.index(r)] = replace(
            r, runtime_state=ContainerRuntimeState.STOPPED
        )
        self.s.admission.confirmed_stop(
            instance_id=tid,
            container_id=r.container_id,
            revision_id=str(r.runtime_revision_id),
        )
        return StopManagedContainerResult(
            managed_instance_id=tid,
            container_id=r.container_id,
            runtime_state=ContainerRuntimeState.STOPPED,
            stop_performed=True,
        )

    def start_managed_container(self, **kwargs: Any) -> StartManagedContainerResult:
        from kodi_lab.broker.docker_inventory import StartManagedContainerRecord

        tid = kwargs["managed_instance_id"]
        r = self.record(tid)

        def dispatch() -> StartManagedContainerRecord:
            self.starts += 1
            if self.start_entered is not None and self.start_finish is not None:
                self.start_entered.set()
                assert self.start_finish.wait(5)
            self.s.records[self.s.records.index(r)] = replace(
                r, runtime_state=ContainerRuntimeState.RUNNING
            )
            return StartManagedContainerRecord(
                tid, r.container_id, ContainerRuntimeState.RUNNING, True
            )

        result = self.s.admission.run_start(
            instance_id=tid,
            container_id=r.container_id,
            revision_id=str(r.runtime_revision_id),
            dispatch=dispatch,
        )
        return StartManagedContainerResult(
            **{k: getattr(result, k) for k in StartManagedContainerResult.model_fields}
        )


def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from kodi_lab.application.lifecycle_operations import (
        ManagedInstanceLifecycleOperationCoordinator,
    )
    from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService

    s = environment(tmp_path, monkeypatch)
    s.choose("omega")
    client = Client()
    s.broker = Broker(s, client)
    s.secrets = ExternalBridgeSecretStore(
        tmp_path / "secrets", master_key=b"x" * 32, key_version=1
    )
    s.owner = ManagedBridgeTrustService(
        s.env.sessions, cast(Any, s.broker), s.secrets, client=client
    )
    s.owner.transport = Native()
    s.readiness = ReadinessTaskRegistry(max_workers=1)
    reconciliation = ManagedInstanceReconciliationService(
        s.env.sessions, broker=s.broker, readiness=s.readiness
    )
    s.status = ManagedRuntimeStatusService(
        s.env.sessions,
        s.broker,
        s.readiness,
        reconciliation,
        recover_running_readiness=True,
        coordinator_factory=lambda *_: ImmediateReadiness(),
    )
    s.lifecycle = ManagedRuntimeLifecycleService(
        s.env.sessions,
        cast(Any, s.broker),
        s.readiness,
        s.status,
        bridge_trust=s.owner,
        lifecycle_operations=ManagedInstanceLifecycleOperationCoordinator(
            s.env.sessions, reconciliation=reconciliation
        ),
        coordinator_factory=lambda *_: ImmediateReadiness(),
    )
    return s


def files(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_selected_container_drift_refuses_reviewed_bridge_before_any_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = setup(tmp_path, monkeypatch)
    try:
        s.records[2] = replace(
            s.records[2], runtime_state=ContainerRuntimeState.RUNNING
        )
        assert s.owner.prepare(s.ids["omega"])
        before = files(tmp_path / "secrets"), files(tmp_path / "profiles")
        with s.env.sessions() as session:
            trust = session.execute(select(ManagedBridgeTrustRow.__table__)).all()
        stages = s.broker.stages
        s.records[2] = replace(s.records[2], container_id="f" * 64)
        with pytest.raises((DevelopmentSessionConflict, ManagedCapacityError)):
            s.lifecycle.install_reviewed_bridge(s.ids["omega"])
        assert (files(tmp_path / "secrets"), files(tmp_path / "profiles")) == before
        assert s.broker.stages == stages and s.broker.stops == 0
        with s.env.sessions() as session:
            assert (
                session.execute(select(ManagedBridgeTrustRow.__table__)).all() == trust
            )
    finally:
        s.readiness.close()


def test_readiness_bridge_preparation_owns_mutation_until_all_side_effects_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = setup(tmp_path, monkeypatch)
    entered, finish = Event(), Event()
    original = s.owner._call

    def paused(*args: Any, **kwargs: Any) -> Any:
        if args[2] == "Addons.GetAddonDetails":
            entered.set()
            assert finish.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(s.owner, "_call", paused)
    try:
        s.lifecycle.start(s.ids["omega"])
        assert entered.wait(5)
        with pytest.raises(LifecycleOperationConflictError):
            s.lifecycle.stop(s.ids["omega"])
        with pytest.raises(DevelopmentSessionConflict, match="busy"):
            s.choose("nexus")
    finally:
        finish.set()
        s.readiness.close()
    s.lifecycle.stop(s.ids["omega"])
    before = files(tmp_path / "secrets"), files(tmp_path / "profiles")
    s.choose("nexus")
    assert (files(tmp_path / "secrets"), files(tmp_path / "profiles")) == before
    assert s.broker.stages == 1


@pytest.mark.parametrize("web_target", ["omega", "nexus"])
def test_actual_web_and_mcp_lifecycle_start_paths_cannot_double_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, web_target: str
) -> None:
    from kodi_lab.application.managed_agent_lifecycle import (
        ManagedAgentLifecycleService,
    )
    from kodi_lab.application.managed_resources import ManagedResourceService

    s = setup(tmp_path, monkeypatch)
    resources = ManagedResourceService(
        s.env.sessions,
        s.broker,
        s.lifecycle,
        playback_factory=lambda *_: SimpleNamespace(playback_active=lambda: False),
    )
    agent = ManagedAgentLifecycleService(
        s.env.sessions,
        resources,
        s.lifecycle,
        s.status,
        lock_root=tmp_path / "agent-locks",
    )
    s.broker.start_entered, s.broker.start_finish = Event(), Event()
    request_id = uuid4()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(
                agent.ensure_ready,
                client_id=s.env.client_id,
                request_id=request_id,
                target_id=s.ids["omega"],
                authorize=lambda _: None,
            )
            try:
                assert s.broker.start_entered.wait(5)
                with pytest.raises(
                    (DevelopmentSessionConflict, LifecycleOperationConflictError)
                ):
                    s.lifecycle.start(s.ids[web_target])
                assert s.broker.starts == 1
            finally:
                s.broker.start_finish.set()
            result = future.result(timeout=10)
            assert result.receipt_state in {"ready", "not_ready"}
        s.readiness.close()
        replay = agent.ensure_ready(
            client_id=s.env.client_id,
            request_id=request_id,
            target_id=s.ids["omega"],
            authorize=lambda _: None,
        )
        assert replay.ready and replay.receipt_state == "ready", replay
        assert s.broker.starts == 1
        assert sum(r.runtime_state == "running" for r in s.records) == 1
        release = agent.release(
            client_id=s.env.client_id,
            request_id=uuid4(),
            target_id=s.ids["omega"],
            authorize=lambda _: None,
        )
        assert release.receipt_state == "released"
        assert s.broker.stops == 1
    finally:
        s.readiness.close()
    assert len(s.records) == 4


def test_legacy_readiness_worker_blocks_retained_opt_in_until_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.development_sessions import RetainedPolicyRequest

    s = setup(tmp_path, monkeypatch)
    s.records[:] = [s.records[2]]  # A legacy single-container installation.
    s.service.configure_retained(
        RetainedPolicyRequest(enabled=False, expected_generation=1)
    )
    entered, finish = Event(), Event()
    original = s.owner._call

    def paused(*args: Any, **kwargs: Any) -> Any:
        if args[2] == "Addons.GetAddonDetails":
            entered.set()
            assert finish.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(s.owner, "_call", paused)
    try:
        s.lifecycle.start(s.ids["omega"])
        assert entered.wait(5)
        with pytest.raises(DevelopmentSessionConflict, match="busy"):
            s.service.configure_retained(
                RetainedPolicyRequest(
                    enabled=True, expected_generation=1, omega_target_id=s.ids["omega"]
                )
            )
    finally:
        finish.set()
        s.readiness.close()
    s.lifecycle.stop(s.ids["omega"])
    s.service.configure_retained(
        RetainedPolicyRequest(
            enabled=True, expected_generation=1, omega_target_id=s.ids["omega"]
        )
    )


def test_read_only_bridge_unavailability_keeps_trust_and_native_receipt_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.managed_bridge_trust import ManagedBridgeTrustError
    from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
    from kodi_lab.kodi.bridge import ExternalBridgeError
    from kodi_lab.storage.repositories import ManagedRuntimeProjectionRepository

    s = setup(tmp_path, monkeypatch)
    s.records[2] = replace(s.records[2], runtime_state=ContainerRuntimeState.RUNNING)
    try:
        s.owner.prepare(s.ids["omega"])
        before = files(tmp_path / "secrets")
        with s.env.sessions() as session:
            token = ManagedRuntimeProjectionRepository(session).observation_token(
                s.ids["omega"]
            )

        def unavailable(*_: Any, **__: Any) -> Any:
            raise ExternalBridgeError("bridge_unreachable")

        monkeypatch.setattr(s.owner, "_resident_identity", unavailable)
        with pytest.raises(ManagedBridgeTrustError, match="unavailable"):
            s.owner.prepare_background(s.ids["omega"])
        with s.env.sessions() as session:
            assert (
                ManagedRuntimeProjectionRepository(session).observation_token(
                    s.ids["omega"]
                )
                == token
            )
            assert (
                session.get(ManagedBridgeTrustRow, str(s.ids["omega"])).state
                == "active"
            )
        assert files(tmp_path / "secrets") == before
        assert (
            not ManagedInstanceReconciliationService(s.env.sessions)
            .status(s.ids["omega"])
            .required
        )
    finally:
        s.readiness.close()


def test_lost_bridge_write_keeps_typed_reconciliation_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.development_sessions import (
        DevelopmentSessionService,
        RetainedSelectionRequest,
    )
    from kodi_lab.application.managed_bridge_trust import ManagedBridgeTrustError
    from kodi_lab.storage.models import ManagedInstanceLifecycleOperationRow

    s = setup(tmp_path, monkeypatch)
    s.records[2] = replace(s.records[2], runtime_state=ContainerRuntimeState.RUNNING)
    original = s.broker.establish_managed_bridge_trust

    def lost(**kwargs: Any) -> Any:
        original(**kwargs)
        raise TimeoutError("lost provisioning reply")

    monkeypatch.setattr(s.broker, "establish_managed_bridge_trust", lost)
    try:
        with pytest.raises(ManagedBridgeTrustError):
            s.owner.prepare_background(s.ids["omega"])
        with s.env.sessions() as session:
            op = session.scalar(select(ManagedInstanceLifecycleOperationRow))
            assert op is not None and op.state == "reconciliation_required"
            operation_id = op.id
        from kodi_lab.application.reconciliation import (
            ManagedInstanceReconciliationService,
        )

        live = ManagedInstanceReconciliationService(
            s.env.sessions, broker=s.broker, readiness=s.readiness
        )
        assert live.status(s.ids["omega"]).blocking
        with pytest.raises(ManagedInstanceReconciliationRequiredError):
            s.lifecycle.install_reviewed_bridge(s.ids["omega"])
        assert s.profiles[s.ids["omega"]].with_name("bridge-stage.txt").is_file()
        s.records[2] = replace(
            s.records[2], runtime_state=ContainerRuntimeState.STOPPED
        )
        assert live.status(s.ids["omega"]).blocking
        with pytest.raises(ManagedInstanceReconciliationRequiredError):
            s.lifecycle.start(s.ids["omega"])
        restarted = DevelopmentSessionService(
            s.env.sessions, s.inventory, data_root=tmp_path / "data"
        )
        monkeypatch.setattr(restarted, "_runtime", s.service._runtime)
        p = restarted.selection_preview(s.ids["nexus"], s.env.workspace_id)
        with pytest.raises(DevelopmentSessionConflict, match="reconciliation_required"):
            restarted.select_retained(
                RetainedSelectionRequest(
                    request_id=uuid4(),
                    expected_generation=p.latest_generation,
                    target_id=p.target_id,
                    workspace_id=p.workspace_id,
                    identity=p.identity,
                )
            )
        with s.env.sessions() as session:
            assert (
                session.get(ManagedInstanceLifecycleOperationRow, operation_id).state
                == "reconciliation_required"
            )
    finally:
        s.readiness.close()


def test_production_native_recovery_cannot_discard_owned_bridge_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = setup(tmp_path, monkeypatch)
    entered, finish = Event(), Event()

    class RecoveredReadiness:
        def wait_until_usable(self, *args: Any, **kwargs: Any) -> Any:
            entered.set()
            assert finish.wait(5)
            return ImmediateReadiness().wait_until_usable(*args, **kwargs)

    s.status._coordinator_factory = lambda *_: RecoveredReadiness()
    try:
        s.lifecycle.start(s.ids["omega"])
        assert entered.wait(5)
        finish.set()
        with s.readiness._lock:
            future = s.readiness._entries[s.ids["omega"]].future
        assert future is not None
        future.result(timeout=5)
        assert s.broker.stages == 1
        with s.env.sessions() as session:
            assert (
                session.get(ManagedBridgeTrustRow, str(s.ids["omega"])).state
                == "active"
            )
    finally:
        finish.set()
        s.readiness.close()


@pytest.mark.parametrize("drift", ["container", "image", "additional"])
def test_running_ensure_refuses_drift_before_new_receipt_or_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    from kodi_lab.application.managed_agent_lifecycle import (
        ManagedAgentLifecycleError,
        ManagedAgentLifecycleService,
    )
    from kodi_lab.application.managed_resources import ManagedResourceService
    from kodi_lab.storage.models import (
        ManagedAgentLifecycleRequestRow,
        ManagedInstanceUseRow,
    )

    s = setup(tmp_path, monkeypatch)
    try:
        s.lifecycle.start(s.ids["omega"])
        with s.readiness._lock:
            future = s.readiness._entries[s.ids["omega"]].future
        assert future is not None
        future.result(timeout=5)
        selected = s.service.status()
        with s.env.sessions() as session:
            uses_before = session.execute(select(ManagedInstanceUseRow.__table__)).all()
        if drift == "container":
            s.records[2] = replace(s.records[2], container_id="f" * 64)
        elif drift == "image":
            s.records[2] = replace(s.records[2], image_id="sha256:" + "f" * 64)
        else:
            s.records.append(
                replace(
                    s.records[0],
                    managed_instance_id=str(uuid4()),
                    container_id="e" * 64,
                )
            )
        resources = ManagedResourceService(s.env.sessions, s.broker, s.lifecycle)
        agent = ManagedAgentLifecycleService(
            s.env.sessions,
            resources,
            s.lifecycle,
            s.status,
            lock_root=tmp_path / "agent-locks",
        )
        with pytest.raises(ManagedAgentLifecycleError):
            agent.ensure_ready(
                client_id=s.env.client_id,
                request_id=uuid4(),
                target_id=s.ids["omega"],
                authorize=lambda _: None,
            )
        with s.env.sessions() as session:
            assert session.scalar(select(ManagedAgentLifecycleRequestRow)) is None
            assert (
                session.execute(select(ManagedInstanceUseRow.__table__)).all()
                == uses_before
            )
        assert s.service.status() == selected
    finally:
        s.readiness.close()


def test_historical_ensure_cannot_report_ready_after_image_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.managed_agent_lifecycle import (
        ManagedAgentLifecycleService,
    )
    from kodi_lab.application.managed_resources import ManagedResourceService

    s = setup(tmp_path, monkeypatch)
    try:
        s.lifecycle.start(s.ids["omega"])
        with s.readiness._lock:
            future = s.readiness._entries[s.ids["omega"]].future
        assert future is not None
        future.result(timeout=5)
        resources = ManagedResourceService(s.env.sessions, s.broker, s.lifecycle)
        agent = ManagedAgentLifecycleService(
            s.env.sessions,
            resources,
            s.lifecycle,
            s.status,
            lock_root=tmp_path / "agent-locks",
        )
        request_id = uuid4()
        first = agent.ensure_ready(
            client_id=s.env.client_id,
            request_id=request_id,
            target_id=s.ids["omega"],
            authorize=lambda _: None,
        )
        assert first.ready
        s.records[2] = replace(s.records[2], image_id="sha256:" + "f" * 64)
        replay = agent.ensure_ready(
            client_id=s.env.client_id,
            request_id=request_id,
            target_id=s.ids["omega"],
            authorize=lambda _: None,
        )
        assert not replay.ready and replay.code == "runtime_changed_since_request"
        assert (
            replay.operation_id == first.operation_id
            and replay.hold_until == first.hold_until
        )
        assert s.broker.starts == 1
    finally:
        s.readiness.close()


@pytest.mark.parametrize("action", ["install", "resume"])
def test_synchronous_bridge_lost_write_retains_original_blocking_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    from kodi_lab.application.development_sessions import RetainedSelectionRequest
    from kodi_lab.application.managed_bridge_trust import (
        ManagedBridgeMutationUnconfirmedError,
    )
    from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
    from kodi_lab.storage.models import ManagedInstanceLifecycleOperationRow

    s = setup(tmp_path, monkeypatch)
    s.records[2] = replace(s.records[2], runtime_state=ContainerRuntimeState.RUNNING)
    # Isolate the synchronous operation; readiness ownership is tested above.
    monkeypatch.setattr(s.lifecycle, "_queue_readiness", lambda *_: None)
    try:
        if action == "resume":
            s.lifecycle.install_reviewed_bridge(s.ids["omega"])
            with s.env.sessions() as session:
                generation = session.get(
                    ManagedBridgeTrustRow, str(s.ids["omega"])
                ).generation
        original = s.broker.establish_managed_bridge_trust

        def lost(**kwargs: Any) -> Any:
            original(**kwargs)
            raise TimeoutError("lost synchronous write reply")

        monkeypatch.setattr(s.broker, "establish_managed_bridge_trust", lost)
        with pytest.raises(ManagedBridgeMutationUnconfirmedError):
            if action == "install":
                s.lifecycle.install_reviewed_bridge(s.ids["omega"])
            else:
                s.lifecycle.resume_reviewed_bridge(
                    s.ids["omega"], expected_generation=generation
                )
        with s.env.sessions() as session:
            uncertain = session.scalars(
                select(ManagedInstanceLifecycleOperationRow).where(
                    ManagedInstanceLifecycleOperationRow.state
                    == "reconciliation_required"
                )
            ).all()
            assert len(uncertain) == 1
            operation_id = uncertain[0].id
            assert uncertain[0].failure_reason == "broker_runtime_dispatch_unconfirmed"
        live = ManagedInstanceReconciliationService(
            s.env.sessions, broker=s.broker, readiness=s.readiness
        )
        for state in (ContainerRuntimeState.RUNNING, ContainerRuntimeState.STOPPED):
            s.records[2] = replace(s.records[2], runtime_state=state)
            assert live.status(s.ids["omega"]).blocking
            with pytest.raises(ManagedInstanceReconciliationRequiredError):
                s.lifecycle.install_reviewed_bridge(s.ids["omega"])
        p = s.service.selection_preview(s.ids["nexus"], s.env.workspace_id)
        with pytest.raises(DevelopmentSessionConflict, match="reconciliation_required"):
            s.service.select_retained(
                RetainedSelectionRequest(
                    request_id=uuid4(),
                    expected_generation=p.latest_generation,
                    target_id=p.target_id,
                    workspace_id=p.workspace_id,
                    identity=p.identity,
                )
            )
        with s.env.sessions() as session:
            assert (
                session.get(ManagedInstanceLifecycleOperationRow, operation_id).state
                == "reconciliation_required"
            )
        assert s.service.status().target_id == s.ids["omega"] and len(s.records) == 4
    finally:
        s.readiness.close()


@pytest.mark.parametrize(
    "code", ["docker_start_failed", "post_start_verification_failed", "internal_error"]
)
def test_remote_native_start_uncertainty_cannot_be_hidden_by_bridge_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    from kodi_lab.application.runtime_lifecycle import LifecycleDispatchUnconfirmedError
    from kodi_lab.broker.errors import DockerStartError, RemoteBrokerError
    from kodi_lab.storage.models import (
        ManagedInstanceLifecycleOperationRow,
        ManagedStartReservationRow,
    )

    s = setup(tmp_path, monkeypatch)
    tid, r = s.ids["omega"], s.records[2]

    def remote(**_: Any) -> Any:
        def dispatched() -> Any:
            s.broker.starts += 1
            s.records[2] = replace(r, runtime_state=ContainerRuntimeState.RUNNING)
            raise DockerStartError("lost verification after native start")

        try:
            s.admission.run_start(
                instance_id=str(tid),
                container_id=r.container_id,
                revision_id=str(r.runtime_revision_id),
                dispatch=dispatched,
            )
        except DockerStartError as error:
            raise RemoteBrokerError(
                code, "fixed production-shaped failure", request_id=str(uuid4())
            ) from error

    monkeypatch.setattr(s.broker, "start_managed_container", remote)
    before = files(tmp_path / "secrets"), files(tmp_path / "profiles")
    try:
        with pytest.raises(LifecycleDispatchUnconfirmedError):
            s.lifecycle.start(tid)
        with s.env.sessions() as session:
            reservation = session.get(ManagedStartReservationRow, str(tid))
            assert reservation is not None and reservation.phase == "uncertain"
            reservation_id = reservation.operation_id
            op = session.scalar(select(ManagedInstanceLifecycleOperationRow))
            assert op is not None and op.state == "reconciliation_required"
            assert op.failure_reason == "broker_runtime_dispatch_unconfirmed"
        with pytest.raises(ManagedInstanceReconciliationRequiredError):
            s.lifecycle.install_reviewed_bridge(tid)
        with pytest.raises(ManagedInstanceReconciliationRequiredError):
            s.lifecycle.resume_reviewed_bridge(tid, expected_generation=1)
        assert (files(tmp_path / "secrets"), files(tmp_path / "profiles")) == before
        assert s.broker.starts == 1 and s.broker.stops == s.broker.stages == 0
        assert not s.owner.transport.calls
        with s.env.sessions() as session:
            assert (
                session.get(ManagedStartReservationRow, str(tid)).operation_id
                == reservation_id
            )
            assert (
                len(session.scalars(select(ManagedInstanceLifecycleOperationRow)).all())
                == 1
            )
    finally:
        s.readiness.close()


@pytest.mark.parametrize("kind", ["start", "provision"])
def test_old_runtime_reservation_refuses_bridge_effects_and_is_never_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from kodi_lab.storage.models import (
        ManagedProvisionReservationRow,
        ManagedStartReservationRow,
    )
    from tests.application.test_mcp_workspace_development import NOW_TEXT

    s = setup(tmp_path, monkeypatch)
    tid, r = s.ids["omega"], s.records[2]
    s.records[2] = replace(r, runtime_state=ContainerRuntimeState.RUNNING)
    model = (
        ManagedStartReservationRow
        if kind == "start"
        else ManagedProvisionReservationRow
    )
    with s.env.sessions.begin() as session:
        values = dict(
            managed_instance_id=str(tid),
            runtime_revision_id=str(r.runtime_revision_id),
            operation_id=str(uuid4()),
            phase="uncertain",
            created_at=NOW_TEXT,
        )
        if kind == "start":
            values["container_id"] = r.container_id
        session.add(model(**values))
    before = files(tmp_path / "secrets"), files(tmp_path / "profiles")
    try:
        for action in ("install", "resume"):
            with pytest.raises(DevelopmentSessionConflict, match=kind + "_pending"):
                if action == "install":
                    s.lifecycle.install_reviewed_bridge(tid)
                else:
                    s.lifecycle.resume_reviewed_bridge(tid, expected_generation=1)
        assert (files(tmp_path / "secrets"), files(tmp_path / "profiles")) == before
        assert s.broker.starts == s.broker.stages == s.broker.stops == 0
        s.lifecycle.stop(tid)  # Explicit exact containment retains its old route.
        assert s.broker.stops == 1
        with s.env.sessions() as session:
            if kind == "start":
                assert session.get(model, str(tid)) is None
            else:
                assert session.get(model, str(tid)).phase == "uncertain"
        assert len(s.records) == 4
    finally:
        s.readiness.close()


@pytest.mark.parametrize(
    "code", ["docker_stop_failed", "post_stop_verification_failed"]
)
def test_remote_stop_uncertainty_is_not_cleared_by_stopped_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    from kodi_lab.application.runtime_lifecycle import LifecycleDispatchUnconfirmedError
    from kodi_lab.broker.errors import RemoteBrokerError
    from kodi_lab.storage.models import ManagedInstanceLifecycleOperationRow

    s = setup(tmp_path, monkeypatch)
    s.records[2] = replace(s.records[2], runtime_state=ContainerRuntimeState.RUNNING)

    def remote(**_: Any) -> Any:
        s.records[2] = replace(
            s.records[2], runtime_state=ContainerRuntimeState.STOPPED
        )
        raise RemoteBrokerError(
            code, "fixed lost stop confirmation", request_id=str(uuid4())
        )

    monkeypatch.setattr(s.broker, "stop_managed_container", remote)
    try:
        with pytest.raises(LifecycleDispatchUnconfirmedError):
            s.lifecycle.stop(s.ids["omega"])
        with s.env.sessions() as session:
            op = session.scalar(select(ManagedInstanceLifecycleOperationRow))
            assert op is not None and op.state == "reconciliation_required"
            assert op.failure_reason == "broker_runtime_dispatch_unconfirmed"
        with pytest.raises(DevelopmentSessionConflict, match="reconciliation_required"):
            s.choose("nexus")
    finally:
        s.readiness.close()


def test_remote_provision_error_keeps_create_reservation_and_original_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.application.runtime_lifecycle import LifecycleDispatchUnconfirmedError
    from kodi_lab.broker.errors import RemoteBrokerError
    from kodi_lab.storage.models import (
        ManagedInstanceLifecycleOperationRow,
        ManagedProvisionReservationRow,
    )

    s = setup(tmp_path, monkeypatch)
    nexus = s.records.pop(1)

    def remote(**_: Any) -> Any:
        def create() -> Any:
            s.records.append(nexus)
            raise RuntimeError("lost create result")

        try:
            s.admission.run_provision(
                instance_id=nexus.managed_instance_id,
                revision_id=str(nexus.runtime_revision_id),
                dispatch=create,
            )
        except RuntimeError as error:
            raise RemoteBrokerError(
                "internal_error", "fixed unconfirmed create", request_id=str(uuid4())
            ) from error

    monkeypatch.setattr(s.broker, "provision_managed_container", remote, raising=False)
    try:
        with pytest.raises(LifecycleDispatchUnconfirmedError):
            s.lifecycle.provision(s.ids["nexus"])
        with s.env.sessions() as session:
            row = session.get(ManagedProvisionReservationRow, nexus.managed_instance_id)
            assert row is not None and row.phase == "uncertain"
            reservation_id = row.operation_id
            op = session.scalar(select(ManagedInstanceLifecycleOperationRow))
            assert op is not None and op.state == "reconciliation_required"
        with pytest.raises(DevelopmentSessionConflict, match="provision_pending"):
            s.lifecycle.provision(s.ids["matrix"])
        with s.env.sessions() as session:
            assert (
                session.get(
                    ManagedProvisionReservationRow, nexus.managed_instance_id
                ).operation_id
                == reservation_id
            )
        assert len(s.records) == 4
    finally:
        s.readiness.close()


def test_proven_pre_dispatch_native_refusal_does_not_create_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.broker.errors import RemoteBrokerError
    from kodi_lab.storage.models import (
        ManagedInstanceLifecycleOperationRow,
        ManagedStartReservationRow,
    )

    s = setup(tmp_path, monkeypatch)

    def refused(**_: Any) -> Any:
        raise RemoteBrokerError(
            "container_identity_precondition_failed",
            "fixed refusal",
            request_id=str(uuid4()),
        )

    monkeypatch.setattr(s.broker, "start_managed_container", refused)
    try:
        with pytest.raises(RemoteBrokerError):
            s.lifecycle.start(s.ids["omega"])
        with s.env.sessions() as session:
            op = session.scalar(select(ManagedInstanceLifecycleOperationRow))
            assert (
                op is not None
                and op.state == "failed"
                and not op.reconciliation_required
            )
            assert session.scalar(select(ManagedStartReservationRow)) is None
        assert s.broker.starts == s.broker.stages == s.broker.stops == 0
    finally:
        s.readiness.close()
