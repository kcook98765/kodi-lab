"""The installed Web runtime can serve the managed live-display route."""

from uvicorn.protocols.websockets.auto import AutoWebSocketsProtocol


def test_uvicorn_has_a_websocket_protocol_for_mediated_display() -> None:
    assert AutoWebSocketsProtocol is not None
