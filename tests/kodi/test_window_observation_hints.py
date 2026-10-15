from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from kodi_lab.kodi.ui_state import DialogState, UiObservation, WindowState, summarize
from kodi_lab.kodi.ui_state_native import UiObservationError
from kodi_lab.kodi.ui_window_resolution import (
    SkinContext,
    SkinObservationHints,
    applicable_skin,
    collect_skin_context,
    needs_skin_context,
    resolve_ui_observation,
)
from kodi_lab.kodi.window_resolver import builtin_resolver

TARGET = UUID(int=1)


def observation(*, dialog: bool = False, window_id: int = 11112) -> UiObservation:
    return UiObservation(
        window=WindowState(id=10000 if dialog else window_id),
        dialog=DialogState(id=window_id if dialog else None, active=dialog),
    )


class Source:
    def __init__(self, *, dialog: bool = False) -> None:
        self.skin_id = "skin.estouchy"
        self.version = "3.0.8"
        self.value = observation(dialog=dialog)
        self.events: list[str] = []
        self.change: str | None = None

    def rpc(self, method: str, params: dict[str, Any] | None) -> object:
        self.events.append(method)
        if method == "GUI.GetProperties":
            return {
                "skin": {"id": self.skin_id},
                "currentwindow": {"id": self.value.dialog.id or self.value.window.id},
            }
        return {
            "addon": {"addonid": self.skin_id, "version": self.version, "enabled": True}
        }

    def collect(self) -> UiObservation:
        self.events.append("collect")
        if self.change == "skin":
            self.skin_id = "skin.confluence"
        elif self.change == "version":
            self.version = "3.0.9"
        return self.value


def collect(
    source: Source, initial: UiObservation | None = None
) -> tuple[UiObservation, SkinContext | None]:
    resolver = builtin_resolver()
    return collect_skin_context(
        source.rpc,
        initial,
        recollect=source.collect,
        accept_identity=lambda skin, window: resolver.has_skin_id(
            window, kodi_version="20.5", skin_id=skin
        ),
        accept_observation=lambda value, skin: (
            needs_skin_context(value, "20.5", resolver)
            and applicable_skin(value, "20.5", resolver, skin)
        ),
    )


def test_warm_hint_brackets_first_collection_and_avoids_discarded_collection() -> None:
    cold = Source()
    collect(cold, cold.collect())
    warm = Source()
    result, skin = collect(warm)
    assert skin == SkinContext("skin.estouchy", "3.0.8")
    assert result is warm.value
    assert cold.events.count("collect") == 2
    assert warm.events == [
        "GUI.GetProperties",
        "Addons.GetAddonDetails",
        "collect",
        "Addons.GetAddonDetails",
        "GUI.GetProperties",
    ]


@pytest.mark.parametrize("dialog", [False, True])
def test_same_numeric_id_uses_fresh_active_skin_after_hint(dialog: bool) -> None:
    source = Source(dialog=dialog)
    _, first = collect(source)
    assert first == SkinContext("skin.estouchy", "3.0.8")
    # Skin switch between requests: same numeric ID, different meaning.
    source.skin_id, source.version = "skin.confluence", "4.8.4"
    fresh, skin = collect(source)
    result = resolve_ui_observation(fresh, kodi_version="20.5", skin=skin)
    state = result.dialog if dialog else result.window
    assert state.resolution and state.resolution.name == "Side menu help"
    assert state.resolution.skin_id == "skin.confluence"


@pytest.mark.parametrize("change", ["skin", "version"])
def test_transition_during_warm_collection_never_receives_stale_mapping(
    change: str,
) -> None:
    source = Source()
    source.change = change
    fresh, skin = collect(source)
    assert skin is None
    result = resolve_ui_observation(fresh, kodi_version="20.5", skin=skin)
    assert result.window.resolution and result.window.resolution.status == "classified"


def test_unsupported_skin_rejected_before_version_read_and_collected_once() -> None:
    source = Source()
    source.skin_id = "skin.unrelated"
    value, skin = collect(source)
    assert value is source.value and skin is None
    assert source.events == ["GUI.GetProperties", "collect"]


@pytest.mark.parametrize(
    "code", ["identity_mismatch", "authentication_failed", "target_unreachable"]
)
def test_warm_collection_errors_propagate_when_no_prior_success_exists(
    code: str,
) -> None:
    source = Source()

    def failed() -> UiObservation:
        raise UiObservationError(code)

    with pytest.raises(UiObservationError, match=code):
        collect_skin_context(source.rpc, None, recollect=failed)


def test_failed_optional_prequery_still_runs_authorized_collection_once() -> None:
    source = Source()

    def rpc(method: str, params: dict[str, Any] | None) -> object:
        raise UiObservationError("target_unreachable")

    result, skin = collect_skin_context(rpc, None, recollect=source.collect)
    assert result is source.value and skin is None and source.events == ["collect"]


def test_hint_expiry_lifecycle_identity_and_failure_eviction() -> None:
    now = [1.0]
    hints = SkinObservationHints(clock=lambda: now[0], ttl=10)
    identity = ("container", "runtime", "endpoint", "generation")
    hints.record(TARGET, identity, needed=True)
    assert hints.enabled(TARGET, identity)
    assert not hints.enabled(TARGET, ("replacement-container", *identity[1:]))
    assert not hints.enabled(TARGET, identity)
    hints.record(TARGET, identity, needed=True)
    now[0] = 11
    assert not hints.enabled(TARGET, identity)
    hints.record(TARGET, identity, needed=True)
    hints.discard(TARGET)
    assert not hints.enabled(TARGET, identity)
    hints.record(TARGET, identity, needed=True)
    hints.record(TARGET, identity, needed=False)
    assert not hints.enabled(TARGET, identity)


def test_hint_cache_is_bounded_and_has_no_skin_identity_values() -> None:
    hints = SkinObservationHints(limit=2)
    for number in range(3):
        hints.record(UUID(int=number), ("runtime",), needed=True)
    assert not hints.enabled(UUID(int=0), ("runtime",))
    assert hints.enabled(UUID(int=1), ("runtime",))
    assert hints.enabled(UUID(int=2), ("runtime",))


@pytest.mark.parametrize("skin", [SkinContext("skin.estuary", "4.0.0"), None])
def test_ambiguous_and_unknown_dialog_summaries_never_name_the_base_window(
    skin: SkinContext | None,
) -> None:
    # 11101 is a contextual/ambiguous Estuary dialog.
    value = observation(dialog=True, window_id=11101 if skin else 29001)
    result = resolve_ui_observation(value, kodi_version="21.3", skin=skin)
    assert result.dialog.resolution and result.dialog.resolution.name is None
    assert result.summary == (
        "Ambiguous dialog 11101 open." if skin else "Unknown dialog 29001 open."
    )
    assert "Home dialog" not in result.summary


def test_invalid_labels_are_preserved_but_not_asserted_as_semantics() -> None:
    value = UiObservation(window=WindowState(id=29001, label="CustomWindow.xml"))
    result = resolve_ui_observation(value, kodi_version="21.3")
    assert result.window.label == "CustomWindow.xml"
    assert result.summary == "Unknown window 29001."
    assert summarize(value) == "CustomWindow.xml."
