from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from kodi_lab.broker.addon_zips import AddonZipInstallationSpec
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.operations import AddonZipInstallationParams
from kodi_lab.development.addon_zip import PayloadManifestEntry
from kodi_lab.development.deployment import (
    DevelopmentTreeEntry,
    DevelopmentTreeManifest,
)

INSTANCE_ID = str(UUID(int=801))
ARTIFACT_ID = str(UUID(int=802))
OPERATION_ID = str(UUID(int=803))
REVISION_ID = str(UUID(int=804))
CONTAINER_ID = "a" * 64


def _spec(tmp_path: Path) -> AddonZipInstallationSpec:
    return AddonZipInstallationSpec(
        managed_instance_id=INSTANCE_ID,
        artifact_id=ARTIFACT_ID,
        installation_operation_id=OPERATION_ID,
        runtime_revision_id=REVISION_ID,
        runtime_uid=2468,
        runtime_gid=1357,
        addon_id="plugin.video.example",
        addon_version="1.2.3",
        payload_manifest_digest="sha256:" + "1" * 64,
        payload_root=tmp_path / "private" / "payload",
        staging_root=tmp_path / "private" / "staging",
        entries=(
            PayloadManifestEntry("addon.xml", "file", 0o644, 7, "sha256:" + "2" * 64),
            PayloadManifestEntry("resources", "directory", 0o755, 0, None),
        ),
        application_started=True,
    )


def _manifest(spec: AddonZipInstallationSpec) -> DevelopmentTreeManifest:
    entries = (
        DevelopmentTreeEntry(
            path=".",
            entry_type="directory",
            mode=0o755,
            size=0,
            sha256=None,
            uid=spec.runtime_uid,
            gid=spec.runtime_gid,
        ),
        *tuple(
            DevelopmentTreeEntry(
                path=item.path,
                entry_type=item.entry_type,
                mode=item.mode,
                size=item.size,
                sha256=item.sha256,
                uid=spec.runtime_uid,
                gid=spec.runtime_gid,
            )
            for item in spec.entries
        ),
    )
    return DevelopmentTreeManifest(entries, "unused", b"{}", 7)


def test_broker_request_exposes_only_semantic_identities() -> None:
    value = AddonZipInstallationParams(
        managed_instance_id=INSTANCE_ID,
        artifact_id=ARTIFACT_ID,
        installation_operation_id=OPERATION_ID,
        expected_container_id=CONTAINER_ID,
    )
    assert set(value.model_dump()) == {
        "managed_instance_id",
        "artifact_id",
        "installation_operation_id",
        "expected_container_id",
    }
    for name, injected in (
        ("path", "/tmp/archive.zip"),
        ("destination", "/home/kodi/.kodi/addons"),
        ("uid", 0),
        ("mode", 0o777),
        ("command", "unzip"),
        ("url", "https://example.invalid/a.zip"),
        ("docker_options", {"binds": ["/"]}),
    ):
        with pytest.raises(ValidationError):
            AddonZipInstallationParams.model_validate(
                {**value.model_dump(), name: injected}
            )


def test_exact_readback_accepts_only_complete_bytes_type_mode_and_ownership(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    exact = _manifest(spec)
    assert DockerManagedContainerAdapter._addon_zip_manifest_matches(spec, exact)

    root, *original = exact.entries
    mutations = (
        replace(original[0], sha256="sha256:" + "9" * 64),
        replace(original[0], mode=0o600),
        replace(original[0], uid=999),
        replace(original[0], gid=999),
        replace(original[0], size=8),
        replace(original[0], entry_type="directory"),
    )
    for changed in mutations:
        manifest = replace(exact, entries=(root, changed, original[1]))
        assert not DockerManagedContainerAdapter._addon_zip_manifest_matches(
            spec, manifest
        )
    assert not DockerManagedContainerAdapter._addon_zip_manifest_matches(
        spec, replace(exact, entries=(root, original[0]))
    )
    unexpected = DevelopmentTreeEntry(
        "unexpected", "file", 0o644, 1, "sha256:" + "3" * 64, 2468, 1357
    )
    assert not DockerManagedContainerAdapter._addon_zip_manifest_matches(
        spec, replace(exact, entries=(root, *original, unexpected))
    )

    for changed_root in (
        replace(root, mode=0o700),
        replace(root, uid=999),
        replace(root, gid=999),
    ):
        assert not DockerManagedContainerAdapter._addon_zip_manifest_matches(
            spec, replace(exact, entries=(changed_root, *original))
        )
