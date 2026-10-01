"""Closed worker launch and observation never accept Docker inputs from Web."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from kodi_lab.broker.errors import RequestError
from kodi_lab.broker.operations import build_foundation_registry
from kodi_lab.broker.protocol import Request
from kodi_lab.broker.reviewed_installations import (
    DockerReviewedInstallationWorker,
    ReviewedWorkerBusy,
    ReviewedWorkerError,
    worker_name,
)
from kodi_lab.domain import RuntimeRecipeInstallationState
from kodi_lab.runtime.recipes import load_reviewed_recipe
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository

IMAGE = "sha256:" + "a" * 64


class FakeContainer:
    def __init__(self, attributes: dict[str, object]) -> None:
        self.attrs = attributes

    def reload(self) -> None:
        pass

    def start(self) -> None:
        self.attrs["State"] = {"Running": True}


class FakeContainers:
    def __init__(self) -> None:
        self.items: list[FakeContainer] = []
        self.created: dict[str, object] | None = None

    def list(self, **_kwargs: object) -> list[FakeContainer]:
        return self.items

    def create(self, **kwargs: object) -> FakeContainer:
        self.created = kwargs
        labels = kwargs["labels"]
        assert isinstance(labels, dict)
        volumes = kwargs["volumes"]
        assert isinstance(volumes, dict)
        mounts = [
            {
                "Type": "bind"
                if source.startswith(("/var/run/", "/proc/"))
                else "volume",
                "Name": None if source.startswith(("/var/run/", "/proc/")) else source,
                "Source": source,
                "Destination": spec["bind"],
                "RW": spec["mode"] == "rw",
            }
            for source, spec in volumes.items()
        ]
        item = FakeContainer(
            {
                "Name": "/" + str(kwargs["name"]),
                "Image": IMAGE,
                "Config": {
                    "Labels": labels,
                    "Entrypoint": kwargs["entrypoint"],
                    "Cmd": kwargs["command"],
                    "User": kwargs["user"],
                },
                "HostConfig": {
                    "NetworkMode": kwargs["network_mode"],
                    "CapAdd": kwargs["cap_add"],
                    "CapDrop": kwargs["cap_drop"],
                    "ReadonlyRootfs": kwargs["read_only"],
                    "Privileged": False,
                    "AutoRemove": kwargs["auto_remove"],
                    "SecurityOpt": kwargs["security_opt"],
                },
                "Mounts": mounts,
                "State": {"Running": False},
            }
        )
        self.items.append(item)
        return item

    def get(self, name: str) -> FakeContainer:
        return next(item for item in self.items if item.attrs["Name"] == "/" + name)


@pytest.fixture
def backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DockerReviewedInstallationWorker, FakeContainers]:
    monkeypatch.setattr(
        "kodi_lab.broker.reviewed_installations.socket.gethostname", lambda: "broker-id"
    )
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
    api = SimpleNamespace(
        inspect_container=lambda name: own,
        inspect_image=lambda name: {
            "Id": IMAGE,
            "Os": "linux",
            "Architecture": "amd64",
        },
    )
    client = SimpleNamespace(api=api, containers=containers, close=lambda: None)
    return (
        DockerReviewedInstallationWorker(
            client_factory=lambda **_kwargs: client,
            runtime_root=tmp_path,
        ),
        containers,
    )


def test_exact_worker_launch_contract_and_liveness(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend
    operation_id = uuid4()
    assert worker.launch("matrix", operation_id)
    created = containers.created
    assert created is not None
    assert created["image"] == IMAGE
    assert created["name"] == worker_name(operation_id)
    assert created["entrypoint"] == ["kodi-lab-runtime"]
    assert created["command"] == [
        "install",
        "matrix",
        "--operation-id",
        str(operation_id),
    ]
    assert created["volumes"] == {
        "state-volume": {"bind": "/var/lib/kodi-lab", "mode": "rw"},
        "socket-volume": {"bind": "/run/kodi-lab", "mode": "rw"},
        "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
        "/proc/1/net/route": {
            "bind": "/tmp/kodi-lab-host-ipv4-route",
            "mode": "ro",
        },
    }
    assert created["environment"] == {"KODI_LAB_TEMPORARY_NETWORK_CHILD_PREFIX": "24"}
    added = created["cap_add"]
    assert isinstance(added, list)
    assert set(added) == {"SETUID", "SETGID"}
    assert created["cap_drop"] == ["ALL"]
    assert created["network_mode"] == "none"
    assert created["read_only"] is True
    assert created["user"] == "0:0"
    assert created["security_opt"] == ["no-new-privileges:true"]
    assert created["tmpfs"] == {"/tmp": "rw,noexec,nosuid,size=64m"}
    assert created["labels"] == {
        "io.kodi-lab.resource": "reviewed-runtime-installation",
        "io.kodi-lab.installation-operation-id": str(operation_id),
        "io.kodi-lab.installation-generation": "matrix",
        "io.kodi-lab.installation-worker-image-id": IMAGE,
    }
    assert worker.observe(operation_id).state == "running"
    assert worker.active() == (operation_id, "matrix")
    assert not worker.launch("matrix", operation_id)
    with pytest.raises(ReviewedWorkerBusy):
        worker.launch("nexus", uuid4())
    containers.items[0].attrs["State"] = {"Running": False}
    assert worker.observe(operation_id).state == "exited"


def test_mismatched_worker_never_counts_as_owned(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend
    operation_id = uuid4()
    worker.launch("omega", operation_id)
    config = containers.items[0].attrs["Config"]
    assert isinstance(config, dict)
    labels = config["Labels"]
    assert isinstance(labels, dict)
    labels["io.kodi-lab.installation-operation-id"] = str(uuid4())
    assert worker.observe(operation_id).state == "mismatch"
    with pytest.raises(ReviewedWorkerError):
        worker.active()


def test_worker_using_other_image_or_read_only_state_mount_is_unrelated(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend
    operation_id = uuid4()
    worker.launch("omega", operation_id)
    item = containers.items[0]
    other_image = "sha256:" + "b" * 64
    item.attrs["Image"] = other_image
    config = item.attrs["Config"]
    assert isinstance(config, dict)
    labels = config["Labels"]
    assert isinstance(labels, dict)
    labels["io.kodi-lab.installation-worker-image-id"] = other_image
    assert worker.observe(operation_id).state == "mismatch"
    with pytest.raises(ReviewedWorkerError):
        worker.active()
    item.attrs["Image"] = IMAGE
    labels["io.kodi-lab.installation-worker-image-id"] = IMAGE
    mounts = item.attrs["Mounts"]
    assert isinstance(mounts, list)
    mounts[0]["RW"] = False
    assert worker.observe(operation_id).state == "mismatch"


def test_protocol_is_closed_to_three_generations_and_semantic_fields(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, _ = backend
    registry = build_foundation_registry(reviewed_installation_worker=worker)
    operation_id = str(uuid4())
    for params in (
        {"generation": "piers", "operation_id": operation_id},
        {"generation": "matrix", "operation_id": operation_id, "image": IMAGE},
        {
            "generation": "matrix",
            "operation_id": operation_id,
            "cap_add": ["SYS_ADMIN"],
        },
        {"generation": "matrix", "operation_id": operation_id, "command": "sh"},
        {"generation": "matrix", "operation_id": operation_id, "mounts": ["/host"]},
        {"generation": "matrix", "operation_id": operation_id, "network_mode": "host"},
        {"generation": "matrix", "operation_id": operation_id, "dockerfile": "FROM x"},
        {
            "generation": "matrix",
            "operation_id": operation_id,
            "package_url": "https://x",
        },
    ):
        with pytest.raises(RequestError) as raised:
            registry.dispatch(
                Request(
                    version=1,
                    request_id=str(uuid4()),
                    operation="launch_reviewed_runtime_installation",
                    params=params,
                )
            )
        assert raised.value.code == "invalid_params"
    with pytest.raises(RequestError) as raised:
        registry.dispatch(
            Request(
                version=1,
                request_id=str(uuid4()),
                operation="reviewed_runtime_image_present",
                params={"operation_id": operation_id, "image_id": IMAGE},
            )
        )
    assert raised.value.code == "invalid_params"


def test_image_presence_uses_only_ready_operation_and_exact_recipe_identity(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, _ = backend
    operation_id = uuid4()
    digest = load_reviewed_recipe("omega").digest
    row = SimpleNamespace(
        state=RuntimeRecipeInstallationState.READY,
        image_id=IMAGE,
        generation="omega",
        recipe_id=load_reviewed_recipe("omega").recipe.recipe_id,
        recipe_revision=load_reviewed_recipe("omega").recipe.recipe_revision,
        recipe_digest=digest,
    )
    monkeypatch.setattr(
        RuntimeRecipeInstallationRepository,
        "get",
        lambda _self, _operation_id: row,
    )
    worker._session_factory = cast(Any, lambda: nullcontext(None))
    client = worker._client()
    client.api.inspect_image = lambda _image_id: {
        "Id": IMAGE,
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {"Labels": {"io.kodi-lab.recipe-digest": digest}},
    }
    assert worker.image_present(operation_id)
    row.recipe_digest = "sha256:" + "b" * 64
    assert not worker.image_present(operation_id)
    row.recipe_digest = digest
    row.state = RuntimeRecipeInstallationState.FAILED
    with pytest.raises(Exception, match="image is unavailable"):
        worker.image_present(operation_id)
