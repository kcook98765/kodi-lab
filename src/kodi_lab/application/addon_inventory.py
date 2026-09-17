"""Live, read-only managed-instance Kodi add-on inventory."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    KodiProcessState,
)
from kodi_lab.broker.errors import ClientError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.domain.development_source_deployments import (
    DevelopmentSourceDeployment,
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentOperationState,
)
from kodi_lab.kodi.addons import (
    KodiAddonInventoryAdapter,
    KodiAddonInventoryIdentityError,
    KodiAddonInventoryLimitExceededError,
    KodiAddonInventoryMalformedDataError,
    KodiAddonInventoryResult,
    KodiAddonInventoryUnsupportedResponseError,
)
from kodi_lab.kodi.readiness import JsonRpcUnavailableError
from kodi_lab.storage.repositories import (
    DevelopmentSourceDeploymentOperationRepository,
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceRepository,
    ManagedInstanceRepository,
    RuntimeExecutionProfileRepository,
)


class AddonInventoryQueryStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class AddonInventoryUnavailableReason(StrEnum):
    INSTANCE_NOT_PROVISIONED = "instance_not_provisioned"
    KODI_NOT_RUNNING = "kodi_not_running"
    KODI_NOT_READY = "kodi_not_ready"
    JSONRPC_UNAVAILABLE = "jsonrpc_unavailable"
    UNSUPPORTED_RESPONSE = "unsupported_response"
    MALFORMED_ADDON_DATA = "malformed_addon_data"
    INVENTORY_LIMIT_EXCEEDED = "inventory_limit_exceeded"


class KodiAddonProvenanceClassification(StrEnum):
    DEVELOPMENT_SOURCE_AT_LAST_VERIFIED_COPY = (
        "development_source_at_last_verified_copy"
    )
    UNKNOWN = "unknown"


class KodiAddonProvenanceEvidenceStatus(StrEnum):
    LAST_OPERATION_VERIFIED = "last_operation_verified"
    STALE = "stale"
    UNKNOWN = "unknown"


class KodiAddonProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: KodiAddonProvenanceClassification
    evidence_status: KodiAddonProvenanceEvidenceStatus
    development_source_id: UUID | None = None
    development_source_logical_name: str | None = Field(default=None, max_length=256)
    verified_at: datetime | None = None


class KodiAddonInventoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addon_id: str = Field(max_length=128)
    name: str = Field(max_length=256)
    version: str = Field(max_length=128)
    enabled: StrictBool
    kodi_type: str = Field(max_length=128)
    provenance: KodiAddonProvenance


class ManagedInstanceAddonInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: UUID
    observed_at: datetime
    kodi_release: str = Field(max_length=32)
    query_status: AddonInventoryQueryStatus
    unavailable_reason: AddonInventoryUnavailableReason | None = None
    complete: StrictBool
    items: list[KodiAddonInventoryItem] = Field(max_length=2_048)


class _BrokerInventoryClient(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...


class _InventoryAdapter(Protocol):
    def get_inventory(self) -> KodiAddonInventoryResult: ...


type InventoryAdapterFactory = Callable[
    [ObserveManagedKodiRuntimeResult, RuntimeExecutionProfile], _InventoryAdapter
]


class ManagedInstanceAddonInventoryService:
    """Resolve trusted runtime state and perform one bounded live observation."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _BrokerInventoryClient,
        *,
        adapter_factory: InventoryAdapterFactory | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._adapter_factory = adapter_factory or (
            lambda observation, profile: KodiAddonInventoryAdapter(observation, profile)
        )
        self._now = now or (lambda: datetime.now(UTC))

    def get(self, instance_id: UUID) -> ManagedInstanceAddonInventory:
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            if instance is None:
                raise ManagedInstanceNotFoundError(instance_id)
            profile = RuntimeExecutionProfileRepository(session).get(
                instance.runtime_revision_id
            )
        kodi_release = "unknown" if profile is None else profile.kodi_release.canonical
        if profile is None:
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.KODI_NOT_READY,
            )

        try:
            inventory = self._broker.managed_container_inventory()
        except ClientError:
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.KODI_NOT_READY,
            )
        matches = [
            item
            for item in inventory.containers
            if item.managed_instance_id == str(instance_id)
        ]
        if not matches:
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.INSTANCE_NOT_PROVISIONED,
            )
        if len(matches) != 1:
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.KODI_NOT_READY,
            )
        container = matches[0]
        if container.runtime_state is ContainerRuntimeState.STOPPED:
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.KODI_NOT_RUNNING,
            )
        if (
            container.runtime_state is not ContainerRuntimeState.RUNNING
            or container.health_state is not ContainerHealthState.HEALTHY
        ):
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.KODI_NOT_READY,
            )
        try:
            observation = self._broker.observe_managed_kodi_runtime(
                managed_instance_id=str(instance_id),
                expected_container_id=container.container_id,
            )
        except ClientError:
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.KODI_NOT_READY,
            )
        if (
            observation.managed_instance_id != str(instance_id)
            or observation.runtime_revision_id != str(instance.runtime_revision_id)
            or observation.container_id != container.container_id
            or observation.jsonrpc_port != profile.jsonrpc_port
            or observation.runtime_state is not ContainerRuntimeState.RUNNING
        ):
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.KODI_NOT_READY,
            )
        if observation.process_state is KodiProcessState.NOT_RUNNING:
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.KODI_NOT_RUNNING,
            )
        if observation.process_state is not KodiProcessState.RUNNING:
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.KODI_NOT_READY,
            )

        try:
            result = self._adapter_factory(observation, profile).get_inventory()
        except ValueError:
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.KODI_NOT_READY,
            )
        except KodiAddonInventoryIdentityError:
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.KODI_NOT_READY,
            )
        except JsonRpcUnavailableError:
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.JSONRPC_UNAVAILABLE,
            )
        except KodiAddonInventoryUnsupportedResponseError:
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.UNSUPPORTED_RESPONSE,
            )
        except KodiAddonInventoryMalformedDataError:
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.MALFORMED_ADDON_DATA,
            )
        except KodiAddonInventoryLimitExceededError:
            return self._unavailable(
                instance_id,
                kodi_release,
                AddonInventoryUnavailableReason.INVENTORY_LIMIT_EXCEEDED,
            )

        with self._session_factory() as session:
            evidence = DevelopmentSourceDeploymentRepository(session).list_for_instance(
                instance_id
            )
            provenances = {
                item.addon_id: self._provenance(
                    session,
                    instance_id,
                    instance.runtime_revision_id,
                    observation.container_id,
                    item.addon_id,
                    item.version,
                    evidence,
                )
                for item in result.items
            }
        return ManagedInstanceAddonInventory(
            instance_id=instance_id,
            observed_at=self._now(),
            kodi_release=result.identity.kodi_release.canonical,
            query_status=AddonInventoryQueryStatus.AVAILABLE,
            unavailable_reason=None,
            complete=True,
            items=[
                KodiAddonInventoryItem(
                    addon_id=item.addon_id,
                    name=item.name,
                    version=item.version,
                    enabled=item.enabled,
                    kodi_type=item.kodi_type,
                    provenance=provenances[item.addon_id],
                )
                for item in result.items
            ],
        )

    def _unavailable(
        self,
        instance_id: UUID,
        kodi_release: str,
        reason: AddonInventoryUnavailableReason,
    ) -> ManagedInstanceAddonInventory:
        return ManagedInstanceAddonInventory(
            instance_id=instance_id,
            observed_at=self._now(),
            kodi_release=kodi_release,
            query_status=AddonInventoryQueryStatus.UNAVAILABLE,
            unavailable_reason=reason,
            complete=False,
            items=[],
        )

    @staticmethod
    def _provenance(
        session: Session,
        instance_id: UUID,
        runtime_revision_id: UUID,
        container_id: str,
        addon_id: str,
        version: str,
        evidence: list[DevelopmentSourceDeployment],
    ) -> KodiAddonProvenance:
        candidates = [item for item in evidence if item.addon_id == addon_id]
        verified: list[tuple[DevelopmentSourceDeployment, str]] = []
        historical: list[tuple[DevelopmentSourceDeployment, str]] = []
        sources = DevelopmentSourceRepository(session)
        operations = DevelopmentSourceDeploymentOperationRepository(session)
        for item in candidates:
            source = sources.get(item.development_source_id)
            if source is None:
                continue
            historical.append((item, source.logical_name))
            operation = operations.get(item.last_successful_operation_id)
            if (
                item.confidence
                is DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
                and item.managed_instance_id == instance_id
                and item.runtime_revision_id == runtime_revision_id
                and item.trusted_container_id == container_id
                and item.observed_source_version == version
                and operation is not None
                and source.addon_id == addon_id
                and operation.state
                is DevelopmentSourceDeploymentOperationState.SUCCEEDED
                and operation.original_managed_instance_id == instance_id
                and operation.managed_instance_id == instance_id
                and operation.development_source_id == item.development_source_id
                and operation.addon_id == addon_id
                and operation.runtime_revision_id == runtime_revision_id
                and operation.expected_container_id == container_id
                and operation.result_classification is not None
                and operation.completed_at is not None
            ):
                verified.append((item, source.logical_name))
        if len(verified) == 1:
            item, logical_name = verified[0]
            return KodiAddonProvenance(
                classification=(
                    KodiAddonProvenanceClassification.DEVELOPMENT_SOURCE_AT_LAST_VERIFIED_COPY
                ),
                evidence_status=(
                    KodiAddonProvenanceEvidenceStatus.LAST_OPERATION_VERIFIED
                ),
                development_source_id=item.development_source_id,
                development_source_logical_name=logical_name,
                verified_at=item.verified_at,
            )
        if len(historical) == 1:
            item, logical_name = historical[0]
            if item.confidence is DevelopmentSourceDeploymentConfidence.UNKNOWN:
                return KodiAddonProvenance(
                    classification=KodiAddonProvenanceClassification.UNKNOWN,
                    evidence_status=KodiAddonProvenanceEvidenceStatus.UNKNOWN,
                )
            return KodiAddonProvenance(
                classification=KodiAddonProvenanceClassification.UNKNOWN,
                evidence_status=KodiAddonProvenanceEvidenceStatus.STALE,
                development_source_id=item.development_source_id,
                development_source_logical_name=logical_name,
                verified_at=item.verified_at,
            )
        return KodiAddonProvenance(
            classification=KodiAddonProvenanceClassification.UNKNOWN,
            evidence_status=KodiAddonProvenanceEvidenceStatus.UNKNOWN,
        )
