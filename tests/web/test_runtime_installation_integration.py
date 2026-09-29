"""HTTP to Unix broker to controlled worker to durable status boundary."""

from __future__ import annotations

import base64
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.broker.authorization import ExactUidAuthorizationPolicy
from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.operations import build_foundation_registry
from kodi_lab.broker.reviewed_installations import DockerReviewedInstallationWorker
from kodi_lab.broker.server import BrokerServer
from kodi_lab.broker.socket_path import SocketPathConfig
from kodi_lab.config import Settings
from kodi_lab.domain import RuntimeRecipeInstallationState
from kodi_lab.runtime.recipes import load_reviewed_recipe
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository
from kodi_lab.web.app import create_app
from tests.broker.test_reviewed_runtime_installation_operation import (
    IMAGE,
    FakeContainer,
    FakeContainers,
)

from .conftest import ORIGIN, PASSWORD, login


def test_web_broker_controlled_worker_durable_status_and_reconciliation(
    tmp_path: Path,
    database: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "broker-socket"
    runtime_root.mkdir(mode=0o750)
    socket_path = runtime_root / "broker.sock"
    containers = FakeContainers()
    own = {
        "Image": IMAGE,
        "Config": {"Entrypoint": ["kodi-lab-broker"]},
        "Mounts": [
            {
                "Destination": "/var/lib/kodi-lab",
                "Type": "volume",
                "Name": "state-volume",
            },
            {"Destination": "/run/kodi-lab", "Type": "volume", "Name": "socket-volume"},
            {
                "Destination": "/var/run/docker.sock",
                "Type": "bind",
                "Source": "/var/run/docker.sock",
            },
        ],
    }
    docker_client = SimpleNamespace(
        api=SimpleNamespace(
            inspect_container=lambda _name: own,
            inspect_image=lambda _name: {
                "Id": IMAGE,
                "Os": "linux",
                "Architecture": "amd64",
            },
        ),
        containers=containers,
        close=lambda: None,
    )
    monkeypatch.setattr(
        "kodi_lab.broker.reviewed_installations.socket.gethostname",
        lambda: "broker-id",
    )
    original_start = FakeContainer.start

    def start_and_record(container: FakeContainer) -> None:
        original_start(container)
        config_attrs = cast(dict[str, Any], container.attrs["Config"])
        labels = cast(dict[str, str], config_attrs["Labels"])
        generation = labels["io.kodi-lab.installation-generation"]
        operation_id = UUID(labels["io.kodi-lab.installation-operation-id"])
        reviewed = load_reviewed_recipe(generation)
        with database.begin() as session:
            repo = RuntimeRecipeInstallationRepository(session)
            repo.create(
                operation_id=operation_id,
                recipe_id=reviewed.recipe.recipe_id,
                recipe_revision=reviewed.recipe.recipe_revision,
                recipe_digest=reviewed.digest,
                generation=generation,
                now=datetime.now(UTC),
            )
            repo.advance(
                operation_id,
                expected=RuntimeRecipeInstallationState.PENDING,
                now=datetime.now(UTC),
            )

    monkeypatch.setattr(FakeContainer, "start", start_and_record)
    worker = DockerReviewedInstallationWorker(
        client_factory=lambda **_kwargs: docker_client,
        runtime_root=runtime_root,
    )
    registry = build_foundation_registry(reviewed_installation_worker=worker)
    config = SocketPathConfig(
        runtime_directory=runtime_root,
        socket_path=socket_path,
        directory_owner_uid=os.getuid(),
        directory_group_gid=os.getgid(),
        socket_owner_uid=os.getuid(),
        socket_group_gid=os.getgid(),
    )
    server = BrokerServer(
        socket_config=config,
        authorization=ExactUidAuthorizationPolicy(os.getuid()),
        registry=registry,
    )
    server.open()
    stopped = threading.Event()
    thread = threading.Thread(target=server.serve_forever, args=(stopped,), daemon=True)
    thread.start()
    data_root = tmp_path / "web-data"
    auth = WebAuthenticationService(database)
    auth.set_password(PASSWORD)
    app = create_app(
        settings=Settings(
            data_root=data_root,
            external_target_master_key=SecretStr(
                base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
            ),
        ),
        session_factory=database,
        broker=BrokerClient(socket_path),
        auth=auth,
    )
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            token = login(client)
            accepted = client.post(
                "/api/runtime-installations/omega",
                headers={**ORIGIN, "X-Kodi-Lab-CSRF": token},
            )
            assert accepted.status_code == 202
            operation_id = accepted.json()["operation_id"]
            assert containers.created is not None
            assert containers.created["command"] == [
                "install",
                "omega",
                "--operation-id",
                operation_id,
            ]
            status = client.get(f"/api/runtime-installations/{operation_id}")
            assert status.status_code == 200
            assert status.json()["status"] == "acquiring"
            assert "Acquiring runtime" in client.get("/runtimes").text
            containers.items[0].attrs["State"] = {"Running": False}
            interrupted = client.get(f"/api/runtime-installations/{operation_id}")
            assert interrupted.json()["status"] == "failed"
            with database() as session:
                row = RuntimeRecipeInstallationRepository(session).get(
                    UUID(operation_id)
                )
                assert row is not None
                assert row.failure_code == "interrupted"
    finally:
        stopped.set()
        thread.join(timeout=2)
        server.close()
        assert not thread.is_alive()
