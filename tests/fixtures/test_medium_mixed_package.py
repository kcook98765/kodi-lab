import io
import tarfile
from collections import Counter
from pathlib import PurePosixPath

from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.fixtures.seed_audit import (
    FIXTURE_MOVIE_DIRECTORY,
    FIXTURE_TV_DIRECTORY,
    audit_medium_mixed_v1_seed,
)


def test_medium_mixed_v1_identity_taxonomy_and_compatibility_are_exact() -> None:
    packaged = load_packaged_fixture("medium-mixed-v1")
    definition = packaged.definition

    assert str(definition.id) == "13cfd4d5-236a-480c-ae7d-eb54398ab184"
    assert definition.slug == "medium-mixed-v1"
    assert definition.fixture_version == 1
    assert definition.size_category.value == "medium"
    assert definition.content_category.value == "mixed"
    assert definition.compatible_kodi_version == "21.3"
    assert definition.content_digest == (
        "sha256:94e321dcb31363f9675292acbdbc5f032833d39d309fed451c183110a4c74a9c"
    )


def test_medium_mixed_v1_semantics_are_exact_and_relationship_complete() -> None:
    semantics = load_packaged_fixture("medium-mixed-v1").semantics

    assert len(semantics.movies) == 25
    assert len(semantics.series) == 5
    assert len(semantics.episodes) == 50
    assert semantics.scan_movies and semantics.scan_tv
    assert semantics.drift_movie_unique_id == "medium-mixed-v1-movie-001"
    assert semantics.drift_episode_unique_id == "medium-mixed-v1-series-01-s01e01"
    assert tuple(movie.title for movie in semantics.movies) == tuple(
        f"Kodi Lab Medium Movie {index:03d}" for index in range(1, 26)
    )
    assert tuple(show.title for show in semantics.series) == tuple(
        f"Kodi Lab Medium Series {index:02d}" for index in range(1, 6)
    )
    assert len({movie.unique_id for movie in semantics.movies}) == 25
    assert len({show.unique_id for show in semantics.series}) == 5
    assert len({episode.unique_id for episode in semantics.episodes}) == 50
    relationships = Counter(
        (episode.show_title, episode.season) for episode in semantics.episodes
    )
    assert relationships == Counter(
        {
            (f"Kodi Lab Medium Series {show:02d}", season): 5
            for show in range(1, 6)
            for season in (1, 2)
        }
    )
    for show in range(1, 6):
        for season in (1, 2):
            assert {
                episode.episode
                for episode in semantics.episodes
                if episode.show_title == f"Kodi Lab Medium Series {show:02d}"
                and episode.season == season
            } == set(range(1, 6))


def test_medium_seed_is_exact_pure_kodi_21_configuration() -> None:
    packaged = load_packaged_fixture("medium-mixed-v1")
    seed = next(
        member
        for member in packaged.members
        if member.archive_path == "userdata/Database/MyVideos131.db"
    )

    assert seed.sha256 == (
        "f81fcaa98a192e6a93b9f36a659d9ce56546fdc96f4edfe74d3f1eb5094c3fd9"
    )
    audit = audit_medium_mixed_v1_seed(seed.data)
    assert packaged.seed_audit == audit
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


def test_medium_package_size_and_archive_safety_remain_bounded() -> None:
    packaged = load_packaged_fixture("medium-mixed-v1")
    archive = packaged.build_archive()

    assert len(packaged.members) == 267
    assert sum(member.size for member in packaged.members) == 1_428_455
    assert len(archive) == 1_638_400
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as opened:
        members = opened.getmembers()
    assert members
    for member in members:
        path = PurePosixPath(member.name)
        assert not path.is_absolute()
        assert ".." not in path.parts
        assert not member.issym()
        assert not member.islnk()
        assert member.uid == 1000
        assert member.gid == 1000
        assert member.mtime == 0
        assert path.parts[0] in {"fixture-media", "userdata"}


def test_medium_assets_are_reused_but_library_paths_are_unique() -> None:
    packaged = load_packaged_fixture("medium-mixed-v1")
    semantics = packaged.semantics
    media = [
        member for member in packaged.members if member.archive_path.endswith(".mp4")
    ]

    assert len(media) == 75
    assert len({member.sha256 for member in media}) == 2
    assert len({movie.file for movie in semantics.movies}) == 25
    assert len({episode.file for episode in semantics.episodes}) == 50
