"""Provider-independent, bounded UI observation contract and normalization."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WindowState(_Value):
    id: int | None = None
    label: str | None = Field(default=None, max_length=160)


class DialogState(WindowState):
    active: StrictBool | None = None


class FocusState(_Value):
    control_id: int | None = None
    label: str | None = Field(default=None, max_length=160)
    is_container: StrictBool | None = None


class ContainerState(_Value):
    associated: StrictBool = False
    id: int | None = None
    content: str | None = Field(default=None, max_length=160)
    item_count: int | None = Field(default=None, ge=0)
    item_number: int | None = Field(default=None, ge=1)


class SelectedItemState(_Value):
    associated: StrictBool = False
    label: str | None = Field(default=None, max_length=160)
    label2: str | None = Field(default=None, max_length=160)
    media_type: str | None = Field(default=None, max_length=160)
    dbid: int | None = Field(default=None, ge=1)
    year: int | None = Field(default=None, ge=1)


class PlaybackState(_Value):
    active: StrictBool | None = None
    kind: Literal["video", "audio"] | None = None


class UiFlags(_Value):
    busy: StrictBool | None = None
    fullscreen_video: StrictBool | None = None
    screensaver: StrictBool | None = None
    text_input: StrictBool | None = None


class UiObservation(_Value):
    schema_version: Literal[1] = 1
    window: WindowState = Field(default_factory=WindowState)
    dialog: DialogState = Field(default_factory=DialogState)
    focus: FocusState = Field(default_factory=FocusState)
    container: ContainerState = Field(default_factory=ContainerState)
    selected_item: SelectedItemState = Field(default_factory=SelectedItemState)
    playback: PlaybackState = Field(default_factory=PlaybackState)
    ui: UiFlags = Field(default_factory=UiFlags)
    changed_during_collection: StrictBool = False
    summary: str = Field(default="UI state unavailable.", max_length=400)


class UiIdentity(_Value):
    window_id: int | None = None
    dialog_id: int | None = None
    control_id: int | None = None
    is_container: StrictBool | None = None
    wrapper_id: int | None = None
    focus_available: StrictBool = False
    dialog_available: StrictBool = False


class PlayerFacts(_Value):
    active: StrictBool | None = None
    video: StrictBool | None = None
    audio: StrictBool | None = None


class UiLabels(_Value):
    window: str | None = Field(default=None, max_length=160)
    control_id: str | None = Field(default=None, max_length=160)
    control: str | None = Field(default=None, max_length=160)
    content: str | None = Field(default=None, max_length=160)
    count: str | None = Field(default=None, max_length=160)
    total: str | None = Field(default=None, max_length=160)
    number: str | None = Field(default=None, max_length=160)
    label: str | None = Field(default=None, max_length=160)
    label2: str | None = Field(default=None, max_length=160)
    media_type: str | None = Field(default=None, max_length=160)
    dbid: str | None = Field(default=None, max_length=160)
    year: str | None = Field(default=None, max_length=160)


class UiCollection(_Value):
    """Closed internal Bridge payload; it is never the MCP response schema."""

    before: UiIdentity
    after: UiIdentity
    player_before: PlayerFacts
    player_after: PlayerFacts
    labels: UiLabels = Field(default_factory=UiLabels)
    labels_available: StrictBool = False
    conditions: UiFlags = Field(default_factory=UiFlags)


def _number(value: str | int | None, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value.strip()) is None:
        return None
    parsed = int(value)
    return parsed if minimum <= parsed <= 2**31 - 1 else None


def _text(value: str | None) -> str | None:
    return " ".join(value.split())[:160] or None if value else None


def summarize(value: UiObservation) -> str:
    """Short deterministic prose; structured fields remain authoritative."""
    if value.ui.fullscreen_video and value.playback.active:
        return "Fullscreen video playback active."
    name = value.window.label or "Kodi"
    if value.dialog.active:
        name = value.dialog.label or name
        name += " open" if name.casefold().endswith("dialog") else " dialog open"
    subject = value.focus.label
    if value.selected_item.associated:
        subject = value.selected_item.label
    result = f"{name}."
    if subject:
        result += f" Focus is on {subject}"
        if (
            value.selected_item.associated
            and value.container.item_number is not None
            and value.container.item_count is not None
        ):
            result += (
                f", item {value.container.item_number} of {value.container.item_count}"
            )
        result += "."
    if value.playback.active:
        result += f" {value.playback.kind or 'Media'} playback active."
    return result[:400]


def normalize_collection(raw: UiCollection) -> UiObservation:
    before, after = raw.before, raw.after
    labels = raw.labels if raw.labels_available else UiLabels()
    focus = _number(before.control_id, minimum=1)
    modal = _number(before.dialog_id, minimum=1)
    if modal == 9999:
        modal = None

    def identity(value: UiIdentity) -> tuple[int | None, ...]:
        dialog = _number(value.dialog_id, minimum=1)
        return (
            _number(value.window_id, minimum=1),
            None if dialog == 9999 else dialog,
            _number(value.control_id, minimum=1),
        )

    identity_changed = identity(before) != identity(after)
    label_focus = _number(labels.control_id, minimum=1)
    gui_changed = (
        identity_changed
        or not before.focus_available
        or not after.focus_available
        or (raw.labels_available and label_focus != focus)
    )
    valid_focus = (
        before.focus_available
        and after.focus_available
        and not gui_changed
        and (before.wrapper_id is None or before.wrapper_id == focus)
    )
    if before.wrapper_id is not None and before.wrapper_id != focus:
        gui_changed = True
    count, total = _number(labels.count), _number(labels.total)
    number = _number(labels.number, minimum=1)
    if number is not None and (count is None or number > count):
        number = None
    associated = bool(
        valid_focus
        and focus is not None
        and before.is_container is True
        and before.wrapper_id == focus
        and modal is None
        and raw.labels_available
        and count is not None
        and total is not None
        and total >= count
    )
    selected = associated and number is not None and _text(labels.label) is not None
    pb = raw.player_after
    player_invalid = (
        pb.active is False and (pb.video is True or pb.audio is True)
    ) or (pb.video is True and pb.audio is True)
    playback = PlaybackState(
        active=None if player_invalid else pb.active,
        kind=(
            None
            if player_invalid or pb.active is not True
            else "video"
            if pb.video is True
            else "audio"
            if pb.audio is True
            else None
        ),
    )
    label = (
        None
        if identity_changed or not before.dialog_available
        else _text(labels.window)
    )
    value = UiObservation(
        window=WindowState(
            id=_number(before.window_id, minimum=1),
            label=label if modal is None else None,
        ),
        dialog=DialogState(
            active=modal is not None if before.dialog_available else None,
            id=modal,
            label=label if modal else None,
        ),
        focus=FocusState(
            control_id=focus,
            label=_text(labels.control) if valid_focus and focus is not None else None,
            is_container=before.is_container
            if valid_focus and focus is not None
            else None,
        ),
        container=(
            ContainerState(
                associated=True,
                id=focus,
                content=_text(labels.content),
                item_count=count,
                item_number=number,
            )
            if associated
            else ContainerState()
        ),
        selected_item=(
            SelectedItemState(
                associated=True,
                label=_text(labels.label),
                label2=_text(labels.label2),
                media_type=_text(labels.media_type),
                dbid=_number(labels.dbid, minimum=1),
                year=_number(labels.year, minimum=1),
            )
            if selected
            else SelectedItemState()
        ),
        playback=playback,
        ui=raw.conditions,
        changed_during_collection=(
            gui_changed or raw.player_before != pb or player_invalid
        ),
    )
    return value.model_copy(update={"summary": summarize(value)})
