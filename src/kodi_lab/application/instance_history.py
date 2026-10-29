"""Narrow live-instance detail and deleted-instance history projections."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.activity import (
    ManagedInstanceActivityItem,
    ManagedInstanceActivityService,
)
from kodi_lab.application.addon_inventory import (
    KodiAddonProvenanceClassification,
    ManagedInstanceAddonInventory,
    ManagedInstanceAddonInventoryService,
)
from kodi_lab.application.development_source_attachments import (
    AttachedDevelopmentSourceRead,
)
from kodi_lab.application.development_source_presentation import (
    DevelopmentSourcePresentationService,
)
from kodi_lab.application.development_sources import DevelopmentSourceRead
from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationService,
    ManagedInstanceReconciliationStatus,
)
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatus,
    ManagedRuntimeStatusService,
)
from kodi_lab.application.snapshot_status import (
    SnapshotHistoryItem,
    SnapshotStatusService,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.domain import (
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceEnablementResult,
    DevelopmentSourceSmokeTestOperationState,
    DevelopmentSourceSmokeTestOverallResult,
    InstanceType,
    ManagedInstanceDeleteOperation,
    PythonBytecodePolicy,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceEnablementOperationRepository,
    DevelopmentSourceSmokeTestOperationRepository,
    FixtureDefinitionRepository,
    ManagedInstanceDeleteOperationRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
)

_COPIED_SOURCE_PROVENANCE = (
    KodiAddonProvenanceClassification.DEVELOPMENT_SOURCE_AT_LAST_VERIFIED_COPY
)


class InstanceFixtureDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_definition_id: UUID
    slug: str = Field(max_length=64)
    fixture_version: int
    size_category: str = Field(max_length=16)
    content_category: str = Field(max_length=16)


class ManagedInstanceDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: ManagedRuntimeStatus
    instance_type: InstanceType
    notes: str | None = Field(default=None, max_length=4096)
    fixture: InstanceFixtureDetail | None
    snapshots: list[SnapshotHistoryItem]
    development_sources: list[AttachedDevelopmentSourceRead]
    attachable_development_sources: list[DevelopmentSourceRead]
    development_source_attachment_mutation_eligible: bool
    addons: ManagedInstanceAddonInventory
    recent_activity: list[ManagedInstanceActivityItem] = Field(max_length=10)
    reconciliation: ManagedInstanceReconciliationStatus
    mutation_active: bool = False


class DeletedManagedInstanceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_managed_instance_id: UUID
    historical_name: str = Field(max_length=256)
    deletion_operation_uuid: UUID
    deletion_time: datetime | None
    operation_state: str = Field(max_length=32)
    kodi_release: str = Field(max_length=32)
    runtime_revision_id: UUID
    runtime_revision_number: int
    fixture_definition_id: UUID | None
    fixture_slug: str | None = Field(default=None, max_length=64)
    preserve_snapshots: bool
    reconciliation_required: bool


class DeletedManagedInstanceHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: DeletedManagedInstanceSummary
    activity: list[ManagedInstanceActivityItem]
    preserved_snapshots: list[SnapshotHistoryItem]


class DeletedManagedInstanceHistoryNotFoundError(LookupError):
    pass


class ManagedInstanceDetailService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        status: ManagedRuntimeStatusService,
        snapshots: SnapshotStatusService,
        activity: ManagedInstanceActivityService,
        reconciliation: ManagedInstanceReconciliationService,
        development_sources: DevelopmentSourcePresentationService,
        addons: ManagedInstanceAddonInventoryService,
    ) -> None:
        self._session_factory = session_factory
        self._status = status
        self._snapshots = snapshots
        self._activity = activity
        self._reconciliation = reconciliation
        self._development_sources = development_sources
        self._addons = addons

    def get(self, instance_id: UUID) -> ManagedInstanceDetail:
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
            if instance is None:
                raise ManagedInstanceNotFoundError(instance_id)
            fixture = (
                None
                if instance.fixture_definition_id is None
                else FixtureDefinitionRepository(session).get(
                    instance.fixture_definition_id
                )
            )
            profile = RuntimeExecutionProfileRepository(session).get(
                instance.runtime_revision_id
            )
            enablement_history = DevelopmentSourceEnablementOperationRepository(
                session
            ).list_for_original_instance(instance_id)
            smoke_history = DevelopmentSourceSmokeTestOperationRepository(
                session
            ).list_for_original_instance(instance_id)
        runtime = self._status.get(instance_id)
        reconciliation = self._reconciliation.status(instance_id)
        sources = self._development_sources.list_attached_sources(instance_id)
        attachable_sources = self._development_sources.list_attachable_sources(
            instance_id
        )
        with self._session_factory() as session:
            active_mutation = (
                ManagedInstanceMutationLeaseRepository(session).current(instance_id)
                is not None
            )
        eligible = (
            instance.instance_type is InstanceType.DISPOSABLE
            and runtime.provision_state.value == "provisioned"
            and runtime.container_state is ContainerRuntimeState.STOPPED
            and not reconciliation.blocking
            and not active_mutation
        )
        addons = self._addons.get(instance_id)
        addons = addons.model_copy(
            update={
                "installed_zip_artifacts": [
                    item.model_copy(
                        update={
                            "enable_eligible": (
                                item.enable_eligible
                                and not reconciliation.blocking
                                and not active_mutation
                            ),
                            "smoke_test_eligible": (
                                item.smoke_test_eligible
                                and not reconciliation.blocking
                                and not active_mutation
                            ),
                        }
                    )
                    for item in addons.installed_zip_artifacts
                ]
            }
        )
        addon_by_id = {item.addon_id: item for item in addons.items}
        presented_sources: list[AttachedDevelopmentSourceRead] = []
        for source in sources:
            state = "Not copied"
            enable_eligible = False
            blocked_dependency_id = None
            blocked_reason = None
            if (
                source.deployment_confidence
                is DevelopmentSourceDeploymentConfidence.STALE
            ):
                state = "Provenance stale"
            elif (
                source.deployment_confidence
                is DevelopmentSourceDeploymentConfidence.UNKNOWN
            ):
                state = "Provenance unknown"
            elif (
                source.deployment_confidence
                is DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
            ):
                if runtime.container_state is not ContainerRuntimeState.RUNNING:
                    state = "Copied, Kodi stopped"
                elif addons.query_status.value != "available":
                    state = "Copied, Kodi not ready"
                else:
                    addon = addon_by_id.get(source.addon_id)
                    if addon is None:
                        state = "Copied, not recognized"
                    elif addon.enabled:
                        state = "Recognized, enabled"
                    elif addon.provenance.classification is _COPIED_SOURCE_PROVENANCE:
                        state = "Recognized, disabled"
                        enable_eligible = True
                    else:
                        state = "Provenance unknown"
            service_declared = "xbmc.service" in source.extension_types
            pure_service = tuple(
                point
                for point in source.extension_types
                if point != "xbmc.addon.metadata"
            ) == ("xbmc.service",)
            if service_declared and not pure_service:
                state = "Unsupported mixed service extensions"
                enable_eligible = False
            elif service_declared and (
                profile is None
                or profile.expected_kodi_version != "21.3"
                or profile.expected_version_tag != "stable"
                or profile.runtime_uid == 0
                or profile.runtime_gid == 0
                or instance.instance_type.value != "disposable"
            ):
                state = "Service startup is unqualified for this runtime"
                enable_eligible = False
            elif (
                service_declared
                or any(
                    point.startswith("xbmc.python.") for point in source.extension_types
                )
            ) and (
                profile is None
                or profile.python_bytecode_policy is not PythonBytecodePolicy.SUPPRESS
            ):
                state = "Runtime bytecode policy incompatible"
                enable_eligible = False
            latest = next(
                (
                    item
                    for item in enablement_history
                    if item.development_source_id == source.development_source_id
                ),
                None,
            )
            if (
                enable_eligible
                and latest is not None
                and latest.result_classification
                is DevelopmentSourceEnablementResult.DEPENDENCY_BLOCKED
            ):
                # Historical failure is advisory. A fresh Enable reruns every
                # preflight after dependencies have been installed or enabled.
                state = "Dependency blocked at last attempt"
                blocked_dependency_id = latest.blocked_dependency_id
                blocked_reason = latest.blocked_dependency_reason
            latest_smoke = next(
                (
                    item
                    for item in smoke_history
                    if item.development_source_id == source.development_source_id
                ),
                None,
            )
            smoke_state = "Not run"
            if latest_smoke is not None:
                if (
                    latest_smoke.state
                    is DevelopmentSourceSmokeTestOperationState.QUEUED
                ):
                    smoke_state = "Queued"
                elif (
                    latest_smoke.state
                    is DevelopmentSourceSmokeTestOperationState.RUNNING
                ):
                    smoke_state = "Running"
                else:
                    smoke_state = {
                        DevelopmentSourceSmokeTestOverallResult.PASSED: "Smoke passed",
                        DevelopmentSourceSmokeTestOverallResult.EXECUTION_FAILED: (
                            "Execution failed"
                        ),
                        DevelopmentSourceSmokeTestOverallResult.TIMEOUT: "Timed out",
                        DevelopmentSourceSmokeTestOverallResult.DEPENDENCY_BLOCKED: (
                            "Dependency blocked"
                        ),
                        DevelopmentSourceSmokeTestOverallResult.SOURCE_TREE_CHANGED: (
                            "Source tree changed"
                        ),
                        (
                            DevelopmentSourceSmokeTestOverallResult.UNSUPPORTED_ADDON_TYPE
                        ): ("Unsupported add-on type"),
                        (
                            DevelopmentSourceSmokeTestOverallResult.RECONCILIATION_REQUIRED
                        ): ("Reconciliation required"),
                        None: "Precondition failed",
                    }[latest_smoke.overall_result]
            advisory_descriptor = frozenset(source.extension_types) in {
                frozenset({"xbmc.python.pluginsource"}),
                frozenset({"xbmc.python.pluginsource", "xbmc.addon.metadata"}),
            }
            addon = addon_by_id.get(source.addon_id)
            smoke_eligible = (
                advisory_descriptor
                and source.deployment_confidence
                is DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION
                and profile is not None
                and profile.python_bytecode_policy is PythonBytecodePolicy.SUPPRESS
                and runtime.container_state is ContainerRuntimeState.RUNNING
                and addons.query_status.value == "available"
                and addon is not None
                and addon.enabled
                and addon.kodi_type == "xbmc.python.pluginsource"
                and addon.provenance.classification is _COPIED_SOURCE_PROVENANCE
                and not reconciliation.blocking
                and not active_mutation
            )
            presented_sources.append(
                source.model_copy(
                    update={
                        "copy_sync_eligible": eligible,
                        "enablement_state": state,
                        "enable_eligible": (
                            enable_eligible
                            and not reconciliation.blocking
                            and not active_mutation
                        ),
                        "service_start_requires_approval": pure_service,
                        "enablement_blocked_dependency_id": blocked_dependency_id,
                        "enablement_blocked_reason": blocked_reason,
                        "smoke_test_state": smoke_state,
                        "smoke_test_eligible": smoke_eligible,
                    }
                )
            )
        return ManagedInstanceDetail(
            runtime=runtime,
            instance_type=instance.instance_type,
            notes=instance.notes,
            fixture=(
                None
                if fixture is None
                else InstanceFixtureDetail(
                    fixture_definition_id=fixture.id,
                    slug=fixture.slug,
                    fixture_version=fixture.fixture_version,
                    size_category=fixture.size_category.value,
                    content_category=fixture.content_category.value,
                )
            ),
            snapshots=self._snapshots.list_for_instance(instance_id),
            development_sources=presented_sources,
            attachable_development_sources=(
                attachable_sources
                if not reconciliation.blocking and not active_mutation
                else []
            ),
            development_source_attachment_mutation_eligible=(
                instance.instance_type is InstanceType.DISPOSABLE
                and not reconciliation.blocking
                and not active_mutation
            ),
            addons=addons,
            recent_activity=self._activity.list_for_live_instance(
                instance_id, limit=10
            ),
            reconciliation=reconciliation,
            mutation_active=active_mutation,
        )


class DeletedManagedInstanceHistoryService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        activity: ManagedInstanceActivityService,
        snapshots: SnapshotStatusService,
    ) -> None:
        self._session_factory = session_factory
        self._activity = activity
        self._snapshots = snapshots

    def list(self) -> list[DeletedManagedInstanceSummary]:
        with self._session_factory() as session:
            latest: dict[UUID, ManagedInstanceDeleteOperation] = {}
            for operation in ManagedInstanceDeleteOperationRepository(
                session
            ).list_all():
                if operation.original_managed_instance_id in latest:
                    continue
                if (
                    ManagedInstanceRepository(session).get(
                        operation.original_managed_instance_id
                    )
                    is not None
                ):
                    continue
                latest[operation.original_managed_instance_id] = operation
            return [
                summary
                for operation in latest.values()
                if (summary := self._summary(session, operation)) is not None
            ]

    def get(self, instance_id: UUID) -> DeletedManagedInstanceHistory:
        with self._session_factory() as session:
            if ManagedInstanceRepository(session).get(instance_id) is not None:
                raise DeletedManagedInstanceHistoryNotFoundError(instance_id)
            operations = ManagedInstanceDeleteOperationRepository(
                session
            ).list_for_original_instance(instance_id)
            if not operations:
                raise DeletedManagedInstanceHistoryNotFoundError(instance_id)
            summary = self._summary(session, operations[0])
            if summary is None:
                raise DeletedManagedInstanceHistoryNotFoundError(instance_id)
        preserved = [
            item
            for item in self._snapshots.list_preserved()
            if item.original_source_managed_instance_id == instance_id
        ]
        return DeletedManagedInstanceHistory(
            summary=summary,
            activity=self._activity.list_for_original_instance(instance_id),
            preserved_snapshots=preserved,
        )

    @staticmethod
    def _summary(
        session: Session, operation: ManagedInstanceDeleteOperation
    ) -> DeletedManagedInstanceSummary | None:
        revision = RuntimeRevisionRepository(session).get(operation.runtime_revision_id)
        if revision is None:
            return None
        definition = RuntimeDefinitionRepository(session).get(
            revision.runtime_definition_id
        )
        if definition is None:
            return None
        fixture = (
            None
            if operation.fixture_definition_id is None
            else FixtureDefinitionRepository(session).get(
                operation.fixture_definition_id
            )
        )
        return DeletedManagedInstanceSummary(
            original_managed_instance_id=operation.original_managed_instance_id,
            historical_name=operation.source_instance_name,
            deletion_operation_uuid=operation.id,
            deletion_time=operation.completed_at,
            operation_state=operation.state.value,
            kodi_release=definition.kodi_version,
            runtime_revision_id=revision.id,
            runtime_revision_number=revision.revision_number,
            fixture_definition_id=operation.fixture_definition_id,
            fixture_slug=None if fixture is None else fixture.slug,
            preserve_snapshots=operation.preserve_snapshots,
            reconciliation_required=operation.reconciliation_required,
        )
