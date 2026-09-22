from __future__ import annotations

import io
import os
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from kodi_lab.broker.docker_inventory import (
    CapturedManagedInstanceSnapshotRecord,
    DockerManagedContainerAdapter,
)
from kodi_lab.broker.errors import (
    ContainerIdentityPreconditionError,
    ContainerMustBeStoppedError,
    DockerSnapshotCaptureError,
    PersistentSnapshotUnsupportedError,
    RequestError,
    RuntimeRevisionPreconditionError,
)
from kodi_lab.broker.operations import (
    CaptureManagedInstanceSnapshotParams,
    CaptureManagedInstanceSnapshotResult,
    build_foundation_registry,
)
from kodi_lab.broker.protocol import Request
from kodi_lab.broker.snapshots import SnapshotCaptureSpec
from kodi_lab.snapshot.storage import SnapshotArtifactIdentity, SnapshotCaptureContext

SNAPSHOT_ID = UUID("92000000-0000-4000-8000-000000000001")
INSTANCE_ID = UUID("92000000-0000-4000-8000-000000000002")
REVISION_ID = UUID("92000000-0000-4000-8000-000000000003")
CONTAINER_ID = "a" * 64


@pytest.fixture(name="tmp_path")
def snapshot_tmp_path(snapshot_data_root: Path) -> Path:
    """Keep snapshot publication data on its reserve-capable scratch filesystem."""

    return snapshot_data_root


def context() -> SnapshotCaptureContext:
    return SnapshotCaptureContext(
        snapshot_id=SNAPSHOT_ID,
        source_managed_instance_id=INSTANCE_ID,
        runtime_revision_id=REVISION_ID,
        kodi_release="21.3",
        captured_fixture_definition_id=None,
        captured_at=datetime(2026, 9, 15, tzinfo=UTC),
        prior_runtime_state="stopped",
    )


def spec(tmp_path: Path) -> SnapshotCaptureSpec:
    return SnapshotCaptureSpec(context(), tmp_path, os.getuid(), os.getgid())


def docker_source_tar() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, member_type, content in (
            (".kodi", tarfile.DIRTYPE, None),
            (".kodi/addons", tarfile.DIRTYPE, None),
            (".kodi/addons/plugin.example", tarfile.DIRTYPE, None),
            (".kodi/addons/plugin.example/state", tarfile.REGTYPE, b"state"),
            (".kodi/userdata", tarfile.DIRTYPE, None),
            (".kodi/userdata/guisettings.xml", tarfile.REGTYPE, b"settings"),
        ):
            info = tarfile.TarInfo(name)
            info.type = member_type
            info.mode = 0o755
            info.size = 0 if content is None else len(content)
            archive.addfile(info, None if content is None else io.BytesIO(content))
    return output.getvalue()


@dataclass
class FakeDocker:
    payload: bytes
    state: str = "exited"
    selected_id: str = CONTAINER_ID
    revision_id: str = str(REVISION_ID)
    count: int = 1

    def containers(
        self, *, all: bool, filters: Mapping[str, list[str]]
    ) -> list[object]:
        assert all is True
        assert filters
        return [{"Id": self.selected_id}] * self.count

    def inspect_container(self, container: str) -> dict[str, object]:
        return {
            "Id": container,
            "Name": f"/kodi-lab-managed-{INSTANCE_ID.hex}",
            "Config": {
                "Labels": {
                    "io.kodi-lab.managed": "true",
                    "io.kodi-lab.instance-id": str(INSTANCE_ID),
                    "io.kodi-lab.runtime-revision-id": self.revision_id,
                }
            },
            "State": {"Status": self.state},
        }

    def get_archive(self, _container: str, path: str) -> tuple[list[bytes], object]:
        assert path == "/home/kodi/.kodi"
        return [self.payload], {}

    def start(self, _container: str) -> None:
        self.state = "running"

    def stop(self, _container: str, *, timeout: int) -> None:
        assert timeout == 10
        self.state = "exited"

    def close(self) -> None:
        return None


def test_adapter_captures_stopped_exact_owned_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "kodi_lab.snapshot.storage.os.chown", lambda *_args, **_kwargs: None
    )
    client = FakeDocker(docker_source_tar())
    result = DockerManagedContainerAdapter(
        lambda: client
    ).capture_managed_instance_snapshot(
        spec=spec(tmp_path), expected_container_id=CONTAINER_ID
    )
    assert result.managed_instance_id == str(INSTANCE_ID)
    assert result.runtime_revision_id == str(REVISION_ID)
    assert result.artifact.snapshot_id == SNAPSHOT_ID
    directory = tmp_path / "snapshots" / str(SNAPSHOT_ID)
    assert {item.name for item in directory.iterdir()} == {"manifest.json", "state.tar"}


def test_adapter_rejects_running_wrong_container_and_wrong_revision(
    tmp_path: Path,
) -> None:
    with pytest.raises(ContainerMustBeStoppedError):
        DockerManagedContainerAdapter(
            lambda: FakeDocker(docker_source_tar(), state="running")
        ).capture_managed_instance_snapshot(
            spec=spec(tmp_path / "running"), expected_container_id=CONTAINER_ID
        )
    with pytest.raises(ContainerIdentityPreconditionError):
        DockerManagedContainerAdapter(
            lambda: FakeDocker(docker_source_tar())
        ).capture_managed_instance_snapshot(
            spec=spec(tmp_path / "identity"), expected_container_id="b" * 64
        )
    with pytest.raises(RuntimeRevisionPreconditionError, match="runtime revision"):
        DockerManagedContainerAdapter(
            lambda: FakeDocker(docker_source_tar(), revision_id=str(uuid4()))
        ).capture_managed_instance_snapshot(
            spec=spec(tmp_path / "revision"), expected_container_id=CONTAINER_ID
        )


def test_adapter_rejects_unsafe_profile_and_existing_final(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "kodi_lab.snapshot.storage.os.chown", lambda *_args, **_kwargs: None
    )
    unsafe = io.BytesIO()
    with tarfile.open(fileobj=unsafe, mode="w") as archive:
        root = tarfile.TarInfo(".kodi")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        link = tarfile.TarInfo(".kodi/userdata/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/escape"
        archive.addfile(link)
    with pytest.raises(DockerSnapshotCaptureError):
        DockerManagedContainerAdapter(
            lambda: FakeDocker(unsafe.getvalue())
        ).capture_managed_instance_snapshot(
            spec=spec(tmp_path / "unsafe"), expected_container_id=CONTAINER_ID
        )
    adapter = DockerManagedContainerAdapter(lambda: FakeDocker(docker_source_tar()))
    adapter.capture_managed_instance_snapshot(
        spec=spec(tmp_path / "collision"), expected_container_id=CONTAINER_ID
    )
    with pytest.raises(DockerSnapshotCaptureError):
        adapter.capture_managed_instance_snapshot(
            spec=spec(tmp_path / "collision"), expected_container_id=CONTAINER_ID
        )


class Resolver:
    def __init__(self, value: SnapshotCaptureSpec | BaseException) -> None:
        self.value = value

    def resolve(self, _snapshot_id: str, _instance_id: str) -> SnapshotCaptureSpec:
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class Provider:
    def capture_managed_instance_snapshot(
        self, *, spec: SnapshotCaptureSpec, expected_container_id: str
    ) -> CapturedManagedInstanceSnapshotRecord:
        return CapturedManagedInstanceSnapshotRecord(
            managed_instance_id=str(INSTANCE_ID),
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            artifact=SnapshotArtifactIdentity(
                snapshot_id=spec.context.snapshot_id,
                archive_digest="sha256:" + "a" * 64,
                manifest_digest="sha256:" + "b" * 64,
                archive_size=10240,
                member_count=2,
            ),
        )


def request(params: dict[str, Any]) -> Request:
    return Request(
        version=1,
        request_id=str(uuid4()),
        operation="capture_managed_instance_snapshot",
        params=params,
    )


def valid_params() -> dict[str, Any]:
    return {
        "snapshot_id": str(SNAPSHOT_ID),
        "managed_instance_id": str(INSTANCE_ID),
        "expected_container_id": CONTAINER_ID,
    }


def test_operation_has_identity_only_schema_and_bounded_result(tmp_path: Path) -> None:
    registry = build_foundation_registry(
        snapshot_capture_resolver=Resolver(spec(tmp_path)),
        snapshot_capture_provider=Provider(),
    )
    result = registry.dispatch(request(valid_params()))
    assert isinstance(result, CaptureManagedInstanceSnapshotResult)
    assert set(CaptureManagedInstanceSnapshotParams.model_fields) == {
        "snapshot_id",
        "managed_instance_id",
        "expected_container_id",
    }
    assert not {
        "path",
        "destination",
        "archive",
        "members",
        "container_path",
        "tar_flags",
    }.intersection(result.model_dump())
    for injected in ("path", "archive", "destination", "runtime_revision_id"):
        with pytest.raises(RequestError):
            registry.dispatch(request({**valid_params(), injected: "attacker"}))


def test_operation_maps_persistent_capture_to_typed_failure(tmp_path: Path) -> None:
    registry = build_foundation_registry(
        snapshot_capture_resolver=Resolver(
            PersistentSnapshotUnsupportedError("unsupported")
        ),
        snapshot_capture_provider=Provider(),
    )
    with pytest.raises(RequestError) as captured:
        registry.dispatch(request(valid_params()))
    assert captured.value.code == "persistent_snapshot_unsupported"
