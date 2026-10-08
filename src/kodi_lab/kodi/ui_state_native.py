"""Bounded conservative native fallback for UI observation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kodi_lab.kodi.ui_state import (
    DialogState,
    FocusState,
    PlaybackState,
    UiFlags,
    UiObservation,
    WindowState,
    summarize,
)

type UiRpc = Callable[[str, dict[str, Any] | None], object]


class UiObservationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _label(value: object) -> str | None:
    return " ".join(value.split())[:160] or None if isinstance(value, str) else None


def _id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= 2**31 - 1 else None


def collect_native(call: UiRpc) -> UiObservation:
    """Native focus is actual focus; it cannot establish container ownership."""
    labels = _object(
        call(
            "XBMC.GetInfoLabels",
            {
                "labels": [
                    "System.CurrentWindow",
                    "System.CurrentControlID",
                    "System.CurrentControl",
                ]
            },
        )
    )
    foreground: int | None = None
    booleans: dict[str, Any] = {}
    playback = PlaybackState()
    for method, params in (
        ("GUI.GetProperties", {"properties": ["currentwindow"]}),
        (
            "XBMC.GetInfoBooleans",
            {
                "booleans": [
                    "System.HasVisibleModalDialog",
                    "System.ScreenSaverActive",
                    "Window.IsVisible(virtualkeyboard)",
                ]
            },
        ),
        ("Player.GetActivePlayers", None),
    ):
        try:
            result = call(method, params)
        except UiObservationError:
            continue
        if method == "GUI.GetProperties":
            foreground = _id(_object(_object(result).get("currentwindow")).get("id"))
        elif method == "XBMC.GetInfoBooleans":
            booleans = _object(result)
        elif isinstance(result, list) and all(
            isinstance(player, dict)
            and player.get("type") in {"audio", "video", "picture"}
            for player in result
        ):
            kinds = {player["type"] for player in result}
            kind = next(iter(kinds)) if len(kinds) == 1 else None
            playback = PlaybackState(
                active=bool(result), kind=kind if kind in {"audio", "video"} else None
            )
    modal = booleans.get("System.HasVisibleModalDialog")
    modal = modal if type(modal) is bool else None
    control = _id(labels.get("System.CurrentControlID"))
    value = UiObservation(
        window=WindowState(
            id=foreground if modal is False else None,
            label=_label(labels.get("System.CurrentWindow"))
            if modal is False
            else None,
        ),
        dialog=DialogState(
            active=modal,
            id=foreground if modal is True else None,
            label=_label(labels.get("System.CurrentWindow")) if modal is True else None,
        ),
        focus=FocusState(
            control_id=control,
            label=_label(labels.get("System.CurrentControl")) if control else None,
        ),
        playback=playback,
        ui=UiFlags(
            busy=foreground in (10138, 10160) if foreground is not None else None,
            fullscreen_video=foreground == 12005 if foreground is not None else None,
            screensaver=booleans.get("System.ScreenSaverActive")
            if type(booleans.get("System.ScreenSaverActive")) is bool
            else None,
            text_input=booleans.get("Window.IsVisible(virtualkeyboard)")
            if type(booleans.get("Window.IsVisible(virtualkeyboard)")) is bool
            else None,
        ),
    )
    # Native requests are separate, so verify focus/window labels once more.
    after = _object(
        call(
            "XBMC.GetInfoLabels",
            {"labels": ["System.CurrentWindow", "System.CurrentControlID"]},
        )
    )
    changed = labels.get("System.CurrentWindow") != after.get(
        "System.CurrentWindow"
    ) or _id(labels.get("System.CurrentControlID")) != _id(
        after.get("System.CurrentControlID")
    )
    if changed:
        value = value.model_copy(
            update={
                "focus": FocusState(control_id=control),
                "changed_during_collection": True,
            }
        )
    return value.model_copy(update={"summary": summarize(value)})
