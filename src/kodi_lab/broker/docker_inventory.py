"""Narrow Docker adapter for Kodi Lab-owned managed containers."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Address, IPv4Network
from typing import Protocol, cast

import docker  # type: ignore[import-untyped]
from docker.errors import (  # type: ignore[import-untyped]
    APIError,
    DockerException,
    NotFound,
)

from kodi_lab.broker.errors import (
    AmbiguousManagedContainerError,
    ContainerIdentityPreconditionError,
    ContainerMustBeStoppedError,
    ContainerStateNotReconcilableError,
    ContainerStateNotStoppableError,
    DockerCreateError,
    DockerDaemonUnavailableError,
    DockerFixtureArchiveError,
    DockerInventoryReadError,
    DockerRemoveError,
    DockerSnapshotCaptureError,
    DockerStartError,
    DockerStopError,
    FixtureBaselineConflictError,
    InvalidManagedContainerMetadataError,
    InventoryLimitExceededError,
    ManagedContainerNotFoundError,
    ManagedNetworkConflictError,
    ManagedNetworkUnavailableError,
    ManagedRuntimeEndpointError,
    ManagedRuntimeNotRunningError,
    PartialProvisioningError,
    PostCreateVerificationError,
    PostDeprovisionVerificationError,
    PostStartVerificationError,
    PostStopVerificationError,
    ProvisioningConflictError,
    RuntimeImageUnavailableError,
    RuntimeRevisionPreconditionError,
    RuntimeStatePreconditionError,
)
from kodi_lab.broker.fixtures import AssignedFixtureSpec
from kodi_lab.broker.protocol import is_canonical_uuid
from kodi_lab.broker.provisioning import (
    MANAGED_NETWORK_DRIVER,
    MANAGED_NETWORK_NAME,
    MANAGED_NETWORK_SUBNET,
    RUNTIME_REVISION_LABEL_KEY,
    ManagedContainerProvisioningSpec,
    ManagedRuntimeObservationSpec,
)
from kodi_lab.broker.snapshots import SnapshotCaptureSpec
from kodi_lab.fixtures.package import (
    FIXTURE_CONTAINER_DESTINATION,
    FIXTURE_VIDEO_DATABASE_CONTAINER_PATHS,
    PackagedFixtureError,
    load_packaged_fixture,
)
from kodi_lab.snapshot.storage import (
    SnapshotArtifactIdentity,
    SnapshotStorageError,
    enumerate_snapshot_source,
    publish_snapshot_from_source,
)

MANAGED_LABEL_KEY = "io.kodi-lab.managed"
MANAGED_LABEL_VALUE = "true"
INSTANCE_ID_LABEL_KEY = "io.kodi-lab.instance-id"
MAX_MANAGED_CONTAINERS = 128
DOCKER_API_BASE_URL = "unix:///var/run/docker.sock"
DOCKER_API_TIMEOUT_SECONDS = 5
DOCKER_STOP_TIMEOUT_SECONDS = 10
NETWORK_RESOURCE_LABEL_KEY = "io.kodi-lab.resource"
NETWORK_RESOURCE_LABEL_VALUE = "network"
NETWORK_ID_LABEL_KEY = "io.kodi-lab.network-id"
NETWORK_ID_LABEL_VALUE = "managed-v1"

_CONTAINER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_FIXED_LIST_FILTERS: Mapping[str, list[str]] = {
    "label": [f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}"]
}


class ContainerRuntimeState(StrEnum):
    """Bounded runtime observation independent of Docker's full state space."""

    RUNNING = "running"
    STOPPED = "stopped"
    TRANSITIONAL = "transitional"
    UNKNOWN = "unknown"


class ContainerHealthState(StrEnum):
    """Bounded health observation independent of raw Docker inspect data."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STARTING = "starting"
    NOT_CONFIGURED = "not_configured"
    UNKNOWN = "unknown"


class KodiProcessState(StrEnum):
    """Kodi process state derived only from the trusted image healthcheck."""

    STARTING = "starting"
    RUNNING = "running"
    NOT_RUNNING = "not_running"
    UNKNOWN_ERROR = "unknown_error"


@dataclass(frozen=True, slots=True)
class ManagedContainerRecord:
    """Minimal trusted semantic record returned by the Docker adapter."""

    managed_instance_id: str
    container_id: str
    image_id: str
    runtime_state: ContainerRuntimeState
    health_state: ContainerHealthState


@dataclass(frozen=True, slots=True)
class StopManagedContainerRecord:
    """Minimal verified outcome of the semantic stop operation."""

    managed_instance_id: str
    container_id: str
    runtime_state: ContainerRuntimeState
    stop_performed: bool


@dataclass(frozen=True, slots=True)
class StartManagedContainerRecord:
    """Minimal verified outcome of the semantic start operation."""

    managed_instance_id: str
    container_id: str
    runtime_state: ContainerRuntimeState
    start_performed: bool


@dataclass(frozen=True, slots=True)
class ProvisionedManagedContainerRecord:
    """Minimal verified provisioning outcome."""

    managed_instance_id: str
    runtime_revision_id: str
    container_id: str
    image_id: str
    runtime_state: ContainerRuntimeState
    created: bool


@dataclass(frozen=True, slots=True)
class DeprovisionedManagedContainerRecord:
    """Minimal verified destructive outcome."""

    managed_instance_id: str
    removed_container_id: str
    runtime_revision_id: str
    removed: bool


@dataclass(frozen=True, slots=True)
class ManagedKodiRuntimeObservationRecord:
    """Minimal fixed-network observation for unprivileged Kodi readiness."""

    managed_instance_id: str
    runtime_revision_id: str
    container_id: str
    runtime_state: ContainerRuntimeState
    process_state: KodiProcessState
    ipv4_address: str
    jsonrpc_port: int


@dataclass(frozen=True, slots=True)
class AppliedAssignedFixtureRecord:
    """Minimal verified result of the one fixed fixture archive operation."""

    managed_instance_id: str
    runtime_revision_id: str
    container_id: str
    fixture_slug: str
    fixture_version: int
    content_digest: str
    runtime_state: ContainerRuntimeState


@dataclass(frozen=True, slots=True)
class CapturedManagedInstanceSnapshotRecord:
    """Bounded artifact identity from one fixed stopped profile capture."""

    managed_instance_id: str
    runtime_revision_id: str
    container_id: str
    artifact: SnapshotArtifactIdentity


@dataclass(frozen=True, slots=True)
class _ReconciliationRecord:
    managed_instance_id: str
    container_id: str
    runtime_state: ContainerRuntimeState
    mutation_performed: bool


class _MutationDirection(StrEnum):
    START = "start"
    STOP = "stop"


class ManagedContainerInventoryProvider(Protocol):
    """Only Docker capability exposed to the broker operation layer."""

    def list_managed_containers(self) -> tuple[ManagedContainerRecord, ...]:
        """Return a bounded deterministic semantic inventory."""


class ManagedContainerOperationsProvider(ManagedContainerInventoryProvider, Protocol):
    """Exact semantic Docker capability exposed to broker handlers."""

    def start_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StartManagedContainerRecord:
        """Start one exactly owned container under optimistic preconditions."""

    def stop_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StopManagedContainerRecord:
        """Stop one exactly owned container under optimistic preconditions."""


class ManagedContainerProvisioningProvider(Protocol):
    def provision_managed_container(
        self, spec: ManagedContainerProvisioningSpec
    ) -> ProvisionedManagedContainerRecord:
        """Create or verify one container from broker-trusted state."""


class ManagedContainerDeprovisioningProvider(Protocol):
    def deprovision_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_revision_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> DeprovisionedManagedContainerRecord:
        """Remove one exact, owned, stopped container under strict preconditions."""


class ManagedKodiRuntimeObservationProvider(Protocol):
    def observe_managed_kodi_runtime(
        self,
        *,
        spec: ManagedRuntimeObservationSpec,
        expected_container_id: str,
    ) -> ManagedKodiRuntimeObservationRecord:
        """Observe one exact owned Kodi runtime on the fixed managed network."""


class AssignedFixtureApplicationProvider(Protocol):
    def apply_assigned_fixture(
        self, *, spec: AssignedFixtureSpec, expected_container_id: str
    ) -> AppliedAssignedFixtureRecord:
        """Apply one broker-resolved fixture to one exact fresh stopped container."""


class ManagedInstanceSnapshotCaptureProvider(Protocol):
    def capture_managed_instance_snapshot(
        self, *, spec: SnapshotCaptureSpec, expected_container_id: str
    ) -> CapturedManagedInstanceSnapshotRecord:
        """Capture the fixed safe profile scope of one exact stopped container."""


class _DockerClient(Protocol):
    def containers(
        self, *, all: bool, filters: Mapping[str, list[str]]
    ) -> list[object]: ...

    def inspect_container(self, container: str) -> object: ...

    def start(self, container: str) -> None: ...

    def stop(self, container: str, *, timeout: int) -> None: ...

    def close(self) -> None: ...


class _DeprovisionDockerClient(_DockerClient, Protocol):
    def remove_container(self, container: str, *, force: bool, v: bool) -> None: ...


class _ProvisioningDockerClient(_DeprovisionDockerClient, Protocol):
    def inspect_image(self, image: str) -> object: ...

    def inspect_network(self, network: str) -> object: ...

    def create_host_config(
        self,
        *,
        privileged: bool,
        restart_policy: Mapping[str, str],
        network_mode: str,
    ) -> object: ...

    def create_endpoint_config(self) -> object: ...

    def create_networking_config(
        self, endpoints_config: Mapping[str, object]
    ) -> object: ...

    def create_container(
        self,
        *,
        image: str,
        name: str,
        labels: Mapping[str, str],
        host_config: object,
        networking_config: object,
    ) -> object: ...


class _FixtureDockerClient(_DockerClient, Protocol):
    def get_archive(self, container: str, path: str) -> tuple[object, object]: ...

    def put_archive(self, container: str, path: str, data: bytes) -> bool: ...


class _SnapshotDockerClient(_DockerClient, Protocol):
    def get_archive(self, container: str, path: str) -> tuple[object, object]: ...


type DockerClientFactory = Callable[[], _DockerClient]


def _production_docker_client() -> _DockerClient:
    return cast(
        _DockerClient,
        docker.APIClient(
            base_url=DOCKER_API_BASE_URL,
            version="auto",
            timeout=DOCKER_API_TIMEOUT_SECONDS,
        ),
    )


def _require_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DockerInventoryReadError("Docker returned malformed inventory data")
    return cast(Mapping[str, object], value)


def _require_container_id(value: object) -> str:
    if not isinstance(value, str) or _CONTAINER_ID_PATTERN.fullmatch(value) is None:
        raise DockerInventoryReadError("Docker returned an invalid container identity")
    return value


def _require_image_id(value: object) -> str:
    if not isinstance(value, str) or _IMAGE_ID_PATTERN.fullmatch(value) is None:
        raise DockerInventoryReadError("Docker returned an invalid image identity")
    return value


def normalize_runtime_state(value: object) -> ContainerRuntimeState:
    """Map Docker state to the bounded Kodi Lab inventory vocabulary."""

    if not isinstance(value, str):
        raise DockerInventoryReadError("Docker returned malformed runtime state")
    if value == "running":
        return ContainerRuntimeState.RUNNING
    if value in {"created", "exited", "dead"}:
        return ContainerRuntimeState.STOPPED
    if value in {"restarting", "removing", "paused"}:
        return ContainerRuntimeState.TRANSITIONAL
    return ContainerRuntimeState.UNKNOWN


def normalize_health_state(state: Mapping[str, object]) -> ContainerHealthState:
    """Map optional Docker health state to a bounded semantic value."""

    health = state.get("Health")
    if health is None:
        return ContainerHealthState.NOT_CONFIGURED
    health_data = _require_mapping(health)
    status = health_data.get("Status")
    if not isinstance(status, str):
        raise DockerInventoryReadError("Docker returned malformed health state")
    if status == "healthy":
        return ContainerHealthState.HEALTHY
    if status == "unhealthy":
        return ContainerHealthState.UNHEALTHY
    if status == "starting":
        return ContainerHealthState.STARTING
    return ContainerHealthState.UNKNOWN


class DockerManagedContainerAdapter:
    """Fixed semantic Docker adapter with no caller-directed raw API."""

    def __init__(
        self, client_factory: DockerClientFactory = _production_docker_client
    ) -> None:
        self._client_factory = client_factory

    def list_managed_containers(self) -> tuple[ManagedContainerRecord, ...]:
        """List only exactly labelled Kodi Lab managed containers."""

        client: _DockerClient | None = None
        try:
            client = self._client_factory()
            summaries = client.containers(all=True, filters=_FIXED_LIST_FILTERS)
            if not isinstance(summaries, list):
                raise DockerInventoryReadError(
                    "Docker returned malformed container inventory"
                )
            if len(summaries) > MAX_MANAGED_CONTAINERS:
                raise InventoryLimitExceededError(
                    "Managed container inventory exceeds the fixed limit"
                )

            records: list[ManagedContainerRecord] = []
            seen_instance_ids: set[str] = set()
            for summary_value in summaries:
                summary = _require_mapping(summary_value)
                summary_id = _require_container_id(summary.get("Id"))
                inspected = _require_mapping(client.inspect_container(summary_id))
                record = self._record_from_inspect(inspected, summary_id)
                if record is None:
                    continue
                if record.managed_instance_id in seen_instance_ids:
                    raise InvalidManagedContainerMetadataError(
                        "Managed instance identity is assigned to multiple containers"
                    )
                seen_instance_ids.add(record.managed_instance_id)
                records.append(record)

            records.sort(key=lambda item: item.managed_instance_id)
            return tuple(records)
        except (
            DockerInventoryReadError,
            InvalidManagedContainerMetadataError,
            InventoryLimitExceededError,
        ):
            raise
        except APIError as error:
            raise DockerInventoryReadError("Docker inventory read failed") from error
        except (DockerException, OSError) as error:
            raise DockerDaemonUnavailableError(
                "Docker daemon is unavailable"
            ) from error
        finally:
            if client is not None:
                with suppress(DockerException, OSError):
                    client.close()

    def provision_managed_container(
        self, spec: ManagedContainerProvisioningSpec
    ) -> ProvisionedManagedContainerRecord:
        """Create or verify one disposable container from trusted fixed policy."""

        client: _ProvisioningDockerClient | None = None
        created_id: str | None = None
        try:
            client = cast(_ProvisioningDockerClient, self._client_factory())
            self._validate_managed_network(client)
            image = self._inspect_required_image(client, spec.image_id)
            image_config = _require_mapping(image.get("Config"))
            if image_config.get("Volumes") not in (None, {}):
                raise ProvisioningConflictError(
                    "Runtime image declares volumes outside disposable policy"
                )

            owned = client.containers(
                all=True,
                filters={
                    "label": [
                        f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}",
                        f"{INSTANCE_ID_LABEL_KEY}={spec.managed_instance_id}",
                    ]
                },
            )
            if not isinstance(owned, list):
                raise DockerInventoryReadError(
                    "Docker returned malformed container selection data"
                )
            if len(owned) > 1:
                raise AmbiguousManagedContainerError(
                    "Multiple containers have the managed instance identity"
                )
            if owned:
                selected = _require_mapping(owned[0])
                selected_id = _require_container_id(selected.get("Id"))
                inspected = _require_mapping(client.inspect_container(selected_id))
                state = self._validate_provisioned_container(
                    inspected,
                    expected_container_id=selected_id,
                    spec=spec,
                    image_config=image_config,
                )
                return ProvisionedManagedContainerRecord(
                    managed_instance_id=spec.managed_instance_id,
                    runtime_revision_id=spec.runtime_revision_id,
                    container_id=selected_id,
                    image_id=spec.image_id,
                    runtime_state=state,
                    created=False,
                )

            named = client.containers(all=True, filters={"name": [spec.container_name]})
            if not isinstance(named, list):
                raise DockerInventoryReadError(
                    "Docker returned malformed name-conflict data"
                )
            for summary_value in named:
                summary = _require_mapping(summary_value)
                candidate_id = _require_container_id(summary.get("Id"))
                candidate = _require_mapping(client.inspect_container(candidate_id))
                if candidate.get("Name") in {
                    spec.container_name,
                    f"/{spec.container_name}",
                }:
                    raise ProvisioningConflictError(
                        "Deterministic managed-container name is occupied"
                    )

            labels = {
                MANAGED_LABEL_KEY: MANAGED_LABEL_VALUE,
                INSTANCE_ID_LABEL_KEY: spec.managed_instance_id,
                RUNTIME_REVISION_LABEL_KEY: spec.runtime_revision_id,
            }
            host_config = client.create_host_config(
                privileged=False,
                restart_policy={"Name": "no"},
                network_mode=spec.network_name,
            )
            endpoint = client.create_endpoint_config()
            networking = client.create_networking_config({spec.network_name: endpoint})
            try:
                created_value = client.create_container(
                    image=spec.image_id,
                    name=spec.container_name,
                    labels=labels,
                    host_config=host_config,
                    networking_config=networking,
                )
                created = _require_mapping(created_value)
                created_id = _require_container_id(created.get("Id"))
            except (DockerException, OSError) as error:
                raise DockerCreateError("Docker container creation failed") from error
            except DockerInventoryReadError as error:
                raise PartialProvisioningError(
                    "Docker created an object but did not return a safe identity"
                ) from error

            try:
                inspected = _require_mapping(client.inspect_container(created_id))
                state = self._validate_provisioned_container(
                    inspected,
                    expected_container_id=created_id,
                    spec=spec,
                    image_config=image_config,
                )
                if state is not ContainerRuntimeState.STOPPED:
                    raise PostCreateVerificationError(
                        "Created container is not stopped"
                    )
            except Exception as error:
                self._rollback_created_container(
                    client, created_id=created_id, spec=spec
                )
                raise PostCreateVerificationError(
                    "Created container did not pass verification"
                ) from error

            return ProvisionedManagedContainerRecord(
                managed_instance_id=spec.managed_instance_id,
                runtime_revision_id=spec.runtime_revision_id,
                container_id=created_id,
                image_id=spec.image_id,
                runtime_state=ContainerRuntimeState.STOPPED,
                created=True,
            )
        except (
            AmbiguousManagedContainerError,
            DockerCreateError,
            DockerInventoryReadError,
            InvalidManagedContainerMetadataError,
            ManagedNetworkConflictError,
            ManagedNetworkUnavailableError,
            PartialProvisioningError,
            PostCreateVerificationError,
            ProvisioningConflictError,
            RuntimeImageUnavailableError,
        ):
            raise
        except APIError as error:
            raise DockerInventoryReadError(
                "Docker provisioning preflight read failed"
            ) from error
        except (DockerException, OSError) as error:
            raise DockerDaemonUnavailableError(
                "Docker daemon is unavailable"
            ) from error
        finally:
            if client is not None:
                with suppress(DockerException, OSError):
                    client.close()

    @staticmethod
    def _validate_managed_network(client: _ProvisioningDockerClient) -> None:
        try:
            network_value = client.inspect_network(MANAGED_NETWORK_NAME)
        except NotFound as error:
            raise ManagedNetworkUnavailableError(
                "Required managed network does not exist"
            ) from error
        try:
            network = _require_mapping(network_value)
            if network.get("Name") != MANAGED_NETWORK_NAME:
                raise ManagedNetworkConflictError("Managed network name is invalid")
            if network.get("Driver") != MANAGED_NETWORK_DRIVER:
                raise ManagedNetworkConflictError("Managed network driver is invalid")
            labels = _require_mapping(network.get("Labels"))
            if (
                labels.get(MANAGED_LABEL_KEY) != MANAGED_LABEL_VALUE
                or labels.get(NETWORK_RESOURCE_LABEL_KEY)
                != NETWORK_RESOURCE_LABEL_VALUE
                or labels.get(NETWORK_ID_LABEL_KEY) != NETWORK_ID_LABEL_VALUE
            ):
                raise ManagedNetworkConflictError("Managed network labels are invalid")
            ipam = _require_mapping(network.get("IPAM"))
            configs = ipam.get("Config")
            if not isinstance(configs, list) or len(configs) != 1:
                raise ManagedNetworkConflictError("Managed network IPAM is invalid")
            config = _require_mapping(configs[0])
            if config.get("Subnet") != MANAGED_NETWORK_SUBNET:
                raise ManagedNetworkConflictError("Managed network subnet is invalid")
        except DockerInventoryReadError as error:
            raise ManagedNetworkConflictError(
                "Managed network configuration is malformed"
            ) from error

    @staticmethod
    def _inspect_required_image(
        client: _ProvisioningDockerClient, image_id: str
    ) -> Mapping[str, object]:
        try:
            image = _require_mapping(client.inspect_image(image_id))
        except NotFound as error:
            raise RuntimeImageUnavailableError(
                "Required immutable runtime image is unavailable"
            ) from error
        if _require_image_id(image.get("Id")) != image_id:
            raise RuntimeImageUnavailableError(
                "Required immutable runtime image identity is inconsistent"
            )
        return image

    @staticmethod
    def _validate_provisioned_container(
        inspected: Mapping[str, object],
        *,
        expected_container_id: str,
        spec: ManagedContainerProvisioningSpec,
        image_config: Mapping[str, object],
    ) -> ContainerRuntimeState:
        if _require_container_id(inspected.get("Id")) != expected_container_id:
            raise ProvisioningConflictError("Managed container identity has changed")
        if inspected.get("Name") not in {
            spec.container_name,
            f"/{spec.container_name}",
        }:
            raise ProvisioningConflictError("Managed container name has drifted")
        if _require_image_id(inspected.get("Image")) != spec.image_id:
            raise ProvisioningConflictError("Managed container image has drifted")
        config = _require_mapping(inspected.get("Config"))
        labels = _require_mapping(config.get("Labels"))
        if (
            labels.get(MANAGED_LABEL_KEY) != MANAGED_LABEL_VALUE
            or labels.get(INSTANCE_ID_LABEL_KEY) != spec.managed_instance_id
            or labels.get(RUNTIME_REVISION_LABEL_KEY) != spec.runtime_revision_id
        ):
            raise InvalidManagedContainerMetadataError(
                "Managed container ownership metadata is invalid"
            )
        for field in ("Env", "Cmd", "Entrypoint", "Healthcheck"):
            if config.get(field) != image_config.get(field):
                raise ProvisioningConflictError(
                    "Managed container image-default configuration has drifted"
                )
        if config.get("Volumes") not in (None, {}):
            raise ProvisioningConflictError("Managed container has volumes")
        if inspected.get("Mounts") not in (None, []):
            raise ProvisioningConflictError("Managed container has mounts")
        host = _require_mapping(inspected.get("HostConfig"))
        restart = _require_mapping(host.get("RestartPolicy"))
        if restart.get("Name") != "no":
            raise ProvisioningConflictError("Managed container restart policy drifted")
        if host.get("Privileged") is not False:
            raise ProvisioningConflictError("Managed container privilege drifted")
        if host.get("NetworkMode") != spec.network_name:
            raise ProvisioningConflictError("Managed container network mode drifted")
        if host.get("PidMode") not in (None, ""):
            raise ProvisioningConflictError("Managed container PID mode drifted")
        if host.get("IpcMode") not in (None, "", "private"):
            raise ProvisioningConflictError("Managed container IPC mode drifted")
        for field in (
            "CapAdd",
            "CapDrop",
            "Devices",
            "DeviceRequests",
            "SecurityOpt",
            "PortBindings",
            "Binds",
        ):
            if host.get(field) not in (None, [], {}):
                raise ProvisioningConflictError(
                    "Managed container host configuration has drifted"
                )
        for field in (
            "Memory",
            "MemoryReservation",
            "MemorySwap",
            "NanoCpus",
            "CpuShares",
            "CpuPeriod",
            "CpuQuota",
        ):
            if host.get(field) not in (None, 0):
                raise ProvisioningConflictError(
                    "Managed container resource policy has drifted"
                )
        if host.get("CpusetCpus") not in (None, "") or host.get("PidsLimit") not in (
            None,
            0,
            -1,
        ):
            raise ProvisioningConflictError(
                "Managed container resource policy has drifted"
            )
        network_settings = _require_mapping(inspected.get("NetworkSettings"))
        networks = _require_mapping(network_settings.get("Networks"))
        if set(networks) != {spec.network_name}:
            raise ProvisioningConflictError(
                "Managed container network attachment drifted"
            )
        ports = network_settings.get("Ports")
        if ports is not None and not isinstance(ports, Mapping):
            raise ProvisioningConflictError("Managed container port state is malformed")
        if isinstance(ports, Mapping) and any(ports.values()):
            raise ProvisioningConflictError("Managed container publishes host ports")
        state = _require_mapping(inspected.get("State"))
        normalized = normalize_runtime_state(state.get("Status"))
        if normalized not in {
            ContainerRuntimeState.RUNNING,
            ContainerRuntimeState.STOPPED,
        }:
            raise ProvisioningConflictError(
                "Managed container is not in an acceptable provisioned state"
            )
        return normalized

    @staticmethod
    def _rollback_created_container(
        client: _ProvisioningDockerClient,
        *,
        created_id: str,
        spec: ManagedContainerProvisioningSpec,
    ) -> None:
        try:
            inspected = _require_mapping(client.inspect_container(created_id))
            if _require_container_id(inspected.get("Id")) != created_id:
                raise ValueError
            config = _require_mapping(inspected.get("Config"))
            labels = _require_mapping(config.get("Labels"))
            if (
                labels.get(MANAGED_LABEL_KEY) != MANAGED_LABEL_VALUE
                or labels.get(INSTANCE_ID_LABEL_KEY) != spec.managed_instance_id
                or labels.get(RUNTIME_REVISION_LABEL_KEY) != spec.runtime_revision_id
            ):
                raise ValueError
            client.remove_container(created_id, force=False, v=False)
        except Exception as cleanup_error:
            raise PartialProvisioningError(
                "Partial provisioning requires operator reconciliation"
            ) from cleanup_error

    def start_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StartManagedContainerRecord:
        """Start one label-selected container under shared reconciliation rules."""

        outcome = self._reconcile_managed_container(
            managed_instance_id=managed_instance_id,
            expected_container_id=expected_container_id,
            expected_runtime_state=expected_runtime_state,
            direction=_MutationDirection.START,
        )
        return StartManagedContainerRecord(
            managed_instance_id=outcome.managed_instance_id,
            container_id=outcome.container_id,
            runtime_state=outcome.runtime_state,
            start_performed=outcome.mutation_performed,
        )

    def stop_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StopManagedContainerRecord:
        """Stop one label-selected container under shared reconciliation rules."""

        outcome = self._reconcile_managed_container(
            managed_instance_id=managed_instance_id,
            expected_container_id=expected_container_id,
            expected_runtime_state=expected_runtime_state,
            direction=_MutationDirection.STOP,
        )
        return StopManagedContainerRecord(
            managed_instance_id=outcome.managed_instance_id,
            container_id=outcome.container_id,
            runtime_state=outcome.runtime_state,
            stop_performed=outcome.mutation_performed,
        )

    def deprovision_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_revision_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> DeprovisionedManagedContainerRecord:
        """Remove one exact, owned, stopped container and prove its absence."""

        if not is_canonical_uuid(managed_instance_id):
            raise InvalidManagedContainerMetadataError(
                "Managed instance identity is not canonical"
            )
        if _CONTAINER_ID_PATTERN.fullmatch(expected_container_id) is None:
            raise ContainerIdentityPreconditionError(
                "Expected container identity is not canonical"
            )
        if not is_canonical_uuid(expected_runtime_revision_id):
            raise RuntimeRevisionPreconditionError(
                "Expected runtime revision identity is not canonical"
            )
        if expected_runtime_state is not ContainerRuntimeState.STOPPED:
            raise ContainerMustBeStoppedError(
                "Deprovision requires an explicit stopped-state precondition"
            )

        client: _DeprovisionDockerClient | None = None
        try:
            client = cast(_DeprovisionDockerClient, self._client_factory())
            filters = {
                "label": [
                    f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}",
                    f"{INSTANCE_ID_LABEL_KEY}={managed_instance_id}",
                ]
            }
            summaries = client.containers(all=True, filters=filters)
            if not isinstance(summaries, list):
                raise DockerInventoryReadError(
                    "Docker returned malformed container selection data"
                )
            if not summaries:
                raise ManagedContainerNotFoundError(
                    "No owned container exists for the managed instance"
                )
            if len(summaries) != 1:
                raise AmbiguousManagedContainerError(
                    "Multiple containers have the managed instance identity"
                )

            summary = _require_mapping(summaries[0])
            selected_container_id = _require_container_id(summary.get("Id"))
            inspected = _require_mapping(
                client.inspect_container(selected_container_id)
            )
            self._validate_deprovision_candidate(
                inspected,
                selected_container_id=selected_container_id,
                managed_instance_id=managed_instance_id,
                expected_container_id=expected_container_id,
                expected_runtime_revision_id=expected_runtime_revision_id,
            )

            try:
                client.remove_container(
                    selected_container_id,
                    force=False,
                    v=False,
                )
            except (DockerException, OSError) as error:
                raise DockerRemoveError("Docker container removal failed") from error

            try:
                still_present = client.inspect_container(selected_container_id)
            except NotFound:
                still_present = None
            except (DockerException, OSError) as error:
                raise PostDeprovisionVerificationError(
                    "Removed container absence could not be verified"
                ) from error
            if still_present is not None:
                raise PostDeprovisionVerificationError(
                    "Removed container identity still exists"
                )

            try:
                remaining = client.containers(all=True, filters=filters)
            except (DockerException, OSError) as error:
                raise PostDeprovisionVerificationError(
                    "Managed-instance container absence could not be verified"
                ) from error
            if not isinstance(remaining, list) or remaining:
                raise PostDeprovisionVerificationError(
                    "A managed container remains for the instance"
                )

            return DeprovisionedManagedContainerRecord(
                managed_instance_id=managed_instance_id,
                removed_container_id=selected_container_id,
                runtime_revision_id=expected_runtime_revision_id,
                removed=True,
            )
        except (
            AmbiguousManagedContainerError,
            ContainerIdentityPreconditionError,
            ContainerMustBeStoppedError,
            DockerInventoryReadError,
            DockerRemoveError,
            InvalidManagedContainerMetadataError,
            ManagedContainerNotFoundError,
            PostDeprovisionVerificationError,
            RuntimeRevisionPreconditionError,
        ):
            raise
        except APIError as error:
            raise DockerInventoryReadError(
                "Docker managed-container read failed"
            ) from error
        except (DockerException, OSError) as error:
            raise DockerDaemonUnavailableError(
                "Docker daemon is unavailable"
            ) from error
        finally:
            if client is not None:
                with suppress(DockerException, OSError):
                    client.close()

    def apply_assigned_fixture(
        self, *, spec: AssignedFixtureSpec, expected_container_id: str
    ) -> AppliedAssignedFixtureRecord:
        """Copy one trusted package archive to one exact fresh stopped container."""

        if _CONTAINER_ID_PATTERN.fullmatch(expected_container_id) is None:
            raise ContainerIdentityPreconditionError(
                "Expected container identity is not canonical"
            )
        try:
            packaged = load_packaged_fixture(spec.fixture_slug)
        except PackagedFixtureError as error:
            raise DockerFixtureArchiveError(
                "Trusted fixture package could not be validated"
            ) from error
        if (
            spec.fixture_id != str(packaged.definition.id)
            or spec.fixture_slug != packaged.definition.slug
            or spec.fixture_version != packaged.definition.fixture_version
            or spec.content_digest != packaged.definition.content_digest
            or spec.destination != FIXTURE_CONTAINER_DESTINATION
            or spec.destination != packaged.destination
            or spec.members != packaged.members
            or spec.archive != packaged.build_archive()
        ):
            raise DockerFixtureArchiveError(
                "Fixture application specification is not the trusted package"
            )
        client: _FixtureDockerClient | None = None
        try:
            client = cast(_FixtureDockerClient, self._client_factory())
            filters = {
                "label": [
                    f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}",
                    f"{INSTANCE_ID_LABEL_KEY}={spec.managed_instance_id}",
                ]
            }
            summaries = client.containers(all=True, filters=filters)
            if not isinstance(summaries, list):
                raise DockerInventoryReadError(
                    "Docker returned malformed fixture container selection"
                )
            if not summaries:
                raise ManagedContainerNotFoundError(
                    "No owned container exists for fixture application"
                )
            if len(summaries) != 1:
                raise AmbiguousManagedContainerError(
                    "Multiple containers have the managed instance identity"
                )
            summary = _require_mapping(summaries[0])
            selected_id = _require_container_id(summary.get("Id"))
            inspected = _require_mapping(client.inspect_container(selected_id))
            self._validate_deprovision_candidate(
                inspected,
                selected_container_id=selected_id,
                managed_instance_id=spec.managed_instance_id,
                expected_container_id=expected_container_id,
                expected_runtime_revision_id=spec.runtime_revision_id,
            )
            fixed_paths = [
                "/home/kodi/.kodi/fixture-media",
                "/home/kodi/.kodi/userdata/sources.xml",
            ]
            database_path = FIXTURE_VIDEO_DATABASE_CONTAINER_PATHS.get(
                spec.fixture_slug
            )
            if database_path is not None:
                fixed_paths.append(database_path)
            for fixed_path in fixed_paths:
                if self._fixed_container_path_exists(client, selected_id, fixed_path):
                    raise FixtureBaselineConflictError(
                        "Fresh container already contains fixture-owned state"
                    )
            try:
                applied = client.put_archive(
                    selected_id,
                    spec.destination,
                    spec.archive,
                )
            except (DockerException, OSError) as error:
                raise DockerFixtureArchiveError(
                    "Docker fixture archive application failed"
                ) from error
            if applied is not True:
                raise DockerFixtureArchiveError(
                    "Docker did not confirm fixture archive application"
                )
            for fixed_path in fixed_paths:
                if not self._fixed_container_path_exists(
                    client, selected_id, fixed_path
                ):
                    raise DockerFixtureArchiveError(
                        "Docker could not verify fixture archive postcondition"
                    )
            final = _require_mapping(client.inspect_container(selected_id))
            self._validate_deprovision_candidate(
                final,
                selected_container_id=selected_id,
                managed_instance_id=spec.managed_instance_id,
                expected_container_id=expected_container_id,
                expected_runtime_revision_id=spec.runtime_revision_id,
            )
            return AppliedAssignedFixtureRecord(
                managed_instance_id=spec.managed_instance_id,
                runtime_revision_id=spec.runtime_revision_id,
                container_id=selected_id,
                fixture_slug=spec.fixture_slug,
                fixture_version=spec.fixture_version,
                content_digest=spec.content_digest,
                runtime_state=ContainerRuntimeState.STOPPED,
            )
        except (
            AmbiguousManagedContainerError,
            ContainerIdentityPreconditionError,
            ContainerMustBeStoppedError,
            DockerFixtureArchiveError,
            DockerInventoryReadError,
            FixtureBaselineConflictError,
            InvalidManagedContainerMetadataError,
            ManagedContainerNotFoundError,
            RuntimeRevisionPreconditionError,
        ):
            raise
        except APIError as error:
            raise DockerInventoryReadError(
                "Docker fixture application preflight failed"
            ) from error
        except (DockerException, OSError) as error:
            raise DockerDaemonUnavailableError(
                "Docker daemon is unavailable"
            ) from error
        finally:
            if client is not None:
                with suppress(DockerException, OSError):
                    client.close()

    def capture_managed_instance_snapshot(
        self, *, spec: SnapshotCaptureSpec, expected_container_id: str
    ) -> CapturedManagedInstanceSnapshotRecord:
        """Capture only the fixed Kodi profile scope from one stopped container."""

        context = spec.context
        managed_instance_id = str(context.source_managed_instance_id)
        runtime_revision_id = str(context.runtime_revision_id)
        if _CONTAINER_ID_PATTERN.fullmatch(expected_container_id) is None:
            raise ContainerIdentityPreconditionError(
                "Expected container identity is not canonical"
            )
        client: _SnapshotDockerClient | None = None
        try:
            client = cast(_SnapshotDockerClient, self._client_factory())
            summaries = client.containers(
                all=True,
                filters={
                    "label": [
                        f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}",
                        f"{INSTANCE_ID_LABEL_KEY}={managed_instance_id}",
                    ]
                },
            )
            if not isinstance(summaries, list):
                raise DockerInventoryReadError(
                    "Docker returned malformed container selection data"
                )
            if not summaries:
                raise ManagedContainerNotFoundError(
                    "No owned container exists for the managed instance"
                )
            if len(summaries) != 1:
                raise AmbiguousManagedContainerError(
                    "Multiple containers have the managed instance identity"
                )
            summary = _require_mapping(summaries[0])
            selected_id = _require_container_id(summary.get("Id"))
            inspected = _require_mapping(client.inspect_container(selected_id))
            self._validate_deprovision_candidate(
                inspected,
                selected_container_id=selected_id,
                managed_instance_id=managed_instance_id,
                expected_container_id=expected_container_id,
                expected_runtime_revision_id=runtime_revision_id,
            )
            first_stream, _first_stat = client.get_archive(
                selected_id, "/home/kodi/.kodi"
            )
            expected_members = enumerate_snapshot_source(
                cast(Iterable[bytes], first_stream)
            )
            second_stream, _second_stat = client.get_archive(
                selected_id, "/home/kodi/.kodi"
            )
            artifact = publish_snapshot_from_source(
                data_root=spec.data_root,
                context=context,
                expected_members=expected_members,
                source_chunks=cast(Iterable[bytes], second_stream),
                owner_uid=spec.owner_uid,
                owner_gid=spec.owner_gid,
            )
            final = _require_mapping(client.inspect_container(selected_id))
            self._validate_deprovision_candidate(
                final,
                selected_container_id=selected_id,
                managed_instance_id=managed_instance_id,
                expected_container_id=expected_container_id,
                expected_runtime_revision_id=runtime_revision_id,
            )
            return CapturedManagedInstanceSnapshotRecord(
                managed_instance_id=managed_instance_id,
                runtime_revision_id=runtime_revision_id,
                container_id=selected_id,
                artifact=artifact,
            )
        except (
            AmbiguousManagedContainerError,
            ContainerIdentityPreconditionError,
            ContainerMustBeStoppedError,
            DockerInventoryReadError,
            InvalidManagedContainerMetadataError,
            ManagedContainerNotFoundError,
            RuntimeRevisionPreconditionError,
        ):
            raise
        except SnapshotStorageError as error:
            raise DockerSnapshotCaptureError(error.code, error.safe_message) from error
        except APIError as error:
            raise DockerSnapshotCaptureError(
                "snapshot_capture_failed", "Docker snapshot capture failed"
            ) from error
        except (DockerException, OSError) as error:
            raise DockerSnapshotCaptureError(
                "snapshot_capture_failed", "Docker snapshot capture failed"
            ) from error
        finally:
            if client is not None:
                with suppress(DockerException, OSError):
                    client.close()

    @staticmethod
    def _fixed_container_path_exists(
        client: _FixtureDockerClient, container_id: str, path: str
    ) -> bool:
        try:
            stream, _stat = client.get_archive(container_id, path)
        except NotFound:
            return False
        except (DockerException, OSError) as error:
            raise DockerFixtureArchiveError(
                "Docker could not inspect fixed fixture path"
            ) from error
        close = getattr(stream, "close", None)
        if callable(close):
            close()
        return True

    def observe_managed_kodi_runtime(
        self,
        *,
        spec: ManagedRuntimeObservationSpec,
        expected_container_id: str,
    ) -> ManagedKodiRuntimeObservationRecord:
        """Return only process state and the canonical managed-network endpoint."""

        if _CONTAINER_ID_PATTERN.fullmatch(expected_container_id) is None:
            raise ContainerIdentityPreconditionError(
                "Expected container identity is not canonical"
            )
        client: _DockerClient | None = None
        try:
            client = self._client_factory()
            summaries = client.containers(
                all=True,
                filters={
                    "label": [
                        f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}",
                        f"{INSTANCE_ID_LABEL_KEY}={spec.managed_instance_id}",
                    ]
                },
            )
            if not isinstance(summaries, list):
                raise DockerInventoryReadError(
                    "Docker returned malformed runtime selection data"
                )
            if not summaries:
                raise ManagedContainerNotFoundError(
                    "No owned container exists for the managed instance"
                )
            if len(summaries) != 1:
                raise AmbiguousManagedContainerError(
                    "Multiple containers have the managed instance identity"
                )
            selected = _require_mapping(summaries[0])
            selected_id = _require_container_id(selected.get("Id"))
            inspected = _require_mapping(client.inspect_container(selected_id))
            runtime_state, process_state, address = (
                self._runtime_observation_from_inspect(
                    inspected,
                    spec=spec,
                    selected_container_id=selected_id,
                    expected_container_id=expected_container_id,
                )
            )
            return ManagedKodiRuntimeObservationRecord(
                managed_instance_id=spec.managed_instance_id,
                runtime_revision_id=spec.runtime_revision_id,
                container_id=selected_id,
                runtime_state=runtime_state,
                process_state=process_state,
                ipv4_address=address,
                jsonrpc_port=spec.profile.jsonrpc_port,
            )
        except (
            AmbiguousManagedContainerError,
            ContainerIdentityPreconditionError,
            DockerInventoryReadError,
            InvalidManagedContainerMetadataError,
            ManagedContainerNotFoundError,
            ManagedRuntimeEndpointError,
            ManagedRuntimeNotRunningError,
            RuntimeRevisionPreconditionError,
        ):
            raise
        except APIError as error:
            raise DockerInventoryReadError(
                "Docker managed-runtime observation failed"
            ) from error
        except (DockerException, OSError) as error:
            raise DockerDaemonUnavailableError(
                "Docker daemon is unavailable"
            ) from error
        finally:
            if client is not None:
                with suppress(DockerException, OSError):
                    client.close()

    @staticmethod
    def _runtime_observation_from_inspect(
        inspected: Mapping[str, object],
        *,
        spec: ManagedRuntimeObservationSpec,
        selected_container_id: str,
        expected_container_id: str,
    ) -> tuple[ContainerRuntimeState, KodiProcessState, str]:
        container_id = _require_container_id(inspected.get("Id"))
        if container_id != selected_container_id:
            raise DockerInventoryReadError("Docker container identity changed")
        if container_id != expected_container_id:
            raise ContainerIdentityPreconditionError(
                "Managed container identity differs from the precondition"
            )
        config = _require_mapping(inspected.get("Config"))
        labels = _require_mapping(config.get("Labels"))
        if (
            labels.get(MANAGED_LABEL_KEY) != MANAGED_LABEL_VALUE
            or labels.get(INSTANCE_ID_LABEL_KEY) != spec.managed_instance_id
        ):
            raise InvalidManagedContainerMetadataError(
                "Selected container ownership metadata is invalid"
            )
        if labels.get(RUNTIME_REVISION_LABEL_KEY) != spec.runtime_revision_id:
            raise RuntimeRevisionPreconditionError(
                "Managed runtime revision differs from trusted state"
            )
        state = _require_mapping(inspected.get("State"))
        runtime_state = normalize_runtime_state(state.get("Status"))
        if runtime_state is not ContainerRuntimeState.RUNNING:
            raise ManagedRuntimeNotRunningError(
                "Managed runtime endpoint requires a running container"
            )
        try:
            health_state = normalize_health_state(state)
        except DockerInventoryReadError:
            health_state = ContainerHealthState.UNKNOWN
        process_state = {
            ContainerHealthState.HEALTHY: KodiProcessState.RUNNING,
            ContainerHealthState.UNHEALTHY: KodiProcessState.NOT_RUNNING,
            ContainerHealthState.STARTING: KodiProcessState.STARTING,
            ContainerHealthState.NOT_CONFIGURED: KodiProcessState.UNKNOWN_ERROR,
            ContainerHealthState.UNKNOWN: KodiProcessState.UNKNOWN_ERROR,
        }[health_state]
        network_settings = _require_mapping(inspected.get("NetworkSettings"))
        networks = _require_mapping(network_settings.get("Networks"))
        if set(networks) != {MANAGED_NETWORK_NAME}:
            raise ManagedRuntimeEndpointError(
                "Managed runtime has an unexpected network attachment"
            )
        endpoint = _require_mapping(networks.get(MANAGED_NETWORK_NAME))
        raw_address = endpoint.get("IPAddress")
        if not isinstance(raw_address, str) or not raw_address:
            raise ManagedRuntimeEndpointError(
                "Managed runtime has no canonical IPv4 address"
            )
        try:
            address = IPv4Address(raw_address)
        except ValueError as error:
            raise ManagedRuntimeEndpointError(
                "Managed runtime IPv4 address is invalid"
            ) from error
        network = IPv4Network(MANAGED_NETWORK_SUBNET)
        if address not in network or address in {
            network.network_address,
            network.broadcast_address,
        }:
            raise ManagedRuntimeEndpointError(
                "Managed runtime IPv4 address is outside the managed subnet"
            )
        return runtime_state, process_state, str(address)

    def _reconcile_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
        direction: _MutationDirection,
    ) -> _ReconciliationRecord:
        """Reconcile one exact owned container to one fixed two-state outcome."""

        if not is_canonical_uuid(managed_instance_id):
            raise InvalidManagedContainerMetadataError(
                "Managed instance identity is not canonical"
            )
        if _CONTAINER_ID_PATTERN.fullmatch(expected_container_id) is None:
            raise ContainerIdentityPreconditionError(
                "Expected container identity is not canonical"
            )

        client: _DockerClient | None = None
        try:
            client = self._client_factory()
            summaries = client.containers(
                all=True,
                filters={
                    "label": [
                        f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}",
                        f"{INSTANCE_ID_LABEL_KEY}={managed_instance_id}",
                    ]
                },
            )
            if not isinstance(summaries, list):
                raise DockerInventoryReadError(
                    "Docker returned malformed container selection data"
                )
            if not summaries:
                raise ManagedContainerNotFoundError(
                    "No owned container exists for the managed instance"
                )
            if len(summaries) != 1:
                raise AmbiguousManagedContainerError(
                    "Multiple containers have the managed instance identity"
                )

            summary = _require_mapping(summaries[0])
            selected_container_id = _require_container_id(summary.get("Id"))
            inspected = _require_mapping(
                client.inspect_container(selected_container_id)
            )
            current_state = self._validate_reconciliation_candidate(
                inspected,
                selected_container_id=selected_container_id,
                managed_instance_id=managed_instance_id,
                expected_container_id=expected_container_id,
            )

            desired_state = (
                ContainerRuntimeState.RUNNING
                if direction is _MutationDirection.START
                else ContainerRuntimeState.STOPPED
            )
            if current_state is desired_state:
                return _ReconciliationRecord(
                    managed_instance_id=managed_instance_id,
                    container_id=expected_container_id,
                    runtime_state=desired_state,
                    mutation_performed=False,
                )
            if current_state in {
                ContainerRuntimeState.TRANSITIONAL,
                ContainerRuntimeState.UNKNOWN,
            }:
                if direction is _MutationDirection.STOP:
                    raise ContainerStateNotStoppableError(
                        "Managed container is not in a safely stoppable state"
                    )
                raise ContainerStateNotReconcilableError(
                    "Managed container is outside safe two-state reconciliation"
                )
            if expected_runtime_state is not current_state:
                raise RuntimeStatePreconditionError(
                    "Managed container runtime state differs from the precondition"
                )

            try:
                if direction is _MutationDirection.START:
                    client.start(selected_container_id)
                else:
                    client.stop(
                        selected_container_id,
                        timeout=DOCKER_STOP_TIMEOUT_SECONDS,
                    )
            except (DockerException, OSError) as error:
                if direction is _MutationDirection.START:
                    raise DockerStartError("Docker start failed") from error
                raise DockerStopError("Docker stop failed") from error

            try:
                final_inspected = _require_mapping(
                    client.inspect_container(selected_container_id)
                )
                final_state = self._validate_reconciliation_candidate(
                    final_inspected,
                    selected_container_id=selected_container_id,
                    managed_instance_id=managed_instance_id,
                    expected_container_id=expected_container_id,
                )
            except (
                DockerException,
                OSError,
                DockerInventoryReadError,
                InvalidManagedContainerMetadataError,
                ContainerIdentityPreconditionError,
            ) as error:
                if direction is _MutationDirection.START:
                    raise PostStartVerificationError(
                        "Running state could not be verified"
                    ) from error
                raise PostStopVerificationError(
                    "Stopped state could not be verified"
                ) from error
            if final_state is not desired_state:
                if direction is _MutationDirection.START:
                    raise PostStartVerificationError(
                        "Managed container did not reach running state"
                    )
                raise PostStopVerificationError(
                    "Managed container did not reach stopped state"
                )

            return _ReconciliationRecord(
                managed_instance_id=managed_instance_id,
                container_id=expected_container_id,
                runtime_state=desired_state,
                mutation_performed=True,
            )
        except (
            AmbiguousManagedContainerError,
            ContainerIdentityPreconditionError,
            ContainerStateNotReconcilableError,
            DockerInventoryReadError,
            DockerStartError,
            DockerStopError,
            InvalidManagedContainerMetadataError,
            ManagedContainerNotFoundError,
            PostStartVerificationError,
            PostStopVerificationError,
            RuntimeStatePreconditionError,
        ):
            raise
        except APIError as error:
            raise DockerInventoryReadError(
                "Docker managed-container read failed"
            ) from error
        except (DockerException, OSError) as error:
            raise DockerDaemonUnavailableError(
                "Docker daemon is unavailable"
            ) from error
        finally:
            if client is not None:
                with suppress(DockerException, OSError):
                    client.close()

    @staticmethod
    def _validate_deprovision_candidate(
        inspected: Mapping[str, object],
        *,
        selected_container_id: str,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_revision_id: str,
    ) -> None:
        """Prove destructive identity and stopped state from fresh inspect data."""

        container_id = _require_container_id(inspected.get("Id"))
        if container_id != selected_container_id:
            raise DockerInventoryReadError(
                "Docker container identity changed during selection"
            )
        if container_id != expected_container_id:
            raise ContainerIdentityPreconditionError(
                "Managed container identity differs from the precondition"
            )
        expected_name = f"kodi-lab-managed-{managed_instance_id.replace('-', '')}"
        if inspected.get("Name") not in {expected_name, f"/{expected_name}"}:
            raise InvalidManagedContainerMetadataError(
                "Managed container deterministic identity is invalid"
            )

        config = _require_mapping(inspected.get("Config"))
        labels_value = config.get("Labels")
        if not isinstance(labels_value, Mapping):
            raise InvalidManagedContainerMetadataError(
                "Selected container has no valid Kodi Lab ownership metadata"
            )
        labels = cast(Mapping[str, object], labels_value)
        if labels.get(MANAGED_LABEL_KEY) != MANAGED_LABEL_VALUE:
            raise InvalidManagedContainerMetadataError(
                "Selected container no longer has Kodi Lab ownership"
            )
        inspected_instance_id = labels.get(INSTANCE_ID_LABEL_KEY)
        if not is_canonical_uuid(inspected_instance_id):
            raise InvalidManagedContainerMetadataError(
                "Managed container has invalid instance identity metadata"
            )
        if inspected_instance_id != managed_instance_id:
            raise InvalidManagedContainerMetadataError(
                "Selected container has different managed instance ownership"
            )
        inspected_revision_id = labels.get(RUNTIME_REVISION_LABEL_KEY)
        if not is_canonical_uuid(inspected_revision_id):
            raise InvalidManagedContainerMetadataError(
                "Managed container has invalid runtime revision metadata"
            )
        if inspected_revision_id != expected_runtime_revision_id:
            raise RuntimeRevisionPreconditionError(
                "Managed container runtime revision differs from the precondition"
            )

        state = _require_mapping(inspected.get("State"))
        if (
            normalize_runtime_state(state.get("Status"))
            is not ContainerRuntimeState.STOPPED
        ):
            raise ContainerMustBeStoppedError(
                "Managed container must be stopped before deprovisioning"
            )

    @staticmethod
    def _record_from_inspect(
        inspected: Mapping[str, object], summary_id: str
    ) -> ManagedContainerRecord | None:
        container_id = _require_container_id(inspected.get("Id"))
        if container_id != summary_id:
            raise DockerInventoryReadError(
                "Docker container identity changed during read"
            )

        config = _require_mapping(inspected.get("Config"))
        labels_value = config.get("Labels")
        if labels_value is None:
            return None
        labels = _require_mapping(labels_value)
        if labels.get(MANAGED_LABEL_KEY) != MANAGED_LABEL_VALUE:
            return None

        instance_id = labels.get(INSTANCE_ID_LABEL_KEY)
        if not is_canonical_uuid(instance_id):
            raise InvalidManagedContainerMetadataError(
                "Managed container has invalid instance identity metadata"
            )
        assert isinstance(instance_id, str)

        state = _require_mapping(inspected.get("State"))
        return ManagedContainerRecord(
            managed_instance_id=instance_id,
            container_id=container_id,
            image_id=_require_image_id(inspected.get("Image")),
            runtime_state=normalize_runtime_state(state.get("Status")),
            health_state=normalize_health_state(state),
        )

    @staticmethod
    def _validate_reconciliation_candidate(
        inspected: Mapping[str, object],
        *,
        selected_container_id: str,
        managed_instance_id: str,
        expected_container_id: str,
    ) -> ContainerRuntimeState:
        """Revalidate ownership and immutable identity from fresh inspect data."""

        container_id = _require_container_id(inspected.get("Id"))
        if container_id != selected_container_id:
            raise DockerInventoryReadError(
                "Docker container identity changed during selection"
            )

        config = _require_mapping(inspected.get("Config"))
        labels_value = config.get("Labels")
        if not isinstance(labels_value, Mapping):
            raise InvalidManagedContainerMetadataError(
                "Selected container has no valid Kodi Lab ownership metadata"
            )
        labels = cast(Mapping[str, object], labels_value)
        if labels.get(MANAGED_LABEL_KEY) != MANAGED_LABEL_VALUE:
            raise InvalidManagedContainerMetadataError(
                "Selected container no longer has Kodi Lab ownership"
            )
        inspected_instance_id = labels.get(INSTANCE_ID_LABEL_KEY)
        if not is_canonical_uuid(inspected_instance_id):
            raise InvalidManagedContainerMetadataError(
                "Managed container has invalid instance identity metadata"
            )
        if inspected_instance_id != managed_instance_id:
            raise InvalidManagedContainerMetadataError(
                "Selected container has different managed instance ownership"
            )
        if container_id != expected_container_id:
            raise ContainerIdentityPreconditionError(
                "Managed container identity differs from the precondition"
            )

        state = _require_mapping(inspected.get("State"))
        return normalize_runtime_state(state.get("Status"))
