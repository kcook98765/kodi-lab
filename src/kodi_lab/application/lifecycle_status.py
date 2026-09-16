"""Trusted bounded semantic observation shared by lifecycle history and recovery."""

from typing import Protocol
from uuid import UUID

from kodi_lab.application.readiness_tasks import (
    ReadinessTaskRegistry,
    ReadinessTaskState,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.broker.errors import ClientError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.domain import ManagedInstanceSemanticState


class LifecycleObservationBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...


def observe_lifecycle_semantic_state(
    broker: LifecycleObservationBroker,
    readiness: ReadinessTaskRegistry | None,
    instance_id: UUID,
) -> ManagedInstanceSemanticState:
    """Map trusted current observations into the closed history vocabulary."""

    try:
        matches = [
            item
            for item in broker.managed_container_inventory().containers
            if item.managed_instance_id == str(instance_id)
        ]
    except ClientError:
        return ManagedInstanceSemanticState.AMBIGUOUS
    if not matches:
        return ManagedInstanceSemanticState.UNPROVISIONED
    if len(matches) != 1:
        return ManagedInstanceSemanticState.AMBIGUOUS
    container = matches[0]
    if container.runtime_state is ContainerRuntimeState.STOPPED:
        return ManagedInstanceSemanticState.STOPPED
    if container.runtime_state is not ContainerRuntimeState.RUNNING:
        return ManagedInstanceSemanticState.TRANSITIONAL
    try:
        observed = broker.observe_managed_kodi_runtime(
            managed_instance_id=str(instance_id),
            expected_container_id=container.container_id,
        )
    except ClientError:
        return ManagedInstanceSemanticState.RUNNING_NOT_READY
    readiness_usable = (
        readiness is not None
        and readiness.snapshot(instance_id).state is ReadinessTaskState.USABLE
    )
    if (
        observed.runtime_state is ContainerRuntimeState.RUNNING
        and observed.process_state is KodiProcessState.RUNNING
        and readiness_usable
    ):
        return ManagedInstanceSemanticState.RUNNING_USABLE
    return ManagedInstanceSemanticState.RUNNING_NOT_READY


def semantic_state_is_unambiguous(state: ManagedInstanceSemanticState) -> bool:
    return state not in {
        ManagedInstanceSemanticState.TRANSITIONAL,
        ManagedInstanceSemanticState.AMBIGUOUS,
    }
