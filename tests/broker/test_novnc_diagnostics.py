from __future__ import annotations

import json
import logging
import socket
import struct
from threading import Event, Thread
from typing import Any
from uuid import uuid4

import pytest

from kodi_lab.viewer_diagnostics import LOGGER

from .test_novnc_transport import DockerDouble, make_tunnel


@pytest.mark.parametrize("ending", ["normal", "helper_exit", "stderr", "revoked"])
def test_helper_terminal_evidence_is_correlated_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ending: str,
) -> None:
    # Alembic logging configuration may have disabled this logger earlier.
    monkeypatch.setattr(logging.getLogger(LOGGER), "disabled", False)
    monkeypatch.setattr(logging.getLogger(LOGGER), "propagate", True)
    caplog.set_level(logging.INFO, logger=LOGGER)

    class Docker(DockerDouble):
        def exec_inspect(self, _: str) -> dict[str, Any]:
            return {"Running": False, "ExitCode": 23, "secret": "inspect-secret"}

    client = Docker()
    tunnel = make_tunnel(client, valid=lambda: ending != "revoked")
    from dataclasses import replace

    tunnel.request = replace(tunnel.request, attempt=uuid4())
    web, broker = socket.socketpair()
    failures: list[Exception] = []

    def run() -> None:
        try:
            tunnel.run(broker, Event())
        except Exception as error:
            failures.append(error)

    worker = Thread(target=run)
    worker.start()
    try:
        if ending in {"normal", "helper_exit"}:
            client.helper.settimeout(1)
            web.settimeout(1)
            challenge = b"KLVNCARM" + b"n" * 32
            payload = challenge + b"RFB-payload-secret"
            client.helper.sendall(
                struct.pack("!B3sI", 1, b"\0\0\0", len(payload)) + payload
            )
            assert client.helper.recv(37) == struct.pack("!BI", 4, 32) + b"n" * 32
            assert web.recv(100) == b"RFB-payload-secret"
            if ending == "normal":
                web.close()
            else:
                client.helper.shutdown(socket.SHUT_RDWR)
        elif ending == "stderr":
            payload = b"stderr-secret"
            client.helper.sendall(
                struct.pack("!B3sI", 2, b"\0\0\0", len(payload)) + payload
            )
        worker.join(2)
        assert not worker.is_alive()
    finally:
        web.close()
        broker.close()
        worker.join(1)
    assert bool(failures) == (ending in {"stderr", "revoked"})
    events = [
        json.loads(record.message) for record in caplog.records if record.name == LOGGER
    ]
    assert len(events) == (1 if ending == "revoked" else 2)
    terminal = events[-1]
    assert terminal["event"] == "helper_ended"
    assert terminal["attempt"] == str(tunnel.request.attempt)
    assert (
        terminal["reason"]
        == {
            "normal": "disconnect",
            "helper_exit": "exec_eof",
            "stderr": "helper_stderr",
            "revoked": "revoked",
        }[ending]
    )
    assert terminal["terminated"] is True
    if ending != "revoked":
        assert terminal["exit_code"] == 23
    assert terminal["armed"] == (ending in {"normal", "helper_exit"})
    assert "secret" not in json.dumps(events)
    assert tunnel.result.done.is_set()
