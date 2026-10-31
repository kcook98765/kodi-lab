"""Web-owned durable reservation for one add-on and one disposable Kodi profile.

Reserving/closing never provisions, starts, stops, deletes, grants or repairs.
Existing lifecycle services own those separate actions. Generation history stays
outside disposable profiles, and no existing container can be implicitly adopted.
"""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_eligibility import (
    RuntimeRevisionEligibilityPolicy,
    RuntimeSelectionPolicy,
)
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.domain import AgentWorkspaceBytesState, AgentWorkspaceState, InstanceType
from kodi_lab.storage.development_sessions import (
    DevelopmentSessionConflict,
    active_session,
)
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    DevelopmentSessionReplacementReviewRow,
    DevelopmentSessionRow,
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    ManagedProvisionReservationRow,
    ManagedStartReservationRow,
)
from kodi_lab.storage.repositories import (
    AgentWorkspaceRepository,
    DevelopmentSourceRepository,
    ManagedInstanceRepository,
    RuntimeDefinitionRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)

# Issue #51 admits this already sealed installation, independently of the
# optional human-viewer setting. New images/revisions require separate review.
APPROVED_OMEGA_VIEW_REVISION = UUID("9ee490cd-8eee-424b-b3b2-06c2757ccaa8")
APPROVED_OMEGA_VIEW_IMAGE = (
    "sha256:8df4cbf1988514ac9713fab7702b0d5f04483f2a167ea95507d9ade66032403b"
)


def development_target_ids() -> frozenset[UUID]:
    from kodi_lab.runtime.lab import KNOWN_SELECTORS, canonical_id
    from kodi_lab.runtime.recipes import (
        load_display_candidate_recipe,
        load_novnc_phase1_recipe,
        recipe_target_id,
    )

    return frozenset(
        {canonical_id(selector) for selector in KNOWN_SELECTORS}
        | {
            recipe_target_id(load_novnc_phase1_recipe()),
            recipe_target_id(load_display_candidate_recipe("piers")),
        }
    )


class DevelopmentSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_generation: int = Field(ge=0)
    target_id: UUID
    workspace_id: UUID


class DevelopmentSessionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: int
    request_id: UUID
    target_id: UUID
    runtime_revision_id: UUID
    workspace_id: UUID
    source_id: UUID
    container_id: str | None
    phase: Literal["reserved", "provisioning", "bound", "uncertain", "closed"]
    created_at: datetime
    updated_at: datetime


class RetainedPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    expected_generation: int = Field(ge=0, strict=True)
    omega_target_id: UUID | None = None


class RetainedSelectionRequest(DevelopmentSessionRequest):
    expected_generation: int = Field(ge=0, strict=True)
    identity: str = Field(pattern=r"^[0-9a-f]{64}$")


class RetainedSelectionPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latest_generation: int
    target_id: UUID
    workspace_id: UUID
    identity: str
    provisioned: bool
    selected: bool


def _status(row: DevelopmentSessionRow) -> DevelopmentSessionStatus:
    return DevelopmentSessionStatus.model_validate(
        {key: getattr(row, key) for key in DevelopmentSessionStatus.model_fields}
    )


class DevelopmentSessionService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        inventory: Callable[[], ManagedContainerInventoryResult],
        *,
        data_root: Path,
    ) -> None:
        from kodi_lab.application.mcp_workspace_development import (
            BoundedIntervalRateLimiter,
        )

        self._selection_rate = BoundedIntervalRateLimiter(5.0)
        self._sessions, self._inventory, self._data_root = (
            sessions,
            inventory,
            data_root,
        )

    def status(self) -> DevelopmentSessionStatus | None:
        with self._sessions() as session:
            row = active_session(session)
            return None if row is None else _status(row)

    @staticmethod
    def _idle(session: Session) -> None:
        if any(
            session.scalar(select(model).limit(1)) is not None
            for model in (
                ManagedStartReservationRow,
                ManagedProvisionReservationRow,
                ManagedInstanceMutationLeaseRow,
                GlobalHeavyOperationLeaseRow,
            )
        ):
            raise DevelopmentSessionConflict("development_session_busy")

    def _runtime(self, session: Session, target_id: UUID, revision_id: UUID) -> None:
        from kodi_lab.domain import TargetType
        from kodi_lab.runtime.recipes import (
            PIERS_DISPLAY_INSTALLATION_DIGEST,
            load_display_candidate_recipe,
            load_installation_recipe,
            load_novnc_phase1_recipe,
            recipe_target_id,
        )
        from kodi_lab.storage.retained_development import retained_targets

        retained = str(target_id) in retained_targets(session)
        if target_id not in development_target_ids() and not retained:
            raise DevelopmentSessionConflict("development_session_noncanonical_target")
        target = TargetRepository(session).get(target_id)
        revision = RuntimeRevisionRepository(session).get(revision_id)
        definition = (
            None
            if revision is None
            else RuntimeDefinitionRepository(session).get(
                revision.runtime_definition_id
            )
        )
        if target is None or revision is None or definition is None:
            raise DevelopmentSessionConflict("development_session_runtime_unavailable")
        reviewed = load_novnc_phase1_recipe()
        qualified_omega = target_id == recipe_target_id(reviewed)
        if qualified_omega:
            instance = ManagedInstanceRepository(session).get(target_id)
            if (
                instance is None
                or instance.instance_type is not InstanceType.DISPOSABLE
                or instance.runtime_revision_id != revision_id
                or revision_id != APPROVED_OMEGA_VIEW_REVISION
                or target.target_type is not TargetType.MANAGED
                or target.name
                != f"{reviewed.recipe.display_generation} Lab · experimental noVNC"
                or target.kodi_version != reviewed.recipe.kodi_version
                or definition.id != reviewed.recipe.definition_id
                or revision.manifest_reference != reviewed.reference
                or revision.manifest_digest != reviewed.digest
                or revision.content_digest != APPROVED_OMEGA_VIEW_IMAGE
            ):
                raise DevelopmentSessionConflict(
                    "development_session_runtime_ineligible"
                )
        preview_digest = (
            load_installation_recipe("piers").digest
            if retained and target.kodi_version == "22.0-rc.1"
            else None
        )
        if revision.manifest_digest == PIERS_DISPLAY_INSTALLATION_DIGEST:
            candidate = load_display_candidate_recipe("piers")
            if (
                target_id == recipe_target_id(candidate)
                and definition.id == candidate.recipe.definition_id
                and revision.manifest_reference == candidate.reference
            ):
                # Selection uses the same exact sealed Preview approval as its
                # installer. It does not grant viewing or control qualification.
                preview_digest = candidate.digest
        eligibility = RuntimeRevisionEligibilityPolicy(
            data_root=self._data_root
        ).evaluate(
            session,
            target=target,
            definition=definition,
            revision=revision,
            policy=RuntimeSelectionPolicy(
                include_legacy=True,
                novnc_phase1=qualified_omega,
                include_prerelease=preview_digest is not None,
                preview_recipe_digest=preview_digest,
            ),
        )
        if not eligibility.eligible:
            raise DevelopmentSessionConflict("development_session_runtime_ineligible")

    def reserve(self, operation: DevelopmentSessionRequest) -> DevelopmentSessionStatus:
        with self._sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            prior = session.scalar(
                select(DevelopmentSessionRow).where(
                    DevelopmentSessionRow.request_id == str(operation.request_id)
                )
            )
            if prior is not None:
                if (prior.generation - 1, prior.target_id, prior.workspace_id) != (
                    operation.expected_generation,
                    str(operation.target_id),
                    str(operation.workspace_id),
                ):
                    raise DevelopmentSessionConflict(
                        "development_session_request_conflict"
                    )
                return _status(prior)
            from kodi_lab.storage.retained_development import retained_targets

            if retained_targets(session):
                raise DevelopmentSessionConflict("retained_mode_requires_selection")
            current = active_session(session)
            generation = (
                session.scalar(select(func.max(DevelopmentSessionRow.generation))) or 0
            )
            if current is not None:
                raise DevelopmentSessionConflict("development_session_already_active")
            if generation >= 256:
                raise DevelopmentSessionConflict("development_session_history_capacity")
            if generation != operation.expected_generation:
                raise DevelopmentSessionConflict(
                    "development_session_generation_conflict"
                )
            self._idle(session)
            # Inventory is read while every broker provision/start reserves through
            # the same SQLite writer boundary. An in-flight dispatch is not empty.
            if self._inventory().containers:
                raise DevelopmentSessionConflict(
                    "development_session_existing_resource"
                )
            instance = ManagedInstanceRepository(session).get(operation.target_id)
            workspace = AgentWorkspaceRepository(session).get(operation.workspace_id)
            source = (
                None
                if workspace is None
                else DevelopmentSourceRepository(session).get(
                    workspace.development_source_id
                )
            )
            if (
                instance is None
                or instance.instance_type is not InstanceType.DISPOSABLE
                or workspace is None
                or workspace.state is not AgentWorkspaceState.READY
                or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
                or source is None
                or source.addon_id != workspace.source_addon_id
            ):
                raise DevelopmentSessionConflict(
                    "development_session_selection_unavailable"
                )
            self._runtime(session, operation.target_id, instance.runtime_revision_id)
            now = timestamp(datetime.now(UTC))
            row = DevelopmentSessionRow(
                generation=generation + 1,
                request_id=str(operation.request_id),
                target_id=str(operation.target_id),
                runtime_revision_id=str(instance.runtime_revision_id),
                workspace_id=str(workspace.id),
                source_id=str(source.id),
                container_id=None,
                phase="reserved",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            policy = session.get(DevelopmentSessionPolicyRow, 1)
            if policy is None:
                policy = DevelopmentSessionPolicyRow(
                    id=1, active_generation=row.generation
                )
                session.add(policy)
            else:
                policy.active_generation = row.generation
            session.commit()
            return _status(row)

    def close_absent(self, expected_generation: int) -> DevelopmentSessionStatus:
        """Explicitly release only an absent, idle environment; retain its history."""
        with self._sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            current = active_session(session)
            if current is None or current.generation != expected_generation:
                raise DevelopmentSessionConflict(
                    "development_session_generation_conflict"
                )
            if current.phase not in {"reserved", "bound"}:
                raise DevelopmentSessionConflict("development_session_requires_review")
            if (
                session.scalar(
                    select(DevelopmentSessionReplacementReviewRow).where(
                        DevelopmentSessionReplacementReviewRow.state == "open",
                        DevelopmentSessionReplacementReviewRow.successor_generation.is_(
                            None
                        ),
                    )
                )
                is not None
            ):
                raise DevelopmentSessionConflict(
                    "development_session_replacement_review_open"
                )
            self._idle(session)
            if self._inventory().containers:
                raise DevelopmentSessionConflict(
                    "development_session_existing_resource"
                )
            current.phase, current.updated_at = "closed", timestamp(datetime.now(UTC))
            policy = session.get(DevelopmentSessionPolicyRow, 1)
            assert policy is not None
            policy.active_generation = None
            session.commit()
            return _status(current)

    def _retained_idle(self, session: Session) -> None:
        """Selection never recovers operations, clears holds, or reaps workers."""
        from datetime import timedelta

        from kodi_lab.application.reconciliation import (
            ManagedInstanceReconciliationService,
        )
        from kodi_lab.storage.managed_activity import read_resource_policy
        from kodi_lab.storage.models import (
            ManagedAgentLifecycleRequestRow,
            ManagedInstanceRow,
            ManagedInstanceUseRow,
            ManagedRemoteGuardRow,
            NovncSessionRow,
        )

        self._idle(session)
        current = active_session(session)
        if current is not None and current.phase not in {"reserved", "bound"}:
            raise DevelopmentSessionConflict("development_session_requires_review")
        if any(
            session.scalar(select(m).limit(1)) is not None
            for m in (ManagedRemoteGuardRow, NovncSessionRow)
        ):
            raise DevelopmentSessionConflict("retained_input_requires_review")
        if (
            session.scalar(
                select(ManagedAgentLifecycleRequestRow)
                .where(
                    ManagedAgentLifecycleRequestRow.state.in_(
                        ("pending", "dispatched", "ambiguous")
                    )
                )
                .limit(1)
            )
            is not None
        ):
            raise DevelopmentSessionConflict("retained_lifecycle_pending")
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
            raise DevelopmentSessionConflict(
                "development_session_replacement_review_open"
            )
        now = datetime.now(UTC)
        minutes = read_resource_policy(session).idle_minutes or 60
        for use in session.scalars(select(ManagedInstanceUseRow)):
            if use.hold_until and use.hold_until > timestamp(now):
                raise DevelopmentSessionConflict("retained_use_active")
            if (
                use.actor_key.startswith("mcp:")
                and (use.released_at is None or use.last_activity_at > use.released_at)
                and datetime.fromisoformat(use.last_activity_at.replace("Z", "+00:00"))
                + timedelta(minutes=minutes)
                > now
            ):
                raise DevelopmentSessionConflict("retained_use_active")
        reconciliation = ManagedInstanceReconciliationService(self._sessions)
        for instance in session.scalars(select(ManagedInstanceRow)):
            if reconciliation.status(
                UUID(instance.target_id), read_session=session
            ).required:
                raise DevelopmentSessionConflict("retained_reconciliation_required")

    @staticmethod
    def _latest(session: Session) -> int:
        return session.scalar(select(func.max(DevelopmentSessionRow.generation))) or 0

    def configure_retained(self, operation: RetainedPolicyRequest) -> dict[str, object]:
        from kodi_lab.storage.retained_development import (
            omega_targets,
            retained_inventory,
            retained_targets,
            session_inventory_matches,
        )

        with self._sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if operation.expected_generation != self._latest(session):
                raise DevelopmentSessionConflict(
                    "development_session_generation_conflict"
                )
            self._retained_idle(session)
            records = self._inventory().containers
            if any(r.runtime_state != "stopped" for r in records):
                raise DevelopmentSessionConflict("retained_runtime_active")
            policy = session.get(DevelopmentSessionPolicyRow, 1)
            if policy is None:
                policy = DevelopmentSessionPolicyRow(id=1, active_generation=None)
                session.add(policy)
            if operation.enabled:
                if str(operation.omega_target_id) not in omega_targets():
                    raise DevelopmentSessionConflict("retained_target_not_enrolled")
                policy.retain_profiles = True
                policy.retained_omega_target_id = str(operation.omega_target_id)
                session.flush()
                retained_inventory(session, records)
                current = active_session(session)
                if current is not None and current.target_id not in retained_targets(
                    session
                ):
                    raise DevelopmentSessionConflict("retained_target_not_enrolled")
                if (
                    current is not None
                    and current.phase == "bound"
                    and not session_inventory_matches(
                        session, records, current.target_id, current.container_id
                    )
                ):
                    raise DevelopmentSessionConflict(
                        "development_session_identity_changed"
                    )
            else:
                current = active_session(session)
                if len(records) > 1 or (
                    records
                    and (
                        current is None
                        or records[0].managed_instance_id != current.target_id
                        or records[0].container_id != current.container_id
                    )
                ):
                    raise DevelopmentSessionConflict("retained_profiles_still_present")
                policy.retain_profiles = False
                policy.retained_omega_target_id = None
            session.commit()
            return {
                "enabled": operation.enabled,
                "running_limit": 1 if operation.enabled else None,
            }

    def _selection_snapshot(
        self, session: Session, target_id: UUID, workspace_id: UUID
    ) -> tuple[str, str | None]:
        from kodi_lab.storage.retained_development import (
            retained_inventory,
            retained_targets,
        )

        if str(target_id) not in retained_targets(session):
            raise DevelopmentSessionConflict("retained_target_not_enrolled")
        instance = ManagedInstanceRepository(session).get(target_id)
        workspace = AgentWorkspaceRepository(session).get(workspace_id)
        source = (
            None
            if workspace is None
            else DevelopmentSourceRepository(session).get(
                workspace.development_source_id
            )
        )
        if (
            instance is None
            or instance.instance_type is not InstanceType.DISPOSABLE
            or workspace is None
            or workspace.state is not AgentWorkspaceState.READY
            or workspace.bytes_state is not AgentWorkspaceBytesState.PRESENT
            or source is None
            or source.addon_id != workspace.source_addon_id
        ):
            raise DevelopmentSessionConflict(
                "development_session_selection_unavailable"
            )
        self._runtime(session, target_id, instance.runtime_revision_id)
        records = self._inventory().containers
        retained_inventory(session, records)
        selected = next(
            (r for r in records if r.managed_instance_id == str(target_id)), None
        )
        identity = hashlib.sha256(
            json.dumps(
                {
                    "target": str(target_id),
                    "runtime": str(instance.runtime_revision_id),
                    "container": None if selected is None else selected.container_id,
                    "image": None if selected is None else selected.image_id,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return identity, None if selected is None else selected.container_id

    def selection_preview(
        self,
        target_id: UUID,
        workspace_id: UUID,
        *,
        authorize: Callable[[Session], None] | None = None,
    ) -> RetainedSelectionPreview:
        with self._sessions() as session:
            if authorize is not None:
                authorize(session)
            identity, container_id = self._selection_snapshot(
                session, target_id, workspace_id
            )
            current = active_session(session)
            result = RetainedSelectionPreview(
                latest_generation=self._latest(session),
                target_id=target_id,
                workspace_id=workspace_id,
                identity=identity,
                provisioned=container_id is not None,
                selected=current is not None
                and current.target_id == str(target_id)
                and current.workspace_id == str(workspace_id),
            )
            if authorize is not None:
                authorize(session)
            return result

    def select_retained(
        self,
        operation: RetainedSelectionRequest,
        *,
        actor: str = "web_admin",
        authorize: Callable[[Session], None] | None = None,
    ) -> DevelopmentSessionStatus:
        from kodi_lab.storage.models import DevelopmentSelectionRow
        from kodi_lab.storage.retained_development import retained_targets

        if actor != "web_admin" and (
            not actor.startswith("mcp:") or str(UUID(actor[4:])) != actor[4:]
        ):
            raise ValueError("selection actor must be server derived")
        fingerprint = hashlib.sha256(operation.model_dump_json().encode()).hexdigest()
        with self._sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if authorize is not None:
                authorize(session)
            if not retained_targets(session):
                raise DevelopmentSessionConflict("retained_policy_disabled")
            prior = session.get(
                DevelopmentSelectionRow, (actor, str(operation.request_id))
            )
            if prior is not None:
                if prior.fingerprint != fingerprint:
                    raise DevelopmentSessionConflict(
                        "development_session_request_conflict"
                    )
                row = session.get(DevelopmentSessionRow, prior.session_generation)
                if row is None:
                    raise DevelopmentSessionConflict(
                        "development_session_requires_review"
                    )
                return _status(row)  # Historical receipt; never republish selection.
            generation = self._latest(session)
            if generation != operation.expected_generation:
                raise DevelopmentSessionConflict(
                    "development_session_generation_conflict"
                )
            if generation >= 256:
                raise DevelopmentSessionConflict("development_session_history_capacity")
            self._retained_idle(session)
            records = self._inventory().containers
            if any(r.runtime_state != "stopped" for r in records):
                raise DevelopmentSessionConflict("retained_runtime_active")
            identity, container_id = self._selection_snapshot(
                session, operation.target_id, operation.workspace_id
            )
            if identity != operation.identity:
                raise DevelopmentSessionConflict("development_session_identity_changed")
            if not self._selection_rate.allow(actor):
                raise DevelopmentSessionConflict("retained_selection_rate_limited")
            instance = ManagedInstanceRepository(session).get(operation.target_id)
            workspace = AgentWorkspaceRepository(session).get(operation.workspace_id)
            assert instance is not None and workspace is not None
            now = timestamp(datetime.now(UTC))
            current = active_session(session)
            if current is not None:
                current.phase, current.updated_at = "closed", now
            row = DevelopmentSessionRow(
                generation=generation + 1,
                request_id=str(
                    uuid5(
                        NAMESPACE_URL,
                        "kodi-lab:retained-select:"
                        + actor
                        + ":"
                        + str(operation.request_id),
                    )
                ),
                target_id=str(operation.target_id),
                runtime_revision_id=str(instance.runtime_revision_id),
                workspace_id=str(workspace.id),
                source_id=str(workspace.development_source_id),
                container_id=container_id,
                phase="bound" if container_id else "reserved",
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            policy = session.get(DevelopmentSessionPolicyRow, 1)
            assert policy is not None
            policy.active_generation = row.generation
            session.add(
                DevelopmentSelectionRow(
                    actor=actor,
                    request_id=str(operation.request_id),
                    fingerprint=fingerprint,
                    session_generation=row.generation,
                )
            )
            session.commit()
            return _status(row)
