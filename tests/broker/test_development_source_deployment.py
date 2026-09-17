from __future__ import annotations

import io
import tarfile
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, cast
from uuid import UUID

import pytest
from docker.errors import NotFound  # type: ignore[import-untyped]

import kodi_lab.broker.docker_inventory as inventory
from kodi_lab.broker.development_sources import (
    DevelopmentSourceDeploymentSpec,
    PriorDevelopmentSourceDeploymentEvidence,
)
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.errors import (
    ContainerMustBeStoppedError,
    DockerDevelopmentSourceDeploymentError,
)
from kodi_lab.development.deployment import capture_destination_archive
from kodi_lab.domain import DevelopmentSourceDeploymentConfidence

INSTANCE_ID = str(UUID(int=1))
SOURCE_ID = str(UUID(int=2))
OPERATION_ID = str(UUID(int=3))
REVISION_ID = str(UUID(int=4))
CONTAINER_ID = "1" * 64
ADDON_ID = "plugin.video.example"


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "development"
    addon = root / "addon"
    if addon.exists():
        return root
    (addon / "resources").mkdir(parents=True)
    files = {
        addon / "addon.xml": (
            f'<addon id="{ADDON_ID}" name="Example" version="1.0.0">'
            '<extension point="xbmc.python.pluginsource"/></addon>'
        ),
        addon / "unchanged.py": "unchanged",
        addon / "later_destination_only.py": "preserved from deployment",
        addon / "resources" / "changed.py": "before",
    }
    for path, content in files.items():
        path.write_text(content)
    return root


def _spec(
    tmp_path: Path,
    *,
    prior: PriorDevelopmentSourceDeploymentEvidence | None = None,
    operation_id: str = OPERATION_ID,
    runtime_uid: int = 42,
    runtime_gid: int = 43,
) -> DevelopmentSourceDeploymentSpec:
    return DevelopmentSourceDeploymentSpec(
        managed_instance_id=INSTANCE_ID,
        development_source_id=SOURCE_ID,
        deployment_operation_id=operation_id,
        runtime_revision_id=REVISION_ID,
        runtime_uid=runtime_uid,
        runtime_gid=runtime_gid,
        development_root=_source(tmp_path),
        relative_locator="addon",
        addon_id=ADDON_ID,
        observed_name="Example",
        observed_version="1.0.0",
        staging_root=tmp_path / "private" / "development-source-staging",
        prior_evidence=prior,
    )


def _inspection(*, state: str = "exited") -> dict[str, object]:
    return {
        "Id": CONTAINER_ID,
        "Name": f"/kodi-lab-managed-{INSTANCE_ID.replace('-', '')}",
        "Config": {
            "Labels": {
                "io.kodi-lab.managed": "true",
                "io.kodi-lab.instance-id": INSTANCE_ID,
                "io.kodi-lab.runtime-revision-id": REVISION_ID,
            }
        },
        "State": {"Status": state},
    }


class FakeDeploymentDockerClient:
    def __init__(self, *, state: str = "exited") -> None:
        self.inspection = _inspection(state=state)
        self.entries: dict[str, tuple[bytes | None, int, int, int]] = {}
        self.put_members: list[tuple[str, ...]] = []
        self.fail_readback_after_apply = False
        self.fail_final_inspect = False
        self.applied = False
        self.closed = False

    def containers(self, **_kwargs: object) -> list[object]:
        return [{"Id": CONTAINER_ID}]

    def inspect_container(self, _container: str) -> object:
        if self.fail_final_inspect and self.applied:
            raise ValueError("controlled unexpected post-apply failure")
        return self.inspection

    def get_archive(self, _container: str, path: str) -> tuple[object, object]:
        if self.fail_readback_after_apply and self.applied:
            raise OSError("controlled interruption")
        prefix = f"/home/kodi/.kodi/addons/{ADDON_ID}"
        if path != prefix or not self.entries:
            raise NotFound("absent")
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            root = tarfile.TarInfo(ADDON_ID)
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            root.uid = 42
            root.gid = 43
            archive.addfile(root)
            for name, (content, mode, uid, gid) in sorted(self.entries.items()):
                info = tarfile.TarInfo(f"{ADDON_ID}/{name}")
                info.mode = mode
                info.uid = uid
                info.gid = gid
                if content is None:
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                else:
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
        return [stream.getvalue()], {}

    def put_archive(self, _container: str, path: str, data: BinaryIO) -> bool:
        payload = data.read()
        names: list[str] = []
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            for member in archive:
                names.append(member.name)
                parts = member.name.split("/")
                if path == "/home/kodi":
                    assert parts.pop(0) == ".kodi"
                    assert parts.pop(0) == "addons"
                    assert parts.pop(0) == ADDON_ID
                relative = "/".join(parts)
                if not relative:
                    continue
                if member.isdir():
                    self.entries[relative] = (
                        None,
                        member.mode,
                        member.uid,
                        member.gid,
                    )
                else:
                    stream = archive.extractfile(member)
                    assert stream is not None
                    self.entries[relative] = (
                        stream.read(),
                        member.mode,
                        member.uid,
                        member.gid,
                    )
        self.put_members.append(tuple(names))
        self.applied = True
        return True

    def close(self) -> None:
        self.closed = True

    def start(self, _container: str) -> None:
        raise AssertionError("deployment reached start")

    def stop(self, _container: str, *, timeout: int) -> None:
        raise AssertionError(f"deployment reached stop {timeout}")

    def current_digest(self) -> str:
        stream, _ = self.get_archive(
            CONTAINER_ID, f"/home/kodi/.kodi/addons/{ADDON_ID}"
        )
        return capture_destination_archive(
            cast(Iterable[bytes], stream),
            root_name=ADDON_ID,
        ).digest


def _apply(
    client: FakeDeploymentDockerClient, spec: DevelopmentSourceDeploymentSpec
) -> inventory.AppliedDevelopmentSourceDeploymentRecord:
    return DockerManagedContainerAdapter(
        lambda: client
    ).copy_sync_attached_development_source(
        spec=spec, expected_container_id=CONTAINER_ID
    )


def _prior(
    result: inventory.AppliedDevelopmentSourceDeploymentRecord,
) -> PriorDevelopmentSourceDeploymentEvidence:
    return PriorDevelopmentSourceDeploymentEvidence(
        source_manifest_digest=result.source_manifest_digest,
        verified_destination_digest=result.verified_destination_digest,
        runtime_revision_id=REVISION_ID,
        trusted_container_id=CONTAINER_ID,
        confidence=DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION,
    )


def test_initial_deployment_uses_runtime_ownership_and_stays_stopped(
    tmp_path: Path,
) -> None:
    client = FakeDeploymentDockerClient()
    result = _apply(client, _spec(tmp_path, runtime_uid=42, runtime_gid=43))
    assert result.result_classification == "initial_deployment"
    assert result.new_count == 5
    assert result.changed_count == 0
    assert result.unchanged_count == 0
    assert len(client.put_members) == 1
    assert all(entry[2:] == (42, 43) for entry in client.entries.values())
    assert client.inspection["State"] == {"Status": "exited"}
    assert client.closed


def test_sync_transfers_only_changed_and_new_and_preserves_destination_only(
    tmp_path: Path,
) -> None:
    client = FakeDeploymentDockerClient()
    first = _apply(client, _spec(tmp_path))
    source = tmp_path / "development" / "addon"
    (source / "resources" / "changed.py").write_text("after")
    (source / "new.py").write_text("new")
    (source / "later_destination_only.py").unlink()
    preserved = client.entries["later_destination_only.py"]
    retained_before = first.verified_destination_digest
    assert client.current_digest() == retained_before
    client.applied = False
    second = _apply(
        client,
        _spec(tmp_path, prior=_prior(first), operation_id=str(UUID(int=5))),
    )
    assert second.result_classification == "synchronized"
    assert second.new_count == 1
    assert second.changed_count == 1
    assert second.unchanged_count == 3
    assert second.destination_only_count == 1
    assert client.put_members[-1] == ("new.py", "resources/changed.py")
    assert client.entries["later_destination_only.py"] == preserved
    assert "later_destination_only.py" not in client.put_members[-1]
    assert second.source_manifest_digest != second.verified_destination_digest
    assert client.current_digest() == second.verified_destination_digest

    puts = len(client.put_members)
    client.applied = False
    third = _apply(
        client,
        _spec(tmp_path, prior=_prior(second), operation_id=str(UUID(int=8))),
    )
    assert third.result_classification == "unchanged"
    assert third.new_count == 0
    assert third.changed_count == 0
    assert third.destination_only_count == 1
    assert third.transferred_bytes == 0
    assert len(client.put_members) == puts
    assert client.entries["later_destination_only.py"] == preserved
    assert third.verified_destination_digest == second.verified_destination_digest


def test_unchanged_sync_transfers_zero_files_and_bytes(tmp_path: Path) -> None:
    client = FakeDeploymentDockerClient()
    first = _apply(client, _spec(tmp_path))
    puts = len(client.put_members)
    second = _apply(
        client,
        _spec(tmp_path, prior=_prior(first), operation_id=str(UUID(int=6))),
    )
    assert second.result_classification == "unchanged"
    assert second.new_count == 0
    assert second.changed_count == 0
    assert second.transferred_bytes == 0
    assert len(client.put_members) == puts


def test_packaged_pyc_source_is_copied_without_exclusion(tmp_path: Path) -> None:
    client = FakeDeploymentDockerClient()
    first = _apply(client, _spec(tmp_path))
    packaged = _source(tmp_path) / "addon" / "resources" / "packaged.pyc"
    packaged.write_bytes(b"pre-existing packaged bytecode")
    second = _apply(
        client,
        _spec(tmp_path, prior=_prior(first), operation_id=str(UUID(int=16))),
    )
    assert second.result_classification == "synchronized"
    assert second.new_count == 1
    assert client.entries["resources/packaged.pyc"][0] == packaged.read_bytes()
    assert client.put_members[-1] == ("resources/packaged.pyc",)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("externally-added", b"untrusted"),
        ("__pycache__", None),
        ("helper.cpython-314.pyc", b"bytecode"),
    ],
)
def test_external_destination_drift_has_no_runtime_artifact_exemptions(
    tmp_path: Path, name: str, content: bytes | None
) -> None:
    client = FakeDeploymentDockerClient()
    first = _apply(client, _spec(tmp_path))
    client.entries[name] = (content, 0o755 if content is None else 0o644, 42, 43)
    client.applied = False
    with pytest.raises(DockerDevelopmentSourceDeploymentError) as captured:
        _apply(
            client,
            _spec(tmp_path, prior=_prior(first), operation_id=str(UUID(int=7))),
        )
    assert captured.value.code == "development_source_replacement_conflict"
    assert len(client.put_members) == 1


def test_persisted_destination_digest_mismatch_fails_before_apply(
    tmp_path: Path,
) -> None:
    client = FakeDeploymentDockerClient()
    first = _apply(client, _spec(tmp_path))
    bad_prior = replace(_prior(first), verified_destination_digest="sha256:" + "0" * 64)
    client.applied = False
    with pytest.raises(DockerDevelopmentSourceDeploymentError) as captured:
        _apply(
            client,
            _spec(tmp_path, prior=bad_prior, operation_id=str(UUID(int=9))),
        )
    assert captured.value.code == "development_source_replacement_conflict"
    assert len(client.put_members) == 1


def test_running_container_is_rejected_without_stop_or_apply(tmp_path: Path) -> None:
    client = FakeDeploymentDockerClient(state="running")
    with pytest.raises(ContainerMustBeStoppedError):
        _apply(client, _spec(tmp_path))
    assert client.put_members == []


def test_identity_drift_is_rejected_before_docker_or_apply(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    addon_xml = tmp_path / "development" / "addon" / "addon.xml"
    addon_xml.write_text(
        '<addon id="plugin.video.drift" name="Example" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource"/></addon>'
    )
    client = FakeDeploymentDockerClient()
    with pytest.raises(DockerDevelopmentSourceDeploymentError) as captured:
        _apply(client, spec)
    assert captured.value.code == "development_source_identity_changed"
    assert client.put_members == []


def test_second_capture_change_is_rejected_before_destination_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(tmp_path)
    real_capture = inventory.capture_development_tree  # type: ignore[attr-defined]
    calls = 0

    def changing_capture(*args: object, **kwargs: object) -> object:
        nonlocal calls
        result = real_capture(*args, **kwargs)  # type: ignore[arg-type]
        calls += 1
        if calls == 1:
            (tmp_path / "development" / "addon" / "unchanged.py").write_text(
                "changed between captures"
            )
        return result

    monkeypatch.setattr(inventory, "capture_development_tree", changing_capture)
    client = FakeDeploymentDockerClient()
    with pytest.raises(DockerDevelopmentSourceDeploymentError) as captured:
        _apply(client, spec)
    assert captured.value.code == "development_source_changed_during_capture"
    assert client.put_members == []


def test_interruption_after_apply_is_typed_reconciliation_required(
    tmp_path: Path,
) -> None:
    client = FakeDeploymentDockerClient()
    client.fail_readback_after_apply = True
    with pytest.raises(DockerDevelopmentSourceDeploymentError) as captured:
        _apply(client, _spec(tmp_path))
    assert captured.value.reconciliation_required
    assert client.put_members


def test_private_staging_root_rejects_symlink_component_before_docker(
    tmp_path: Path,
) -> None:
    target = tmp_path / "staging-target"
    target.mkdir(mode=0o700)
    (tmp_path / "private").symlink_to(target, target_is_directory=True)
    client = FakeDeploymentDockerClient()
    with pytest.raises(DockerDevelopmentSourceDeploymentError) as captured:
        _apply(client, _spec(tmp_path))
    assert captured.value.code == "development_source_staging_unavailable"
    assert client.put_members == []
    assert list(target.iterdir()) == []


def test_unexpected_post_apply_failure_is_typed_reconciliation_required(
    tmp_path: Path,
) -> None:
    client = FakeDeploymentDockerClient()
    client.fail_final_inspect = True
    with pytest.raises(DockerDevelopmentSourceDeploymentError) as captured:
        _apply(client, _spec(tmp_path))
    assert captured.value.code == "development_source_reconciliation_required"
    assert captured.value.reconciliation_required
    assert client.put_members
