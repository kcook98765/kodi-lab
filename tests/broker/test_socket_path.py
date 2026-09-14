"""Unix socket creation, metadata, and unsafe-path refusal tests."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

import pytest

from kodi_lab.broker.authorization import ExactUidAuthorizationPolicy
from kodi_lab.broker.errors import BrokerStartupError
from kodi_lab.broker.server import BrokerServer
from kodi_lab.broker.socket_path import SocketPathConfig, prepare_unix_listener


def test_listener_uses_unix_socket_and_expected_mode(
    socket_config: SocketPathConfig,
) -> None:
    prepared = prepare_unix_listener(socket_config)
    try:
        metadata = os.lstat(socket_config.socket_path)
        assert prepared.socket.family == socket.AF_UNIX
        assert stat.S_ISSOCK(metadata.st_mode)
        assert stat.S_IMODE(metadata.st_mode) == 0o660
        assert metadata.st_uid == os.getuid()
        assert metadata.st_gid == os.getgid()
    finally:
        prepared.close()
    assert not socket_config.socket_path.exists()


def test_cleanup_refuses_socket_whose_metadata_changed(
    socket_config: SocketPathConfig,
) -> None:
    prepared = prepare_unix_listener(socket_config)
    os.chmod(socket_config.socket_path, 0o600)
    prepared.close()
    assert socket_config.socket_path.exists()
    socket_config.socket_path.unlink()


def test_server_creates_no_tcp_listener(socket_config: SocketPathConfig) -> None:
    server = BrokerServer(
        socket_config=socket_config,
        authorization=ExactUidAuthorizationPolicy(os.getuid()),
    )
    server.open()
    try:
        assert server.listener_family == socket.AF_UNIX
    finally:
        server.close()


def test_existing_symlink_is_refused(
    socket_config: SocketPathConfig, tmp_path: Path
) -> None:
    target = tmp_path / "target"
    target.write_text("untouched")
    socket_config.socket_path.symlink_to(target)
    with pytest.raises(BrokerStartupError, match="symbolic link"):
        prepare_unix_listener(socket_config)
    assert target.read_text() == "untouched"
    assert socket_config.socket_path.is_symlink()


def test_existing_regular_file_is_refused(socket_config: SocketPathConfig) -> None:
    socket_config.socket_path.write_text("untouched")
    with pytest.raises(BrokerStartupError, match="non-socket"):
        prepare_unix_listener(socket_config)
    assert socket_config.socket_path.read_text() == "untouched"


def test_active_socket_is_refused(socket_config: SocketPathConfig) -> None:
    active = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    active.bind(str(socket_config.socket_path))
    os.chmod(socket_config.socket_path, 0o660)
    active.listen(1)
    try:
        with pytest.raises(BrokerStartupError, match="already listening"):
            prepare_unix_listener(socket_config)
    finally:
        active.close()
        socket_config.socket_path.unlink()


def test_safe_stale_socket_is_replaced_and_cleaned(
    socket_config: SocketPathConfig,
) -> None:
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_config.socket_path))
    stale.close()
    os.chmod(socket_config.socket_path, 0o660)
    prepared = prepare_unix_listener(socket_config)
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(socket_config.socket_path))
        finally:
            probe.close()
    finally:
        prepared.close()
    assert not socket_config.socket_path.exists()


def test_stale_socket_with_unsafe_mode_is_refused(
    socket_config: SocketPathConfig,
) -> None:
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_config.socket_path))
    stale.close()
    os.chmod(socket_config.socket_path, 0o666)
    with pytest.raises(BrokerStartupError, match="unsafe metadata"):
        prepare_unix_listener(socket_config)
    socket_config.socket_path.unlink()


def test_runtime_directory_mode_must_be_exact(socket_config: SocketPathConfig) -> None:
    socket_config.runtime_directory.chmod(0o770)
    with pytest.raises(BrokerStartupError, match="unexpected mode"):
        prepare_unix_listener(socket_config)


def test_socket_must_be_direct_child_of_runtime_directory(
    socket_config: SocketPathConfig,
) -> None:
    escaped = SocketPathConfig(
        runtime_directory=socket_config.runtime_directory,
        socket_path=socket_config.runtime_directory / "nested" / "broker.sock",
        directory_owner_uid=os.getuid(),
        directory_group_gid=os.getgid(),
        socket_owner_uid=os.getuid(),
        socket_group_gid=os.getgid(),
    )
    with pytest.raises(BrokerStartupError, match="directly inside"):
        prepare_unix_listener(escaped)
