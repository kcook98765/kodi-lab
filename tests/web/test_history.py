from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import (
    InstanceRuntimeState,
    ManagedInstanceDeleteOperationPhase,
    ManagedInstanceDeleteOperationState,
    ManagedInstanceDeletePriorState,
    ManagedInstanceLifecycleKind,
    ManagedInstanceLifecycleOperationState,
    ManagedInstanceSemanticState,
    ResetOperationPhase,
    ResetOperationState,
    Snapshot,
    SnapshotCreateOperationPhase,
    SnapshotCreateOperationState,
    SnapshotDeleteOperationPhase,
    SnapshotDeleteOperationState,
    SnapshotRestoreOperationPhase,
    SnapshotRestoreOperationState,
    SnapshotSourceBinding,
    SnapshotState,
    SnapshotValidationResult,
)
from kodi_lab.storage.models import (
    ManagedInstanceDeleteOperationRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceRow,
    ResetOperationRow,
    SnapshotCreateOperationRow,
    SnapshotDeleteOperationRow,
    SnapshotRestoreOperationRow,
    TargetRow,
)
from kodi_lab.storage.repositories import SnapshotRepository

from .conftest import INSTANCE_ID, REVISION_ID, login

AT = "2026-09-16T12:00:00.000000Z"
SNAPSHOT_ID = UUID("98000000-0000-4000-8000-000000000001")


def _add_lifecycle_history(
    database: sessionmaker[Session], *, count: int, unsafe_reason: bool = False
) -> None:
    with database.begin() as session:
        for index in range(count):
            timestamp = f"2026-09-16T12:00:{index:02d}.000000Z"
            failed = unsafe_reason and index == count - 1
            session.add(
                ManagedInstanceLifecycleOperationRow(
                    id=str(UUID(int=index + 1)),
                    original_managed_instance_id=str(INSTANCE_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    instance_name="Kodi history",
                    kind=ManagedInstanceLifecycleKind.START,
                    requested_at=timestamp,
                    started_at=timestamp,
                    completed_at=timestamp,
                    prior_state=ManagedInstanceSemanticState.STOPPED,
                    resulting_state=ManagedInstanceSemanticState.RUNNING_NOT_READY,
                    state=(
                        ManagedInstanceLifecycleOperationState.FAILED
                        if failed
                        else ManagedInstanceLifecycleOperationState.SUCCEEDED
                    ),
                    failure_reason=(
                        "raw /var/lib/private container " + "a" * 64 if failed else None
                    ),
                    reconciliation_required=False,
                )
            )


def test_new_history_routes_require_authentication(client: TestClient) -> None:
    assert (
        client.get(f"/instances/{INSTANCE_ID}", follow_redirects=False).status_code
        == 303
    )
    assert (
        client.get(
            f"/instances/{INSTANCE_ID}/history", follow_redirects=False
        ).status_code
        == 303
    )
    assert client.get("/history/instances", follow_redirects=False).status_code == 303
    assert (
        client.get(
            f"/history/instances/{INSTANCE_ID}", follow_redirects=False
        ).status_code
        == 303
    )
    assert client.get(f"/api/instances/{INSTANCE_ID}/activity").status_code == 401


def test_live_detail_preserves_escaped_notes_and_shows_ten_recent_items(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    with database.begin() as session:
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        assert instance is not None
        instance.notes = "  first line\n<script>unsafe</script>\nlast line  "
    _add_lifecycle_history(database, count=12, unsafe_reason=True)
    login(client)
    detail = client.get(f"/instances/{INSTANCE_ID}")
    assert detail.status_code == 200
    assert detail.headers["Cache-Control"] == "no-store"
    assert "Kodi 21.3 · revision 2" in detail.text
    assert (
        "  first line\n&lt;script&gt;unsafe&lt;/script&gt;\nlast line  " in detail.text
    )
    assert "<script>unsafe</script>" not in detail.text
    assert detail.text.count('id="activity-') == 10
    assert "View Snapshots" in detail.text
    assert "View full retained history" in detail.text

    full = client.get(f"/instances/{INSTANCE_ID}/history")
    assert full.status_code == 200
    assert full.text.count('id="activity-') == 12
    api = client.get(f"/api/instances/{INSTANCE_ID}/activity")
    assert len(api.json()) == 12
    limited = client.get(f"/api/instances/{INSTANCE_ID}/activity?limit=10")
    assert len(limited.json()) == 10
    for response_text in (detail.text, full.text, api.text):
        for forbidden in (
            "/var/lib/private",
            "container_id",
            "lease_token",
            "manifest_digest",
            '"manifest":',
            "a" * 64,
        ):
            assert forbidden not in response_text


def _delete_instance_with_retained_history(
    database: sessionmaker[Session], *, preserve_snapshot: bool
) -> None:
    _add_lifecycle_history(database, count=1)
    with database.begin() as session:
        session.add_all(
            [
                ResetOperationRow(
                    id="98000000-0000-4000-8000-000000000003",
                    managed_instance_id=str(INSTANCE_ID),
                    requested_at=AT,
                    started_at=AT,
                    heartbeat_at=AT,
                    completed_at=AT,
                    state=ResetOperationState.SUCCEEDED,
                    phase=ResetOperationPhase.SUCCEEDED,
                    reconciliation_required=False,
                ),
                SnapshotCreateOperationRow(
                    id="98000000-0000-4000-8000-000000000004",
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
                    id="98000000-0000-4000-8000-000000000005",
                    snapshot_id=str(SNAPSHOT_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    requested_at=AT,
                    started_at=AT,
                    heartbeat_at=AT,
                    completed_at=AT,
                    state=SnapshotRestoreOperationState.SUCCEEDED,
                    phase=SnapshotRestoreOperationPhase.SUCCEEDED,
                    reconciliation_required=False,
                ),
                SnapshotDeleteOperationRow(
                    id="98000000-0000-4000-8000-000000000006",
                    snapshot_id=str(SNAPSHOT_ID),
                    managed_instance_id=str(INSTANCE_ID),
                    prior_snapshot_state=SnapshotState.AVAILABLE,
                    requested_at=AT,
                    started_at=AT,
                    heartbeat_at=AT,
                    completed_at=AT,
                    state=SnapshotDeleteOperationState.FAILED,
                    phase=SnapshotDeleteOperationPhase.FAILED,
                    failure_reason="bounded deletion failure",
                    reconciliation_required=False,
                ),
                ManagedInstanceDeleteOperationRow(
                    id="98000000-0000-4000-8000-000000000002",
                    original_managed_instance_id=str(INSTANCE_ID),
                    source_instance_name="Deleted <script>instance</script>",
                    runtime_revision_id=str(REVISION_ID),
                    fixture_definition_id=None,
                    preserve_snapshots=preserve_snapshot,
                    snapshot_count=1 if preserve_snapshot else 0,
                    aggregate_snapshot_bytes=16 if preserve_snapshot else 0,
                    completed_snapshot_count=1 if preserve_snapshot else 0,
                    prior_state=ManagedInstanceDeletePriorState.STOPPED,
                    notes_at_deletion="private deleted note /secret/path",
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
        instance = session.get(ManagedInstanceRow, str(INSTANCE_ID))
        target = session.get(TargetRow, str(INSTANCE_ID))
        assert instance is not None and target is not None
        session.delete(instance)
        session.flush()
        session.delete(target)
    if preserve_snapshot:
        instant = datetime(2026, 9, 16, 12, tzinfo=UTC)
        with database.begin() as session:
            SnapshotRepository(session).create(
                Snapshot(
                    id=SNAPSHOT_ID,
                    source_managed_instance_id=None,
                    original_source_managed_instance_id=INSTANCE_ID,
                    source_binding=SnapshotSourceBinding.DELETED_SOURCE,
                    source_instance_name="Deleted <script>instance</script>",
                    source_deleted_at=instant,
                    runtime_revision_id=REVISION_ID,
                    captured_fixture_definition_id=None,
                    captured_at=instant,
                    prior_runtime_state=InstanceRuntimeState.STOPPED,
                    label="Preserved <safe>",
                    manifest_version=1,
                    archive_digest="sha256:" + "b" * 64,
                    manifest_digest="sha256:" + "c" * 64,
                    archive_size=16,
                    member_count=1,
                    state=SnapshotState.AVAILABLE,
                    validation_at=instant,
                    validation_result=SnapshotValidationResult.PASSED,
                )
            )


def test_deleted_history_is_discoverable_without_notes_or_live_route(
    client: TestClient, database: sessionmaker[Session]
) -> None:
    _delete_instance_with_retained_history(database, preserve_snapshot=True)
    login(client)
    assert client.get(f"/instances/{INSTANCE_ID}").status_code == 404
    global_page = client.get("/history/instances")
    assert global_page.status_code == 200
    assert str(INSTANCE_ID) in global_page.text
    assert "Deleted &lt;script&gt;instance&lt;/script&gt;" in global_page.text
    assert "private deleted note" not in global_page.text
    assert "/secret/path" not in global_page.text
    assert "Kodi 21.3 · revision 2" in global_page.text

    detail = client.get(f"/history/instances/{INSTANCE_ID}")
    assert detail.status_code == 200
    assert "Start succeeded" in detail.text
    assert "Reset succeeded" in detail.text
    assert "Snapshot creation succeeded" in detail.text
    assert "Snapshot restore succeeded" in detail.text
    assert "Snapshot deletion failed" in detail.text
    assert str(SNAPSHOT_ID) in detail.text
    assert "private deleted note" not in detail.text
    assert "/secret/path" not in detail.text
    assert "/restore" not in detail.text.lower()

    preserved = client.get("/snapshots/preserved")
    assert preserved.status_code == 200
    assert f"/history/instances/{INSTANCE_ID}" in preserved.text
    assert "Deleted &lt;script&gt;instance&lt;/script&gt;" in preserved.text
