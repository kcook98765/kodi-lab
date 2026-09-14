"""Read-only Docker adapter for Kodi Lab-owned managed containers."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

import docker  # type: ignore[import-untyped]
from docker.errors import APIError, DockerException  # type: ignore[import-untyped]

from kodi_lab.broker.errors import (
    DockerDaemonUnavailableError,
    DockerInventoryReadError,
    InvalidManagedContainerMetadataError,
    InventoryLimitExceededError,
)
from kodi_lab.broker.protocol import is_canonical_uuid

MANAGED_LABEL_KEY = "io.kodi-lab.managed"
MANAGED_LABEL_VALUE = "true"
INSTANCE_ID_LABEL_KEY = "io.kodi-lab.instance-id"
MAX_MANAGED_CONTAINERS = 128
DOCKER_API_BASE_URL = "unix:///var/run/docker.sock"
DOCKER_API_TIMEOUT_SECONDS = 5

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


@dataclass(frozen=True, slots=True)
class ManagedContainerRecord:
    """Minimal trusted semantic record returned by the Docker adapter."""

    managed_instance_id: str
    container_id: str
    image_id: str
    runtime_state: ContainerRuntimeState
    health_state: ContainerHealthState


class ManagedContainerInventoryProvider(Protocol):
    """Only Docker capability exposed to the broker operation layer."""

    def list_managed_containers(self) -> tuple[ManagedContainerRecord, ...]:
        """Return a bounded deterministic semantic inventory."""


class _DockerReadClient(Protocol):
    def containers(
        self, *, all: bool, filters: Mapping[str, list[str]]
    ) -> list[object]: ...

    def inspect_container(self, container: str) -> object: ...

    def close(self) -> None: ...


type DockerReadClientFactory = Callable[[], _DockerReadClient]


def _production_docker_client() -> _DockerReadClient:
    return cast(
        _DockerReadClient,
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
    if value in {"exited", "dead"}:
        return ContainerRuntimeState.STOPPED
    if value in {"created", "restarting", "removing", "paused"}:
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


class DockerManagedContainerInventory:
    """Fixed-filter, read-only adapter with no caller-directed Docker API."""

    def __init__(
        self, client_factory: DockerReadClientFactory = _production_docker_client
    ) -> None:
        self._client_factory = client_factory

    def list_managed_containers(self) -> tuple[ManagedContainerRecord, ...]:
        """List only exactly labelled Kodi Lab managed containers."""

        client: _DockerReadClient | None = None
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
