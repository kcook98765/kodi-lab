"""Installation-time production identity contract tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kodi_lab.broker.client import (
    READ_TIMEOUT_SECONDS as CLIENT_READ_TIMEOUT,
)
from kodi_lab.broker.client import (
    WRITE_TIMEOUT_SECONDS as CLIENT_WRITE_TIMEOUT,
)
from kodi_lab.broker.errors import BrokerStartupError
from kodi_lab.broker.server import (
    READ_TIMEOUT_SECONDS as SERVER_READ_TIMEOUT,
)
from kodi_lab.broker.server import (
    WRITE_TIMEOUT_SECONDS as SERVER_WRITE_TIMEOUT,
)
from kodi_lab.broker.server import (
    ProductionBrokerConfiguration,
)
from kodi_lab.broker.socket_path import (
    PRODUCTION_RUNTIME_DIRECTORY,
    PRODUCTION_SOCKET_PATH,
)


def test_production_configuration_requires_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kodi_lab.broker.server.os.geteuid", lambda: 1000)
    with pytest.raises(BrokerStartupError, match="must run as root"):
        ProductionBrokerConfiguration.resolve()


def test_protocol_read_and_write_timeouts_are_five_seconds() -> None:
    assert SERVER_READ_TIMEOUT == 5.0
    assert SERVER_WRITE_TIMEOUT == 5.0
    assert CLIENT_READ_TIMEOUT == 5.0
    assert CLIENT_WRITE_TIMEOUT == 5.0


def test_production_configuration_resolves_exact_user_and_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kodi_lab.broker.server.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "kodi_lab.broker.server.pwd.getpwnam",
        lambda name: SimpleNamespace(pw_uid=1234, pw_gid=5678, pw_name=name),
    )
    monkeypatch.setattr(
        "kodi_lab.broker.server.grp.getgrnam",
        lambda name: SimpleNamespace(gr_gid=5678, gr_name=name),
    )

    configuration = ProductionBrokerConfiguration.resolve()

    assert configuration.authorization.expected_uid == 1234
    assert configuration.socket.runtime_directory == PRODUCTION_RUNTIME_DIRECTORY
    assert configuration.socket.socket_path == PRODUCTION_SOCKET_PATH
    assert configuration.socket.directory_owner_uid == 0
    assert configuration.socket.directory_group_gid == 5678
    assert configuration.socket.directory_mode == 0o750
    assert configuration.socket.socket_owner_uid == 0
    assert configuration.socket.socket_group_gid == 5678
    assert configuration.socket.socket_mode == 0o660


def test_production_configuration_refuses_missing_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kodi_lab.broker.server.os.geteuid", lambda: 0)

    def missing(_name: str) -> None:
        raise KeyError

    monkeypatch.setattr("kodi_lab.broker.server.pwd.getpwnam", missing)
    with pytest.raises(BrokerStartupError, match="could not be resolved"):
        ProductionBrokerConfiguration.resolve()


def test_production_user_must_have_dedicated_primary_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kodi_lab.broker.server.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "kodi_lab.broker.server.pwd.getpwnam",
        lambda _name: SimpleNamespace(pw_uid=1234, pw_gid=1111),
    )
    monkeypatch.setattr(
        "kodi_lab.broker.server.grp.getgrnam",
        lambda _name: SimpleNamespace(gr_gid=2222),
    )
    with pytest.raises(BrokerStartupError, match="dedicated primary group"):
        ProductionBrokerConfiguration.resolve()
