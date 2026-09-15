import inspect
import json
from collections.abc import Callable
from uuid import UUID

import pytest

from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.domain import (
    ProcessObservationKind,
    ReadinessProbeKind,
    RuntimeExecutionProfile,
    RuntimeKind,
)
from kodi_lab.kodi.fixtures import (
    FixtureInitializationTimeoutError,
    SmallMixedFixtureKodiAdapter,
)
from kodi_lab.kodi.readiness import JsonRpcInvalidResponseError

REVISION_ID = UUID("00000000-0000-4000-8000-000000000002")


def profile() -> RuntimeExecutionProfile:
    return RuntimeExecutionProfile(
        runtime_revision_id=REVISION_ID,
        runtime_kind=RuntimeKind.KODI,
        expected_application_name="Kodi",
        expected_kodi_version="21.3",
        expected_version_tag="stable",
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
    }
    for name in {
        "scan_assigned_fixture_library",
        "get_fixture_library_state",
        "remove_fixture_movie_for_reset_test",
    }:
        assert list(inspect.signature(public[name]).parameters) == ["self"]
