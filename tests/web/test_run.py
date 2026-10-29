from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn

from kodi_lab.web import run


@pytest.mark.parametrize("enabled", (False, True))
def test_packaged_web_entrypoint_installs_mcp_audit_logging(
    monkeypatch: Any,
    enabled: bool,
) -> None:
    observed: dict[str, object] = {}
    settings = SimpleNamespace(
        database_url="sqlite+pysqlite:///:memory:",
        runtime_root=Path("/runtime"),
        web_host="10.251.0.10",
        effective_web_bind_host="10.251.0.10",
        web_port=8180,
        experimental_novnc_instances=("fixture-instance",) if enabled else (),
    )

    monkeypatch.setattr(run, "Settings", lambda **kwargs: settings)
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
    from kodi_lab.web.novnc_protocol import BoundedWebSocketProtocol

    assert observed["ws"] == (BoundedWebSocketProtocol if enabled else "auto")
    assert observed["ws_max_size"] == 256 and observed["ws_max_queue"] == 1


def test_packaged_web_log_config_emits_mcp_audit_to_process_stderr() -> None:
    script = "\n".join(
        (
            "import json",
            "import logging",
            "import logging.config",
            "from kodi_lab.web.run import application_logging_config",
            "logging.config.dictConfig(application_logging_config())",
            "logging.getLogger('kodi_lab.mcp').info(",
            "    'mcp_request %s',",
            "    json.dumps({'event': 'mcp_request', 'schema_version': 1, "
            "'tool': 'target_list', 'outcome': 'success'}, separators=(',', ':'))",
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
    line = next(
        line for line in completed.stderr.splitlines() if "mcp_request " in line
    )
    payload = json.loads(line.partition("mcp_request ")[2])
    assert payload == {
        "event": "mcp_request",
        "schema_version": 1,
        "tool": "target_list",
        "outcome": "success",
    }


def test_live_audit_outputs_only_approved_fields() -> None:
    script = "\n".join(
        (
            "import logging, logging.config",
            "from kodi_lab.web.run import application_logging_config",
            "logging.config.dictConfig(application_logging_config())",
            "logger = logging.getLogger('kodi_lab.live_display.audit')",
            "logger.info('managed live display ended', extra={",
            "'display_session_id':'fixture-session','managed_instance_id':'fixture-target',",
            "'result':'display_stopped','established':True,'frame_count':3,",
            "'aggregate_byte_count':30000,'capture_ms':123.5,'encode_ms':4.25,",
            "'send_ms':2.5,'decode_ms':18.0,'skipped_frames':1,",
            "'password':'private-audit-canary','pixels':b'raw-image-canary',",
            "'csrf_token':'private-token-canary'})",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    assert completed.stdout == ""
    payload = json.loads(completed.stderr.strip())
    assert payload["event"] == "managed_live_display"
    assert payload["frame_count"] == 3 and payload["capture_ms"] == 123.5
    assert payload["encode_ms"] == 4.25 and payload["send_ms"] == 2.5
    assert payload["decode_ms"] == 18.0 and payload["skipped_frames"] == 1
    assert not {"password", "pixels", "csrf_token"} & set(payload)
    assert "canary" not in completed.stderr


def test_deployment_measurements_reach_packaged_web_stderr() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import logging.config",
                    "from kodi_lab.web.run import application_logging_config",
                    "from kodi_lab.development.measurements import "
                    "DeploymentMeasurement",
                    "logging.config.dictConfig(application_logging_config())",
                    "DeploymentMeasurement('operation', 'managed_workspace').emit()",
                )
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == ""
    payload = json.loads(completed.stderr.partition("deployment measurement ")[2])
    assert payload["operation_id"] == "operation"
    assert payload["layer"] == "managed_workspace"
    assert payload["completed"] is False
