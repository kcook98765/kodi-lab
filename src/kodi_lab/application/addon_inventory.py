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
from kodi_lab.domain import (
    AddonZipOrigin,
    AddonZipSmokeTestOperationState,
    AddonZipSmokeTestOverallResult,
    RuntimeExecutionProfile,
)
from kodi_lab.domain.addon_zips import (
    AddonZipEnablementOperationState,
    AddonZipEnablementResult,
    AddonZipInstallationConfidence,
    AddonZipInstallationEvidence,
    AddonZipInstallationOperationState,
    AddonZipInstallationResult,
)
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
    AddonRepositoryMetadataRepository,
    AddonRepositorySourceRepository,
    AddonZipArtifactRepository,
    AddonZipEnablementOperationRepository,
    AddonZipInstallationEvidenceRepository,
    AddonZipInstallationOperationRepository,
    AddonZipSmokeTestOperationRepository,
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
    PRIOR_VERIFIED_ZIP_INSTALL = "prior_verified_zip_install"
    PRIOR_VERIFIED_REPOSITORY_INSTALL = "prior_verified_repository_install"
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
    addon_zip_artifact_id: UUID | None = None
    addon_zip_logical_label: str | None = Field(default=None, max_length=256)
    repository_source_id: UUID | None = None
    repository_logical_name: str | None = Field(default=None, max_length=120)
    verified_at: datetime | None = None


class KodiAddonInventoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addon_id: str = Field(max_length=128)
    name: str = Field(max_length=256)
    version: str = Field(max_length=128)
    enabled: StrictBool
    kodi_type: str = Field(max_length=128)
    provenance: KodiAddonProvenance


class AddonZipEnablementPresentationState(StrEnum):
    INSTALLED_DISABLED = "installed_disabled"
    INSTALLED_ENABLED = "installed_enabled"
    EVIDENCE_STALE = "installation_evidence_stale"
    EVIDENCE_UNKNOWN = "installation_evidence_unknown"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    RECONCILIATION_REQUIRED = "enablement_requires_reconciliation"


class InstalledAddonZipPresentation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: UUID
    logical_label: str = Field(max_length=256)
    addon_id: str = Field(max_length=128)
    addon_version: str = Field(max_length=128)
    provenance_wording: str = Field(max_length=160)
    live_enabled: StrictBool | None = None
    state: AddonZipEnablementPresentationState
    enable_eligible: StrictBool
    smoke_test_state: str = Field(max_length=64)
    smoke_test_eligible: StrictBool
    blocked_dependency_id: str | None = Field(default=None, max_length=128)
    blocked_dependency_reason: str | None = Field(default=None, max_length=64)


class ManagedInstanceAddonInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: UUID
    observed_at: datetime
    kodi_release: str = Field(max_length=32)
    query_status: AddonInventoryQueryStatus
    unavailable_reason: AddonInventoryUnavailableReason | None = None
    complete: StrictBool
    items: list[KodiAddonInventoryItem] = Field(max_length=2_048)
    installed_zip_artifacts: list[InstalledAddonZipPresentation] = Field(
        default_factory=list, max_length=2_048
    )


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
            zip_evidence = AddonZipInstallationEvidenceRepository(
                session
            ).list_for_instance(instance_id)
            provenances = {
                item.addon_id: self._provenance(
                    session,
                    instance_id,
                    instance.runtime_revision_id,
                    observation.container_id,
                    item.addon_id,
                    item.version,
                    evidence,
                    zip_evidence,
                )
                for item in result.items
            }
            zip_presentations = self._zip_presentations(
                session,
                instance_id,
                instance.runtime_revision_id,
                observation.container_id,
                result,
                zip_evidence,
                profile,
            )
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
            installed_zip_artifacts=zip_presentations,
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
            installed_zip_artifacts=self._unavailable_zip_presentations(instance_id),
        )

    def _unavailable_zip_presentations(
        self, instance_id: UUID
    ) -> list[InstalledAddonZipPresentation]:
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            if instance is None:
                return []
            profile = RuntimeExecutionProfileRepository(session).get(
                instance.runtime_revision_id
            )
            return self._zip_presentations(
                session,
                instance_id,
                instance.runtime_revision_id,
                None,
                None,
                AddonZipInstallationEvidenceRepository(session).list_for_instance(
                    instance_id
                ),
                profile,
            )

    @staticmethod
    def _zip_presentations(
        session: Session,
        instance_id: UUID,
        runtime_revision_id: UUID,
        container_id: str | None,
        inventory: KodiAddonInventoryResult | None,
        evidence: list[AddonZipInstallationEvidence],
        profile: RuntimeExecutionProfile | None,
    ) -> list[InstalledAddonZipPresentation]:
        artifacts = AddonZipArtifactRepository(session)
        repository_sources = AddonRepositorySourceRepository(session)
        operations = AddonZipEnablementOperationRepository(
            session
        ).list_for_original_instance(instance_id)
        smoke_operations = AddonZipSmokeTestOperationRepository(
            session
        ).list_for_original_instance(instance_id)
        live_by_id = (
            {}
            if inventory is None
            else {item.addon_id: item for item in inventory.items}
        )
        rows: list[InstalledAddonZipPresentation] = []
        for item in evidence:
            artifact = artifacts.get(item.artifact_id)
            if artifact is None:
                continue
            repository_source = (
                None
                if artifact.repository_source_id is None
                else repository_sources.get(artifact.repository_source_id)
            )
            latest = next(
                (
                    operation
                    for operation in operations
                    if operation.artifact_id == item.artifact_id
                ),
                None,
            )
            live = live_by_id.get(item.addon_id)
            latest_smoke = next(
                (
                    operation
                    for operation in smoke_operations
                    if operation.artifact_id == item.artifact_id
                ),
                None,
            )
            smoke_state = "Not run"
            if latest_smoke is not None:
                if latest_smoke.state is AddonZipSmokeTestOperationState.QUEUED:
                    smoke_state = "Queued"
                elif latest_smoke.state is AddonZipSmokeTestOperationState.RUNNING:
                    smoke_state = "Running"
                else:
                    smoke_state = {
                        AddonZipSmokeTestOverallResult.PASSED: "Smoke passed",
                        AddonZipSmokeTestOverallResult.EXECUTION_FAILED: (
                            "Execution failed"
                        ),
                        AddonZipSmokeTestOverallResult.TIMEOUT: "Timed out",
                        AddonZipSmokeTestOverallResult.DEPENDENCY_BLOCKED: (
                            "Dependency blocked"
                        ),
                        AddonZipSmokeTestOverallResult.INSTALLED_TREE_CHANGED: (
                            "Installed tree changed"
                        ),
                        AddonZipSmokeTestOverallResult.UNSUPPORTED_ADDON_TYPE: (
                            "Unsupported add-on type"
                        ),
                        AddonZipSmokeTestOverallResult.RECONCILIATION_REQUIRED: (
                            "Reconciliation required"
                        ),
                        None: {
                            "zip_installation_evidence_stale": (
                                "Installation evidence stale"
                            ),
                            "zip_installation_evidence_unknown": (
                                "Installation evidence unknown"
                            ),
                        }.get(latest_smoke.failure_reason or "", "Precondition failed"),
                    }[latest_smoke.overall_result]
            exact_lineage = (
                item.managed_instance_id == instance_id
                and item.original_managed_instance_id == instance_id
                and item.runtime_revision_id == runtime_revision_id
                and container_id is not None
                and item.trusted_container_id == container_id
            )
            state = AddonZipEnablementPresentationState.EVIDENCE_UNKNOWN
            if (
                latest is not None
                and latest.state
                is AddonZipEnablementOperationState.RECONCILIATION_REQUIRED
            ):
                state = AddonZipEnablementPresentationState.RECONCILIATION_REQUIRED
            elif item.confidence is AddonZipInstallationConfidence.STALE:
                state = AddonZipEnablementPresentationState.EVIDENCE_STALE
            elif item.confidence is AddonZipInstallationConfidence.UNKNOWN:
                state = AddonZipEnablementPresentationState.EVIDENCE_UNKNOWN
            elif (
                latest is not None
                and latest.result_classification
                is AddonZipEnablementResult.DEPENDENCY_BLOCKED
            ):
                state = AddonZipEnablementPresentationState.DEPENDENCY_BLOCKED
            elif (
                exact_lineage
                and live is not None
                and live.version == item.addon_version
            ):
                state = (
                    AddonZipEnablementPresentationState.INSTALLED_ENABLED
                    if live.enabled
                    else AddonZipEnablementPresentationState.INSTALLED_DISABLED
                )
            rows.append(
                InstalledAddonZipPresentation(
                    artifact_id=item.artifact_id,
                    logical_label=artifact.logical_label,
                    addon_id=item.addon_id,
                    addon_version=item.addon_version,
                    provenance_wording=(
                        (
                            "Prior verified ZIP install"
                            if artifact.origin is AddonZipOrigin.UPLOADED_ZIP
                            else (
                                "Prior verified repository install — "
                                + repository_source.logical_name
                                if repository_source is not None
                                else "Repository installation evidence unknown"
                            )
                        )
                        if item.confidence
                        is AddonZipInstallationConfidence.VERIFIED_AT_LAST_OPERATION
                        and exact_lineage
                        else (
                            "Installation evidence stale"
                            if item.confidence is AddonZipInstallationConfidence.STALE
                            else "Installation evidence unknown"
                        )
                    ),
                    live_enabled=None if live is None else live.enabled,
                    state=state,
                    enable_eligible=(
                        artifact.origin is AddonZipOrigin.UPLOADED_ZIP
                        and state
                        is AddonZipEnablementPresentationState.INSTALLED_DISABLED
                        and exact_lineage
                        and profile is not None
                        and profile.python_bytecode_policy.value == "suppress"
                    ),
                    smoke_test_state=smoke_state,
                    smoke_test_eligible=(
                        artifact.origin is AddonZipOrigin.UPLOADED_ZIP
                        and item.confidence
                        is AddonZipInstallationConfidence.VERIFIED_AT_LAST_OPERATION
                        and exact_lineage
                        and live is not None
                        and live.version == item.addon_version
                        and live.enabled
                        and live.kodi_type == "xbmc.python.pluginsource"
                        and profile is not None
                        and profile.python_bytecode_policy.value == "suppress"
                    ),
                    blocked_dependency_id=(
                        None if latest is None else latest.blocked_dependency_id
                    ),
                    blocked_dependency_reason=(
                        None if latest is None else latest.blocked_dependency_reason
                    ),
                )
            )
        return rows

    @staticmethod
    def _provenance(
        session: Session,
        instance_id: UUID,
        runtime_revision_id: UUID,
        container_id: str,
        addon_id: str,
        version: str,
        evidence: list[DevelopmentSourceDeployment],
        zip_evidence: list[AddonZipInstallationEvidence],
    ) -> KodiAddonProvenance:
        zip_candidates = [item for item in zip_evidence if item.addon_id == addon_id]
        zip_operations = AddonZipInstallationOperationRepository(session)
        zip_artifacts = AddonZipArtifactRepository(session)
        repository_sources = AddonRepositorySourceRepository(session)
        repository_metadata = AddonRepositoryMetadataRepository(session)
        verified_zip: list[
            tuple[AddonZipInstallationEvidence, str, AddonZipOrigin, UUID | None]
        ] = []
        for zip_item in zip_candidates:
            zip_operation = zip_operations.get(zip_item.installation_operation_id)
            artifact = zip_artifacts.get(zip_item.artifact_id)
            repository_candidate = (
                None
                if artifact is None or artifact.repository_candidate_id is None
                else repository_metadata.get_candidate(artifact.repository_candidate_id)
            )
            if (
                zip_item.confidence
                is AddonZipInstallationConfidence.VERIFIED_AT_LAST_OPERATION
                and zip_item.result
                is AddonZipInstallationResult.INSTALLED_RECOGNIZED_DISABLED
                and zip_item.managed_instance_id == instance_id
                and zip_item.original_managed_instance_id == instance_id
                and zip_item.runtime_revision_id == runtime_revision_id
                and zip_item.trusted_container_id == container_id
                and zip_item.addon_version == version
                and zip_operation is not None
                and artifact is not None
                and zip_operation.state is AddonZipInstallationOperationState.SUCCEEDED
                and zip_operation.original_managed_instance_id == instance_id
                and zip_operation.managed_instance_id == instance_id
                and zip_operation.artifact_id == zip_item.artifact_id
                and zip_operation.addon_id == addon_id
                and zip_operation.addon_version == version
                and zip_operation.runtime_revision_id == runtime_revision_id
                and zip_operation.expected_container_id == container_id
                and zip_operation.stopped_readback_matched
                and zip_operation.final_readback_matched
                and zip_operation.recognition_enabled is False
                and artifact.addon_id == addon_id
                and artifact.addon_version == version
                and artifact.payload_manifest_digest == zip_item.payload_manifest_digest
                and artifact.origin is zip_item.artifact_origin
                and artifact.repository_source_id == zip_item.repository_source_id
                and artifact.repository_snapshot_id == zip_item.repository_snapshot_id
                and artifact.repository_candidate_id == zip_item.repository_candidate_id
                and (
                    artifact.origin is AddonZipOrigin.UPLOADED_ZIP
                    or (
                        repository_candidate is not None
                        and repository_candidate.repository_source_id
                        == artifact.repository_source_id
                        and repository_candidate.snapshot_id
                        == artifact.repository_snapshot_id
                        and repository_candidate.addon_id == addon_id
                        and repository_candidate.version == version
                    )
                )
            ):
                verified_zip.append(
                    (
                        zip_item,
                        artifact.logical_label,
                        artifact.origin,
                        artifact.repository_source_id,
                    )
                )
        if len(verified_zip) == 1:
            zip_item, logical_label, origin, repository_source_id = verified_zip[0]
            repository_source = (
                None
                if repository_source_id is None
                else repository_sources.get(repository_source_id)
            )
            return KodiAddonProvenance(
                classification=(
                    KodiAddonProvenanceClassification.PRIOR_VERIFIED_ZIP_INSTALL
                    if origin is AddonZipOrigin.UPLOADED_ZIP
                    else (
                        KodiAddonProvenanceClassification.PRIOR_VERIFIED_REPOSITORY_INSTALL
                    )
                ),
                evidence_status=(
                    KodiAddonProvenanceEvidenceStatus.LAST_OPERATION_VERIFIED
                ),
                addon_zip_artifact_id=zip_item.artifact_id,
                addon_zip_logical_label=logical_label,
                repository_source_id=repository_source_id,
                repository_logical_name=(
                    None
                    if repository_source is None
                    else repository_source.logical_name
                ),
                verified_at=zip_item.verified_at,
            )
        candidates = [item for item in evidence if item.addon_id == addon_id]
        verified: list[tuple[DevelopmentSourceDeployment, str]] = []
        historical: list[tuple[DevelopmentSourceDeployment, str]] = []
        sources = DevelopmentSourceRepository(session)
        operations = DevelopmentSourceDeploymentOperationRepository(session)
        for deployment_item in candidates:
            source = sources.get(deployment_item.development_source_id)
            if source is None:
                continue
            historical.append((deployment_item, source.logical_name))
            deployment_operation = operations.get(
                deployment_item.last_successful_operation_id
            )
            if (
                deployment_item.confidence
                is DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
                and deployment_item.managed_instance_id == instance_id
                and deployment_item.runtime_revision_id == runtime_revision_id
                and deployment_item.trusted_container_id == container_id
                and deployment_item.observed_source_version == version
                and deployment_operation is not None
                and source.addon_id == addon_id
                and deployment_operation.state
                is DevelopmentSourceDeploymentOperationState.SUCCEEDED
                and deployment_operation.original_managed_instance_id == instance_id
                and deployment_operation.managed_instance_id == instance_id
                and deployment_operation.development_source_id
                == deployment_item.development_source_id
                and deployment_operation.addon_id == addon_id
                and deployment_operation.runtime_revision_id == runtime_revision_id
                and deployment_operation.expected_container_id == container_id
                and deployment_operation.result_classification is not None
                and deployment_operation.completed_at is not None
            ):
                verified.append((deployment_item, source.logical_name))
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
        if zip_candidates:
            confidence = zip_candidates[0].confidence
            return KodiAddonProvenance(
                classification=KodiAddonProvenanceClassification.UNKNOWN,
                evidence_status=(
                    KodiAddonProvenanceEvidenceStatus.STALE
                    if confidence is AddonZipInstallationConfidence.STALE
                    else KodiAddonProvenanceEvidenceStatus.UNKNOWN
                ),
            )
        return KodiAddonProvenance(
            classification=KodiAddonProvenanceClassification.UNKNOWN,
            evidence_status=KodiAddonProvenanceEvidenceStatus.UNKNOWN,
        )
