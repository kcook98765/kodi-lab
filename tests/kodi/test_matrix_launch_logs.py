"""Matrix API-shaped doubles exercise the unchanged reviewed resident bytes.

These are protocol regressions, not native Matrix installation acceptance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.kodi.test_kodi_logs import logs as logs
from tests.kodi.test_kodi_logs import read
from tests.kodi.test_managed_launch_bridge import setup_bridge


@pytest.mark.parametrize("lose_response", [False, True])
def test_matrix_plugin_dispatch_replay_and_pending_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lose_response: bool
) -> None:
    bridge, state, payload, calls = setup_bridge(
        tmp_path,
        monkeypatch,
        '<extension point="xbmc.python.pluginsource" library="main.py">'
        "<provides>video</provides></extension>",
    )
    monkeypatch.setattr(bridge["xbmc"], "getInfoLabel", lambda _name: "19.4 (19.4.0)")
    # Matrix lacks Addon.getSettings. Neither launch nor logs needs that API.
    assert not hasattr(bridge["xbmcaddon"].Addon(), "getSettings")
    if lose_response:
        save = bridge["_save_launch"]

        def fail_after_dispatch(receipt: dict[str, Any]) -> None:
            if receipt["state"] == "accepted":
                raise OSError("lost accepted receipt")
            save(receipt)

        monkeypatch.setitem(
            bridge["_managed_launch"].__globals__, "_save_launch", fail_after_dispatch
        )
        with pytest.raises(OSError, match="lost accepted receipt"):
            bridge["_managed_launch"](payload, state)
    else:
        first = bridge["_managed_launch"](payload, state)
        assert first["identity"]["kodi_version"] == "19.4"
        assert first["receipt"]["state"] == "accepted"
    replay = bridge["_managed_launch"](
        {"launch_id": payload["launch_id"]}, state, status_only=True
    )
    assert replay["receipt"]["state"] == ("pending" if lose_response else "accepted")
    assert bridge["_managed_launch"](payload, state) == replay
    assert calls == ["ActivateWindow(videos,plugin://plugin.video.launch/,return)"]


def test_matrix_logs_need_only_translated_logpath_and_preserve_rotation(
    logs: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, root = logs
    monkeypatch.setattr(bridge["xbmc"], "getInfoLabel", lambda _name: "19.4 (19.4.0)")
    assert not hasattr(bridge["xbmcaddon"].Addon(), "getSettings")
    path = root / "kodi.log"
    path.write_text("matrix iteration A\n", encoding="utf-8")
    stats = read(logs, log="current")
    assert stats.bytes_read == 0
    assert read(logs, mode="tail", max_lines=200, max_bytes=8192).content == (
        "matrix iteration A\n"
    )
    path.rename(root / "kodi.old.log")
    path.write_text("matrix iteration B\n", encoding="utf-8")
    with pytest.raises(Exception, match=r"^log_changed$"):
        read(logs, mode="tail", snapshot_id=stats.snapshot_id)
    assert read(logs, log="previous", mode="tail").content == "matrix iteration A\n"
