import inspect
import json
from collections.abc import Callable
from uuid import UUID

import pytest

from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.domain import (
    ProcessObservationKind,
    PythonBytecodePolicy,
    ReadinessProbeKind,
    RuntimeExecutionProfile,
    RuntimeKind,
)
from kodi_lab.fixtures.package import load_packaged_fixture
from kodi_lab.kodi.fixtures import (
    FixtureInitializationEvent,
    FixtureInitializationTimeoutError,
    SmallMixedFixtureKodiAdapter,
)
from kodi_lab.kodi.readiness import JsonRpcInvalidResponseError

REVISION_ID = UUID("00000000-0000-4000-8000-000000000002")


def profile(kodi_version: str = "21.3") -> RuntimeExecutionProfile:
    return RuntimeExecutionProfile(
        runtime_revision_id=REVISION_ID,
        runtime_kind=RuntimeKind.KODI,
        runtime_uid=1000,
        runtime_gid=1000,
        python_bytecode_policy=PythonBytecodePolicy.INTERPRETER_DEFAULT,
        expected_application_name="Kodi",
        expected_kodi_version=(
            "22.0" if kodi_version == "22.0-beta.2" else kodi_version
        ),
        expected_version_tag=("beta" if kodi_version == "22.0-beta.2" else "stable"),
        expected_prerelease_ordinal=(2 if kodi_version == "22.0-beta.2" else None),
        jsonrpc_port=8080,
        jsonrpc_path="/jsonrpc",
        process_observation=ProcessObservationKind.DOCKER_HEALTHCHECK,
        readiness_probe=ReadinessProbeKind.KODI_JSONRPC,
        readiness_timeout_seconds=90,
        poll_interval_seconds=1,
        http_timeout_seconds=3,
    )


def observation() -> ObserveManagedKodiRuntimeResult:
    return ObserveManagedKodiRuntimeResult(
        managed_instance_id="00000000-0000-4000-8000-000000000001",
        runtime_revision_id=str(REVISION_ID),
        container_id="a" * 64,
        runtime_state=ContainerRuntimeState.RUNNING,
        process_state=KodiProcessState.RUNNING,
        ipv4_address="10.250.0.2",
        jsonrpc_port=8080,
    )


def item_result(method: str) -> dict[str, object]:
    if method == "VideoLibrary.GetMovies":
        return {
            "movies": [
                {
                    "movieid": 7,
                    "title": "Kodi Lab Fixture Movie",
                    "year": 2024,
                    "uniqueid": {"kodi-lab": "small-mixed-v1-movie"},
                    "file": (
                        "/home/kodi/.kodi/fixture-media/movies/"
                        "kodi-lab-fixture-movie/Kodi Lab Fixture Movie (2024).mp4"
                    ),
                }
            ]
        }
    if method == "VideoLibrary.GetTVShows":
        return {
            "tvshows": [
                {
                    "tvshowid": 8,
                    "title": "Kodi Lab Fixture Series",
                    "year": 2024,
                    "uniqueid": {"kodi-lab": "small-mixed-v1-series"},
                    "file": (
                        "/home/kodi/.kodi/fixture-media/tvshows/"
                        "kodi-lab-fixture-series/"
                    ),
                }
            ]
        }
    if method == "VideoLibrary.GetEpisodes":
        return {
            "episodes": [
                {
                    "episodeid": 9,
                    "title": "Kodi Lab Fixture Episode",
                    "showtitle": "Kodi Lab Fixture Series",
                    "season": 1,
                    "episode": 1,
                    "uniqueid": {"kodi-lab": "small-mixed-v1-episode"},
                    "file": (
                        "/home/kodi/.kodi/fixture-media/tvshows/"
                        "kodi-lab-fixture-series/Season 01/"
                        "Kodi Lab Fixture Series S01E01.mp4"
                    ),
                }
            ]
        }
    raise AssertionError(method)


class FakeTransport:
    def __init__(
        self,
        mutate: Callable[[str, dict[str, object]], object] | None = None,
    ) -> None:
        self.requests: list[dict[str, object]] = []
        self.mutate = mutate
        self.movie_removed = False

    def post(self, **kwargs: object) -> bytes:
        body = kwargs["body"]
        assert isinstance(body, bytes)
        request = json.loads(body)
        self.requests.append(request)
        method = request["method"]
        if self.mutate is not None:
            result = self.mutate(method, request)
        elif method == "VideoLibrary.Scan":
            result = "OK"
        elif method == "VideoLibrary.RemoveMovie":
            self.movie_removed = True
            result = "OK"
        elif method == "VideoLibrary.GetMovies" and self.movie_removed:
            result = {"movies": []}
        else:
            result = item_result(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
        ).encode()


def test_fixed_scan_and_exact_semantic_fixture_success() -> None:
    transport = FakeTransport()
    adapter = SmallMixedFixtureKodiAdapter(
        observation(), profile(), transport=transport
    )
    adapter.scan_assigned_fixture_library()
    state = adapter.wait_for_fixture_state()

    assert state.is_baseline
    scan = transport.requests[0]
    assert scan == {
        "jsonrpc": "2.0",
        "id": "kodi-lab-fixture-scan-movies",
        "method": "VideoLibrary.Scan",
        "params": {
            "directory": "/home/kodi/.kodi/fixture-media/movies/",
            "showdialogs": False,
        },
    }
    assert [request["method"] for request in transport.requests[1:4]] == [
        "VideoLibrary.GetMovies",
        "VideoLibrary.GetTVShows",
        "VideoLibrary.GetEpisodes",
    ]
    assert transport.requests[4] == {
        "jsonrpc": "2.0",
        "id": "kodi-lab-fixture-scan-tv",
        "method": "VideoLibrary.Scan",
        "params": {
            "directory": "/home/kodi/.kodi/fixture-media/tvshows/",
            "showdialogs": False,
        },
    }


def test_same_fixed_semantic_adapter_supports_reviewed_releases() -> None:
    nexus = SmallMixedFixtureKodiAdapter(
        observation(), profile("20.5"), transport=FakeTransport()
    )
    assert nexus.get_fixture_library_state().is_baseline
    matrix = SmallMixedFixtureKodiAdapter(
        observation(), profile("19.5"), transport=FakeTransport()
    )
    assert matrix.get_fixture_library_state().is_baseline
    preview = SmallMixedFixtureKodiAdapter(
        observation(), profile("22.0-beta.2"), transport=FakeTransport()
    )
    assert preview.get_fixture_library_state().is_baseline
    with pytest.raises(ValueError, match="only proven releases"):
        SmallMixedFixtureKodiAdapter(
            observation(), profile("22.0"), transport=FakeTransport()
        )


def test_wrong_fixture_media_path_is_not_semantic_success() -> None:
    def wrong_path(method: str, _request: dict[str, object]) -> object:
        result = item_result(method)
        key = {
            "VideoLibrary.GetMovies": "movies",
            "VideoLibrary.GetTVShows": "tvshows",
            "VideoLibrary.GetEpisodes": "episodes",
        }[method]
        items = result[key]
        assert isinstance(items, list)
        items[0]["file"] = "/untrusted/path"
        return result

    state = SmallMixedFixtureKodiAdapter(
        observation(), profile(), transport=FakeTransport(wrong_path)
    ).get_fixture_library_state()
    assert not state.is_baseline


@pytest.mark.parametrize(
    "missing", ["VideoLibrary.GetMovies", "VideoLibrary.GetEpisodes"]
)
def test_missing_movie_or_episode_is_not_fixture_baseline(missing: str) -> None:
    transport = FakeTransport(
        lambda method, _request: {} if method == missing else item_result(method)
    )
    state = SmallMixedFixtureKodiAdapter(
        observation(), profile(), transport=transport
    ).get_fixture_library_state()
    assert not state.is_baseline


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"jsonrpc":"2.0","id":"wrong","result":{}}',
        b'{"jsonrpc":"2.0","id":"kodi-lab-fixture-movies","error":{}}',
        b'{"jsonrpc":"2.0","id":"kodi-lab-fixture-movies","result":{"movies":{}}}',
    ],
)
def test_malformed_or_error_responses_fail_closed(payload: bytes) -> None:
    class BrokenTransport:
        def post(self, **_kwargs: object) -> bytes:
            return payload

    adapter = SmallMixedFixtureKodiAdapter(
        observation(), profile(), transport=BrokenTransport()
    )
    with pytest.raises(JsonRpcInvalidResponseError):
        adapter.get_fixture_library_state()


def test_fixture_wait_is_bounded() -> None:
    transport = FakeTransport(lambda _method, _request: {})
    ticks = iter([0.0, 0.0, 0.4, 0.8, 1.1, 1.2])
    adapter = SmallMixedFixtureKodiAdapter(
        observation(), profile(), transport=transport
    )
    with pytest.raises(FixtureInitializationTimeoutError):
        adapter.wait_for_fixture_state(
            deadline_seconds=1.0,
            clock=lambda: next(ticks),
            sleep=lambda _seconds: None,
        )


def test_bounded_drift_removes_only_known_fixture_movie() -> None:
    transport = FakeTransport()
    state = SmallMixedFixtureKodiAdapter(
        observation(), profile(), transport=transport
    ).remove_fixture_movie_for_reset_test()
    assert not state.movie_present
    assert state.series_present
    assert state.episode_present
    removal = next(
        request
        for request in transport.requests
        if request["method"] == "VideoLibrary.RemoveMovie"
    )
    assert removal["params"] == {"movieid": 7}


def test_public_adapter_surface_accepts_no_method_path_or_entity_identity() -> None:
    public = {
        name: value
        for name, value in inspect.getmembers(
            SmallMixedFixtureKodiAdapter, inspect.isfunction
        )
        if not name.startswith("_")
    }
    assert set(public) == {
        "scan_assigned_fixture_library",
        "get_fixture_library_state",
        "wait_for_fixture_state",
        "remove_fixture_movie_for_reset_test",
        "remove_fixture_episode_for_reset_test",
        "apply_fixture_drift_plan_for_reset_test",
    }
    for name in {
        "scan_assigned_fixture_library",
        "get_fixture_library_state",
        "remove_fixture_movie_for_reset_test",
        "remove_fixture_episode_for_reset_test",
        "apply_fixture_drift_plan_for_reset_test",
    }:
        assert list(inspect.signature(public[name]).parameters) == ["self"]


def _medium_result(method: str) -> dict[str, object]:
    semantics = load_packaged_fixture("medium-mixed-v1").semantics
    if method == "VideoLibrary.GetMovies":
        return {
            "movies": [
                {
                    "movieid": index,
                    "title": movie.title,
                    "year": movie.year,
                    "uniqueid": {"kodi-lab": movie.unique_id},
                    "file": movie.file,
                }
                for index, movie in enumerate(semantics.movies, 101)
            ]
        }
    if method == "VideoLibrary.GetTVShows":
        return {
            "tvshows": [
                {
                    "tvshowid": index,
                    "title": show.title,
                    "year": show.year,
                    "uniqueid": {"kodi-lab": show.unique_id},
                    "file": show.directory,
                }
                for index, show in enumerate(semantics.series, 201)
            ]
        }
    if method == "VideoLibrary.GetEpisodes":
        return {
            "episodes": [
                {
                    "episodeid": index,
                    "title": episode.title,
                    "showtitle": episode.show_title,
                    "season": episode.season,
                    "episode": episode.episode,
                    "uniqueid": {"kodi-lab": episode.unique_id},
                    "file": episode.file,
                }
                for index, episode in enumerate(semantics.episodes, 301)
            ]
        }
    raise AssertionError(method)


def test_medium_semantics_verify_exact_25_5_50_with_bounded_calls() -> None:
    events: list[FixtureInitializationEvent] = []
    transport = FakeTransport(
        lambda method, _request: (
            "OK" if method == "VideoLibrary.Scan" else _medium_result(method)
        )
    )
    adapter = SmallMixedFixtureKodiAdapter(
        observation(),
        profile(),
        load_packaged_fixture("medium-mixed-v1").semantics,
        transport=transport,
        event_observer=events.append,
    )

    adapter.scan_assigned_fixture_library()
    state = adapter.wait_for_fixture_state()

    assert state.is_baseline
    assert (state.movie_count, state.series_count, state.episode_count) == (25, 5, 50)
    methods = [request["method"] for request in transport.requests]
    assert methods.count("VideoLibrary.Scan") == 2
    assert methods.count("VideoLibrary.GetMovies") == 2
    assert methods.count("VideoLibrary.GetTVShows") == 2
    assert methods.count("VideoLibrary.GetEpisodes") == 2
    assert events.count(FixtureInitializationEvent.MOVIES_SCAN_STARTED) == 1
    assert events.count(FixtureInitializationEvent.MOVIES_SCAN_COMPLETED) == 1
    assert events.count(FixtureInitializationEvent.TV_SCAN_STARTED) == 1
    assert events.count(FixtureInitializationEvent.TV_SCAN_COMPLETED) == 1
    assert events.count(FixtureInitializationEvent.SEMANTIC_VERIFICATION_STARTED) == 2
    assert events.count(FixtureInitializationEvent.SEMANTIC_VERIFICATION_COMPLETED) == 2


@pytest.mark.parametrize(
    "failure",
    [
        "missing_movie",
        "unexpected_movie",
        "duplicate_movie",
        "missing_show",
        "extra_show",
        "missing_episode",
        "extra_episode",
        "duplicate_episode",
        "wrong_show",
        "wrong_season",
        "wrong_episode",
    ],
)
def test_medium_semantics_fail_closed_on_identity_or_relationship_drift(
    failure: str,
) -> None:
    def wrong(method: str, _request: dict[str, object]) -> object:
        result = _medium_result(method)
        if method == "VideoLibrary.GetMovies":
            movies = result["movies"]
            assert isinstance(movies, list)
            if failure == "missing_movie":
                movies.pop()
            if failure == "unexpected_movie":
                movies.append({"title": "Unexpected"})
            if failure == "duplicate_movie":
                movies[1] = dict(movies[0])
        if method == "VideoLibrary.GetTVShows":
            shows = result["tvshows"]
            assert isinstance(shows, list)
            if failure == "missing_show":
                shows.pop()
            if failure == "extra_show":
                shows.append({"title": "Unexpected"})
        if method == "VideoLibrary.GetEpisodes":
            episodes = result["episodes"]
            assert isinstance(episodes, list)
            if failure == "missing_episode":
                episodes.pop()
            if failure == "extra_episode":
                episodes.append({"title": "Unexpected"})
            if failure == "duplicate_episode":
                episodes[1] = dict(episodes[0])
            if failure == "wrong_show":
                episodes[0]["showtitle"] = "Unexpected"
            if failure == "wrong_season":
                episodes[0]["season"] = 9
            if failure == "wrong_episode":
                episodes[0]["episode"] = 9
        return result

    adapter = SmallMixedFixtureKodiAdapter(
        observation(),
        profile(),
        load_packaged_fixture("medium-mixed-v1").semantics,
        transport=FakeTransport(wrong),
    )
    assert not adapter.get_fixture_library_state().is_baseline


def test_medium_combined_drift_removes_only_package_defined_targets() -> None:
    class MediumDriftTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.episode_removed = False

        def post(self, **kwargs: object) -> bytes:
            body = kwargs["body"]
            assert isinstance(body, bytes)
            request = json.loads(body)
            self.requests.append(request)
            method = request["method"]
            if method == "VideoLibrary.RemoveMovie":
                self.movie_removed = True
                result: object = "OK"
            elif method == "VideoLibrary.RemoveEpisode":
                self.episode_removed = True
                result = "OK"
            else:
                result = _medium_result(method)
                if method == "VideoLibrary.GetMovies" and self.movie_removed:
                    result["movies"] = result["movies"][1:]  # type: ignore[index]
                if method == "VideoLibrary.GetEpisodes" and self.episode_removed:
                    result["episodes"] = result["episodes"][1:]  # type: ignore[index]
            return json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "result": result}
            ).encode()

    transport = MediumDriftTransport()
    adapter = SmallMixedFixtureKodiAdapter(
        observation(),
        profile(),
        load_packaged_fixture("medium-mixed-v1").semantics,
        transport=transport,
    )
    state = adapter.apply_fixture_drift_plan_for_reset_test()

    assert (state.movie_count, state.series_count, state.episode_count) == (24, 5, 49)
    removals = [
        request
        for request in transport.requests
        if request["method"]
        in {"VideoLibrary.RemoveMovie", "VideoLibrary.RemoveEpisode"}
    ]
    assert [request["params"] for request in removals] == [
        {"movieid": 101},
        {"episodeid": 301},
    ]


def _movies_only_result(method: str) -> dict[str, object]:
    semantics = load_packaged_fixture("small-movies-v1").semantics
    if method == "VideoLibrary.GetMovies":
        return {
            "movies": [
                {
                    "movieid": index,
                    "title": movie.title,
                    "year": movie.year,
                    "uniqueid": {"kodi-lab": movie.unique_id},
                    "file": movie.file,
                }
                for index, movie in enumerate(semantics.movies, 11)
            ]
        }
    if method == "VideoLibrary.GetTVShows":
        return {"tvshows": []}
    if method == "VideoLibrary.GetEpisodes":
        return {"episodes": []}
    raise AssertionError(method)


def test_movies_only_semantics_verify_three_exact_movies_and_scan_no_tv() -> None:
    transport = FakeTransport(
        lambda method, _request: (
            "OK" if method == "VideoLibrary.Scan" else _movies_only_result(method)
        )
    )
    adapter = SmallMixedFixtureKodiAdapter(
        observation(),
        profile(),
        load_packaged_fixture("small-movies-v1").semantics,
        transport=transport,
    )

    adapter.scan_assigned_fixture_library()
    state = adapter.wait_for_fixture_state()

    assert state.is_baseline
    assert (state.movie_count, state.series_count, state.episode_count) == (3, 0, 0)
    scans = [
        request
        for request in transport.requests
        if request["method"] == "VideoLibrary.Scan"
    ]
    assert len(scans) == 1
    assert scans[0]["params"] == {
        "directory": "/home/kodi/.kodi/fixture-media/movies/",
        "showdialogs": False,
    }


@pytest.mark.parametrize("failure", ["missing", "unexpected", "tvshow", "episode"])
def test_movies_only_semantics_fail_closed_on_wrong_entities(failure: str) -> None:
    def wrong(method: str, _request: dict[str, object]) -> object:
        result = _movies_only_result(method)
        if method == "VideoLibrary.GetMovies" and failure == "missing":
            movies = result["movies"]
            assert isinstance(movies, list)
            result["movies"] = movies[:-1]
        if method == "VideoLibrary.GetMovies" and failure == "unexpected":
            movies = result["movies"]
            assert isinstance(movies, list)
            movies.append({"title": "Unexpected"})
        if method == "VideoLibrary.GetTVShows" and failure == "tvshow":
            result["tvshows"] = item_result(method)["tvshows"]
        if method == "VideoLibrary.GetEpisodes" and failure == "episode":
            result["episodes"] = item_result(method)["episodes"]
        return result

    adapter = SmallMixedFixtureKodiAdapter(
        observation(),
        profile(),
        load_packaged_fixture("small-movies-v1").semantics,
        transport=FakeTransport(wrong),
    )
    assert not adapter.get_fixture_library_state().is_baseline


def test_movies_only_bounded_drift_removes_one_trusted_movie() -> None:
    class MoviesTransport(FakeTransport):
        def post(self, **kwargs: object) -> bytes:
            body = kwargs["body"]
            assert isinstance(body, bytes)
            request = json.loads(body)
            self.requests.append(request)
            method = request["method"]
            if method == "VideoLibrary.RemoveMovie":
                self.movie_removed = True
                result: object = "OK"
            else:
                result = _movies_only_result(method)
                if method == "VideoLibrary.GetMovies" and self.movie_removed:
                    result["movies"] = result["movies"][1:]  # type: ignore[index]
            return json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "result": result}
            ).encode()

    transport = MoviesTransport()
    adapter = SmallMixedFixtureKodiAdapter(
        observation(),
        profile(),
        load_packaged_fixture("small-movies-v1").semantics,
        transport=transport,
    )
    state = adapter.remove_fixture_movie_for_reset_test()
    assert (state.movie_count, state.series_count, state.episode_count) == (2, 0, 0)
    removal = next(
        request
        for request in transport.requests
        if request["method"] == "VideoLibrary.RemoveMovie"
    )
    assert removal["params"] == {"movieid": 11}


def _tv_only_result(method: str) -> dict[str, object]:
    semantics = load_packaged_fixture("small-tv-v1").semantics
    if method == "VideoLibrary.GetMovies":
        return {"movies": []}
    if method == "VideoLibrary.GetTVShows":
        show = semantics.series[0]
        return {
            "tvshows": [
                {
                    "tvshowid": 21,
                    "title": show.title,
                    "year": show.year,
                    "uniqueid": {"kodi-lab": show.unique_id},
                    "file": show.directory,
                }
            ]
        }
    if method == "VideoLibrary.GetEpisodes":
        return {
            "episodes": [
                {
                    "episodeid": index + 30,
                    "title": episode.title,
                    "showtitle": episode.show_title,
                    "season": episode.season,
                    "episode": episode.episode,
                    "uniqueid": {"kodi-lab": episode.unique_id},
                    "file": episode.file,
                }
                for index, episode in enumerate(semantics.episodes, 1)
            ]
        }
    raise AssertionError(method)


def test_tv_only_semantics_verify_relationships_and_scan_only_tv() -> None:
    transport = FakeTransport(
        lambda method, _request: (
            "OK" if method == "VideoLibrary.Scan" else _tv_only_result(method)
        )
    )
    adapter = SmallMixedFixtureKodiAdapter(
        observation(),
        profile(),
        load_packaged_fixture("small-tv-v1").semantics,
        transport=transport,
    )

    adapter.scan_assigned_fixture_library()
    state = adapter.wait_for_fixture_state()

    assert state.is_baseline
    assert (state.movie_count, state.series_count, state.episode_count) == (0, 1, 3)
    scans = [
        request
        for request in transport.requests
        if request["method"] == "VideoLibrary.Scan"
    ]
    assert scans == [
        {
            "jsonrpc": "2.0",
            "id": "kodi-lab-fixture-scan-tv",
            "method": "VideoLibrary.Scan",
            "params": {
                "directory": "/home/kodi/.kodi/fixture-media/tvshows/",
                "showdialogs": False,
            },
        }
    ]


@pytest.mark.parametrize(
    "failure",
    [
        "unexpected_movie",
        "missing_show",
        "extra_show",
        "missing_episode",
        "extra_episode",
        "wrong_season",
        "wrong_episode",
        "wrong_show",
        "malformed",
    ],
)
def test_tv_only_semantics_fail_closed_on_wrong_entities(failure: str) -> None:
    def wrong(method: str, _request: dict[str, object]) -> object:
        result = _tv_only_result(method)
        if method == "VideoLibrary.GetMovies" and failure == "unexpected_movie":
            result = item_result(method)
        if method == "VideoLibrary.GetTVShows" and failure == "missing_show":
            result["tvshows"] = []
        if method == "VideoLibrary.GetTVShows" and failure == "extra_show":
            shows = result["tvshows"]
            assert isinstance(shows, list)
            shows.append({"title": "Unexpected"})
        if method == "VideoLibrary.GetEpisodes":
            episodes = result["episodes"]
            assert isinstance(episodes, list)
            if failure == "missing_episode":
                episodes.pop()
            if failure == "extra_episode":
                episodes.append({"title": "Unexpected"})
            if failure == "wrong_season":
                episodes[0]["season"] = 2
            if failure == "wrong_episode":
                episodes[0]["episode"] = 8
            if failure == "wrong_show":
                episodes[0]["showtitle"] = "Unexpected"
            if failure == "malformed":
                result["episodes"] = {}
        return result

    adapter = SmallMixedFixtureKodiAdapter(
        observation(),
        profile(),
        load_packaged_fixture("small-tv-v1").semantics,
        transport=FakeTransport(wrong),
    )
    if failure == "malformed":
        with pytest.raises(JsonRpcInvalidResponseError):
            adapter.get_fixture_library_state()
    else:
        assert not adapter.get_fixture_library_state().is_baseline


def test_tv_only_bounded_drift_removes_predefined_episode_one() -> None:
    class TvTransport(FakeTransport):
        def post(self, **kwargs: object) -> bytes:
            body = kwargs["body"]
            assert isinstance(body, bytes)
            request = json.loads(body)
            self.requests.append(request)
            method = request["method"]
            if method == "VideoLibrary.RemoveEpisode":
                self.movie_removed = True
                result: object = "OK"
            else:
                result = _tv_only_result(method)
                if method == "VideoLibrary.GetEpisodes" and self.movie_removed:
                    episodes = result["episodes"]
                    assert isinstance(episodes, list)
                    result["episodes"] = episodes[1:]
            return json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "result": result}
            ).encode()

    transport = TvTransport()
    adapter = SmallMixedFixtureKodiAdapter(
        observation(),
        profile(),
        load_packaged_fixture("small-tv-v1").semantics,
        transport=transport,
    )
    state = adapter.remove_fixture_episode_for_reset_test()

    assert (state.movie_count, state.series_count, state.episode_count) == (0, 1, 2)
    removal = next(
        request
        for request in transport.requests
        if request["method"] == "VideoLibrary.RemoveEpisode"
    )
    assert removal["params"] == {"episodeid": 31}
