"""Principal-bound version Start/Stop over existing lifecycle and broker authority.

No acquisition, repinning, profile replacement, workspace selection or Bridge
trust is performed. Accepted child identities are never dispatched twice.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict, cast
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.lifecycle_operations import RunningLifecycleOperation
from kodi_lab.application.managed_agent_lifecycle import ManagedAgentLifecycleService
from kodi_lab.application.managed_capability_prerequisites import (
    ManagedCapabilityPrerequisiteService,
)
from kodi_lab.application.managed_instances import (
    ExactRevisionSelection,
    ManagedInstanceService,
)
from kodi_lab.application.managed_resources import ManagedResourceService
from kodi_lab.application.managed_runtime_admission import require_managed_runtime
from kodi_lab.application.mcp_workspace_development import BoundedIntervalRateLimiter
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import ManagedRuntimeStatusService
from kodi_lab.broker.operations import (
    ManagedContainerInventoryResult,
    ProvisionManagedContainerResult,
)
from kodi_lab.domain import InstanceType, ManagedInstanceLifecycleOperation
from kodi_lab.runtime.recipes import load_installation_recipe
from kodi_lab.storage.managed_activity import (
    end_running_holds,
    record_activity,
    timestamp,
)
from kodi_lab.storage.managed_clean_reset import (
    active_clean_reset,
    require_no_clean_reset,
)
from kodi_lab.storage.managed_lifecycle_owner import WEB_ADMIN_OWNER, lifecycle_actor
from kodi_lab.storage.managed_start import require_managed_start
from kodi_lab.storage.managed_version_slots import (
    ManagedVersionSlot,
    managed_profile_major,
    managed_version_slots,
    require_unique_version_profile,
)
from kodi_lab.storage.models import (
    ManagedCleanResetRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedInstanceUseRow,
    ManagedProvisionReservationRow,
    ManagedRemoteGuardRow,
    ManagedStartReservationRow,
    ManagedVersionOperationRow,
    McpClientRow,
    NovncSessionRow,
    RuntimeContainerArtifactRow,
    RuntimeRecipeInstallationRow,
    TargetRow,
)
from kodi_lab.storage.repositories import ManagedRuntimeProjectionRepository

type Version = Literal["matrix", "nexus", "omega", "piers"]
type AuthorizeVersion = Callable[[Session, UUID], None]
_ACTIVE = ("accepted", "prepared", "provisioning", "starting", "stopping", "uncertain")


class _StatusIdentity(TypedDict):
    version: str
    kodi_release: str
    authorized: bool
    target_id: UUID


class ManagedVersionError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ManagedVersionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Version
    request_id: UUID = Field(strict=False)


class ManagedStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ManagedVersionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str
    kodi_release: str
    authorized: bool
    target_id: UUID | None = None
    state: Literal["empty", "stopped", "starting", "running", "blocked"]
    code: str = Field(max_length=64)
    native_ready: bool = False
    native_readiness: str = Field(default="not_checked", max_length=64)
    bridge_readiness: Literal["blocked", "unknown"] = "unknown"
    bridge_reason: str = Field(default="not_observed", max_length=64)
    agent_readiness: Literal["not_assessed"] = "not_assessed"
    agent_reason: Literal["workspace_authority_not_assessed"] = (
        "workspace_authority_not_assessed"
    )
    operation_id: UUID | None = None
    request_id: UUID | None = None
    next_action: Literal[
        "grant_access",
        "start",
        "stop",
        "poll_original_request",
        "review_history",
        "review_bridge",
        "ask_administrator",
    ]


class ManagedVersionsStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    versions: tuple[ManagedVersionStatus, ...]


class ManagedVersionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    operation_id: UUID
    request_id: UUID
    action: Literal["start", "stop"]
    receipt_state: str = Field(max_length=16)
    code: str = Field(max_length=64)
    replayed: bool
    lifecycle_operation_id: UUID | None
    provision_operation_id: UUID | None
    hold_until: datetime | None
    stop_confirmed: bool
    recovery: str | None = Field(default=None, max_length=256)
    status: ManagedVersionStatus


class ManagedVersionLifecycleService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        lifecycle: ManagedRuntimeLifecycleService,
        resources: ManagedResourceService,
        status: ManagedRuntimeStatusService,
        installer: RuntimeInstallationService,
        agent: ManagedAgentLifecycleService,
        inventory: Callable[[], ManagedContainerInventoryResult],
        *,
        data_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        rate_limiter: BoundedIntervalRateLimiter | None = None,
    ) -> None:
        self._sessions, self._lifecycle, self._resources = (
            sessions,
            lifecycle,
            resources,
        )
        self._status, self._installer, self._agent = status, installer, agent
        self._inventory, self._data_root, self._clock = inventory, data_root, clock
        self._rate = rate_limiter or BoundedIntervalRateLimiter(5)
        self._capabilities = ManagedCapabilityPrerequisiteService(sessions)
        self._reconciliation = ManagedInstanceReconciliationService(sessions)

    @staticmethod
    def slot(version: str) -> ManagedVersionSlot:
        for slot in managed_version_slots():
            if slot.generation == version:
                return slot
        raise ManagedVersionError("managed_version_unsupported")

    def status(
        self, client_id: UUID, authorize: AuthorizeVersion
    ) -> ManagedVersionsStatus:
        values = []
        for slot in managed_version_slots():
            # Denials reveal only the public supported-version vocabulary.
            try:
                with self._sessions() as session:
                    authorize(session, slot.canonical_target_id)
            except PermissionError:
                values.append(self._denied(slot))
                continue
            value = self._observe(slot, client_id)
            try:
                with self._sessions() as session:
                    authorize(session, slot.canonical_target_id)
            except PermissionError:
                value = self._denied(slot)
            values.append(value)
        return ManagedVersionsStatus(versions=tuple(values))

    @staticmethod
    def _denied(slot: ManagedVersionSlot) -> ManagedVersionStatus:
        return ManagedVersionStatus(
            version=slot.generation,
            kodi_release=slot.kodi_version,
            authorized=False,
            state="blocked",
            code="not_authorized",
            next_action="grant_access",
        )

    def invoke(
        self,
        *,
        client_id: UUID,
        request_id: UUID,
        version: Version,
        action: Literal["start", "stop"],
        authorize: AuthorizeVersion,
    ) -> ManagedVersionResult:
        slot = self.slot(version)
        with self._sessions() as session:
            authorize(session, slot.canonical_target_id)
        # Same cross-process lock as legacy ensure/release, never an independent
        # container controller. Web is fenced by the existing typed child leases.
        with self._agent._lock(slot.canonical_target_id):
            row, replayed = self._accept(client_id, request_id, slot, action, authorize)
            try:
                self._recover(row.id)
                row = self._get(row.id)
                if row.phase == "accepted":
                    self._provision(row, authorize)
                row = self._get(row.id)
                if row.phase == "prepared":
                    if action == "start":
                        self._start(row, authorize)
                    else:
                        self._stop(row, authorize)
                self._recover(row.id)
            except Exception as error:
                if isinstance(error, PermissionError):
                    self._refuse(row.id, error)
                    raise
                self._refuse(row.id, error)
            return self._result(row.id, client_id, authorize, replayed)

    def _get(self, identity: str) -> ManagedVersionOperationRow:
        with self._sessions() as session:
            row = session.get(ManagedVersionOperationRow, identity)
            assert row is not None
            session.expunge(row)
            return row

    def _records(
        self, session: Session, slot: ManagedVersionSlot
    ) -> ManagedContainerInventoryResult:
        inventory = self._inventory()
        selected = 0
        for record in inventory.containers:
            major = managed_profile_major(
                session, record.managed_instance_id, record.runtime_revision_id
            )
            if major == slot.major and record.managed_instance_id != str(
                slot.canonical_target_id
            ):
                raise ManagedVersionError("canonical_slot_requires_review")
            if record.managed_instance_id == str(slot.canonical_target_id):
                selected += 1
                if selected > 1:
                    raise ManagedVersionError("managed_version_slot_conflict")
            if record.runtime_state not in {"running", "stopped"}:
                raise ManagedVersionError("managed_runtime_unknown")
        return inventory

    def _runtime(self, session: Session, row: ManagedVersionOperationRow) -> None:
        instance = session.get(ManagedInstanceRow, row.target_id)
        if row.runtime_revision_id is None:
            if instance is not None:
                raise ManagedVersionError("runtime_changed_since_request")
            return
        if instance is None or instance.runtime_revision_id != row.runtime_revision_id:
            raise ManagedVersionError("runtime_changed_since_request")
        # Revoking future Start eligibility must not trap an exact running
        # profile. Stop still requires the same grant, pin, image and ownership.
        if row.action == "start":
            require_managed_runtime(
                session,
                UUID(row.target_id),
                UUID(row.runtime_revision_id),
                data_root=self._data_root,
            )
        artifact = session.get(RuntimeContainerArtifactRow, row.runtime_revision_id)
        if artifact is None or artifact.image_id != row.image_id:
            raise ManagedVersionError("runtime_changed_since_request")

    def _bound(
        self, session: Session, row: ManagedVersionOperationRow, *, epoch: bool = True
    ) -> None:
        self._runtime(session, row)
        inventory = self._records(session, self.slot(row.generation))
        selected = [
            r for r in inventory.containers if r.managed_instance_id == row.target_id
        ]
        if row.runtime_revision_id is not None:
            require_unique_version_profile(
                session,
                inventory.containers,
                target_id=row.target_id,
                revision_id=row.runtime_revision_id,
            )
        if (row.container_id is None and selected) or (
            row.container_id is not None
            and (
                len(selected) != 1
                or selected[0].container_id != row.container_id
                or selected[0].runtime_revision_id != row.runtime_revision_id
                or selected[0].image_id != row.image_id
            )
        ):
            raise ManagedVersionError("profile_changed_since_request")
        if epoch and row.runtime_revision_id is not None:
            token = ManagedRuntimeProjectionRepository(session).observation_token(
                UUID(row.target_id)
            )
            if (
                token is None
                or token.runtime_state_revision != row.expected_state_revision
            ):
                raise ManagedVersionError("runtime_changed_since_request")

    def register_cached(
        self, session: Session, slot: ManagedVersionSlot, *, create_target: bool = False
    ) -> ManagedInstanceRow:
        """Reuse approved registration inside an authorized parent transaction."""
        instance = session.get(ManagedInstanceRow, str(slot.canonical_target_id))
        if instance is None:
            reviewed = load_installation_recipe(slot.generation)
            cached = session.scalar(
                select(RuntimeRecipeInstallationRow)
                .where(
                    RuntimeRecipeInstallationRow.generation == slot.generation,
                    RuntimeRecipeInstallationRow.recipe_digest == reviewed.digest,
                    RuntimeRecipeInstallationRow.state == "ready",
                )
                .order_by(RuntimeRecipeInstallationRow.updated_at.desc())
                .limit(1)
            )
            if cached is None:
                raise ManagedVersionError("approved_cached_runtime_required")
            observed = self._installer.observe_create(
                cast(Version, slot.generation), UUID(cached.id)
            )
            if (
                observed.status != "ready"
                or observed.busy
                or observed.runtime_revision_id is None
            ):
                raise ManagedVersionError("approved_cached_runtime_required")
            if (
                create_target
                and session.get(TargetRow, str(slot.canonical_target_id)) is None
            ):
                from kodi_lab.runtime.install import register_recipe_target

                register_recipe_target(session, reviewed)
            ManagedInstanceService(
                self._sessions,
                data_root=self._data_root,
                include_legacy=True,
                include_prerelease=slot.generation == "piers",
                preview_recipe_digest=reviewed.digest
                if slot.generation == "piers"
                else None,
            ).create_in_session(
                session,
                target_id=slot.canonical_target_id,
                selection=ExactRevisionSelection(
                    reviewed.recipe.definition_id,
                    observed.runtime_revision_id,
                ),
                instance_type=InstanceType.DISPOSABLE,
            )
            session.flush()
            instance = session.get(ManagedInstanceRow, str(slot.canonical_target_id))
        assert instance is not None
        return instance

    def _accept(
        self,
        client_id: UUID,
        request_id: UUID,
        slot: ManagedVersionSlot,
        action: Literal["start", "stop"],
        authorize: AuthorizeVersion,
    ) -> tuple[ManagedVersionOperationRow, bool]:
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            authorize(session, slot.canonical_target_id)
            if session.scalar(
                select(ManagedCleanResetRow.id).where(
                    ManagedCleanResetRow.client_id == str(client_id),
                    ManagedCleanResetRow.request_id == str(request_id),
                )
            ):
                raise ManagedVersionError("request_conflict")
            previous = session.scalar(
                select(ManagedVersionOperationRow).where(
                    ManagedVersionOperationRow.client_id == str(client_id),
                    ManagedVersionOperationRow.request_id == str(request_id),
                )
            )
            if previous is not None:
                if (previous.action, previous.generation, previous.target_id) != (
                    action,
                    slot.generation,
                    str(slot.canonical_target_id),
                ):
                    raise ManagedVersionError("request_conflict")
                session.expunge(previous)
                return previous, True
            count = (
                session.scalar(
                    select(func.count()).select_from(ManagedVersionOperationRow)
                )
                or 0
            )
            owned = (
                session.scalar(
                    select(func.count())
                    .select_from(ManagedVersionOperationRow)
                    .where(ManagedVersionOperationRow.client_id == str(client_id))
                )
                or 0
            )
            if count >= 32768 or owned >= 1024:
                raise ManagedVersionError("managed_version_receipt_quota")
            if not self._rate.allow(f"{client_id}:{slot.generation}:{action}"):
                raise ManagedVersionError("rate_limited")
            now = timestamp(self._clock())
            row = ManagedVersionOperationRow(
                id=str(uuid4()),
                client_id=str(client_id),
                request_id=str(request_id),
                target_id=str(slot.canonical_target_id),
                generation=slot.generation,
                action=action,
                phase="prepared",
                code="accepted",
                created_at=now,
                updated_at=now,
            )
            try:
                with session.begin_nested():
                    require_no_clean_reset(session, row.target_id)
                    if self._active_blocker(session, row.target_id):
                        raise ManagedVersionError("managed_version_operation_pending")
                    inventory = self._records(session, slot)
                    if action == "start" and any(
                        r.managed_instance_id != row.target_id
                        and r.runtime_state != "stopped"
                        for r in inventory.containers
                    ):
                        raise ManagedVersionError("other_version_running")
                    instance = (
                        self.register_cached(
                            session, slot, create_target=client_id == WEB_ADMIN_OWNER
                        )
                        if action == "start"
                        else session.get(ManagedInstanceRow, row.target_id)
                    )
                    if instance is not None:
                        row.runtime_revision_id = instance.runtime_revision_id
                        artifact = session.get(
                            RuntimeContainerArtifactRow, instance.runtime_revision_id
                        )
                        if artifact is None:
                            raise ManagedVersionError(
                                "approved_cached_runtime_required"
                            )
                        row.image_id = artifact.image_id
                        row.expected_state_revision = instance.runtime_state_revision
                    matches = [
                        r
                        for r in inventory.containers
                        if r.managed_instance_id == row.target_id
                    ]
                    row.container_id = (
                        matches[0].container_id if len(matches) == 1 else None
                    )
                    self._bound(session, row)
                    if action == "start" and not matches:
                        # Recheck even an existing empty registration's exact image.
                        cached = session.scalar(
                            select(RuntimeRecipeInstallationRow)
                            .where(
                                RuntimeRecipeInstallationRow.runtime_revision_id
                                == row.runtime_revision_id,
                                RuntimeRecipeInstallationRow.image_id == row.image_id,
                                RuntimeRecipeInstallationRow.state == "ready",
                            )
                            .limit(1)
                        )
                        if cached is None:
                            raise ManagedVersionError(
                                "approved_cached_runtime_required"
                            )
                        observed = self._installer.observe_create(
                            cast(Version, slot.generation), UUID(cached.id)
                        )
                        if (
                            observed.status != "ready"
                            or observed.busy
                            or str(observed.runtime_revision_id)
                            != row.runtime_revision_id
                            or observed.image_id != row.image_id
                        ):
                            raise ManagedVersionError(
                                "approved_cached_runtime_required"
                            )
                        row.phase = "accepted"
            except PermissionError:
                raise
            except Exception as error:
                row.runtime_revision_id = row.image_id = row.container_id = None
                row.expected_state_revision = None
                row.phase, row.code = "blocked", self._code(error)
            session.add(row)
            session.flush()
            session.expunge(row)
            return row, False

    @staticmethod
    def _code(error: Exception) -> str:
        code = getattr(error, "code", None)
        return (
            code
            if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)
            else "managed_operation_blocked"
        )

    def _active_blocker(self, session: Session, target_id: str) -> bool:
        for parent in session.scalars(
            select(ManagedVersionOperationRow).where(
                ManagedVersionOperationRow.target_id == target_id,
                ManagedVersionOperationRow.phase.in_(_ACTIVE),
            )
        ):
            owner = session.get(McpClientRow, parent.client_id)
            if (
                parent.phase in {"accepted", "prepared"}
                and parent.lifecycle_operation_id is None
                and parent.client_id != str(WEB_ADMIN_OWNER)
                and (owner is None or not owner.enabled)
            ):
                # Administrative client revocation can close only undispatched
                # work. Never clear a broker reservation, lease, profile or hold.
                idle = all(
                    session.get(model, target_id) is None
                    for model in (
                        ManagedInstanceMutationLeaseRow,
                        ManagedProvisionReservationRow,
                        ManagedStartReservationRow,
                    )
                )
                child = (
                    session.get(
                        ManagedInstanceLifecycleOperationRow,
                        parent.provision_operation_id,
                    )
                    if parent.provision_operation_id
                    else None
                )
                prepared = parent.provision_operation_id is None or (
                    child is not None
                    and child.state.value == "succeeded"
                    and child.original_managed_instance_id == target_id
                    and child.kind.value == "provision"
                    and child.id == str(uuid5(UUID(parent.id), "provision"))
                )
                if (
                    idle
                    and prepared
                    and not self._reconciliation.status(
                        UUID(target_id), read_session=session
                    ).required
                ):
                    parent.phase, parent.code = "blocked", "client_authority_revoked"
                    parent.updated_at = timestamp(self._clock())
                    continue
            if parent.phase == "uncertain" and self._reconciled(session, parent):
                parent.phase, parent.code = "blocked", "reconciled_new_request_required"
                parent.updated_at = timestamp(self._clock())
                continue
            return True
        return False

    def _reconciled(self, session: Session, row: ManagedVersionOperationRow) -> bool:
        child_id = row.lifecycle_operation_id or row.provision_operation_id
        child = (
            session.get(ManagedInstanceLifecycleOperationRow, child_id)
            if child_id
            else None
        )
        expected = row.action if row.lifecycle_operation_id else "provision"
        return (
            child is not None
            and child.state.value in {"succeeded", "failed", "reconciliation_required"}
            and child.original_managed_instance_id == row.target_id
            and child.kind.value == expected
            and child.id == str(uuid5(UUID(row.id), expected))
            and all(
                session.get(model, row.target_id) is None
                for model in (
                    ManagedInstanceMutationLeaseRow,
                    ManagedProvisionReservationRow,
                    ManagedStartReservationRow,
                )
            )
            and not self._reconciliation.status(
                UUID(row.target_id), read_session=session
            ).required
        )

    def _pending(
        self,
        session: Session,
        original: ManagedVersionOperationRow,
        phase: str,
        authorize: AuthorizeVersion,
    ) -> ManagedVersionOperationRow:
        authorize(session, UUID(original.target_id))
        row = session.get(ManagedVersionOperationRow, original.id)
        if row is None or row.phase != phase:
            raise ManagedVersionError("request_conflict")
        self._bound(session, row)
        self._idle(session, row)
        return row

    def _idle(self, session: Session, row: ManagedVersionOperationRow) -> None:
        self._lifecycle._require_no_reset(UUID(row.target_id))
        for model in (
            ManagedInstanceMutationLeaseRow,
            ManagedProvisionReservationRow,
            ManagedStartReservationRow,
        ):
            if session.get(model, row.target_id) is not None:
                raise ManagedVersionError("active_operation_requires_review")
        if (
            row.runtime_revision_id is not None
            and self._reconciliation.status(
                UUID(row.target_id), read_session=session
            ).required
        ):
            raise ManagedVersionError("operation_requires_reconciliation")
        if row.action == "stop":
            self._protect_users(session, row)

    @staticmethod
    def _protect_users(session: Session, row: ManagedVersionOperationRow) -> None:
        if (
            session.get(NovncSessionRow, row.target_id) is not None
            or session.get(ManagedRemoteGuardRow, row.target_id) is not None
        ):
            raise ManagedVersionError("active_viewer_or_input")

    def _record_start(self, session: Session, row: ManagedVersionOperationRow) -> None:
        # Activity is diagnostic/idle-stop input, never an implicit reservation.
        # Keep historical receipt hold_until values intact on replay/recovery.
        record_activity(
            session,
            UUID(row.target_id),
            actor=lifecycle_actor(row.client_id),
            now=self._clock(),
        )

    def _terminal(
        self,
        identity: str,
        session: Session,
        operation: ManagedInstanceLifecycleOperation,
    ) -> None:
        row = session.get(ManagedVersionOperationRow, identity)
        assert row is not None
        token = ManagedRuntimeProjectionRepository(session).observation_token(
            UUID(row.target_id)
        )
        row.expected_state_revision = (
            None if token is None else token.runtime_state_revision
        )
        if operation.state.value == "succeeded":
            row.phase = (
                "prepared" if operation.kind.value == "provision" else "complete"
            )
            row.code = "prepared" if row.phase == "prepared" else "start_accepted"
        else:
            row.phase = "uncertain" if operation.reconciliation_required else "blocked"
            row.code = (
                "operation_requires_reconciliation"
                if row.phase == "uncertain"
                else "lifecycle_failed"
            )
        row.updated_at = timestamp(self._clock())

    def _provision(
        self, row: ManagedVersionOperationRow, authorize: AuthorizeVersion
    ) -> None:
        child_id = uuid5(UUID(row.id), "provision")

        def admission(session: Session) -> None:
            self._pending(session, row, "accepted", authorize)

        def claimed(session: Session, handle: RunningLifecycleOperation) -> None:
            current = session.get(ManagedVersionOperationRow, row.id)
            assert current is not None
            current.phase, current.provision_operation_id = (
                "provisioning",
                str(handle.operation_id),
            )
            current.expected_state_revision = handle.runtime_state_revision + 1

        def result(
            session: Session,
            _handle: RunningLifecycleOperation,
            value: ProvisionManagedContainerResult,
        ) -> None:
            authorize(session, UUID(row.target_id))
            current = session.get(ManagedVersionOperationRow, row.id)
            if (
                current is None
                or current.phase != "provisioning"
                or not value.created
                or (
                    value.managed_instance_id,
                    value.runtime_revision_id,
                    value.image_id,
                )
                != (row.target_id, row.runtime_revision_id, row.image_id)
            ):
                raise ManagedVersionError("provision_identity_unconfirmed")
            current.container_id = value.container_id

        self._lifecycle.provision(
            UUID(row.target_id),
            operation_id=child_id,
            before_dispatch=lambda: self._before_dispatch(
                row.id, "provisioning", authorize
            ),
            precondition=admission,
            on_claim=claimed,
            on_result=result,
            on_terminal=lambda session, op: self._terminal(row.id, session, op),
        )

    def _start(
        self, row: ManagedVersionOperationRow, authorize: AuthorizeVersion
    ) -> None:
        current = self._status.get(UUID(row.target_id))
        if current.container_state == "running":
            with self._sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                saved = self._pending(session, row, "prepared", authorize)
                require_managed_start(
                    session,
                    self._inventory().containers,
                    target_id=row.target_id,
                    revision_id=str(row.runtime_revision_id),
                )
                self._record_start(session, saved)
                saved.phase, saved.code = "complete", "already_running"
            return

        def admission(session: Session) -> None:
            self._pending(session, row, "prepared", authorize)

        def claimed(session: Session, handle: RunningLifecycleOperation) -> None:
            saved = session.get(ManagedVersionOperationRow, row.id)
            assert saved is not None
            saved.phase, saved.lifecycle_operation_id = (
                "starting",
                str(handle.operation_id),
            )
            saved.expected_state_revision = handle.runtime_state_revision + 1
            # _pending fences the stopped observation and exact runtime epoch.
            end_running_holds(session, UUID(row.target_id))
            self._record_start(session, saved)

        self._lifecycle.start_managed(
            UUID(row.target_id),
            operation_id=uuid5(UUID(row.id), "start"),
            before_dispatch=lambda: self._before_dispatch(
                row.id, "starting", authorize
            ),
            precondition=admission,
            on_claim=claimed,
            on_terminal=lambda session, op: self._terminal(row.id, session, op),
        )

    def _before_dispatch(
        self, identity: str, phase: str, authorize: AuthorizeVersion
    ) -> None:
        with self._sessions() as session:
            row = session.get(ManagedVersionOperationRow, identity)
            assert row is not None
            authorize(session, UUID(row.target_id))
            if row.phase != phase:
                raise ManagedVersionError("request_conflict")
            self._bound(session, row, epoch=False)
            token = ManagedRuntimeProjectionRepository(session).observation_token(
                UUID(row.target_id)
            )
            child = (
                row.provision_operation_id
                if phase == "provisioning"
                else row.lifecycle_operation_id
            )
            lease = session.get(ManagedInstanceMutationLeaseRow, row.target_id)
            if (
                token is None
                or row.expected_state_revision != token.runtime_state_revision + 1
                or lease is None
                or lease.operation_id != child
            ):
                raise ManagedVersionError("runtime_changed_since_request")

    def _stop(
        self, row: ManagedVersionOperationRow, authorize: AuthorizeVersion
    ) -> None:
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            saved = self._pending(session, row, "prepared", authorize)
            use = session.get(
                ManagedInstanceUseRow, (row.target_id, lifecycle_actor(row.client_id))
            )
            if use is not None:
                use.hold_until, use.released_at = None, timestamp(self._clock())
            selected = [
                r
                for r in self._inventory().containers
                if r.managed_instance_id == row.target_id
            ]
            if not selected or selected[0].runtime_state == "stopped":
                saved.phase, saved.code = "complete", "already_stopped"
                return

        def authority(session: Session) -> None:
            authorize(session, UUID(row.target_id))
            self._bound(session, row, epoch=False)
            self._protect_users(session, row)

        def claimed(session: Session, handle: RunningLifecycleOperation) -> None:
            saved = session.get(ManagedVersionOperationRow, row.id)
            assert saved is not None and saved.phase == "prepared"
            saved.phase, saved.lifecycle_operation_id = (
                "stopping",
                str(handle.operation_id),
            )
            saved.expected_state_revision = handle.runtime_state_revision + 1

        self._resources.stop_if_eligible(
            UUID(row.target_id),
            reason="released",
            actor=lifecycle_actor(row.client_id),
            operation_id=uuid5(UUID(row.id), "stop"),
            authorize=authority,
            on_claim=claimed,
        )

    def _recover(self, identity: str) -> None:
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(ManagedVersionOperationRow, identity)
            assert row is not None
            if row.phase not in {"provisioning", "starting", "stopping", "uncertain"}:
                return
            if row.phase == "uncertain" and self._reconciled(session, row):
                row.phase, row.code = "blocked", "reconciled_new_request_required"
                row.updated_at = timestamp(self._clock())
                return
            child_id = (
                row.provision_operation_id
                if row.phase == "provisioning"
                else row.lifecycle_operation_id or row.provision_operation_id
            )
            child = (
                session.get(ManagedInstanceLifecycleOperationRow, child_id)
                if child_id
                else None
            )
            if child is not None:
                expected_kind = (
                    "provision"
                    if child_id == row.provision_operation_id
                    else row.action
                )
                if (
                    child.original_managed_instance_id != row.target_id
                    or child.kind.value != expected_kind
                    or child.id != str(uuid5(UUID(row.id), expected_kind))
                ):
                    child = None
            if child is not None and child.state.value == "succeeded":
                if child.kind.value == "provision":
                    if row.container_id is None:
                        row.phase, row.code = (
                            "uncertain",
                            "provision_identity_unconfirmed",
                        )
                    else:
                        self._bound(session, row)
                        row.phase, row.code = "prepared", "prepared"
                else:
                    row.phase, row.code = (
                        "complete",
                        "stopped" if row.action == "stop" else "start_accepted",
                    )
            elif child is not None and child.state.value == "failed":
                row.phase, row.code = "blocked", "lifecycle_failed"
            else:
                lease = session.get(ManagedInstanceMutationLeaseRow, row.target_id)
                live = (
                    lease is not None
                    and lease.operation_id == child_id
                    and lease.lease_expires_at is not None
                    and datetime.fromisoformat(
                        lease.lease_expires_at.replace("Z", "+00:00")
                    )
                    > self._clock()
                )
                if not live or child is None or child.reconciliation_required:
                    row.phase, row.code = (
                        "uncertain",
                        "operation_requires_reconciliation",
                    )
                if row.phase == "uncertain" and self._reconciled(session, row):
                    row.phase, row.code = "blocked", "reconciled_new_request_required"
            row.updated_at = timestamp(self._clock())

    def _refuse(self, identity: str, error: Exception) -> None:
        with self._sessions.begin() as session:
            row = session.get(ManagedVersionOperationRow, identity)
            assert row is not None
            if row.phase in {"accepted", "prepared", "blocked"}:
                row.phase, row.code = "blocked", self._code(error)
            else:
                row.phase, row.code = "uncertain", "operation_requires_reconciliation"
            row.updated_at = timestamp(self._clock())

    def _observe(
        self, slot: ManagedVersionSlot, client_id: UUID
    ) -> ManagedVersionStatus:
        base: _StatusIdentity = {
            "version": slot.generation,
            "kodi_release": slot.kodi_version,
            "authorized": True,
            "target_id": slot.canonical_target_id,
        }
        operation = request = None
        try:
            with self._sessions() as session:
                parent = session.scalar(
                    select(ManagedVersionOperationRow)
                    .where(
                        ManagedVersionOperationRow.target_id
                        == str(slot.canonical_target_id),
                        ManagedVersionOperationRow.phase.in_(_ACTIVE),
                    )
                    .order_by(ManagedVersionOperationRow.created_at.desc())
                    .limit(1)
                )
                if parent is not None and parent.client_id == str(client_id):
                    operation, request = UUID(parent.id), UUID(parent.request_id)
                resetting = active_clean_reset(session, str(slot.canonical_target_id))
                if resetting is not None:
                    owned = resetting.client_id == str(client_id)
                    return ManagedVersionStatus(
                        **base,
                        state="blocked",
                        code="managed_reset_pending",
                        operation_id=UUID(resetting.id) if owned else None,
                        request_id=UUID(resetting.request_id) if owned else None,
                        next_action="poll_original_request"
                        if owned
                        else "ask_administrator",
                    )
                inventory = self._records(session, slot)
                instance = session.get(
                    ManagedInstanceRow, str(slot.canonical_target_id)
                )
                if instance is None:
                    cached = self._installer.observe_create(
                        cast(Version, slot.generation)
                    )
                    available = cached.status == "ready" and not cached.busy
                    return ManagedVersionStatus(
                        **base,
                        state="empty",
                        code="empty"
                        if available
                        else "approved_cached_runtime_required",
                        next_action="start" if available else "ask_administrator",
                    )
                require_managed_runtime(
                    session,
                    slot.canonical_target_id,
                    UUID(instance.runtime_revision_id),
                    data_root=self._data_root,
                )
                if parent is not None:
                    lease = session.get(
                        ManagedInstanceMutationLeaseRow, parent.target_id
                    )
                    stale = parent.phase in {
                        "provisioning",
                        "starting",
                        "stopping",
                    } and (
                        lease is None
                        or lease.lease_expires_at is None
                        or datetime.fromisoformat(
                            lease.lease_expires_at.replace("Z", "+00:00")
                        )
                        <= self._clock()
                    )
                    blocked = parent.phase == "uncertain" or stale
                    return ManagedVersionStatus(
                        **base,
                        state="blocked" if blocked else "starting",
                        code="operation_requires_reconciliation"
                        if stale
                        else parent.code,
                        operation_id=operation,
                        request_id=request,
                        next_action="review_history"
                        if blocked
                        else "poll_original_request",
                    )
                require_unique_version_profile(
                    session,
                    inventory.containers,
                    target_id=str(slot.canonical_target_id),
                    revision_id=instance.runtime_revision_id,
                )
                if self._reconciliation.status(
                    slot.canonical_target_id, read_session=session
                ).required:
                    raise ManagedVersionError("operation_requires_reconciliation")
            observation = self._status.get_observation(slot.canonical_target_id)
            runtime = observation.status
            if runtime.provision_state == "unprovisioned":
                return ManagedVersionStatus(
                    **base, state="empty", code="empty", next_action="start"
                )
            capabilities = self._capabilities.inspect(slot.canonical_target_id)
            bridge: Literal["blocked", "unknown"] = (
                "blocked" if capabilities.trust_state != "active" else "unknown"
            )
            reason = (
                "bridge_recovery_required"
                if bridge == "blocked"
                else "stored_identity_requires_observation"
            )
            native = (
                runtime.container_state == "running"
                and runtime.usability_state == "usable"
            )
            state: Literal["stopped", "starting", "running", "blocked"] = "blocked"
            if runtime.container_state == "stopped":
                state = "stopped"
            elif runtime.container_state == "running":
                state = (
                    "running"
                    if native
                    else "starting"
                    if runtime.usability_state == "checking"
                    else "blocked"
                )
            return ManagedVersionStatus(
                **base,
                state=state,
                code=runtime.failure_code
                or ("native_ready" if native else runtime.usability_state.value),
                native_ready=native,
                native_readiness=runtime.usability_state.value,
                bridge_readiness=bridge,
                bridge_reason=reason,
                next_action="start"
                if state == "stopped"
                else "review_history"
                if state == "blocked"
                else "review_bridge"
                if native
                else "poll_original_request",
            )
        except Exception as error:
            return ManagedVersionStatus(
                **base,
                state="blocked",
                code=self._code(error),
                operation_id=operation,
                request_id=request,
                next_action="review_history",
            )

    def _result(
        self,
        identity: str,
        client_id: UUID,
        authorize: AuthorizeVersion,
        replayed: bool,
    ) -> ManagedVersionResult:
        row = self._get(identity)
        assert row.client_id == str(client_id)
        value = self._observe(self.slot(row.generation), client_id)
        with self._sessions() as session:
            authorize(session, UUID(row.target_id))
            stopped = row.action == "stop" and row.phase == "complete"
            if stopped:
                try:
                    self._bound(session, row)
                    selected = [
                        record
                        for record in self._inventory().containers
                        if record.managed_instance_id == row.target_id
                    ]
                    stopped = not selected or selected[0].runtime_state == "stopped"
                except Exception:
                    stopped = False
        return ManagedVersionResult(
            operation_id=UUID(row.id),
            request_id=UUID(row.request_id),
            action=cast(Literal["start", "stop"], row.action),
            receipt_state=row.phase,
            code=row.code,
            replayed=replayed,
            lifecycle_operation_id=UUID(row.lifecycle_operation_id)
            if row.lifecycle_operation_id
            else None,
            provision_operation_id=UUID(row.provision_operation_id)
            if row.provision_operation_id
            else None,
            hold_until=datetime.fromisoformat(row.hold_until.replace("Z", "+00:00"))
            if row.hold_until
            else None,
            stop_confirmed=stopped,
            recovery={
                "other_version_running": (
                    "Stop the other version safely using its own grant. Automatic "
                    "switching is not enabled."
                ),
                "approved_cached_runtime_required": (
                    "Ask the administrator to prepare an approved cached runtime. "
                    "This operation cannot download or approve images."
                ),
                "canonical_slot_requires_review": (
                    "An older or duplicate same-version profile needs administrator "
                    "review. Nothing was adopted or deleted."
                ),
                "active_viewer_or_input": (
                    "Close the human viewer or finish input cleanup first. This "
                    "operation cannot revoke another user's viewing or control "
                    "session."
                ),
                "keep_awake": (
                    "An independent hold protects Kodi. Wait for its owner to release "
                    "it; Stop does not clear another user's hold."
                ),
                "other_client_active": (
                    "Another client has recent active use. Wait for that client to "
                    "release it or ask the administrator to review."
                ),
                "operation_requires_reconciliation": (
                    "Review and reconcile the original lifecycle operation in "
                    "Advanced history. Do not redispatch it with a fresh request ID."
                ),
                "managed_version_operation_pending": (
                    "Resume the original request or wait for its owner. A fresh "
                    "request cannot bypass pending or uncertain work."
                ),
                "reconciled_new_request_required": (
                    "The original child outcome was resolved. This receipt will not "
                    "dispatch again; a new authorized intent needs a fresh request "
                    "ID."
                ),
            }.get(row.code),
            status=value,
        )
