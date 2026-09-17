from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kodi_lab.domain import (
    DevelopmentSource,
    DevelopmentSourceAttachmentOperation,
    DevelopmentSourceAttachmentOperationKind,
    DevelopmentSourceAttachmentOperationState,
    DevelopmentSourceKind,
    ManagedInstanceDevelopmentSource,
)
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceAttachmentOperationRepository,
    DevelopmentSourceRepository,
    ManagedInstanceDevelopmentSourceRepository,
    ManagedInstanceRepository,
    TargetRepository,
)

NOW = datetime(2026, 9, 16, 21, 0, tzinfo=UTC)
I1 = UUID("98200000-0000-4000-8000-000000000001")
I2 = UUID("98200000-0000-4000-8000-000000000002")
S1 = UUID("98200000-0000-4000-8000-000000000011")
S2 = UUID("98200000-0000-4000-8000-000000000012")
REVISION = UUID("98200000-0000-4000-8000-000000000021")
DEFINITION = UUID("98200000-0000-4000-8000-000000000022")


def seed(session: Session) -> None:
    session.add(
        RuntimeDefinitionRow(
            id=str(DEFINITION), slug="attachment-storage", kodi_version="21.3"
        )
    )
    session.add(
        RuntimeRevisionRow(
            id=str(REVISION),
            runtime_definition_id=str(DEFINITION),
            revision_number=1,
            source_kind="test",
            source_reference="test",
            content_digest="sha256:" + "a" * 64,
            manifest_reference="test",
            manifest_digest="sha256:" + "b" * 64,
            built_at=NOW.isoformat(),
            approval_state="approved",
            approved_at=NOW.isoformat(),
            requires_trusted_build_confirmation=False,
        )
    )
    session.flush()
    for identity in (I1, I2):
        session.add(
            TargetRow(
                id=str(identity),
                name=f"instance-{identity}",
                target_type="managed",
                kodi_version="21.3",
                availability="unavailable",
                health="warning",
                connection_state="disconnected",
            )
        )
        session.flush()
        session.add(
            ManagedInstanceRow(
                target_id=str(identity),
                instance_type="disposable",
                runtime_revision_id=str(REVISION),
                runtime_state="stopped",
                start_on_host_boot=False,
            )
        )
    sources = DevelopmentSourceRepository(session)
    for identity, logical_name, addon_id in (
        (S1, "Zulu", "plugin.video.zulu"),
        (S2, "Alpha", "plugin.video.alpha"),
    ):
        sources.create(
            DevelopmentSource(
                id=identity,
                logical_name=logical_name,
                source_kind=DevelopmentSourceKind.LOCAL_DIRECTORY,
                relative_locator=f"sources/{logical_name.lower()}",
                addon_id=addon_id,
                observed_name=logical_name,
                observed_version="1.0.0",
                extension_types=("xbmc.python.pluginsource",),
                registered_at=NOW,
                validated_at=NOW,
            )
        )


def history(
    identity: UUID,
    *,
    source_id: UUID,
    kind: DevelopmentSourceAttachmentOperationKind,
    state: DevelopmentSourceAttachmentOperationState = (
        DevelopmentSourceAttachmentOperationState.SUCCEEDED
    ),
) -> DevelopmentSourceAttachmentOperation:
    source_name = "Zulu" if source_id == S1 else "Alpha"
    addon_id = "plugin.video.zulu" if source_id == S1 else "plugin.video.alpha"
    return DevelopmentSourceAttachmentOperation(
        id=identity,
        original_managed_instance_id=I1,
        managed_instance_id=I1,
        development_source_id=source_id,
        source_logical_name=source_name,
        addon_id=addon_id,
        kind=kind,
        requested_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        state=state,
        failure_reason="typed_failure" if state.value == "failed" else None,
    )


def test_repository_cardinality_order_duplicate_and_delete(db_session: Session) -> None:
    seed(db_session)
    repository = ManagedInstanceDevelopmentSourceRepository(db_session)
    zulu = ManagedInstanceDevelopmentSource(I1, S1, NOW)
    alpha = ManagedInstanceDevelopmentSource(I1, S2, NOW + timedelta(seconds=1))
    reused = ManagedInstanceDevelopmentSource(I2, S1, NOW + timedelta(seconds=2))
    repository.create(zulu)
    repository.create(alpha)
    repository.create(reused)

    assert repository.get(I1, S1) == zulu
    assert repository.get_by_addon_id(I1, "plugin.video.alpha") == alpha
    assert repository.list_for_instance(I1) == [alpha, zulu]
    assert repository.list_instance_ids_for_source(S1) == [I1, I2]
    db_session.commit()
    with pytest.raises(IntegrityError):
        repository.create(zulu)
    db_session.rollback()

    assert repository.delete(I1, S2)
    assert not repository.delete(I1, S2)
    assert repository.list_for_instance(I1) == [zulu]


def test_history_survives_instance_deletion_and_source_catalog_survives(
    db_session: Session,
) -> None:
    seed(db_session)
    associations = ManagedInstanceDevelopmentSourceRepository(db_session)
    associations.create(ManagedInstanceDevelopmentSource(I1, S1, NOW))
    operations = DevelopmentSourceAttachmentOperationRepository(db_session)
    attached = history(
        UUID("98200000-0000-4000-8000-000000000101"),
        source_id=S1,
        kind=DevelopmentSourceAttachmentOperationKind.ATTACH,
    )
    detached_failure = history(
        UUID("98200000-0000-4000-8000-000000000102"),
        source_id=S1,
        kind=DevelopmentSourceAttachmentOperationKind.DETACH,
        state=DevelopmentSourceAttachmentOperationState.FAILED,
    )
    operations.create(attached)
    operations.create(detached_failure)

    assert ManagedInstanceRepository(db_session).delete(I1)
    assert TargetRepository(db_session).delete(I1)
    db_session.flush()

    assert associations.list_for_instance(I1) == []
    assert DevelopmentSourceRepository(db_session).get(S1) is not None
    retained = operations.list_for_original_instance(I1)
    assert len(retained) == 2
    assert all(item.managed_instance_id is None for item in retained)
    assert {item.development_source_id for item in retained} == {S1}
