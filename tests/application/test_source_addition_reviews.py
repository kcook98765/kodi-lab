"""Disposable database and tar fixtures exercise the complete guarded workflow."""

from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_deployments import (
    DevelopmentSourceDeploymentError,
    DevelopmentSourceDeploymentService,
)
from kodi_lab.application.source_addition_reviews import SourceAdditionReviewService
from kodi_lab.broker.development_sources import (
    DevelopmentSourceDeploymentStateError,
    SqliteDevelopmentSourceDeploymentResolver,
)
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.errors import DevelopmentSourceDeploymentClientError, RequestError
from kodi_lab.broker.operations import (
    CopySyncAttachedDevelopmentSourceResult,
    build_foundation_registry,
)
from kodi_lab.broker.protocol import Request
from kodi_lab.development.addition_reviews import SourceAdditionProof
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSourceRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceMutationLeaseRow,
    RuntimeExecutionProfileRow,
)
from kodi_lab.storage.repositories import DevelopmentSourceDeploymentRepository
from tests.application import test_development_source_deployments as fixture
from tests.application.test_development_source_deployments import factory as factory
from tests.application.test_session_source_copy import bind
from tests.broker.test_development_source_deployment import (
    FakeDeploymentDockerClient,
    _inspection,
)

PATH = "resources/generated.xml"


class LocalBroker(fixture.FakeBroker):
    def __init__(self, resolver: Any, adapter: Any) -> None:
        super().__init__()
        self.registry = build_foundation_registry(
            container_provider=adapter,
            development_source_deployment_resolver=resolver,
            development_source_deployment_provider=adapter,
        )

    def dispatch(self, name: str, params: dict[str, Any]) -> Any:
        try:
            return self.registry.dispatch(
                Request(
                    version=1, request_id=str(uuid4()), operation=name, params=params
                )
            )
        except RequestError as error:
            raise DevelopmentSourceDeploymentClientError(
                error.code, "Fixture broker refused", request_id=str(uuid4())
            ) from error

    def review_source_additions(self, **kwargs: Any) -> SourceAdditionProof:
        return SourceAdditionProof.model_validate(
            self.dispatch("review_source_additions", kwargs)
        )

    def copy_sync_attached_development_source(
        self, **kwargs: Any
    ) -> CopySyncAttachedDevelopmentSourceResult:
        return CopySyncAttachedDevelopmentSourceResult.model_validate(
            self.dispatch("copy_sync_attached_development_source", kwargs)
        )


def setup(factory: sessionmaker[Session], tmp_path: Path) -> tuple[Any, ...]:
    clock = fixture.Clock()
    bind(factory)
    source = tmp_path / "source"
    (source / "resources").mkdir()
    (source / "resources/original.xml").write_text("trusted")
    client = FakeDeploymentDockerClient()
    client.inspection = _inspection()
    client.inspection["Id"] = fixture.CONTAINER_ID
    client.inspection["Name"] = "/kodi-lab-managed-" + str(fixture.INSTANCE_ID).replace(
        "-", ""
    )
    client.inspection["Config"] = {
        "Labels": {
            "io.kodi-lab.managed": "true",
            "io.kodi-lab.instance-id": str(fixture.INSTANCE_ID),
            "io.kodi-lab.runtime-revision-id": str(fixture.REVISION_ID),
        }
    }
    client.containers = lambda **_: [{"Id": fixture.CONTAINER_ID}]  # type: ignore[method-assign]
    with factory.begin() as session:
        profile = session.get(RuntimeExecutionProfileRow, str(fixture.REVISION_ID))
        assert profile is not None
        profile.runtime_uid, profile.runtime_gid = 42, 43
    resolver = SqliteDevelopmentSourceDeploymentResolver(
        factory, development_root=tmp_path, data_root=tmp_path / "data", clock=clock
    )
    adapter = DockerManagedContainerAdapter(lambda: client)
    broker = LocalBroker(resolver, adapter)
    coordinator = fixture._coordinator(factory, tmp_path, broker, clock)
    operation = coordinator.queue(fixture.INSTANCE_ID, fixture.SOURCE_ID)
    lease = coordinator.acquire(operation.operation_id)
    assert lease is not None
    DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    client.entries[PATH] = (b"generated", 0o644, 42, 43)
    (source / "resources/original.xml").write_text("candidate")
    client.put_members.clear()
    client.applied = False
    service = SourceAdditionReviewService(factory, broker, coordinator, clock=clock)
    return service, coordinator, broker, resolver, client, clock


def test_real_preview_approval_copy_receipt_and_replay(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    service, coordinator, broker, resolver, client, _ = setup(factory, tmp_path)
    review = service.preview(fixture.INSTANCE_ID, fixture.SOURCE_ID, (PATH,))
    assert not client.put_members and review.operation_id is None
    submitted: list[UUID] = []
    approval = service.approve(review.id, (PATH,), submitted.append)
    assert approval.operation_id == submitted[0]
    assert service.approve(review.id, (PATH,), submitted.append) == approval
    assert len(submitted) == 1
    lease = coordinator.acquire(submitted[0])
    assert lease is not None
    spec = resolver.resolve(
        str(fixture.INSTANCE_ID),
        str(fixture.SOURCE_ID),
        str(lease.operation_id),
        fixture.CONTAINER_ID,
    )
    DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    receipt = service.get(review.id)
    assert receipt is not None and receipt.dispatched_at is not None
    assert (
        receipt.operation is not None and receipt.operation.state.value == "succeeded"
    )
    assert client.entries[PATH] == (b"generated", 0o644, 42, 43)
    with factory() as session:
        evidence = DevelopmentSourceDeploymentRepository(session).get(
            fixture.INSTANCE_ID, fixture.SOURCE_ID
        )
        assert (
            evidence is not None
            and evidence.verified_destination_digest == client.current_digest()
        )
    with (
        pytest.raises(DevelopmentSourceDeploymentStateError),
        resolver.approved_copy_guard(spec),
    ):
        pytest.fail("Consumed approval replayed")
    assert (
        service.approve(review.id, (PATH,), submitted.append).operation_id
        == lease.operation_id
    )
    assert len(submitted) == 1


@pytest.mark.parametrize(
    "change",
    [
        "workspace_generation",
        "workspace_digest",
        "source_registration",
        "attachment",
        "expiry",
        "missing_file_approval",
        "duplicate_approval",
    ],
)
def test_changes_before_approval_cannot_claim_copy(
    factory: sessionmaker[Session], tmp_path: Path, change: str
) -> None:
    service, _coordinator, _broker, _resolver, client, clock = setup(factory, tmp_path)
    review = service.preview(fixture.INSTANCE_ID, fixture.SOURCE_ID, (PATH,))
    with factory.begin() as session:
        if change.startswith("workspace"):
            workspace = session.scalar(select(AgentWorkspaceRow))
            assert workspace is not None
            if change == "workspace_generation":
                workspace.generation += 1
            else:
                workspace.current_tree_digest = "sha256:" + "a" * 64
        elif change == "source_registration":
            row = session.get(DevelopmentSourceRow, str(fixture.SOURCE_ID))
            assert row is not None
            row.validated_at = "2026-11-22T01:00:00Z"
        elif change == "attachment":
            attachment = session.scalar(
                select(ManagedInstanceDevelopmentSourceRow).where(
                    ManagedInstanceDevelopmentSourceRow.managed_instance_id
                    == str(fixture.INSTANCE_ID)
                )
            )
            assert attachment is not None
            session.delete(attachment)
    if change == "expiry":
        clock.now += timedelta(minutes=16)
    paths = (
        ()
        if change == "missing_file_approval"
        else (PATH, PATH)
        if change == "duplicate_approval"
        else (PATH,)
    )
    with pytest.raises((DevelopmentSourceDeploymentError, ValueError)):
        service.approve(review.id, paths, lambda _: pytest.fail("Copy submitted"))
    assert not client.put_members
    assert service.get(review.id).operation_id is None


@pytest.mark.parametrize(
    "change",
    [
        "generation",
        "revoked_lease",
        "expired_lease",
        "destination",
        "candidate",
        "container",
        "runtime",
        "postcopy_failure",
    ],
)
def test_changes_after_approval_refuse_or_retain_recovery(
    factory: sessionmaker[Session], tmp_path: Path, change: str
) -> None:
    service, coordinator, broker, _resolver, client, clock = setup(factory, tmp_path)
    review = service.preview(fixture.INSTANCE_ID, fixture.SOURCE_ID, (PATH,))
    queued = service.approve(review.id, (PATH,), lambda _: None)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    if change == "generation":
        with factory.begin() as session:
            workspace = session.scalar(select(AgentWorkspaceRow))
            assert workspace is not None
            workspace.generation += 1
    elif change == "revoked_lease":
        with factory.begin() as session:
            row = session.get(ManagedInstanceMutationLeaseRow, str(fixture.INSTANCE_ID))
            assert row is not None
            session.delete(row)
    elif change == "expired_lease":
        clock.now += timedelta(minutes=11)
    elif change == "destination":
        client.entries[PATH] = (b"replaced", 0o644, 42, 43)
    elif change == "candidate":
        (tmp_path / "source/resources/original.xml").write_text("new candidate")
    elif change == "container":
        client.inspection["Image"] = "sha256:" + "f" * 64
    elif change == "runtime":
        client.inspection["Config"]["Labels"]["io.kodi-lab.runtime-revision-id"] = str(
            uuid4()
        )
    else:
        client.fail_readback_after_apply = True
    # Direct dispatch models a stale broker request after ordinary acquisition.
    with pytest.raises(DevelopmentSourceDeploymentClientError):
        broker.copy_sync_attached_development_source(
            managed_instance_id=str(fixture.INSTANCE_ID),
            development_source_id=str(fixture.SOURCE_ID),
            expected_container_id=fixture.CONTAINER_ID,
            deployment_operation_id=str(lease.operation_id),
        )
    assert bool(client.put_members) == (change == "postcopy_failure")
    if change in {
        "destination",
        "candidate",
        "container",
        "runtime",
        "postcopy_failure",
    }:
        receipt = service.get(review.id)
        assert receipt.dispatched_at is not None
        # A failed first dispatch cannot try again, even with a still-running operation.
        with pytest.raises(DevelopmentSourceDeploymentClientError):
            broker.copy_sync_attached_development_source(
                managed_instance_id=str(fixture.INSTANCE_ID),
                development_source_id=str(fixture.SOURCE_ID),
                expected_container_id=fixture.CONTAINER_ID,
                deployment_operation_id=str(lease.operation_id),
            )


def test_submit_loss_retains_original_queued_receipt(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    service, _coordinator, _broker, _resolver, client, _clock = setup(factory, tmp_path)
    review = service.preview(fixture.INSTANCE_ID, fixture.SOURCE_ID, (PATH,))
    submitted: list[UUID] = []

    def lost_reply(operation_id: UUID) -> None:
        submitted.append(operation_id)
        raise RuntimeError("simulated lost executor reply")

    with pytest.raises(RuntimeError, match="lost executor reply"):
        service.approve(review.id, (PATH,), lost_reply)
    saved = service.get(review.id)
    assert saved is not None and saved.operation_id == submitted[0]
    assert saved.operation is not None and saved.operation.state.value == "queued"
    assert service.approve(review.id, (PATH,), lost_reply) == saved
    assert len(submitted) == 1 and not client.put_members


def test_failed_copy_retains_consumed_review_and_ordinary_recovery_receipt(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    service, coordinator, broker, _resolver, client, _clock = setup(factory, tmp_path)
    review = service.preview(fixture.INSTANCE_ID, fixture.SOURCE_ID, (PATH,))
    saved = service.approve(review.id, (PATH,), lambda _: None)
    lease = coordinator.acquire(saved.operation_id)
    assert lease is not None
    client.fail_readback_after_apply = True
    with pytest.raises(DevelopmentSourceDeploymentError):
        DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    receipt = service.get(review.id)
    assert receipt is not None and receipt.dispatched_at is not None
    assert receipt.operation is not None and receipt.operation.reconciliation_required
    assert receipt.operation.state.value == "reconciliation_required"
    with factory() as session:
        evidence = DevelopmentSourceDeploymentRepository(session).get(
            fixture.INSTANCE_ID, fixture.SOURCE_ID
        )
        assert evidence is not None and evidence.confidence.value == "unknown"
        assert (
            session.get(ManagedInstanceMutationLeaseRow, str(fixture.INSTANCE_ID))
            is None
        )
        assert session.scalar(select(GlobalHeavyOperationLeaseRow)) is None


def test_final_preimage_exclusion_blocks_concurrent_generation_write(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import OperationalError

    service, coordinator, broker, _resolver, client, _clock = setup(factory, tmp_path)
    review = service.preview(fixture.INSTANCE_ID, fixture.SOURCE_ID, (PATH,))
    saved = service.approve(review.id, (PATH,), lambda _: None)
    lease = coordinator.acquire(saved.operation_id)
    assert lease is not None
    put = client.put_archive
    blocked: list[bool] = []

    def concurrent_write(*args: Any) -> bool:
        with factory() as other:
            other.connection().exec_driver_sql("PRAGMA busy_timeout=1")
            with pytest.raises(OperationalError, match="locked"):
                other.connection().exec_driver_sql("BEGIN IMMEDIATE")
            blocked.append(True)
        return bool(put(*args))

    monkeypatch.setattr(client, "put_archive", concurrent_write)
    DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    assert blocked == [True]


def test_generation_change_during_staging_refuses_before_write(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kodi_lab.broker.docker_inventory as inventory

    service, coordinator, broker, _resolver, client, _clock = setup(factory, tmp_path)
    review = service.preview(fixture.INSTANCE_ID, fixture.SOURCE_ID, (PATH,))
    saved = service.approve(review.id, (PATH,), lambda _: None)
    lease = coordinator.acquire(saved.operation_id)
    assert lease is not None
    from kodi_lab.development.deployment import capture_development_tree

    capture = capture_development_tree
    calls = 0

    def change_generation(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        result = capture(*args, **kwargs)
        calls += 1
        if calls == 2:
            with factory.begin() as session:
                workspace = session.scalar(select(AgentWorkspaceRow))
                assert workspace is not None
                workspace.generation += 1
        return result

    monkeypatch.setattr(inventory, "capture_development_tree", change_generation)
    with pytest.raises(DevelopmentSourceDeploymentError):
        DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    receipt = service.get(review.id)
    assert receipt is not None and receipt.dispatched_at is not None
    assert receipt.operation is not None and receipt.operation.state.value == "failed"
    assert not receipt.operation.reconciliation_required and not client.put_members


@pytest.mark.parametrize("identity", ["target", "container"])
def test_other_target_or_container_cannot_use_the_approved_operation(
    factory: sessionmaker[Session], tmp_path: Path, identity: str
) -> None:
    service, coordinator, broker, _resolver, client, _clock = setup(factory, tmp_path)
    review = service.preview(fixture.INSTANCE_ID, fixture.SOURCE_ID, (PATH,))
    saved = service.approve(review.id, (PATH,), lambda _: None)
    lease = coordinator.acquire(saved.operation_id)
    assert lease is not None
    with pytest.raises(DevelopmentSourceDeploymentClientError):
        broker.copy_sync_attached_development_source(
            managed_instance_id=str(
                fixture.SECOND_INSTANCE_ID
                if identity == "target"
                else fixture.INSTANCE_ID
            ),
            development_source_id=str(fixture.SOURCE_ID),
            expected_container_id="f" * 64
            if identity == "container"
            else fixture.CONTAINER_ID,
            deployment_operation_id=str(lease.operation_id),
        )
    receipt = service.get(review.id)
    assert receipt is not None and receipt.dispatched_at is None
    assert not client.put_members


def test_stale_baseline_from_default_refusal_remains_reviewable(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    service, coordinator, broker, _resolver, client, _clock = setup(factory, tmp_path)
    ordinary = coordinator.queue(fixture.INSTANCE_ID, fixture.SOURCE_ID)
    lease = coordinator.acquire(ordinary.operation_id)
    assert lease is not None
    with pytest.raises(DevelopmentSourceDeploymentError):
        DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    with factory() as session:
        evidence = DevelopmentSourceDeploymentRepository(session).get(
            fixture.INSTANCE_ID, fixture.SOURCE_ID
        )
        assert evidence is not None and evidence.confidence.value == "stale"
    review = service.preview(fixture.INSTANCE_ID, fixture.SOURCE_ID, (PATH,))
    assert review.operation_id is None and not client.put_members


def test_review_does_not_broaden_persistent_target_authority(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    from kodi_lab.domain import InstanceType
    from kodi_lab.storage.models import ManagedInstanceRow

    service, _coordinator, _broker, _resolver, client, _clock = setup(factory, tmp_path)
    with factory.begin() as session:
        instance = session.get(ManagedInstanceRow, str(fixture.INSTANCE_ID))
        assert instance is not None
        instance.instance_type = InstanceType.PERSISTENT
    with pytest.raises(DevelopmentSourceDeploymentClientError):
        service.preview(fixture.INSTANCE_ID, fixture.SOURCE_ID, (PATH,))
    assert not client.put_members
