"""Closed worker launch and observation never accept Docker inputs from Web."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from docker.errors import DockerException  # type: ignore[import-untyped]

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
    def __init__(self, attributes: dict[str, Any]) -> None:
        self.attrs = attributes
        self.starts = 0

    def reload(self) -> None:
        pass

    def start(self) -> None:
        self.starts += 1
        self.attrs["State"] = {"Running": True}


class FakeContainers:
    def __init__(self) -> None:
        self.items: list[FakeContainer] = []
        self.created: dict[str, object] | None = None
        self.on_create: Callable[[dict[str, Any]], None] | None = None

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
                "Mode": spec["mode"],
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
                },
                "Mounts": mounts,
                "State": {"Running": False},
            }
        )
        self.items.append(item)
        if self.on_create is not None:
            self.on_create(item.attrs)
        return item

    def get(self, name: str) -> FakeContainer:
        return next(item for item in self.items if item.attrs["Name"] == "/" + name)


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
        info=lambda: {"OperatingSystem": "Ubuntu 24.04", "OSType": "linux"},
    )
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
    client.info = lambda: {"OperatingSystem": engine_os, "OSType": "linux"}
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
    containers.items[0].attrs["State"] = {"Running": False}
    assert worker.observe(operation).state == "exited"


def test_linux_literal_contract_does_not_require_desktop_observation(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend

    def forbidden() -> None:
        raise AssertionError("Linux literal identity must not query Desktop")

    worker._client().info = forbidden
    operation = uuid4()
    assert worker.launch("omega", operation)
    assert worker.observe(operation).state == "running"
    assert worker.active() == (operation, "omega")
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
        client.info = lambda: {"OperatingSystem": "Ubuntu 24.04", "OSType": "linux"}
    elif context == "windows-engine":
        client.info = lambda: {"OperatingSystem": "Docker Desktop", "OSType": "windows"}
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
        client.info = lambda: {"OperatingSystem": [], "OSType": "linux"}
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
