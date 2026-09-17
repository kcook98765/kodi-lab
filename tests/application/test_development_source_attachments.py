from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application import (
    DevelopmentSourceAddonConflictError,
    DevelopmentSourceAlreadyAttachedError,
    DevelopmentSourceAttachmentService,
    DevelopmentSourceAttachmentValidationError,
    DevelopmentSourceIdentityChangedError,
    DevelopmentSourceNotAttachedError,
    DevelopmentSourceNotFoundError,
    ManagedInstanceNotFoundError,
    PersistentDevelopmentSourceAttachmentUnsupportedError,
)
from kodi_lab.application.development_sources import (
    DevelopmentSourceAdministrationService,
)
from kodi_lab.application.mutations import ManagedInstanceMutationConflictError
from kodi_lab.application.reconciliation import (
    ManagedInstanceReconciliationRequiredError,
)
from kodi_lab.development import DevelopmentSourceValidator
from kodi_lab.domain import (
    DevelopmentSourceAttachmentOperationKind,
    DevelopmentSourceAttachmentOperationState,
    ManagedInstanceMutationKind,
)
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import (
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ResetOperationRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.repositories import (
    DevelopmentSourceAttachmentOperationRepository,
    DevelopmentSourceRepository,
    ManagedInstanceDevelopmentSourceRepository,
)

I1 = UUID("98100000-0000-4000-8000-000000000001")
I2 = UUID("98100000-0000-4000-8000-000000000002")
PERSISTENT = UUID("98100000-0000-4000-8000-000000000003")
S1 = UUID("98100000-0000-4000-8000-000000000011")
S2 = UUID("98100000-0000-4000-8000-000000000012")
REVISION = UUID("98100000-0000-4000-8000-000000000021")
DEFINITION = UUID("98100000-0000-4000-8000-000000000022")
NOW = datetime(2026, 9, 16, 20, 0, tzinfo=UTC)


def write_source(
    root: Path,
    locator: str,
    *,
    addon_id: str,
    name: str,
    version: str,
) -> None:
    directory = root.joinpath(*locator.split("/"))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "addon.xml").write_text(
        f'<addon id="{addon_id}" name="{name}" version="{version}" '
        'provider-name="Kodi Lab"><extension point="xbmc.python.pluginsource" />'
        "</addon>",
        encoding="utf-8",
    )


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'attachments.db'}")
    Base.metadata.create_all(engine)
    result = create_session_factory(engine)
    with result.begin() as session:
        session.add(
            RuntimeDefinitionRow(
                id=str(DEFINITION), slug="attachment-runtime", kodi_version="21.3"
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
        for identity, instance_type in (
            (I1, "disposable"),
            (I2, "disposable"),
            (PERSISTENT, "persistent"),
        ):
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
                    instance_type=instance_type,
                    runtime_revision_id=str(REVISION),
                    runtime_state="running" if identity == I2 else "stopped",
                    start_on_host_boot=False,
                )
            )
    yield result
    engine.dispose()


def register_sources(factory: sessionmaker[Session], root: Path) -> None:
    write_source(
        root,
        "z-source",
        addon_id="plugin.video.zeta",
        name="Zeta observed",
        version="1.0.0",
    )
    write_source(
        root,
        "a-source",
        addon_id="plugin.video.alpha",
        name="Alpha observed",
        version="2.0.0",
    )
    for source_id, logical_name, locator in (
        (S1, "Zulu", "z-source"),
        (S2, "Alpha", "a-source"),
    ):

        def source_uuid(value: UUID = source_id) -> UUID:
            return value

        DevelopmentSourceAdministrationService(
            factory,
            DevelopmentSourceValidator(root),
            clock=lambda: NOW,
            uuid_factory=source_uuid,
        ).register_source(logical_name=logical_name, relative_locator=locator)


def attachment_service(
    factory: sessionmaker[Session], root: Path
) -> DevelopmentSourceAttachmentService:
    ids = iter(
        UUID(f"98100000-0000-4000-8000-{ordinal:012d}") for ordinal in range(101, 130)
    )
    ticks = iter(NOW + timedelta(seconds=ordinal) for ordinal in range(30))
    return DevelopmentSourceAttachmentService(
        factory,
        DevelopmentSourceValidator(root),
        clock=lambda: next(ticks),
        uuid_factory=lambda: next(ids),
    )


def test_attach_list_cardinality_history_and_path_free_dto(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    register_sources(factory, tmp_path)
    service = attachment_service(factory, tmp_path)

    zulu = service.attach_source(I1, S1)
    alpha = service.attach_source(I1, S2)
    reused = service.attach_source(I2, S1)

    assert service.list_attached_sources(I1) == [alpha, zulu]
    assert service.list_attached_sources(I2) == [reused]
    serialized = zulu.model_dump_json()
    assert "relative_locator" not in serialized
    assert str(tmp_path) not in serialized
    with factory() as session:
        history = DevelopmentSourceAttachmentOperationRepository(
            session
        ).list_for_original_instance(I1)
        instances = ManagedInstanceDevelopmentSourceRepository(
            session
        ).list_instance_ids_for_source(S1)
    assert {item.kind for item in history} == {
        DevelopmentSourceAttachmentOperationKind.ATTACH
    }
    assert all(
        item.state is DevelopmentSourceAttachmentOperationState.SUCCEEDED
        for item in history
    )
    assert instances == [I1, I2]


def test_duplicate_attach_and_addon_conflict_are_typed_and_atomic(
    factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_sources(factory, tmp_path)
    service = attachment_service(factory, tmp_path)
    service.attach_source(I1, S1)
    with pytest.raises(DevelopmentSourceAlreadyAttachedError):
        service.attach_source(I1, S1)

    existing = ManagedInstanceDevelopmentSourceRepository
    original = existing.get_by_addon_id
    monkeypatch.setattr(
        existing,
        "get_by_addon_id",
        lambda repository, instance_id, addon_id: (
            repository.get(instance_id, S1)
            if addon_id == "plugin.video.alpha"
            else original(repository, instance_id, addon_id)
        ),
    )
    with pytest.raises(DevelopmentSourceAddonConflictError):
        service.attach_source(I1, S2)
    assert [
        item.development_source_id for item in service.list_attached_sources(I1)
    ] == [S1]
    with factory() as session:
        history = DevelopmentSourceAttachmentOperationRepository(
            session
        ).list_for_original_instance(I1)
    assert [item.state for item in history].count(
        DevelopmentSourceAttachmentOperationState.FAILED
    ) == 2


@pytest.mark.parametrize("failure", ("missing", "invalid"))
def test_attach_revalidation_rejects_missing_or_invalid_source(
    factory: sessionmaker[Session], tmp_path: Path, failure: str
) -> None:
    register_sources(factory, tmp_path)
    addon_xml = tmp_path / "z-source" / "addon.xml"
    if failure == "missing":
        addon_xml.unlink()
        addon_xml.parent.rmdir()
    else:
        addon_xml.write_text("<addon", encoding="utf-8")
    service = attachment_service(factory, tmp_path)
    with pytest.raises(DevelopmentSourceAttachmentValidationError) as captured:
        service.attach_source(I1, S1)
    assert captured.value.code in {"source_unavailable", "addon_xml_malformed"}
    assert service.list_attached_sources(I1) == []


def test_revalidation_allows_observation_change_but_rejects_identity_change(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    register_sources(factory, tmp_path)
    write_source(
        tmp_path,
        "z-source",
        addon_id="plugin.video.zeta",
        name="Changed name",
        version="9.9.9",
    )
    service = attachment_service(factory, tmp_path)
    attached = service.attach_source(I1, S1)
    assert attached.observed_name == "Zeta observed"
    assert attached.observed_version == "1.0.0"

    write_source(
        tmp_path,
        "z-source",
        addon_id="plugin.video.changed",
        name="Changed identity",
        version="10.0.0",
    )
    with pytest.raises(DevelopmentSourceIdentityChangedError) as captured:
        service.attach_source(I2, S1)
    assert captured.value.code == "development_source_identity_changed"
    with factory() as session:
        catalog = DevelopmentSourceRepository(session).get(S1)
    assert catalog is not None
    assert catalog.addon_id == "plugin.video.zeta"
    assert catalog.observed_name == "Zeta observed"


def test_detach_removes_only_association_and_retains_catalog_and_history(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    register_sources(factory, tmp_path)
    service = attachment_service(factory, tmp_path)
    attached = service.attach_source(I1, S1)
    (tmp_path / "z-source" / "addon.xml").unlink()

    assert service.detach_source(I1, S1) == attached
    assert service.list_attached_sources(I1) == []
    with pytest.raises(DevelopmentSourceNotAttachedError):
        service.detach_source(I1, S1)
    with factory() as session:
        assert DevelopmentSourceRepository(session).get(S1) is not None
        history = DevelopmentSourceAttachmentOperationRepository(
            session
        ).list_for_original_instance(I1)
    assert {item.kind for item in history} == {
        DevelopmentSourceAttachmentOperationKind.ATTACH,
        DevelopmentSourceAttachmentOperationKind.DETACH,
    }


def test_missing_and_persistent_entities_fail_before_mutation(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    register_sources(factory, tmp_path)
    service = attachment_service(factory, tmp_path)
    with pytest.raises(ManagedInstanceNotFoundError):
        service.attach_source(UUID(int=999), S1)
    with pytest.raises(DevelopmentSourceNotFoundError):
        service.attach_source(I1, UUID(int=999))
    with pytest.raises(PersistentDevelopmentSourceAttachmentUnsupportedError):
        service.attach_source(PERSISTENT, S1)


def test_active_mutation_lease_blocks_attachment(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    register_sources(factory, tmp_path)
    with factory.begin() as session:
        session.add(
            ManagedInstanceMutationLeaseRow(
                managed_instance_id=str(I1),
                operation_kind=ManagedInstanceMutationKind.FIXTURE_ASSIGNMENT,
                operation_id=str(UUID(int=777)),
            )
        )
    with pytest.raises(ManagedInstanceMutationConflictError):
        attachment_service(factory, tmp_path).attach_source(I1, S1)


def test_unresolved_reconciliation_blocks_attachment_before_validation(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    register_sources(factory, tmp_path)
    with factory.begin() as session:
        session.add(
            ResetOperationRow(
                id="98100000-0000-4000-8000-000000000999",
                managed_instance_id=str(I1),
                requested_at=NOW.isoformat(),
                completed_at=NOW.isoformat(),
                state="reconciliation_required",
                phase="reconciliation_required",
                failure_reason="typed reset uncertainty",
                reconciliation_required=True,
            )
        )
    with pytest.raises(ManagedInstanceReconciliationRequiredError):
        attachment_service(factory, tmp_path).attach_source(I1, S1)
    assert not (tmp_path / "z-source" / "deployment-state").exists()
