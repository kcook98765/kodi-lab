from __future__ import annotations

import json
import runpy
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import ModuleType
from typing import Any

import pytest

from kodi_lab.kodi.ui_state import UiCollection, normalize_collection

ROOT = Path(__file__).parents[2] / "bridge/service.kodi_lab_bridge"


def _collector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []

    class List:
        def getId(self) -> int:
            return 50

    class Window:
        def __init__(self, window_id: int) -> None:
            assert window_id == 10025

        def getFocusId(self) -> int:
            return 50

        def getFocus(self) -> List:
            return List()

    class Player:
        def isPlaying(self) -> bool:
            return False

        isPlayingVideo = isPlaying
        isPlayingAudio = isPlaying

    def rpc(encoded: str) -> str:
        request = json.loads(encoded)
        requests.append(request)
        if request["method"] == "XBMC.GetInfoLabels":
            labels = {name: "" for name in request["params"]["labels"]}
            labels.update(
                {
                    "System.CurrentWindow": "Movies",
                    "System.CurrentControlID": "50",
                    "System.CurrentControl": "Film",
                    "Container(50).NumItems": "3",
                    "Container(50).NumAllItems": "4",
                    "Container(50).CurrentItem": "1",
                    "Container(50).ListItem.Label": "Film",
                }
            )
            result: object = labels
        else:
            result = {name: False for name in request["params"]["booleans"]}
        return json.dumps({"id": request["id"], "result": result})

    def forbidden(*args: Any) -> None:
        raise AssertionError("Unsafe direct generic label access")

    xbmc = ModuleType("xbmc")
    xbmc.Player, xbmc.executeJSONRPC = Player, rpc  # type: ignore[attr-defined]
    xbmc.getInfoLabel, xbmc.getCondVisibility = forbidden, forbidden  # type: ignore[attr-defined]
    gui = ModuleType("xbmcgui")
    gui.Window, gui.ControlList = Window, List  # type: ignore[attr-defined]
    gui.getCurrentWindowId = lambda: 10025  # type: ignore[attr-defined]
    gui.getCurrentWindowDialogId = lambda: 9999  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "xbmc", xbmc)
    monkeypatch.setitem(sys.modules, "xbmcgui", gui)
    vfs = ModuleType("xbmcvfs")
    vfs.translatePath = lambda path: str(tmp_path / "observation.lock")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "xbmcvfs", vfs)
    module = runpy.run_path(str(ROOT / "ui_observation.py"))
    return module, requests


def test_collector_uses_gui_rpc_only_for_labels_and_conditions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module, requests = _collector(monkeypatch, tmp_path)
    observation = normalize_collection(UiCollection.model_validate(module["collect"]()))
    assert observation.selected_item.associated
    assert [request["method"] for request in requests] == [
        "XBMC.GetInfoLabels",
        "XBMC.GetInfoBooleans",
    ]
    assert all(
        "FolderPath" not in name and name != "ListItem.Label"
        for name in requests[0]["params"]["labels"]
    )


def test_script_and_service_interpreters_share_collector_gate_and_release_on_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first, _ = _collector(monkeypatch, tmp_path)
    second, _ = _collector(monkeypatch, tmp_path)
    entered, release, started, second_entered = Event(), Event(), Event(), Event()

    def failing(abort: Any = None) -> None:
        entered.set()
        assert release.wait(2)
        raise ValueError("optional collector failure")

    def completed(abort: Any = None) -> str:
        second_entered.set()
        return "released"

    first["collect"].__globals__["_collect"] = failing
    second["collect"].__globals__["_collect"] = completed

    def second_call() -> object:
        started.set()
        return second["collect"]()

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(first["collect"])
        assert entered.wait(2)
        waiting = pool.submit(second_call)
        assert started.wait(2)
        assert not second_entered.wait(0.03)
        release.set()
        with pytest.raises(ValueError, match="collector failure"):
            pending.result(timeout=2)
        assert waiting.result(timeout=2) == "released"
    assert second["collect"]() == "released"
