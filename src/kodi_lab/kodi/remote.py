"""Closed, at-most-once Kodi browser-remote JSON-RPC adapter."""

from __future__ import annotations

import http.client
import json
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID

from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.kodi.readiness import MAX_JSONRPC_RESPONSE_BYTES

REMOTE_RUNTIME_REVISION_IDS = frozenset(
    {
        UUID("51950000-0000-4000-8000-000000000005"),
        UUID("52000000-0000-4000-8000-000000000005"),
        UUID("3a000000-0000-4000-8000-000000000006"),
        UUID("52200000-0000-4000-8000-000000000005"),
    }
)
REMOTE_TIMEOUT_SECONDS = 3.0


class BrowserRemoteAction(StrEnum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    SELECT = "select"
    BACK = "back"
    HOME = "home"
    CONTEXT_MENU = "context_menu"
    INFO = "info"
    PLAY_PAUSE = "play_pause"
    STOP = "stop"
    PREVIOUS = "previous"
    NEXT = "next"
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    MUTE_TOGGLE = "mute_toggle"


NAVIGATION_ACTIONS = frozenset(
    {
        BrowserRemoteAction.UP,
        BrowserRemoteAction.DOWN,
        BrowserRemoteAction.LEFT,
        BrowserRemoteAction.RIGHT,
        BrowserRemoteAction.SELECT,
        BrowserRemoteAction.BACK,
        BrowserRemoteAction.HOME,
        BrowserRemoteAction.CONTEXT_MENU,
        BrowserRemoteAction.INFO,
    }
)
PLAYER_ACTIONS = frozenset(
    {
        BrowserRemoteAction.PLAY_PAUSE,
        BrowserRemoteAction.STOP,
        BrowserRemoteAction.PREVIOUS,
        BrowserRemoteAction.NEXT,
    }
)

_DIRECT_REQUESTS: dict[BrowserRemoteAction, tuple[str, dict[str, object] | None]] = {
    BrowserRemoteAction.UP: ("Input.Up", None),
    BrowserRemoteAction.DOWN: ("Input.Down", None),
    BrowserRemoteAction.LEFT: ("Input.Left", None),
    BrowserRemoteAction.RIGHT: ("Input.Right", None),
    BrowserRemoteAction.SELECT: ("Input.Select", None),
    BrowserRemoteAction.BACK: ("Input.Back", None),
    BrowserRemoteAction.HOME: ("Input.Home", None),
    BrowserRemoteAction.CONTEXT_MENU: ("Input.ContextMenu", None),
    BrowserRemoteAction.INFO: ("Input.Info", None),
    BrowserRemoteAction.VOLUME_UP: ("Application.SetVolume", {"volume": "increment"}),
    BrowserRemoteAction.VOLUME_DOWN: (
        "Application.SetVolume",
        {"volume": "decrement"},
    ),
    BrowserRemoteAction.MUTE_TOGGLE: ("Application.SetMute", {"mute": "toggle"}),
}


class RemoteKodiRejectedError(RuntimeError):
    """Kodi rejected or returned an invalid bounded response."""


class RemoteNoActivePlayerError(RuntimeError):
    """A player-relative action has no deterministic active player."""


class RemoteActionAmbiguousError(RuntimeError):
    """The control request may have reached Kodi; it must not be retried."""


class _Transport(Protocol):
    def post(
        self, *, host: str, port: int, path: str, body: bytes, timeout: float
    ) -> bytes: ...


class BoundedRemoteHttpTransport:
    """One bounded HTTP attempt; transport failure after dispatch is ambiguous."""

    def post(
        self, *, host: str, port: int, path: str, body: bytes, timeout: float
    ) -> bytes:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        dispatched = False
        try:
            connection.connect()
            dispatched = True
            connection.request(
                "POST",
                path,
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            if response.status < 200 or response.status >= 300:
                raise RemoteKodiRejectedError("Kodi rejected the fixed request")
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    if int(length) > MAX_JSONRPC_RESPONSE_BYTES:
                        raise RemoteKodiRejectedError("Kodi response is too large")
                except ValueError as error:
                    raise RemoteKodiRejectedError(
                        "Kodi response length is invalid"
                    ) from error
            payload = response.read(MAX_JSONRPC_RESPONSE_BYTES + 1)
            if len(payload) > MAX_JSONRPC_RESPONSE_BYTES:
                raise RemoteKodiRejectedError("Kodi response is too large")
            return payload
        except RemoteKodiRejectedError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            if dispatched:
                raise RemoteActionAmbiguousError(
                    "Kodi acknowledgement was not received"
                ) from error
            raise RemoteKodiRejectedError("Kodi is unavailable") from error
        finally:
            connection.close()


class KodiBrowserRemoteAdapter:
    """Build only the server-defined remote requests for one trusted endpoint."""

    def __init__(
        self,
        observation: ObserveManagedKodiRuntimeResult,
        profile: RuntimeExecutionProfile,
        *,
        transport: _Transport | None = None,
    ) -> None:
        if observation.runtime_revision_id != str(profile.runtime_revision_id):
            raise ValueError("observation and profile revisions differ")
        if observation.jsonrpc_port != profile.jsonrpc_port:
            raise ValueError("observation and profile ports differ")
        self._host = observation.ipv4_address
        self._port = profile.jsonrpc_port
        self._path = profile.jsonrpc_path
        self._timeout = min(REMOTE_TIMEOUT_SECONDS, profile.http_timeout_seconds)
        self._transport = transport or BoundedRemoteHttpTransport()

    def dispatch(self, action: BrowserRemoteAction, invocation_id: UUID) -> None:
        params: dict[str, object] | None
        if action in PLAYER_ACTIONS:
            player_id = self._active_player(invocation_id)
            method, params = self._player_request(action, player_id)
        else:
            method, params = _DIRECT_REQUESTS[action]
        result = self._request(
            f"remote-{invocation_id}", method, params, ambiguous_on_transport=True
        )
        if not self._valid_result(action, result):
            raise RemoteKodiRejectedError("Kodi did not acknowledge the remote action")

    @staticmethod
    def _valid_result(action: BrowserRemoteAction, result: object) -> bool:
        if action is BrowserRemoteAction.PLAY_PAUSE:
            return (
                isinstance(result, dict)
                and set(result) == {"speed"}
                and isinstance(result.get("speed"), int)
                and not isinstance(result.get("speed"), bool)
            )
        if action in {BrowserRemoteAction.VOLUME_UP, BrowserRemoteAction.VOLUME_DOWN}:
            return (
                isinstance(result, int)
                and not isinstance(result, bool)
                and 0 <= result <= 100
            )
        if action is BrowserRemoteAction.MUTE_TOGGLE:
            return isinstance(result, bool)
        return result == "OK"

    def _active_player(self, invocation_id: UUID) -> int:
        result = self._request(
            f"remote-player-{invocation_id}",
            "Player.GetActivePlayers",
            None,
            ambiguous_on_transport=False,
        )
        if not isinstance(result, list) or any(
            not isinstance(item, dict) for item in result
        ):
            raise RemoteKodiRejectedError("Active-player response is invalid")
        candidates: list[tuple[int, int]] = []
        priority = {"video": 0, "audio": 1, "picture": 2}
        for item in result:
            player_id = item.get("playerid")
            player_type = item.get("type")
            if (
                not isinstance(player_id, int)
                or isinstance(player_id, bool)
                or player_id < 0
                or not isinstance(player_type, str)
                or player_type not in priority
            ):
                raise RemoteKodiRejectedError("Active-player entry is invalid")
            candidates.append((priority[player_type], player_id))
        if not candidates:
            raise RemoteNoActivePlayerError("No Kodi player is active")
        return min(candidates)[1]

    @staticmethod
    def _player_request(
        action: BrowserRemoteAction, player_id: int
    ) -> tuple[str, dict[str, object]]:
        if action is BrowserRemoteAction.PLAY_PAUSE:
            return "Player.PlayPause", {"playerid": player_id}
        if action is BrowserRemoteAction.STOP:
            return "Player.Stop", {"playerid": player_id}
        if action is BrowserRemoteAction.PREVIOUS:
            return "Player.GoTo", {"playerid": player_id, "to": "previous"}
        if action is BrowserRemoteAction.NEXT:
            return "Player.GoTo", {"playerid": player_id, "to": "next"}
        raise AssertionError("non-player action reached player mapping")

    def _request(
        self,
        request_id: str,
        method: str,
        params: dict[str, object] | None,
        *,
        ambiguous_on_transport: bool,
    ) -> object:
        request: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params
        try:
            payload = self._transport.post(
                host=self._host,
                port=self._port,
                path=self._path,
                body=json.dumps(request, separators=(",", ":")).encode(),
                timeout=self._timeout,
            )
        except RemoteActionAmbiguousError:
            if ambiguous_on_transport:
                raise
            raise RemoteKodiRejectedError(
                "Active-player query was not acknowledged"
            ) from None
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RemoteKodiRejectedError("Kodi response is invalid") from error
        if (
            not isinstance(document, dict)
            or document.get("jsonrpc") != "2.0"
            or document.get("id") != request_id
            or set(document)
            not in ({"jsonrpc", "id", "result"}, {"jsonrpc", "id", "error"})
        ):
            raise RemoteKodiRejectedError("Kodi response envelope is invalid")
        if "error" in document:
            raise RemoteKodiRejectedError("Kodi rejected the remote action")
        return cast(dict[str, object], document)["result"]


def fixed_request_mapping() -> dict[BrowserRemoteAction, tuple[str, object]]:
    """Expose mappings for tests/documentation, never through the Web API."""
    result: dict[BrowserRemoteAction, tuple[str, object]] = {
        action: (method, params)
        for action, (method, params) in _DIRECT_REQUESTS.items()
    }
    result.update(
        {
            BrowserRemoteAction.PLAY_PAUSE: ("Player.PlayPause", "derived_playerid"),
            BrowserRemoteAction.STOP: ("Player.Stop", "derived_playerid"),
            BrowserRemoteAction.PREVIOUS: ("Player.GoTo", "derived_playerid_previous"),
            BrowserRemoteAction.NEXT: ("Player.GoTo", "derived_playerid_next"),
        }
    )
    return result
