import io
import tarfile
from pathlib import PurePosixPath

import pytest

from kodi_lab.fixtures.package import (
    FIXTURE_CONTAINER_DESTINATION,
    PackagedFixtureError,
    load_packaged_fixture,
)
from kodi_lab.fixtures.seed_audit import (
    FIXTURE_MOVIE_DIRECTORY,
    FIXTURE_TV_DIRECTORY,
    KODI_VIDEO_DATABASE_FILENAME,
    FixtureSeedAuditError,
    audit_small_mixed_v2_seed,
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


def test_v1_source_visibility_did_not_establish_scanner_content_assignment() -> None:
    fixture = load_packaged_fixture("small-mixed-v1")
    paths = {member.archive_path for member in fixture.members}

    assert "userdata/sources.xml" in paths
    assert f"userdata/Database/{KODI_VIDEO_DATABASE_FILENAME}" not in paths
    sources = next(
        member.data
        for member in fixture.members
        if member.archive_path == "userdata/sources.xml"
    )
    assert b"/home/kodi/.kodi/fixture-media/" in sources
    assert b"metadata.local" not in sources
    assert b"<content>movies</content>" not in sources
    assert b"<content>tvshows</content>" not in sources


def test_small_mixed_v2_is_distinct_and_preserves_synthetic_assets() -> None:
    v1 = load_packaged_fixture("small-mixed-v1")
    v2 = load_packaged_fixture("small-mixed-v2")

    assert str(v2.definition.id) == "f7bd24c7-bccc-44ad-b286-999979b59d9e"
    assert v2.definition.id != v1.definition.id
    assert v2.definition.slug == "small-mixed-v2"
    assert v2.definition.fixture_version == 2
    assert v2.definition.content_digest == (
        "sha256:624adef0e88de74d8c34d1176d0d306e7f4a2731db147eb26b526ab42b810d18"
    )
    assert v2.definition.content_digest != v1.definition.content_digest
    assert v2.definition.compatible_kodi_version == "21.3"
    v1_files = {member.archive_path: member.data for member in v1.members}
    v2_files = {member.archive_path: member.data for member in v2.members}
    assert set(v2_files) - set(v1_files) == {"userdata/Database/MyVideos131.db"}
    assert all(v2_files[path] == data for path, data in v1_files.items())


def test_small_mixed_v2_seed_is_exact_configuration_only_omega_database() -> None:
    fixture = load_packaged_fixture("small-mixed-v2")
    seed = next(
        member
        for member in fixture.members
        if member.archive_path == "userdata/Database/MyVideos131.db"
    )

    assert seed.sha256 == (
        "f81fcaa98a192e6a93b9f36a659d9ce56546fdc96f4edfe74d3f1eb5094c3fd9"
    )
    audit = audit_small_mixed_v2_seed(seed.data)
    assert fixture.seed_audit == audit
    assert audit.database_filename == "MyVideos131.db"
    assert audit.schema_version == 131
    assert audit.configured_paths[0][1:4] == (
        FIXTURE_MOVIE_DIRECTORY,
        "movies",
        "metadata.local",
    )
    assert audit.configured_paths[1][1:4] == (
        FIXTURE_TV_DIRECTORY,
        "tvshows",
        "metadata.local",
    )
    counts = dict(audit.table_counts)
    assert counts["path"] == 2
    assert counts["version"] == 1
    assert counts["videoversiontype"] == 387
    for table, count in counts.items():
        if table not in {"path", "version", "videoversiontype"}:
            assert count == 0, table


def test_seed_audit_rejects_corrupt_or_mutated_database() -> None:
    fixture = load_packaged_fixture("small-mixed-v2")
    seed = next(
        member.data for member in fixture.members if member.archive_path.endswith(".db")
    )
    corrupted = bytearray(seed)
    corrupted[0] ^= 0xFF
    with pytest.raises(FixtureSeedAuditError):
        audit_small_mixed_v2_seed(bytes(corrupted))


@pytest.mark.parametrize("slug", ["small-mixed-v1", "small-mixed-v2"])
def test_fixture_archive_is_deterministic_bounded_and_traversal_free(
    slug: str,
) -> None:
    fixture = load_packaged_fixture(slug)
    first = fixture.build_archive()
    second = fixture.build_archive()
    assert first == second
    assert len(first) < 500_000

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
