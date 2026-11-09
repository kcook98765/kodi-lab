"""Sanitized #274 inspection metadata; all Docker calls use the existing fake."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from kodi_lab.broker.reviewed_installations import (
    DockerReviewedInstallationWorker,
    ReviewedWorkerError,
)
from tests.broker.test_reviewed_runtime_installation_operation import (
    FakeContainers,
    desktop_backend,
    translated,
)
from tests.broker.test_reviewed_runtime_installation_operation import backend as backend

DEVELOPMENT = "/mnt/kodi-lab-development"


def captured_broker() -> dict[str, Any]:
    # Captured by the native #274 campaign on Docker 29.9.1. Only the host
    # project path, volume names/paths and application image identity are replaced.
    fixture = Path(__file__).parent / "fixtures/docker_readonly_development_broker.json"
    return cast(dict[str, Any], json.loads(fixture.read_text())["broker"])


def configure_broker(
    worker: DockerReviewedInstallationWorker, *, desktop: bool = False
) -> dict[str, Any]:
    own = captured_broker()
    client = worker._client()
    client.api.inspect_container = lambda _name: own
    if desktop:
        desktop_backend(worker)
        translated(own)
    return own


def test_native_failure_fixture_allows_read_only_worker_observation(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
) -> None:
    worker, containers = backend
    configure_broker(worker)
    assert worker.active() is None
    assert containers.created is None and containers.items == []


@pytest.mark.parametrize("mode", ["ro", ""])
@pytest.mark.parametrize("descriptor", ["structured", "bind"])
@pytest.mark.parametrize("desktop", [False, True], ids=["linux", "desktop-wsl2"])
def test_verified_readonly_representations_keep_the_closed_worker_contract(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers],
    mode: str,
    descriptor: str,
    desktop: bool,
) -> None:
    worker, containers = backend
    own = configure_broker(worker, desktop=desktop)
    own["Mounts"][0]["Mode"] = mode
    if descriptor == "bind":
        own["HostConfig"]["Mounts"] = None
        own["HostConfig"]["Binds"].append(f"/srv/addon-sources:{DEVELOPMENT}:ro")
    if desktop:
        containers.on_create = translated
    operation = uuid4()
    assert worker.launch("nexus", operation)
    assert worker.observe(operation).state == "running"
    assert worker.active() == (operation, "nexus")
    # The host-selected source is broker-only, never an installation worker bind.
    assert len(containers.items[0].attrs["Mounts"]) == 4
    assert all(
        m["Destination"] != DEVELOPMENT for m in containers.items[0].attrs["Mounts"]
    )


@pytest.mark.parametrize(
    "defect",
    [
        "rw-mode",
        "mixed-mode",
        "other-mode",
        "missing-mode",
        "null-mode",
        "rw-true",
        "rw-integer",
        "missing-rw",
        "writable-request",
        "missing-readonly",
        "integer-readonly",
        "wrong-source",
        "wrong-destination",
        "missing-source",
        "missing-destination",
        "wrong-type",
        "missing-type",
        "duplicate-resolved",
        "extra-resolved",
        "wrong-request-source",
        "wrong-request-target",
        "wrong-request-type",
        "missing-request-source",
        "missing-request-target",
        "missing-request-type",
        "duplicate-request",
        "unknown-request-field",
        "unsafe-bind-options",
        "missing-descriptor",
        "conflicting-bind-mode",
        "conflicting-bind-source",
    ],
)
def test_empty_mode_does_not_hide_unsafe_or_ambiguous_metadata(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers], defect: str
) -> None:
    worker, containers = backend
    own = configure_broker(worker)
    mount = own["Mounts"][0]
    host = own["HostConfig"]
    request = host["Mounts"][0]
    if defect in {"rw-mode", "mixed-mode", "other-mode", "null-mode"}:
        mount["Mode"] = {
            "rw-mode": "rw",
            "mixed-mode": "ro,rw",
            "other-mode": "ro,z",
            "null-mode": None,
        }[defect]
    elif defect.startswith("missing-") and defect in {
        "missing-mode",
        "missing-rw",
        "missing-source",
        "missing-destination",
        "missing-type",
    }:
        mount.pop(
            {
                "missing-mode": "Mode",
                "missing-rw": "RW",
                "missing-source": "Source",
                "missing-destination": "Destination",
                "missing-type": "Type",
            }[defect]
        )
    elif defect in {"rw-true", "rw-integer"}:
        mount["RW"] = True if defect == "rw-true" else 0
    elif defect in {"writable-request", "integer-readonly"}:
        request["ReadOnly"] = False if defect == "writable-request" else 1
    elif defect == "missing-readonly":
        request.pop("ReadOnly")
    elif defect == "wrong-source":
        mount["Source"] = "/srv/unrelated"
    elif defect == "wrong-destination":
        mount["Destination"] = "/host"
    elif defect == "wrong-type":
        mount["Type"] = "volume"
    elif defect == "duplicate-resolved":
        own["Mounts"].append(dict(mount))
    elif defect == "extra-resolved":
        own["Mounts"].append({"Type": "bind", "Source": "/", "Destination": "/host"})
    elif defect.startswith("wrong-request-"):
        key, value = {
            "wrong-request-source": ("Source", "/srv/unrelated"),
            "wrong-request-target": ("Target", "/host"),
            "wrong-request-type": ("Type", "volume"),
        }[defect]
        request[key] = value
    elif defect.startswith("missing-request-"):
        request.pop(
            {
                "missing-request-source": "Source",
                "missing-request-target": "Target",
                "missing-request-type": "Type",
            }[defect]
        )
    elif defect == "duplicate-request":
        host["Mounts"].append(dict(request))
    elif defect == "unknown-request-field":
        request["RW"] = False
    elif defect == "unsafe-bind-options":
        request["BindOptions"] = {"Propagation": "rshared"}
    elif defect == "missing-descriptor":
        host["Mounts"] = None
    elif defect.startswith("conflicting-bind-"):
        host["Mounts"] = None
        host["Binds"].append(
            f"/srv/addon-sources:{DEVELOPMENT}:rw"
            if defect == "conflicting-bind-mode"
            else f"/srv/unrelated:{DEVELOPMENT}:ro"
        )
    else:
        raise AssertionError(defect)
    with pytest.raises(ReviewedWorkerError, match=r"broker.*mounts"):
        worker.active()
    assert containers.created is None and containers.items == []


@pytest.mark.parametrize("destination", ["/var/run/docker.sock", "/var/lib/kodi-lab"])
def test_empty_mode_is_not_an_alias_for_writable_mounts(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers], destination: str
) -> None:
    worker, containers = backend
    own = configure_broker(worker)
    own["Mounts"][0]["Mode"] = "ro"
    next(m for m in own["Mounts"] if m["Destination"] == destination)["Mode"] = ""
    with pytest.raises(ReviewedWorkerError, match=r"broker.*mounts"):
        worker.active()
    assert containers.created is None


@pytest.mark.parametrize("desktop", [False, True], ids=["linux", "desktop-wsl2"])
def test_worker_readonly_route_accepts_empty_mode_with_exact_requested_ro_bind(
    backend: tuple[DockerReviewedInstallationWorker, FakeContainers], desktop: bool
) -> None:
    worker, containers = backend
    own = configure_broker(worker, desktop=desktop)
    own["Mounts"][0]["Mode"] = "ro"

    def metadata(attrs: dict[str, Any]) -> None:
        if desktop:
            translated(attrs)
        route = next(
            m
            for m in attrs["Mounts"]
            if m["Destination"] == "/tmp/kodi-lab-host-ipv4-route"
        )
        route["Mode"] = ""

    containers.on_create = metadata
    operation = uuid4()
    assert worker.launch("nexus", operation)
    assert worker.observe(operation).state == "running"
    assert worker.active() == (operation, "nexus")
