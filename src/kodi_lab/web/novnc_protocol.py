"""Route-specific wire limits; the JPEG/other WebSocket ceiling stays 256."""

from __future__ import annotations

import re
from typing import Any

from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol

_RFB_PATH = re.compile(
    r"/api/instances/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/novnc"
)


def incoming_limit(path: str) -> int:
    return 65536 if _RFB_PATH.fullmatch(path) else 256


class BoundedWebSocketProtocol(WebSocketProtocol):
    async def process_request(self, path: str, request_headers: Any) -> Any:
        # Applied before WebSocket handshake/reading, including fragmented messages.
        # Queries/encoded paths do not acquire the larger budget.
        self.max_size = incoming_limit(path)
        self.max_queue = 1
        return await super().process_request(path, request_headers)
