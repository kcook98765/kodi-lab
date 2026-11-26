"""Closed worker launch and observation never accept Docker inputs from Web."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from docker.errors import (  # type: ignore[import-untyped]
    APIError,
    DockerException,
    NotFound,
)

from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.broker.errors import RequestError
from kodi_lab.broker.operations import (
    OperationRegistry,
    register_reviewed_runtime_operations,
)
from kodi_lab.broker.protocol import Request
from kodi_lab.broker.reviewed_installations import (
    LAUNCH_LABEL,
    DockerReviewedInstallationWorker,
    ReviewedWorkerBusy,
    ReviewedWorkerError,
    worker_name,
)
from kodi_lab.broker.worker_receipts import RECEIPT_LIMIT
from kodi_lab.domain import RuntimeRecipeInstallationState
from kodi_lab.managed import reviewed_image_file
from kodi_lab.runtime.install import _installation_lock, _maintenance_gate
from kodi_lab.runtime.recipes import (
    load_inactive_ostree_recipe,
    load_installation_recipe,
)
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import RuntimeRecipeInstallationRepository

IMAGE = "sha256:" + "a" * 64
ENGINE = "fixture-engine"


class FakeContainer:
    def __init__(self, attributes: dict[str, Any], owner: FakeContainers) -> None:
        self.attrs = attributes
        self.owner = owner
        self.id = attributes.get("Id", uuid4().hex + uuid4().hex)
        self.starts = 0
        self.log_tail = b"worker diagnostics\n"
        self.before_remove: Callable[[], None] | None = None

    def reload(self) -> None:
        pass

    def start(self) -> None:
        self.starts += 1
        self.attrs["State"].update(Running=True, Status="running")

    def exit(self, code: int = 0, *, oom: bool = False) -> None:
        self.attrs["State"].update(
            Running=False,
            Status="exited",
            ExitCode=code,
            OOMKilled=oom,
            StartedAt="2026-11-25T12:00:00Z",
            FinishedAt="2026-11-25T12:01:00Z",
        )

    def logs(self, **_kwargs: object) -> bytes:
        return self.log_tail

    def remove(self, *, force: bool, v: bool) -> None:
        assert force is False and v is False
        if self.before_remove is not None:
            self.before_remove()
        self.owner.items.remove(self)
        self.owner.removed.append(self.id)


class FakeContainers:
    def __init__(self) -> None:
        self.items: list[FakeContainer] = []
        self.removed: list[str] = []
        self.rows: dict[UUID, Any] = {}
        self.created: dict[str, object] | None = None
        self.on_create: Callable[[dict[str, Any]], None] | None = None

    def list(
        self, *, filters: dict[str, Any], limit: int, **_kwargs: object
    ) -> list[FakeContainer]:
        key, value = filters["label"].split("=")
        return [
            item
            for item in self.items
            if item.attrs["Config"]["Labels"].get(key) == value
            and item.attrs["State"].get(
                "Status", "running" if item.attrs["State"]["Running"] else "exited"
            )
            in filters["status"]
        ][:limit]

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
                "Mode": spec["mode"],
            }
            for source, spec in volumes.items()
        ]
        item = FakeContainer(
            {
                "Id": uuid4().hex + uuid4().hex,
                "Name": "/" + str(kwargs["name"]),
                "Image": IMAGE,
                "Config": {
                    "Labels": labels,
                    "Entrypoint": kwargs["entrypoint"],
                    "Cmd": kwargs["command"],
                    "User": kwargs["user"],
                },
                "HostConfig": {
                    "Binds": [
                        f"{source}:{spec['bind']}:{spec['mode']}"
                        for source, spec in volumes.items()
                    ],
                    "NetworkMode": kwargs["network_mode"],
                    "CapAdd": kwargs["cap_add"],
                    "CapDrop": kwargs["cap_drop"],
                    "ReadonlyRootfs": kwargs["read_only"],
                    "Privileged": False,
                    "AutoRemove": kwargs["auto_remove"],
                    "SecurityOpt": kwargs["security_opt"],
                    "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                },
                "Mounts": mounts,
                "State": {
                    "Status": "created",
                    "Running": False,
                    "Paused": False,
                    "Restarting": False,
                    "Dead": False,
                    "ExitCode": 0,
                    "OOMKilled": False,
                    "StartedAt": "",
                    "FinishedAt": "",
                },
                "NetworkSettings": {"Networks": {"none": {}}},
            },
            self,
        )
        self.items.append(item)
        if self.on_create is not None:
            self.on_create(item.attrs)
        return item

    def get(self, name: str) -> FakeContainer:
        for item in self.items:
            if item.attrs.get("Name") == "/" + name or item.id == name:
                return item
        raise NotFound("no exact fixture container")

    def terminal(
        self, operation: UUID, *, ready: bool = True, generation: str = "omega"
    ) -> None:
        self.rows[operation] = SimpleNamespace(
            id=str(operation),
            generation=generation,
            recipe_digest=load_installation_recipe(generation).digest,
            state=RuntimeRecipeInstallationState.READY
            if ready
            else RuntimeRecipeInstallationState.FAILED,
            completed_at=datetime.now(UTC).isoformat(),
            image_id=IMAGE if ready else None,
            failure_code=None if ready else "installation_failed",
        )


def broker_attributes() -> dict[str, Any]:
    own: dict[str, Any] = {
        "Image": IMAGE,
        "Config": {"Entrypoint": ["kodi-lab-broker"], "Labels": {}},
        "Mounts": [
            {
                "Destination": "/var/lib/kodi-lab",
                "Type": "volume",
                "Name": "state-volume",
                "RW": True,
            },
            {
                "Destination": "/run/kodi-lab",
                "Type": "volume",
                "Name": "socket-volume",
                "RW": True,
            },
            {
                "Destination": "/var/run/docker.sock",
                "Type": "bind",
                "Source": "/var/run/docker.sock",
                "RW": True,
            },
            {
                "Destination": "/tmp/kodi-lab-host-ipv4-route",
                "Type": "bind",
                "Source": "/proc/1/net/route",
                "RW": False,
            },
        ],
    }

    for mount in own["Mounts"]:
        mount["Mode"] = "rw" if mount["RW"] else "ro"
    own["HostConfig"] = {
        "Binds": [
            f"{m.get('Name') if m['Type'] == 'volume' else m['Source']}"
            f":{m['Destination']}:{m['Mode']}"
            for m in own["Mounts"]
        ]
    }
    return own


@pytest.fixture
def backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DockerReviewedInstallationWorker, FakeContainers]:
    monkeypatch.setattr(
        "kodi_lab.broker.reviewed_installations.socket.gethostname", lambda: "broker-id"
    )
    containers = FakeContainers()
    own = broker_attributes()
    api = SimpleNamespace(
        inspect_container=lambda name: own,
        inspect_image=lambda name: {
            "Id": IMAGE,
            "Os": "linux",
            "Architecture": "amd64",
        },
    )
    client = SimpleNamespace(
        api=api,
        containers=containers,
        close=lambda: None,
        info=lambda: {
            "OperatingSystem": "Ubuntu 24.04",
            "OSType": "linux",
            "ID": ENGINE,
        },
    )
    worker = DockerReviewedInstallationWorker(
        client_factory=lambda **_kwargs: client,
        runtime_root=tmp_path,
    )
    monkeypatch.setattr(worker, "_recorded_operation", containers.rows.get)
    return worker, containers


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
    labels = created["labels"]
    assert isinstance(labels, dict)
    nonce = UUID(labels[LAUNCH_LABEL])
    assert labels == {
        "io.kodi-lab.resource": "reviewed-runtime-installation",
        "io.kodi-lab.installation-operation-id": str(operation_id),
        "io.kodi-lab.installation-generation": "matrix",
        "io.kodi-lab.installation-worker-image-id": IMAGE,
        LAUNCH_LABEL: str(nonce),
    }
    assert worker.observe(operation_id).state == "running"
    assert worker.active() == (operation_id, "matrix")
    assert not worker.launch("matrix", operation_id)
    with pytest.raises(ReviewedWorkerBusy):
        worker.launch("nexus", uuid4())
    containers.items[0].exit()
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
    registry = OperationRegistry()
    register_reviewed_runtime_operations(registry, worker)
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


@pytest.mark.parametrize("generation", ["matrix", "nexus", "omega", "piers"])
def test_image_presence_uses_only_ready_operation_and_exact_recipe_identity(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    monkeypatch: pytest.MonkeyPatch,
    generation: str,
) -> None:
    worker, _ = backend
    operation_id = uuid4()
    reviewed = load_installation_recipe(generation)
    digest = reviewed.digest
    image_digest = (
        load_inactive_ostree_recipe(reviewed.recipe.kodi_version).digest
        if generation == "piers"
        else digest
    )
    row = SimpleNamespace(
        state=RuntimeRecipeInstallationState.READY,
        image_id=IMAGE,
        generation=generation,
        recipe_id=reviewed.recipe.recipe_id,
        recipe_revision=reviewed.recipe.recipe_revision,
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
        "Config": {"Labels": {"io.kodi-lab.recipe-digest": image_digest}},
    }
    assert not worker.image_present(operation_id)
    pin = worker._runtime_root / reviewed_image_file(generation)
    pin.write_text("sha256:" + "b" * 64)
    assert not worker.image_present(operation_id)
    pin.write_text(IMAGE)
    assert worker.image_present(operation_id)
    row.recipe_digest = "sha256:" + "b" * 64
    assert not worker.image_present(operation_id)
    row.recipe_digest = digest
    row.state = RuntimeRecipeInstallationState.FAILED
    with pytest.raises(Exception, match="image is unavailable"):
        worker.image_present(operation_id)


DISTRO = "Ubuntu-24.04"
DESKTOP_ROOT = "/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/" + DISTRO
DESKTOP_SOCKET = DESKTOP_ROOT + "/docker.sock"
DESKTOP_ROUTE = (
    DESKTOP_ROOT + "/6d2a43bd27d2d28602da880e9832c42fb02f7f5e404136f74c7b34b9c3da7b4f"
)
SOCKET_DESTINATION = "/var/run/docker.sock"
ROUTE_DESTINATION = "/tmp/kodi-lab-host-ipv4-route"


def desktop_backend(worker: DockerReviewedInstallationWorker) -> Any:
    client = worker._client()
    own = client.api.inspect_container("broker-id")
    own["Config"]["Labels"]["desktop.docker.io/wsl-distro"] = DISTRO
    client.info = lambda: {
        "OperatingSystem": "Docker Desktop (containerized)",
        "OSType": "linux",
        "ID": ENGINE,
    }
    return client


def translated(
    attrs: dict[str, Any],
    mount_socket: bool = True,
    mount_route: bool = True,
    bind_socket: bool = True,
    bind_route: bool = True,
) -> None:
    for mount in attrs["Mounts"]:
        if mount["Destination"] == SOCKET_DESTINATION and mount_socket:
            mount["Source"] = DESKTOP_SOCKET
        elif mount["Destination"] == ROUTE_DESTINATION and mount_route:
            mount["Source"] = DESKTOP_ROUTE
    binds = []
    for bind in attrs["HostConfig"]["Binds"]:
        source, destination, mode = bind.split(":")
        if destination == SOCKET_DESTINATION and bind_socket:
            source = DESKTOP_SOCKET
        elif destination == ROUTE_DESTINATION and bind_route:
            source = DESKTOP_ROUTE
        binds.append(f"{source}:{destination}:{mode}")
    attrs["HostConfig"]["Binds"] = binds


def rejected_everywhere(
    worker: DockerReviewedInstallationWorker,
    containers: FakeContainers,
    operation_id: UUID,
) -> None:
    with pytest.raises(ReviewedWorkerError, match="created worker identity"):
        worker.launch("omega", operation_id)
    assert len(containers.items) == 1
    item = containers.items[0]
    assert item.starts == 0
    assert worker.observe(operation_id).state == "mismatch"
    item.attrs["State"] = {"Running": True}
    with pytest.raises(ReviewedWorkerError, match="ownership is ambiguous"):
        worker.active()
    assert item.starts == 0


@pytest.mark.parametrize(
    "engine_os", ["Docker Desktop", "Docker Desktop (containerized)"]
)
@pytest.mark.parametrize(
    ("mount_socket", "mount_route", "bind_socket", "bind_route"),
    [
        (False, False, False, False),
        (True, True, True, True),
        (True, False, True, False),
        (False, True, False, True),
        (True, True, False, False),
        (False, False, True, True),
        (True, False, False, True),
    ],
)
def test_desktop_aliases_and_mixed_descriptors_keep_exact_worker_lifecycle(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    engine_os: str,
    mount_socket: bool,
    mount_route: bool,
    bind_socket: bool,
    bind_route: bool,
) -> None:
    worker, containers = backend
    client = desktop_backend(worker)
    client.info = lambda: {
        "OperatingSystem": engine_os,
        "OSType": "linux",
        "ID": ENGINE,
    }
    containers.on_create = lambda attrs: translated(
        attrs, mount_socket, mount_route, bind_socket, bind_route
    )
    operation = uuid4()
    assert worker.launch("omega", operation)
    assert containers.items[0].starts == 1
    assert worker.observe(operation).state == "running"
    assert worker.active() == (operation, "omega")
    assert not worker.launch("omega", operation)
    assert len(containers.items) == 1 and containers.items[0].starts == 1
    containers.items[0].exit()
    assert worker.observe(operation).state == "exited"


def test_linux_literal_contract_does_not_require_desktop_observation(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend
    operation = uuid4()
    assert worker.launch("omega", operation)
    client = worker._client()
    client.info = lambda: {"ID": ENGINE}
    assert worker.observe(operation).state == "running"
    assert worker.active() == (operation, "omega")
    client.info = lambda: {"ID": "another-engine"}
    assert worker.observe(operation).state == "mismatch"
    with pytest.raises(ReviewedWorkerError, match="ownership is ambiguous"):
        worker.active()
    assert containers.items[0].starts == 1


@pytest.mark.parametrize(
    "context",
    [
        "linux",
        "windows-engine",
        "missing-distro",
        "worker-label-only",
        "other-distro",
        "empty-distro",
        "traversal-distro",
        "encoded-distro",
        "long-distro",
        "info-error",
        "malformed-info",
        "malformed-os",
        "missing-route",
        "wrong-broker-route",
        "writable-broker-route",
        "readonly-broker-state",
        "duplicate-broker-route",
    ],
)
def test_desktop_aliases_need_verified_engine_and_broker_context(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers], context: str
) -> None:
    worker, containers = backend
    client = desktop_backend(worker)
    own = client.api.inspect_container("broker-id")
    labels = own["Config"]["Labels"]
    if context == "linux":
        client.info = lambda: {
            "OperatingSystem": "Ubuntu 24.04",
            "OSType": "linux",
            "ID": ENGINE,
        }
    elif context == "windows-engine":
        client.info = lambda: {
            "OperatingSystem": "Docker Desktop",
            "OSType": "windows",
            "ID": ENGINE,
        }
    elif context in {"missing-distro", "worker-label-only"}:
        labels.clear()
    elif context == "other-distro":
        labels["desktop.docker.io/wsl-distro"] = "OtherDistro"
    elif context == "empty-distro":
        labels["desktop.docker.io/wsl-distro"] = ""
    elif context == "traversal-distro":
        labels["desktop.docker.io/wsl-distro"] = "../Ubuntu-24.04"
    elif context == "encoded-distro":
        labels["desktop.docker.io/wsl-distro"] = "%55buntu-24.04"
    elif context == "long-distro":
        labels["desktop.docker.io/wsl-distro"] = "a" * 65
    elif context == "info-error":

        def unavailable() -> None:
            raise DockerException("unavailable")

        client.info = unavailable
    elif context == "malformed-info":
        client.info = lambda: None
    elif context == "malformed-os":
        client.info = lambda: {"OperatingSystem": [], "OSType": "linux", "ID": ENGINE}
    elif context == "missing-route":
        own["Mounts"].pop()
    elif context == "wrong-broker-route":
        own["Mounts"][-1]["Source"] = "/tmp/unrelated"
    elif context == "writable-broker-route":
        own["Mounts"][-1]["RW"] = True
    elif context == "readonly-broker-state":
        own["Mounts"][0]["RW"] = False
    elif context == "duplicate-broker-route":
        own["Mounts"].append(dict(own["Mounts"][-1]))

    def transform(attrs: dict[str, Any]) -> None:
        translated(attrs)
        if context == "worker-label-only":
            attrs["Config"]["Labels"]["desktop.docker.io/wsl-distro"] = DISTRO

    containers.on_create = transform
    if context in {
        "missing-route",
        "wrong-broker-route",
        "writable-broker-route",
        "readonly-broker-state",
        "duplicate-broker-route",
        "info-error",
        "malformed-info",
    }:
        with pytest.raises(ReviewedWorkerError):
            worker.launch("omega", uuid4())
        assert containers.created is None and containers.items == []
    else:
        rejected_everywhere(worker, containers, uuid4())


@pytest.mark.parametrize(
    ("destination", "bad_source"),
    [
        (SOCKET_DESTINATION, DESKTOP_ROOT + "/other.sock"),
        (SOCKET_DESTINATION, DESKTOP_ROOT + "/" + "a" * 64),
        (SOCKET_DESTINATION, DESKTOP_ROUTE),
        (ROUTE_DESTINATION, DESKTOP_SOCKET),
        (ROUTE_DESTINATION, DESKTOP_ROOT + "/" + "a" * 64),
        (ROUTE_DESTINATION, DESKTOP_ROUTE.replace(DISTRO, "OtherDistro")),
        (ROUTE_DESTINATION, DESKTOP_ROUTE + "/"),
        (ROUTE_DESTINATION, DESKTOP_ROUTE + "suffix"),
        (ROUTE_DESTINATION, DESKTOP_ROUTE.replace(DISTRO + "/", DISTRO + "//")),
        (ROUTE_DESTINATION, DESKTOP_ROUTE.replace(DISTRO + "/", DISTRO + "/./")),
        (
            ROUTE_DESTINATION,
            DESKTOP_ROUTE.replace(DISTRO + "/", DISTRO + "/../" + DISTRO + "/"),
        ),
        (ROUTE_DESTINATION, DESKTOP_ROUTE.replace(DISTRO, "%55buntu-24.04")),
        (ROUTE_DESTINATION, DESKTOP_ROUTE.replace("/run/desktop", "/tmp/desktop")),
        (ROUTE_DESTINATION, DESKTOP_ROUTE.replace("/", "\\")),
        (ROUTE_DESTINATION, DESKTOP_ROUTE + "\0"),
        (SOCKET_DESTINATION, "/tmp/attacker.sock"),
        (ROUTE_DESTINATION, "/tmp/attacker-route"),
    ],
)
def test_unrelated_or_ambiguous_desktop_sources_never_start_or_count_as_owned(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    destination: str,
    bad_source: str,
) -> None:
    worker, containers = backend
    desktop_backend(worker)

    def transform(attrs: dict[str, Any]) -> None:
        translated(attrs)
        for mount in attrs["Mounts"]:
            if mount["Destination"] == destination:
                mount["Source"] = bad_source
        attrs["HostConfig"]["Binds"] = [
            f"{bad_source}:{destination}:{bind.rsplit(':', 1)[1]}"
            if bind.split(":")[1] == destination
            else bind
            for bind in attrs["HostConfig"]["Binds"]
        ]

    containers.on_create = transform
    rejected_everywhere(worker, containers, uuid4())


@pytest.mark.parametrize(
    "defect",
    [
        "extra",
        "duplicate",
        "missing",
        "duplicate-replacing-role",
        "wrong-type",
        "wrong-destination",
        "route-rw",
        "socket-ro",
        "state-ro",
        "lock-volume",
        "state-volume",
        "rw-not-bool",
        "mount-mode",
        "bind-mode",
        "bind-source",
        "bind-volume",
        "bind-duplicate",
        "bind-missing",
        "bind-extra",
        "bind-option",
        "structured-mount",
        "volumes-from",
    ],
)
def test_mount_roles_modes_and_descriptors_remain_exact(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers], defect: str
) -> None:
    worker, containers = backend
    desktop_backend(worker)

    def transform(attrs: dict[str, Any]) -> None:
        translated(attrs)
        mounts = attrs["Mounts"]
        host = attrs["HostConfig"]
        binds = host["Binds"]
        route = next(m for m in mounts if m["Destination"] == ROUTE_DESTINATION)
        socket = next(m for m in mounts if m["Destination"] == SOCKET_DESTINATION)
        if defect == "extra":
            mounts.append(
                {
                    "Type": "bind",
                    "Source": "/",
                    "Destination": "/host",
                    "RW": True,
                    "Mode": "rw",
                }
            )
        elif defect == "duplicate":
            mounts.append(dict(route))
        elif defect == "missing":
            mounts.pop()
        elif defect == "duplicate-replacing-role":
            mounts[-1] = dict(mounts[0])
        elif defect == "wrong-type":
            route["Type"] = "volume"
        elif defect == "wrong-destination":
            route["Destination"] = "/host-route"
        elif defect == "route-rw":
            route["RW"] = True
        elif defect == "socket-ro":
            socket["RW"] = False
        elif defect == "state-ro":
            mounts[0]["RW"] = False
        elif defect == "lock-volume":
            mounts[1]["Name"] = "unrelated-lock-volume"
        elif defect == "state-volume":
            mounts[0]["Name"] = "unrelated-state-volume"
        elif defect == "rw-not-bool":
            mounts[0]["RW"] = 1
        elif defect == "mount-mode":
            route["Mode"] = "rw"
        elif defect == "bind-mode":
            binds[-1] = binds[-1].removesuffix(":ro") + ":rw"
        elif defect == "bind-source":
            binds[-1] = "/tmp/unrelated:" + ROUTE_DESTINATION + ":ro"
        elif defect == "bind-volume":
            binds[0] = "unrelated-state:/var/lib/kodi-lab:rw"
        elif defect == "bind-duplicate":
            binds[-1] = binds[0]
        elif defect == "bind-missing":
            binds.pop()
        elif defect == "bind-extra":
            binds.append("/:/host:rw")
        elif defect == "bind-option":
            binds[-1] += ",shared"
        elif defect == "structured-mount":
            host["Mounts"] = [{"Type": "bind", "Source": "/", "Target": "/host"}]
        elif defect == "volumes-from":
            host["VolumesFrom"] = ["unrelated-container"]

    containers.on_create = transform
    rejected_everywhere(worker, containers, uuid4())


@pytest.mark.parametrize(
    "defect",
    [
        "image",
        "name",
        "operation",
        "worker-label",
        "image-label",
        "generation",
        "entrypoint",
        "command",
        "uid",
        "caps",
        "cap-drop",
        "network",
        "writable-root",
        "privileged",
        "security",
        "auto-remove",
    ],
)
def test_desktop_compatibility_preserves_all_worker_identity_and_security_checks(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers], defect: str
) -> None:
    worker, containers = backend
    desktop_backend(worker)

    def transform(attrs: dict[str, Any]) -> None:
        translated(attrs)
        config = attrs["Config"]
        labels = config["Labels"]
        host = attrs["HostConfig"]
        if defect == "image":
            attrs["Image"] = "sha256:" + "b" * 64
        elif defect == "name":
            attrs["Name"] = "/unrelated-worker"
        elif defect == "operation":
            labels["io.kodi-lab.installation-operation-id"] = str(uuid4())
        elif defect == "worker-label":
            labels["io.kodi-lab.resource"] = "unrelated"
        elif defect == "image-label":
            labels["io.kodi-lab.installation-worker-image-id"] = "sha256:" + "b" * 64
        elif defect == "generation":
            labels["io.kodi-lab.installation-generation"] = "unknown"
        elif defect == "entrypoint":
            config["Entrypoint"] = ["sh"]
        elif defect == "command":
            config["Cmd"] = [
                "install",
                "matrix",
                "--operation-id",
                labels["io.kodi-lab.installation-operation-id"],
            ]
        elif defect == "uid":
            config["User"] = "1000:1000"
        elif defect == "caps":
            host["CapAdd"] = ["SETUID", "SETGID", "SYS_ADMIN"]
        elif defect == "cap-drop":
            host["CapDrop"] = []
        elif defect == "network":
            host["NetworkMode"] = "host"
        elif defect == "writable-root":
            host["ReadonlyRootfs"] = False
        elif defect == "privileged":
            host["Privileged"] = True
        elif defect == "security":
            host["SecurityOpt"] = []
        elif defect == "auto-remove":
            host["AutoRemove"] = True

    containers.on_create = transform
    operation = uuid4()
    with pytest.raises(ReviewedWorkerError, match="created worker identity"):
        worker.launch("omega", operation)
    assert containers.items[0].starts == 0
    # The inventory resolves by the original requested name, independently of
    # a corrupted returned Name/operation label.
    containers.get = lambda name: containers.items[0]  # type: ignore[method-assign]
    assert worker.observe(operation).state == "mismatch"
    containers.items[0].attrs["State"] = {"Running": True}
    if defect == "worker-label":
        assert worker.active() is None
    else:
        with pytest.raises(ReviewedWorkerError):
            worker.active()
    assert containers.items[0].starts == 0


def test_desktop_context_preserves_optional_broker_development_mount(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend
    client = desktop_backend(worker)
    own = client.api.inspect_container("broker-id")
    own["Mounts"].append(
        {
            "Type": "bind",
            "Source": "/srv/addon-sources",
            "Destination": "/mnt/kodi-lab-development",
            "RW": False,
            "Mode": "ro",
        }
    )
    own["HostConfig"]["Mounts"] = [
        {
            "Type": "bind",
            "Source": "/srv/addon-sources",
            "Target": "/mnt/kodi-lab-development",
            "ReadOnly": True,
            "BindOptions": {"CreateMountpoint": False},
        }
    ]
    containers.on_create = translated
    operation = uuid4()
    assert worker.launch("omega", operation)
    assert len(containers.items[0].attrs["Mounts"]) == 4
    assert worker.observe(operation).state == "running"
    assert worker.active() == (operation, "omega")


def test_changed_broker_context_cannot_adopt_an_existing_translated_worker(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend
    client = desktop_backend(worker)
    containers.on_create = translated
    operation = uuid4()
    assert worker.launch("omega", operation)
    client.api.inspect_container("broker-id")["Config"]["Labels"][
        "desktop.docker.io/wsl-distro"
    ] = "OtherDistro"
    assert worker.observe(operation).state == "mismatch"
    with pytest.raises(ReviewedWorkerError, match="ownership is ambiguous"):
        worker.active()
    with pytest.raises(ReviewedWorkerError, match="ownership is ambiguous"):
        worker.launch("omega", operation)
    assert len(containers.items) == 1 and containers.items[0].starts == 1


@pytest.mark.parametrize(
    "defect", ["readonly", "duplicate", "wrong-source", "wrong-type", "malformed"]
)
def test_broker_socket_provenance_remains_exact_before_create(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers], defect: str
) -> None:
    worker, containers = backend
    client = desktop_backend(worker)
    mounts = client.api.inspect_container("broker-id")["Mounts"]
    socket = next(m for m in mounts if m["Destination"] == SOCKET_DESTINATION)
    if defect == "readonly":
        socket["RW"] = False
    elif defect == "duplicate":
        mounts.append(dict(socket))
    elif defect == "wrong-source":
        socket["Source"] = "/tmp/unrelated.sock"
    elif defect == "wrong-type":
        socket["Type"] = "volume"
    elif defect == "malformed":
        mounts.append(None)
    with pytest.raises(ReviewedWorkerError, match=r"broker.*mounts"):
        worker.launch("omega", uuid4())
    assert containers.created is None and containers.items == []


def test_terminal_retirement_records_ready_before_exact_removal(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend
    operation = uuid4()
    assert worker.launch("omega", operation)
    item = containers.items[0]
    item.exit()
    containers.terminal(operation)
    item.log_tail = b"a" * 9000 + b"verified ready\n"

    def check_receipt() -> None:
        path = worker._receipts.pending()[0]
        result = worker._receipts.read(path).diagnostics
        assert result is not None
        assert result.outcome == "ready"
        assert result.recorded_operation == operation
        assert result.log_tail.endswith("verified ready\n")
        assert len(result.log_tail) == 8192

    item.before_remove = check_receipt
    worker.retire_terminal_workers()
    worker.retire_terminal_workers()
    assert containers.removed == [item.id]
    assert worker.active() is None
    assert worker.observe(operation).state == "missing"
    assert worker._receipts.contains_retired(operation)
    with pytest.raises(ReviewedWorkerError, match="already recorded"):
        worker.launch("omega", operation)


@pytest.mark.parametrize(("code", "oom"), [(2, False), (143, False), (137, True)])
def test_failure_or_signal_preserves_diagnostics(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    code: int,
    oom: bool,
) -> None:
    worker, containers = backend
    operation = uuid4()
    worker.launch("omega", operation)
    item = containers.items[0]
    item.exit(code, oom=oom)
    containers.terminal(operation, ready=False)
    worker.retire_terminal_workers()
    path = worker._receipts.directory("retired") / f"{operation.hex}.json"
    result = worker._receipts.read(path).diagnostics
    assert result is not None
    assert result.outcome == "failed"
    assert result.failure_code == "installation_failed"
    assert (result.exit_code, result.oom_killed) == (code, oom)
    assert result.log_tail == "worker diagnostics\n"
    assert containers.removed == [item.id]


@pytest.mark.parametrize("lost_remove_reply", [False, True])
def test_broker_restart_retries_after_durable_diagnostics(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    monkeypatch: pytest.MonkeyPatch,
    lost_remove_reply: bool,
) -> None:
    worker, containers = backend
    operation = uuid4()
    worker.launch("omega", operation)
    item = containers.items[0]
    item.exit()
    containers.terminal(operation)

    def fail_remove() -> None:
        if lost_remove_reply:
            containers.items.remove(item)
            containers.removed.append(item.id)
        raise DockerException("interrupted removal")

    item.before_remove = fail_remove
    worker.retire_terminal_workers()
    (pending,) = worker._receipts.pending()
    saved = worker._receipts.read(pending)
    assert saved.diagnostics is not None
    assert not worker._receipts.contains_retired(operation)

    restarted = DockerReviewedInstallationWorker(
        client_factory=worker._client_factory,
        runtime_root=worker._runtime_root,
    )
    monkeypatch.setattr(restarted, "_recorded_operation", containers.rows.get)
    item.before_remove = None
    item.log_tail = b"changed tail must not replace original evidence"
    restarted.retire_terminal_workers()
    restarted.retire_terminal_workers()
    assert not restarted._receipts.pending()
    retired = restarted._receipts.directory("retired") / pending.name
    assert restarted._receipts.read(retired) == saved
    assert containers.removed == [item.id]


def test_nonterminal_application_outcome_waits_for_existing_reconciliation(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend
    operation = uuid4()
    worker.launch("omega", operation)
    item = containers.items[0]
    containers.terminal(operation, ready=False)
    row = containers.rows[operation]
    row.state = RuntimeRecipeInstallationState.ACQUIRING
    worker.retire_terminal_workers()
    assert containers.items == [item]
    item.exit(137)
    worker.retire_terminal_workers()
    assert containers.items == [item]
    assert worker.observe(operation).state == "exited"
    row.state = RuntimeRecipeInstallationState.FAILED
    row.failure_code = "interrupted"
    worker.retire_terminal_workers()
    assert containers.removed == [item.id]


@pytest.mark.parametrize("created_only", [True, False])
def test_interruption_before_installation_row(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    created_only: bool,
) -> None:
    worker, containers = backend
    operation = uuid4()
    if created_only:

        def fail_create_reply(_attrs: dict[str, Any]) -> None:
            raise DockerException("lost reply after create")

        containers.on_create = fail_create_reply
        with pytest.raises(ReviewedWorkerError):
            worker.launch("omega", operation)
    else:
        worker.launch("omega", operation)
        containers.items[0].exit(137)
    (item,) = containers.items
    worker.retire_terminal_workers()
    retired = worker._receipts.directory("retired") / f"{operation.hex}.json"
    result = worker._receipts.read(retired).diagnostics
    assert result is not None
    assert result.outcome == ("not_started" if created_only else "interrupted")
    assert result.recorded_operation is None
    assert containers.removed == [item.id]


def test_preserves_worker_after_ambiguous_start_dispatch(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, containers = backend

    def delayed_start(_self: FakeContainer) -> None:
        raise DockerException("start outcome unknown")

    monkeypatch.setattr(FakeContainer, "start", delayed_start)
    with pytest.raises(ReviewedWorkerError):
        worker.launch("omega", uuid4())
    worker.retire_terminal_workers()
    assert len(containers.items) == 1
    assert not containers.removed


@pytest.mark.parametrize(
    "protection",
    ["nonce", "id", "engine", "mount", "network", "ready_recipe", "legacy"],
)
def test_protected_or_unrelated_workers_survive(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    protection: str,
) -> None:
    worker, containers = backend
    operation = uuid4()
    worker.launch("omega", operation)
    (item,) = containers.items
    item.exit()
    containers.terminal(operation)
    if protection == "nonce":
        item.attrs["Config"]["Labels"][LAUNCH_LABEL] = str(uuid4())
    elif protection == "id":
        item.attrs["Id"] = "b" * 64
    elif protection == "engine":
        worker._client().info = lambda: {"ID": "foreign-engine"}
    elif protection == "mount":
        item.attrs["Mounts"][0]["RW"] = False
    elif protection == "network":
        item.attrs["NetworkSettings"]["Networks"]["foreign"] = {}
    elif protection == "ready_recipe":
        containers.rows[operation].recipe_digest = "sha256:" + "b" * 64
    elif protection == "legacy":
        worker._receipts.pending()[0].unlink()
    worker.retire_terminal_workers()
    assert containers.items == [item]
    assert not containers.removed


def test_repeated_installs_ignore_stopped_legacy_inventory_and_preserve_live_work(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend
    operation = uuid4()
    worker.launch("omega", operation)
    (legacy,) = containers.items
    legacy.exit()
    worker._receipts.pending()[0].unlink()
    containers.items.extend([legacy] * 128)
    unrelated = FakeContainer(
        {"State": {"Running": True, "Status": "running"}, "Config": {"Labels": {}}},
        containers,
    )
    containers.items.append(unrelated)
    for _ in range(4):
        operation = uuid4()
        worker.launch("omega", operation)
        item = containers.items[-1]
        assert worker.active() == (operation, "omega")
        worker.retire_terminal_workers()
        assert item in containers.items
        item.exit()
        containers.terminal(operation)
        worker.retire_terminal_workers()
        assert item not in containers.items
    assert containers.items == [legacy] * 129 + [unrelated]
    assert len(containers.removed) == 4
    assert worker.active() is None


def test_verified_warm_reuse_preserves_original_ready_operation(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend
    previous, operation = uuid4(), uuid4()
    containers.terminal(previous)
    worker.launch("omega", operation)
    (item,) = containers.items
    item.exit()
    worker.retire_terminal_workers()
    assert not containers.removed
    item.log_tail = f"kodi-lab-reviewed-reuse {operation} {previous} {IMAGE}\n".encode()
    worker.retire_terminal_workers()
    retired = worker._receipts.directory("retired") / f"{operation.hex}.json"
    result = worker._receipts.read(retired).diagnostics
    assert result is not None
    assert result.outcome == "reused"
    assert result.recorded_operation == previous


def test_retirement_checks_desktop_aliases_and_survives_same_site_broker_upgrade(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend
    client = desktop_backend(worker)
    own = client.api.inspect_container("broker-id")
    translated(own)
    containers.on_create = translated
    operation = uuid4()
    assert worker.launch("omega", operation)
    (item,) = containers.items
    item.exit()
    containers.terminal(operation)
    own["Image"] = "sha256:" + "b" * 64
    client.api.inspect_image = lambda image: {
        "Id": image,
        "Os": "linux",
        "Architecture": "amd64",
    }
    item.attrs["Mounts"][-1]["Source"] = "/tmp/foreign-route"
    worker.retire_terminal_workers()
    assert not containers.removed
    translated(item.attrs)
    worker.retire_terminal_workers()
    assert containers.removed == [item.id]


def test_retirement_expires_only_completed_receipts(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend
    completed, pending = uuid4(), uuid4()
    worker.launch("omega", completed)
    containers.items[0].exit()
    containers.terminal(completed)
    worker.retire_terminal_workers()
    retired_path = worker._receipts.directory("retired") / f"{completed.hex}.json"
    assert retired_path.exists()
    worker.launch("omega", pending)
    pending_path = worker._receipts.pending()[0]
    os.utime(retired_path, (0, 0))
    os.utime(pending_path, (0, 0))
    worker.retire_terminal_workers()
    assert not retired_path.exists()
    assert pending_path.exists()
    assert len(containers.items) == 1


def test_retirement_does_not_cross_the_existing_acquisition_lock(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend
    operation = uuid4()
    worker.launch("omega", operation)
    (item,) = containers.items
    item.exit()
    containers.terminal(operation)
    lock = worker._runtime_root / "recipe-install.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker.retire_terminal_workers()
        assert not containers.removed
        assert worker._receipts.read(worker._receipts.pending()[0]).diagnostics is None
    finally:
        os.close(descriptor)
    worker.retire_terminal_workers()
    assert containers.removed == [item.id]


def test_started_worker_can_take_installation_lock_during_retirement_probe(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, containers = backend
    operation = uuid4()
    worker.launch("omega", operation)
    active = worker._active
    checked = False

    def worker_enters_acquisition_during_inventory(
        client: Any,
    ) -> tuple[UUID, str] | None:
        nonlocal checked
        # Docker has accepted Start. The entrypoint is only now taking the lock.
        with _installation_lock(worker._runtime_root):
            checked = True
            return active(client)

    monkeypatch.setattr(worker, "_active", worker_enters_acquisition_during_inventory)
    worker.retire_terminal_workers()
    assert checked
    assert len(containers.items) == 1 and not containers.removed


def test_installer_waits_for_in_progress_maintenance_without_failing_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered_gate = Event()
    acquired_installation_lock = Event()
    flock = fcntl.flock

    def signal_shared_gate(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_SH:
            entered_gate.set()
        flock(fd, operation)

    def acquire_for_installer() -> None:
        with (
            _maintenance_gate(tmp_path, maintenance=False),
            _installation_lock(tmp_path),
        ):
            acquired_installation_lock.set()

    monkeypatch.setattr(fcntl, "flock", signal_shared_gate)
    installer = Thread(target=acquire_for_installer)
    with (
        _maintenance_gate(tmp_path, maintenance=True),
        _installation_lock(tmp_path),
    ):
        installer.start()
        assert entered_gate.wait(5)
        assert not acquired_installation_lock.is_set()
    installer.join(timeout=5)
    assert not installer.is_alive()
    assert acquired_installation_lock.is_set()


def docker_api_error(status: int, *, method: str, path: str) -> APIError:
    return APIError(
        "synthetic Docker response",
        response=SimpleNamespace(
            status_code=status, request=SimpleNamespace(method=method, path_url=path)
        ),
    )


def test_definitively_rejected_creates_release_pending_inventory(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, containers = backend
    original_create = containers.create
    for index in range(RECEIPT_LIMIT + 3):
        operation = uuid4()
        status = (400, 404, 409)[index % 3]

        def reject(status: int = status, **_: object) -> FakeContainer:
            raise docker_api_error(
                status, method="POST", path="/v1.53/containers/create?name=fixture"
            )

        monkeypatch.setattr(containers, "create", reject)
        with pytest.raises(ReviewedWorkerError, match="worker launch is unavailable"):
            worker.launch("omega", operation)
        assert not worker._receipts.pending()
        retired = worker._receipts.directory("retired") / f"{operation.hex}.json"
        assert worker._receipts.read(retired).create_rejected_status == status
    assert (
        len(list(worker._receipts.directory("retired").glob("*.json"))) <= RECEIPT_LIMIT
    )
    assert not containers.items and not containers.removed

    monkeypatch.setattr(containers, "create", original_create)
    operation = uuid4()
    assert worker.launch("omega", operation)
    assert worker.active() == (operation, "omega")


@pytest.mark.parametrize("response", ["timeout", "server_error", "inspect_not_found"])
def test_ambiguous_create_failure_retains_intent(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    monkeypatch: pytest.MonkeyPatch,
    response: str,
) -> None:
    worker, containers = backend
    operation = uuid4()

    def lose_create_result(**_: object) -> FakeContainer:
        if response == "timeout":
            raise TimeoutError("fixture transport timeout")
        if response == "server_error":
            raise docker_api_error(500, method="POST", path="/v1.53/containers/create")
        raise docker_api_error(404, method="GET", path="/v1.53/containers/abc/json")

    monkeypatch.setattr(containers, "create", lose_create_result)
    with pytest.raises(ReviewedWorkerError, match="worker launch is unavailable"):
        worker.launch("omega", operation)
    (pending,) = worker._receipts.pending()
    worker.retire_terminal_workers()
    assert worker._receipts.pending() == [pending]
    assert worker._receipts.read(pending).create_rejected_status is None
    assert worker.observe(operation).state == "mismatch"
    assert not containers.removed


def test_create_name_conflict_leaves_legacy_container_untouched(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, containers = backend
    operation = uuid4()
    worker.launch("omega", operation)
    (legacy,) = containers.items
    legacy.exit()
    del legacy.attrs["Config"]["Labels"][LAUNCH_LABEL]
    worker._receipts.pending()[0].unlink()

    def reject(**_: object) -> FakeContainer:
        raise docker_api_error(409, method="POST", path="/v1.53/containers/create")

    monkeypatch.setattr(containers, "create", reject)
    with pytest.raises(ReviewedWorkerError, match="worker launch is unavailable"):
        worker.launch("omega", operation)
    worker.retire_terminal_workers()
    assert not worker._receipts.pending()
    assert containers.items == [legacy] and not containers.removed


def test_interrupted_worker_reconciles_and_retires_after_broker_upgrade(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    tmp_path: Path,
) -> None:
    worker, containers = backend
    operation = uuid4()
    assert worker.launch("omega", operation)
    (item,) = containers.items
    reviewed = load_installation_recipe("omega")
    engine = create_database_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    metadata.create_all(engine)
    factory = create_session_factory(engine)
    now = datetime.now(UTC)
    with factory.begin() as session:
        repo = RuntimeRecipeInstallationRepository(session)
        repo.create(
            operation_id=operation,
            recipe_id=reviewed.recipe.recipe_id,
            recipe_revision=reviewed.recipe.recipe_revision,
            recipe_digest=reviewed.digest,
            generation="omega",
            now=now,
        )
        repo.advance(
            operation, expected=RuntimeRecipeInstallationState.PENDING, now=now
        )
    client = worker._client()
    client.api.inspect_container("broker-id")["Image"] = "sha256:" + "b" * 64
    client.api.inspect_image = lambda image: {
        "Id": image,
        "Os": "linux",
        "Architecture": "amd64",
    }
    restarted = DockerReviewedInstallationWorker(
        client_factory=worker._client_factory,
        runtime_root=worker._runtime_root,
        session_factory=factory,
    )
    assert restarted.active() == (operation, "omega")
    assert restarted.observe(operation).state == "running"
    item.exit(137)
    restarted.retire_terminal_workers()
    assert containers.items == [item]
    assert restarted.observe(operation).state == "exited"

    service = RuntimeInstallationService(
        factory,
        SimpleNamespace(
            observe_reviewed_runtime_installation=lambda value: restarted.observe(
                UUID(value)
            )
        ),
    )
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation)
        assert row is not None
    result = service._reconcile(row)
    assert result.state is RuntimeRecipeInstallationState.FAILED
    assert result.failure_code == "interrupted"
    restarted.retire_terminal_workers()
    assert containers.removed == [item.id]
    retired = restarted._receipts.directory("retired") / f"{operation.hex}.json"
    receipt = restarted._receipts.read(retired)
    assert receipt.image == IMAGE
    assert receipt.diagnostics is not None and receipt.diagnostics.outcome == "failed"


@pytest.mark.parametrize("failure", ["rename", "file_sync", "directory_sync"])
def test_durability_failure_preserves_worker_across_retry_and_restart(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    worker, containers = backend
    operation = uuid4()
    worker.launch("omega", operation)
    (item,) = containers.items
    item.exit()
    containers.terminal(operation)
    (pending,) = worker._receipts.pending()
    replace = Path.replace
    fsync = os.fsync

    def fault_rename(path: Path, target: Path) -> Path:
        if target == pending:
            raise OSError("injected receipt rename failure")
        return replace(path, target)

    def fault_fsync(fd: int) -> None:
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if failure == "directory_sync" and path == pending.parent:
            raise OSError("injected receipt directory fsync failure")
        if failure == "file_sync" and path.parent == pending.parent:
            raise OSError("injected receipt file fsync failure")
        fsync(fd)

    restarted = DockerReviewedInstallationWorker(
        client_factory=worker._client_factory, runtime_root=worker._runtime_root
    )
    monkeypatch.setattr(restarted, "_recorded_operation", containers.rows.get)
    with monkeypatch.context() as fault:
        if failure == "rename":
            fault.setattr(Path, "replace", fault_rename)
        else:
            fault.setattr(os, "fsync", fault_fsync)
        worker.retire_terminal_workers()
        assert not containers.removed
        worker.retire_terminal_workers()
        restarted.retire_terminal_workers()
        assert containers.items == [item] and not containers.removed
    restarted.retire_terminal_workers()
    assert containers.removed == [item.id]


@pytest.mark.parametrize("failed_path", ["file", "directory"])
def test_existing_diagnostics_must_sync_again_after_broker_restart(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    monkeypatch: pytest.MonkeyPatch,
    failed_path: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker, containers = backend
    operation = uuid4()
    worker.launch("omega", operation)
    (item,) = containers.items
    item.exit()
    containers.terminal(operation)
    item.log_tail = b"private-fixture-diagnostic"

    def lose_remove_response() -> None:
        raise DockerException("fixture Docker error: private-fixture-diagnostic")

    item.before_remove = lose_remove_response
    worker.retire_terminal_workers()
    (pending,) = worker._receipts.pending()
    receipt = worker._receipts.read(pending)
    assert receipt.diagnostics is not None
    item.before_remove = None
    fsync = os.fsync
    failed = pending if failed_path == "file" else pending.parent

    def fault_fsync(fd: int) -> None:
        if Path(os.readlink(f"/proc/self/fd/{fd}")) == failed:
            raise OSError("injected retry durability failure")
        fsync(fd)

    restarted = DockerReviewedInstallationWorker(
        client_factory=worker._client_factory, runtime_root=worker._runtime_root
    )
    monkeypatch.setattr(restarted, "_recorded_operation", containers.rows.get)
    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", fault_fsync)
        restarted.retire_terminal_workers()
        assert containers.items == [item] and not containers.removed
        assert restarted._receipts.read(pending) == receipt
    restarted.retire_terminal_workers()
    assert containers.removed == [item.id]
    retired = restarted._receipts.directory("retired") / pending.name
    assert restarted._receipts.read(retired) == receipt
    assert "private-fixture-diagnostic" not in caplog.text
