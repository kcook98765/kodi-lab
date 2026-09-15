import io
import tarfile
from dataclasses import replace
from uuid import UUID

import pytest
from docker.errors import NotFound  # type: ignore[import-untyped]

from kodi_lab.broker.docker_inventory import (
    AppliedAssignedFixtureRecord,
    ContainerRuntimeState,
    DockerManagedContainerAdapter,
)
from kodi_lab.broker.errors import (
    AmbiguousManagedContainerError,
    ContainerIdentityPreconditionError,
    ContainerMustBeStoppedError,
    DockerFixtureArchiveError,
    FixtureBaselineConflictError,
    InvalidManagedContainerMetadataError,
    ManagedContainerNotFoundError,
    RuntimeRevisionPreconditionError,
)
from kodi_lab.broker.fixtures import AssignedFixtureSpec
from kodi_lab.fixtures.package import load_packaged_fixture

INSTANCE_ID = str(UUID(int=1))
REVISION_ID = str(UUID(int=2))
CONTAINER_ID = "1" * 64


def spec() -> AssignedFixtureSpec:
    packaged = load_packaged_fixture("small-mixed-v1")
    return AssignedFixtureSpec(
        managed_instance_id=INSTANCE_ID,
        runtime_revision_id=REVISION_ID,
        fixture_id=str(packaged.definition.id),
        fixture_slug=packaged.definition.slug,
        fixture_version=packaged.definition.fixture_version,
        content_digest=packaged.definition.content_digest,
        destination=packaged.destination,
        archive=packaged.build_archive(),
        members=packaged.members,
    )


def inspect_data(
    *,
    state: str = "exited",
    container_id: str = CONTAINER_ID,
    revision_id: str = REVISION_ID,
    managed: str = "true",
    instance_id: str = INSTANCE_ID,
) -> dict[str, object]:
    return {
        "Id": container_id,
        "Name": f"/kodi-lab-managed-{INSTANCE_ID.replace('-', '')}",
        "Config": {
            "Labels": {
                "io.kodi-lab.managed": managed,
                "io.kodi-lab.instance-id": instance_id,
                "io.kodi-lab.runtime-revision-id": revision_id,
            }
        },
        "State": {"Status": state},
    }


class FakeFixtureDockerClient:
    def __init__(
        self,
        *,
        summaries: list[object] | None = None,
        inspection: object | None = None,
        preexisting: bool = False,
    ) -> None:
        self.summaries: list[object] = (
            [{"Id": CONTAINER_ID}] if summaries is None else summaries
        )
        self.inspection = inspect_data() if inspection is None else inspection
        self.preexisting = preexisting
        self.applied = False
        self.put_calls: list[tuple[str, str, bytes]] = []
        self.closed = False

    def containers(self, **_kwargs: object) -> list[object]:
        return self.summaries

    def inspect_container(self, _container: str) -> object:
        return self.inspection

    def get_archive(self, _container: str, _path: str) -> tuple[object, object]:
        if not self.preexisting and not self.applied:
            raise NotFound("absent")
        return io.BytesIO(b"ignored"), {}

    def put_archive(self, container: str, path: str, data: bytes) -> bool:
        self.put_calls.append((container, path, data))
        self.applied = True
        return True

    def close(self) -> None:
        self.closed = True

    def start(self, _container: str) -> None:
        raise AssertionError("fixture apply reached start")

    def stop(self, _container: str, *, timeout: int) -> None:
        raise AssertionError(f"fixture apply reached stop {timeout}")


def apply(client: FakeFixtureDockerClient) -> AppliedAssignedFixtureRecord:
    return DockerManagedContainerAdapter(lambda: client).apply_assigned_fixture(
        spec=spec(), expected_container_id=CONTAINER_ID
    )


def test_stopped_owned_container_receives_only_fixed_safe_archive() -> None:
    client = FakeFixtureDockerClient()
    result = apply(client)
    assert result.runtime_state is ContainerRuntimeState.STOPPED
    assert result.fixture_slug == "small-mixed-v1"
    assert len(client.put_calls) == 1
    container, destination, archive_bytes = client.put_calls[0]
    assert container == CONTAINER_ID
    assert destination == "/home/kodi/.kodi"
    with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
        members = archive.getmembers()
    assert all(member.uid == 1000 and member.gid == 1000 for member in members)
    assert all(not member.issym() and not member.islnk() for member in members)
    assert client.closed


def test_running_container_and_preexisting_fixture_state_fail_closed() -> None:
    with pytest.raises(ContainerMustBeStoppedError):
        apply(FakeFixtureDockerClient(inspection=inspect_data(state="running")))
    with pytest.raises(FixtureBaselineConflictError):
        apply(FakeFixtureDockerClient(preexisting=True))


def test_adapter_rejects_noncanonical_destination_or_archive_before_docker() -> None:
    client = FakeFixtureDockerClient()
    adapter = DockerManagedContainerAdapter(lambda: client)
    with pytest.raises(DockerFixtureArchiveError):
        adapter.apply_assigned_fixture(
            spec=replace(spec(), destination="/tmp/caller-selected"),
            expected_container_id=CONTAINER_ID,
        )
    with pytest.raises(DockerFixtureArchiveError):
        adapter.apply_assigned_fixture(
            spec=replace(spec(), archive=b"not-the-trusted-archive"),
            expected_container_id=CONTAINER_ID,
        )
    assert client.put_calls == []


def test_missing_ambiguous_wrong_container_revision_and_ownership_fail_closed() -> None:
    with pytest.raises(ManagedContainerNotFoundError):
        apply(FakeFixtureDockerClient(summaries=[]))
    with pytest.raises(AmbiguousManagedContainerError):
        apply(
            FakeFixtureDockerClient(summaries=[{"Id": CONTAINER_ID}, {"Id": "2" * 64}])
        )
    with pytest.raises(ContainerIdentityPreconditionError):
        DockerManagedContainerAdapter(
            lambda: FakeFixtureDockerClient()
        ).apply_assigned_fixture(spec=spec(), expected_container_id="2" * 64)
    with pytest.raises(RuntimeRevisionPreconditionError):
        apply(
            FakeFixtureDockerClient(
                inspection=inspect_data(revision_id=str(UUID(int=9)))
            )
        )
    with pytest.raises(InvalidManagedContainerMetadataError):
        apply(FakeFixtureDockerClient(inspection=inspect_data(managed="false")))
