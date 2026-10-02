"""Closed semantic expectations for trusted fixture packages."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FixtureMovieExpectation:
    title: str
    year: int
    unique_id: str
    file: str
    genres: tuple[str, ...] | None = None
    actors: tuple[str, ...] | None = None
    director: str | None = None
    studio: str | None = None
    rating: float | None = None
    set_name: str | None = None
    tags: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class FixtureSeriesExpectation:
    title: str
    year: int
    unique_id: str
    directory: str


@dataclass(frozen=True, slots=True)
class FixtureEpisodeExpectation:
    title: str
    show_title: str
    season: int
    episode: int
    unique_id: str
    file: str


@dataclass(frozen=True, slots=True)
class FixtureSemanticSpecification:
    movies: tuple[FixtureMovieExpectation, ...]
    series: tuple[FixtureSeriesExpectation, ...]
    episodes: tuple[FixtureEpisodeExpectation, ...]
    scan_movies: bool
    scan_tv: bool
    drift_movie_unique_id: str | None
    drift_episode_unique_id: str | None


MIXED_FIXTURE_SEMANTICS = FixtureSemanticSpecification(
    movies=(
        FixtureMovieExpectation(
            title="Kodi Lab Fixture Movie",
            year=2024,
            unique_id="small-mixed-v1-movie",
            file=(
                "/home/kodi/.kodi/fixture-media/movies/"
                "kodi-lab-fixture-movie/Kodi Lab Fixture Movie (2024).mp4"
            ),
        ),
    ),
    series=(
        FixtureSeriesExpectation(
            title="Kodi Lab Fixture Series",
            year=2024,
            unique_id="small-mixed-v1-series",
            directory=(
                "/home/kodi/.kodi/fixture-media/tvshows/kodi-lab-fixture-series/"
            ),
        ),
    ),
    episodes=(
        FixtureEpisodeExpectation(
            title="Kodi Lab Fixture Episode",
            show_title="Kodi Lab Fixture Series",
            season=1,
            episode=1,
            unique_id="small-mixed-v1-episode",
            file=(
                "/home/kodi/.kodi/fixture-media/tvshows/"
                "kodi-lab-fixture-series/Season 01/"
                "Kodi Lab Fixture Series S01E01.mp4"
            ),
        ),
    ),
    scan_movies=True,
    scan_tv=True,
    drift_movie_unique_id="small-mixed-v1-movie",
    drift_episode_unique_id=None,
)
