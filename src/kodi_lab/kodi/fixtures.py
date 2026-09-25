"""Unprivileged fixed-method Kodi adapter for trusted fixture semantics."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, cast

from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.fixtures.seed_audit import (
    FIXTURE_MOVIE_DIRECTORY,
    FIXTURE_TV_DIRECTORY,
)
from kodi_lab.fixtures.semantics import (
    MIXED_FIXTURE_SEMANTICS,
    FixtureEpisodeExpectation,
    FixtureMovieExpectation,
    FixtureSemanticSpecification,
    FixtureSeriesExpectation,
)
from kodi_lab.kodi.readiness import (
    JsonRpcInvalidResponseError,
    _BoundedHttpTransport,
    _HttpTransport,
)

FIXTURE_INITIALIZATION_TIMEOUT_SECONDS = 60.0
FIXTURE_POLL_INTERVAL_SECONDS = 1.0
FIXTURE_MOVIE_TITLE = "Kodi Lab Fixture Movie"
FIXTURE_MOVIE_YEAR = 2024
FIXTURE_MOVIE_UNIQUE_ID = "small-mixed-v1-movie"
FIXTURE_SERIES_TITLE = "Kodi Lab Fixture Series"
FIXTURE_SERIES_YEAR = 2024
FIXTURE_SERIES_UNIQUE_ID = "small-mixed-v1-series"
FIXTURE_EPISODE_TITLE = "Kodi Lab Fixture Episode"
FIXTURE_EPISODE_UNIQUE_ID = "small-mixed-v1-episode"
FIXTURE_MOVIE_FILE = (
    FIXTURE_MOVIE_DIRECTORY + "kodi-lab-fixture-movie/Kodi Lab Fixture Movie (2024).mp4"
)
FIXTURE_SERIES_DIRECTORY = FIXTURE_TV_DIRECTORY + "kodi-lab-fixture-series/"
FIXTURE_EPISODE_FILE = (
    FIXTURE_SERIES_DIRECTORY + "Season 01/Kodi Lab Fixture Series S01E01.mp4"
)


class FixtureInitializationTimeoutError(RuntimeError):
    """Kodi did not construct the expected fixture library within the deadline."""


class FixtureInitializationEvent(StrEnum):
    """Bounded ephemeral timing points for fixture validation instrumentation."""

    MOVIES_SCAN_STARTED = "movies_scan_started"
    MOVIES_SCAN_COMPLETED = "movies_scan_completed"
    TV_SCAN_STARTED = "tv_scan_started"
    TV_SCAN_COMPLETED = "tv_scan_completed"
    SEMANTIC_VERIFICATION_STARTED = "semantic_verification_started"
    SEMANTIC_VERIFICATION_COMPLETED = "semantic_verification_completed"


@dataclass(frozen=True, slots=True)
class FixtureLibraryState:
    movie_present: bool
    series_present: bool
    episode_present: bool
    movie_count: int
    series_count: int
    episode_count: int
    expected_movie_count: int = 1
    expected_series_count: int = 1
    expected_episode_count: int = 1

    @property
    def is_baseline(self) -> bool:
        return (
            self.movie_present == (self.expected_movie_count > 0)
            and self.series_present == (self.expected_series_count > 0)
            and self.episode_present == (self.expected_episode_count > 0)
            and self.movie_count == self.expected_movie_count
            and self.series_count == self.expected_series_count
            and self.episode_count == self.expected_episode_count
        )


class SmallMixedFixtureKodiAdapter:
    """Expose no caller-directed method, path, title, ID, or JSON-RPC params."""

    def __init__(
        self,
        observation: ObserveManagedKodiRuntimeResult,
        profile: RuntimeExecutionProfile,
        semantics: FixtureSemanticSpecification = MIXED_FIXTURE_SEMANTICS,
        *,
        timeout: float | None = None,
        transport: _HttpTransport | None = None,
        event_observer: Callable[[FixtureInitializationEvent], None] | None = None,
    ) -> None:
        if observation.runtime_revision_id != str(profile.runtime_revision_id):
            raise ValueError("observation and execution profile revisions differ")
        if observation.jsonrpc_port != profile.jsonrpc_port:
            raise ValueError("observation and execution profile ports differ")
        if profile.kodi_release.canonical not in {
            "19.5",
            "20.5",
            "21.3",
            "22.0-beta.2",
        }:
            raise ValueError(
                "small-mixed fixture adapter supports only proven releases"
            )
        self._host = observation.ipv4_address
        self._port = profile.jsonrpc_port
        self._path = profile.jsonrpc_path
        self._timeout = profile.http_timeout_seconds if timeout is None else timeout
        if not 0 < self._timeout <= profile.http_timeout_seconds:
            raise ValueError("HTTP timeout exceeds trusted execution profile")
        self._transport = transport or _BoundedHttpTransport()
        self._semantics = semantics
        self._event_observer = event_observer
        self._movie_scan_requested = False
        self._tv_scan_requested = False
        self._movie_scan_completed = False
        self._tv_scan_completed = False

    def scan_assigned_fixture_library(self) -> None:
        if self._semantics.scan_movies:
            self._emit(FixtureInitializationEvent.MOVIES_SCAN_STARTED)
            document = self._request(
                "kodi-lab-fixture-scan-movies",
                "VideoLibrary.Scan",
                {"directory": FIXTURE_MOVIE_DIRECTORY, "showdialogs": False},
            )
            if document.get("result") != "OK":
                raise JsonRpcInvalidResponseError("fixture scan result is invalid")
            self._movie_scan_requested = True
            self._tv_scan_requested = False
        elif self._semantics.scan_tv:
            self._movie_scan_requested = False
            self._scan_tv_fixture_library()
        else:
            raise ValueError("trusted fixture scan plan is empty")

    def _scan_tv_fixture_library(self) -> None:
        self._emit(FixtureInitializationEvent.TV_SCAN_STARTED)
        document = self._request(
            "kodi-lab-fixture-scan-tv",
            "VideoLibrary.Scan",
            {"directory": FIXTURE_TV_DIRECTORY, "showdialogs": False},
        )
        if document.get("result") != "OK":
            raise JsonRpcInvalidResponseError("fixture TV scan result is invalid")
        self._tv_scan_requested = True

    def get_fixture_library_state(self) -> FixtureLibraryState:
        self._emit(FixtureInitializationEvent.SEMANTIC_VERIFICATION_STARTED)
        try:
            movies = self._items(
                self._request(
                    "kodi-lab-fixture-movies",
                    "VideoLibrary.GetMovies",
                    {"properties": ["title", "year", "uniqueid", "file"]},
                ),
                "movies",
            )
            shows = self._items(
                self._request(
                    "kodi-lab-fixture-shows",
                    "VideoLibrary.GetTVShows",
                    {"properties": ["title", "year", "uniqueid", "file"]},
                ),
                "tvshows",
            )
            episodes = self._items(
                self._request(
                    "kodi-lab-fixture-episodes",
                    "VideoLibrary.GetEpisodes",
                    {
                        "properties": [
                            "title",
                            "showtitle",
                            "season",
                            "episode",
                            "uniqueid",
                            "file",
                        ]
                    },
                ),
                "episodes",
            )
            return FixtureLibraryState(
                movie_present=bool(self._semantics.movies)
                and self._movies_match(movies, self._semantics.movies),
                series_present=bool(self._semantics.series)
                and self._series_match(shows, self._semantics.series),
                episode_present=bool(self._semantics.episodes)
                and self._episodes_match(episodes, self._semantics.episodes),
                movie_count=len(movies),
                series_count=len(shows),
                episode_count=len(episodes),
                expected_movie_count=len(self._semantics.movies),
                expected_series_count=len(self._semantics.series),
                expected_episode_count=len(self._semantics.episodes),
            )
        finally:
            self._emit(FixtureInitializationEvent.SEMANTIC_VERIFICATION_COMPLETED)

    def wait_for_fixture_state(
        self,
        *,
        deadline_seconds: float = FIXTURE_INITIALIZATION_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> FixtureLibraryState:
        if not 0 < deadline_seconds <= FIXTURE_INITIALIZATION_TIMEOUT_SECONDS:
            raise ValueError("fixture deadline exceeds the approved bound")
        deadline = clock() + deadline_seconds
        last = FixtureLibraryState(
            False,
            False,
            False,
            0,
            0,
            0,
            len(self._semantics.movies),
            len(self._semantics.series),
            len(self._semantics.episodes),
        )
        while clock() < deadline:
            last = self.get_fixture_library_state()
            if (
                self._movie_scan_requested
                and not self._movie_scan_completed
                and last.movie_present
                and last.movie_count == len(self._semantics.movies)
            ):
                self._movie_scan_completed = True
                self._emit(FixtureInitializationEvent.MOVIES_SCAN_COMPLETED)
            if (
                self._movie_scan_requested
                and last.movie_present
                and self._semantics.scan_tv
                and not self._tv_scan_requested
            ):
                self._scan_tv_fixture_library()
                continue
            elif last.is_baseline:
                if self._tv_scan_requested and not self._tv_scan_completed:
                    self._tv_scan_completed = True
                    self._emit(FixtureInitializationEvent.TV_SCAN_COMPLETED)
                return last
            remaining = deadline - clock()
            if remaining > 0:
                sleep(min(FIXTURE_POLL_INTERVAL_SECONDS, remaining))
        raise FixtureInitializationTimeoutError(
            "assigned fixture did not reach its semantic baseline"
        )

    def remove_fixture_movie_for_reset_test(self) -> FixtureLibraryState:
        """Remove only the known fixture movie; accepts no caller-selected identity."""

        document = self._request(
            "kodi-lab-fixture-mutation-find-movie",
            "VideoLibrary.GetMovies",
            {"properties": ["title", "year", "uniqueid", "file"]},
        )
        if self._semantics.drift_movie_unique_id is None:
            raise ValueError("trusted fixture has no bounded movie drift target")
        target = next(
            movie
            for movie in self._semantics.movies
            if movie.unique_id == self._semantics.drift_movie_unique_id
        )
        matches = [
            item
            for item in self._items(document, "movies")
            if self._is_movie(item, target)
        ]
        if len(matches) != 1:
            raise JsonRpcInvalidResponseError(
                "fixture movie identity is missing or ambiguous"
            )
        movie_id = matches[0].get("movieid")
        if not isinstance(movie_id, int) or isinstance(movie_id, bool) or movie_id < 0:
            raise JsonRpcInvalidResponseError("fixture movie ID is invalid")
        removed = self._request(
            "kodi-lab-fixture-mutation-remove-movie",
            "VideoLibrary.RemoveMovie",
            {"movieid": movie_id},
        )
        if removed.get("result") != "OK":
            raise JsonRpcInvalidResponseError("fixture movie removal result is invalid")
        state = self.get_fixture_library_state()
        if (
            state.movie_count != len(self._semantics.movies) - 1
            or state.movie_present
            or state.series_present != bool(self._semantics.series)
            or state.episode_present != bool(self._semantics.episodes)
            or state.series_count != len(self._semantics.series)
            or state.episode_count != len(self._semantics.episodes)
        ):
            raise JsonRpcInvalidResponseError(
                "fixture drift mutation postcondition failed"
            )
        return state

    def remove_fixture_episode_for_reset_test(self) -> FixtureLibraryState:
        """Remove only the trusted fixture episode; accepts no selected identity."""

        target_id = self._semantics.drift_episode_unique_id
        if target_id is None:
            raise ValueError("trusted fixture has no bounded episode drift target")
        document = self._request(
            "kodi-lab-fixture-mutation-find-episode",
            "VideoLibrary.GetEpisodes",
            {
                "properties": [
                    "title",
                    "showtitle",
                    "season",
                    "episode",
                    "uniqueid",
                    "file",
                ]
            },
        )
        target = next(
            episode
            for episode in self._semantics.episodes
            if episode.unique_id == target_id
        )
        items = self._items(document, "episodes")
        matches = [item for item in items if self._is_episode(item, target)]
        if len(matches) != 1:
            raise JsonRpcInvalidResponseError(
                "fixture episode identity is missing or ambiguous"
            )
        episode_id = matches[0].get("episodeid")
        if (
            not isinstance(episode_id, int)
            or isinstance(episode_id, bool)
            or episode_id < 1
        ):
            raise JsonRpcInvalidResponseError("fixture episode ID is invalid")
        removed = self._request(
            "kodi-lab-fixture-mutation-remove-episode",
            "VideoLibrary.RemoveEpisode",
            {"episodeid": episode_id},
        )
        if removed.get("result") != "OK":
            raise JsonRpcInvalidResponseError(
                "fixture episode removal result is invalid"
            )
        remaining_document = self._request(
            "kodi-lab-fixture-mutation-remaining-episodes",
            "VideoLibrary.GetEpisodes",
            {
                "properties": [
                    "title",
                    "showtitle",
                    "season",
                    "episode",
                    "uniqueid",
                    "file",
                ]
            },
        )
        remaining_items = self._items(remaining_document, "episodes")
        remaining_expected = tuple(
            episode
            for episode in self._semantics.episodes
            if episode.unique_id != target_id
        )
        if not self._episodes_match(remaining_items, remaining_expected):
            raise JsonRpcInvalidResponseError(
                "fixture episode drift identities are invalid"
            )
        state = self.get_fixture_library_state()
        if (
            state.movie_count != len(self._semantics.movies)
            or state.series_count != len(self._semantics.series)
            or state.episode_count != len(self._semantics.episodes) - 1
            or state.movie_present != bool(self._semantics.movies)
            or state.series_present != bool(self._semantics.series)
            or state.episode_present
        ):
            raise JsonRpcInvalidResponseError(
                "fixture episode drift mutation postcondition failed"
            )
        return state

    def apply_fixture_drift_plan_for_reset_test(self) -> FixtureLibraryState:
        """Apply the package-defined two-item Medium drift with no target input."""

        movie_target_id = self._semantics.drift_movie_unique_id
        episode_target_id = self._semantics.drift_episode_unique_id
        if movie_target_id is None or episode_target_id is None:
            raise ValueError("trusted fixture has no bounded combined drift plan")
        movie_target = next(
            movie
            for movie in self._semantics.movies
            if movie.unique_id == movie_target_id
        )
        movie_document = self._request(
            "kodi-lab-fixture-drift-find-movie",
            "VideoLibrary.GetMovies",
            {"properties": ["title", "year", "uniqueid", "file"]},
        )
        movie_matches = [
            item
            for item in self._items(movie_document, "movies")
            if self._is_movie(item, movie_target)
        ]
        if len(movie_matches) != 1:
            raise JsonRpcInvalidResponseError(
                "fixture movie identity is missing or ambiguous"
            )
        movie_id = movie_matches[0].get("movieid")
        if not isinstance(movie_id, int) or isinstance(movie_id, bool) or movie_id < 0:
            raise JsonRpcInvalidResponseError("fixture movie ID is invalid")
        movie_removed = self._request(
            "kodi-lab-fixture-drift-remove-movie",
            "VideoLibrary.RemoveMovie",
            {"movieid": movie_id},
        )
        if movie_removed.get("result") != "OK":
            raise JsonRpcInvalidResponseError("fixture movie removal result is invalid")

        episode_target = next(
            episode
            for episode in self._semantics.episodes
            if episode.unique_id == episode_target_id
        )
        episode_document = self._request(
            "kodi-lab-fixture-drift-find-episode",
            "VideoLibrary.GetEpisodes",
            {
                "properties": [
                    "title",
                    "showtitle",
                    "season",
                    "episode",
                    "uniqueid",
                    "file",
                ]
            },
        )
        episode_matches = [
            item
            for item in self._items(episode_document, "episodes")
            if self._is_episode(item, episode_target)
        ]
        if len(episode_matches) != 1:
            raise JsonRpcInvalidResponseError(
                "fixture episode identity is missing or ambiguous"
            )
        episode_id = episode_matches[0].get("episodeid")
        if (
            not isinstance(episode_id, int)
            or isinstance(episode_id, bool)
            or episode_id < 1
        ):
            raise JsonRpcInvalidResponseError("fixture episode ID is invalid")
        episode_removed = self._request(
            "kodi-lab-fixture-drift-remove-episode",
            "VideoLibrary.RemoveEpisode",
            {"episodeid": episode_id},
        )
        if episode_removed.get("result") != "OK":
            raise JsonRpcInvalidResponseError(
                "fixture episode removal result is invalid"
            )
        state = self.get_fixture_library_state()
        if (
            state.movie_count != len(self._semantics.movies) - 1
            or state.series_count != len(self._semantics.series)
            or state.episode_count != len(self._semantics.episodes) - 1
            or state.movie_present
            or not state.series_present
            or state.episode_present
        ):
            raise JsonRpcInvalidResponseError(
                "fixture combined drift mutation postcondition failed"
            )
        return state

    def _request(
        self,
        request_id: str,
        method: Literal[
            "VideoLibrary.Scan",
            "VideoLibrary.GetMovies",
            "VideoLibrary.GetTVShows",
            "VideoLibrary.GetEpisodes",
            "VideoLibrary.RemoveMovie",
            "VideoLibrary.RemoveEpisode",
        ],
        params: dict[str, object],
    ) -> dict[str, object]:
        payload = self._transport.post(
            host=self._host,
            port=self._port,
            path=self._path,
            body=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
                separators=(",", ":"),
            ).encode(),
            timeout=self._timeout,
        )
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise JsonRpcInvalidResponseError(
                "fixture JSON-RPC response is not valid JSON"
            ) from error
        if (
            not isinstance(document, dict)
            or document.get("jsonrpc") != "2.0"
            or document.get("id") != request_id
            or "error" in document
            or "result" not in document
        ):
            raise JsonRpcInvalidResponseError(
                "fixture JSON-RPC response envelope is invalid"
            )
        return cast(dict[str, object], document)

    @staticmethod
    def _items(document: dict[str, object], key: str) -> list[dict[str, object]]:
        result = document.get("result")
        if not isinstance(result, dict):
            raise JsonRpcInvalidResponseError("fixture library result is invalid")
        raw_items = result.get(key, [])
        if not isinstance(raw_items, list) or any(
            not isinstance(item, dict) for item in raw_items
        ):
            raise JsonRpcInvalidResponseError("fixture library items are invalid")
        return cast(list[dict[str, object]], raw_items)

    @staticmethod
    def _has_unique_id(item: dict[str, object], expected: str) -> bool:
        unique = item.get("uniqueid")
        return isinstance(unique, dict) and unique.get("kodi-lab") == expected

    @classmethod
    def _is_movie(
        cls, item: dict[str, object], expected: FixtureMovieExpectation
    ) -> bool:
        return (
            item.get("title") == expected.title
            and item.get("year") == expected.year
            and item.get("file") == expected.file
            and cls._has_unique_id(item, expected.unique_id)
        )

    @classmethod
    def _is_series(
        cls, item: dict[str, object], expected: FixtureSeriesExpectation
    ) -> bool:
        return (
            item.get("title") == expected.title
            and item.get("year") == expected.year
            and item.get("file") == expected.directory
            and cls._has_unique_id(item, expected.unique_id)
        )

    @classmethod
    def _is_episode(
        cls, item: dict[str, object], expected: FixtureEpisodeExpectation
    ) -> bool:
        return (
            item.get("title") == expected.title
            and item.get("showtitle") == expected.show_title
            and item.get("season") == expected.season
            and item.get("episode") == expected.episode
            and item.get("file") == expected.file
            and cls._has_unique_id(item, expected.unique_id)
        )

    @classmethod
    def _movies_match(
        cls,
        items: list[dict[str, object]],
        expected: tuple[FixtureMovieExpectation, ...],
    ) -> bool:
        expected_by_id = {value.unique_id: value for value in expected}
        if len(items) != len(expected_by_id):
            return False
        seen: set[str] = set()
        for item in items:
            unique_id = cls._kodi_lab_unique_id(item)
            expectation = expected_by_id.get(unique_id)
            if (
                expectation is None
                or unique_id in seen
                or not cls._is_movie(item, expectation)
            ):
                return False
            seen.add(unique_id)
        return seen == expected_by_id.keys()

    @classmethod
    def _series_match(
        cls,
        items: list[dict[str, object]],
        expected: tuple[FixtureSeriesExpectation, ...],
    ) -> bool:
        expected_by_id = {value.unique_id: value for value in expected}
        if len(items) != len(expected_by_id):
            return False
        seen: set[str] = set()
        for item in items:
            unique_id = cls._kodi_lab_unique_id(item)
            expectation = expected_by_id.get(unique_id)
            if (
                expectation is None
                or unique_id in seen
                or not cls._is_series(item, expectation)
            ):
                return False
            seen.add(unique_id)
        return seen == expected_by_id.keys()

    @classmethod
    def _episodes_match(
        cls,
        items: list[dict[str, object]],
        expected: tuple[FixtureEpisodeExpectation, ...],
    ) -> bool:
        expected_by_id = {value.unique_id: value for value in expected}
        if len(items) != len(expected_by_id):
            return False
        seen: set[str] = set()
        for item in items:
            unique_id = cls._kodi_lab_unique_id(item)
            expectation = expected_by_id.get(unique_id)
            if (
                expectation is None
                or unique_id in seen
                or not cls._is_episode(item, expectation)
            ):
                return False
            seen.add(unique_id)
        return seen == expected_by_id.keys()

    @staticmethod
    def _kodi_lab_unique_id(item: dict[str, object]) -> str:
        unique = item.get("uniqueid")
        value = unique.get("kodi-lab") if isinstance(unique, dict) else None
        return value if isinstance(value, str) else ""

    def _emit(self, event: FixtureInitializationEvent) -> None:
        if self._event_observer is not None:
            self._event_observer(event)
