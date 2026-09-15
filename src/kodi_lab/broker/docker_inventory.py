"""Narrow Docker adapter for Kodi Lab-owned managed containers."""

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
    AmbiguousManagedContainerError,
    ContainerIdentityPreconditionError,
    ContainerStateNotReconcilableError,
    ContainerStateNotStoppableError,
    DockerDaemonUnavailableError,
    DockerInventoryReadError,
    DockerStartError,
    DockerStopError,
    InvalidManagedContainerMetadataError,
    InventoryLimitExceededError,
    ManagedContainerNotFoundError,
    PostStartVerificationError,
    PostStopVerificationError,
    RuntimeStatePreconditionError,
)
from kodi_lab.broker.protocol import is_canonical_uuid

MANAGED_LABEL_KEY = "io.kodi-lab.managed"
MANAGED_LABEL_VALUE = "true"
INSTANCE_ID_LABEL_KEY = "io.kodi-lab.instance-id"
MAX_MANAGED_CONTAINERS = 128
DOCKER_API_BASE_URL = "unix:///var/run/docker.sock"
DOCKER_API_TIMEOUT_SECONDS = 5
DOCKER_STOP_TIMEOUT_SECONDS = 10

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


class _DockerClient(Protocol):
    def containers(
        self, *, all: bool, filters: Mapping[str, list[str]]
    ) -> list[object]: ...

    def inspect_container(self, container: str) -> object: ...

    def start(self, container: str) -> None: ...

    def stop(self, container: str, *, timeout: int) -> None: ...

    def close(self) -> None: ...


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
