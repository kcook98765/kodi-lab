import io
import tarfile
from collections.abc import Callable

import pytest

from kodi_lab.domain import FixtureContentCategory, FixtureSizeCategory
from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.fixtures.seed_audit import (
    FixtureSeedAudit,
    audit_small_tv_v1_seed,
    audit_small_tv_v2_seed,
    audit_small_tv_v3_seed,
)


def test_small_tv_v1_identity_taxonomy_and_exact_compatibility() -> None:
    packaged = load_packaged_fixture("small-tv-v1")
    fixture = packaged.definition

    assert str(fixture.id) == "6143bf65-b438-442a-a017-6ff1c7232b59"
    assert fixture.slug == "small-tv-v1"
    assert fixture.fixture_version == 1
    assert fixture.size_category is FixtureSizeCategory.SMALL
    assert fixture.content_category is FixtureContentCategory.TV
    assert fixture.compatible_kodi_release.canonical == "21.3"
    assert fixture.content_digest == (
        "sha256:1a04f9eb0e47c146909e22fb06610abae61715a670c4dcff6be7afd83ff06185"
    )


def test_small_tv_v1_semantics_define_one_show_and_three_relationships() -> None:
    semantics = load_packaged_fixture("small-tv-v1").semantics

    assert semantics.movies == ()
    assert tuple(show.unique_id for show in semantics.series) == ("small-tv-v1-series",)
    assert tuple(episode.unique_id for episode in semantics.episodes) == (
        "small-tv-v1-episode-1",
        "small-tv-v1-episode-2",
        "small-tv-v1-episode-3",
    )
    assert all(
        episode.show_title == "Kodi Lab Fixture Series TV"
        and episode.season == 1
        and episode.episode == index
        for index, episode in enumerate(semantics.episodes, 1)
    )
    assert not semantics.scan_movies
    assert semantics.scan_tv
    assert semantics.drift_episode_unique_id == "small-tv-v1-episode-1"


def test_small_tv_v1_seed_is_tv_configuration_only() -> None:
    packaged = load_packaged_fixture("small-tv-v1")
    seed = next(
        member
        for member in packaged.members
        if member.archive_path.endswith("MyVideos131.db")
    )
    audit = audit_small_tv_v1_seed(seed.data)

    assert audit.schema_version == 131
    assert audit.configured_paths == (
        (
            1,
            "/home/kodi/.kodi/fixture-media/tvshows/",
            "tvshows",
            "metadata.local",
            None,
            0,
            0,
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


def test_small_tv_archive_is_safe_and_contains_no_movies_tree() -> None:
    packaged = load_packaged_fixture("small-tv-v1")
    with tarfile.open(fileobj=io.BytesIO(packaged.build_archive())) as archive:
        members = archive.getmembers()
    names = {member.name for member in members}

    assert "userdata/Database/MyVideos131.db" in names
    assert not any(name.startswith("fixture-media/movies/") for name in names)
    assert all(
        not name.startswith("/") and ".." not in name.split("/") for name in names
    )
    assert all(not member.issym() and not member.islnk() for member in members)


def test_small_content_families_are_immutable_and_distinct() -> None:
    mixed = load_packaged_fixture("small-mixed-v2")
    movies = load_packaged_fixture("small-movies-v1")
    tv = load_packaged_fixture("small-tv-v1")

    assert len({mixed.definition.id, movies.definition.id, tv.definition.id}) == 3
    assert (
        len(
            {
                mixed.definition.content_digest,
                movies.definition.content_digest,
                tv.definition.content_digest,
            }
        )
        == 3
    )
    assert (
        mixed.definition.content_category,
        movies.definition.content_category,
        tv.definition.content_category,
    ) == (
        FixtureContentCategory.MIXED,
        FixtureContentCategory.MOVIES,
        FixtureContentCategory.TV,
    )
    assert (mixed.semantics.scan_movies, mixed.semantics.scan_tv) == (True, True)
    assert (movies.semantics.scan_movies, movies.semantics.scan_tv) == (True, False)
    assert (tv.semantics.scan_movies, tv.semantics.scan_tv) == (False, True)


@pytest.mark.parametrize(
    (
        "slug",
        "fixture_id",
        "version",
        "release",
        "digest",
        "database",
        "schema",
        "audit",
    ),
    [
        (
            "small-tv-v2",
            "6668f956-d44c-4f28-9337-fb7e2b14a8dd",
            2,
            "20.5",
            "sha256:ee5132bd2d7631055c47d353f2a48bd7a36a4b15b70be473591f21ed3a654391",
            "MyVideos121.db",
            121,
            audit_small_tv_v2_seed,
        ),
        (
            "small-tv-v3",
            "3085d006-b73b-49ff-9143-41f7b13f5fc1",
            3,
            "19.5",
            "sha256:a7554a4596fa642aed857b16b019f3a16f3668212a5cd166b7933af61f0c3e50",
            "MyVideos119.db",
            119,
            audit_small_tv_v3_seed,
        ),
    ],
)
def test_stable_tv_variants_are_exact_and_configuration_only(
    slug: str,
    fixture_id: str,
    version: int,
    release: str,
    digest: str,
    database: str,
    schema: int,
    audit: Callable[[bytes], FixtureSeedAudit],
) -> None:
    packaged = load_packaged_fixture(slug)
    definition = packaged.definition
    assert str(definition.id) == fixture_id
    assert definition.fixture_version == version
    assert definition.compatible_kodi_release.canonical == release
    assert definition.content_digest == digest
    seed = next(
        member for member in packaged.members if member.archive_path.endswith(database)
    )
    result = audit(seed.data)
    assert result.schema_version == schema
    assert len(result.configured_paths) == 1
    assert result.configured_paths[0][2] == "tvshows"
    assert all(
        dict(result.table_counts)[name] == 0
        for name in ("movie", "tvshow", "episode", "files", "art", "bookmark")
    )


def test_tv_family_semantics_and_assets_are_release_independent() -> None:
    variants = tuple(
        load_packaged_fixture(slug)
        for slug in ("small-tv-v1", "small-tv-v2", "small-tv-v3")
    )
    assert variants[0].semantics == variants[1].semantics == variants[2].semantics
    for packaged in variants[1:]:
        comparable = {
            member.archive_path: member.sha256
            for member in packaged.members
            if not member.archive_path.startswith("userdata/Database/")
        }
        original = {
            member.archive_path: member.sha256
            for member in variants[0].members
            if not member.archive_path.startswith("userdata/Database/")
        }
        assert comparable == original
