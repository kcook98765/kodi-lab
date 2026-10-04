"""Application-owned semantic reset and fixture status projection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import ManagedInstanceNotFoundError
from kodi_lab.application.reset_activity import (
    ResetActivityRegistry,
    ResetActivitySnapshot,
    ResetActivityState,
    ResetOutcome,
)
from kodi_lab.application.reset_operations import (
    ResetOperationCoordinator,
    ResetOperationStatus,
)
from kodi_lab.application.runtime_status import ProvisionState
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.errors import ClientError
from kodi_lab.broker.operations import (
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
)
from kodi_lab.domain import (
    FixtureContentCategory,
    FixtureDefinition,
    FixtureSizeCategory,
    InstanceType,
    ManagedInstance,
    RuntimeDefinition,
    RuntimeRevision,
)
from kodi_lab.storage.repositories import (
    FixtureDefinitionRepository,
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
)


class ResetEligibility(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class ResetEligibilityReason(StrEnum):
    PERSISTENT_INSTANCE = "persistent_instance"
    FIXTURE_INCOMPATIBLE = "fixture_incompatible"
    TRANSITIONAL_CONTAINER_STATE = "transitional_container_state"
    BROKER_UNAVAILABLE = "broker_unavailable"
    AMBIGUOUS_CONTAINER_OWNERSHIP = "ambiguous_container_ownership"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    RESET_IN_PROGRESS = "reset_in_progress"
    CATALOG_UNAVAILABLE = "catalog_unavailable"
    RUNTIME_REVISION_MISMATCH = "runtime_revision_mismatch"


class FixtureAssignmentState(StrEnum):
    NONE = "none"
    ASSIGNED = "assigned"


class FixtureCompatibilityState(StrEnum):
    NONE_ASSIGNED = "none_assigned"
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


class FixtureResetStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignment: FixtureAssignmentState
    compatibility: FixtureCompatibilityState
    slug: str | None = Field(default=None, max_length=64)
    display_name: str | None = Field(default=None, max_length=128)
    fixture_version: int | None = Field(default=None, ge=1)
    size_category: FixtureSizeCategory | None = None
    content_category: FixtureContentCategory | None = None
    compatible_kodi_release: str | None = Field(default=None, max_length=32)
    baseline_note: str | None = Field(default=None, max_length=256)


class ManagedInstanceResetStatus(BaseModel):
    """Bounded status without container identity or infrastructure authority."""

    model_config = ConfigDict(extra="forbid")

    managed_instance_id: UUID
    eligibility: ResetEligibility
    eligibility_reason: ResetEligibilityReason | None
    eligibility_detail: str = Field(max_length=256)
    pinned_runtime_revision_id: UUID
    pinned_runtime_revision_number: int = Field(ge=1)
    pinned_kodi_release: str = Field(max_length=32)
    exact_pin_statement: str = Field(max_length=128)
    fixture: FixtureResetStatus
    provision_state: ProvisionState
    container_state: ContainerRuntimeState | None
    activity_state: ResetActivityState
    last_outcome: ResetOutcome
    failure_phase: str | None = Field(default=None, max_length=64)
    failure_reason: str | None = Field(default=None, max_length=256)
    reconciliation_required: bool
    operation: ResetOperationStatus | None = None


class _ResetStatusBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...


@dataclass(frozen=True, slots=True)
class _ResetContext:
    instance: ManagedInstance
    revision: RuntimeRevision
    definition: RuntimeDefinition
    fixture: FixtureDefinition | None
    fixture_missing: bool


class ResetStatusService:
    """Compose durable catalog, broker inventory, and ephemeral reset activity."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: _ResetStatusBroker,
        activity: ResetActivityRegistry,
        operations: ResetOperationCoordinator | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._activity = activity
        self._operations = operations

    def list_statuses(self) -> list[ManagedInstanceResetStatus]:
        contexts = self._load_contexts()
        try:
            inventory = self._broker.managed_container_inventory()
        except ClientError:
            return [
                self._build(context, None, broker_unavailable=True)
                for context in contexts
            ]
        grouped: dict[UUID, list[ManagedContainerInventoryEntry]] = {}
        for item in inventory.containers:
            grouped.setdefault(UUID(item.managed_instance_id), []).append(item)
        return [
            self._build(context, grouped.get(context.instance.target_id, []))
            for context in contexts
        ]

    def get(self, instance_id: UUID) -> ManagedInstanceResetStatus:
        for status in self.list_statuses():
            if status.managed_instance_id == instance_id:
                return status
        raise ManagedInstanceNotFoundError(instance_id)

    def _load_contexts(self) -> list[_ResetContext]:
        with self._session_factory() as session:
            revisions = RuntimeRevisionRepository(session)
            definitions = RuntimeDefinitionRepository(session)
            fixtures = FixtureDefinitionRepository(session)
            contexts: list[_ResetContext] = []
            for instance in ManagedInstanceRepository(session).list():
                revision = revisions.get(instance.runtime_revision_id)
                if revision is None:
                    continue
                definition = definitions.get(revision.runtime_definition_id)
                if definition is None:
                    continue
                fixture = (
                    None
                    if instance.fixture_definition_id is None
                    else fixtures.get(instance.fixture_definition_id)
                )
                contexts.append(
                    _ResetContext(
                        instance=instance,
                        revision=revision,
                        definition=definition,
                        fixture=fixture,
                        fixture_missing=(
                            instance.fixture_definition_id is not None
                            and fixture is None
                        ),
                    )
                )
            return contexts

    def _build(
        self,
        context: _ResetContext,
        containers: list[ManagedContainerInventoryEntry] | None,
        *,
        broker_unavailable: bool = False,
    ) -> ManagedInstanceResetStatus:
        activity = self._activity.snapshot(context.instance.target_id)
        operation = (
            None
            if self._operations is None
            else self._operations.latest_for_instance(context.instance.target_id)
        )
        if operation is not None and self._operations is not None:
            operation = self._operations.terminalize_expired(operation)
        operation_status = (
            None
            if operation is None or self._operations is None
            else self._operations.to_status(operation)
        )
        if operation is not None:
            activity_state = (
                ResetActivityState.SUCCEEDED
                if operation.state.value == "succeeded"
                else ResetActivityState.RECONCILIATION_REQUIRED
                if operation.reconciliation_required
                else ResetActivityState.FAILED
                if operation.state.value == "failed"
                else ResetActivityState(operation.phase.value)
            )
            activity = ResetActivitySnapshot(
                generation=activity.generation,
                state=activity_state,
                last_outcome=(
                    ResetOutcome.SUCCEEDED
                    if operation.state.value == "succeeded"
                    else ResetOutcome.RECONCILIATION_REQUIRED
                    if operation.reconciliation_required
                    else ResetOutcome.FAILED
                    if operation.state.value == "failed"
                    else ResetOutcome.NONE
                ),
                failure_phase=(
                    operation.phase.value if operation.failure_reason else None
                ),
                failure_reason=operation.failure_reason,
                reconciliation_required=operation.reconciliation_required,
            )
        fixture = self._fixture_status(context)
        provision, container = self._container_status(containers, broker_unavailable)
        eligibility, reason, detail = self._eligibility(
            context,
            fixture,
            activity,
            containers,
            broker_unavailable,
        )
        return ManagedInstanceResetStatus(
            managed_instance_id=context.instance.target_id,
            eligibility=eligibility,
            eligibility_reason=reason,
            eligibility_detail=detail,
            pinned_runtime_revision_id=context.revision.id,
            pinned_runtime_revision_number=context.revision.revision_number,
            pinned_kodi_release=context.definition.kodi_release.canonical,
            exact_pin_statement=(
                "Reset recreates this exact runtime revision; it does not upgrade Kodi."
            ),
            fixture=fixture,
            provision_state=provision,
            container_state=container,
            activity_state=activity.state,
            last_outcome=activity.last_outcome,
            failure_phase=activity.failure_phase,
            failure_reason=activity.failure_reason,
            reconciliation_required=activity.reconciliation_required,
            operation=operation_status,
        )

    @staticmethod
    def _container_status(
        containers: list[ManagedContainerInventoryEntry] | None,
        broker_unavailable: bool,
    ) -> tuple[ProvisionState, ContainerRuntimeState | None]:
        if broker_unavailable:
            return ProvisionState.UNKNOWN, ContainerRuntimeState.UNKNOWN
        assert containers is not None
        if not containers:
            return ProvisionState.UNPROVISIONED, None
        if len(containers) != 1:
            return ProvisionState.UNKNOWN, ContainerRuntimeState.UNKNOWN
        return ProvisionState.PROVISIONED, containers[0].runtime_state

    @staticmethod
    def _fixture_status(context: _ResetContext) -> FixtureResetStatus:
        fixture = context.fixture
        if fixture is None:
            if context.fixture_missing:
                return FixtureResetStatus(
                    assignment=FixtureAssignmentState.ASSIGNED,
                    compatibility=FixtureCompatibilityState.INCOMPATIBLE,
                    baseline_note="Assigned fixture catalog metadata is unavailable.",
                )
            return FixtureResetStatus(
                assignment=FixtureAssignmentState.NONE,
                compatibility=FixtureCompatibilityState.NONE_ASSIGNED,
            )
        compatible = fixture.compatible_kodi_release == context.definition.kodi_release
        note = {
            "small-mixed-v1": (
                "Historical v1 assignment; a populated live baseline was not proven."
            ),
            "small-mixed-v2": (
                "Populated Kodi 21.3 reset baseline proven for this fixture version."
            ),
            "small-mixed-v3": (
                "Kodi 20.5-specific populated reset baseline fixture version."
            ),
            "small-mixed-v4": (
                "Kodi 19.5-specific populated reset baseline fixture version."
            ),
            "small-mixed-v5": (
                "Kodi 22 Beta 2-specific populated reset baseline fixture version."
            ),
            "small-mixed-v6": (
                "Kodi 19.4-specific populated reset baseline fixture version."
            ),
            "small-movies-v1": ("Proven Small/Movies fixture for exact Kodi 21.3."),
            "small-movies-v2": ("Small/Movies fixture for exact Kodi 20.5."),
            "small-movies-v3": ("Small/Movies fixture for exact Kodi 19.5."),
            "small-movies-v4": ("Rich Small/Movies fixture for exact Kodi 20.5."),
            "small-movies-v5": (
                "Rich Small/Movies fixture with post-scan state for exact Kodi 20.5."
            ),
            "small-tv-v1": ("Small/TV fixture for exact Kodi 21.3."),
            "small-tv-v2": ("Small/TV fixture for exact Kodi 20.5."),
            "small-tv-v3": ("Small/TV fixture for exact Kodi 19.5."),
            "medium-mixed-v1": ("Medium/Mixed fixture for exact Kodi 21.3."),
            "cross-movies-matrix-v1": ("Cross-Version Movies for exact Kodi 19.4."),
            "cross-movies-nexus-v1": ("Cross-Version Movies for exact Kodi 20.5."),
            "cross-movies-omega-v1": ("Cross-Version Movies for exact Kodi 21.3."),
        }.get(fixture.slug)
        return FixtureResetStatus(
            assignment=FixtureAssignmentState.ASSIGNED,
            compatibility=(
                FixtureCompatibilityState.COMPATIBLE
                if compatible
                else FixtureCompatibilityState.INCOMPATIBLE
            ),
            slug=fixture.slug,
            display_name=(
                "Small Mixed"
                if fixture.slug.startswith("small-mixed-")
                else "Medium Mixed"
                if fixture.slug.startswith("medium-mixed-")
                else "Small Movies"
                if fixture.slug.startswith("small-movies-")
                else "Small TV"
                if fixture.slug.startswith("small-tv-")
                else "Cross-Version Movies"
                if fixture.slug.startswith("cross-movies-")
                else fixture.slug
            ),
            fixture_version=fixture.fixture_version,
            size_category=fixture.size_category,
            content_category=fixture.content_category,
            compatible_kodi_release=fixture.compatible_kodi_release.canonical,
            baseline_note=note,
        )

    @staticmethod
    def _eligibility(
        context: _ResetContext,
        fixture: FixtureResetStatus,
        activity: ResetActivitySnapshot,
        containers: list[ManagedContainerInventoryEntry] | None,
        broker_unavailable: bool,
    ) -> tuple[ResetEligibility, ResetEligibilityReason | None, str]:
        unsupported = ResetEligibility.UNSUPPORTED
        if context.instance.instance_type is InstanceType.PERSISTENT:
            return (
                unsupported,
                ResetEligibilityReason.PERSISTENT_INSTANCE,
                "Persistent managed instances cannot be reset.",
            )
        if context.fixture_missing:
            return (
                unsupported,
                ResetEligibilityReason.CATALOG_UNAVAILABLE,
                "Assigned fixture catalog metadata is unavailable.",
            )
        if fixture.compatibility is FixtureCompatibilityState.INCOMPATIBLE:
            return (
                unsupported,
                ResetEligibilityReason.FIXTURE_INCOMPATIBLE,
                "The assigned fixture is incompatible with the exact runtime pin.",
            )
        if activity.reconciliation_required:
            return (
                unsupported,
                ResetEligibilityReason.RECONCILIATION_REQUIRED,
                "Reconciliation is required before another reset is considered safe.",
            )
        if activity.active:
            return (
                unsupported,
                ResetEligibilityReason.RESET_IN_PROGRESS,
                "A reset is already in progress in this application process.",
            )
        if broker_unavailable:
            return (
                unsupported,
                ResetEligibilityReason.BROKER_UNAVAILABLE,
                "Managed container state is temporarily unavailable.",
            )
        assert containers is not None
        if len(containers) > 1:
            return (
                unsupported,
                ResetEligibilityReason.AMBIGUOUS_CONTAINER_OWNERSHIP,
                "Managed container ownership is ambiguous.",
            )
        if containers and containers[0].runtime_revision_id != str(
            context.instance.runtime_revision_id
        ):
            return (
                unsupported,
                ResetEligibilityReason.RUNTIME_REVISION_MISMATCH,
                "Managed runtime differs from the selected pin.",
            )
        if containers and containers[0].runtime_state not in {
            ContainerRuntimeState.STOPPED,
            ContainerRuntimeState.RUNNING,
        }:
            return (
                unsupported,
                ResetEligibilityReason.TRANSITIONAL_CONTAINER_STATE,
                "The managed container state is transitional or unknown.",
            )
        return (
            ResetEligibility.SUPPORTED,
            None,
            "Reset is available for this disposable instance.",
        )
