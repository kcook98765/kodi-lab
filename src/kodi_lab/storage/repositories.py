"""Explicit repositories for targets and managed instances."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from kodi_lab.domain import ManagedInstance, Target
from kodi_lab.storage.models import ManagedInstanceRow, TargetRow


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
        runtime_revision=row.runtime_revision,
        runtime_state=row.runtime_state,
        start_on_host_boot=row.start_on_host_boot,
        notes=row.notes,
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
                runtime_revision=instance.runtime_revision,
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
        row.runtime_revision = instance.runtime_revision
        row.runtime_state = instance.runtime_state
        row.start_on_host_boot = instance.start_on_host_boot
        row.notes = instance.notes
        self._session.flush()
