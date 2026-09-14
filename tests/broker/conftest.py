"""Temporary-only broker fixtures."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from kodi_lab.broker.authorization import (
    ExactUidAuthorizationPolicy,
    PeerCredentialExtractor,
    extract_peer_credentials,
)
from kodi_lab.broker.operations import OperationRegistry
from kodi_lab.broker.server import BrokerServer
from kodi_lab.broker.socket_path import SocketPathConfig


@pytest.fixture
def socket_config(tmp_path: Path) -> SocketPathConfig:
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir(mode=0o750)
    runtime_directory.chmod(0o750)
    return SocketPathConfig(
        runtime_directory=runtime_directory,
        socket_path=runtime_directory / "broker.sock",
        directory_owner_uid=os.getuid(),
        directory_group_gid=os.getgid(),
        socket_owner_uid=os.getuid(),
        socket_group_gid=os.getgid(),
    )


@pytest.fixture
def run_broker() -> Any:
    @contextmanager
    def run(
        socket_config: SocketPathConfig,
        *,
        expected_uid: int | None = None,
        registry: OperationRegistry | None = None,
        credential_extractor: PeerCredentialExtractor = extract_peer_credentials,
    ) -> Iterator[BrokerServer]:
        server = BrokerServer(
            socket_config=socket_config,
            authorization=ExactUidAuthorizationPolicy(
                os.getuid() if expected_uid is None else expected_uid
            ),
            registry=registry,
            credential_extractor=credential_extractor,
        )
        server.open()
        stop_event = threading.Event()
        failures: list[BaseException] = []

        def serve() -> None:
            try:
                server.serve_forever(stop_event)
            except BaseException as error:
                failures.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            stop_event.set()
            thread.join(timeout=2)
            server.close()
            assert not thread.is_alive()
            assert not failures

    return run
