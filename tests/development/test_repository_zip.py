from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from kodi_lab.development.addon_zip import AddonZipValidationError
from kodi_lab.domain import AddonZipExtensionClassification
from tests.development.test_addon_zip import make_zip, publish

REPO_ID = "repository.fixture"


def repository_descriptor() -> bytes:
    return (
        f'<addon id="{REPO_ID}" name="Fixture Repository" version="1.0.0">'
        '<requires><import addon="xbmc.addon" version="19.0.0" /></requires>'
        '<extension point="xbmc.addon.repository">'
        '<dir minversion="19.0.0" maxversion="22.9.999">'
        "<info>https://packages.example.org/catalog/addons.xml</info>"
        '<checksum verify="sha256">https://packages.example.org/catalog/addons.xml.sha256</checksum>'
        "<datadir>https://packages.example.org/packages/</datadir>"
        "<hashes>sha256</hashes></dir></extension>"
        '<extension point="xbmc.addon.metadata">'
        "<summary>Fixture</summary></extension></addon>"
    ).encode()


def repository_zip(
    descriptor: bytes | None = None,
    *,
    root: str = REPO_ID,
    extra: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in (
            (
                f"{root}/addon.xml",
                descriptor if descriptor is not None else repository_descriptor(),
            ),
            *extra,
        ):
            member = zipfile.ZipInfo(name)
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = 0o100644 << 16
            archive.writestr(member, content)
    return output.getvalue()


def test_repository_and_existing_video_have_distinct_admission(tmp_path: Path) -> None:
    _, repo = publish(tmp_path, repository_zip())
    _, video = publish(tmp_path, make_zip())
    assert (
        repo.extension_classification is AddonZipExtensionClassification.KODI_REPOSITORY
    )
    assert (
        video.extension_classification
        is AddonZipExtensionClassification.PYTHON_VIDEO_PLUGIN
    )
    assert repo.addon_id == REPO_ID


def test_root_package_directory_and_optional_repository_name(tmp_path: Path) -> None:
    descriptor = (
        repository_descriptor()
        .replace(
            b'point="xbmc.addon.repository"',
            b'point="xbmc.addon.repository" name="Fixture repository"',
        )
        .replace(
            b"https://packages.example.org/packages/", b"https://packages.example.org/"
        )
    )
    _, result = publish(tmp_path, repository_zip(descriptor))
    assert (
        result.extension_classification
        is AddonZipExtensionClassification.KODI_REPOSITORY
    )


@pytest.mark.parametrize(
    "before,after",
    [
        (b'point="xbmc.addon.repository"', b'point="xbmc.service"'),
        (
            b'<extension point="xbmc.addon.metadata">',
            b'<extension point="xbmc.service"/><extension point="xbmc.addon.metadata">',
        ),
        (b'<dir minversion="19.0.0"', b'<dir minversion="23.0.0"'),
        (b'verify="sha256"', b'verify="false"'),
        (b"<hashes>sha256</hashes>", b"<hashes>false</hashes>"),
        (b"https://packages.example.org", b"http://packages.example.org"),
        (b"https://packages.example.org", b"https://127.0.0.1"),
        (b"https://packages.example.org", b"https://user:secret@packages.example.org"),
        (b'addon="xbmc.addon"', b'addon="script.module.executable"'),
        (b"<dir minversion=", b'<dir unknown="true" minversion='),
        (b"<datadir>", b"<unknown>"),
        (
            b'point="xbmc.addon.metadata"',
            b'point="xbmc.addon.metadata" library="execute.py"',
        ),
    ],
)
def test_repository_type_and_trust_boundaries(
    tmp_path: Path, before: bytes, after: bytes
) -> None:
    with pytest.raises(AddonZipValidationError):
        publish(
            tmp_path, repository_zip(repository_descriptor().replace(before, after))
        )


@pytest.mark.parametrize(
    "payload", [b"<addon", b'<!DOCTYPE addon [<!ENTITY x "unsafe">]><addon/>']
)
def test_malformed_manifest_rejected(tmp_path: Path, payload: bytes) -> None:
    with pytest.raises(AddonZipValidationError):
        publish(tmp_path, repository_zip(payload))


def test_repository_root_mismatch_and_unsafe_entries(tmp_path: Path) -> None:
    for data in (
        repository_zip(root="repository.other"),
        repository_zip(extra=(("../escape", b"bad"),)),
    ):
        with pytest.raises(AddonZipValidationError):
            publish(tmp_path, data)


def test_duplicate_directory_fields_and_overlapping_ranges(tmp_path: Path) -> None:
    descriptor = repository_descriptor()
    start = descriptor.index(b"<dir ")
    end = descriptor.index(b"</dir>") + len(b"</dir>")
    for mutated in (
        descriptor.replace(b"</dir>", b"<hashes>sha256</hashes></dir>"),
        descriptor[:end] + descriptor[start:end] + descriptor[end:],
    ):
        with pytest.raises(AddonZipValidationError):
            publish(tmp_path, repository_zip(mutated))
