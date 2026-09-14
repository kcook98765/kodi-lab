from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from kodi_lab.domain import (
    ConnectionState,
    HealthStatus,
    InstanceRuntimeState,
    InstanceType,
    ManagedInstance,
    RevisionApprovalState,
    RuntimeDefinition,
    RuntimeRevision,
    Target,
    TargetAvailability,
    TargetType,
)
from kodi_lab.storage.models import RuntimeDefinitionRow, RuntimeRevisionRow
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)

BUILT_AT = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def make_definition(slug: str = "kodi-21.3") -> RuntimeDefinition:
    return RuntimeDefinition(id=uuid4(), slug=slug, kodi_version="21.3")


def make_revision(
    definition: RuntimeDefinition,
    revision_number: int,
    *,
    approved: bool = False,
    requires_trust: bool = False,
) -> RuntimeRevision:
    revision = RuntimeRevision(
        id=uuid4(),
        runtime_definition_id=definition.id,
        revision_number=revision_number,
        source_kind="official-package",
        source_reference=f"ppa:team-xbmc/ppa@21.3-r{revision_number}",
        content_digest=f"{revision_number:064x}",
        manifest_reference="catalog/kodi-21.3.yaml",
        manifest_digest="f" * 64,
        built_at=BUILT_AT,
        build_log_reference=f"logs/runtime-{revision_number}.log",
        requires_trusted_build_confirmation=requires_trust,
    )
    return (
        revision.approve(approved_at=BUILT_AT, note="reviewed")
        if approved
        else revision
    )


def test_runtime_definition_create_get_and_list(db_session: Session) -> None:
    repository = RuntimeDefinitionRepository(db_session)
    definition_b = make_definition("kodi-22")
    definition_a = make_definition("kodi-21.3")

    repository.create(definition_b)
    repository.create(definition_a)
    db_session.commit()
    db_session.expire_all()

    assert repository.get(definition_a.id) == definition_a
    assert repository.get_by_slug("kodi-21.3") == definition_a
    assert repository.list() == [definition_a, definition_b]


def test_runtime_definition_slug_is_unique_in_database(db_session: Session) -> None:
    repository = RuntimeDefinitionRepository(db_session)
    repository.create(make_definition())

    with pytest.raises(IntegrityError):
        repository.create(make_definition())


def test_runtime_definition_nonblank_fields_are_database_enforced(
    db_session: Session,
) -> None:
    definition = make_definition()
    RuntimeDefinitionRepository(db_session).create(definition)
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.execute(
            update(RuntimeDefinitionRow)
            .where(RuntimeDefinitionRow.id == str(definition.id))
            .values(slug=" ")
        )


def test_revision_create_get_and_ordered_list(db_session: Session) -> None:
    definition = make_definition()
    definitions = RuntimeDefinitionRepository(db_session)
    revisions = RuntimeRevisionRepository(db_session)
    definitions.create(definition)
    revision_2 = make_revision(definition, 2)
    revision_1 = make_revision(definition, 1)
    revisions.create(revision_2)
    revisions.create(revision_1)
    db_session.commit()
    db_session.expire_all()

    assert revisions.get(revision_1.id) == revision_1
    assert revisions.list_for_definition(definition.id) == [revision_1, revision_2]


def test_revision_number_is_unique_within_definition(db_session: Session) -> None:
    definition = make_definition()
    definitions = RuntimeDefinitionRepository(db_session)
    revisions = RuntimeRevisionRepository(db_session)
    definitions.create(definition)
    revisions.create(make_revision(definition, 1))

    with pytest.raises(IntegrityError):
        revisions.create(make_revision(definition, 1))


def test_revision_uuid_identity_is_unique(db_session: Session) -> None:
    definition = make_definition()
    definitions = RuntimeDefinitionRepository(db_session)
    revisions = RuntimeRevisionRepository(db_session)
    definitions.create(definition)
    revision = make_revision(definition, 1)
    revisions.create(revision)

    with pytest.raises(IntegrityError):
        revisions.create(replace(revision, revision_number=2))


def test_same_revision_number_is_allowed_for_different_definitions(
    db_session: Session,
) -> None:
    first = make_definition("kodi-21.3")
    second = make_definition("kodi-22")
    definitions = RuntimeDefinitionRepository(db_session)
    revisions = RuntimeRevisionRepository(db_session)
    definitions.create(first)
    definitions.create(second)

    revisions.create(make_revision(first, 1))
    revisions.create(make_revision(second, 1))

    assert len(revisions.list_for_definition(first.id)) == 1
    assert len(revisions.list_for_definition(second.id)) == 1


def test_revision_requires_existing_definition(db_session: Session) -> None:
    with pytest.raises(IntegrityError):
        RuntimeRevisionRepository(db_session).create(
            make_revision(make_definition(), 1)
        )


def test_newest_approved_uses_greatest_revision_number(db_session: Session) -> None:
    definition = make_definition()
    definitions = RuntimeDefinitionRepository(db_session)
    revisions = RuntimeRevisionRepository(db_session)
    definitions.create(definition)
    approved_1 = make_revision(definition, 1, approved=True)
    approved_3 = make_revision(definition, 3, approved=True)
    revisions.create(approved_3)
    revisions.create(make_revision(definition, 4))
    revisions.create(approved_1)

    assert revisions.newest_approved(definition.id) == approved_3


def test_newest_approved_returns_none_when_no_revision_is_approved(
    db_session: Session,
) -> None:
    definition = make_definition()
    RuntimeDefinitionRepository(db_session).create(definition)
    revisions = RuntimeRevisionRepository(db_session)
    revisions.create(make_revision(definition, 1))

    assert revisions.newest_approved(definition.id) is None


def test_explicit_lifecycle_changes_preserve_intrinsic_revision_data(
    db_session: Session,
) -> None:
    definition = make_definition()
    original = make_revision(definition, 1, requires_trust=True)
    RuntimeDefinitionRepository(db_session).create(definition)
    repository = RuntimeRevisionRepository(db_session)
    repository.create(original)

    approved = repository.approve(original.id, approved_at=BUILT_AT, note="reviewed")
    confirmed = repository.confirm_trusted_build(original.id, confirmed_at=BUILT_AT)
    db_session.commit()
    stored = repository.get(original.id)

    assert approved.content_digest == original.content_digest
    assert confirmed.content_digest == original.content_digest
    assert stored is not None
    assert stored.approval_state is RevisionApprovalState.APPROVED
    assert stored.trusted_build_confirmed_at == BUILT_AT
    assert stored.revision_number == original.revision_number
    assert not hasattr(repository, "update")
    assert not hasattr(repository, "delete")

    revoked_approval = repository.revoke_approval(original.id)
    revoked_trust = repository.revoke_trusted_build_confirmation(original.id)
    assert revoked_approval.approval_state is RevisionApprovalState.NOT_APPROVED
    assert revoked_approval.approved_at is None
    assert revoked_trust.trusted_build_confirmed_at is None


def test_revision_database_checks_intrinsic_and_lifecycle_values(
    db_session: Session,
) -> None:
    definition = make_definition()
    revision = make_revision(definition, 1)
    RuntimeDefinitionRepository(db_session).create(definition)
    RuntimeRevisionRepository(db_session).create(revision)
    db_session.commit()

    invalid_updates: list[dict[str, object]] = [
        {"revision_number": 0},
        {"source_kind": " "},
        {"source_reference": None},
        {"content_digest": "sha256:not-a-digest"},
        {"approval_state": "approved", "approved_at": None},
        {"trusted_build_confirmed_at": "2026-09-14T12:00:00Z"},
    ]
    for values in invalid_updates:
        with pytest.raises(IntegrityError):
            db_session.execute(
                update(RuntimeRevisionRow)
                .where(RuntimeRevisionRow.id == str(revision.id))
                .values(**values)
            )
        db_session.rollback()


def test_referenced_revision_and_parent_definition_cannot_be_deleted(
    db_session: Session,
) -> None:
    definition = make_definition()
    revision = make_revision(definition, 1)
    target = Target(
        id=uuid4(),
        name="Kodi 21",
        target_type=TargetType.MANAGED,
        kodi_version="21.3",
        availability=TargetAvailability.UNAVAILABLE,
        health=HealthStatus.WARNING,
        connection_state=ConnectionState.DISCONNECTED,
    )
    RuntimeDefinitionRepository(db_session).create(definition)
    RuntimeRevisionRepository(db_session).create(revision)
    TargetRepository(db_session).create(target)
    ManagedInstanceRepository(db_session).create(
        ManagedInstance(
            target_id=target.id,
            instance_type=InstanceType.PERSISTENT,
            runtime_revision_id=revision.id,
            runtime_state=InstanceRuntimeState.STOPPED,
        )
    )
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.execute(
            delete(RuntimeRevisionRow).where(RuntimeRevisionRow.id == str(revision.id))
        )
    db_session.rollback()

    with pytest.raises(IntegrityError):
        db_session.execute(
            delete(RuntimeDefinitionRow).where(
                RuntimeDefinitionRow.id == str(definition.id)
            )
        )
