"""Curated protected hybrid collector, compatible with Kodi's Python 3.0 API.

Never call generic ListItem/Container getInfoLabel from a request thread.
XBMC.GetInfoLabels/GetInfoBooleans marshal their work through Kodi's GUI
messenger. The gate serializes collectors, not GUI actions.
"""

import json
import os
import stat
import threading
import time
from contextlib import contextmanager

import xbmc
import xbmcgui
import xbmcvfs

_GATE = threading.Lock()
_LABELS = {
    "window": "System.CurrentWindow",
    "control_id": "System.CurrentControlID",
    "control": "System.CurrentControl",
}
_SCOPED = {
    "content": "Content",
    "count": "NumItems",
    "total": "NumAllItems",
    "number": "CurrentItem",
    "label": "ListItem.Label",
    "label2": "ListItem.Label2",
    "media_type": "ListItem.DBTYPE",
    "dbid": "ListItem.DBID",
    "year": "ListItem.Year",
}
_CONDITIONS = {
    "screensaver": "System.ScreenSaverActive",
    "text_input": "Window.IsVisible(virtualkeyboard)",
}


def _identity(classify=False):
    value = {
        "window_id": None,
        "dialog_id": None,
        "control_id": None,
        "is_container": None,
        "wrapper_id": None,
        "focus_available": False,
        "dialog_available": False,
    }
    try:
        value["window_id"] = xbmcgui.getCurrentWindowId()
        modal = xbmcgui.getCurrentWindowDialogId()
        value["dialog_available"] = True
        value["dialog_id"] = modal if modal not in (0, -1, 9999) else None
        window = xbmcgui.Window(value["dialog_id"] or value["window_id"])
        focus = window.getFocusId()
        value["control_id"] = focus if focus > 0 else None
        value["focus_available"] = True
        if classify and value["control_id"] is not None:
            control = window.getFocus()
            value["wrapper_id"] = control.getId()
            value["is_container"] = isinstance(control, xbmcgui.ControlList)
    except Exception:
        value["focus_available"] = False
    return value


def _player():
    try:
        player = xbmc.Player()
        return {
            "active": player.isPlaying(),
            "video": player.isPlayingVideo(),
            "audio": player.isPlayingAudio(),
        }
    except Exception:
        return {"active": None, "video": None, "audio": None}


def _rpc(method, params):
    request = {
        "jsonrpc": "2.0",
        "id": "ui-state-v1",
        "method": method,
        "params": params,
    }
    result = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    if result.get("id") != request["id"] or not isinstance(result.get("result"), dict):
        raise ValueError("UI collection failed")
    return result["result"]


@contextmanager
def _process_guard():
    # Shared across resident service interpreter lifetimes. The host target gate
    # controls admission; this lock also survives uncertain transport completion.
    path = xbmcvfs.translatePath("special://temp/kodi-lab-ui-observation.lock")
    try:
        previous = os.lstat(path)
    except FileNotFoundError:
        previous = None
    if previous is not None and not stat.S_ISREG(previous.st_mode):
        raise RuntimeError("Invalid observation lock")
    descriptor = os.open(
        path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("Invalid observation lock")
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")

            def lock():
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            def lock():
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        deadline = time.monotonic() + 3
        while True:
            try:
                lock()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("UI observation busy") from None
                time.sleep(0.01)
        yield
    finally:
        os.close(descriptor)


def collect(abort=None):
    if not _GATE.acquire(timeout=3):
        raise RuntimeError("UI observation busy")
    try:
        with _process_guard():
            if abort is not None and abort():
                raise RuntimeError("UI observation stopping")
            return _collect(abort)
    finally:
        _GATE.release()


def _collect(abort=None):
    before = _identity(True)
    player_before = _player()
    requested = dict(_LABELS)
    if before["dialog_id"] is None and before["is_container"] and before["control_id"]:
        requested.update(
            {
                key: f"Container({before['control_id']}).{suffix}"
                for key, suffix in _SCOPED.items()
            }
        )
    labels = {}
    labels_available = False
    if abort is not None and abort():
        raise RuntimeError("UI observation stopping")
    try:
        raw = _rpc("XBMC.GetInfoLabels", {"labels": list(requested.values())})
        labels = {
            key: raw[name][:160] if isinstance(raw.get(name), str) else None
            for key, name in requested.items()
        }
        labels_available = True
    except Exception:
        pass
    conditions = {
        key: None for key in ("busy", "fullscreen_video", "screensaver", "text_input")
    }
    if abort is not None and abort():
        raise RuntimeError("UI observation stopping")
    try:
        raw = _rpc("XBMC.GetInfoBooleans", {"booleans": list(_CONDITIONS.values())})
        conditions.update(
            {
                key: raw.get(name) if type(raw.get(name)) is bool else None
                for key, name in _CONDITIONS.items()
            }
        )
    except Exception:
        pass
    after = _identity()
    if abort is not None and abort():
        raise RuntimeError("UI observation stopping")
    conditions["busy"] = (
        before["dialog_id"] in (10138, 10160) if before["dialog_available"] else None
    )
    conditions["fullscreen_video"] = (
        before["window_id"] == 12005 if before["window_id"] is not None else None
    )
    if before["dialog_id"] == 10103:
        conditions["text_input"] = True
    return {
        "before": before,
        "after": after,
        "player_before": player_before,
        "player_after": _player(),
        "labels": labels,
        "labels_available": labels_available,
        "conditions": conditions,
    }
