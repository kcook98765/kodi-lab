"""Closed retained-profile policy shared by selection, leases and broker admission."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sqlalchemy.orm import Session

from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    ManagedInstanceRow,
    RuntimeContainerArtifactRow,
)


class RetainedContainer(Protocol):
    @property
    def managed_instance_id(self) -> str: ...
    @property
    def container_id(self) -> str: ...
    @property
    def runtime_revision_id(self) -> str | None: ...
    @property
    def image_id(self) -> str: ...


def retained_targets(session: Session) -> frozenset[str]:
    from kodi_lab.runtime.recipes import load_installation_recipe, recipe_target_id
    from kodi_lab.storage.development_sessions import DevelopmentSessionConflict

    policy = session.get(DevelopmentSessionPolicyRow, 1)
    if policy is None or not policy.retain_profiles:
        return frozenset()
    omega = policy.retained_omega_target_id
    if omega not in omega_targets():
        raise DevelopmentSessionConflict("retained_policy_requires_review")
    return frozenset(
        {
            str(recipe_target_id(load_installation_recipe(g)))
            for g in ("matrix", "nexus", "piers")
        }
        | {omega}
    )


def omega_targets() -> frozenset[str]:
    from kodi_lab.runtime.recipes import (
        load_installation_recipe,
        load_novnc_phase1_recipe,
        recipe_target_id,
    )

    return frozenset(
        str(recipe_target_id(recipe))
        for recipe in (load_installation_recipe("omega"), load_novnc_phase1_recipe())
    )


def retained_inventory(session: Session, records: Sequence[RetainedContainer]) -> None:
    """No unrelated/duplicate/repinned container is admitted as a retained slot."""
    from kodi_lab.storage.development_sessions import DevelopmentSessionConflict

    allowed = retained_targets(session)
    seen: set[str] = set()
    for record in records:
        instance = session.get(ManagedInstanceRow, record.managed_instance_id)
        artifact = (
            None
            if instance is None
            else session.get(RuntimeContainerArtifactRow, instance.runtime_revision_id)
        )
        if (
            record.managed_instance_id not in allowed
            or record.managed_instance_id in seen
            or instance is None
            or record.runtime_revision_id != instance.runtime_revision_id
            or artifact is None
            or record.image_id != artifact.image_id
        ):
            raise DevelopmentSessionConflict("retained_inventory_requires_review")
        seen.add(record.managed_instance_id)


def session_inventory_matches(
    session: Session,
    records: Sequence[RetainedContainer],
    current_target: str,
    current_container: str | None,
) -> bool:
    from kodi_lab.storage.development_sessions import DevelopmentSessionConflict

    try:
        if retained_targets(session):
            retained_inventory(session, records)
        elif len(records) != 1:
            return False
    except DevelopmentSessionConflict:
        return False
    selected = [r for r in records if r.managed_instance_id == current_target]
    return len(selected) == 1 and selected[0].container_id == current_container


def require_retained_selection(session: Session, target_id: str) -> None:
    """Early mutation admission; exact observed-container checks remain mandatory."""
    from kodi_lab.storage.development_sessions import (
        DevelopmentSessionConflict,
        active_session,
    )

    if not retained_targets(session):
        return
    current = active_session(session)
    instance = session.get(ManagedInstanceRow, target_id)
    if (
        current is None
        or current.phase != "bound"
        or current.target_id != target_id
        or instance is None
        or instance.runtime_revision_id != current.runtime_revision_id
        or current.container_id is None
    ):
        raise DevelopmentSessionConflict("retained_selection_required")
