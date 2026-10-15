"""Shared post-normalization enrichment, with optional fresh skin context."""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import httpx

from kodi_lab.kodi.ui_state import UiObservation, summarize
from kodi_lab.kodi.ui_state_native import UiObservationError, UiRpc
from kodi_lab.kodi.window_resolver import WindowResolver, builtin_resolver


@dataclass(frozen=True)
class SkinContext:
    skin_id: str
    version: str


class SkinObservationHints:
    """Bounded scheduling hints, never cached skin identity or semantics.

    A stale hint can only cause fresh context reads. Every name still requires
    freshly matching skin ID/version on both sides of the authorized collection.
    Container/endpoint identity changes and failures clear hints; expiry bounds
    unobservable restarts, reconnects and profile transitions.
    """

    def __init__(
        self,
        *,
        limit: int = 1024,
        ttl: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entries: OrderedDict[UUID, tuple[tuple[str, ...], float]] = OrderedDict()
        self._limit, self._ttl, self._clock = limit, ttl, clock
        self._lock = threading.Lock()

    def enabled(self, target: UUID, identity: tuple[str, ...]) -> bool:
        with self._lock:
            previous = self._entries.pop(target, None)
            if (
                previous is None
                or previous[0] != identity
                or previous[1] <= self._clock()
            ):
                return False
            self._entries[target] = previous
            return True

    def record(self, target: UUID, identity: tuple[str, ...], *, needed: bool) -> None:
        with self._lock:
            self._entries.pop(target, None)
            if needed:
                self._entries[target] = (identity, self._clock() + self._ttl)
                while len(self._entries) > self._limit:
                    self._entries.popitem(last=False)

    def discard(self, target: UUID) -> None:
        with self._lock:
            self._entries.pop(target, None)


def needs_skin_context(
    value: UiObservation, version: str, resolver: WindowResolver
) -> bool:
    return not value.changed_during_collection and any(
        resolver.needs_skin(state.id, state.label, kodi_version=version)
        for state in (value.window, value.dialog)
    )


def applicable_skin(
    value: UiObservation, version: str, resolver: WindowResolver, skin: SkinContext
) -> bool:
    return any(
        resolver.has_skin_mapping(
            state.id,
            kodi_version=version,
            skin_id=skin.skin_id,
            skin_version=skin.version,
        )
        for state in (value.window, value.dialog)
    )


def collect_skin_context(
    call: UiRpc,
    value: UiObservation | None,
    *,
    recollect: Callable[[], UiObservation],
    accept_skin: Callable[[SkinContext], bool] | None = None,
    accept_identity: Callable[[str, int], bool] | None = None,
    accept_observation: Callable[[UiObservation, SkinContext], bool] | None = None,
) -> tuple[UiObservation, SkinContext | None]:
    """Bracket a fresh full collection with active skin/version observations.

    Only called for unnamed IDs with available skin candidates. Kodi's GUI skin
    property lacks version, so bounded add-on detail reads are necessary.
    Bracketing GUI/detail reads reject skin, version or foreground changes.
    The initial observation is only a trigger, never associated with a skin
    sampled later. A scheduling hint can pass None to bracket the first full
    collection instead of discarding one. All identity reads remain fresh.
    Collection retains the provider's authorization checks. These observations
    are bounded/non-atomic, just like the existing native inspection pipeline.
    """
    expected = (
        (value.dialog.id if value.dialog.active else value.window.id) if value else None
    )
    if value is not None and (expected is None or value.dialog.active is None):
        return value, None

    def fallback() -> tuple[UiObservation, None]:
        # Outside optional-read handlers so provider authority failures propagate.
        return value if value is not None else recollect(), None

    def gui() -> tuple[str, int] | None:
        result = call("GUI.GetProperties", {"properties": ["skin", "currentwindow"]})
        if not isinstance(result, dict):
            return None
        skin, window = result.get("skin"), result.get("currentwindow")
        if not isinstance(skin, dict) or not isinstance(window, dict):
            return None
        identity, window_id = skin.get("id"), window.get("id")
        if (
            not isinstance(identity, str)
            or len(identity) > 160
            or not re.fullmatch(r"skin\.[a-z0-9._-]+", identity)
            or type(window_id) is not int
            or not 0 < window_id <= 2**31 - 1
            or (expected is not None and window_id != expected)
        ):
            return None
        return identity, window_id

    def addon_version(identity: str) -> str | None:
        details = call(
            "Addons.GetAddonDetails",
            {"addonid": identity, "properties": ["version", "enabled"]},
        )
        addon = details.get("addon") if isinstance(details, dict) else None
        if (
            not isinstance(addon, dict)
            or addon.get("addonid") != identity
            or addon.get("enabled") is not True
        ):
            return None
        version = addon.get("version")
        if not isinstance(version, str) or not 1 <= len(version) <= 80:
            return None
        return version

    def begin() -> tuple[tuple[str, int], str] | None:
        try:
            before = gui()
            if before is None or (accept_identity and not accept_identity(*before)):
                return None
            version = addon_version(before[0])
            if version is None:
                return None
            if accept_skin is not None and not accept_skin(
                SkinContext(before[0], version)
            ):
                return None
            return before, version
        except (UiObservationError, OSError, ValueError):
            return None

    pending = begin()
    if pending is None:
        return fallback()
    before, version = pending

    # Deliberately outside optional-context exception handling: revocation,
    # identity mismatch and other provider failures must retain their behavior.
    try:
        refreshed = recollect()
    except (OSError, httpx.HTTPError):
        if value is None:
            raise
        return value, None
    except UiObservationError as error:
        if value is not None and error.code in {
            "target_unreachable",
            "ui_observation_failed",
        }:
            return value, None
        raise
    foreground = refreshed.dialog.id if refreshed.dialog.active else refreshed.window.id
    if (
        refreshed.changed_during_collection
        or foreground != before[1]
        or refreshed.dialog.active is None
    ):
        return value if value is not None else refreshed, None
    if accept_observation and not accept_observation(
        refreshed, SkinContext(before[0], version)
    ):
        return refreshed, None
    try:
        if addon_version(before[0]) != version or gui() != before:
            return value if value is not None else refreshed, None
    except (UiObservationError, OSError, ValueError):
        return value if value is not None else refreshed, None
    return refreshed, SkinContext(before[0], version)


def resolve_ui_observation(
    value: UiObservation,
    *,
    kodi_version: str | None,
    resolver: WindowResolver | None = None,
    skin: SkinContext | None = None,
) -> UiObservation:
    resolver = resolver or builtin_resolver()
    # A caller-supplied context is also unusable after collection instability.
    if value.changed_during_collection:
        skin = None
    context = {
        "kodi_version": kodi_version,
        "skin_id": skin.skin_id if skin else None,
        "skin_version": skin.version if skin else None,
    }
    result = value.model_copy(
        update={
            "window": value.window.model_copy(
                update={
                    "resolution": resolver.resolve(
                        value.window.id, value.window.label, **context
                    )
                }
            ),
            "dialog": value.dialog.model_copy(
                update={
                    "resolution": resolver.resolve(
                        value.dialog.id, value.dialog.label, **context
                    )
                }
            ),
        }
    )
    return result.model_copy(update={"summary": summarize(result)})
