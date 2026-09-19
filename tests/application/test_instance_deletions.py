from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

import pytest
from sqlalchemy import create_engine, delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.instance_delete_operations import (
    ManagedInstanceDeleteConflictError,
    ManagedInstanceDeleteOperationCoordinator,
)
from kodi_lab.application.instance_deletions import (
    DisposableManagedInstanceDeletionService,
    InstanceDeleteBroker,
)
from kodi_lab.application.promotion_verifications import PromotionVerificationService
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationRequiredError,
)
from kodi_lab.application.snapshot_delete_operations import (
    SnapshotDeleteConflictError,
    SnapshotDeleteOperationCoordinator,
)
from kodi_lab.application.snapshot_restore_operations import (
    SnapshotRestoreOperationCoordinator,
    SnapshotSourceInstanceDeletedError,
)
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
)
from kodi_lab.broker.operations import (
    DeleteManagedSnapshotArtifactResult,
    DeprovisionManagedContainerResult,
    ManagedContainerInventoryEntry,
    ManagedContainerInventoryResult,
    StopManagedContainerResult,
)
from kodi_lab.domain import (
    DevelopmentSourceDeploymentConfidence,
    DevelopmentSourceDeploymentOperationPhase,
    DevelopmentSourceDeploymentOperationState,
    DevelopmentSourceDeploymentRequestKind,
    DevelopmentSourceDeploymentResult,
    DevelopmentSourceEnablementOperationPhase,
    DevelopmentSourceEnablementOperationState,
    DevelopmentSourceEnablementResult,
    GlobalHeavyOperationKind,
    InstanceRuntimeState,
    ManagedInstanceDeleteOperationState,
    ManagedInstanceMutationKind,
    Snapshot,
    SnapshotSourceBinding,
    SnapshotState,
    SnapshotValidationResult,
)
from kodi_lab.snapshot.storage import delete_snapshot_artifact, snapshot_directory
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    AgentWorkspacePromotionHandoffRow,
    AgentWorkspacePromotionOperationRow,
    AgentWorkspacePromotionPlanRow,
    AgentWorkspaceRow,
    DevelopmentSourceAttachmentOperationRow,
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceDeploymentRow,
    DevelopmentSourceEnablementOperationRow,
    DevelopmentSourceRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceDeleteOperationRow,
    ManagedInstanceDevelopmentSourceRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    PromotionVerificationLogEvidenceRow,
    PromotionVerificationOperationRow,
    ResetOperationRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    SnapshotDeleteOperationRow,
    SnapshotRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceMutationLeaseRepository,
    ManagedInstanceRepository,
    SnapshotRepository,
)

INSTANCE_ID = UUID("95000000-0000-4000-8000-000000000001")
SIBLING_ID = UUID("95000000-0000-4000-8000-000000000002")
REVISION_ID = UUID("95000000-0000-4000-8000-000000000003")
DEFINITION_ID = UUID("95000000-0000-4000-8000-000000000004")
SNAPSHOT_A = UUID("95000000-0000-4000-8000-000000000010")
SNAPSHOT_B = UUID("95000000-0000-4000-8000-000000000009")
SIBLING_SNAPSHOT = UUID("95000000-0000-4000-8000-000000000011")
WORKSPACE_ID = UUID("95000000-0000-4000-8000-000000000040")
PLAN_ID = UUID("95000000-0000-4000-8000-000000000041")
PROMOTION_ID = UUID("95000000-0000-4000-8000-000000000042")
HANDOFF_ID = UUID("95000000-0000-4000-8000-000000000043")
VERIFICATION_ID = UUID("95000000-0000-4000-8000-000000000044")
VERIFICATION_DEPLOYMENT_ID = UUID("95000000-0000-4000-8000-000000000045")
CONTAINER_ID = "a" * 64
IMAGE_ID = "sha256:" + "b" * 64
NOW = datetime(2026, 9, 16, tzinfo=UTC)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'instance-delete.db'}")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    with result.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="delete-test", kodi_version="21.3"
            )
        )
        session.add(
            RuntimeRevisionRow(
                id=str(REVISION_ID),
                runtime_definition_id=str(DEFINITION_ID),
                revision_number=1,
                source_kind="test",
                source_reference="test",
                content_digest="sha256:" + "c" * 64,
                manifest_reference="test",
                manifest_digest="sha256:" + "d" * 64,
                built_at="2026-09-16T00:00:00.000000Z",
                build_log_reference=None,
                approval_state="approved",
                approved_at="2026-09-16T00:00:00.000000Z",
                approval_note=None,
                requires_trusted_build_confirmation=False,
                trusted_build_confirmed_at=None,
            )
        )
        for identity, name, notes in (
            (INSTANCE_ID, "deletion source", "  exact notes 🐍  "),
            (SIBLING_ID, "sibling", None),
        ):
            session.add(
                TargetRow(
                    id=str(identity),
                    name=name,
                    target_type="managed",
                    kodi_version="21.3",
                    availability="available",
                    health="healthy",
                    connection_state="connected",
                )
            )
            session.add(
                ManagedInstanceRow(
                    target_id=str(identity),
                    instance_type="disposable",
                    runtime_revision_id=str(REVISION_ID),
                    runtime_state="stopped",
                    start_on_host_boot=False,
                    notes=notes,
                )
            )
        session.add(
            DevelopmentSourceRow(
                id="95000000-0000-4000-8000-000000000030",
                logical_name="Deletion attachment",
                source_kind="local_directory",
                relative_locator="sources/deletion",
                addon_id="plugin.video.deletion-attachment",
                observed_name="Deletion attachment",
                observed_version="1.0.0",
                extension_types_json='["xbmc.python.pluginsource"]',
                registered_at="2026-09-16T00:00:00.000000Z",
                validated_at="2026-09-16T00:00:00.000000Z",
            )
        )
        session.add(
            ManagedInstanceDevelopmentSourceRow(
                managed_instance_id=str(INSTANCE_ID),
                development_source_id="95000000-0000-4000-8000-000000000030",
                attached_at="2026-09-16T00:00:00.000000Z",
            )
        )
        session.add(
            DevelopmentSourceAttachmentOperationRow(
                id="95000000-0000-4000-8000-000000000031",
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                development_source_id="95000000-0000-4000-8000-000000000030",
                source_logical_name="Deletion attachment",
                addon_id="plugin.video.deletion-attachment",
                kind="attach",
                requested_at="2026-09-16T00:00:00.000000Z",
                completed_at="2026-09-16T00:00:00.000000Z",
                state="succeeded",
            )
        )
        session.add(
            DevelopmentSourceDeploymentOperationRow(
                id="95000000-0000-4000-8000-000000000032",
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                development_source_id="95000000-0000-4000-8000-000000000030",
                source_logical_name="Deletion attachment",
                addon_id="plugin.video.deletion-attachment",
                runtime_revision_id=str(REVISION_ID),
                request_kind=DevelopmentSourceDeploymentRequestKind.COPY_SYNC,
                requested_at="2026-09-16T00:00:00.000000Z",
                state=DevelopmentSourceDeploymentOperationState.SUCCEEDED,
                phase=DevelopmentSourceDeploymentOperationPhase.FINALIZE,
                result_classification=DevelopmentSourceDeploymentResult.INITIAL_DEPLOYMENT,
                new_count=1,
                changed_count=0,
                unchanged_count=0,
                destination_only_count=0,
                transferred_bytes=10,
                expected_container_id=CONTAINER_ID,
                started_at="2026-09-16T00:00:00.000000Z",
                heartbeat_at="2026-09-16T00:00:00.000000Z",
                completed_at="2026-09-16T00:00:01.000000Z",
                failure_reason=None,
                reconciliation_required=False,
                lease_token_digest=None,
                lease_expires_at=None,
            )
        )
        session.add(
            DevelopmentSourceDeploymentRow(
                managed_instance_id=str(INSTANCE_ID),
                development_source_id="95000000-0000-4000-8000-000000000030",
                addon_id="plugin.video.deletion-attachment",
                mode="copy",
                last_successful_operation_id=("95000000-0000-4000-8000-000000000032"),
                source_manifest_digest="sha256:" + "1" * 64,
                verified_destination_digest="sha256:" + "2" * 64,
                runtime_revision_id=str(REVISION_ID),
                trusted_container_id=CONTAINER_ID,
                observed_source_name="Deletion attachment",
                observed_source_version="1.0.0",
                verified_at="2026-09-16T00:00:01.000000Z",
                confidence=DevelopmentSourceDeploymentConfidence.VERIFIED_AT_LAST_OPERATION,
            )
        )
        session.add(
            DevelopmentSourceEnablementOperationRow(
                id="95000000-0000-4000-8000-000000000033",
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                development_source_id="95000000-0000-4000-8000-000000000030",
                source_logical_name="Deletion attachment",
                addon_id="plugin.video.deletion-attachment",
                addon_version="1.0.0",
                runtime_revision_id=str(REVISION_ID),
                requested_at="2026-09-16T00:00:02.000000Z",
                started_at="2026-09-16T00:00:02.000000Z",
                completed_at="2026-09-16T00:00:02.000000Z",
                state=DevelopmentSourceEnablementOperationState.SUCCEEDED,
                phase=DevelopmentSourceEnablementOperationPhase.FINALIZE,
                result_classification=DevelopmentSourceEnablementResult.ENABLED,
                prior_enabled=False,
                result_enabled=True,
                enable_performed=True,
                reconciliation_required=False,
            )
        )
        for identity, owner, label in (
            (SNAPSHOT_A, INSTANCE_ID, "A"),
            (SNAPSHOT_B, INSTANCE_ID, "B"),
            (SIBLING_SNAPSHOT, SIBLING_ID, "sibling"),
        ):
            SnapshotRepository(session).create(
                Snapshot(
                    id=identity,
                    source_managed_instance_id=owner,
                    runtime_revision_id=REVISION_ID,
                    captured_fixture_definition_id=None,
                    captured_at=NOW,
                    prior_runtime_state=InstanceRuntimeState.STOPPED,
                    label=label,
                    manifest_version=1,
                    archive_digest="sha256:" + "e" * 64,
                    manifest_digest="sha256:" + "f" * 64,
                    archive_size=10,
                    member_count=1,
                    state=SnapshotState.AVAILABLE,
                    validation_at=NOW,
                    validation_result=SnapshotValidationResult.PASSED,
                )
            )
        session.add(
            ResetOperationRow(
                id="95000000-0000-4000-8000-000000000020",
                managed_instance_id=str(INSTANCE_ID),
                requested_at="2026-09-16T00:00:00.000000Z",
                completed_at="2026-09-16T00:01:00.000000Z",
                state="succeeded",
                phase="succeeded",
                reconciliation_required=False,
            )
        )
    yield result
    engine.dispose()


def make_artifacts(data_root: Path) -> dict[UUID, bytes]:
    root = data_root / "snapshots"
    root.mkdir(parents=True, mode=0o750)
    os.chmod(root, 0o750)
    result: dict[UUID, bytes] = {}
    for identity in (SNAPSHOT_A, SNAPSHOT_B, SIBLING_SNAPSHOT):
        directory = snapshot_directory(data_root, identity)
        directory.mkdir(mode=0o750)
        payload = f"artifact-{identity}".encode()
        (directory / "state.tar").write_bytes(payload)
        os.chmod(directory / "state.tar", 0o440)
        result[identity] = payload
    return result


def add_terminal_phase9_history(
    factory: sessionmaker[Session], *, verification_state: str
) -> None:
    consumed = verification_state == "succeeded"
    with factory.begin() as session:
        session.add(
            AgentWorkspaceRow(
                id=str(WORKSPACE_ID),
                development_source_id="95000000-0000-4000-8000-000000000030",
                source_addon_id="plugin.video.deletion-attachment",
                source_logical_name="Deletion attachment",
                source_observed_name="Deletion attachment",
                source_observed_version="1.0.0",
                state="promoted",
                bytes_state="present",
                baseline_artifact_id="95000000-0000-4000-8000-000000000046",
                baseline_manifest_digest="sha256:" + "3" * 64,
                generation=1,
                current_tree_digest="sha256:" + "4" * 64,
                capture_started_at=NOW.isoformat(),
                captured_at=NOW.isoformat(),
                created_at=NOW.isoformat(),
                updated_at=NOW.isoformat(),
                promoted_at=NOW.isoformat(),
            )
        )
        session.add(
            AgentWorkspacePromotionPlanRow(
                id=str(PLAN_ID),
                workspace_id=str(WORKSPACE_ID),
                development_source_id="95000000-0000-4000-8000-000000000030",
                original_target_instance_id=str(INSTANCE_ID),
                target_instance_id=str(INSTANCE_ID),
                original_target_name="deletion source",
                workspace_generation=1,
                workspace_digest="sha256:" + "4" * 64,
                baseline_artifact_id="95000000-0000-4000-8000-000000000046",
                baseline_digest="sha256:" + "3" * 64,
                source_capture_digest="sha256:" + "5" * 64,
                addon_id="plugin.video.deletion-attachment",
                current_version="1.0.0",
                promoted_name="Deletion attachment",
                promoted_version="1.0.1",
                target_runtime_revision_id=str(REVISION_ID),
                target_container_id=CONTAINER_ID,
                target_deployment_operation_id=("95000000-0000-4000-8000-000000000032"),
                target_source_digest="sha256:" + "1" * 64,
                target_destination_digest="sha256:" + "2" * 64,
                expected_enabled=True,
                changed_file_count=1,
                review_text_bytes=10,
                created_at=NOW.isoformat(),
                reviewable_at=NOW.isoformat(),
                expires_at=NOW.isoformat(),
                state="consumed",
                source_operation_id=str(PROMOTION_ID),
            )
        )
        session.flush()
        session.add(
            AgentWorkspacePromotionOperationRow(
                id=str(PROMOTION_ID),
                plan_id=str(PLAN_ID),
                workspace_id=str(WORKSPACE_ID),
                development_source_id="95000000-0000-4000-8000-000000000030",
                original_target_instance_id=str(INSTANCE_ID),
                target_instance_id=str(INSTANCE_ID),
                state="succeeded",
                phase="finalize",
                requested_at=NOW.isoformat(),
                application_started=True,
                backup_artifact_id="95000000-0000-4000-8000-000000000047",
                backup_verified=True,
                started_at=NOW.isoformat(),
                completed_at=NOW.isoformat(),
                reconciliation_required=False,
            )
        )
        session.add(
            DevelopmentSourceDeploymentOperationRow(
                id=str(VERIFICATION_DEPLOYMENT_ID),
                original_managed_instance_id=str(INSTANCE_ID),
                managed_instance_id=str(INSTANCE_ID),
                development_source_id="95000000-0000-4000-8000-000000000030",
                source_logical_name="Deletion attachment",
                addon_id="plugin.video.deletion-attachment",
                runtime_revision_id=str(REVISION_ID),
                request_kind="copy_sync",
                requested_at=NOW.isoformat(),
                started_at=NOW.isoformat(),
                heartbeat_at=NOW.isoformat(),
                completed_at=NOW.isoformat(),
                state="succeeded" if consumed else "failed",
                phase="finalize",
                result_classification="synchronized" if consumed else None,
                reconciliation_required=False,
            )
        )
        session.flush()
        session.add(
            AgentWorkspacePromotionHandoffRow(
                id=str(HANDOFF_ID),
                operation_id=str(PROMOTION_ID),
                workspace_id=str(WORKSPACE_ID),
                development_source_id="95000000-0000-4000-8000-000000000030",
                original_target_instance_id=str(INSTANCE_ID),
                target_instance_id=str(INSTANCE_ID),
                runtime_revision_id=str(REVISION_ID),
                trusted_container_id=CONTAINER_ID,
                prior_deployment_operation_id=("95000000-0000-4000-8000-000000000032"),
                prior_source_digest="sha256:" + "1" * 64,
                prior_destination_digest="sha256:" + "2" * 64,
                created_at=NOW.isoformat(),
                expected_addon_id="plugin.video.deletion-attachment",
                expected_name="Deletion attachment",
                expected_version="1.0.1",
                expected_enabled=True,
                verification_operation_id=str(VERIFICATION_ID),
                consumed_at=NOW.isoformat() if consumed else None,
            )
        )
        session.flush()
        session.add(
            PromotionVerificationOperationRow(
                id=str(VERIFICATION_ID),
                source_promotion_id=str(PROMOTION_ID),
                handoff_id=str(HANDOFF_ID),
                workspace_id=str(WORKSPACE_ID),
                development_source_id="95000000-0000-4000-8000-000000000030",
                original_target_instance_id=str(INSTANCE_ID),
                target_instance_id=str(INSTANCE_ID),
                runtime_revision_id=str(REVISION_ID),
                deployment_operation_id=str(VERIFICATION_DEPLOYMENT_ID),
                state=verification_state,
                phase="finalize",
                addon_id="plugin.video.deletion-attachment",
                expected_name="Deletion attachment",
                expected_version="1.0.1",
                expected_enabled=True,
                sync_result="succeeded" if consumed else "failed",
                reload_result="succeeded" if consumed else "pending",
                inventory_result="succeeded" if consumed else "pending",
                log_result="clean" if consumed else "pending",
                target_error_count=0,
                unrelated_warning_count=0,
                stop_completed=consumed,
                sync_started=consumed,
                sync_completed=consumed,
                start_dispatched=consumed,
                handoff_consumed=consumed,
                requested_at=NOW.isoformat(),
                started_at=NOW.isoformat(),
                completed_at=NOW.isoformat(),
                failure_code=(
                    None if consumed else f"promotion_verification_{verification_state}"
                ),
            )
        )
        session.add(
            PromotionVerificationLogEvidenceRow(
                verification_operation_id=str(VERIFICATION_ID),
                target_error_snippets_json='["bounded retained snippet"]',
            )
        )


@dataclass
class FakeBroker:
    data_root: Path
    state: ContainerRuntimeState | None = None
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def managed_container_inventory(self) -> ManagedContainerInventoryResult:
        containers = []
        if self.state is not None:
            containers.append(
                ManagedContainerInventoryEntry(
                    managed_instance_id=str(INSTANCE_ID),
                    container_id=CONTAINER_ID,
                    image_id=IMAGE_ID,
                    runtime_state=self.state,
                    health_state=ContainerHealthState.NOT_CONFIGURED,
                )
            )
        return ManagedContainerInventoryResult(containers=containers)

    def stop_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_state: ContainerRuntimeState,
    ) -> StopManagedContainerResult:
        assert managed_instance_id == str(INSTANCE_ID)
        assert expected_container_id == CONTAINER_ID
        assert expected_runtime_state is ContainerRuntimeState.RUNNING
        self.calls.append(("stop", None))
        self.state = ContainerRuntimeState.STOPPED
        return StopManagedContainerResult(
            managed_instance_id=managed_instance_id,
            container_id=CONTAINER_ID,
            runtime_state=ContainerRuntimeState.STOPPED,
            stop_performed=True,
        )

    def deprovision_managed_container(
        self,
        *,
        managed_instance_id: str,
        expected_container_id: str,
        expected_runtime_revision_id: str,
        expected_runtime_state: Literal[ContainerRuntimeState.STOPPED],
    ) -> DeprovisionManagedContainerResult:
        assert expected_runtime_revision_id == str(REVISION_ID)
        assert expected_runtime_state is ContainerRuntimeState.STOPPED
        self.calls.append(("deprovision", None))
        self.state = None
        return DeprovisionManagedContainerResult(
            managed_instance_id=managed_instance_id,
            removed_container_id=expected_container_id,
            runtime_revision_id=expected_runtime_revision_id,
            removed=True,
        )

    def delete_managed_snapshot_artifact(
        self, *, snapshot_id: str
    ) -> DeleteManagedSnapshotArtifactResult:
        identity = UUID(snapshot_id)
        root = self.data_root / "snapshots"
        metadata = root.stat()
        present = delete_snapshot_artifact(
            data_root=self.data_root,
            snapshot_id=identity,
            owner_uid=metadata.st_uid,
            owner_gid=metadata.st_gid,
        )
        self.calls.append(("delete", snapshot_id))
        return DeleteManagedSnapshotArtifactResult(
            snapshot_id=snapshot_id,
            artifact_absent=True,
            artifact_was_present=present,
        )


def execute(
    factory: sessionmaker[Session], broker: FakeBroker, *, preserve: bool
) -> UUID:
    coordinator = ManagedInstanceDeleteOperationCoordinator(factory)
    queued = coordinator.queue(INSTANCE_ID, preserve_snapshots=preserve)
    lease = coordinator.acquire(queued.operation_id)
    assert lease is not None
    readiness = ReadinessTaskRegistry(max_workers=1)
    try:
        DisposableManagedInstanceDeletionService(
            factory,
            cast(InstanceDeleteBroker, broker),
            readiness,
            coordinator,
            lease,
            data_root=broker.data_root,
        ).delete()
    finally:
        readiness.close()
    return queued.operation_id


def test_preserve_detaches_atomically_and_keeps_bytes_and_exact_notes(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    expected = make_artifacts(tmp_path)
    broker = FakeBroker(tmp_path)
    operation_id = execute(factory, broker, preserve=True)

    with factory() as session:
        assert ManagedInstanceRepository(session).get(INSTANCE_ID) is None
        snapshots = SnapshotRepository(session).list_for_original_instance(INSTANCE_ID)
        operation = session.get(ManagedInstanceDeleteOperationRow, str(operation_id))
        reset_history = session.get(
            ResetOperationRow, "95000000-0000-4000-8000-000000000020"
        )
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None
        assert (
            session.get(
                ManagedInstanceDevelopmentSourceRow,
                (str(INSTANCE_ID), "95000000-0000-4000-8000-000000000030"),
            )
            is None
        )
        assert (
            session.get(
                DevelopmentSourceRow,
                "95000000-0000-4000-8000-000000000030",
            )
            is not None
        )
        attachment_history = session.get(
            DevelopmentSourceAttachmentOperationRow,
            "95000000-0000-4000-8000-000000000031",
        )
        deployment_evidence = session.get(
            DevelopmentSourceDeploymentRow,
            (str(INSTANCE_ID), "95000000-0000-4000-8000-000000000030"),
        )
        deployment_history = session.get(
            DevelopmentSourceDeploymentOperationRow,
            "95000000-0000-4000-8000-000000000032",
        )
        enablement_history = session.get(
            DevelopmentSourceEnablementOperationRow,
            "95000000-0000-4000-8000-000000000033",
        )
    assert len(snapshots) == 2
    assert all(item.source_managed_instance_id is None for item in snapshots)
    assert all(
        item.source_binding is SnapshotSourceBinding.DELETED_SOURCE
        for item in snapshots
    )
    assert all(item.source_instance_name == "deletion source" for item in snapshots)
    assert all(item.source_deleted_at is not None for item in snapshots)
    assert operation is not None
    assert attachment_history is not None
    assert attachment_history.managed_instance_id is None
    assert deployment_evidence is None
    assert deployment_history is not None
    assert deployment_history.managed_instance_id is None
    assert deployment_history.original_managed_instance_id == str(INSTANCE_ID)
    assert enablement_history is not None
    assert enablement_history.managed_instance_id is None
    assert enablement_history.original_managed_instance_id == str(INSTANCE_ID)
    assert operation.state is ManagedInstanceDeleteOperationState.SUCCEEDED
    assert operation.notes_at_deletion == "  exact notes 🐍  "
    assert reset_history is not None
    assert reset_history.managed_instance_id == str(INSTANCE_ID)
    assert operation.completed_snapshot_count == 2
    for identity in (SNAPSHOT_A, SNAPSHOT_B, SIBLING_SNAPSHOT):
        assert (snapshot_directory(tmp_path, identity) / "state.tar").read_bytes() == (
            expected[identity]
        )


@pytest.mark.parametrize("verification_state", ["succeeded", "failed", "incomplete"])
def test_terminal_phase9_history_survives_instance_deletion(
    factory: sessionmaker[Session], tmp_path: Path, verification_state: str
) -> None:
    make_artifacts(tmp_path)
    add_terminal_phase9_history(factory, verification_state=verification_state)

    execute(factory, FakeBroker(tmp_path), preserve=True)

    with factory() as session:
        plan = session.get(AgentWorkspacePromotionPlanRow, str(PLAN_ID))
        promotion = session.get(AgentWorkspacePromotionOperationRow, str(PROMOTION_ID))
        handoff = session.get(AgentWorkspacePromotionHandoffRow, str(HANDOFF_ID))
        verification = session.get(
            PromotionVerificationOperationRow, str(VERIFICATION_ID)
        )
        evidence = session.get(
            PromotionVerificationLogEvidenceRow, str(VERIFICATION_ID)
        )
        workspace = session.get(AgentWorkspaceRow, str(WORKSPACE_ID))
        assert plan is not None and plan.target_instance_id is None
        assert plan.original_target_instance_id == str(INSTANCE_ID)
        assert plan.original_target_name == "deletion source"
        assert promotion is not None and promotion.target_instance_id is None
        assert promotion.original_target_instance_id == str(INSTANCE_ID)
        assert handoff is not None and handoff.target_instance_id is None
        assert handoff.original_target_instance_id == str(INSTANCE_ID)
        assert verification is not None and verification.target_instance_id is None
        assert verification.original_target_instance_id == str(INSTANCE_ID)
        assert verification.state.value == verification_state
        assert evidence is not None
        assert workspace is not None and workspace.state.value == "promoted"
        assert workspace.bytes_state.value == "present"

    class Inventory:
        def get(self, _instance_id: UUID) -> object:
            raise AssertionError("deleted history projection must not query Kodi")

    status = PromotionVerificationService(
        factory, cast(Any, FakeBroker(tmp_path)), Inventory()
    ).get(VERIFICATION_ID)
    assert status.target_instance_id == INSTANCE_ID
    assert status.target_name == "deletion source"
    assert not status.target_available


@pytest.mark.parametrize(
    "blocking_state", ["queued", "running", "reconciliation_required"]
)
def test_active_or_unresolved_phase9_verification_blocks_instance_deletion(
    factory: sessionmaker[Session], blocking_state: str
) -> None:
    add_terminal_phase9_history(factory, verification_state="succeeded")
    with factory.begin() as session:
        if blocking_state in {"queued", "running"}:
            session.execute(
                update(PromotionVerificationOperationRow).values(
                    state=blocking_state,
                    phase="queued" if blocking_state == "queued" else "preflight",
                    sync_result="pending",
                    reload_result="pending",
                    inventory_result="pending",
                    log_result="pending",
                    stop_completed=False,
                    sync_started=False,
                    sync_completed=False,
                    start_dispatched=False,
                    handoff_consumed=False,
                    started_at=None if blocking_state == "queued" else NOW.isoformat(),
                    completed_at=None,
                    failure_code=None,
                    lease_token_digest=(
                        None if blocking_state == "queued" else "a" * 64
                    ),
                    lease_expires_at=(
                        None if blocking_state == "queued" else NOW.isoformat()
                    ),
                )
            )
            session.execute(
                update(AgentWorkspacePromotionHandoffRow).values(consumed_at=None)
            )
        else:
            session.execute(
                update(PromotionVerificationOperationRow).values(
                    state="reconciliation_required",
                    failure_code="promotion_verification_reconciliation_required",
                )
            )

    with pytest.raises(
        (
            ManagedInstanceDeleteConflictError,
            ManagedInstanceReconciliationRequiredError,
        )
    ):
        ManagedInstanceDeleteOperationCoordinator(factory).queue(
            INSTANCE_ID, preserve_snapshots=True
        )
    with factory() as session:
        assert ManagedInstanceRepository(session).get(INSTANCE_ID) is not None
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None


def test_delete_is_container_first_artifact_first_deterministic_and_isolated(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    expected = make_artifacts(tmp_path)
    broker = FakeBroker(tmp_path, ContainerRuntimeState.RUNNING)
    operation_id = execute(factory, broker, preserve=False)

    assert broker.calls == [
        ("stop", None),
        ("deprovision", None),
        ("delete", str(SNAPSHOT_B)),
        ("delete", str(SNAPSHOT_A)),
    ]
    with factory() as session:
        assert ManagedInstanceRepository(session).get(INSTANCE_ID) is None
        assert SnapshotRepository(session).get(SNAPSHOT_A) is None
        assert SnapshotRepository(session).get(SNAPSHOT_B) is None
        assert SnapshotRepository(session).get(SIBLING_SNAPSHOT) is not None
        operation = session.get(ManagedInstanceDeleteOperationRow, str(operation_id))
        nested = session.scalar(
            select(func.count()).select_from(SnapshotDeleteOperationRow)
        )
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None
        assert (
            session.get(
                ManagedInstanceDevelopmentSourceRow,
                (str(INSTANCE_ID), "95000000-0000-4000-8000-000000000030"),
            )
            is None
        )
        assert (
            session.get(
                DevelopmentSourceRow,
                "95000000-0000-4000-8000-000000000030",
            )
            is not None
        )
        attachment_history = session.get(
            DevelopmentSourceAttachmentOperationRow,
            "95000000-0000-4000-8000-000000000031",
        )
    assert operation is not None
    assert attachment_history is not None
    assert attachment_history.managed_instance_id is None
    assert operation.completed_snapshot_count == 2
    assert nested == 0
    assert not snapshot_directory(tmp_path, SNAPSHOT_A).exists()
    assert not snapshot_directory(tmp_path, SNAPSHOT_B).exists()
    assert (
        snapshot_directory(tmp_path, SIBLING_SNAPSHOT) / "state.tar"
    ).read_bytes() == (expected[SIBLING_SNAPSHOT])


def test_instance_delete_and_snapshot_delete_are_bidirectionally_exclusive(
    factory: sessionmaker[Session],
) -> None:
    instance = ManagedInstanceDeleteOperationCoordinator(factory)
    instance.queue(INSTANCE_ID, preserve_snapshots=True)
    with pytest.raises(SnapshotDeleteConflictError):
        SnapshotDeleteOperationCoordinator(factory).queue(INSTANCE_ID, SNAPSHOT_A)


def test_active_snapshot_delete_blocks_instance_delete(
    factory: sessionmaker[Session],
) -> None:
    SnapshotDeleteOperationCoordinator(factory).queue(INSTANCE_ID, SNAPSHOT_A)
    with pytest.raises(ManagedInstanceDeleteConflictError):
        ManagedInstanceDeleteOperationCoordinator(factory).queue(
            INSTANCE_ID, preserve_snapshots=True
        )


@pytest.mark.parametrize(
    "kind",
    [
        ManagedInstanceMutationKind.RESET,
        ManagedInstanceMutationKind.SNAPSHOT_CREATE,
        ManagedInstanceMutationKind.SNAPSHOT_RESTORE,
        ManagedInstanceMutationKind.LIFECYCLE,
        ManagedInstanceMutationKind.FIXTURE_ASSIGNMENT,
        ManagedInstanceMutationKind.RUNTIME_REASSIGNMENT,
        ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ATTACHMENT,
        ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_DEPLOYMENT,
        ManagedInstanceMutationKind.DEVELOPMENT_SOURCE_ENABLEMENT,
    ],
)
def test_existing_instance_mutation_blocks_delete(
    factory: sessionmaker[Session], kind: ManagedInstanceMutationKind
) -> None:
    with factory.begin() as session:
        ManagedInstanceMutationLeaseRepository(session).reserve(
            INSTANCE_ID,
            kind=kind,
            operation_id=UUID("95000000-0000-4000-8000-000000000099"),
        )
    with pytest.raises(ManagedInstanceDeleteConflictError):
        ManagedInstanceDeleteOperationCoordinator(factory).queue(
            INSTANCE_ID, preserve_snapshots=True
        )


def test_transitional_snapshot_blocks_before_any_reservation(
    factory: sessionmaker[Session],
) -> None:
    with factory.begin() as session:
        session.execute(
            update(SnapshotRow)
            .where(SnapshotRow.id == str(SNAPSHOT_A))
            .values(state=SnapshotState.DELETING)
        )
    with pytest.raises(ManagedInstanceDeleteConflictError):
        ManagedInstanceDeleteOperationCoordinator(factory).queue(
            INSTANCE_ID, preserve_snapshots=False
        )
    with factory() as session:
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None


def test_detached_restore_rejects_before_any_lease(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    make_artifacts(tmp_path)
    execute(factory, FakeBroker(tmp_path), preserve=True)
    with pytest.raises(
        SnapshotSourceInstanceDeletedError, match="source_instance_deleted"
    ):
        SnapshotRestoreOperationCoordinator(factory).queue(INSTANCE_ID, SNAPSHOT_A)
    with factory() as session:
        assert session.get(ManagedInstanceMutationLeaseRow, str(INSTANCE_ID)) is None
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None


def test_preserve_queue_does_not_reserve_heavy_but_delete_queue_does(
    factory: sessionmaker[Session],
) -> None:
    ManagedInstanceDeleteOperationCoordinator(factory).queue(
        INSTANCE_ID, preserve_snapshots=True
    )
    with factory() as session:
        assert session.get(GlobalHeavyOperationLeaseRow, 1) is None


def test_delete_queue_reserves_typed_global_heavy(
    factory: sessionmaker[Session],
) -> None:
    ManagedInstanceDeleteOperationCoordinator(factory).queue(
        INSTANCE_ID, preserve_snapshots=False
    )
    with factory() as session:
        heavy = session.get(GlobalHeavyOperationLeaseRow, 1)
    assert heavy is not None
    assert heavy.operation_kind is GlobalHeavyOperationKind.INSTANCE_DELETE


@pytest.mark.parametrize("preserve", [True, False])
def test_zero_snapshot_unprovisioned_deletion_preserves_none_notes(
    factory: sessionmaker[Session], tmp_path: Path, preserve: bool
) -> None:
    with factory.begin() as session:
        session.execute(
            delete(SnapshotRow).where(
                SnapshotRow.source_managed_instance_id == str(INSTANCE_ID)
            )
        )
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert instance is not None
        instance.notes = None
    operation_id = execute(factory, FakeBroker(tmp_path), preserve=preserve)

    with factory() as session:
        operation = session.get(ManagedInstanceDeleteOperationRow, str(operation_id))
    assert operation is not None
    assert operation.snapshot_count == 0
    assert operation.completed_snapshot_count == 0
    assert operation.notes_at_deletion is None
    assert operation.prior_state == "unprovisioned"


@pytest.mark.parametrize("preserve", [True, False])
def test_failed_and_quarantined_snapshots_follow_explicit_policy(
    factory: sessionmaker[Session], tmp_path: Path, preserve: bool
) -> None:
    make_artifacts(tmp_path)
    with factory.begin() as session:
        first = session.get(SnapshotRow, str(SNAPSHOT_A))
        second = session.get(SnapshotRow, str(SNAPSHOT_B))
        assert first is not None and second is not None
        first.state = SnapshotState.FAILED
        first.validation_result = SnapshotValidationResult.FAILED
        second.state = SnapshotState.QUARANTINED
        second.validation_result = SnapshotValidationResult.FAILED

    execute(factory, FakeBroker(tmp_path), preserve=preserve)

    with factory() as session:
        remaining = SnapshotRepository(session).list_for_original_instance(INSTANCE_ID)
    if preserve:
        assert {item.state for item in remaining} == {
            SnapshotState.FAILED,
            SnapshotState.QUARANTINED,
        }
        assert all(
            item.source_binding is SnapshotSourceBinding.DELETED_SOURCE
            for item in remaining
        )
    else:
        assert remaining == []
        assert not snapshot_directory(tmp_path, SNAPSHOT_A).exists()
        assert not snapshot_directory(tmp_path, SNAPSHOT_B).exists()


@pytest.mark.parametrize(
    ("initial_state", "expected_calls"),
    [
        (ContainerRuntimeState.STOPPED, [("deprovision", None)]),
        (
            ContainerRuntimeState.RUNNING,
            [("stop", None), ("deprovision", None)],
        ),
    ],
)
def test_preserve_removes_stopped_or_running_container_before_detach(
    factory: sessionmaker[Session],
    tmp_path: Path,
    initial_state: ContainerRuntimeState,
    expected_calls: list[tuple[str, str | None]],
) -> None:
    make_artifacts(tmp_path)
    broker = FakeBroker(tmp_path, initial_state)

    execute(factory, broker, preserve=True)

    assert broker.calls == expected_calls
    with factory() as session:
        assert all(
            item.source_binding is SnapshotSourceBinding.DELETED_SOURCE
            for item in SnapshotRepository(session).list_for_original_instance(
                INSTANCE_ID
            )
        )
