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
    DevelopmentSourceDestinationVerificationSpec,
    PriorDevelopmentSourceDeploymentEvidence,
)
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.errors import (
    ContainerMustBeStoppedError,
    DockerDevelopmentSourceDeploymentError,
)
from kodi_lab.development.deployment import (
    capture_destination_archive,
    capture_development_tree,
)
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
            '<extension point="xbmc.python.pluginsource" library="unchanged.py">'
            "<provides>video</provides></extension>"
            '<extension point="xbmc.addon.metadata"/></addon>'
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
                if path == "/home/kodi" and member.name == ".kodi/addons":
                    continue
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


def _verification_spec(
    destination_digest: str,
) -> DevelopmentSourceDestinationVerificationSpec:
    return DevelopmentSourceDestinationVerificationSpec(
        managed_instance_id=INSTANCE_ID,
        development_source_id=SOURCE_ID,
        runtime_revision_id=REVISION_ID,
        runtime_uid=42,
        runtime_gid=43,
        addon_id=ADDON_ID,
        addon_version="1.0.0",
        verified_destination_digest=destination_digest,
        trusted_container_id=CONTAINER_ID,
    )


def test_running_destination_verification_reuses_complete_capture(
    tmp_path: Path,
) -> None:
    client = FakeDeploymentDockerClient()
    deployed = _apply(client, _spec(tmp_path))
    client.inspection = _inspection(state="running")
    result = DockerManagedContainerAdapter(
        lambda: client
    ).verify_attached_development_source_destination(
        spec=_verification_spec(deployed.verified_destination_digest),
        expected_container_id=CONTAINER_ID,
    )
    assert result.matched is True
    assert result.addon_id == ADDON_ID
    assert result.addon_version == "1.0.0"
    assert result.video_plugin_descriptor_eligible is True


def test_running_destination_verification_reports_missing_declared_library(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path) / "addon"
    (source / "addon.xml").write_text(
        f'<addon id="{ADDON_ID}" name="Example" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="missing.py">'
        "<provides>video</provides></extension></addon>"
    )
    client = FakeDeploymentDockerClient()
    deployed = _apply(client, _spec(tmp_path))
    client.inspection = _inspection(state="running")
    result = DockerManagedContainerAdapter(
        lambda: client
    ).verify_attached_development_source_destination(
        spec=_verification_spec(deployed.verified_destination_digest),
        expected_container_id=CONTAINER_ID,
    )
    assert result.matched is True
    assert result.video_plugin_descriptor_eligible is False


def test_running_destination_verification_rejects_external_drift(
    tmp_path: Path,
) -> None:
    client = FakeDeploymentDockerClient()
    deployed = _apply(client, _spec(tmp_path))
    client.entries["drift.txt"] = (b"external", 0o644, 42, 43)
    client.inspection = _inspection(state="running")
    with pytest.raises(
        DockerDevelopmentSourceDeploymentError,
        match="Current destination bytes do not match",
    ):
        DockerManagedContainerAdapter(
            lambda: client
        ).verify_attached_development_source_destination(
            spec=_verification_spec(deployed.verified_destination_digest),
            expected_container_id=CONTAINER_ID,
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


def test_initial_copy_owns_addons_parent_without_rewriting_profile_root(
    tmp_path: Path,
) -> None:
    class ParentCheckingClient(FakeDeploymentDockerClient):
        def put_archive(self, container: str, path: str, data: BinaryIO) -> bool:
            with tarfile.open(fileobj=io.BytesIO(data.read())) as archive:
                # Docker otherwise creates this implicit parent as root:root,
                # preventing the non-root reviewed Bridge installer writing here.
                parent = archive.getmember(".kodi/addons")
                assert parent.isdir()
                assert (parent.uid, parent.gid, parent.mode) == (42, 43, 0o755)
                assert ".kodi" not in archive.getnames()
                assert archive.getnames().index(".kodi/addons") < (
                    archive.getnames().index(f".kodi/addons/{ADDON_ID}")
                )
            data.seek(0)
            return super().put_archive(container, path, data)

    client = ParentCheckingClient()
    result = _apply(client, _spec(tmp_path))
    assert result.result_classification == "initial_deployment"
    assert client.inspection["State"] == {"Status": "exited"}


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


def test_stale_exact_prior_materialization_is_reaffirmed_without_write(
    tmp_path: Path,
) -> None:
    client = FakeDeploymentDockerClient()
    first = _apply(client, _spec(tmp_path))
    puts = len(client.put_members)
    stale = replace(
        _prior(first), confidence=DevelopmentSourceDeploymentConfidence.STALE
    )
    result = _apply(
        client, _spec(tmp_path, prior=stale, operation_id=str(UUID(int=10)))
    )
    assert result.result_classification == "unchanged"
    assert result.source_manifest_digest == first.source_manifest_digest
    assert result.verified_destination_digest == first.verified_destination_digest
    assert len(client.put_members) == puts


@pytest.mark.parametrize("drift", ["changed_byte", "extra_file", "changed_source"])
def test_stale_reaffirmation_rejects_drift_without_write(
    tmp_path: Path, drift: str
) -> None:
    client = FakeDeploymentDockerClient()
    first = _apply(client, _spec(tmp_path))
    puts = len(client.put_members)
    if drift == "changed_byte":
        client.entries["unchanged.py"] = (b"manual edit", 0o644, 42, 43)
    elif drift == "extra_file":
        client.entries["extra.py"] = (b"unknown", 0o644, 42, 43)
    else:
        (_source(tmp_path) / "addon" / "unchanged.py").write_text("source edit")
    stale = replace(
        _prior(first), confidence=DevelopmentSourceDeploymentConfidence.STALE
    )
    with pytest.raises(DockerDevelopmentSourceDeploymentError) as captured:
        _apply(client, _spec(tmp_path, prior=stale, operation_id=str(UUID(int=11))))
    assert captured.value.code == "development_source_replacement_conflict"
    assert len(client.put_members) == puts


@pytest.mark.parametrize("difference", ["unknown", "container", "revision"])
def test_stale_reaffirmation_requires_exact_prior_authority(
    tmp_path: Path, difference: str
) -> None:
    client = FakeDeploymentDockerClient()
    first = _apply(client, _spec(tmp_path))
    stale = replace(
        _prior(first), confidence=DevelopmentSourceDeploymentConfidence.STALE
    )
    if difference == "unknown":
        stale = replace(stale, confidence=DevelopmentSourceDeploymentConfidence.UNKNOWN)
    elif difference == "container":
        stale = replace(stale, trusted_container_id="9" * 64)
    else:
        stale = replace(stale, runtime_revision_id=str(UUID(int=99)))
    puts = len(client.put_members)
    with pytest.raises(DockerDevelopmentSourceDeploymentError) as captured:
        _apply(client, _spec(tmp_path, prior=stale, operation_id=str(UUID(int=12))))
    assert captured.value.code == "development_source_replacement_conflict"
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


def test_copy_sync_measurements_cover_real_work_without_more_readbacks(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import json
    import logging

    client = FakeDeploymentDockerClient()
    with caplog.at_level(logging.INFO, logger="kodi_lab.deployment.measurement"):
        first = _apply(client, _spec(tmp_path))
        result = _apply(client, _spec(tmp_path, prior=_prior(first)))
    measurements = [
        json.loads(record.getMessage().removeprefix("deployment measurement "))
        for record in caplog.records
        if record.name == "kodi_lab.deployment.measurement"
    ]
    assert len(measurements) == 2
    initial, noop = measurements
    assert initial["completed"] and noop["completed"]
    assert (
        initial["counts"]["source_stage_bytes"]
        == initial["counts"]["source_confirm_bytes"]
    )
    assert initial["counts"]["transferred_payload_bytes"] == first.transferred_bytes > 0
    assert initial["counts"]["delta_archive_bytes"] > first.transferred_bytes
    assert noop["counts"]["transferred_payload_bytes"] == result.transferred_bytes == 0
    assert "delta_archive_bytes" not in noop["counts"]
    assert noop["counts"]["source_stage_bytes"] == 0
    assert noop["counts"]["source_scan_bytes"] == noop["counts"]["source_confirm_bytes"]
    assert (
        noop["counts"]["destination_before_archive_bytes"]
        == noop["counts"]["destination_after_archive_bytes"]
    )
    assert set(noop["phases_seconds"]) >= {
        "source_scan_hash",
        "source_scan_hash_stage_confirm",
        "destination_before",
        "destination_after",
        "destination_verify",
        "stage_cleanup",
    }
    assert len(client.put_members) == 1


def test_delta_stages_only_changed_files_but_hashes_every_member_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeDeploymentDockerClient()
    spec = _spec(tmp_path)
    first = _apply(client, spec)
    source = tmp_path / "development" / "addon"
    (source / "resources" / "changed.py").write_text("after")
    real_capture = capture_development_tree
    captured: list[tuple[int, tuple[str, ...]]] = []

    def capture(*args: object, **kwargs: object) -> object:
        result = real_capture(*args, **kwargs)  # type: ignore[arg-type]
        stage = kwargs.get("stage_root")
        paths = (
            ()
            if not isinstance(stage, Path)
            else tuple(
                sorted(
                    str(path.relative_to(stage))
                    for path in stage.rglob("*")
                    if path.is_file()
                )
            )
        )
        captured.append((len(result.entries), paths))
        return result

    monkeypatch.setattr(inventory, "capture_development_tree", capture)
    changed = _apply(client, _spec(tmp_path, prior=_prior(first)))
    assert captured == [(5, ()), (5, ("resources/changed.py",))]
    assert changed.transferred_bytes == len("after")
    captured.clear()
    _apply(client, _spec(tmp_path, prior=_prior(changed)))
    assert captured == [(5, ()), (5, ())]


def test_unstaged_source_change_during_destination_read_still_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeDeploymentDockerClient()
    first = _apply(client, _spec(tmp_path))
    original_get = client.get_archive
    source = tmp_path / "development" / "addon"
    (source / "resources" / "changed.py").write_text("after")

    def get_archive(container: str, path: str) -> tuple[object, object]:
        result = original_get(container, path)
        (source / "unchanged.py").write_text("out of band change")
        return result

    monkeypatch.setattr(client, "get_archive", get_archive)
    with pytest.raises(DockerDevelopmentSourceDeploymentError) as caught:
        _apply(client, _spec(tmp_path, prior=_prior(first)))
    assert caught.value.code == "development_source_changed_during_capture"
    assert len(client.put_members) == 1


@pytest.mark.parametrize("change", ["nested", "mode"])
def test_selective_staging_handles_new_nested_paths_and_mode_only_edits(
    tmp_path: Path,
    change: str,
) -> None:
    client = FakeDeploymentDockerClient()
    first = _apply(client, _spec(tmp_path))
    source = tmp_path / "development" / "addon"
    if change == "nested":
        (source / "nested" / "deep").mkdir(parents=True)
        (source / "nested" / "deep" / "new.bin").write_bytes(b"\x00native resource\xff")
        selected = "nested/deep/new.bin"
    else:
        (source / "unchanged.py").chmod(0o751)
        selected = "unchanged.py"
    result = _apply(client, _spec(tmp_path, prior=_prior(first)))
    assert selected in client.put_members[-1]
    assert result.transferred_bytes == len(client.entries[selected][0] or b"")
    assert client.current_digest() == result.verified_destination_digest
    if change == "mode":
        assert client.entries[selected][1] == 0o755


def test_rejected_changed_capture_reports_confirmed_staged_size_not_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import json
    import logging

    client = FakeDeploymentDockerClient()
    first = _apply(client, _spec(tmp_path))
    source = tmp_path / "development" / "addon"
    (source / "resources" / "changed.py").write_text("after")
    original_get = client.get_archive

    def get_archive(container: str, path: str) -> tuple[object, object]:
        result = original_get(container, path)
        (source / "resources" / "changed.py").write_text("larger captured content")
        return result

    monkeypatch.setattr(client, "get_archive", get_archive)
    with (
        caplog.at_level(logging.INFO, logger="kodi_lab.deployment.measurement"),
        pytest.raises(DockerDevelopmentSourceDeploymentError),
    ):
        _apply(client, _spec(tmp_path, prior=_prior(first)))
    item = json.loads(
        caplog.records[-1].getMessage().removeprefix("deployment measurement ")
    )
    assert not item["completed"]
    assert item["counts"]["planned_stage_bytes"] == len("after")
    assert item["counts"]["source_stage_bytes"] == len("larger captured content")
    assert len(client.put_members) == 1
