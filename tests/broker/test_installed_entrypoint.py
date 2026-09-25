"""Installed broker process and readiness behavior."""

from __future__ import annotations

import logging
import signal
from threading import Event
from types import SimpleNamespace

import pytest

from kodi_lab.broker import run
from kodi_lab.broker.errors import BrokerAuthorizationError, BrokerUnavailableError


def test_entrypoint_serves_production_until_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[object] = []
    monkeypatch.setattr("kodi_lab.broker.run.sys.argv", ["kodi-lab-broker"])
    monkeypatch.setattr("kodi_lab.broker.run._configure_logging", lambda: None)
    monkeypatch.setattr(
        "kodi_lab.broker.run.signal.signal",
        lambda signum, _handler: observed.append(signum),
    )

    class FakeServer:
        def serve_forever(self, stopped: object) -> None:
            observed.append(stopped)

    monkeypatch.setattr(
        "kodi_lab.broker.run.BrokerServer", SimpleNamespace(production=FakeServer)
    )
    assert run.main() == 0
    assert signal.SIGTERM in observed
    assert signal.SIGINT in observed
    assert any(isinstance(item, Event) for item in observed)


def test_entrypoint_rejects_unrecognized_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kodi_lab.broker.run.sys.argv", ["kodi-lab-broker", "--socket", "/tmp/x"]
    )
    monkeypatch.setattr(
        "kodi_lab.broker.run.BrokerServer",
        SimpleNamespace(production=lambda: pytest.fail("broker must not start")),
    )
    assert run.main() == 2


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (None, 1),
        (BrokerAuthorizationError("unauthorized_peer", "denied", request_id=None), 0),
        (BrokerUnavailableError("unavailable"), 1),
    ],
)
def test_healthcheck_requires_live_broker_response(
    monkeypatch: pytest.MonkeyPatch, error: Exception | None, expected: int
) -> None:
    def ping() -> None:
        if error is not None:
            raise error

    monkeypatch.setattr(
        "kodi_lab.broker.run.BrokerClient", lambda: SimpleNamespace(ping=ping)
    )
    assert run._healthcheck() == expected


def test_container_audit_formatter_preserves_only_bounded_broker_fields() -> None:
    record = logging.LogRecord(
        "kodi_lab.broker.audit",
        logging.INFO,
        __file__,
        1,
        "broker request completed",
        (),
        None,
    )
    record.broker_result_code = "success"
    record.secret = "do not log"
    formatted = run._AuditFormatter().format(record)
    assert '"broker_result_code":"success"' in formatted
    assert '"secret"' not in formatted
