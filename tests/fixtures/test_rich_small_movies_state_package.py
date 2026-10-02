import copy
import io
import json
import tarfile
from pathlib import Path
from types import MappingProxyType
from typing import cast

import pytest

from kodi_lab.fixtures.package import (
    PackagedFixtureError,
    _parse_semantics,
    load_packaged_fixture,
)
from kodi_lab.fixtures.seed_audit import audit_small_movies_v2_seed


def _manifest() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "src/kodi_lab/fixtures/small-movies-v5/manifest.json"
            ).read_text()
        ),
    )


def test_v5_has_distinct_immutable_identity_and_identical_scan_payload() -> None:
    old = load_packaged_fixture("small-movies-v4")
    new = load_packaged_fixture("small-movies-v5")
    assert old.definition.content_digest == (
        "sha256:2a548663491fea8262696291a96473ce6a9dabb71eccaa43c63f729ead0495c2"
    )
    assert new.definition.id != old.definition.id
    assert str(new.definition.id) == "1f5e518e-a643-4dcb-aead-2d1c01b68bcc"
    assert new.definition.content_digest == (
        "sha256:ca5207ff2e3888c259c31718daacaa5bdd11bddbdfc3417e4a33ad5efac3aeb6"
    )
    assert new.definition.fixture_version == 5
    assert new.definition.compatible_kodi_release.canonical == "20.5"
    assert new.semantics.movies == old.semantics.movies
    assert old.semantics.movie_playback == ()
    assert len(new.semantics.movie_playback) == 12
    assert {member.archive_path: member.sha256 for member in new.members} == {
        member.archive_path: member.sha256 for member in old.members
    }
    seed = next(m for m in new.members if m.archive_path.endswith("MyVideos121.db"))
    assert old.seed_audit is not None
    assert (
        audit_small_movies_v2_seed(seed.data).table_counts
        == old.seed_audit.table_counts
    )
    with tarfile.open(fileobj=io.BytesIO(new.build_archive())) as archive:
        assert all(not m.issym() and not m.islnk() for m in archive.getmembers())
        assert all(
            not m.name.startswith("/") and ".." not in Path(m.name).parts
            for m in archive.getmembers()
        )
    assert new.build_archive() == new.build_archive()


def test_v5_exact_watched_unwatched_and_progress_subsets() -> None:
    states = load_packaged_fixture("small-movies-v5").semantics.movie_playback
    by_number = {int(state.unique_id.rsplit("-", 1)[1]): state for state in states}
    assert {n for n, state in by_number.items() if state.playcount == 1} == {
        1,
        5,
        7,
        10,
        12,
    }
    assert {n for n, state in by_number.items() if state.playcount == 0} == {
        2,
        3,
        4,
        6,
        8,
        9,
        11,
    }
    assert {n for n, state in by_number.items() if state.resume_position > 0} == {3, 8}
    assert all(
        (by_number[n].resume_position, by_number[n].resume_total) == (1.0, 2.0)
        for n in (3, 8)
    )
    assert all(
        state.lastplayed == "2020-01-01 00:00:00" for state in states if state.playcount
    )
    assert all(state.lastplayed is None for state in states if not state.playcount)


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x["watched"][0].update(unique_id="unknown-movie"),
        lambda x: x["watched"][0].update(lastplayed=""),
        lambda x: x["in_progress"][0].update(position=-1),
        lambda x: x["in_progress"][0].update(position=float("nan")),
        lambda x: x["in_progress"][0].update(total=0),
        lambda x: x["in_progress"][0].update(extra="not-allowed"),
    ],
)
def test_v5_rejects_unknown_identity_or_invalid_state(change: object) -> None:
    manifest = copy.deepcopy(_manifest())
    semantics = manifest["semantic_expectations"]
    assert isinstance(semantics, dict)
    state = semantics["post_scan_movie_state"]
    assert isinstance(state, dict) and callable(change)
    change(state)
    with pytest.raises(PackagedFixtureError, match="post-scan state"):
        _parse_semantics(MappingProxyType(manifest), slug="small-movies-v5")
