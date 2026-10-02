import io
import tarfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.fixtures.seed_audit import audit_small_movies_v2_seed


def test_rich_nexus_fixture_is_closed_and_configuration_only() -> None:
    packaged = load_packaged_fixture("small-movies-v4")
    definition = packaged.definition
    assert str(definition.id) == "b69b4aa6-9968-4d5f-8ac9-3d5b16556d94"
    assert definition.fixture_version == 4
    assert definition.compatible_kodi_version == "20.5"
    assert definition.size_category.value == "small"
    assert definition.content_category.value == "movies"
    assert len(packaged.semantics.movies) == 12
    assert packaged.semantics.series == ()
    assert packaged.semantics.episodes == ()
    assert packaged.semantics.scan_movies and not packaged.semantics.scan_tv
    seed = next(
        member
        for member in packaged.members
        if member.archive_path.endswith("MyVideos121.db")
    )
    audit = audit_small_movies_v2_seed(seed.data)
    assert audit.configured_paths[0][1] == "/home/kodi/.kodi/fixture-media/movies/"
    assert all(
        count == 0
        for table, count in audit.table_counts
        if table not in {"path", "version"}
    )
    assert dict(audit.table_counts)["path"] == 1


def test_rich_nexus_nfos_match_manifest_and_keep_synthetic_boundary() -> None:
    packaged = load_packaged_fixture("small-movies-v4")
    by_path = {member.archive_path: member for member in packaged.members}
    assert len(by_path) == 50
    assert (
        len(
            {
                member.sha256
                for member in packaged.members
                if member.archive_path.endswith("poster.png")
            }
        )
        == 12
    )
    assert (
        len(
            {
                member.sha256
                for member in packaged.members
                if member.archive_path.endswith("fanart.png")
            }
        )
        == 12
    )
    for movie in packaged.semantics.movies:
        nfo_path = (
            movie.file.removeprefix("/home/kodi/.kodi/").removesuffix(".mp4") + ".nfo"
        )
        root = ET.fromstring(by_path[nfo_path].data)
        assert root.findtext("title") == movie.title
        assert int(root.findtext("year") or 0) == movie.year
        assert root.findtext("uniqueid") == movie.unique_id
        assert root.findtext("director") == movie.director
        assert root.findtext("studio") == movie.studio
        assert root.findtext("ratings/rating/value") == f"{movie.rating:.1f}"
        assert {node.text for node in root.findall("genre")} == set(movie.genres or ())
        assert {node.findtext("name") for node in root.findall("actor")} == set(
            movie.actors or ()
        )
        assert {node.text for node in root.findall("tag")} == set(movie.tags or ())
        assert root.findtext("set/name") == movie.set_name
        assert "synthetic test clip" in (root.findtext("plot") or "")
        assert not root.findall("playcount") and not root.findall("resume")
        assert by_path[movie.file.removeprefix("/home/kodi/.kodi/")].size > 1000
    assert (
        len(
            {
                member.sha256
                for member in packaged.members
                if member.archive_path.endswith(".mp4")
            }
        )
        == 1
    )


def test_rich_movie_intersections_have_exact_nontrivial_membership() -> None:
    movies = load_packaged_fixture("small-movies-v4").semantics.movies

    def ids(predicate: object) -> set[str]:
        assert callable(predicate)
        return {movie.unique_id for movie in movies if predicate(movie)}

    assert len(movies) == 12
    assert Counter(movie.year // 10 * 10 for movie in movies) == {
        1970: 3,
        1980: 3,
        1990: 3,
        2000: 3,
    }
    assert ids(lambda movie: "Science Fiction" in (movie.genres or ())) == {
        "small-movies-v4-movie-03",
        "small-movies-v4-movie-05",
        "small-movies-v4-movie-08",
        "small-movies-v4-movie-09",
    }
    assert ids(lambda movie: movie.year // 10 == 199) == {
        "small-movies-v4-movie-07",
        "small-movies-v4-movie-08",
        "small-movies-v4-movie-09",
    }
    assert ids(lambda movie: "Ada Vale" in (movie.actors or ())) == {
        "small-movies-v4-movie-01",
        "small-movies-v4-movie-03",
        "small-movies-v4-movie-05",
        "small-movies-v4-movie-08",
        "small-movies-v4-movie-09",
    }
    assert ids(lambda movie: movie.rating is not None and movie.rating >= 8) == {
        "small-movies-v4-movie-01",
        "small-movies-v4-movie-03",
        "small-movies-v4-movie-05",
        "small-movies-v4-movie-08",
        "small-movies-v4-movie-09",
        "small-movies-v4-movie-10",
        "small-movies-v4-movie-12",
    }
    assert ids(lambda movie: movie.set_name == "Kodi Lab Set A") == {
        "small-movies-v4-movie-01",
        "small-movies-v4-movie-03",
        "small-movies-v4-movie-08",
    }
    assert ids(
        lambda movie: (
            "Science Fiction" in (movie.genres or ()) and movie.year // 10 == 199
        )
    ) == {
        "small-movies-v4-movie-08",
        "small-movies-v4-movie-09",
    }
    assert ids(
        lambda movie: (
            "Ada Vale" in (movie.actors or ()) and "Adventure" in (movie.genres or ())
        )
    ) == {
        "small-movies-v4-movie-01",
        "small-movies-v4-movie-08",
    }
    assert ids(
        lambda movie: (
            movie.rating is not None
            and movie.rating >= 8
            and "Night" in (movie.tags or ())
        )
    ) == {
        "small-movies-v4-movie-01",
        "small-movies-v4-movie-03",
        "small-movies-v4-movie-09",
        "small-movies-v4-movie-12",
    }
    assert not ids(
        lambda movie: "Horror" in (movie.genres or ()) and movie.year >= 2000
    )


def test_rich_archive_is_deterministic_and_stays_within_fixed_profile() -> None:
    packaged = load_packaged_fixture("small-movies-v4")
    assert packaged.build_archive() == packaged.build_archive()
    with tarfile.open(fileobj=io.BytesIO(packaged.build_archive())) as archive:
        members = archive.getmembers()
    assert all(not member.issym() and not member.islnk() for member in members)
    assert all(
        not member.name.startswith("/") and ".." not in Path(member.name).parts
        for member in members
    )
    assert not any(
        member.name.startswith("fixture-media/tvshows/") for member in members
    )
    assert {member.name for member in members if member.isfile()} == {
        member.archive_path for member in packaged.members
    }
