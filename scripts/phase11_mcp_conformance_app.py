"""Test-only authenticated Phase 11 app for the official MCP server runner."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

from starlette.types import ASGIApp, Receive, Scope, Send

from kodi_lab.application.mcp_administration import READ_ONLY_INSPECTION_PRESET_ID
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.config import Settings
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.web.app import WebApplicationContext, create_app
from tests.web.conftest import INSTANCE_ID, FakeBroker, seed_instance


class _BearerInjection:
    """Supply the test credential the third-party runner cannot configure."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._authorization = f"Bearer {token}".encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            headers = [
                (key, value)
                for key, value in scope.get("headers", [])
                if key.lower() != b"authorization"
            ]
            headers.append((b"authorization", self._authorization))
            scope["headers"] = headers
        await self._app(scope, receive, send)


def _build() -> ASGIApp:
    root_value = os.environ.get("KODI_LAB_CONFORMANCE_ROOT")
    if root_value is None:
        raise RuntimeError("KODI_LAB_CONFORMANCE_ROOT is required")
    root = Path(root_value).resolve()
    root.mkdir(parents=True, exist_ok=True)
    development_root = root / "development"
    development_root.mkdir()
    engine = create_database_engine(f"sqlite:///{root / 'conformance.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    seed_instance(factory)
    app = create_app(
        settings=Settings(
            data_root=root / "data",
            development_root=development_root,
            web_port=8180,
        ),
        session_factory=factory,
        broker=FakeBroker(snapshot_data_root=root / "data"),
        readiness=ReadinessTaskRegistry(max_workers=1),
    )
    context = cast(WebApplicationContext, app.state.kodi_lab)
    client = context.mcp_administration.create_client(
        display_name="Official conformance runner",
        preset_id=READ_ONLY_INSPECTION_PRESET_ID,
    )
    context.mcp_administration.assign_target_preset(
        INSTANCE_ID, READ_ONLY_INSPECTION_PRESET_ID
    )
    context.mcp_administration.grant_target(client.id, INSTANCE_ID)
    token = context.mcp_administration.issue_credential(client.id).token
    return _BearerInjection(app, token)


application = _build()
