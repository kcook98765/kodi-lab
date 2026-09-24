from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import uvicorn

from kodi_lab.web import run


def test_packaged_web_entrypoint_installs_mcp_audit_logging(
    monkeypatch: Any,
) -> None:
    observed: dict[str, object] = {}
    settings = SimpleNamespace(
        database_url="sqlite+pysqlite:///:memory:",
        runtime_root=Path("/runtime"),
        web_host="10.251.0.10",
        web_port=8180,
    )

    monkeypatch.setattr(run, "Settings", lambda: settings)
    monkeypatch.setattr(run, "create_database_engine", lambda url: object())
    monkeypatch.setattr(run, "create_session_factory", lambda engine: object())
    monkeypatch.setattr(run, "BrokerClient", lambda path: object())
    monkeypatch.setattr(run, "create_app", lambda **kwargs: object())
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda app, **kwargs: observed.update({"app": app, **kwargs}),
    )

    run.main()

    config = observed["log_config"]
    assert isinstance(config, dict)
    assert config["loggers"]["kodi_lab.mcp"] == {
        "handlers": ["default"],
        "level": "INFO",
        "propagate": False,
    }
    assert observed["host"] == "10.251.0.10"
    assert observed["port"] == 8180


def test_packaged_web_log_config_emits_mcp_audit_to_process_stderr() -> None:
    script = "\n".join(
        (
            "import logging",
            "import logging.config",
            "from kodi_lab.web.run import application_logging_config",
            "logging.config.dictConfig(application_logging_config())",
            "logging.getLogger('kodi_lab.mcp').info(",
            "    'mcp_request client_id=client tool=target_list outcome=success'",
            ")",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == ""
    assert "mcp_request client_id=client tool=target_list outcome=success" in (
        completed.stderr
    )
