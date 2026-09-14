"""Explicit repositories for targets and managed instances."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from kodi_lab.domain import (
    ManagedInstance,
    RuntimeDefinition,
    RuntimeRevision,
    Target,
)
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    TargetRow,
)


class EntityNotFoundError(LookupError):
    """Raised when an update target no longer exists."""


def _target_from_row(row: TargetRow) -> Target:
    return Target(
        id=UUID(row.id),
        name=row.name,
        target_type=row.target_type,
        kodi_version=row.kodi_version,
        availability=row.availability,
        health=row.health,
        connection_state=row.connection_state,
    )


def _instance_from_row(row: ManagedInstanceRow) -> ManagedInstance:
    return ManagedInstance(
        target_id=UUID(row.target_id),
        instance_type=row.instance_type,
        runtime_revision_id=UUID(row.runtime_revision_id),
        runtime_state=row.runtime_state,
        start_on_host_boot=row.start_on_host_boot,
        notes=row.notes,
    )


def _timestamp_to_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp_from_text(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _definition_from_row(row: RuntimeDefinitionRow) -> RuntimeDefinition:
    return RuntimeDefinition(
        id=UUID(row.id),
        slug=row.slug,
        kodi_version=row.kodi_version,
    )


def _revision_from_row(row: RuntimeRevisionRow) -> RuntimeRevision:
    return RuntimeRevision(
        id=UUID(row.id),
        runtime_definition_id=UUID(row.runtime_definition_id),
        revision_number=row.revision_number,
        source_kind=row.source_kind,
        source_reference=row.source_reference,
        content_digest=row.content_digest,
        manifest_reference=row.manifest_reference,
        manifest_digest=row.manifest_digest,
        built_at=_timestamp_from_text(row.built_at),
        build_log_reference=row.build_log_reference,
        approval_state=row.approval_state,
        approved_at=(
            None if row.approved_at is None else _timestamp_from_text(row.approved_at)
        ),
        approval_note=row.approval_note,
        requires_trusted_build_confirmation=row.requires_trusted_build_confirmation,
        trusted_build_confirmed_at=(
            None
            if row.trusted_build_confirmed_at is None
            else _timestamp_from_text(row.trusted_build_confirmed_at)
        ),
    )


class TargetRepository:
    """Persistence operations for normalized targets."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, target: Target) -> None:
        self._session.add(
            TargetRow(
                id=str(target.id),
                name=target.name,
                target_type=target.target_type,
                kodi_version=target.kodi_version,
                availability=target.availability,
                health=target.health,
                connection_state=target.connection_state,
            )
        )
        self._session.flush()

    def get(self, target_id: UUID) -> Target | None:
        row = self._session.get(TargetRow, str(target_id))
        return None if row is None else _target_from_row(row)

    def list(self) -> list[Target]:
        rows = self._session.scalars(
            select(TargetRow).order_by(TargetRow.name, TargetRow.id)
        )
        return [_target_from_row(row) for row in rows]

    def update(self, target: Target) -> None:
        row = self._session.get(TargetRow, str(target.id))
        if row is None:
            raise EntityNotFoundError(f"target {target.id} does not exist")
        row.name = target.name
        row.target_type = target.target_type
        row.kodi_version = target.kodi_version
        row.availability = target.availability
        row.health = target.health
        row.connection_state = target.connection_state
        self._session.flush()

    def delete(self, target_id: UUID) -> bool:
        row = self._session.get(TargetRow, str(target_id))
        if row is None:
            return False
        self._session.delete(row)
        self._session.flush()
        return True


class ManagedInstanceRepository:
    """Persistence operations for managed-instance subtype data."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, instance: ManagedInstance) -> None:
        self._session.add(
            ManagedInstanceRow(
                target_id=str(instance.target_id),
                instance_type=instance.instance_type,
                runtime_revision_id=str(instance.runtime_revision_id),
                runtime_state=instance.runtime_state,
                start_on_host_boot=instance.start_on_host_boot,
                notes=instance.notes,
            )
        )
        self._session.flush()

    def get(self, target_id: UUID) -> ManagedInstance | None:
        row = self._session.get(ManagedInstanceRow, str(target_id))
        return None if row is None else _instance_from_row(row)

    def list(self) -> list[ManagedInstance]:
        rows = self._session.scalars(
            select(ManagedInstanceRow).order_by(ManagedInstanceRow.target_id)
        )
        return [_instance_from_row(row) for row in rows]

    def update(self, instance: ManagedInstance) -> None:
        row = self._session.get(ManagedInstanceRow, str(instance.target_id))
        if row is None:
            raise EntityNotFoundError(
                f"managed instance {instance.target_id} does not exist"
            )
        row.instance_type = instance.instance_type
        row.runtime_revision_id = str(instance.runtime_revision_id)
        row.runtime_state = instance.runtime_state
        row.start_on_host_boot = instance.start_on_host_boot
        row.notes = instance.notes
        self._session.flush()


class RuntimeDefinitionRepository:
    """Persistence operations for logical runtime definitions."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, definition: RuntimeDefinition) -> None:
        self._session.add(
            RuntimeDefinitionRow(
                id=str(definition.id),
                slug=definition.slug,
                kodi_version=definition.kodi_version,
            )
        )
        self._session.flush()

    def get(self, definition_id: UUID) -> RuntimeDefinition | None:
        row = self._session.get(RuntimeDefinitionRow, str(definition_id))
        return None if row is None else _definition_from_row(row)

    def get_by_slug(self, slug: str) -> RuntimeDefinition | None:
        row = self._session.scalar(
            select(RuntimeDefinitionRow).where(RuntimeDefinitionRow.slug == slug)
        )
        return None if row is None else _definition_from_row(row)

    def list(self) -> list[RuntimeDefinition]:
        rows = self._session.scalars(
            select(RuntimeDefinitionRow).order_by(
                RuntimeDefinitionRow.slug, RuntimeDefinitionRow.id
            )
        )
        return [_definition_from_row(row) for row in rows]


class RuntimeRevisionRepository:
    """Creation, lookup, and explicit lifecycle changes for immutable revisions."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, revision: RuntimeRevision) -> None:
        self._session.add(
            RuntimeRevisionRow(
                id=str(revision.id),
                runtime_definition_id=str(revision.runtime_definition_id),
                revision_number=revision.revision_number,
                source_kind=revision.source_kind,
                source_reference=revision.source_reference,
                content_digest=revision.content_digest,
                manifest_reference=revision.manifest_reference,
                manifest_digest=revision.manifest_digest,
                built_at=_timestamp_to_text(revision.built_at),
                build_log_reference=revision.build_log_reference,
                approval_state=revision.approval_state,
                approved_at=(
                    None
                    if revision.approved_at is None
                    else _timestamp_to_text(revision.approved_at)
                ),
                approval_note=revision.approval_note,
                requires_trusted_build_confirmation=(
                    revision.requires_trusted_build_confirmation
                ),
                trusted_build_confirmed_at=(
                    None
                    if revision.trusted_build_confirmed_at is None
                    else _timestamp_to_text(revision.trusted_build_confirmed_at)
                ),
            )
        )
        self._session.flush()

    def get(self, revision_id: UUID) -> RuntimeRevision | None:
        row = self._session.get(RuntimeRevisionRow, str(revision_id))
        return None if row is None else _revision_from_row(row)

    def list_for_definition(self, definition_id: UUID) -> list[RuntimeRevision]:
        rows = self._session.scalars(
            select(RuntimeRevisionRow)
            .where(RuntimeRevisionRow.runtime_definition_id == str(definition_id))
            .order_by(RuntimeRevisionRow.revision_number, RuntimeRevisionRow.id)
        )
        return [_revision_from_row(row) for row in rows]

    def newest_approved(self, definition_id: UUID) -> RuntimeRevision | None:
        row = self._session.scalar(
            select(RuntimeRevisionRow)
            .where(
                RuntimeRevisionRow.runtime_definition_id == str(definition_id),
                RuntimeRevisionRow.approval_state == "approved",
            )
            .order_by(RuntimeRevisionRow.revision_number.desc())
            .limit(1)
        )
        return None if row is None else _revision_from_row(row)

    def approve(
        self, revision_id: UUID, *, approved_at: datetime, note: str | None = None
    ) -> RuntimeRevision:
        current, row = self._require_revision(revision_id)
        updated = current.approve(approved_at=approved_at, note=note)
        normalized_approved_at = updated.approved_at
        assert normalized_approved_at is not None
        row.approval_state = updated.approval_state
        row.approved_at = _timestamp_to_text(normalized_approved_at)
        row.approval_note = updated.approval_note
        self._session.flush()
        return updated

    def revoke_approval(self, revision_id: UUID) -> RuntimeRevision:
        current, row = self._require_revision(revision_id)
        updated = current.revoke_approval()
        row.approval_state = updated.approval_state
        row.approved_at = None
        row.approval_note = None
        self._session.flush()
        return updated

    def confirm_trusted_build(
        self, revision_id: UUID, *, confirmed_at: datetime
    ) -> RuntimeRevision:
        current, row = self._require_revision(revision_id)
        updated = current.confirm_trusted_build(confirmed_at=confirmed_at)
        confirmed = updated.trusted_build_confirmed_at
        assert confirmed is not None
        row.trusted_build_confirmed_at = _timestamp_to_text(confirmed)
        self._session.flush()
        return updated

    def revoke_trusted_build_confirmation(self, revision_id: UUID) -> RuntimeRevision:
        current, row = self._require_revision(revision_id)
        updated = current.revoke_trusted_build_confirmation()
        row.trusted_build_confirmed_at = None
        self._session.flush()
        return updated

    def _require_revision(
        self, revision_id: UUID
    ) -> tuple[RuntimeRevision, RuntimeRevisionRow]:
        row = self._session.get(RuntimeRevisionRow, str(revision_id))
        if row is None:
            raise EntityNotFoundError(f"runtime revision {revision_id} does not exist")
        return _revision_from_row(row), row
