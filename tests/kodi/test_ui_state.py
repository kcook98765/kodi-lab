from __future__ import annotations

from typing import Any

import pytest

from kodi_lab.kodi.ui_state import (
    DialogState,
    UiCollection,
    UiObservation,
    normalize_collection,
    summarize,
)
from kodi_lab.kodi.ui_state_native import (
    UiObservationError,
    collect_native,
)


def test_deterministic_dialog_summary_does_not_repeat_dialog() -> None:
    value = UiObservation(dialog=DialogState(active=True, label="OK dialog"))
    assert summarize(value) == "OK dialog open."


def movie_collection() -> UiCollection:
    identity = {
        "window_id": 10025,
        "dialog_id": None,
        "control_id": 50,
        "is_container": True,
        "wrapper_id": 50,
        "focus_available": True,
        "dialog_available": True,
    }
    return UiCollection.model_validate(
        {
            "before": identity,
            "after": {**identity, "is_container": None, "wrapper_id": None},
            "player_before": {"active": False, "audio": False, "video": False},
            "player_after": {"active": False, "audio": False, "video": False},
            "labels_available": True,
            "labels": {
                "window": "Movies",
                "control_id": "50",
                "control": "Blade Runner",
                "content": "movies",
                "count": "843",
                "total": "844",
                "number": "5",
                "label": "Blade Runner",
                "label2": "1982",
                "media_type": "movie",
                "dbid": "123",
                "year": "1982",
            },
            "conditions": {
                "busy": False,
                "fullscreen_video": False,
                "screensaver": False,
                "text_input": False,
            },
        }
    )


def test_generic_movie_contract() -> None:
    observation = normalize_collection(movie_collection())
    assert observation.window.id == 10025
    assert observation.focus.control_id == observation.container.id == 50
    assert observation.container.associated and observation.selected_item.associated
    assert observation.selected_item.dbid == 123
    assert observation.summary == "Movies. Focus is on Blade Runner, item 5 of 843."
    assert len(observation.model_dump_json()) < 1200
    assert (
        UiObservation.model_validate_json(observation.model_dump_json()) == observation
    )


@pytest.mark.parametrize("dialog", [None, 10106])
def test_retained_movie_metadata_never_implies_sidebar_or_dialog_focus(
    dialog: int | None,
) -> None:
    raw = movie_collection().model_dump()
    for key in ("before", "after"):
        raw[key].update(
            control_id=6051, is_container=False, wrapper_id=6051, dialog_id=dialog
        )
    raw["labels"].update(
        control_id="6051",
        control="Options",
        window="Context menu" if dialog else "Movies",
    )
    value = normalize_collection(UiCollection.model_validate(raw))
    assert value.focus.label == "Options"
    assert value.focus.is_container is False
    assert not value.container.associated
    assert not value.selected_item.associated
    assert value.selected_item.label is None
    assert value.dialog.active is (dialog is not None)
    if dialog:
        assert value.window.id == 10025 and value.window.label is None
        assert value.dialog.id == dialog


@pytest.mark.parametrize("direct,label", [(0, "0"), (None, ""), (-1, "")])
def test_no_focus_normalization(direct: int | None, label: str) -> None:
    raw = movie_collection().model_dump()
    for key in ("before", "after"):
        raw[key].update(control_id=direct, wrapper_id=None, is_container=None)
    raw["labels"]["control_id"] = label
    value = normalize_collection(UiCollection.model_validate(raw))
    assert value.focus.control_id is None and value.focus.label is None
    assert not value.selected_item.associated
    assert not value.changed_during_collection


@pytest.mark.parametrize(
    "change", ["window_id", "dialog_id", "control_id", "label_focus", "wrapper"]
)
def test_identity_change_invalidates_association(change: str) -> None:
    raw = movie_collection().model_dump()
    if change == "label_focus":
        raw["labels"]["control_id"] = "51"
    elif change == "wrapper":
        raw["before"]["wrapper_id"] = 51
    else:
        raw["after"][change] = 10000
    value = normalize_collection(UiCollection.model_validate(raw))
    assert value.changed_during_collection
    assert not value.container.associated and not value.selected_item.associated
    assert value.focus.label is None


def test_empty_list_and_parent_have_no_selected_media() -> None:
    raw = movie_collection().model_dump()
    raw["labels"].update(count="0", total="1", number="0", label="..")
    value = normalize_collection(UiCollection.model_validate(raw))
    assert value.container.associated and value.container.item_count == 0
    assert value.container.item_number is None and not value.selected_item.associated


def test_home_without_media_and_missing_optional_fields() -> None:
    raw = movie_collection().model_dump()
    for key in ("before", "after"):
        raw[key].update(
            window_id=10000, control_id=9000, is_container=False, wrapper_id=9000
        )
    raw["labels"] = {"window": "Home", "control_id": "9000", "control": "Movies"}
    raw["conditions"] = {}
    value = normalize_collection(UiCollection.model_validate(raw))
    assert value.summary == "Home. Focus is on Movies."
    assert value.ui.screensaver is None and not value.selected_item.associated


@pytest.mark.parametrize("kind", [None, "video", "audio"])
def test_basic_playback(kind: str | None) -> None:
    raw = movie_collection().model_dump()
    for key in ("player_before", "player_after"):
        raw[key] = {
            "active": kind is not None,
            "video": kind == "video",
            "audio": kind == "audio",
        }
    raw["conditions"]["fullscreen_video"] = kind == "video"
    value = normalize_collection(UiCollection.model_validate(raw))
    assert value.playback.active is (kind is not None) and value.playback.kind == kind
    if kind == "video":
        assert value.summary == "Fullscreen video playback active."


def test_partial_labels_failure_and_inconsistent_player_are_unavailable() -> None:
    raw = movie_collection().model_dump()
    raw["labels_available"] = False
    raw["player_after"]["video"] = True
    value = normalize_collection(UiCollection.model_validate(raw))
    assert value.playback.active is None and value.changed_during_collection
    assert not value.container.associated


def native_fixture(method: str, params: dict[str, Any] | None) -> object:
    if method == "XBMC.GetInfoLabels":
        return {
            "System.CurrentWindow": "Movies",
            "System.CurrentControlID": "50",
            "System.CurrentControl": "Blade Runner",
        }
    if method == "GUI.GetProperties":
        return {"currentwindow": {"id": 10025, "label": "Movies"}}
    if method == "XBMC.GetInfoBooleans":
        return {
            "System.HasVisibleModalDialog": False,
            "System.ScreenSaverActive": False,
            "Window.IsVisible(virtualkeyboard)": False,
        }
    if method == "Player.GetActivePlayers":
        return []
    raise UiObservationError("ui_observation_failed")


def test_native_fallback_does_not_manufacture_association() -> None:
    value = collect_native(native_fixture)
    assert value.window.id == 10025 and value.focus.control_id == 50
    assert value.focus.is_container is None
    assert not value.selected_item.associated and not value.container.associated
    assert not value.playback.active
