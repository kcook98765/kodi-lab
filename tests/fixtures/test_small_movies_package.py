import io
import tarfile

import pytest

from kodi_lab.domain import FixtureContentCategory, FixtureSizeCategory
from kodi_lab.fixtures.package import PackagedFixtureError, load_packaged_fixture
from kodi_lab.fixtures.seed_audit import audit_small_movies_v1_seed


def test_small_movies_v1_identity_taxonomy_and_exact_compatibility() -> None:
    packaged = load_packaged_fixture("small-movies-v1")
    fixture = packaged.definition

    assert str(fixture.id) == "9526870f-5fc2-4850-9fcf-0523aceb3cf5"
    assert fixture.slug == "small-movies-v1"
    assert fixture.fixture_version == 1
    assert fixture.size_category is FixtureSizeCategory.SMALL
    assert fixture.content_category is FixtureContentCategory.MOVIES
    assert fixture.compatible_kodi_release.canonical == "21.3"
    assert fixture.content_digest == (
        "sha256:3ffce70ae22cb3c3472a9dc46624cd4e4f85b539fce48b96634844ab3ea21da0"
    )


def test_small_movies_v1_semantic_spec_is_exact_and_movies_only() -> None:
    semantics = load_packaged_fixture("small-movies-v1").semantics

    assert tuple(movie.title for movie in semantics.movies) == (
        "Kodi Lab Fixture Movie 1",
        "Kodi Lab Fixture Movie 2",
        "Kodi Lab Fixture Movie 3",
    )
    assert tuple(movie.unique_id for movie in semantics.movies) == (
        "small-movies-v1-movie-1",
        "small-movies-v1-movie-2",
        "small-movies-v1-movie-3",
    )
    assert semantics.series == ()
    assert semantics.episodes == ()
    assert semantics.scan_movies
    assert not semantics.scan_tv


def test_small_movies_v1_seed_is_configuration_only() -> None:
    packaged = load_packaged_fixture("small-movies-v1")
    seed = next(
        member
        for member in packaged.members
        if member.archive_path.endswith("MyVideos131.db")
    )
    audit = audit_small_movies_v1_seed(seed.data)

    assert audit.schema_version == 131
    assert audit.configured_paths == (
        (
            1,
            "/home/kodi/.kodi/fixture-media/movies/",
            "movies",
            "metadata.local",
            None,
            1,
            1,
            "",
            0,
            0,
            0,
            None,
            None,
        ),
    )
    counts = dict(audit.table_counts)
    for table in ("movie", "tvshow", "episode", "files", "art", "bookmark"):
        assert counts[table] == 0
    assert counts["path"] == 1


def test_small_movies_archive_is_safe_and_has_no_tv_tree() -> None:
    packaged = load_packaged_fixture("small-movies-v1")
    with tarfile.open(fileobj=io.BytesIO(packaged.build_archive())) as archive:
        members = archive.getmembers()
    names = {member.name for member in members}

    assert "userdata/Database/MyVideos131.db" in names
    assert not any(name.startswith("fixture-media/tvshows/") for name in names)
    assert all(
        not name.startswith("/") and ".." not in name.split("/") for name in names
    )
    assert all(not member.issym() and not member.islnk() for member in members)


def test_existing_mixed_fixture_is_distinct_and_unchanged() -> None:
    movies = load_packaged_fixture("small-movies-v1").definition
    mixed = load_packaged_fixture("small-mixed-v2").definition
    assert movies.id != mixed.id
    assert movies.content_digest != mixed.content_digest
    assert mixed.content_digest == (
        "sha256:624adef0e88de74d8c34d1176d0d306e7f4a2731db147eb26b526ab42b810d18"
    )
    assert mixed.content_category is FixtureContentCategory.MIXED


def test_unknown_movies_fixture_is_not_a_trusted_package() -> None:
    packaged = load_packaged_fixture("small-movies-v1")
    assert packaged.semantics.drift_movie_unique_id == "small-movies-v1-movie-1"
    with pytest.raises(PackagedFixtureError):
        load_packaged_fixture("small-movies-v2")
