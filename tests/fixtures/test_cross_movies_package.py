"""The cross-version dataset is three ordinary exact-release fixtures."""

import hashlib
import io
import json
import runpy
import tarfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

import pytest

from kodi_lab.domain import FixtureContentCategory, FixtureSizeCategory
from kodi_lab.fixtures.package import TRUSTED_FIXTURE_SLUGS, load_packaged_fixture
from kodi_lab.fixtures.seed_audit import (
    FIXTURE_MOVIE_DIRECTORY,
    FixtureSeedAudit,
    audit_small_movies_v1_seed,
    audit_small_movies_v2_seed,
    audit_small_movies_v3_seed,
)

ROOT = Path(__file__).resolve().parents[2]
FAMILY = (
    (
        "cross-movies-matrix-v1",
        "8b25126e-f633-4778-bb24-41e232e19f26",
        "19.4",
        "MyVideos119.db",
        119,
        audit_small_movies_v3_seed,
    ),
    (
        "cross-movies-nexus-v1",
        "f153d0d8-d76b-42e7-984c-f7a9b2fd0aa1",
        "20.5",
        "MyVideos121.db",
        121,
        audit_small_movies_v2_seed,
    ),
    (
        "cross-movies-omega-v1",
        "81765a3d-1fd8-4b16-8f2e-850858052093",
        "21.3",
        "MyVideos131.db",
        131,
        audit_small_movies_v1_seed,
    ),
)
YEARS = (2001, 2005, 2009, 2011, 2015, 2019, 2021, 2024, 2026)


@pytest.mark.parametrize("slug,fixture_id,release,database,schema,audit", FAMILY)
def test_each_family_member_has_exact_manifest_seed_and_archive(
    slug: str,
    fixture_id: str,
    release: str,
    database: str,
    schema: int,
    audit: Callable[[bytes], FixtureSeedAudit],
) -> None:
    packaged = load_packaged_fixture(slug)
    definition = packaged.definition
    manifest_path = ROOT / "src/kodi_lab/fixtures" / slug / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    assert slug in TRUSTED_FIXTURE_SLUGS
    assert str(definition.id) == fixture_id
    assert definition.slug == slug
    assert definition.fixture_version == 1
    assert definition.size_category is FixtureSizeCategory.SMALL
    assert definition.content_category is FixtureContentCategory.MOVIES
    assert definition.compatible_kodi_version == release
    assert manifest["compatible_kodi_releases"] == [release]
    assert release in definition.description
    assert "Cross-Version Movies" in definition.description
    assert manifest["video_database_seed"]["database_filename"] == database
    assert manifest["video_database_seed"]["schema_version"] == schema

    seed = next(
        member
        for member in packaged.members
        if member.archive_path == f"userdata/Database/{database}"
    )
    assert packaged.seed_audit == audit(seed.data)
    assert packaged.seed_audit is not None
    assert packaged.seed_audit.schema_version == schema
    assert len(packaged.seed_audit.configured_paths) == 1
    assert packaged.seed_audit.configured_paths[0][1:4] == (
        FIXTURE_MOVIE_DIRECTORY,
        "movies",
        "metadata.local",
    )
    counts = dict(packaged.seed_audit.table_counts)
    assert counts["path"] == 1
    for table in ("movie", "tvshow", "episode", "files", "art", "bookmark"):
        assert counts[table] == 0

    with tarfile.open(fileobj=io.BytesIO(packaged.build_archive())) as archive:
        members = archive.getmembers()
    assert len(packaged.members) == 38
    assert all(not member.issym() and not member.islnk() for member in members)
    assert all(
        not member.name.startswith("/") and ".." not in member.name.split("/")
        for member in members
    )
    assert all(member.uid == 1000 and member.gid == 1000 for member in members)


def test_logical_movies_and_scanning_contract_are_identical_across_family() -> None:
    packages = [load_packaged_fixture(item[0]) for item in FAMILY]
    semantics = [package.semantics for package in packages]
    assert semantics[0] == semantics[1] == semantics[2]
    expected = tuple(
        (f"Kodi Lab Cross-Version Movie {index:03d}", year)
        for index, year in enumerate(YEARS, 1)
    )
    for spec in semantics:
        assert tuple((movie.title, movie.year) for movie in spec.movies) == expected
        assert Counter(movie.year // 10 * 10 for movie in spec.movies) == {
            2000: 3,
            2010: 3,
            2020: 3,
        }
        assert len({movie.unique_id for movie in spec.movies}) == 9
        assert len({movie.file for movie in spec.movies}) == 9
        assert spec.drift_movie_unique_id == "cross-movies-v1-movie-001"
        assert spec.scan_movies and not spec.scan_tv
        assert spec.series == ()
        assert spec.episodes == ()
        assert spec.movie_playback == ()


def test_media_metadata_and_sources_are_local_and_identical() -> None:
    packages = [load_packaged_fixture(item[0]) for item in FAMILY]
    media_maps = [
        {
            member.archive_path: member.sha256
            for member in package.members
            if not member.archive_path.startswith("userdata/Database/")
        }
        for package in packages
    ]
    assert media_maps[0] == media_maps[1] == media_maps[2]
    for package in packages:
        nfos = [
            member for member in package.members if member.archive_path.endswith(".nfo")
        ]
        assert len(nfos) == 9
        for index, member in enumerate(nfos, 1):
            root = ElementTree.fromstring(member.data)
            assert root.tag == "movie"
            assert root.findtext("title") == f"Kodi Lab Cross-Version Movie {index:03d}"
            assert root.findtext("year") == str(YEARS[index - 1])
            assert b"http://" not in member.data
            assert b"https://" not in member.data
        sources = next(
            member
            for member in package.members
            if member.archive_path == "userdata/sources.xml"
        )
        assert FIXTURE_MOVIE_DIRECTORY.encode() in sources.data
        assert b"http://" not in sources.data
        assert b"https://" not in sources.data


def test_fixed_builder_reproduces_all_committed_artifacts(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(ROOT / "scripts/materialize-cross-movies-v1.py"))
    materialize = namespace["_materialize"]
    materialize.__globals__["FIXTURES"] = tmp_path
    for member in namespace["FAMILY"]:
        materialize(*member)
    for slug, *_rest in FAMILY:
        committed = ROOT / "src/kodi_lab/fixtures" / slug
        generated = tmp_path / slug
        expected = {
            path.relative_to(committed).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in committed.rglob("*")
            if path.is_file()
        }
        actual = {
            path.relative_to(generated).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in generated.rglob("*")
            if path.is_file()
        }
        assert actual == expected
