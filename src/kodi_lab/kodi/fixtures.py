"""Unprivileged fixed-method Kodi adapter for the small-mixed fixtures."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.fixtures.seed_audit import (
    FIXTURE_MOVIE_DIRECTORY,
    FIXTURE_TV_DIRECTORY,
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


@dataclass(frozen=True, slots=True)
class FixtureLibraryState:
    movie_present: bool
    series_present: bool
    episode_present: bool
    movie_count: int
    series_count: int
    episode_count: int

    @property
    def is_baseline(self) -> bool:
        return (
            self.movie_present
            and self.series_present
            and self.episode_present
            and self.movie_count == 1
            and self.series_count == 1
            and self.episode_count == 1
        )


class SmallMixedFixtureKodiAdapter:
    """Expose no caller-directed method, path, title, ID, or JSON-RPC params."""

    def __init__(
        self,
        observation: ObserveManagedKodiRuntimeResult,
        profile: RuntimeExecutionProfile,
        *,
        timeout: float | None = None,
        transport: _HttpTransport | None = None,
    ) -> None:
        if observation.runtime_revision_id != str(profile.runtime_revision_id):
            raise ValueError("observation and execution profile revisions differ")
        if observation.jsonrpc_port != profile.jsonrpc_port:
            raise ValueError("observation and execution profile ports differ")
        if profile.kodi_release.canonical not in {"19.5", "20.5", "21.3"}:
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
        self._movie_scan_requested = False
        self._tv_scan_requested = False

    def scan_assigned_fixture_library(self) -> None:
        document = self._request(
            "kodi-lab-fixture-scan-movies",
            "VideoLibrary.Scan",
            {"directory": FIXTURE_MOVIE_DIRECTORY, "showdialogs": False},
        )
        if document.get("result") != "OK":
            raise JsonRpcInvalidResponseError("fixture scan result is invalid")
        self._movie_scan_requested = True
        self._tv_scan_requested = False

    def _scan_tv_fixture_library(self) -> None:
        document = self._request(
            "kodi-lab-fixture-scan-tv",
            "VideoLibrary.Scan",
            {"directory": FIXTURE_TV_DIRECTORY, "showdialogs": False},
        )
        if document.get("result") != "OK":
            raise JsonRpcInvalidResponseError("fixture TV scan result is invalid")
        self._tv_scan_requested = True

    def get_fixture_library_state(self) -> FixtureLibraryState:
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
            movie_present=any(self._is_movie(item) for item in movies),
            series_present=any(self._is_series(item) for item in shows),
            episode_present=any(self._is_episode(item) for item in episodes),
            movie_count=len(movies),
            series_count=len(shows),
            episode_count=len(episodes),
        )

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
        last = FixtureLibraryState(False, False, False, 0, 0, 0)
        while clock() < deadline:
            last = self.get_fixture_library_state()
            if (
                self._movie_scan_requested
                and last.movie_present
                and not self._tv_scan_requested
            ):
                self._scan_tv_fixture_library()
                continue
            elif last.is_baseline:
                return last
            remaining = deadline - clock()
            if remaining > 0:
                sleep(min(FIXTURE_POLL_INTERVAL_SECONDS, remaining))
        raise FixtureInitializationTimeoutError(
            "small-mixed fixture did not reach its semantic baseline"
        )

    def remove_fixture_movie_for_reset_test(self) -> FixtureLibraryState:
        """Remove only the known fixture movie; accepts no caller-selected identity."""

        document = self._request(
            "kodi-lab-fixture-mutation-find-movie",
            "VideoLibrary.GetMovies",
            {"properties": ["title", "year", "uniqueid", "file"]},
        )
        matches = [
            item for item in self._items(document, "movies") if self._is_movie(item)
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
        if state.movie_present or not state.series_present or not state.episode_present:
            raise JsonRpcInvalidResponseError(
                "fixture drift mutation postcondition failed"
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
    def _is_movie(cls, item: dict[str, object]) -> bool:
        return (
            item.get("title") == FIXTURE_MOVIE_TITLE
            and item.get("year") == FIXTURE_MOVIE_YEAR
            and item.get("file") == FIXTURE_MOVIE_FILE
            and cls._has_unique_id(item, FIXTURE_MOVIE_UNIQUE_ID)
        )

    @classmethod
    def _is_series(cls, item: dict[str, object]) -> bool:
        return (
            item.get("title") == FIXTURE_SERIES_TITLE
            and item.get("year") == FIXTURE_SERIES_YEAR
            and item.get("file") == FIXTURE_SERIES_DIRECTORY
            and cls._has_unique_id(item, FIXTURE_SERIES_UNIQUE_ID)
        )

    @classmethod
    def _is_episode(cls, item: dict[str, object]) -> bool:
        return (
            item.get("title") == FIXTURE_EPISODE_TITLE
            and item.get("showtitle") == FIXTURE_SERIES_TITLE
            and item.get("season") == 1
            and item.get("episode") == 1
            and item.get("file") == FIXTURE_EPISODE_FILE
            and cls._has_unique_id(item, FIXTURE_EPISODE_UNIQUE_ID)
        )
