from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.activity import (
    ManagedInstanceActivityKind,
    ManagedInstanceActivityService,
)
from kodi_lab.domain import (
    DevelopmentSourceAttachmentOperationKind,
    DevelopmentSourceAttachmentOperationState,
    DevelopmentSourceDeploymentOperationPhase,
    DevelopmentSourceDeploymentOperationState,
    DevelopmentSourceDeploymentRequestKind,
    DevelopmentSourceDeploymentResult,
    DevelopmentSourceEnablementOperationPhase,
    DevelopmentSourceEnablementOperationState,
    DevelopmentSourceEnablementResult,
    ManagedInstanceDeleteOperationPhase,
    ManagedInstanceDeleteOperationState,
    ManagedInstanceDeletePriorState,
    ManagedInstanceLifecycleKind,
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceSemanticState,
    ResetOperationPhase,
    ResetOperationState,
    SnapshotCreateOperationPhase,
    SnapshotCreateOperationState,
    SnapshotDeleteOperationPhase,
    SnapshotDeleteOperationState,
    SnapshotRestoreOperationPhase,
    SnapshotRestoreOperationState,
    SnapshotState,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    DevelopmentSourceAttachmentOperationRow,
    DevelopmentSourceDeploymentOperationRow,
    DevelopmentSourceEnablementOperationRow,
    DevelopmentSourceRow,
    ManagedInstanceDeleteOperationRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceRow,
    ResetOperationRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    SnapshotCreateOperationRow,
    SnapshotDeleteOperationRow,
    SnapshotRestoreOperationRow,
    TargetRow,
)

INSTANCE_ID = UUID("97000000-0000-4000-8000-000000000001")
REVISION_ID = UUID("97000000-0000-4000-8000-000000000002")
DEFINITION_ID = UUID("97000000-0000-4000-8000-000000000003")
OPERATION_ID = UUID("97000000-0000-4000-8000-000000000010")
SNAPSHOT_ID = UUID("97000000-0000-4000-8000-000000000011")
AT = "2026-09-16T12:00:00.000000Z"


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'activity.db'}")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    with result.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION_ID), slug="kodi-21-3", kodi_version="21.3"
            )
        )
        session.add(
            RuntimeRevisionRow(
                id=str(REVISION_ID),
                runtime_definition_id=str(DEFINITION_ID),
                revision_number=1,
                source_kind="test",
                source_reference="sensitive/source/path",
                content_digest="sha256:" + "c" * 64,
                manifest_reference="sensitive/manifest/path",
                manifest_digest="sha256:" + "d" * 64,
                built_at=AT,
                approval_state="approved",
                approved_at=AT,
                requires_trusted_build_confirmation=False,
            )
        )
        session.add(
            TargetRow(
                id=str(INSTANCE_ID),
                name="activity instance",
                target_type="managed",
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.flush()
        session.add(
            ManagedInstanceRow(
                target_id=str(INSTANCE_ID),
                instance_type="disposable",
                runtime_revision_id=str(REVISION_ID),
                runtime_state="stopped",
                start_on_host_boot=False,
                notes="never project this note",
            )
        )
    yield result
    engine.dispose()


def _seed_equal_timestamp_mixed_history(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        session.add(
            DevelopmentSourceRow(
                id="97000000-0000-4000-8000-000000000020",
                logical_name="Activity source",
                source_kind="local_directory",
                relative_locator="private/activity-source",
                addon_id="plugin.video.activity",
                observed_name="Activity source",
                observed_version="1.0.0",
                extension_types_json='["xbmc.python.pluginsource"]',
                registered_at=AT,
                validated_at=AT,
            )
        )
        session.flush()
        session.add_all(
            [
                DevelopmentSourceEnablementOperationRow(
                    id=str(OPERATION_ID),
                    original_managed_instance_id=str(INSTANCE_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    development_source_id=("97000000-0000-4000-8000-000000000020"),
                    source_logical_name="Activity source",
                    addon_id="plugin.video.activity",
                    addon_version="1.0.0",
                    runtime_revision_id=str(REVISION_ID),
                    requested_at=AT,
                    started_at=AT,
                    completed_at=AT,
                    state=DevelopmentSourceEnablementOperationState.SUCCEEDED,
                    phase=DevelopmentSourceEnablementOperationPhase.FINALIZE,
                    result_classification=DevelopmentSourceEnablementResult.ENABLED,
                    prior_enabled=False,
                    result_enabled=True,
                    enable_performed=True,
                    reconciliation_required=False,
                ),
                DevelopmentSourceDeploymentOperationRow(
                    id=str(OPERATION_ID),
                    original_managed_instance_id=str(INSTANCE_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    development_source_id=("97000000-0000-4000-8000-000000000020"),
                    source_logical_name="Activity source",
                    addon_id="plugin.video.activity",
                    runtime_revision_id=str(REVISION_ID),
                    request_kind=DevelopmentSourceDeploymentRequestKind.COPY_SYNC,
                    requested_at=AT,
                    state=DevelopmentSourceDeploymentOperationState.SUCCEEDED,
                    phase=DevelopmentSourceDeploymentOperationPhase.FINALIZE,
                    result_classification=DevelopmentSourceDeploymentResult.UNCHANGED,
                    new_count=0,
                    changed_count=0,
                    unchanged_count=3,
                    destination_only_count=1,
                    transferred_bytes=0,
                    expected_container_id="e" * 64,
                    started_at=AT,
                    heartbeat_at=AT,
                    completed_at=AT,
                    failure_reason=None,
                    reconciliation_required=False,
                    lease_token_digest=None,
                    lease_expires_at=None,
                ),
                DevelopmentSourceAttachmentOperationRow(
                    id=str(OPERATION_ID),
                    original_managed_instance_id=str(INSTANCE_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    development_source_id="97000000-0000-4000-8000-000000000020",
                    source_logical_name="Activity source",
                    addon_id="plugin.video.activity",
                    kind=DevelopmentSourceAttachmentOperationKind.ATTACH,
                    requested_at=AT,
                    completed_at=AT,
                    state=DevelopmentSourceAttachmentOperationState.SUCCEEDED,
                ),
                ManagedInstanceLifecycleOperationRow(
                    id=str(OPERATION_ID),
                    original_managed_instance_id=str(INSTANCE_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    instance_name="activity instance",
                    kind=ManagedInstanceLifecycleKind.START,
                    requested_at=AT,
                    started_at=AT,
                    completed_at=AT,
                    prior_state=ManagedInstanceSemanticState.STOPPED,
                    resulting_state=ManagedInstanceSemanticState.RUNNING_NOT_READY,
                    state=ManagedInstanceLifecycleOperationState.SUCCEEDED,
                    failure_reason=None,
                    reconciliation_required=False,
                ),
                ResetOperationRow(
                    id=str(OPERATION_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    requested_at=AT,
                    started_at=AT,
                    heartbeat_at=AT,
                    completed_at=AT,
                    state=ResetOperationState.RECONCILIATION_REQUIRED,
                    phase=ResetOperationPhase.RECONCILIATION_REQUIRED,
                    failure_reason="raw /secret/path container " + "a" * 64,
                    reconciliation_required=True,
                ),
                SnapshotCreateOperationRow(
                    id=str(OPERATION_ID),
                    snapshot_id=str(SNAPSHOT_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    requested_at=AT,
                    started_at=AT,
                    heartbeat_at=AT,
                    completed_at=AT,
                    state=SnapshotCreateOperationState.SUCCEEDED,
                    phase=SnapshotCreateOperationPhase.SUCCEEDED,
                    reconciliation_required=False,
                ),
                SnapshotRestoreOperationRow(
                    id=str(OPERATION_ID),
                    snapshot_id=str(SNAPSHOT_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    requested_at=AT,
                    started_at=AT,
                    heartbeat_at=AT,
                    completed_at=AT,
                    old_container_id="b" * 64,
                    fresh_container_id="c" * 64,
                    state=SnapshotRestoreOperationState.SUCCEEDED,
                    phase=SnapshotRestoreOperationPhase.SUCCEEDED,
                    reconciliation_required=False,
                ),
                SnapshotDeleteOperationRow(
                    id=str(OPERATION_ID),
                    snapshot_id=str(SNAPSHOT_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    prior_snapshot_state=SnapshotState.AVAILABLE,
                    requested_at=AT,
                    started_at=AT,
                    heartbeat_at=AT,
                    completed_at=AT,
                    state=SnapshotDeleteOperationState.FAILED,
                    phase=SnapshotDeleteOperationPhase.FAILED,
                    failure_reason="raw broker exception /secret/path",
                    reconciliation_required=False,
                ),
                ManagedInstanceDeleteOperationRow(
                    id=str(OPERATION_ID),
                    original_managed_instance_id=str(INSTANCE_ID),
                    source_instance_name="activity instance",
                    runtime_revision_id=str(REVISION_ID),
                    fixture_definition_id=None,
                    preserve_snapshots=True,
                    snapshot_count=0,
                    aggregate_snapshot_bytes=0,
                    completed_snapshot_count=0,
                    prior_state=ManagedInstanceDeletePriorState.STOPPED,
                    notes_at_deletion="deleted secret note",
                    requested_at=AT,
                    started_at=AT,
                    heartbeat_at=AT,
                    completed_at=AT,
                    state=ManagedInstanceDeleteOperationState.SUCCEEDED,
                    phase=ManagedInstanceDeleteOperationPhase.SUCCEEDED,
                    reconciliation_required=False,
                ),
            ]
        )


def test_mixed_projection_has_fixed_tie_order_bounded_mapping_and_redaction(
    factory: sessionmaker[Session],
) -> None:
    _seed_equal_timestamp_mixed_history(factory)
    items = ManagedInstanceActivityService(factory).list_for_original_instance(
        INSTANCE_ID
    )
    assert [item.kind for item in items] == [
        ManagedInstanceActivityKind.DEVELOPMENT_SOURCE_ENABLEMENT,
        ManagedInstanceActivityKind.DEVELOPMENT_SOURCE_DEPLOYMENT,
        ManagedInstanceActivityKind.DEVELOPMENT_SOURCE_ATTACHMENT,
        ManagedInstanceActivityKind.LIFECYCLE,
        ManagedInstanceActivityKind.RESET,
        ManagedInstanceActivityKind.SNAPSHOT_CREATE,
        ManagedInstanceActivityKind.SNAPSHOT_RESTORE,
        ManagedInstanceActivityKind.SNAPSHOT_DELETE,
        ManagedInstanceActivityKind.INSTANCE_DELETE,
    ]
    assert [item.summary for item in items] == [
        "Development source add-on enabled",
        "Development source copy unchanged",
        "Development source attached",
        "Start succeeded",
        "Reset reconciliation required",
        "Snapshot creation succeeded",
        "Snapshot restore succeeded",
        "Snapshot deletion failed",
        "Instance deletion preserved Snapshots",
    ]
    assert items[2].development_source_attachment_kind is (
        DevelopmentSourceAttachmentOperationKind.ATTACH
    )
    assert items[3].lifecycle_kind is ManagedInstanceLifecycleKind.START
    assert items[5].snapshot_uuid == SNAPSHOT_ID
    assert items[-1].preserve_snapshots is True
    serialized = " ".join(item.model_dump_json() for item in items)
    for forbidden in (
        "/secret/path",
        "deleted secret note",
        "container_id",
        "lease",
        "manifest",
        "source_reference",
        "private/activity-source",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "e" * 64,
    ):
        assert forbidden not in serialized


def test_projection_limit_is_explicit_and_full_history_is_not_silently_limited(
    factory: sessionmaker[Session],
) -> None:
    at_prefix = "2026-09-16T12:00:"
    with factory.begin() as session:
        for index in range(12):
            session.add(
                ManagedInstanceLifecycleOperationRow(
                    id=str(UUID(int=index + 1)),
                    original_managed_instance_id=str(INSTANCE_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    instance_name="activity instance",
                    kind=ManagedInstanceLifecycleKind.STOP,
                    requested_at=f"{at_prefix}{index:02d}.000000Z",
                    started_at=f"{at_prefix}{index:02d}.000000Z",
                    completed_at=f"{at_prefix}{index:02d}.000000Z",
                    prior_state=ManagedInstanceSemanticState.RUNNING_NOT_READY,
                    resulting_state=ManagedInstanceSemanticState.STOPPED,
                    state=ManagedInstanceLifecycleOperationState.SUCCEEDED,
                    reconciliation_required=False,
                )
            )
    service = ManagedInstanceActivityService(factory)
    assert len(service.list_for_live_instance(INSTANCE_ID, limit=10)) == 10
    full = service.list_for_live_instance(INSTANCE_ID)
    assert len(full) == 12
    assert full[0].operation_uuid == UUID(int=12)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        service.list_for_live_instance(INSTANCE_ID, limit=0)
