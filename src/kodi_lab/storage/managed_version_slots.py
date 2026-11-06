"""Version-level profile admission shared by every managed broker entry point.

These read-only checks run inside the caller's SQLite writer transaction.
They do not select/adopt a legacy profile, approve a runtime or clear a receipt.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from kodi_lab.domain import TargetType
from kodi_lab.domain.kodi_release import KodiReleaseIdentity
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    ManagedProvisionReservationRow,
    RuntimeDefinitionRow,
    RuntimeRevisionRow,
    TargetRow,
)
from kodi_lab.storage.retained_development import RetainedContainer


@dataclass(frozen=True, slots=True)
class ManagedVersionSlot:
    generation: str
    major: int
    kodi_version: str
    canonical_target_id: UUID


def managed_version_slots() -> tuple[ManagedVersionSlot, ...]:
    """The four logical slots, independent of registration or workspace setup."""
    from kodi_lab.runtime.recipes import (
        installation_generations,
        load_installation_recipe,
        recipe_target_id,
    )

    return tuple(
        ManagedVersionSlot(
            generation,
            KodiReleaseIdentity.parse(recipe.recipe.kodi_version).major,
            recipe.recipe.kodi_version,
            recipe_target_id(recipe),
        )
        for generation in installation_generations()
        for recipe in (load_installation_recipe(generation),)
    )


class ManagedVersionSlotConflict(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def managed_profile_major(
    session: Session, target_id: str, revision_id: str | None
) -> int:
    target = session.get(TargetRow, target_id)
    instance = session.get(ManagedInstanceRow, target_id)
    revision = session.get(RuntimeRevisionRow, revision_id) if revision_id else None
    definition = (
        None
        if revision is None
        else session.get(RuntimeDefinitionRow, revision.runtime_definition_id)
    )
    if (
        target is None
        or target.target_type is not TargetType.MANAGED
        or instance is None
        or instance.runtime_revision_id != revision_id
        or definition is None
        or target.kodi_version != definition.kodi_version
    ):
        raise ManagedVersionSlotConflict("managed_version_slot_identity_unavailable")
    try:
        return KodiReleaseIdentity.parse(definition.kodi_version).major
    except ValueError as error:
        raise ManagedVersionSlotConflict(
            "managed_version_slot_identity_unavailable"
        ) from error


def require_unique_version_profile(
    session: Session,
    records: Sequence[RetainedContainer],
    *,
    target_id: str,
    revision_id: str,
    own_provision_operation_id: str | None = None,
) -> None:
    """Reserve by version, including other target IDs and uncertain creates.

    Legacy singletons remain exact legacy identities, never canonical adoption.
    Duplicate registrations/history without writable containers consume no slot.
    Unknown inventory identity fails closed even when its version is unknown.
    """
    major = managed_profile_major(session, target_id, revision_id)
    matches = 0
    for record in records:
        if (
            managed_profile_major(
                session, record.managed_instance_id, record.runtime_revision_id
            )
            != major
        ):
            continue
        matches += 1
        if record.managed_instance_id != target_id or matches > 1:
            raise ManagedVersionSlotConflict("managed_version_slot_conflict")
    for reservation in session.scalars(select(ManagedProvisionReservationRow)):
        reserved = managed_profile_major(
            session, reservation.managed_instance_id, reservation.runtime_revision_id
        )
        if reserved == major and reservation.operation_id != own_provision_operation_id:
            raise ManagedVersionSlotConflict("managed_version_slot_pending")
