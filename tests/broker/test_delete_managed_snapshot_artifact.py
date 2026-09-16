from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.errors import RequestError, SnapshotStateError
from kodi_lab.broker.operations import (
    DeleteManagedSnapshotArtifactParams,
    DeleteManagedSnapshotArtifactResult,
    build_foundation_registry,
)
from kodi_lab.broker.protocol import Request
from kodi_lab.broker.snapshots import (
    LocalSnapshotArtifactDeletionProvider,
    SnapshotDeleteSpec,
    SqliteSnapshotDeleteResolver,
    SqliteSnapshotRestoreResolver,
)
from kodi_lab.domain import (
    InstanceRuntimeState,
    Snapshot,
    SnapshotSourceBinding,
    SnapshotState,
    SnapshotValidationResult,
)
from kodi_lab.snapshot.storage import snapshot_directory
from kodi_lab.storage.engine import create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    SnapshotRepository,
    TargetRepository,
)

SNAPSHOT_ID = UUID("94200000-0000-4000-8000-000000000001")
INSTANCE_ID = UUID("94200000-0000-4000-8000-000000000002")
REVISION_ID = UUID("94200000-0000-4000-8000-000000000003")
DEFINITION_ID = UUID("94200000-0000-4000-8000-000000000004")


@pytest.fixture
def factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'broker-delete.db'}")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    timestamp = datetime(2026, 9, 16, tzinfo=UTC)
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
                content_digest="sha256:" + "a" * 64,
                manifest_reference="test",
                manifest_digest="sha256:" + "b" * 64,
                built_at="2026-09-16T00:00:00.000000Z",
                build_log_reference=None,
                approval_state="approved",
                approved_at="2026-09-16T00:00:00.000000Z",
                approval_note=None,
                requires_trusted_build_confirmation=False,
                trusted_build_confirmed_at=None,
            )
        )
        session.add(
            TargetRow(
                id=str(INSTANCE_ID),
                name="delete",
                target_type="managed",
                kodi_version="21.3",
                availability="available",
                health="healthy",
                connection_state="connected",
            )
        )
        session.add(
            ManagedInstanceRow(
                target_id=str(INSTANCE_ID),
                instance_type="disposable",
                runtime_revision_id=str(REVISION_ID),
                runtime_state="stopped",
                start_on_host_boot=False,
                notes=None,
            )
        )
        SnapshotRepository(session).create(
            Snapshot(
                id=SNAPSHOT_ID,
                source_managed_instance_id=INSTANCE_ID,
                runtime_revision_id=REVISION_ID,
                captured_fixture_definition_id=None,
                captured_at=timestamp,
                prior_runtime_state=InstanceRuntimeState.STOPPED,
                label="delete",
                manifest_version=1,
                archive_digest="sha256:" + "c" * 64,
                manifest_digest="sha256:" + "d" * 64,
                archive_size=10240,
                member_count=1,
                state=SnapshotState.AVAILABLE,
                validation_at=timestamp,
                validation_result=SnapshotValidationResult.PASSED,
            )
        )
    return result


def request(params: dict[str, object]) -> Request:
    return Request(
        version=1,
        request_id=str(uuid4()),
        operation="delete_managed_snapshot_artifact",
        params=params,
    )


def test_resolver_requires_canonical_deleting_state(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    resolver = SqliteSnapshotDeleteResolver(
        factory, data_root=tmp_path, owner_uid=os.getuid(), owner_gid=os.getgid()
    )
    with pytest.raises(SnapshotStateError, match="awaiting deletion"):
        resolver.resolve(str(SNAPSHOT_ID))
    with factory.begin() as session:
        assert SnapshotRepository(session).mark_deleting(
            SNAPSHOT_ID, prior_state=SnapshotState.AVAILABLE
        )
    assert resolver.resolve(str(SNAPSHOT_ID)) == SnapshotDeleteSpec(
        SNAPSHOT_ID, tmp_path, os.getuid(), os.getgid()
    )
    with pytest.raises(SnapshotStateError, match="not available"):
        SqliteSnapshotRestoreResolver(
            factory,
            data_root=tmp_path,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        ).resolve(str(SNAPSHOT_ID), str(INSTANCE_ID))
    with pytest.raises(SnapshotStateError, match="canonical"):
        resolver.resolve(SNAPSHOT_ID.hex)


def test_operation_removes_only_uuid_artifact_and_has_no_path_authority(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    root = tmp_path / "snapshots"
    root.mkdir(mode=0o750)
    os.chmod(root, 0o750)
    target = snapshot_directory(tmp_path, SNAPSHOT_ID)
    target.mkdir(mode=0o750)
    (target / "state.tar").write_bytes(b"secret")
    sibling = snapshot_directory(tmp_path, UUID("94200000-0000-4000-8000-000000000005"))
    sibling.mkdir(mode=0o750)
    (sibling / "state.tar").write_bytes(b"keep")
    with factory.begin() as session:
        assert SnapshotRepository(session).mark_deleting(
            SNAPSHOT_ID, prior_state=SnapshotState.AVAILABLE
        )
    registry = build_foundation_registry(
        snapshot_delete_resolver=SqliteSnapshotDeleteResolver(
            factory, data_root=tmp_path, owner_uid=os.getuid(), owner_gid=os.getgid()
        ),
        snapshot_delete_provider=LocalSnapshotArtifactDeletionProvider(),
    )
    params: dict[str, object] = {"snapshot_id": str(SNAPSHOT_ID)}
    result = registry.dispatch(request(params))
    assert isinstance(result, DeleteManagedSnapshotArtifactResult)
    assert result.artifact_absent and result.artifact_was_present
    assert root.is_dir() and (sibling / "state.tar").read_bytes() == b"keep"
    assert set(DeleteManagedSnapshotArtifactParams.model_fields) == {"snapshot_id"}
    for injected in ("path", "wildcard", "recursive", "filename", "docker"):
        with pytest.raises(RequestError):
            registry.dispatch(request({**params, injected: "attacker"}))


def test_absent_is_accepted_only_after_deleting_transition(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    root = tmp_path / "snapshots"
    root.mkdir(mode=0o750)
    os.chmod(root, 0o750)
    resolver = SqliteSnapshotDeleteResolver(
        factory, data_root=tmp_path, owner_uid=os.getuid(), owner_gid=os.getgid()
    )
    registry = build_foundation_registry(
        snapshot_delete_resolver=resolver,
        snapshot_delete_provider=LocalSnapshotArtifactDeletionProvider(),
    )
    with pytest.raises(RequestError) as raised:
        registry.dispatch(request({"snapshot_id": str(SNAPSHOT_ID)}))
    assert raised.value.code == "snapshot_delete_incompatible"
    with factory.begin() as session:
        assert SnapshotRepository(session).mark_deleting(
            SNAPSHOT_ID, prior_state=SnapshotState.AVAILABLE
        )
    result = registry.dispatch(request({"snapshot_id": str(SNAPSHOT_ID)}))
    assert isinstance(result, DeleteManagedSnapshotArtifactResult)
    assert result.artifact_absent and not result.artifact_was_present


def test_detached_deleting_snapshot_needs_no_live_source_parent(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with factory.begin() as session:
        snapshot = SnapshotRepository(session)
        assert (
            snapshot.detach_for_deleted_source(
                INSTANCE_ID,
                source_name="deleted source",
                deleted_at=datetime(2026, 9, 16, 1, tzinfo=UTC),
            )
            == 1
        )
        detached = snapshot.get(SNAPSHOT_ID)
        assert detached is not None
        assert detached.source_binding is SnapshotSourceBinding.DELETED_SOURCE
        assert snapshot.mark_deleting(SNAPSHOT_ID, prior_state=SnapshotState.AVAILABLE)
        assert ManagedInstanceRepository(session).delete(INSTANCE_ID)
        assert TargetRepository(session).delete(INSTANCE_ID)

    resolved = SqliteSnapshotDeleteResolver(
        factory, data_root=tmp_path, owner_uid=os.getuid(), owner_gid=os.getgid()
    ).resolve(str(SNAPSHOT_ID))
    assert resolved.snapshot_id == SNAPSHOT_ID
    with pytest.raises(SnapshotStateError, match="does not exist"):
        SqliteSnapshotRestoreResolver(
            factory,
            data_root=tmp_path,
            owner_uid=os.getuid(),
            owner_gid=os.getgid(),
        ).resolve(str(SNAPSHOT_ID), str(INSTANCE_ID))
