"""Closed stale-lease cleanup and bounded read-only reconciliation projections."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.lifecycle_status import (
    observe_lifecycle_semantic_state,
    semantic_state_is_unambiguous,
)
from kodi_lab.application.readiness_tasks import (
    ReadinessTaskRegistry,
    ReadinessTaskState,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.broker.errors import ClientError
from kodi_lab.broker.operations import (
    AddonZipDestinationResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    ObserveManagedKodiRuntimeResult,
)
from kodi_lab.development import (
    discard_unaccepted_addon_zip_artifact,
    remove_upload_stage,
)
from kodi_lab.domain import (
    AddonZipEnablementOperation,
    AddonZipImportOperation,
    AddonZipInstallationConfidence,
    AddonZipInstallationOperation,
    AddonZipInstallationOperationState,
    AddonZipSmokeTestOperation,
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentOperation,
    DevelopmentSourceDeploymentOperationState,
    DevelopmentSourceEnablementOperation,
    DevelopmentSourceSmokeTestOperation,
    GlobalHeavyOperationKind,
    InstanceRuntimeState,
    ManagedInstanceDeleteOperation,
    ManagedInstanceDeleteOperationState,
    ManagedInstanceLifecycleOperation,
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceMutationKind,
    ManagedInstanceSemanticState,
    ResetOperation,
    ResetOperationState,
    Snapshot,
    SnapshotCreateOperation,
    SnapshotCreateOperationPhase,
    SnapshotCreateOperationState,
    SnapshotDeleteOperation,
    SnapshotDeleteOperationState,
    SnapshotRestoreOperation,
    SnapshotRestoreOperationState,
    SnapshotState,
)
from kodi_lab.snapshot.storage import snapshot_artifact_presence
from kodi_lab.storage.models import (
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
)
from kodi_lab.storage.repositories import (
    AddonZipEnablementOperationRepository,
    AddonZipImportOperationRepository,
    AddonZipInstallationEvidenceRepository,
    AddonZipInstallationOperationRepository,
    AddonZipSmokeTestOperationRepository,
    DevelopmentSourceDeploymentOperationRepository,
    DevelopmentSourceDeploymentRepository,
    DevelopmentSourceEnablementOperationRepository,
    DevelopmentSourceSmokeTestOperationRepository,
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceDeleteOperationRepository,
    ManagedInstanceLifecycleOperationRepository,
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    ResetOperationRepository,
    SnapshotCreateOperationRepository,
    SnapshotDeleteOperationRepository,
    SnapshotRepository,
    SnapshotRestoreOperationRepository,
)


class CoordinationCorruptionError(RuntimeError):
    """A reservation cannot be proven to belong to its persisted operation."""

    code = "managed_instance_coordination_corruption"


class ManagedInstanceReconciliationRequiredError(RuntimeError):
    """A typed unresolved operation makes the requested mutation unsafe."""

    code = "managed_instance_reconciliation_required"

    def __init__(self, status: ManagedInstanceReconciliationStatus) -> None:
        super().__init__(
            status.safe_reason or "Managed-instance reconciliation is required."
        )
        self.status = status


class ReconciliationOperationKind(StrEnum):
    ADDON_ZIP_ENABLEMENT = "addon_zip_enablement"
    ADDON_ZIP_INSTALLATION = "addon_zip_installation"
    ADDON_ZIP_SMOKE_TEST = "addon_zip_smoke_test"
    DEVELOPMENT_SOURCE_DEPLOYMENT = "development_source_deployment"
    DEVELOPMENT_SOURCE_ENABLEMENT = "development_source_enablement"
    DEVELOPMENT_SOURCE_SMOKE_TEST = "development_source_smoke_test"
    LIFECYCLE = "lifecycle"
    RESET = "reset"
    SNAPSHOT_CREATE = "snapshot_create"
    SNAPSHOT_RESTORE = "snapshot_restore"
    SNAPSHOT_DELETE = "snapshot_delete"
    INSTANCE_DELETE = "instance_delete"
    MULTIPLE = "multiple_reconciliation_records"
    COORDINATION_CORRUPTION = "coordination_corruption"


class ReconciliationAction(StrEnum):
    ADDON_ZIP_ENABLEMENT = "addon_zip_enablement"
    ADDON_ZIP_INSTALLATION = "addon_zip_installation"
    ADDON_ZIP_SMOKE_TEST = "addon_zip_smoke_test"
    RESET = "reset"
    SNAPSHOT_CREATE = "snapshot_create"
    SNAPSHOT_RESTORE = "snapshot_restore"
    SNAPSHOT_DELETE = "snapshot_delete"
    INSTANCE_DELETE = "instance_delete"
    PROVISION = "provision"
    START = "start"
    STOP = "stop"
    DEPROVISION = "deprovision"
    FIXTURE_ASSIGNMENT = "fixture_assignment"
    RUNTIME_REASSIGNMENT = "runtime_reassignment"
    DEVELOPMENT_SOURCE_ATTACHMENT = "development_source_attachment"
    DEVELOPMENT_SOURCE_DEPLOYMENT = "development_source_deployment"
    DEVELOPMENT_SOURCE_ENABLEMENT = "development_source_enablement"
    DEVELOPMENT_SOURCE_SMOKE_TEST = "development_source_smoke_test"


INSTANCE_MUTATIONS = [
    ReconciliationAction.RESET,
    ReconciliationAction.SNAPSHOT_CREATE,
    ReconciliationAction.SNAPSHOT_RESTORE,
    ReconciliationAction.INSTANCE_DELETE,
    ReconciliationAction.PROVISION,
    ReconciliationAction.START,
    ReconciliationAction.STOP,
    ReconciliationAction.DEPROVISION,
    ReconciliationAction.FIXTURE_ASSIGNMENT,
    ReconciliationAction.RUNTIME_REASSIGNMENT,
    ReconciliationAction.DEVELOPMENT_SOURCE_ATTACHMENT,
    ReconciliationAction.DEVELOPMENT_SOURCE_DEPLOYMENT,
    ReconciliationAction.DEVELOPMENT_SOURCE_ENABLEMENT,
    ReconciliationAction.DEVELOPMENT_SOURCE_SMOKE_TEST,
    ReconciliationAction.ADDON_ZIP_INSTALLATION,
    ReconciliationAction.ADDON_ZIP_ENABLEMENT,
    ReconciliationAction.ADDON_ZIP_SMOKE_TEST,
]


class ManagedInstanceReconciliationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool
    blocking: bool
    operation_kind: ReconciliationOperationKind | None = None
    operation_uuid: UUID | None = None
    operation_state: str | None = Field(default=None, max_length=32)
    last_phase: str | None = Field(default=None, max_length=64)
    safe_reason: str | None = Field(default=None, max_length=256)
    observed_state: str | None = Field(default=None, max_length=64)
    blocked_actions: list[ReconciliationAction] = Field(max_length=32)
    requested_at: datetime | None = None
    started_at: datetime | None = None
    stale_since: datetime | None = None


class SnapshotReconciliationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    required: bool
    blocking: bool
    operation_kind: ReconciliationOperationKind | None = None
    operation_uuid: UUID | None = None
    operation_state: str | None = Field(default=None, max_length=32)
    last_phase: str | None = Field(default=None, max_length=64)
    safe_reason: str | None = Field(default=None, max_length=256)
    observed_state: str | None = Field(default=None, max_length=64)
    blocked_actions: list[ReconciliationAction] = Field(max_length=3)
    requested_at: datetime | None = None
    started_at: datetime | None = None


class StaleSweepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inspected_reservations: int
    released_reservations: int
    terminalized_operations: list[UUID] = Field(max_length=1024)


class _ObservationBroker(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult: ...


def _timestamp(value: str | None) -> datetime | None:
    return (
        None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _row_expired(
    row: ManagedInstanceMutationLeaseRow | GlobalHeavyOperationLeaseRow, now: datetime
) -> bool:
    expiry = _timestamp(row.lease_expires_at)
    return row.lease_token_digest is not None and expiry is not None and expiry <= now


class StaleOperationReconciliationDispatcher:
    """Terminalize only closed-enum, exactly-owned expired reservations."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        broker: _ObservationBroker | None = None,
        readiness: ReadinessTaskRegistry | None = None,
        data_root: Path | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._broker = broker
        self._readiness = readiness
        self._data_root = data_root

    def sweep_startup(self) -> StaleSweepResult:
        with self._session_factory() as session:
            try:
                mutation_ids = [
                    UUID(row.managed_instance_id)
                    for row in ManagedInstanceMutationLeaseRepository(
                        session
                    ).list_current()
                    if _row_expired(row, self._clock())
                ]
            except (LookupError, ValueError) as error:
                raise CoordinationCorruptionError(
                    "Mutation reservation identity is unsupported."
                ) from error
        terminalized: list[UUID] = []
        released = 0
        inspected = 0
        for instance_id in mutation_ids:
            result = self.sweep_instance(instance_id)
            inspected += result.inspected_reservations
            released += result.released_reservations
            terminalized.extend(result.terminalized_operations)
        result = self.sweep_global()
        inspected += result.inspected_reservations
        released += result.released_reservations
        terminalized.extend(
            operation_id
            for operation_id in result.terminalized_operations
            if operation_id not in terminalized
        )
        return StaleSweepResult(
            inspected_reservations=inspected,
            released_reservations=released,
            terminalized_operations=terminalized,
        )

    def sweep_instance(self, instance_id: UUID) -> StaleSweepResult:
        with self._session_factory.begin() as session:
            try:
                row = ManagedInstanceMutationLeaseRepository(session).current(
                    instance_id
                )
            except (LookupError, ValueError) as error:
                raise CoordinationCorruptionError(
                    "Mutation reservation kind is unsupported."
                ) from error
            if row is None or not _row_expired(row, self._clock()):
                return StaleSweepResult(
                    inspected_reservations=0,
                    released_reservations=0,
                    terminalized_operations=[],
                )
            operation_id, released = self._dispatch_mutation(
                session, row, self._clock()
            )
            return StaleSweepResult(
                inspected_reservations=1,
                released_reservations=released,
                terminalized_operations=[] if operation_id is None else [operation_id],
            )

    def sweep_global(self) -> StaleSweepResult:
        with self._session_factory.begin() as session:
            try:
                row = GlobalHeavyOperationLeaseRepository(session).current()
            except (LookupError, ValueError) as error:
                raise CoordinationCorruptionError(
                    "Global reservation kind is unsupported."
                ) from error
            if row is None or not _row_expired(row, self._clock()):
                return StaleSweepResult(
                    inspected_reservations=0,
                    released_reservations=0,
                    terminalized_operations=[],
                )
            operation_id, released = self._dispatch_global(session, row, self._clock())
            return StaleSweepResult(
                inspected_reservations=1,
                released_reservations=released,
                terminalized_operations=[operation_id],
            )

    def _dispatch_mutation(
        self, session: Session, row: ManagedInstanceMutationLeaseRow, now: datetime
    ) -> tuple[UUID | None, int]:
        try:
            kind = ManagedInstanceMutationKind(row.operation_kind)
            instance_id = UUID(row.managed_instance_id)
            operation_id = UUID(row.operation_id)
        except (TypeError, ValueError) as error:
            raise CoordinationCorruptionError(
                "Mutation reservation identity is invalid."
            ) from error
        digest = row.lease_token_digest
        assert digest is not None
        operation: (
            ResetOperation
            | SnapshotCreateOperation
            | SnapshotRestoreOperation
            | ManagedInstanceDeleteOperation
            | ManagedInstanceLifecycleOperation
            | DevelopmentSourceDeploymentOperation
            | DevelopmentSourceSmokeTestOperation
            | AddonZipInstallationOperation
            | AddonZipEnablementOperation
            | AddonZipSmokeTestOperation
            | None
        )
        if kind is ManagedInstanceMutationKind.LIFECYCLE:
            operation = ManagedInstanceLifecycleOperationRepository(session).get(
                operation_id
            )
            if (
                operation is None
                or operation.original_managed_instance_id != instance_id
                or operation.state is not ManagedInstanceLifecycleOperationState.RUNNING
            ):
                raise CoordinationCorruptionError(
                    "Expired lifecycle ownership is inconsistent."
                )
            observed = (
                observe_lifecycle_semantic_state(
                    self._broker, self._readiness, instance_id
                )
                if self._broker is not None
                else ManagedInstanceSemanticState.AMBIGUOUS
            )
            if not ManagedInstanceLifecycleOperationRepository(
                session
            ).mark_stale_reconciliation(
                operation_id, resulting_state=observed, now=now
            ):
                raise CoordinationCorruptionError(
                    "Expired lifecycle operation ownership changed."
                )
            if not ManagedInstanceMutationLeaseRepository(
                session
            ).release_expired_exact(
                instance_id,
                kind=kind,
                operation_id=operation_id,
                token_digest=digest,
                now=now,
            ):
                raise CoordinationCorruptionError(
                    "Expired lifecycle lease ownership changed."
                )
            return operation_id, 1
        if kind in {
            ManagedInstanceMutationKind.FIXTURE_ASSIGNMENT,
            ManagedInstanceMutationKind.RUNTIME_REASSIGNMENT,
            ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ATTACHMENT,
        }:
            if not ManagedInstanceMutationLeaseRepository(
                session
            ).release_expired_exact(
                instance_id,
                kind=kind,
                operation_id=operation_id,
                token_digest=digest,
                now=now,
            ):
                raise CoordinationCorruptionError(
                    "Expired short mutation ownership changed."
                )
            return None, 1
        released = 1
        if kind is ManagedInstanceMutationKind.RESET:
            operation = ResetOperationRepository(session).get(operation_id)
            self._validate_operation(operation, instance_id, digest, now)
            if not ResetOperationRepository(session).mark_stale_reconciliation(
                operation_id, token_digest=digest, now=now
            ):
                raise CoordinationCorruptionError("Expired reset ownership changed.")
        elif kind is ManagedInstanceMutationKind.SNAPSHOT_CREATE:
            operation = SnapshotCreateOperationRepository(session).get(operation_id)
            self._validate_operation(operation, instance_id, digest, now)
            self._require_matching_global(
                session,
                GlobalHeavyOperationKind.SNAPSHOT_CREATE,
                operation_id,
                digest,
                now,
            )
            if not SnapshotCreateOperationRepository(session).mark_stale_reconciliation(
                operation_id, token_digest=digest, now=now
            ):
                raise CoordinationCorruptionError(
                    "Expired snapshot-create ownership changed."
                )
            assert isinstance(operation, SnapshotCreateOperation)
            SnapshotRepository(session).mark_invalid(
                operation.snapshot_id, quarantined=True, validated_at=now
            )
            self._release_global(
                session,
                GlobalHeavyOperationKind.SNAPSHOT_CREATE,
                operation_id,
                digest,
                now,
            )
            released += 1
        elif kind is ManagedInstanceMutationKind.SNAPSHOT_RESTORE:
            operation = SnapshotRestoreOperationRepository(session).get(operation_id)
            self._validate_operation(operation, instance_id, digest, now)
            self._require_matching_global(
                session,
                GlobalHeavyOperationKind.SNAPSHOT_RESTORE,
                operation_id,
                digest,
                now,
            )
            if not SnapshotRestoreOperationRepository(
                session
            ).mark_stale_reconciliation(operation_id, token_digest=digest, now=now):
                raise CoordinationCorruptionError(
                    "Expired snapshot-restore ownership changed."
                )
            self._release_global(
                session,
                GlobalHeavyOperationKind.SNAPSHOT_RESTORE,
                operation_id,
                digest,
                now,
            )
            released += 1
        elif kind is ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_DEPLOYMENT:
            operation = DevelopmentSourceDeploymentOperationRepository(session).get(
                operation_id
            )
            self._validate_operation(operation, instance_id, digest, now)
            self._require_matching_global(
                session,
                GlobalHeavyOperationKind.DEVELOPMENT_SOURCE_DEPLOYMENT,
                operation_id,
                digest,
                now,
            )
            if not DevelopmentSourceDeploymentOperationRepository(
                session
            ).mark_stale_reconciliation(operation_id, token_digest=digest, now=now):
                raise CoordinationCorruptionError(
                    "Expired development-source deployment ownership changed."
                )
            assert isinstance(operation, DevelopmentSourceDeploymentOperation)
            DevelopmentSourceDeploymentRepository(session).mark_confidence(
                instance_id,
                operation.development_source_id,
                DevelopmentSourceDeploymentConfidence.UNKNOWN,
            )
            self._release_global(
                session,
                GlobalHeavyOperationKind.DEVELOPMENT_SOURCE_DEPLOYMENT,
                operation_id,
                digest,
                now,
            )
            released += 1
        elif kind is ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ENABLEMENT:
            enablement_operation = DevelopmentSourceEnablementOperationRepository(
                session
            ).get(operation_id)
            if (
                enablement_operation is None
                or enablement_operation.managed_instance_id != instance_id
                or not enablement_operation.active
            ):
                raise CoordinationCorruptionError(
                    "Expired enablement ownership is inconsistent."
                )
            if not DevelopmentSourceEnablementOperationRepository(
                session
            ).mark_stale_reconciliation(operation_id, now=now):
                raise CoordinationCorruptionError("Expired enablement history changed.")
        elif kind is ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_SMOKE_TEST:
            operation = DevelopmentSourceSmokeTestOperationRepository(session).get(
                operation_id
            )
            if (
                operation is None
                or operation.managed_instance_id != instance_id
                or operation.state.value != "running"
                or operation.lease_token_digest != digest
            ):
                raise CoordinationCorruptionError(
                    "Expired smoke-test ownership is inconsistent."
                )
            if not DevelopmentSourceSmokeTestOperationRepository(session).mark_stale(
                operation_id, token_digest=digest, now=now
            ):
                raise CoordinationCorruptionError("Expired smoke-test history changed.")
            if operation.invocation_dispatched:
                DevelopmentSourceDeploymentRepository(session).mark_confidence(
                    instance_id,
                    operation.development_source_id,
                    DevelopmentSourceDeploymentConfidence.UNKNOWN,
                )
        elif kind is ManagedInstanceMutationKind.ADDON_ZIP_SMOKE_TEST:
            operation = AddonZipSmokeTestOperationRepository(session).get(operation_id)
            if (
                operation is None
                or operation.managed_instance_id != instance_id
                or operation.state.value != "running"
                or operation.lease_token_digest != digest
            ):
                raise CoordinationCorruptionError(
                    "Expired ZIP smoke-test ownership is inconsistent."
                )
            if not AddonZipSmokeTestOperationRepository(session).mark_stale(
                operation_id, token_digest=digest, now=now
            ):
                raise CoordinationCorruptionError(
                    "Expired ZIP smoke-test history changed."
                )
            if operation.invocation_dispatched:
                AddonZipInstallationEvidenceRepository(
                    session
                ).mark_artifact_confidence(
                    instance_id,
                    operation.artifact_id,
                    AddonZipInstallationConfidence.UNKNOWN,
                )
        elif kind is ManagedInstanceMutationKind.ADDON_ZIP_INSTALLATION:
            operation = AddonZipInstallationOperationRepository(session).get(
                operation_id
            )
            if (
                operation is None
                or operation.managed_instance_id != instance_id
                or operation.state is not AddonZipInstallationOperationState.RUNNING
                or operation.lease_token_digest != digest
            ):
                raise CoordinationCorruptionError(
                    "Expired uploaded-ZIP installation ownership is inconsistent."
                )
            self._require_matching_global(
                session,
                GlobalHeavyOperationKind.ADDON_ZIP_INSTALLATION,
                operation_id,
                digest,
                now,
            )
            if not AddonZipInstallationOperationRepository(
                session
            ).mark_stale_reconciliation(operation_id, token_digest=digest, now=now):
                raise CoordinationCorruptionError(
                    "Expired uploaded-ZIP installation history changed."
                )
            AddonZipInstallationEvidenceRepository(session).mark_confidence(
                instance_id, AddonZipInstallationConfidence.UNKNOWN
            )
            self._release_global(
                session,
                GlobalHeavyOperationKind.ADDON_ZIP_INSTALLATION,
                operation_id,
                digest,
                now,
            )
            released += 1
        elif kind is ManagedInstanceMutationKind.ADDON_ZIP_ENABLEMENT:
            operation = AddonZipEnablementOperationRepository(session).get(operation_id)
            if (
                operation is None
                or operation.managed_instance_id != instance_id
                or not operation.active
            ):
                raise CoordinationCorruptionError(
                    "Expired ZIP enablement ownership is inconsistent."
                )
            if not AddonZipEnablementOperationRepository(
                session
            ).mark_stale_reconciliation(operation_id, now=now):
                raise CoordinationCorruptionError(
                    "Expired ZIP enablement history changed."
                )
            if operation.enable_performed:
                AddonZipInstallationEvidenceRepository(
                    session
                ).mark_artifact_confidence(
                    instance_id,
                    operation.artifact_id,
                    AddonZipInstallationConfidence.UNKNOWN,
                )
        elif kind is ManagedInstanceMutationKind.INSTANCE_DELETE:
            operation = ManagedInstanceDeleteOperationRepository(session).get(
                operation_id
            )
            self._validate_operation(operation, instance_id, digest, now)
            assert isinstance(operation, ManagedInstanceDeleteOperation)
            if not operation.preserve_snapshots:
                self._require_matching_global(
                    session,
                    GlobalHeavyOperationKind.INSTANCE_DELETE,
                    operation_id,
                    digest,
                    now,
                )
            if not ManagedInstanceDeleteOperationRepository(
                session
            ).mark_stale_reconciliation(operation_id, token_digest=digest, now=now):
                raise CoordinationCorruptionError(
                    "Expired instance-delete ownership changed."
                )
            if not operation.preserve_snapshots:
                self._release_global(
                    session,
                    GlobalHeavyOperationKind.INSTANCE_DELETE,
                    operation_id,
                    digest,
                    now,
                )
                released += 1
        else:
            raise CoordinationCorruptionError("Unsupported mutation reservation kind.")
        if not ManagedInstanceMutationLeaseRepository(session).release_expired_exact(
            instance_id,
            kind=kind,
            operation_id=operation_id,
            token_digest=digest,
            now=now,
        ):
            raise CoordinationCorruptionError("Expired mutation ownership changed.")
        return operation_id, released

    def _dispatch_global(
        self, session: Session, row: GlobalHeavyOperationLeaseRow, now: datetime
    ) -> tuple[UUID, int]:
        try:
            kind = GlobalHeavyOperationKind(row.operation_kind)
            operation_id = UUID(row.operation_id)
        except (TypeError, ValueError) as error:
            raise CoordinationCorruptionError(
                "Global reservation identity is invalid."
            ) from error
        digest = row.lease_token_digest
        assert digest is not None
        operation: (
            SnapshotDeleteOperation
            | SnapshotCreateOperation
            | SnapshotRestoreOperation
            | ManagedInstanceDeleteOperation
            | DevelopmentSourceDeploymentOperation
            | AddonZipInstallationOperation
            | AddonZipImportOperation
            | None
        )
        if kind is GlobalHeavyOperationKind.SNAPSHOT_DELETE:
            operation = SnapshotDeleteOperationRepository(session).get(operation_id)
            self._validate_operation(
                operation,
                operation.managed_instance_id if operation else None,
                digest,
                now,
            )
            if not SnapshotDeleteOperationRepository(session).mark_stale_reconciliation(
                operation_id, token_digest=digest, now=now
            ):
                raise CoordinationCorruptionError(
                    "Expired snapshot-delete ownership changed."
                )
            self._release_global(session, kind, operation_id, digest, now)
            return operation_id, 1
        if kind is GlobalHeavyOperationKind.ADDON_ZIP_IMPORT:
            operation = AddonZipImportOperationRepository(session).get(operation_id)
            if (
                operation is None
                or operation.state.value != "running"
                or operation.lease_token_digest != digest
                or operation.lease_expires_at is None
                or operation.lease_expires_at > now
            ):
                raise CoordinationCorruptionError(
                    "Expired uploaded-ZIP import ownership is inconsistent."
                )
            if not AddonZipImportOperationRepository(session).mark_stale_failed(
                operation_id, token_digest=digest, now=now
            ):
                raise CoordinationCorruptionError(
                    "Expired uploaded-ZIP import history changed."
                )
            if self._data_root is not None:
                discard_unaccepted_addon_zip_artifact(
                    self._data_root, operation.artifact_id
                )
                remove_upload_stage(self._data_root, operation.artifact_id)
            self._release_global(session, kind, operation_id, digest, now)
            return operation_id, 1
        if kind is GlobalHeavyOperationKind.SNAPSHOT_CREATE:
            operation = SnapshotCreateOperationRepository(session).get(operation_id)
        elif kind is GlobalHeavyOperationKind.SNAPSHOT_RESTORE:
            operation = SnapshotRestoreOperationRepository(session).get(operation_id)
        elif kind is GlobalHeavyOperationKind.INSTANCE_DELETE:
            operation = ManagedInstanceDeleteOperationRepository(session).get(
                operation_id
            )
        elif kind is GlobalHeavyOperationKind.DEVELOPMENT_SOURCE_DEPLOYMENT:
            operation = DevelopmentSourceDeploymentOperationRepository(session).get(
                operation_id
            )
        elif kind is GlobalHeavyOperationKind.ADDON_ZIP_INSTALLATION:
            operation = AddonZipInstallationOperationRepository(session).get(
                operation_id
            )
        else:
            raise CoordinationCorruptionError("Unsupported global reservation kind.")
        if operation is None:
            raise CoordinationCorruptionError("Global reservation owner is missing.")
        instance_id = (
            operation.original_managed_instance_id
            if isinstance(operation, ManagedInstanceDeleteOperation)
            else operation.managed_instance_id
        )
        if instance_id is None:
            raise CoordinationCorruptionError(
                "Paired mutation reservation has no live instance identity."
            )
        mutation = ManagedInstanceMutationLeaseRepository(session).current(instance_id)
        if mutation is None:
            raise CoordinationCorruptionError("Paired mutation reservation is missing.")
        dispatched_id, released = self._dispatch_mutation(session, mutation, now)
        return dispatched_id or operation_id, released

    @staticmethod
    def _validate_operation(
        operation: ResetOperation
        | SnapshotCreateOperation
        | SnapshotRestoreOperation
        | SnapshotDeleteOperation
        | ManagedInstanceDeleteOperation
        | DevelopmentSourceDeploymentOperation
        | AddonZipInstallationOperation
        | None,
        instance_id: UUID | None,
        digest: str,
        now: datetime,
    ) -> None:
        if operation is None:
            raise CoordinationCorruptionError("Reservation owner operation is missing.")
        operation_instance_id = (
            operation.original_managed_instance_id
            if isinstance(operation, ManagedInstanceDeleteOperation)
            else operation.managed_instance_id
        )
        if instance_id is not None and operation_instance_id != instance_id:
            raise CoordinationCorruptionError(
                "Reservation instance identity does not match its owner."
            )
        running_states = {
            ResetOperationState.RUNNING,
            SnapshotCreateOperationState.RUNNING,
            SnapshotRestoreOperationState.RUNNING,
            SnapshotDeleteOperationState.RUNNING,
            ManagedInstanceDeleteOperationState.RUNNING,
            DevelopmentSourceDeploymentOperationState.RUNNING,
            AddonZipInstallationOperationState.RUNNING,
        }
        if operation.state not in running_states:
            raise CoordinationCorruptionError("Reservation owner is not running.")
        if operation.lease_token_digest != digest:
            raise CoordinationCorruptionError(
                "Reservation token ownership is inconsistent."
            )
        if operation.lease_expires_at is None or operation.lease_expires_at > now:
            raise CoordinationCorruptionError("Reservation expiry is inconsistent.")

    @staticmethod
    def _require_matching_global(
        session: Session,
        kind: GlobalHeavyOperationKind,
        operation_id: UUID,
        digest: str,
        now: datetime,
    ) -> None:
        row = GlobalHeavyOperationLeaseRepository(session).current()
        if (
            row is None
            or row.operation_kind != kind
            or row.operation_id != str(operation_id)
            or row.lease_token_digest != digest
            or not _row_expired(row, now)
        ):
            raise CoordinationCorruptionError(
                "Paired global reservation is inconsistent."
            )

    @staticmethod
    def _release_global(
        session: Session,
        kind: GlobalHeavyOperationKind,
        operation_id: UUID,
        digest: str,
        now: datetime,
    ) -> None:
        if not GlobalHeavyOperationLeaseRepository(session).release_expired_exact(
            kind=kind,
            operation_id=operation_id,
            token_digest=digest,
            now=now,
        ):
            raise CoordinationCorruptionError("Expired global ownership changed.")


class ManagedInstanceReconciliationService:
    """Read typed unresolved records and enforce one bounded mutation policy."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        broker: _ObservationBroker | None = None,
        data_root: Path | None = None,
        readiness: ReadinessTaskRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._broker = broker
        self._data_root = data_root
        self._readiness = readiness

    def status(self, instance_id: UUID) -> ManagedInstanceReconciliationStatus:
        with self._session_factory() as session:
            records: list[tuple[ReconciliationOperationKind, object]] = []
            records.extend(
                (ReconciliationOperationKind.LIFECYCLE, item)
                for item in ManagedInstanceLifecycleOperationRepository(
                    session
                ).reconciliation_for_instance(instance_id)
            )
            records.extend(
                (ReconciliationOperationKind.ADDON_ZIP_ENABLEMENT, item)
                for item in AddonZipEnablementOperationRepository(
                    session
                ).reconciliation_for_instance(instance_id)
            )
            records.extend(
                (ReconciliationOperationKind.ADDON_ZIP_INSTALLATION, item)
                for item in AddonZipInstallationOperationRepository(
                    session
                ).reconciliation_for_instance(instance_id)
            )
            records.extend(
                (ReconciliationOperationKind.ADDON_ZIP_SMOKE_TEST, item)
                for item in AddonZipSmokeTestOperationRepository(
                    session
                ).reconciliation_for_instance(instance_id)
            )
            records.extend(
                (ReconciliationOperationKind.RESET, item)
                for item in ResetOperationRepository(
                    session
                ).reconciliation_for_instance(instance_id)
            )
            records.extend(
                (ReconciliationOperationKind.SNAPSHOT_CREATE, item)
                for item in SnapshotCreateOperationRepository(
                    session
                ).reconciliation_for_instance(instance_id)
            )
            records.extend(
                (ReconciliationOperationKind.SNAPSHOT_RESTORE, item)
                for item in SnapshotRestoreOperationRepository(
                    session
                ).reconciliation_for_instance(instance_id)
            )
            records.extend(
                (ReconciliationOperationKind.INSTANCE_DELETE, item)
                for item in ManagedInstanceDeleteOperationRepository(
                    session
                ).reconciliation_for_instance(instance_id)
            )
            records.extend(
                (ReconciliationOperationKind.DEVELOPMENT_SOURCE_DEPLOYMENT, item)
                for item in DevelopmentSourceDeploymentOperationRepository(
                    session
                ).reconciliation_for_instance(instance_id)
            )
            records.extend(
                (ReconciliationOperationKind.DEVELOPMENT_SOURCE_ENABLEMENT, item)
                for item in DevelopmentSourceEnablementOperationRepository(
                    session
                ).reconciliation_for_instance(instance_id)
            )
            records.extend(
                (ReconciliationOperationKind.DEVELOPMENT_SOURCE_SMOKE_TEST, item)
                for item in DevelopmentSourceSmokeTestOperationRepository(
                    session
                ).reconciliation_for_instance(instance_id)
            )
            snapshots = SnapshotRepository(session)
            if (
                len(records) == 1
                and records[0][0] is ReconciliationOperationKind.SNAPSHOT_CREATE
            ):
                create = records[0][1]
                assert isinstance(create, SnapshotCreateOperation)
                snapshot = snapshots.get(create.snapshot_id)
            else:
                snapshot = None
        if not records:
            return ManagedInstanceReconciliationStatus(
                required=False, blocking=False, blocked_actions=[]
            )
        if len(records) != 1:
            return ManagedInstanceReconciliationStatus(
                required=True,
                blocking=True,
                operation_kind=ReconciliationOperationKind.MULTIPLE,
                safe_reason=(
                    "Multiple unresolved operation records require operator review."
                ),
                observed_state="multiple_reconciliation_records",
                blocked_actions=INSTANCE_MUTATIONS,
            )
        kind, raw = records[0]
        operation = raw
        blocking = True
        observed = "runtime_state_uncertain"
        if kind is ReconciliationOperationKind.SNAPSHOT_CREATE:
            assert isinstance(operation, SnapshotCreateOperation)
            blocking, observed = self._classify_create(instance_id, operation, snapshot)
        elif kind is ReconciliationOperationKind.SNAPSHOT_RESTORE:
            assert isinstance(operation, SnapshotRestoreOperation)
            observed = self._observe_restore(instance_id, operation)
        elif kind is ReconciliationOperationKind.INSTANCE_DELETE:
            observed = self._observe_instance_delete(instance_id)
        elif kind is ReconciliationOperationKind.RESET:
            observed = self._observe_reset(instance_id)
        elif kind is ReconciliationOperationKind.LIFECYCLE:
            assert isinstance(operation, ManagedInstanceLifecycleOperation)
            semantic = (
                observe_lifecycle_semantic_state(
                    self._broker, self._readiness, instance_id
                )
                if self._broker is not None
                else ManagedInstanceSemanticState.AMBIGUOUS
            )
            observed = semantic.value
            blocking = not semantic_state_is_unambiguous(semantic)
        elif kind is ReconciliationOperationKind.DEVELOPMENT_SOURCE_DEPLOYMENT:
            observed = "deployment_destination_requires_read_only_inspection"
        elif kind is ReconciliationOperationKind.DEVELOPMENT_SOURCE_ENABLEMENT:
            observed = "live_enabled_state_does_not_resolve_history"
        elif kind is ReconciliationOperationKind.ADDON_ZIP_ENABLEMENT:
            observed = "live_enabled_state_does_not_resolve_zip_history"
        elif kind is ReconciliationOperationKind.DEVELOPMENT_SOURCE_SMOKE_TEST:
            assert isinstance(operation, DevelopmentSourceSmokeTestOperation)
            blocking, observed = self._classify_smoke(instance_id, operation)
        elif kind is ReconciliationOperationKind.ADDON_ZIP_SMOKE_TEST:
            assert isinstance(operation, AddonZipSmokeTestOperation)
            blocking, observed = self._classify_smoke(instance_id, operation)
        elif kind is ReconciliationOperationKind.ADDON_ZIP_INSTALLATION:
            assert isinstance(operation, AddonZipInstallationOperation)
            blocking, observed = self._classify_addon_zip(instance_id, operation)
        reason = {
            ReconciliationOperationKind.LIFECYCLE: (
                "Lifecycle state requires operator reconciliation."
            ),
            ReconciliationOperationKind.RESET: (
                "Reset state requires operator reconciliation."
            ),
            ReconciliationOperationKind.SNAPSHOT_CREATE: (
                "Snapshot creation requires operator reconciliation."
            ),
            ReconciliationOperationKind.SNAPSHOT_RESTORE: (
                "Snapshot restore requires operator reconciliation."
            ),
            ReconciliationOperationKind.INSTANCE_DELETE: (
                "Instance deletion requires operator reconciliation."
            ),
            ReconciliationOperationKind.DEVELOPMENT_SOURCE_DEPLOYMENT: (
                "Development-source deployment requires operator reconciliation."
            ),
            ReconciliationOperationKind.DEVELOPMENT_SOURCE_ENABLEMENT: (
                "Development-source enablement requires operator reconciliation."
            ),
            ReconciliationOperationKind.DEVELOPMENT_SOURCE_SMOKE_TEST: (
                "Development-source smoke execution requires reconciliation."
            ),
            ReconciliationOperationKind.ADDON_ZIP_INSTALLATION: (
                "Uploaded-ZIP installation requires operator reconciliation."
            ),
            ReconciliationOperationKind.ADDON_ZIP_ENABLEMENT: (
                "Uploaded-ZIP enablement requires operator reconciliation."
            ),
            ReconciliationOperationKind.ADDON_ZIP_SMOKE_TEST: (
                "Uploaded-ZIP smoke execution requires reconciliation."
            ),
        }[kind]
        assert isinstance(
            operation,
            (
                ResetOperation,
                SnapshotCreateOperation,
                SnapshotRestoreOperation,
                ManagedInstanceDeleteOperation,
                ManagedInstanceLifecycleOperation,
                DevelopmentSourceDeploymentOperation,
                DevelopmentSourceEnablementOperation,
                DevelopmentSourceSmokeTestOperation,
                AddonZipInstallationOperation,
                AddonZipEnablementOperation,
                AddonZipSmokeTestOperation,
            ),
        )
        return ManagedInstanceReconciliationStatus(
            required=True,
            blocking=blocking,
            operation_kind=kind,
            operation_uuid=operation.id,
            operation_state=operation.state.value,
            last_phase=(
                operation.kind.value
                if isinstance(operation, ManagedInstanceLifecycleOperation)
                else operation.phase.value
            ),
            safe_reason=reason,
            observed_state=observed,
            blocked_actions=(
                [
                    action
                    for action in INSTANCE_MUTATIONS
                    if action is not ReconciliationAction.STOP
                ]
                if blocking
                and kind
                in {
                    ReconciliationOperationKind.DEVELOPMENT_SOURCE_SMOKE_TEST,
                    ReconciliationOperationKind.ADDON_ZIP_SMOKE_TEST,
                }
                else INSTANCE_MUTATIONS
                if blocking
                else []
            ),
            requested_at=operation.requested_at,
            started_at=operation.started_at,
        )

    def _classify_smoke(
        self,
        instance_id: UUID,
        operation: DevelopmentSourceSmokeTestOperation | AddonZipSmokeTestOperation,
    ) -> tuple[bool, str]:
        if self._broker is None or operation.expected_container_id is None:
            return True, "original_container_state_unknown"
        try:
            matches = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == str(instance_id)
                and item.container_id == operation.expected_container_id
            ]
        except ClientError:
            return True, "original_container_state_unknown"
        if not matches:
            return False, "original_container_absent"
        if (
            len(matches) == 1
            and matches[0].runtime_state is ContainerRuntimeState.STOPPED
        ):
            return False, "original_container_stopped"
        if (
            len(matches) == 1
            and matches[0].runtime_state is ContainerRuntimeState.RUNNING
        ):
            return True, "original_container_running"
        return True, "original_container_state_unknown"

    def _classify_addon_zip(
        self, instance_id: UUID, operation: AddonZipInstallationOperation
    ) -> tuple[bool, str]:
        """Classify bounded observations without resolving ambiguous history."""

        expected_container = operation.expected_container_id
        if self._broker is None or expected_container is None:
            return True, "zip_runtime_identity_unknown"
        with self._session_factory() as session:
            instance = ManagedInstanceRepository(session).get(instance_id)
        if (
            instance is None
            or instance.runtime_revision_id != operation.runtime_revision_id
        ):
            return True, "zip_container_or_revision_changed"
        try:
            matching = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == str(instance_id)
            ]
            if len(matching) != 1 or matching[0].container_id != expected_container:
                return True, "zip_container_or_revision_changed"
            observe_destination = getattr(
                self._broker, "observe_addon_zip_destination", None
            )
            if not callable(observe_destination):
                return True, "zip_destination_observation_unavailable"
            destination = cast(
                AddonZipDestinationResult,
                observe_destination(
                    managed_instance_id=str(instance_id),
                    artifact_id=str(operation.artifact_id),
                    installation_operation_id=str(operation.id),
                    expected_container_id=expected_container,
                ),
            )
        except (ClientError, RuntimeError, ValueError):
            return True, "zip_destination_observation_unavailable"
        if (
            operation.dependency_baseline_json is not None
            and operation.dependency_post_json is not None
            and operation.dependency_baseline_json != operation.dependency_post_json
        ):
            return True, "zip_dependency_state_changed"
        if destination.destination_state == "absent":
            return True, "zip_destination_absent"
        if destination.destination_state != "exact_payload_present":
            return True, "zip_destination_partial_or_unexpected"
        if operation.recognition_enabled is True:
            return True, "zip_recognized_unexpectedly_enabled"
        if operation.failure_reason in {
            "Kodi did not recognize the installed add-on.",
            "Kodi recognition did not match the exact disabled postcondition.",
        }:
            return True, "zip_recognition_missing_or_unexpected"
        if (
            operation.recognition_enabled is False
            and operation.dependency_post_json is not None
        ):
            return True, "zip_expected_disabled_recognition_observed"
        return True, "zip_exact_payload_recognition_unknown"

    def require_allowed(
        self, instance_id: UUID, action: ReconciliationAction
    ) -> ManagedInstanceReconciliationStatus:
        status = self.status(instance_id)
        if action in status.blocked_actions:
            raise ManagedInstanceReconciliationRequiredError(status)
        return status

    def _classify_create(
        self,
        instance_id: UUID,
        operation: SnapshotCreateOperation,
        snapshot: Snapshot | None,
    ) -> tuple[bool, str]:
        if snapshot is None:
            return True, "snapshot_publication_and_source_state_uncertain"
        if operation.phase in {
            SnapshotCreateOperationPhase.ACQUIRING_LEASES,
            SnapshotCreateOperationPhase.RESOLVING_INSTANCE,
        }:
            return False, "source_not_mutated_snapshot_quarantined"
        if snapshot.prior_runtime_state is InstanceRuntimeState.STOPPED:
            suffix = (
                "quarantined"
                if snapshot.state is SnapshotState.QUARANTINED
                else "published"
            )
            return False, f"source_stopped_snapshot_{suffix}"
        if (
            snapshot.prior_runtime_state is not InstanceRuntimeState.RUNNING
            or self._broker is None
        ):
            return True, "source_runtime_recovery_uncertain"
        try:
            matches = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == str(instance_id)
            ]
            if (
                len(matches) != 1
                or matches[0].runtime_state is not ContainerRuntimeState.RUNNING
            ):
                return True, "source_runtime_recovery_uncertain"
            observed = self._broker.observe_managed_kodi_runtime(
                managed_instance_id=str(instance_id),
                expected_container_id=matches[0].container_id,
            )
        except ClientError:
            return True, "source_runtime_observation_unavailable"
        readiness_proven = (
            self._readiness is not None
            and self._readiness.snapshot(instance_id).state is ReadinessTaskState.USABLE
        )
        if (
            observed.runtime_state is ContainerRuntimeState.RUNNING
            and observed.process_state is KodiProcessState.RUNNING
            and readiness_proven
        ):
            suffix = (
                "quarantined"
                if snapshot.state is SnapshotState.QUARANTINED
                else "published"
            )
            return False, f"source_running_snapshot_{suffix}"
        return True, "source_runtime_recovery_uncertain"

    def _observe_reset(self, instance_id: UUID) -> str:
        if self._broker is None:
            return "runtime_state_requires_inspection"
        try:
            matches = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == str(instance_id)
            ]
        except ClientError:
            return "runtime_observation_unavailable"
        if not matches:
            return "unprovisioned"
        if len(matches) != 1:
            return "ambiguous_transitional"
        return (
            "runtime_running"
            if matches[0].runtime_state is ContainerRuntimeState.RUNNING
            else "runtime_stopped"
        )

    def _observe_instance_delete(self, instance_id: UUID) -> str:
        with self._session_factory() as session:
            instance_present = (
                ManagedInstanceRepository(session).get(instance_id) is not None
            )
            snapshots = SnapshotRepository(session).list_for_original_instance(
                instance_id
            )
        if self._broker is None:
            container_count = -1
        else:
            try:
                container_count = sum(
                    item.managed_instance_id == str(instance_id)
                    for item in self._broker.managed_container_inventory().containers
                )
            except ClientError:
                return "instance_deletion_observation_unavailable"
        if container_count > 1:
            return "ambiguous_transitional"
        if not instance_present:
            return (
                "instance_absent_preserved_snapshots"
                if snapshots
                else "instance_absent_no_snapshots"
            )
        if container_count == 1:
            return "instance_present_container_present"
        if container_count == 0:
            if self._data_root is not None and snapshots:
                presence = [
                    snapshot_artifact_presence(self._data_root, item.id)
                    for item in snapshots
                ]
                if all(item == "absent" for item in presence):
                    return "instance_present_artifacts_absent"
            return "instance_present_container_absent"
        return "instance_deletion_state_requires_inspection"

    def _observe_restore(
        self, instance_id: UUID, operation: SnapshotRestoreOperation
    ) -> str:
        if self._broker is None:
            return "restore_state_requires_inspection"
        try:
            containers = [
                item
                for item in self._broker.managed_container_inventory().containers
                if item.managed_instance_id == str(instance_id)
            ]
        except ClientError:
            return "restore_observation_unavailable"
        if len(containers) > 1:
            return "ambiguous_transitional"
        if not containers:
            return "unprovisioned"
        current: ManagedContainerInventoryEntry = containers[0]
        if current.container_id == operation.old_container_id:
            return "old_container_present"
        if current.container_id != operation.fresh_container_id:
            return "ambiguous_transitional"
        return (
            "fresh_container_running"
            if current.runtime_state is ContainerRuntimeState.RUNNING
            else "fresh_container_stopped"
        )


class SnapshotReconciliationService:
    """Bounded Snapshot-specific unresolved state without operation-model leakage."""

    def __init__(
        self, session_factory: sessionmaker[Session], *, data_root: Path | None = None
    ) -> None:
        self._session_factory = session_factory
        self._data_root = data_root

    def status(self, snapshot_id: UUID) -> SnapshotReconciliationStatus:
        with self._session_factory() as session:
            snapshot = SnapshotRepository(session).get(snapshot_id)
            delete_records = SnapshotDeleteOperationRepository(
                session
            ).reconciliation_for_snapshot(snapshot_id)
            create_records = SnapshotCreateOperationRepository(
                session
            ).reconciliation_for_snapshot(snapshot_id)
        records: list[tuple[ReconciliationOperationKind, object]] = [
            *(
                (ReconciliationOperationKind.SNAPSHOT_CREATE, item)
                for item in create_records
            ),
            *(
                (ReconciliationOperationKind.SNAPSHOT_DELETE, item)
                for item in delete_records
            ),
        ]
        if not records:
            return SnapshotReconciliationStatus(
                snapshot_id=snapshot_id,
                required=False,
                blocking=False,
                blocked_actions=[],
            )
        if len(records) != 1:
            return SnapshotReconciliationStatus(
                snapshot_id=snapshot_id,
                required=True,
                blocking=True,
                operation_kind=ReconciliationOperationKind.MULTIPLE,
                safe_reason=(
                    "Multiple unresolved Snapshot records require operator review."
                ),
                observed_state="multiple_reconciliation_records",
                blocked_actions=[
                    ReconciliationAction.SNAPSHOT_RESTORE,
                    ReconciliationAction.SNAPSHOT_DELETE,
                    ReconciliationAction.INSTANCE_DELETE,
                ],
            )
        kind, raw = records[0]
        assert isinstance(raw, (SnapshotCreateOperation, SnapshotDeleteOperation))
        if kind is ReconciliationOperationKind.SNAPSHOT_DELETE and self._data_root:
            if snapshot is None:
                observed = "inconsistent_storage"
            else:
                presence = snapshot_artifact_presence(self._data_root, snapshot_id)
                observed = {
                    "directory": "snapshot_and_artifact_present",
                    "absent": "snapshot_present_artifact_absent",
                }.get(presence, "inconsistent_storage")
        elif snapshot is None:
            observed = "snapshot_row_absent"
        elif snapshot.state is SnapshotState.DELETING:
            observed = "snapshot_row_deleting"
        elif snapshot.state is SnapshotState.QUARANTINED:
            observed = "snapshot_quarantined"
        elif snapshot.state is SnapshotState.AVAILABLE:
            observed = "snapshot_row_available"
        else:
            observed = "snapshot_row_transitional"
        return SnapshotReconciliationStatus(
            snapshot_id=snapshot_id,
            required=True,
            blocking=True,
            operation_kind=kind,
            operation_uuid=raw.id,
            operation_state=raw.state.value,
            last_phase=raw.phase.value,
            safe_reason=(
                "Snapshot deletion requires operator reconciliation."
                if kind is ReconciliationOperationKind.SNAPSHOT_DELETE
                else "Snapshot creation requires operator reconciliation."
            ),
            observed_state=observed,
            blocked_actions=[
                ReconciliationAction.SNAPSHOT_RESTORE,
                ReconciliationAction.SNAPSHOT_DELETE,
                ReconciliationAction.INSTANCE_DELETE,
            ],
            requested_at=raw.requested_at,
            started_at=raw.started_at,
        )

    def require_allowed(self, snapshot_id: UUID, action: ReconciliationAction) -> None:
        status = self.status(snapshot_id)
        if action in status.blocked_actions:
            raise ManagedInstanceReconciliationRequiredError(
                ManagedInstanceReconciliationStatus(
                    required=True,
                    blocking=True,
                    operation_kind=status.operation_kind,
                    operation_uuid=status.operation_uuid,
                    operation_state=status.operation_state,
                    last_phase=status.last_phase,
                    safe_reason=status.safe_reason,
                    observed_state=status.observed_state,
                    blocked_actions=status.blocked_actions,
                    requested_at=status.requested_at,
                    started_at=status.started_at,
                )
            )
