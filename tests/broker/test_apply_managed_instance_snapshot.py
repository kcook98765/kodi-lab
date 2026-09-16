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
    AppliedManagedInstanceSnapshotRecord,
    DockerManagedContainerAdapter,
)
from kodi_lab.broker.errors import DockerSnapshotRestoreError, RequestError
from kodi_lab.broker.operations import (
    ApplyManagedInstanceSnapshotParams,
    ApplyManagedInstanceSnapshotResult,
    build_foundation_registry,
)
from kodi_lab.broker.protocol import Request
from kodi_lab.broker.snapshots import SnapshotCaptureSpec, SnapshotRestoreSpec
from kodi_lab.snapshot.storage import (
    SnapshotArtifactIdentity,
    SnapshotCaptureContext,
    snapshot_directory,
)
from kodi_lab.snapshot.validation import validate_published_snapshot

SNAPSHOT_ID = UUID("93200000-0000-4000-8000-000000000001")
INSTANCE_ID = UUID("93200000-0000-4000-8000-000000000002")
REVISION_ID = UUID("93200000-0000-4000-8000-000000000003")
CONTAINER_ID = "a" * 64


def context() -> SnapshotCaptureContext:
    return SnapshotCaptureContext(
        snapshot_id=SNAPSHOT_ID,
        source_managed_instance_id=INSTANCE_ID,
        runtime_revision_id=REVISION_ID,
        kodi_release="21.3",
        captured_fixture_definition_id=None,
        captured_at=datetime(2026, 9, 16, tzinfo=UTC),
        prior_runtime_state="stopped",
    )


def capture_spec(tmp_path: Path) -> SnapshotCaptureSpec:
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


def fresh_source_tar(*, extra: str | None = None) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, member_type, content in (
            (".kodi", tarfile.DIRTYPE, None),
            (".kodi/userdata", tarfile.DIRTYPE, None),
            (".kodi/userdata/guisettings.xml", tarfile.REGTYPE, b"settings"),
        ):
            info = tarfile.TarInfo(name)
            info.type = member_type
            info.mode = 0o755
            info.size = 0 if content is None else len(content)
            archive.addfile(info, None if content is None else io.BytesIO(content))
        if extra is not None:
            info = tarfile.TarInfo(extra)
            info.type = tarfile.REGTYPE
            info.mode = 0o600
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    return output.getvalue()


@dataclass
class RestoreDocker:
    payload: bytes
    restored_payload: bytes
    state: str = "exited"

    def containers(
        self, *, all: bool, filters: Mapping[str, list[str]]
    ) -> list[object]:
        assert all and filters
        return [{"Id": CONTAINER_ID}]

    def inspect_container(self, container: str) -> dict[str, object]:
        return {
            "Id": container,
            "Name": f"/kodi-lab-managed-{INSTANCE_ID.hex}",
            "Config": {
                "Labels": {
                    "io.kodi-lab.managed": "true",
                    "io.kodi-lab.instance-id": str(INSTANCE_ID),
                    "io.kodi-lab.runtime-revision-id": str(REVISION_ID),
                }
            },
            "State": {"Status": self.state},
        }

    def get_archive(self, _container: str, path: str) -> tuple[list[bytes], object]:
        assert path == "/home/kodi/.kodi"
        return [self.payload], {}

    def put_archive(self, _container: str, path: str, data: object) -> bool:
        assert path == "/home/kodi/.kodi"
        assert hasattr(data, "read")
        assert data.read()
        self.payload = self.restored_payload
        return True

    def start(self, _container: str) -> None:
        self.state = "running"

    def stop(self, _container: str, *, timeout: int) -> None:
        assert timeout == 10
        self.state = "exited"

    def close(self) -> None:
        return None


def published_restore_spec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> SnapshotRestoreSpec:
    monkeypatch.setattr(
        "kodi_lab.snapshot.storage.os.chown", lambda *_args, **_kwargs: None
    )
    from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter

    class CaptureDocker(RestoreDocker):
        def put_archive(self, _container: str, path: str, data: object) -> bool:
            raise AssertionError(path)

    source = docker_source_tar()
    client = CaptureDocker(source, source)
    captured = DockerManagedContainerAdapter(
        lambda: client
    ).capture_managed_instance_snapshot(
        spec=capture_spec(tmp_path), expected_container_id=CONTAINER_ID
    )
    return SnapshotRestoreSpec(
        context=context(),
        artifact=captured.artifact,
        data_root=tmp_path,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    )


def test_adapter_applies_to_exact_stopped_fresh_container_and_verifies_readback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    restore_spec = published_restore_spec(monkeypatch, tmp_path)
    client = RestoreDocker(fresh_source_tar(), docker_source_tar())
    result = DockerManagedContainerAdapter(
        lambda: client
    ).apply_managed_instance_snapshot(
        spec=restore_spec, expected_container_id=CONTAINER_ID
    )
    assert result.artifact == restore_spec.artifact
    assert result.container_id == CONTAINER_ID
    assert result.recognized_databases_checked == 0


def test_adapter_rejects_unknown_fresh_state_before_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    restore_spec = published_restore_spec(monkeypatch, tmp_path)
    client = RestoreDocker(
        fresh_source_tar(extra=".kodi/userdata/unexpected"), docker_source_tar()
    )
    with pytest.raises(DockerSnapshotRestoreError, match="Fresh profile"):
        DockerManagedContainerAdapter(lambda: client).apply_managed_instance_snapshot(
            spec=restore_spec, expected_container_id=CONTAINER_ID
        )
    assert client.payload != docker_source_tar()


def test_adapter_revalidates_tampered_artifact_before_docker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    restore_spec = published_restore_spec(monkeypatch, tmp_path)
    archive = snapshot_directory(tmp_path, SNAPSHOT_ID) / "state.tar"
    archive.chmod(0o640)
    with pytest.raises(DockerSnapshotRestoreError):
        DockerManagedContainerAdapter(
            lambda: RestoreDocker(fresh_source_tar(), docker_source_tar())
        ).apply_managed_instance_snapshot(
            spec=restore_spec, expected_container_id=CONTAINER_ID
        )


class Resolver:
    def __init__(self, value: SnapshotRestoreSpec) -> None:
        self.value = value

    def resolve(self, _snapshot_id: str, _instance_id: str) -> SnapshotRestoreSpec:
        return self.value


class Provider:
    def apply_managed_instance_snapshot(
        self, *, spec: SnapshotRestoreSpec, expected_container_id: str
    ) -> AppliedManagedInstanceSnapshotRecord:
        return AppliedManagedInstanceSnapshotRecord(
            managed_instance_id=str(INSTANCE_ID),
            runtime_revision_id=str(REVISION_ID),
            container_id=expected_container_id,
            artifact=spec.artifact,
            recognized_databases_checked=1,
        )


def request(params: dict[str, Any]) -> Request:
    return Request(
        version=1,
        request_id=str(uuid4()),
        operation="apply_managed_instance_snapshot",
        params=params,
    )


def test_operation_schema_has_only_three_trusted_identities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    restore_spec = published_restore_spec(monkeypatch, tmp_path)
    registry = build_foundation_registry(
        snapshot_restore_resolver=Resolver(restore_spec),
        snapshot_restore_provider=Provider(),
    )
    params = {
        "snapshot_id": str(SNAPSHOT_ID),
        "managed_instance_id": str(INSTANCE_ID),
        "expected_container_id": CONTAINER_ID,
    }
    result = registry.dispatch(request(params))
    assert isinstance(result, ApplyManagedInstanceSnapshotResult)
    assert set(ApplyManagedInstanceSnapshotParams.model_fields) == set(params)
    for injected in ("path", "destination", "archive", "members", "tar_flags"):
        with pytest.raises(RequestError):
            registry.dispatch(request({**params, injected: "attacker"}))


def test_central_validator_identity_remains_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    restore_spec = published_restore_spec(monkeypatch, tmp_path)
    before = validate_published_snapshot(
        data_root=tmp_path,
        context=restore_spec.context,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
    ).artifact
    assert isinstance(before, SnapshotArtifactIdentity)
    assert before == restore_spec.artifact
