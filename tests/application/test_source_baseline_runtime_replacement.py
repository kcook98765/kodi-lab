"""Historical receipts cannot authorize overwrites on a replacement runtime."""

from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.development_source_deployments import (
    DevelopmentSourceDeploymentError,
    DevelopmentSourceDeploymentLeaseLostError,
    DevelopmentSourceDeploymentOperationCoordinator,
    DevelopmentSourceDeploymentService,
)
from kodi_lab.application.session_source_copy import SessionSourceCopyService
from kodi_lab.broker.development_sources import (
    DevelopmentSourceDeploymentStateError,
    SqliteDevelopmentSourceDeploymentResolver,
)
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.errors import (
    DevelopmentSourceDeploymentClientError,
    DockerDevelopmentSourceDeploymentError,
)
from kodi_lab.broker.operations import (
    CopySyncAttachedDevelopmentSourceResult,
    ManagedContainerInventoryResult,
)
from kodi_lab.domain import DevelopmentSourceDeploymentConfidence
from kodi_lab.storage.models import (
    DevelopmentSessionRow,
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceRow,
    RuntimeExecutionProfileRow,
    RuntimeRevisionRow,
)
from kodi_lab.storage.repositories import DevelopmentSourceDeploymentRepository
from tests.application import test_development_source_deployments as fixtures
from tests.application.test_development_source_deployments import factory as factory
from tests.application.test_session_source_copy import bind
from tests.broker.test_development_source_deployment import FakeDeploymentDockerClient

NEW_REVISION = UUID(int=9001)
NEW_CONTAINER = "c" * 64


class CopyDockerClient(FakeDeploymentDockerClient):
    def __init__(self, revision: UUID, container: str) -> None:
        super().__init__()
        self.inspection.update(
            Id=container,
            Name=f"/kodi-lab-managed-{fixtures.INSTANCE_ID.hex}",
            Config={
                "Labels": {
                    "io.kodi-lab.managed": "true",
                    "io.kodi-lab.instance-id": str(fixtures.INSTANCE_ID),
                    "io.kodi-lab.runtime-revision-id": str(revision),
                }
            },
        )

    def containers(self, **_kwargs: object) -> list[object]:
        return [{"Id": self.inspection["Id"]}]


class CopyBroker(fixtures.FakeBroker):
    """Use the real resolver and Docker copy path behind the service boundary."""

    def __init__(self, factory: sessionmaker[Session], root: Path) -> None:
        super().__init__()
        self.resolver = SqliteDevelopmentSourceDeploymentResolver(
            factory, development_root=root, data_root=root / "data"
        )
        self.client = CopyDockerClient(fixtures.REVISION_ID, fixtures.CONTAINER_ID)

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        original = super().managed_container_inventory()
        return original.model_copy(
            update={
                "containers": [
                    original.containers[0].model_copy(
                        update={"container_id": self.client.inspection["Id"]}
                    )
                ]
            }
        )

    def copy_sync_attached_development_source(
        self,
        *,
        managed_instance_id: str,
        development_source_id: str,
        expected_container_id: str,
        deployment_operation_id: str,
    ) -> CopySyncAttachedDevelopmentSourceResult:
        self.calls.append(
            (
                managed_instance_id,
                development_source_id,
                expected_container_id,
                deployment_operation_id,
            )
        )
        try:
            spec = self.resolver.resolve(
                managed_instance_id,
                development_source_id,
                deployment_operation_id,
                expected_container_id,
            )
            result = DockerManagedContainerAdapter(
                lambda: self.client
            ).copy_sync_attached_development_source(
                spec=spec, expected_container_id=expected_container_id
            )
        except (
            DevelopmentSourceDeploymentStateError,
            DockerDevelopmentSourceDeploymentError,
        ) as error:
            raise DevelopmentSourceDeploymentClientError(
                error.code, str(error), request_id=deployment_operation_id
            ) from error
        return CopySyncAttachedDevelopmentSourceResult.model_validate(asdict(result))


def prepare(
    factory: sessionmaker[Session], tmp_path: Path, *, replace_runtime: bool = True
) -> tuple[
    CopyBroker,
    DevelopmentSourceDeploymentOperationCoordinator,
    SessionSourceCopyService,
    UUID,
]:
    with factory.begin() as session:
        profile = session.get(RuntimeExecutionProfileRow, str(fixtures.REVISION_ID))
        assert profile is not None
        profile.runtime_uid, profile.runtime_gid = 42, 43
    broker = CopyBroker(factory, tmp_path)
    coordinator, historical_id = fixtures._successful_deployment(
        factory, tmp_path, broker, fixtures.Clock()
    )
    if replace_runtime:
        with factory.begin() as session:
            revision = session.get(RuntimeRevisionRow, str(fixtures.REVISION_ID))
            profile = session.get(RuntimeExecutionProfileRow, str(fixtures.REVISION_ID))
            assert revision is not None and profile is not None
            session.add(
                RuntimeRevisionRow(
                    **{
                        **{
                            c.name: getattr(revision, c.name)
                            for c in revision.__table__.columns
                        },
                        "id": str(NEW_REVISION),
                        "revision_number": 2,
                    }
                )
            )
            session.flush()
            session.add(
                RuntimeExecutionProfileRow(
                    **{
                        **{
                            c.name: getattr(profile, c.name)
                            for c in profile.__table__.columns
                        },
                        "runtime_revision_id": str(NEW_REVISION),
                    }
                )
            )
            instance = session.get(ManagedInstanceRow, str(fixtures.INSTANCE_ID))
            assert instance is not None
            instance.runtime_revision_id = str(NEW_REVISION)
            DevelopmentSourceDeploymentRepository(session).mark_instance_confidence(
                fixtures.INSTANCE_ID, DevelopmentSourceDeploymentConfidence.STALE
            )
        broker.client = CopyDockerClient(NEW_REVISION, NEW_CONTAINER)
    bind(factory)
    if replace_runtime:
        with factory.begin() as session:
            selected = session.get(DevelopmentSessionRow, 1)
            assert selected is not None
            selected.runtime_revision_id = str(NEW_REVISION)
            selected.container_id = NEW_CONTAINER
    return (
        broker,
        coordinator,
        SessionSourceCopyService(factory, coordinator),
        historical_id,
    )


@pytest.mark.parametrize(
    "replace_runtime,detached_receipt", [(False, False), (True, False), (True, True)]
)
def test_supported_copy_commits_exact_provenance_and_preserves_history(
    factory: sessionmaker[Session],
    tmp_path: Path,
    replace_runtime: bool,
    detached_receipt: bool,
) -> None:
    broker, coordinator, service, historical_id = prepare(
        factory, tmp_path, replace_runtime=replace_runtime
    )
    with factory.begin() as session:
        historical = session.get(
            DevelopmentSourceDeploymentOperationRow, str(historical_id)
        )
        assert historical is not None
        if detached_receipt:
            historical.managed_instance_id = None
        before = {
            c.name: getattr(historical, c.name) for c in historical.__table__.columns
        }
    receipt = service.admit(request_id=uuid4(), expected_generation=1)
    assert (
        service.admit(request_id=receipt.request_id, expected_generation=1) == receipt
    )
    lease = coordinator.acquire(receipt.operation_id)
    assert lease is not None
    result = DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    assert result.result_classification == (
        "initial_deployment" if replace_runtime else "unchanged"
    )
    assert len(broker.client.put_members) == 1
    with pytest.raises(DevelopmentSourceDeploymentLeaseLostError):
        coordinator.acquire(receipt.operation_id)
    replay = service.request_copy(
        request_id=receipt.request_id,
        expected_generation=1,
        submit=lambda _operation: pytest.fail("terminal request was resubmitted"),
    )
    assert replay.submission == "not_requested" and len(broker.calls) == 2
    with factory() as session:
        evidence = DevelopmentSourceDeploymentRepository(session).get(
            fixtures.INSTANCE_ID, fixtures.SOURCE_ID
        )
        historical = session.get(
            DevelopmentSourceDeploymentOperationRow, str(historical_id)
        )
        assert historical is not None
        assert {
            c.name: getattr(historical, c.name) for c in historical.__table__.columns
        } == before
    assert evidence is not None
    assert evidence.last_successful_operation_id == receipt.operation_id
    assert evidence.runtime_revision_id == (
        NEW_REVISION if replace_runtime else fixtures.REVISION_ID
    )
    assert evidence.trusted_container_id == (
        NEW_CONTAINER if replace_runtime else fixtures.CONTAINER_ID
    )
    assert evidence.source_manifest_digest == result.source_manifest_digest
    assert evidence.verified_destination_digest == broker.client.current_digest()
    assert (
        evidence.confidence
        is DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
    )


@pytest.mark.parametrize("confidence", ["stale", "verified_at_last_operation"])
def test_historical_receipt_cannot_authorize_existing_destination(
    factory: sessionmaker[Session], tmp_path: Path, confidence: str
) -> None:
    broker, coordinator, service, _ = prepare(factory, tmp_path)
    with factory.begin() as session:
        baseline = session.get(
            DevelopmentSourceDeploymentRow,
            (str(fixtures.INSTANCE_ID), str(fixtures.SOURCE_ID)),
        )
        assert baseline is not None
        baseline.confidence = DevelopmentSourceDeploymentConfidence(confidence)
    # Even exact historical bytes in the new container cannot authorize a copy.
    broker.client.entries = {
        "addon.xml": ((tmp_path / "source/addon.xml").read_bytes(), 0o644, 42, 43)
    }
    with pytest.raises(DevelopmentSourceDeploymentStateError):
        broker.resolver.resolve_destination_verification(
            str(fixtures.INSTANCE_ID), str(fixtures.SOURCE_ID), NEW_CONTAINER
        )
    receipt = service.admit(request_id=uuid4(), expected_generation=1)
    lease = coordinator.acquire(receipt.operation_id)
    assert lease is not None
    with pytest.raises(DevelopmentSourceDeploymentError, match="replacement_conflict"):
        DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    assert not broker.client.put_members


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_revision_id", str(NEW_REVISION)),
        ("expected_container_id", NEW_CONTAINER),
        ("original_managed_instance_id", str(fixtures.SECOND_INSTANCE_ID)),
        ("managed_instance_id", str(fixtures.SECOND_INSTANCE_ID)),
        ("addon_id", "plugin.video.other"),
        ("state", "failed"),
        ("result_classification", None),
    ],
)
def test_contradictory_historical_receipt_refuses_even_absent_destination(
    factory: sessionmaker[Session], tmp_path: Path, field: str, value: str | None
) -> None:
    broker, coordinator, service, historical_id = prepare(factory, tmp_path)
    with factory.begin() as session:
        historical = session.get(
            DevelopmentSourceDeploymentOperationRow, str(historical_id)
        )
        assert historical is not None
        setattr(historical, field, value)
    receipt = service.admit(request_id=uuid4(), expected_generation=1)
    lease = coordinator.acquire(receipt.operation_id)
    assert lease is not None
    with pytest.raises(DevelopmentSourceDeploymentError, match="state_invalid"):
        DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    assert not broker.client.put_members
    with factory() as session:
        evidence = session.get(
            DevelopmentSourceDeploymentRow,
            (str(fixtures.INSTANCE_ID), str(fixtures.SOURCE_ID)),
        )
        assert evidence is not None and evidence.last_successful_operation_id == str(
            historical_id
        )


@pytest.mark.parametrize("boundary", ["source", "attachment", "session", "ownership"])
def test_historical_baseline_does_not_relax_live_dispatch_requirements(
    factory: sessionmaker[Session], tmp_path: Path, boundary: str
) -> None:
    broker, coordinator, service, _ = prepare(factory, tmp_path)
    receipt = service.admit(request_id=uuid4(), expected_generation=1)
    lease = coordinator.acquire(receipt.operation_id)
    assert lease is not None
    with factory.begin() as session:
        if boundary == "source":
            (tmp_path / "source/addon.xml").unlink()
        elif boundary == "attachment":
            attachment = session.get(
                ManagedInstanceDevelopmentSourceRow,
                (str(fixtures.INSTANCE_ID), str(fixtures.SOURCE_ID)),
            )
            assert attachment is not None
            session.delete(attachment)
        elif boundary == "session":
            selected = session.get(DevelopmentSessionRow, 1)
            assert selected is not None
            selected.phase = "closed"
        else:
            operation = session.get(
                DevelopmentSourceDeploymentOperationRow, str(receipt.operation_id)
            )
            assert operation is not None
            operation.expected_container_id = "e" * 64
    with pytest.raises(DevelopmentSourceDeploymentError):
        DevelopmentSourceDeploymentService(broker, coordinator, lease).deploy()
    assert not broker.client.put_members
