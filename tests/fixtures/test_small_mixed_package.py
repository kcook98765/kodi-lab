import io
import tarfile
from pathlib import PurePosixPath

import pytest

from kodi_lab.fixtures.package import (
    FIXTURE_CONTAINER_DESTINATION,
    PackagedFixtureError,
    load_packaged_fixture,
)


def test_small_mixed_v1_manifest_content_and_digest_are_fixed() -> None:
    fixture = load_packaged_fixture("small-mixed-v1")

    assert str(fixture.definition.id) == "35d6854d-6cfc-5247-97df-7f2023c6ae9d"
    assert fixture.definition.slug == "small-mixed-v1"
    assert fixture.definition.fixture_version == 1
    assert fixture.definition.size_category.value == "small"
    assert fixture.definition.content_category.value == "mixed"
    assert fixture.definition.compatible_kodi_version == "21.3"
    assert fixture.definition.content_digest == (
        "sha256:5d3e523907983fffc3143c2c95a87cab8732d988954207bf17f5363d27b5c547"
    )
    assert fixture.destination == FIXTURE_CONTAINER_DESTINATION
    content = b"\n".join(member.data for member in fixture.members)
    assert b"Kodi Lab Fixture Movie" in content
    assert b"Kodi Lab Fixture Series" in content
    assert b"Kodi Lab Fixture Episode" in content
    assert b"small-mixed-v1-movie" in content
    assert b"small-mixed-v1-series" in content
    assert b"small-mixed-v1-episode" in content


def test_fixture_archive_is_deterministic_bounded_and_traversal_free() -> None:
    fixture = load_packaged_fixture("small-mixed-v1")
    first = fixture.build_archive()
    second = fixture.build_archive()
    assert first == second
    assert len(first) < 100_000

    with tarfile.open(fileobj=io.BytesIO(first), mode="r:") as archive:
        members = archive.getmembers()
    assert members
    for member in members:
        path = PurePosixPath(member.name)
        assert not path.is_absolute()
        assert ".." not in path.parts
        assert not member.issym()
        assert not member.islnk()
        assert member.uid == 1000
        assert member.gid == 1000
        assert member.mode == (0o755 if member.isdir() else 0o644)
        assert member.mtime == 0
        assert path.parts[0] in {"fixture-media", "userdata"}


def test_unknown_fixture_cannot_be_interpreted_as_a_resource_path() -> None:
    for value in ("unknown", "../small-mixed-v1", "/tmp/fixture"):
        with pytest.raises(PackagedFixtureError):
            load_packaged_fixture(value)
