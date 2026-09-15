"""Localhost-only Web process entry point."""

from __future__ import annotations

import uvicorn

from kodi_lab.broker.client import BrokerClient
from kodi_lab.config import Settings
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.web.app import create_app


def main() -> None:
    settings = Settings()
    engine = create_database_engine(settings.database_url)
    app = create_app(
        settings=settings,
        session_factory=create_session_factory(engine),
        broker=BrokerClient(settings.runtime_root / "broker.sock"),
    )
    uvicorn.run(app, host=settings.web_host, port=settings.web_port, access_log=True)


if __name__ == "__main__":
    main()
