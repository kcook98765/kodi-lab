from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from kodi_lab.managed import player
from kodi_lab.managed.protocol import Refusal, validate
from kodi_lab.managed.runtime import Runtime

BINDING = ("container", "same start")
LOCAL = "/tmp/campaign-first.mp4"
REMOTE = "https://example.invalid/fixture?private=fixture-secret"


def observe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replies: list[Any],
    *,
    binding: tuple[str, str] = BINDING,
    expected: str | None = None,
) -> dict[str, Any]:
    root = tmp_path / "state"
    root.mkdir(mode=0o700, exist_ok=True)
    runtime = Runtime(root, lambda *_args, **_kwargs: None)
    (runtime.credentials / "rpc").write_bytes(b"synthetic per-start key")

    def native(method: str, **_params: Any) -> Any:
        name, response = replies.pop(0)
        assert method == name
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(runtime, "rpc", native)
    monkeypatch.setattr(player, "incarnation", lambda _runtime: binding)
    reply = player.observe(runtime, BINDING, expected)
    assert not replies
    return reply


def samples(first: str, second: str, speed: int) -> list[Any]:
    active = [{"playerid": 1, "type": "video"}]
    return [
        ("Player.GetActivePlayers", active),
        ("Player.GetProperties", {"speed": speed}),
        ("Player.GetItem", {"item": {"file": first, "title": "private title"}}),
        ("Player.GetProperties", {"speed": speed}),
        ("Player.GetItem", {"item": {"file": second}}),
        ("Player.GetActivePlayers", active),
    ]


def test_stable_playing_and_paused_and_private_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = hashlib.sha256(LOCAL.encode()).hexdigest()
    playing = observe(
        tmp_path, monkeypatch, samples(LOCAL, LOCAL, 1), expected=expected
    )
    paused = observe(tmp_path, monkeypatch, samples(LOCAL, LOCAL, 0), expected=expected)
    remote = observe(
        tmp_path, monkeypatch, samples(REMOTE, REMOTE, 1), expected=expected
    )
    assert playing["state"] == "playing"
    assert paused["state"] == "paused"
    assert playing["matches_expected_local_media"] is True
    assert paused["media_key"] == playing["media_key"] != remote["media_key"]
    assert remote["incarnation"] == playing["incarnation"]
    assert "matches_expected_local_media" not in remote
    assert all(
        part not in str(remote)
        for part in ("fixture-secret", "private title", "invalid")
    )


def test_idle_uncertainty_and_runtime_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idle: list[Any] = [("Player.GetActivePlayers", [])] * 2
    assert observe(tmp_path, monkeypatch, idle.copy())["state"] == "idle"
    assert observe(tmp_path, monkeypatch, idle, binding=("container", "new start")) == {
        "state": "unavailable"
    }
    changing = samples(LOCAL, REMOTE, 1)[:-1]
    assert observe(tmp_path, monkeypatch, changing)["state"] == "uncertain"
    unavailable = [("Player.GetActivePlayers", Refusal("private fixture error"))]
    assert observe(tmp_path, monkeypatch, unavailable) == {"state": "unavailable"}


def test_optional_arguments_keep_gui_compatible() -> None:
    assert validate("managed_gui", {}) == ("managed_gui", {})
    assert validate("managed_gui", {"generation": "matrix", "include_player": "true"})
    for invalid in (
        {"include_player": "anything"},
        {"expected_local_media_sha256": "a" * 64},
        {"include_player": "true", "expected_local_media_sha256": LOCAL},
    ):
        with pytest.raises(Refusal, match="invalid_arguments"):
            validate("managed_gui", invalid)
