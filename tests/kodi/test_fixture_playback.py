import json
from copy import deepcopy

import pytest

from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.kodi.fixtures import SmallMixedFixtureKodiAdapter
from kodi_lab.kodi.readiness import JsonRpcInvalidResponseError
from tests.kodi.test_fixture_adapter import observation, profile


class PlaybackTransport:
    def __init__(
        self,
        *,
        bad_identity: bool = False,
        bad_movie_id: bool = False,
        bad_readback: bool = False,
    ) -> None:
        self.semantics = load_packaged_fixture("small-movies-v5").semantics
        self.calls: list[dict[str, object]] = []
        self.bad_identity = bad_identity
        self.bad_movie_id = bad_movie_id
        self.bad_readback = bad_readback
        self.movies = [
            {
                "movieid": index,
                "title": movie.title,
                "year": movie.year,
                "uniqueid": {"kodi-lab": movie.unique_id},
                "file": movie.file,
                "genre": list(movie.genres or ()),
                "cast": [{"name": name} for name in movie.actors or ()],
                "director": [movie.director],
                "studio": [movie.studio],
                "rating": movie.rating,
                "set": movie.set_name or "",
                "tag": list(movie.tags or ()),
                "playcount": 0,
                "lastplayed": "",
                "resume": {"position": 0, "total": 0},
            }
            for index, movie in enumerate(self.semantics.movies, 1)
        ]

    def post(self, **kwargs: object) -> bytes:
        body = kwargs["body"]
        assert isinstance(body, bytes)
        request = json.loads(body)
        self.calls.append(request)
        method = request["method"]
        params = request["params"]
        if method == "VideoLibrary.Scan":
            result: object = "OK"
        elif method == "VideoLibrary.GetMovies":
            movies = deepcopy(self.movies)
            if (
                self.bad_identity
                and request["id"] == "kodi-lab-fixture-state-identities"
            ):
                movies[0]["uniqueid"] = {"kodi-lab": "unexpected"}
            if (
                self.bad_movie_id
                and request["id"] == "kodi-lab-fixture-state-identities"
            ):
                movies[0]["movieid"] = 0
            result = {"movies": movies}
        elif method == "VideoLibrary.GetTVShows":
            result = {"tvshows": []}
        elif method == "VideoLibrary.GetEpisodes":
            result = {"episodes": []}
        elif method == "VideoLibrary.SetMovieDetails":
            movie = self.movies[params["movieid"] - 1]
            movie["playcount"] = params["playcount"]
            movie["resume"] = params["resume"]
            movie["lastplayed"] = params.get("lastplayed", "")
            result = "OK"
        elif method == "VideoLibrary.GetMovieDetails":
            movie = deepcopy(self.movies[params["movieid"] - 1])
            if self.bad_readback:
                movie["playcount"] = 0
            result = {"moviedetails": movie}
        else:
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
        ).encode()


def _adapter(transport: PlaybackTransport) -> SmallMixedFixtureKodiAdapter:
    return SmallMixedFixtureKodiAdapter(
        observation(), profile("20.5"), transport.semantics, transport=transport
    )


def test_nexus_state_is_applied_only_after_scan_and_exact_readback() -> None:
    transport = PlaybackTransport()
    adapter = _adapter(transport)
    with pytest.raises(ValueError, match="not ready"):
        adapter.apply_prescribed_movie_playback()
    adapter.scan_assigned_fixture_library()
    baseline = adapter.wait_for_fixture_state()
    assert baseline.is_scan_baseline and not baseline.is_baseline
    adapter.apply_prescribed_movie_playback()
    assert adapter.get_fixture_library_state().is_baseline
    mutations = [
        c for c in transport.calls if c["method"] == "VideoLibrary.SetMovieDetails"
    ]
    readbacks = [
        c for c in transport.calls if c["method"] == "VideoLibrary.GetMovieDetails"
    ]
    assert len(mutations) == len(readbacks) == 7
    for call in mutations:
        params = call["params"]
        assert isinstance(params, dict)
        assert set(params) <= {"movieid", "playcount", "lastplayed", "resume"}
        assert isinstance(params["movieid"], int)
    with pytest.raises(ValueError, match="not ready"):
        adapter.apply_prescribed_movie_playback()


def test_mismatched_identity_fails_before_any_mutation() -> None:
    transport = PlaybackTransport(bad_identity=True)
    adapter = _adapter(transport)
    adapter.scan_assigned_fixture_library()
    assert adapter.wait_for_fixture_state().is_scan_baseline
    with pytest.raises(JsonRpcInvalidResponseError, match="identities changed"):
        adapter.apply_prescribed_movie_playback()
    assert not any(
        c["method"] == "VideoLibrary.SetMovieDetails" for c in transport.calls
    )


def test_bad_readback_fails_closed() -> None:
    transport = PlaybackTransport(bad_readback=True)
    adapter = _adapter(transport)
    adapter.scan_assigned_fixture_library()
    adapter.wait_for_fixture_state()
    with pytest.raises(JsonRpcInvalidResponseError, match="readback differs"):
        adapter.apply_prescribed_movie_playback()


def test_invalid_kodi_movie_id_fails_before_mutation() -> None:
    transport = PlaybackTransport(bad_movie_id=True)
    adapter = _adapter(transport)
    adapter.scan_assigned_fixture_library()
    adapter.wait_for_fixture_state()
    with pytest.raises(JsonRpcInvalidResponseError, match="movie identity is invalid"):
        adapter.apply_prescribed_movie_playback()
    assert not any(
        c["method"] == "VideoLibrary.SetMovieDetails" for c in transport.calls
    )


def test_playback_contract_rejected_on_unproven_release() -> None:
    transport = PlaybackTransport()
    with pytest.raises(ValueError, match=r"Nexus 20\.5 only"):
        SmallMixedFixtureKodiAdapter(
            observation(), profile("21.3"), transport.semantics, transport=transport
        )
