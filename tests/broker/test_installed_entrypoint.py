"""Installed broker process and readiness behavior."""

from __future__ import annotations

import logging
import signal
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from kodi_lab.broker import run
from kodi_lab.broker.errors import BrokerAuthorizationError, BrokerUnavailableError


def test_entrypoint_serves_production_until_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[object] = []
    monkeypatch.setattr("kodi_lab.broker.run.sys.argv", ["kodi-lab-broker"])
    monkeypatch.setattr("kodi_lab.broker.run._configure_logging", lambda: None)
    monkeypatch.setattr(
        "kodi_lab.broker.run.signal.signal",
        lambda signum, _handler: observed.append(signum),
    )
    retirement_started = Event()
    inventory_started = Event()

    def inventory(
        stopped: Event, managed_root: Path, runtime_directory: Path, web_group: int
    ) -> None:
        assert managed_root == run.MANAGED_STATE
        assert runtime_directory == tmp_path
        assert web_group == 10001
        inventory_started.set()
        stopped.wait(2)

    socket = SimpleNamespace(runtime_directory=tmp_path, socket_group_gid=10001)
    monkeypatch.setattr(
        run,
        "ProductionBrokerConfiguration",
        SimpleNamespace(resolve=lambda: SimpleNamespace(socket=socket)),
    )
    monkeypatch.setattr(run, "serve_storage_inventory", inventory)

    def retirement(_worker: object, stopped: Event) -> None:
        retirement_started.set()
        stopped.wait(2)

    monkeypatch.setattr(
        "kodi_lab.broker.reviewed_installations.DockerReviewedInstallationWorker.serve_retirement",
        retirement,
    )

    class FakeServer:
        def open(self) -> None:
            observed.append("opened")

        def serve_forever(self, stopped: object) -> None:
            observed.append(stopped)
            assert retirement_started.wait(2)
            assert inventory_started.wait(2)

    terminated = Event()
    managed = SimpleNamespace(
        start=lambda: observed.append("managed started"),
        join=lambda timeout=None: terminated.wait(timeout),
        terminate=terminated.set,
        is_alive=lambda: not terminated.is_set(),
        exitcode=None,
    )
    monkeypatch.setattr(run, "_managed_process", lambda: managed)

    monkeypatch.setattr(
        "kodi_lab.broker.run.BrokerServer",
        SimpleNamespace(production=lambda **_kwargs: FakeServer()),
    )
    assert run.main() == 0
    assert observed[2:4] == ["opened", "managed started"]
    assert terminated.is_set()
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
