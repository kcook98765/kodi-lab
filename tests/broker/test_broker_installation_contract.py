"""Fresh broker self-identity gates worker creation, including real Desktop metadata."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from docker.errors import DockerException  # type: ignore[import-untyped]

from kodi_lab.broker.operations import build_foundation_registry
from kodi_lab.broker.protocol import Request
from kodi_lab.broker.reviewed_installations import (
    DockerReviewedInstallationWorker,
    ReviewedWorkerError,
)
from tests.broker.test_reviewed_runtime_installation_operation import (
    DESKTOP_ROUTE,
    DESKTOP_SOCKET,
    IMAGE,
    ROUTE_DESTINATION,
    SOCKET_DESTINATION,
    FakeContainers,
    translated,
)
from tests.broker.test_reviewed_runtime_installation_operation import (
    backend as backend,
)

Backend = tuple[DockerReviewedInstallationWorker, FakeContainers]


@pytest.fixture
def actual_backend(backend: Backend) -> Backend:
    worker, containers = backend
    data = json.loads(
        (Path(__file__).parent / "fixtures/docker_desktop_broker.json").read_text()
    )
    client = worker._client()
    client.api.inspect_container = lambda _name: data["broker"]
    client.api.inspect_image = lambda _name: data["image"]
    client.info = lambda: data["engine"]
    containers.on_create = translated
    return worker, containers


def request(operation: str, params: dict[str, str]) -> Request:
    return Request(
        version=1, request_id=str(uuid4()), operation=operation, params=params
    )


def test_actual_desktop_broker_preflight_then_exact_worker_entrypoint(
    actual_backend: Backend,
) -> None:
    worker, containers = actual_backend
    contract = worker._self_contract(worker._client())
    assert contract[:3] == (
        IMAGE,
        "kodi-lab-fixture_remote_state",
        "kodi-lab-fixture_broker_socket",
    )
    assert contract[3] is not None
    assert (contract[3].socket, contract[3].route) == (DESKTOP_SOCKET, DESKTOP_ROUTE)
    registry = build_foundation_registry(reviewed_installation_worker=worker)
    before = registry.dispatch(request("active_reviewed_runtime_installation", {}))
    assert before.model_dump() == {
        "operation_id": None,
        "generation": None,
        "lock_held": False,
    }
    assert containers.created is None
    operation = str(uuid4())
    launched = registry.dispatch(
        request(
            "launch_reviewed_runtime_installation",
            {"generation": "omega", "operation_id": operation},
        )
    )
    assert launched.model_dump()["newly_accepted"] is True
    assert containers.items[0].starts == 1
    active = registry.dispatch(request("active_reviewed_runtime_installation", {}))
    assert active.model_dump()["operation_id"] == operation
    observed = registry.dispatch(
        request("observe_reviewed_runtime_installation", {"operation_id": operation})
    )
    assert observed.model_dump()["state"] == "running"


def literal_sources(own: dict[str, Any], *, mounts: bool, binds: bool) -> None:
    sources = {
        SOCKET_DESTINATION: SOCKET_DESTINATION,
        ROUTE_DESTINATION: "/proc/1/net/route",
    }
    if mounts:
        for m in own["Mounts"]:
            if m["Destination"] in sources:
                m["Source"] = sources[m["Destination"]]
    if binds:
        own["HostConfig"]["Binds"] = [
            f"{sources.get(dest, source)}:{dest}:{mode}"
            for source, dest, mode in (b.split(":") for b in own["HostConfig"]["Binds"])
        ]


@pytest.mark.parametrize(
    ("mounts", "binds"), [(True, True), (True, False), (False, True), (False, False)]
)
def test_broker_accepts_exact_literal_alias_and_mixed_descriptors(
    actual_backend: Backend,
    mounts: bool,
    binds: bool,
) -> None:
    worker, _ = actual_backend
    own = worker._client().api.inspect_container("broker-id")
    literal_sources(own, mounts=mounts, binds=binds)
    assert worker._self_contract(worker._client())[3] is not None
    assert worker.active() is None


@pytest.mark.parametrize("without_route", [False, True])
def test_native_linux_literal_broker_needs_no_desktop_engine_query(
    actual_backend: Backend,
    without_route: bool,
) -> None:
    worker, containers = actual_backend
    client = worker._client()
    own = client.api.inspect_container("broker-id")
    literal_sources(own, mounts=True, binds=True)
    if without_route:
        own["Mounts"] = [
            m for m in own["Mounts"] if m["Destination"] != ROUTE_DESTINATION
        ]
        own["HostConfig"]["Binds"] = [
            b
            for b in own["HostConfig"]["Binds"]
            if b.split(":")[1] != ROUTE_DESTINATION
        ]
    own["Config"]["Labels"] = {}
    containers.on_create = None

    def forbidden() -> None:
        raise AssertionError("Linux literal mounts do not require Desktop context")

    client.info = forbidden
    assert worker._self_contract(client)[3] is None
    assert worker.launch("omega", uuid4())


@pytest.mark.parametrize(
    "defect",
    [
        "engine-linux",
        "engine-windows",
        "engine-missing",
        "engine-malformed",
        "engine-error",
        "distro-missing",
        "distro-wrong",
        "distro-malformed",
        "distro-traversal",
        "labels-malformed",
        "socket-other",
        "route-other",
        "socket-wrong-distro",
        "route-wrong-distro",
        "socket-route-swap",
        "route-socket-swap",
        "route-suffix",
        "route-dot",
        "route-slash",
        "socket-mode",
        "route-mode",
        "socket-rw",
        "route-rw",
        "state-rw",
        "lock-rw",
        "rw-integer",
        "type",
        "destination",
        "missing",
        "extra",
        "duplicate",
        "duplicate-replacing-role",
        "mounts-malformed",
        "mount-malformed",
        "binds-missing",
        "binds-malformed",
        "bind-malformed",
        "bind-source",
        "bind-mode",
        "bind-option",
        "bind-volume",
        "bind-duplicate",
        "bind-extra",
        "structured-role",
        "volumes-from",
        "volume-name",
        "same-volumes",
        "host-malformed",
        "config-malformed",
        "image-malformed",
        "image-other",
        "image-wrong-os",
        "image-wrong-arch",
        "image-id-malformed",
        "entrypoint",
    ],
)
def test_invalid_broker_contract_fails_before_any_worker_creation(
    actual_backend: Backend,
    defect: str,
) -> None:
    worker, containers = actual_backend
    client = worker._client()
    own = client.api.inspect_container("broker-id")
    mounts, host, config = own["Mounts"], own["HostConfig"], own["Config"]
    by_dest = {m["Destination"]: m for m in mounts}
    sock, route = by_dest[SOCKET_DESTINATION], by_dest[ROUTE_DESTINATION]
    state, lock = by_dest["/var/lib/kodi-lab"], by_dest["/run/kodi-lab"]
    if defect == "engine-linux":
        client.info = lambda: {"OperatingSystem": "Ubuntu", "OSType": "linux"}
    elif defect == "engine-windows":
        client.info = lambda: {"OperatingSystem": "Docker Desktop", "OSType": "windows"}
    elif defect == "engine-missing":
        client.info = lambda: {}
    elif defect == "engine-malformed":
        client.info = lambda: []
    elif defect == "engine-error":

        def unavailable() -> None:
            raise DockerException("unavailable")

        client.info = unavailable
    elif defect == "distro-missing":
        config["Labels"].pop("desktop.docker.io/wsl-distro")
    elif defect == "distro-wrong":
        config["Labels"]["desktop.docker.io/wsl-distro"] = "OtherDistro"
    elif defect == "distro-malformed":
        config["Labels"]["desktop.docker.io/wsl-distro"] = []
    elif defect == "distro-traversal":
        config["Labels"]["desktop.docker.io/wsl-distro"] = "../Ubuntu-24.04"
    elif defect == "labels-malformed":
        config["Labels"] = []
    elif defect == "socket-other":
        sock["Source"] = "/tmp/other.sock"
    elif defect == "route-other":
        route["Source"] = DESKTOP_ROUTE[:-1] + "0"
    elif defect == "socket-wrong-distro":
        sock["Source"] = DESKTOP_SOCKET.replace("Ubuntu-24.04", "OtherDistro")
    elif defect == "route-wrong-distro":
        route["Source"] = DESKTOP_ROUTE.replace("Ubuntu-24.04", "OtherDistro")
    elif defect == "socket-route-swap":
        sock["Source"] = DESKTOP_ROUTE
    elif defect == "route-socket-swap":
        route["Source"] = DESKTOP_SOCKET
    elif defect == "route-suffix":
        route["Source"] += "suffix"
    elif defect == "route-dot":
        route["Source"] = DESKTOP_ROUTE.replace("/6d2", "/./6d2")
    elif defect == "route-slash":
        route["Source"] += "/"
    elif defect == "socket-mode":
        sock["Mode"] = "ro"
    elif defect == "route-mode":
        route["Mode"] = "rw"
    elif defect == "socket-rw":
        sock["RW"] = False
    elif defect == "route-rw":
        route["RW"] = True
    elif defect == "state-rw":
        state["RW"] = False
    elif defect == "lock-rw":
        lock["RW"] = False
    elif defect == "rw-integer":
        sock["RW"] = 1
    elif defect == "type":
        route["Type"] = "volume"
    elif defect == "destination":
        route["Destination"] = "/other"
    elif defect == "missing":
        mounts.remove(route)
    elif defect == "extra":
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
        mounts.append(deepcopy(route))
    elif defect == "duplicate-replacing-role":
        mounts[mounts.index(route)] = deepcopy(sock)
    elif defect == "mounts-malformed":
        own["Mounts"] = {}
    elif defect == "mount-malformed":
        mounts.append(None)
    elif defect == "binds-missing":
        host.pop("Binds")
    elif defect == "binds-malformed":
        host["Binds"] = {}
    elif defect == "bind-malformed":
        host["Binds"][0] = None
    elif defect == "bind-source":
        host["Binds"][-1] = "/tmp/other:" + ROUTE_DESTINATION + ":ro"
    elif defect == "bind-mode":
        host["Binds"][-1] = host["Binds"][-1].removesuffix(":ro") + ":rw"
    elif defect == "bind-option":
        host["Binds"][-1] += ",shared"
    elif defect == "bind-volume":
        host["Binds"][0] = "unrelated:/run/kodi-lab:rw"
    elif defect == "bind-duplicate":
        host["Binds"][-1] = host["Binds"][0]
    elif defect == "bind-extra":
        host["Binds"].append("/:/host:rw")
    elif defect == "structured-role":
        host["Mounts"] = [{"Type": "bind", "Source": "/", "Target": SOCKET_DESTINATION}]
    elif defect == "volumes-from":
        host["VolumesFrom"] = ["other"]
    elif defect == "volume-name":
        lock["Name"] = "other"
    elif defect == "same-volumes":
        lock["Name"] = state["Name"]
    elif defect == "host-malformed":
        own["HostConfig"] = None
    elif defect == "config-malformed":
        own["Config"] = None
    elif defect == "image-malformed":
        client.api.inspect_image = lambda _id: None
    elif defect == "image-other":
        client.api.inspect_image = lambda _id: {
            "Id": "sha256:" + "c" * 64,
            "Os": "linux",
            "Architecture": "amd64",
        }
    elif defect == "image-wrong-os":
        client.api.inspect_image = lambda _id: {
            "Id": IMAGE,
            "Os": "windows",
            "Architecture": "amd64",
        }
    elif defect == "image-wrong-arch":
        client.api.inspect_image = lambda _id: {
            "Id": IMAGE,
            "Os": "linux",
            "Architecture": "arm64",
        }
    elif defect == "image-id-malformed":
        own["Image"] = "sha256:" + "z" * 64
    elif defect == "entrypoint":
        config["Entrypoint"] = ["sh"]
    else:
        raise AssertionError(defect)
    with pytest.raises(ReviewedWorkerError):
        worker.launch("omega", uuid4())
    assert containers.created is None and containers.items == []
    with pytest.raises(ReviewedWorkerError):
        worker.active()


@pytest.mark.parametrize("structured", [False, True])
def test_supported_readonly_development_overlay_remains_broker_only(
    actual_backend: Backend,
    structured: bool,
) -> None:
    worker, containers = actual_backend
    own = worker._client().api.inspect_container("broker-id")
    own["Mounts"].append(
        {
            "Type": "bind",
            "Source": "/srv/addon-sources",
            "Destination": "/mnt/kodi-lab-development",
            "Mode": "ro",
            "RW": False,
        }
    )
    if structured:
        own["HostConfig"]["Mounts"] = [
            {
                "Type": "bind",
                "Source": "/srv/addon-sources",
                "Target": "/mnt/kodi-lab-development",
                "ReadOnly": True,
                "BindOptions": {"CreateMountpoint": False},
            }
        ]
    else:
        own["HostConfig"]["Binds"].append(
            "/srv/addon-sources:/mnt/kodi-lab-development:ro"
        )
    assert worker.launch("omega", uuid4())
    assert len(containers.items[0].attrs["Mounts"]) == 4


@pytest.mark.parametrize(
    "defect",
    [
        "rw",
        "duplicate",
        "source-conflict",
        "extra-descriptor",
        "propagation",
        "wrong-target",
        "traversal",
        "missing-mode",
    ],
)
def test_development_overlay_does_not_authorize_unexpected_broker_mounts(
    actual_backend: Backend,
    defect: str,
) -> None:
    worker, containers = actual_backend
    own = worker._client().api.inspect_container("broker-id")
    m = {
        "Type": "bind",
        "Source": "/srv/addon-sources",
        "Destination": "/mnt/kodi-lab-development",
        "Mode": "ro",
        "RW": False,
    }
    d = {
        "Type": "bind",
        "Source": "/srv/addon-sources",
        "Target": "/mnt/kodi-lab-development",
        "ReadOnly": True,
    }
    own["Mounts"].append(m)
    own["HostConfig"]["Mounts"] = [d]
    if defect == "rw":
        d["ReadOnly"] = False
    elif defect == "duplicate":
        own["Mounts"].append(deepcopy(m))
    elif defect == "source-conflict":
        d["Source"] = "/other"
    elif defect == "extra-descriptor":
        own["HostConfig"]["Binds"].append(
            "/srv/addon-sources:/mnt/kodi-lab-development:ro"
        )
    elif defect == "propagation":
        d["BindOptions"] = {"Propagation": "shared"}
    elif defect == "wrong-target":
        d["Target"] = "/other"
    elif defect == "traversal":
        m["Source"] = d["Source"] = "/srv/../other"
    elif defect == "missing-mode":
        m.pop("Mode")
    with pytest.raises(ReviewedWorkerError):
        worker.launch("omega", uuid4())
    assert containers.created is None and containers.items == []
