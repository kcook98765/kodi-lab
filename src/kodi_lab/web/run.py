"""Localhost-only Web process entry point."""

from __future__ import annotations

import json
import logging
import math
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


class LivePreviewAuditFormatter(logging.Formatter):
    """Serialize only bounded source-owned audit fields; never arbitrary extras."""

    def format(self, record: logging.LogRecord) -> str:
        messages = {"managed live display started", "managed live display ended"}
        data: dict[str, object] = {"event": "managed_live_display"}
        data["message"] = (
            record.msg if record.msg in messages else "managed live display"
        )
        for key in (
            "display_session_id",
            "managed_instance_id",
            "interface",
            "actor",
            "started_at",
            "ended_at",
            "result",
            "established",
            "frame_count",
            "aggregate_byte_count",
            "skipped_frames",
            "capture_ms",
            "encode_ms",
            "send_ms",
            "decode_ms",
        ):
            value = getattr(record, key, None)
            if (
                (isinstance(value, str) and len(value) <= 128 and value.isprintable())
                or type(value) in {bool, int}
                or (type(value) is float and math.isfinite(value))
            ):
                data[key] = value
        return json.dumps(data, separators=(",", ":"), sort_keys=True)


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
    config["formatters"]["live_preview"] = {"()": LivePreviewAuditFormatter}
    config["handlers"]["live_preview"] = {
        "class": "logging.StreamHandler",
        "formatter": "live_preview",
        "stream": "ext://sys.stderr",
    }
    loggers["kodi_lab.live_display.audit"] = {
        "handlers": ["live_preview"],
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
        ws_max_size=256,
        ws_max_queue=1,
        log_config=application_logging_config(),
    )


if __name__ == "__main__":
    main()
