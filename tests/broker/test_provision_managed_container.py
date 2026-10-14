"""Fake-only Docker provisioning policy, drift, and rollback tests."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any
from uuid import UUID

import pytest
from docker.errors import APIError, NotFound  # type: ignore[import-untyped]

from kodi_lab.broker.docker_inventory import (
    INSTANCE_ID_LABEL_KEY,
    MANAGED_LABEL_KEY,
    MANAGED_LABEL_VALUE,
    NETWORK_ID_LABEL_KEY,
    NETWORK_ID_LABEL_VALUE,
    NETWORK_RESOURCE_LABEL_KEY,
    NETWORK_RESOURCE_LABEL_VALUE,
    ContainerRuntimeState,
    DockerManagedContainerAdapter,
    ProvisionedManagedContainerRecord,
)
from kodi_lab.broker.errors import (
    AmbiguousManagedContainerError,
    DockerCreateError,
    InvalidManagedContainerMetadataError,
    ManagedNetworkConflictError,
    ManagedNetworkUnavailableError,
    PartialProvisioningError,
    PostCreateVerificationError,
    ProvisioningConflictError,
    RuntimeImageUnavailableError,
)
from kodi_lab.broker.provisioning import (
    MANAGED_NETWORK_NAME,
    MANAGED_NETWORK_SUBNET,
    RUNTIME_REVISION_LABEL_KEY,
    ManagedContainerProvisioningSpec,
)
from kodi_lab.domain import PythonBytecodePolicy

INSTANCE_ID = str(UUID(int=1))
REVISION_ID = str(UUID(int=2))
CONTAINER_ID = "1" * 64
IMAGE_ID = "sha256:" + ("a" * 64)
NAME = "kodi-lab-managed-" + UUID(int=1).hex


def spec(
    policy: PythonBytecodePolicy = PythonBytecodePolicy.INTERPRETER_DEFAULT,
) -> ManagedContainerProvisioningSpec:
    return ManagedContainerProvisioningSpec(
        managed_instance_id=INSTANCE_ID,
        runtime_revision_id=REVISION_ID,
        image_id=IMAGE_ID,
        container_name=NAME,
        runtime_uid=1000,
        runtime_gid=1000,
        python_bytecode_policy=policy,
    )


def network_data() -> dict[str, object]:
    return {
        "Name": MANAGED_NETWORK_NAME,
        "Driver": "bridge",
        "Labels": {
            MANAGED_LABEL_KEY: MANAGED_LABEL_VALUE,
            NETWORK_RESOURCE_LABEL_KEY: NETWORK_RESOURCE_LABEL_VALUE,
            NETWORK_ID_LABEL_KEY: NETWORK_ID_LABEL_VALUE,
        },
        "IPAM": {"Config": [{"Subnet": MANAGED_NETWORK_SUBNET}]},
    }


def image_data() -> dict[str, object]:
    return {
        "Id": IMAGE_ID,
        "Config": {
            "Env": ["IMAGE_DEFAULT=1"],
            "Cmd": ["/image-default-command"],
            "Entrypoint": ["/image-default-entrypoint"],
            "Volumes": None,
        },
    }


def container_data(
    *, container_id: str = CONTAINER_ID, state: str = "created"
) -> dict[str, object]:
    image_config = image_data()["Config"]
    assert isinstance(image_config, dict)
    return {
        "Id": container_id,
        "Name": f"/{NAME}",
        "Image": IMAGE_ID,
        "Config": {
            **image_config,
            "User": "1000:1000",
            "Labels": {
                MANAGED_LABEL_KEY: MANAGED_LABEL_VALUE,
                INSTANCE_ID_LABEL_KEY: INSTANCE_ID,
                RUNTIME_REVISION_LABEL_KEY: REVISION_ID,
            },
        },
        "State": {"Status": state},
        "Mounts": [],
        "HostConfig": {
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "Privileged": False,
            "NetworkMode": MANAGED_NETWORK_NAME,
            "PidMode": "",
            "IpcMode": "private",
            "CapAdd": None,
            "Devices": None,
            "PortBindings": None,
            "Binds": None,
        },
        "NetworkSettings": {
            "Networks": {MANAGED_NETWORK_NAME: {"IPAddress": "dynamic"}},
            "Ports": {},
        },
    }


class FakeProvisioningDockerClient:
    def __init__(
        self,
        *,
        owned: list[object] | None = None,
        named: list[object] | None = None,
        inspections: list[object | BaseException] | None = None,
        network: object | BaseException | None = None,
        image: object | BaseException | None = None,
        create_error: BaseException | None = None,
        remove_error: BaseException | None = None,
    ) -> None:
        self.owned = [] if owned is None else owned
        self.named = [] if named is None else named
        self.inspections = [] if inspections is None else inspections
        self.network = network_data() if network is None else network
        self.image = image_data() if image is None else image
        self.create_error = create_error
        self.remove_error = remove_error
        self.create_calls: list[dict[str, object]] = []
        self.remove_calls: list[tuple[str, bool, bool]] = []
        self.start_calls = self.stop_calls = 0

    def containers(
        self, *, all: bool, filters: Mapping[str, list[str]]
    ) -> list[object]:
        return self.named if "name" in filters else self.owned

    def inspect_container(self, container: str) -> object:
        if not self.inspections:
            raise AssertionError("unexpected inspect")
        value = self.inspections.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def inspect_image(self, image: str) -> object:
        if isinstance(self.image, BaseException):
            raise self.image
        return self.image

    def inspect_network(self, network: str) -> object:
        if isinstance(self.network, BaseException):
            raise self.network
        return self.network

    def create_host_config(
        self,
        *,
        privileged: bool,
        restart_policy: Mapping[str, str],
        network_mode: str,
    ) -> object:
        return {
            "privileged": privileged,
            "restart_policy": dict(restart_policy),
            "network_mode": network_mode,
        }

    def create_endpoint_config(self) -> object:
        return {"dynamic": True}

    def create_networking_config(
        self, endpoints_config: Mapping[str, object]
    ) -> object:
        return {"endpoints": dict(endpoints_config)}

    def create_container(self, **kwargs: Any) -> object:
        self.create_calls.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        return {"Id": CONTAINER_ID}

    def remove_container(self, container: str, *, force: bool, v: bool) -> None:
        self.remove_calls.append((container, force, v))
        if self.remove_error is not None:
            raise self.remove_error

    def start(self, container: str) -> None:
        self.start_calls += 1

    def stop(self, container: str, *, timeout: int) -> None:
        self.stop_calls += 1

    def close(self) -> None:
        pass


def provision(
    client: FakeProvisioningDockerClient,
    policy: PythonBytecodePolicy = PythonBytecodePolicy.INTERPRETER_DEFAULT,
) -> ProvisionedManagedContainerRecord:
    return DockerManagedContainerAdapter(lambda: client).provision_managed_container(
        spec(policy)
    )


def test_create_configuration_is_fully_broker_derived_and_minimal() -> None:
    client = FakeProvisioningDockerClient(inspections=[container_data()])
    result = provision(client)
    assert result.created is True
    assert result.runtime_state is ContainerRuntimeState.STOPPED
    assert client.start_calls == client.stop_calls == 0
    assert client.remove_calls == []
    assert client.create_calls == [
        {
            "image": IMAGE_ID,
            "name": NAME,
            "labels": {
                MANAGED_LABEL_KEY: MANAGED_LABEL_VALUE,
                INSTANCE_ID_LABEL_KEY: INSTANCE_ID,
                RUNTIME_REVISION_LABEL_KEY: REVISION_ID,
            },
            "host_config": {
                "privileged": False,
                "restart_policy": {"Name": "no"},
                "network_mode": MANAGED_NETWORK_NAME,
            },
            "networking_config": {
                "endpoints": {MANAGED_NETWORK_NAME: {"dynamic": True}}
            },
            "user": "1000:1000",
        }
    ]
    forbidden = {
        "command",
        "entrypoint",
        "environment",
        "ports",
        "volumes",
        "host_config_overrides",
        "cpu_quota",
        "mem_limit",
    }
    assert forbidden.isdisjoint(client.create_calls[0])


def test_suppression_environment_is_exact_and_profile_derived() -> None:
    created = container_data()
    config = created["Config"]
    assert isinstance(config, dict)
    config["Env"] = ["IMAGE_DEFAULT=1", "PYTHONDONTWRITEBYTECODE=1"]
    client = FakeProvisioningDockerClient(inspections=[created])
    result = provision(client, PythonBytecodePolicy.SUPPRESS)
    assert result.created is True
    assert client.create_calls[0]["environment"] == [
        "IMAGE_DEFAULT=1",
        "PYTHONDONTWRITEBYTECODE=1",
    ]


@pytest.mark.parametrize(
    "environment",
    [
        ["IMAGE_DEFAULT=1"],
        ["IMAGE_DEFAULT=1", "PYTHONDONTWRITEBYTECODE=0"],
        ["IMAGE_DEFAULT=1", "PYTHONDONTWRITEBYTECODE=true"],
        ["IMAGE_DEFAULT=1", "PYTHONDONTWRITEBYTECODE=1", "UNTRUSTED=1"],
    ],
)
def test_suppression_environment_drift_fails(
    environment: list[str],
) -> None:
    drifted = container_data()
    config = drifted["Config"]
    assert isinstance(config, dict)
    config["Env"] = environment
    client = FakeProvisioningDockerClient(
        owned=[{"Id": CONTAINER_ID}], inspections=[drifted]
    )
    with pytest.raises(ProvisioningConflictError):
        provision(client, PythonBytecodePolicy.SUPPRESS)


def test_interpreter_default_rejects_injected_suppression() -> None:
    drifted = container_data()
    config = drifted["Config"]
    assert isinstance(config, dict)
    config["Env"] = ["IMAGE_DEFAULT=1", "PYTHONDONTWRITEBYTECODE=1"]
    client = FakeProvisioningDockerClient(
        owned=[{"Id": CONTAINER_ID}], inspections=[drifted]
    )
    with pytest.raises(ProvisioningConflictError):
        provision(client)


def test_suppression_overrides_one_image_default_without_duplicates() -> None:
    image = image_data()
    image_config = image["Config"]
    assert isinstance(image_config, dict)
    image_config["Env"] = ["FIRST=1", "PYTHONDONTWRITEBYTECODE=0", "LAST=2"]
    created = container_data()
    created_config = created["Config"]
    assert isinstance(created_config, dict)
    created_config["Env"] = ["FIRST=1", "LAST=2", "PYTHONDONTWRITEBYTECODE=1"]
    client = FakeProvisioningDockerClient(image=image, inspections=[created])
    provision(client, PythonBytecodePolicy.SUPPRESS)
    assert client.create_calls[0]["environment"] == [
        "FIRST=1",
        "LAST=2",
        "PYTHONDONTWRITEBYTECODE=1",
    ]


@pytest.mark.parametrize(
    "environment",
    [
        ["DUPLICATE=1", "DUPLICATE=2"],
        ["PYTHONDONTWRITEBYTECODE=0", "PYTHONDONTWRITEBYTECODE=1"],
        ["MALFORMED"],
    ],
)
def test_duplicate_or_malformed_image_environment_fails_closed(
    environment: list[str],
) -> None:
    image = image_data()
    image_config = image["Config"]
    assert isinstance(image_config, dict)
    image_config["Env"] = environment
    with pytest.raises(ProvisioningConflictError):
        provision(FakeProvisioningDockerClient(image=image))


def test_fully_matching_existing_container_is_idempotent_even_running() -> None:
    client = FakeProvisioningDockerClient(
        owned=[{"Id": CONTAINER_ID}], inspections=[container_data(state="running")]
    )
    result = provision(client)
    assert result.created is False
    assert result.runtime_state is ContainerRuntimeState.RUNNING
    assert client.create_calls == []
    assert client.remove_calls == []


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("Name",), "/wrong"),
        (("Image",), "sha256:" + ("b" * 64)),
        (("Config", "Labels", RUNTIME_REVISION_LABEL_KEY), str(UUID(int=9))),
        (("Config", "Env"), ["INJECTED=1"]),
        (("Config", "User"), "0:0"),
        (("Config", "Cmd"), ["injected"]),
        (("Config", "Entrypoint"), ["injected"]),
        (("Config", "Volumes"), {"/data": {}}),
        (("Mounts",), [{"Source": "/host"}]),
        (("HostConfig", "RestartPolicy", "Name"), "always"),
        (("HostConfig", "Privileged"), True),
        (("HostConfig", "NetworkMode"), "legacy"),
        (("HostConfig", "CapAdd"), ["SYS_ADMIN"]),
        (("HostConfig", "CapDrop"), ["NET_RAW"]),
        (("HostConfig", "DeviceRequests"), [{"Capabilities": [["gpu"]]}]),
        (("HostConfig", "SecurityOpt"), ["apparmor=unconfined"]),
        (("HostConfig", "PortBindings"), {"8080/tcp": [{"HostPort": "1"}]}),
        (("HostConfig", "Memory"), 1024),
        (("HostConfig", "NanoCpus"), 1_000_000_000),
        (("HostConfig", "PidsLimit"), 32),
        (("NetworkSettings", "Networks"), {"legacy": {}}),
        (("NetworkSettings", "Ports"), "malformed"),
        (("State", "Status"), "restarting"),
    ],
)
def test_existing_configuration_drift_never_mutates(
    path: tuple[str, ...], value: object
) -> None:
    data = copy.deepcopy(container_data())
    cursor: dict[str, object] = data
    for key in path[:-1]:
        nested = cursor[key]
        assert isinstance(nested, dict)
        cursor = nested
    cursor[path[-1]] = value
    client = FakeProvisioningDockerClient(
        owned=[{"Id": CONTAINER_ID}], inspections=[data]
    )
    with pytest.raises(
        (ProvisioningConflictError, InvalidManagedContainerMetadataError)
    ) as raised:
        provision(client)
    assert isinstance(
        raised.value, (ProvisioningConflictError, InvalidManagedContainerMetadataError)
    )
    assert client.create_calls == []
    assert client.remove_calls == []


def test_same_name_unowned_and_duplicate_owned_fail_without_mutation() -> None:
    named = FakeProvisioningDockerClient(
        named=[{"Id": CONTAINER_ID}],
        inspections=[{"Id": CONTAINER_ID, "Name": f"/{NAME}"}],
    )
    with pytest.raises(ProvisioningConflictError):
        provision(named)
    duplicate = FakeProvisioningDockerClient(owned=[{"Id": "1" * 64}, {"Id": "2" * 64}])
    with pytest.raises(AmbiguousManagedContainerError):
        provision(duplicate)
    assert named.create_calls == []
    assert duplicate.create_calls == []


def test_reconstruction_destination_preflight_is_read_only_and_exact() -> None:
    available = FakeProvisioningDockerClient()
    DockerManagedContainerAdapter(
        lambda: available
    ).validate_reconstruction_destination(spec())
    assert available.create_calls == []
    assert available.remove_calls == []

    missing = FakeProvisioningDockerClient(image=NotFound("PRIVATE"))
    with pytest.raises(RuntimeImageUnavailableError):
        DockerManagedContainerAdapter(
            lambda: missing
        ).validate_reconstruction_destination(spec())
    assert missing.create_calls == []
    assert missing.remove_calls == []


def test_image_and_network_prerequisites_fail_closed() -> None:
    with pytest.raises(RuntimeImageUnavailableError):
        provision(FakeProvisioningDockerClient(image=NotFound("PRIVATE")))
    with pytest.raises(ManagedNetworkUnavailableError):
        provision(FakeProvisioningDockerClient(network=NotFound("PRIVATE")))
    wrong = network_data()
    labels = wrong["Labels"]
    assert isinstance(labels, dict)
    labels[NETWORK_ID_LABEL_KEY] = "wrong"
    with pytest.raises(ManagedNetworkConflictError):
        provision(FakeProvisioningDockerClient(network=wrong))
    wrong_subnet = network_data()
    wrong_subnet["IPAM"] = {"Config": [{"Subnet": "172.31.0.0/16"}]}
    with pytest.raises(ManagedNetworkConflictError):
        provision(FakeProvisioningDockerClient(network=wrong_subnet))
    wrong_driver = network_data()
    wrong_driver["Driver"] = "macvlan"
    with pytest.raises(ManagedNetworkConflictError):
        provision(FakeProvisioningDockerClient(network=wrong_driver))
    multiple_subnets = network_data()
    multiple_subnets["IPAM"] = {
        "Config": [
            {"Subnet": MANAGED_NETWORK_SUBNET},
            {"Subnet": "10.251.0.0/24"},
        ]
    }
    with pytest.raises(ManagedNetworkConflictError):
        provision(FakeProvisioningDockerClient(network=multiple_subnets))
    with pytest.raises(ManagedNetworkConflictError):
        provision(FakeProvisioningDockerClient(network={"Name": MANAGED_NETWORK_NAME}))


def test_create_failure_is_sanitized_and_has_no_cleanup_target() -> None:
    client = FakeProvisioningDockerClient(create_error=APIError("PRIVATE-CREATE"))
    with pytest.raises(DockerCreateError) as raised:
        provision(client)
    assert "PRIVATE" not in str(raised.value)
    assert client.remove_calls == []


def test_post_create_failure_safely_removes_only_reverified_created_id() -> None:
    bad = container_data()
    bad["Image"] = "sha256:" + ("b" * 64)
    client = FakeProvisioningDockerClient(inspections=[bad, container_data()])
    with pytest.raises(PostCreateVerificationError):
        provision(client)
    assert client.remove_calls == [(CONTAINER_ID, False, False)]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("Name",), "/wrong"),
        (("Image",), "sha256:" + ("b" * 64)),
        (("Config", "Labels", MANAGED_LABEL_KEY), "false"),
        (("Config", "Labels", RUNTIME_REVISION_LABEL_KEY), str(UUID(int=9))),
        (("Config", "Env"), ["INJECTED=1"]),
        (("Mounts",), [{"Source": "/host"}]),
        (("HostConfig", "RestartPolicy", "Name"), "always"),
        (("HostConfig", "Privileged"), True),
        (("HostConfig", "SecurityOpt"), ["apparmor=unconfined"]),
        (("HostConfig", "Memory"), 1024),
        (("HostConfig", "PidsLimit"), 32),
        (("NetworkSettings", "Networks"), {"legacy": {}}),
        (("NetworkSettings", "Ports"), {"80/tcp": [{"HostPort": "8080"}]}),
        (("State", "Status"), "running"),
    ],
)
def test_each_post_create_invariant_failure_uses_proof_gated_rollback(
    path: tuple[str, ...], value: object
) -> None:
    bad = copy.deepcopy(container_data())
    cursor: dict[str, object] = bad
    for key in path[:-1]:
        nested = cursor[key]
        assert isinstance(nested, dict)
        cursor = nested
    cursor[path[-1]] = value
    client = FakeProvisioningDockerClient(inspections=[bad, container_data()])
    with pytest.raises(PostCreateVerificationError):
        provision(client)
    assert client.remove_calls == [(CONTAINER_ID, False, False)]


def test_post_create_inspect_failure_rolls_back_only_after_fresh_proof() -> None:
    client = FakeProvisioningDockerClient(
        inspections=[APIError("PRIVATE-INSPECT"), container_data()]
    )
    with pytest.raises(PostCreateVerificationError) as raised:
        provision(client)
    assert "PRIVATE" not in str(raised.value)
    assert client.remove_calls == [(CONTAINER_ID, False, False)]


def test_invalid_create_identity_is_uncertain_and_never_removed_by_name() -> None:
    client = FakeProvisioningDockerClient()

    def invalid_create(**kwargs: Any) -> object:
        client.create_calls.append(kwargs)
        return {"Id": "short"}

    client.create_container = invalid_create  # type: ignore[method-assign]
    with pytest.raises(PartialProvisioningError):
        provision(client)
    assert client.remove_calls == []


@pytest.mark.parametrize(
    "cleanup_inspection",
    [
        APIError("PRIVATE-MISSING"),
        container_data(container_id="2" * 64),
        {**container_data(), "Config": {"Labels": {}}},
    ],
)
def test_uncertain_cleanup_never_removes_and_requires_reconciliation(
    cleanup_inspection: object,
) -> None:
    bad = container_data()
    bad["Image"] = "sha256:" + ("b" * 64)
    client = FakeProvisioningDockerClient(inspections=[bad, cleanup_inspection])
    with pytest.raises(PartialProvisioningError) as raised:
        provision(client)
    assert "PRIVATE" not in str(raised.value)
    assert client.remove_calls == []


def test_cleanup_remove_failure_requires_reconciliation() -> None:
    bad = container_data()
    bad["Image"] = "sha256:" + ("b" * 64)
    client = FakeProvisioningDockerClient(
        inspections=[bad, container_data()], remove_error=APIError("PRIVATE-REMOVE")
    )
    with pytest.raises(PartialProvisioningError):
        provision(client)
    assert client.remove_calls == [(CONTAINER_ID, False, False)]


def test_adapter_public_surface_has_no_generic_docker_mutation() -> None:
    public = {
        name
        for name in dir(DockerManagedContainerAdapter)
        if not name.startswith("_")
        and callable(getattr(DockerManagedContainerAdapter, name))
    }
    assert public == {
        "apply_addon_zip_installation",
        "apply_assigned_fixture",
        "apply_managed_instance_snapshot",
        "apply_repository_replacement",
        "capture_managed_instance_snapshot",
        "copy_sync_attached_development_source",
        "deprovision_managed_container",
        "inspect_repository_replacement",
        "list_managed_containers",
        "observe_addon_zip_destination",
        "observe_managed_kodi_runtime",
        "provision_managed_container",
        "validate_reconstruction_destination",
        "start_managed_container",
        "stop_managed_container",
        "verify_attached_development_source_destination",
    }
