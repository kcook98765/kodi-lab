"""Real leases, broker Start proofs and trust with isolated native doubles."""

import hashlib
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.lifecycle_operations import LifecycleOperationConflictError
from kodi_lab.application.managed_bridge_preparation import (
    BridgePreparationError,
    ManagedBridgePreparationService,
)
from kodi_lab.application.managed_bridge_trust import (
    ManagedBridgeTrustService,
    invalidate_managed_bridge_trust,
)
from kodi_lab.broker.docker_inventory import ContainerHealthState
from kodi_lab.broker.managed_bridge_trust import (
    approved_bridge_build,
    require_reviewed_installation_lease,
)
from kodi_lab.broker.operations import EstablishManagedBridgeTrustResult
from kodi_lab.domain.create_kodi import CreateCheckpoint as CP
from kodi_lab.kodi.rc1_bridge import (
    packaged_rc1_bridge_sources,
    packaged_stable_bridge_sources,
)
from kodi_lab.storage.models import (
    ManagedBridgePreparationChildRow,
    ManagedBridgeTrustRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
)
from kodi_lab.storage.secrets import ExternalBridgeSecretStore
from tests.application.test_create_kodi_installation import (
    factory as factory,
)
from tests.application.test_create_kodi_installation import (
    finish,
)
from tests.application.test_create_kodi_installation import (
    schema as schema,
)
from tests.application.test_managed_bridge_trust import Client
from tests.create_kodi_provisioning_support import Interrupted
from tests.create_kodi_runner_support import Environment


class Native:
    def __init__(self, env: Environment, version: str) -> None:
        self.env, self.version = env, version
        self.installed = self.enabled = False

    def post(self, *, body: bytes, **kwargs: Any) -> bytes:
        request = json.loads(body)
        if request["method"].startswith("Addons."):
            if request["method"] == "Addons.GetAddonDetails":
                result: Any = (
                    {
                        "addon": {
                            "addonid": "service.kodi_lab_bridge",
                            "version": self.version,
                            "enabled": self.enabled,
                        }
                    }
                    if self.installed
                    else {}
                )
            else:
                assert request["method"] == "Addons.SetAddonEnabled"
                self.enabled = request["params"]["enabled"]
                result = "OK"
            return json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "result": result}
            ).encode()
        return self.env.transport.post(body=body, **kwargs)


class Setup:
    def __init__(
        self,
        factory: sessionmaker[Session],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        generation: str = "omega",
        keep_running: bool = False,
    ) -> None:
        self.env = Environment(
            factory,
            cached=generation != "piers",
            generation=generation,
            start_after_setup=keep_running,
            preview_approved=generation == "piers",
            bridge_required=True,
        )
        original_dispatch = self.env.broker.dispatch

        def dispatch(target: str) -> Any:
            result = original_dispatch(target)
            self.env.broker.records = [
                replace(record, health_state=ContainerHealthState.HEALTHY)
                if record.managed_instance_id == target
                else record
                for record in self.env.broker.records
            ]
            return result

        monkeypatch.setattr(self.env.broker, "dispatch", dispatch)
        release = {
            "matrix": "19.4",
            "nexus": "20.5",
            "omega": "21.3",
            "piers": "22.0-rc.1",
        }[generation]
        major, minor = (int(x) for x in release.split("-")[0].split("."))
        self.env.transport.native_version = {
            "major": major,
            "minor": minor,
            "tag": "releasecandidate" if generation == "piers" else "stable",
            **({"tagversion": "1"} if generation == "piers" else {}),
        }
        self.client = Client()
        build = approved_bridge_build(release)
        self.client.value = replace(
            self.client.value,
            kodi_version="22.0" if generation == "piers" else release,
            kodi_release=release if generation == "piers" else None,
            addon_version=build["addon_version"],
            build_fingerprint=build["build_fingerprint"],
        )
        secrets = ExternalBridgeSecretStore(
            tmp_path / "bridge-secrets", master_key=b"x" * 32, key_version=1
        )
        self.trust = ManagedBridgeTrustService(
            factory, cast(Any, self.env.broker), secrets, client=self.client
        )
        self.native = Native(self.env, build["addon_version"])
        self.trust.transport = self.native
        self.env.lifecycle._bridge_trust = self.trust
        self.service = ManagedBridgePreparationService(factory, self.env.lifecycle)
        self.env.runner.bridge = self.service
        self.stages = 0
        self.fault = ""
        self.error: Exception | None = None
        original_prepare = self.env.lifecycle.prepare_managed_bridge

        def prepare(*args: Any, **kwargs: Any) -> Any:
            try:
                return original_prepare(*args, **kwargs)
            except Exception as error:
                self.error = error
                raise

        monkeypatch.setattr(self.env.lifecycle, "prepare_managed_bridge", prepare)

        def establish(**kwargs: Any) -> EstablishManagedBridgeTrustResult:
            with factory() as session:
                require_reviewed_installation_lease(session, str(self.env.op.target_id))
                assert session.get(
                    ManagedInstanceMutationLeaseRow, str(self.env.op.target_id)
                )
            if kwargs.get("install_reviewed"):
                self.stages += 1
                if self.fault == "stage":
                    raise RuntimeError("fixture stage failure")
                if self.fault == "interrupted":
                    raise Interrupted()
                self.native.installed = True
            return EstablishManagedBridgeTrustResult(
                managed_instance_id=kwargs["managed_instance_id"],
                container_id=kwargs["expected_container_id"],
                generation=kwargs["generation"],
                bridge_id=str(self.client.value.bridge_id),
            )

        monkeypatch.setattr(
            self.env.broker, "establish_managed_bridge_trust", establish, raising=False
        )

    def native_complete(self) -> None:
        for _ in range(24):
            if self.env.op.checkpoint is CP.COMPLETE:
                return
            assert self.env.op.checkpoint is not CP.NEEDS_ATTENTION
            self.env.step()
            if (
                self.env.op.stage.value == "installation"
                and self.env.op.checkpoint is CP.DISPATCHED
            ):
                assert self.env.op.installation_operation_id is not None
                finish(self.env.sessions, self.env.op.installation_operation_id)
        pytest.fail("native setup did not complete")

    def finish(self) -> Any:
        self.native_complete()
        for _ in range(6):
            self.env.step()
            receipt = self.service.for_create(self.env.op.id)
            if receipt and receipt.checkpoint in {"complete", "needs_attention"}:
                return receipt
        pytest.fail("Bridge preparation did not finish")


@pytest.fixture
def setup(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Setup]:
    value = Setup(factory, tmp_path, monkeypatch)
    try:
        yield value
    finally:
        value.env.close()


@pytest.mark.parametrize("generation", ["matrix", "nexus", "omega", "piers"])
@pytest.mark.parametrize("keep_running", [False, True])
def test_fresh_create_prepares_exact_reviewed_bridge_and_requested_final_state(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generation: str,
    keep_running: bool,
) -> None:
    setup = Setup(factory, tmp_path, monkeypatch, generation, keep_running)
    try:
        result = setup.finish()
        assert result.checkpoint == "complete", (
            result.reason,
            repr(setup.error),
            repr(setup.error.__cause__) if setup.error else None,
        )
        assert setup.stages == 1
        assert setup.env.broker.provisions == 1 and setup.env.broker.starts == 2
        assert setup.env.broker.stops == (1 if keep_running else 2)
        assert setup.env.broker.records[0].runtime_state == (
            "running" if keep_running else "stopped"
        )
        assert setup.native.installed and setup.native.enabled
        with factory() as session:
            trust = session.get(ManagedBridgeTrustRow, str(setup.env.op.target_id))
            assert trust and trust.state == "active"
            assert (trust.container_id, trust.runtime_revision_id) == (
                setup.env.op.container_id,
                str(setup.env.op.runtime_revision_id),
            )
            assert (
                session.get(ManagedInstanceMutationLeaseRow, result.target_id) is None
            )
        before = (setup.stages, setup.env.broker.starts, setup.env.broker.stops)
        for _ in range(3):
            setup.env.step()
        assert before == (setup.stages, setup.env.broker.starts, setup.env.broker.stops)
    finally:
        setup.env.close()


def test_actual_packaged_builds_match_each_reviewed_inventory() -> None:
    for release in ("19.4", "20.5", "21.3", "22.0-rc.1"):
        build = approved_bridge_build(release)
        sources = (
            packaged_rc1_bridge_sources()
            if release == "22.0-rc.1"
            else packaged_stable_bridge_sources()
        )
        assert all(
            hashlib.sha256(sources[name]).hexdigest() == digest
            for name, digest in build["files"].items()
        )
    assert approved_bridge_build("22.0-rc.1")["addon_version"] == "1.1.32"


def test_existing_valid_bridge_checks_without_staging_or_restart(setup: Setup) -> None:
    first = setup.finish()
    row = setup.service.accept(
        request_id=uuid4(), target_id=UUID(first.target_id), keep_running=True
    )
    # Start the same stopped profile, then authenticate as a no-op.
    row = setup.service.continue_once(UUID(row.id), version=row.version)
    starts, stops, stages = (
        setup.env.broker.starts,
        setup.env.broker.stops,
        setup.stages,
    )
    row = setup.service.continue_once(UUID(row.id), version=row.version)
    assert row.checkpoint == "complete", row.reason
    assert (starts, stops, stages) == (
        setup.env.broker.starts,
        setup.env.broker.stops,
        setup.stages,
    )


@pytest.mark.parametrize("fault", ["stage", "restart", "authenticate"])
def test_failures_keep_profile_and_original_receipt_without_false_completion(
    setup: Setup, fault: str
) -> None:
    setup.native_complete()
    original = setup.env.op.container_id
    setup.fault = "stage" if fault == "stage" else ""
    if fault == "restart":
        setup.env.broker.start_fault = "lost_response"
    if fault == "authenticate":
        setup.client.fail = "bridge_authentication_failed"
    row = setup.finish()
    assert row.checkpoint == "needs_attention"
    assert setup.env.broker.records[0].container_id == original
    assert setup.env.broker.provisions == 1
    stages, starts = setup.stages, setup.env.broker.starts
    assert setup.service.continue_once(UUID(row.id), version=row.version).id == row.id
    assert (stages, starts) == (setup.stages, setup.env.broker.starts)
    with pytest.raises(BridgePreparationError):
        setup.service.resume(UUID(row.id), version=row.version)


def test_interrupted_dispatch_is_never_reissued(setup: Setup) -> None:
    setup.native_complete()
    setup.env.step()  # durable maintenance acceptance
    setup.fault = "interrupted"
    with pytest.raises(Interrupted):
        setup.env.step()
    row = setup.service.for_create(setup.env.op.id)
    assert row and row.checkpoint == "dispatched"
    again = setup.service.continue_once(UUID(row.id), version=row.version)
    assert again.id == row.id and setup.stages == 1
    with setup.env.sessions() as session:
        links = list(session.scalars(select(ManagedBridgePreparationChildRow)))
        assert len(links) == 1
        child = session.get(ManagedInstanceLifecycleOperationRow, links[0].operation_id)
        assert child and child.state.value == "running"


def test_identity_drift_before_dispatch_refuses_without_install(setup: Setup) -> None:
    setup.native_complete()
    setup.env.step()
    setup.env.broker.records[0] = replace(
        setup.env.broker.records[0], container_id="e" * 64
    )
    setup.env.step()
    row = setup.service.for_create(setup.env.op.id)
    assert row and row.checkpoint == "needs_attention" and setup.stages == 0
    with pytest.raises(BridgePreparationError, match="identity_changed"):
        setup.service.resume(UUID(row.id), version=row.version)
    assert setup.service.recovery_action(row) == "prepare_current"
    current = setup.service.accept(request_id=uuid4(), target_id=setup.env.op.target_id)
    assert current.id != row.id and current.container_id == "e" * 64
    for _ in range(2):
        current = setup.service.continue_once(UUID(current.id), version=current.version)
    assert current.checkpoint == "complete"
    original = setup.service.get(UUID(row.id))
    assert original and original.container_id != current.container_id
    assert original.checkpoint == "needs_attention"


def test_maintenance_receipt_without_its_live_child_cannot_stage(setup: Setup) -> None:
    setup.native_complete()
    setup.env.step()
    row = setup.service.for_create(setup.env.op.id)
    assert row and row.checkpoint == "pending"
    with pytest.raises(PermissionError, match="maintenance_authority_changed"):
        setup.trust.stage_reviewed(setup.env.op.target_id, preparation_id=UUID(row.id))
    assert setup.stages == 0
    with setup.env.sessions() as session:
        assert session.get(ManagedBridgeTrustRow, row.target_id) is None


def test_explicit_retry_before_mutation_keeps_parent_and_all_child_attempts(
    setup: Setup,
) -> None:
    setup.native_complete()
    setup.env.step()
    setup.env.lifecycle._bridge_trust = None
    setup.env.step()
    original = setup.service.for_create(setup.env.op.id)
    assert original and original.checkpoint == "needs_attention" and setup.stages == 0
    setup.env.lifecycle._bridge_trust = setup.trust
    row = setup.service.resume(UUID(original.id), version=original.version)
    assert row.id == original.id and row.attempt == 1
    for _ in range(2):
        row = setup.service.continue_once(UUID(row.id), version=row.version)
    assert row.checkpoint == "complete" and setup.stages == 1
    with setup.env.sessions() as session:
        links = list(
            session.scalars(
                select(ManagedBridgePreparationChildRow).where(
                    ManagedBridgePreparationChildRow.preparation_id == row.id
                )
            )
        )
        assert len(links) == 3  # Failed original, repaired Bridge, final Stop.
        assert len({link.operation_id for link in links}) == 3


def test_invalidated_trust_requires_explicit_repair_on_same_profile(
    setup: Setup,
) -> None:
    first = setup.finish()
    with setup.env.sessions.begin() as session:
        invalidate_managed_bridge_trust(
            session, UUID(first.target_id), secrets=setup.trust.secrets
        )
    row = setup.service.accept(request_id=uuid4(), target_id=UUID(first.target_id))
    for _ in range(3):
        row = setup.service.continue_once(UUID(row.id), version=row.version)
    assert row.checkpoint == "complete", row.reason
    assert row.container_id == first.container_id and setup.stages == 2


def test_concurrent_continuation_and_stop_cannot_disturb_owner(setup: Setup) -> None:
    setup.native_complete()
    setup.env.step()
    row = setup.service.for_create(setup.env.op.id)
    assert row
    entered, release = Event(), Event()
    original = setup.trust.stage_reviewed

    def stage(*args: Any, **kwargs: Any) -> None:
        entered.set()
        assert release.wait(5)
        original(*args, **kwargs)

    setup.trust.stage_reviewed = stage  # type: ignore[method-assign]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                setup.service.continue_once, UUID(row.id), version=row.version
            )
            assert entered.wait(5)
            losing = setup.service.continue_once(UUID(row.id), version=row.version + 1)
            assert losing.checkpoint == "dispatched"
            with pytest.raises(LifecycleOperationConflictError):
                setup.env.lifecycle.stop(setup.env.op.target_id)
            release.set()
            result = first.result(timeout=5)
        assert result.phase == "stop" and result.checkpoint == "pending"
        assert setup.stages == 1
    finally:
        release.set()


def test_retained_nexus_repair_preserves_hub_selection_profiles_and_agent_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.storage.development_sessions import DevelopmentSessionConflict
    from kodi_lab.storage.models import (
        DevelopmentSessionRow,
        McpClientRow,
        McpClientTargetGrantRow,
    )
    from tests.application.test_managed_start_selection import setup as retained_setup
    from tests.create_kodi_readiness_support import Transport

    env = retained_setup(tmp_path, monkeypatch)
    sessions = env.s.env.sessions
    env.s.choose("omega")
    target = env.s.ids["nexus"]
    # Synthetic policy catalog; sealed runtime admission is exercised above
    # with all four reviewed recipe installations.
    monkeypatch.setattr(
        "kodi_lab.application.managed_bridge_preparation.require_managed_runtime",
        lambda *_args, **_kwargs: None,
    )
    original_inventory = env.broker.managed_container_inventory

    def inventory(**kwargs: Any) -> Any:
        result = original_inventory()
        if kwargs.get("managed_instance_id"):
            return result.model_copy(
                update={
                    "containers": [
                        r
                        for r in result.containers
                        if r.managed_instance_id == kwargs["managed_instance_id"]
                    ]
                }
            )
        return result

    monkeypatch.setattr(env.broker, "managed_container_inventory", inventory)
    transport = Transport()
    transport.native_version = {"major": 20, "minor": 5, "tag": "stable"}
    native = Native(SimpleNamespace(transport=transport), "1.1.31")  # type: ignore[arg-type]
    client = Client()
    client.value = replace(client.value, kodi_version="20.5")
    store = ExternalBridgeSecretStore(
        tmp_path / "nexus-secrets", master_key=b"x" * 32, key_version=1
    )
    trust = ManagedBridgeTrustService(sessions, env.broker, store, client=client)
    trust.transport = native
    env.lifecycle._bridge_trust = trust
    service = ManagedBridgePreparationService(sessions, env.lifecycle)
    count = 0

    def establish(**kwargs: Any) -> EstablishManagedBridgeTrustResult:
        nonlocal count
        with sessions() as session:
            require_reviewed_installation_lease(session, str(target))
        if kwargs.get("install_reviewed"):
            count += 1
            native.installed = True
        return EstablishManagedBridgeTrustResult(
            managed_instance_id=str(target),
            container_id=kwargs["expected_container_id"],
            generation=kwargs["generation"],
            bridge_id=str(client.value.bridge_id),
        )

    monkeypatch.setattr(
        env.broker, "establish_managed_bridge_trust", establish, raising=False
    )
    protected = (DevelopmentSessionRow, McpClientTargetGrantRow, McpClientRow)
    with sessions.begin() as session:
        session.add(
            ManagedBridgeTrustRow(
                target_id=str(target),
                runtime_revision_id=str(env.s.revisions[target]),
                container_id="f" * 64,
                bridge_port=8765,
                state="invalidated",
                generation=6,
                prepared_at="2026-11-11T00:00:00.000000Z",
            )
        )
    with sessions() as session:
        before = [session.execute(select(model.__table__)).all() for model in protected]
    profiles = {key: path.read_bytes() for key, path in env.s.profiles.items()}
    try:
        with pytest.raises(
            DevelopmentSessionConflict, match="retained_selection_required"
        ):
            env.lifecycle.install_reviewed_bridge(target)
        request = uuid4()
        row = service.accept(request_id=request, target_id=target)
        for _ in range(3):
            row = service.continue_once(UUID(row.id), version=row.version)
        assert row.checkpoint == "complete", row.reason
        assert count == 1 and env.broker.starts == 2 and env.broker.stops == 2
        assert service.accept(request_id=request, target_id=target).id == row.id
        assert {
            key: path.read_bytes() for key, path in env.s.profiles.items()
        } == profiles
        with sessions() as session:
            assert [
                session.execute(select(model.__table__)).all() for model in protected
            ] == before
            current = session.get(ManagedBridgeTrustRow, str(target))
            assert current and current.state == "active" and current.generation > 6
            assert current.container_id == row.container_id
    finally:
        env.readiness.close()
