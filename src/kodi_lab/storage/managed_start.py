"""Positive runtime admission for ordinary Start; supplies no development grant."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, true
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.domain import AgentWorkspaceBytesState, AgentWorkspaceState
from kodi_lab.storage.development_sessions import (
    DevelopmentSessionConflict,
    active_session,
)
from kodi_lab.storage.managed_activity import read_resource_policy
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionReplacementReviewRow,
    DevelopmentSourceRow,
    GlobalHeavyOperationLeaseRow,
    ManagedAgentLifecycleRequestRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedRemoteGuardRow,
    NovncSessionRow,
)
from kodi_lab.storage.retained_development import (
    RetainedContainer,
    require_runtime_reservations_idle,
    retained_inventory,
    retained_targets,
)


@dataclass(frozen=True)
class ManagedStartBinding:
    target_id: str
    revision_id: str
    image_id: str
    container_id: str
    state: str


def require_managed_start(
    session: Session,
    records: Sequence[RetainedContainer],
    *,
    target_id: str,
    revision_id: str,
    own_operation_id: str | None = None,
    now: datetime | None = None,
) -> ManagedStartBinding:
    """Read only, under the caller's writer transaction. Never clear a blocker."""
    from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
    from kodi_lab.runtime.recipes import (
        load_installation_recipe,
        load_novnc_phase1_recipe,
        recipe_target_id,
    )

    def deny(code: str) -> None:
        raise DevelopmentSessionConflict(code)

    canonical = {
        str(recipe_target_id(load_installation_recipe(g)))
        for g in ("matrix", "nexus", "omega", "piers")
    }
    canonical.add(str(recipe_target_id(load_novnc_phase1_recipe())))
    retained = retained_targets(session)
    if target_id not in (retained or canonical):
        deny("managed_start_noncanonical_target")
    instance = session.get(ManagedInstanceRow, target_id)
    matches = [item for item in records if item.managed_instance_id == target_id]
    if (
        instance is None
        or instance.runtime_revision_id != revision_id
        or len(matches) != 1
        or matches[0].runtime_revision_id != revision_id
        or matches[0].runtime_state not in {"stopped", "running"}
    ):
        deny("managed_start_identity_changed")
    target = matches[0]
    require_runtime_reservations_idle(session, target_id)
    multi = read_resource_policy(session).max_running > 1
    if retained or active_session(session) is not None:
        if retained:
            retained_inventory(session, records)
        permitted = {"stopped", "running"} if multi else {"stopped"}
        if any(
            item.runtime_state not in permitted
            for item in records
            if item.managed_instance_id != target_id
        ):
            deny("retained_other_runtime_active")
        current = active_session(session)
        if current is not None:
            selected = [
                item
                for item in records
                if item.managed_instance_id == current.target_id
            ]
            selected_instance = session.get(ManagedInstanceRow, current.target_id)
            workspace = session.get(AgentWorkspaceRow, current.workspace_id)
            source = session.get(DevelopmentSourceRow, current.source_id)
            if (
                current.phase != "bound"
                or selected_instance is None
                or selected_instance.runtime_revision_id != current.runtime_revision_id
                or len(selected) != 1
                or selected[0].container_id != current.container_id
                or selected[0].runtime_revision_id != current.runtime_revision_id
                or workspace is None
                or workspace.development_source_id != current.source_id
                or workspace.state is not AgentWorkspaceState.READY
                or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
                or source is None
                or workspace.source_addon_id != source.addon_id
            ):
                deny("development_session_requires_review")
        if any(
            lease.operation_id != own_operation_id
            for lease in session.scalars(select(ManagedInstanceMutationLeaseRow))
            if not multi or lease.managed_instance_id == target_id
        ):
            deny("development_session_busy")
        if session.scalar(select(GlobalHeavyOperationLeaseRow).limit(1)) is not None:
            deny("retained_input_or_operation_active")
        for model in (
            ManagedRemoteGuardRow,
            NovncSessionRow,
        ):
            query = select(model).limit(1)
            if multi:
                query = query.where(model.instance_id == target_id)
            if session.scalar(query) is not None:
                deny("retained_input_or_operation_active")
        if (
            session.scalar(
                select(ManagedAgentLifecycleRequestRow)
                .where(
                    (
                        (
                            ManagedAgentLifecycleRequestRow.original_instance_id
                            == target_id
                        )
                        if multi
                        else true()
                    ),
                    ManagedAgentLifecycleRequestRow.state.in_(
                        ("pending", "dispatched", "ambiguous")
                    ),
                )
                .limit(1)
            )
            is not None
        ):
            deny("retained_lifecycle_pending")
        if (
            session.scalar(
                select(DevelopmentSessionReplacementReviewRow)
                .where(
                    DevelopmentSessionReplacementReviewRow.state == "open",
                    DevelopmentSessionReplacementReviewRow.successor_generation.is_(
                        None
                    ),
                )
                .limit(1)
            )
            is not None
        ):
            deny("development_session_replacement_review_open")
        reconciliation = ManagedInstanceReconciliationService(
            sessionmaker(bind=session.get_bind())
        )
        for managed in session.scalars(select(ManagedInstanceRow)):
            if multi and managed.target_id != target_id:
                continue
            if reconciliation.status(
                UUID(managed.target_id), read_session=session
            ).required:
                deny("retained_reconciliation_required")
    return ManagedStartBinding(
        target_id,
        revision_id,
        target.image_id,
        target.container_id,
        target.runtime_state,
    )
