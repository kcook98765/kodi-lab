"""Localhost-only Web process entry point."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any, cast

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from kodi_lab.broker.client import BrokerClient
from kodi_lab.config import Settings
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.web.app import create_app


class McpAccessLogFilter(logging.Filter):
    """MCP URLs are fixed; suppress query/path material in request-line logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            peer, method, path, version, status = record.args
            if isinstance(path, str) and path.split("?", 1)[0].startswith("/mcp"):
                record.args = (peer, method, "/mcp", version, status)
        return True


def application_logging_config() -> dict[str, Any]:
    """Return the Web process logging graph, including bounded MCP audits."""

    config = deepcopy(LOGGING_CONFIG)
    config["filters"] = {"mcp_access": {"()": McpAccessLogFilter}}
    config["handlers"]["access"]["filters"] = ["mcp_access"]
    loggers = cast(dict[str, dict[str, object]], config["loggers"])
    loggers["kodi_lab.mcp"] = {
        "handlers": ["default"],
        "level": "INFO",
        "propagate": False,
    }
    loggers["kodi_lab.screenshot.diagnostic"] = {
        "handlers": ["default"],
        "level": "INFO",
        "propagate": False,
    }
    return config


def main() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    engine = create_database_engine(settings.database_url)
    app = create_app(
        settings=settings,
        session_factory=create_session_factory(engine),
        broker=BrokerClient(settings.runtime_root / "broker.sock"),
    )
    uvicorn.run(
        app,
        host=settings.effective_web_bind_host,
        port=settings.web_port,
        access_log=True,
        log_config=application_logging_config(),
    )


if __name__ == "__main__":
    main()
