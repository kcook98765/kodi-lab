"""Fixture-free managed version replacement over exact typed lifecycle authority."""

from datetime import datetime
from typing import Literal, cast
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from kodi_lab.application.lifecycle_operations import RunningLifecycleOperation
from kodi_lab.application.managed_versions import (
    AuthorizeVersion,
    ManagedProfileIdentity,
    ManagedVersionError,
    ManagedVersionInput,
    ManagedVersionLifecycleService,
    ManagedVersionStatus,
    Version,
)
from kodi_lab.application.runtime_retirement_receipts import (
    RuntimeRetirementScope,
    retirement_receipt_in_session,
)
from kodi_lab.broker.operations import ProvisionManagedContainerResult
from kodi_lab.domain import InstanceType, ManagedInstanceLifecycleOperation
from kodi_lab.storage.development_sessions import active_session
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.managed_clean_reset import require_no_clean_reset
from kodi_lab.storage.managed_lifecycle_owner import WEB_ADMIN_OWNER, lifecycle_actor
from kodi_lab.storage.models import (
    DevelopmentSessionPolicyRow,
    DevelopmentSessionRow,
    ManagedCleanResetRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedInstanceUseRow,
    ManagedProvisionReservationRow,
    ManagedScreenshotRow,
    ManagedStartReservationRow,
    ManagedVersionOperationRow,
    RuntimeContainerArtifactRow,
    RuntimeRecipeInstallationRow,
)


class ManagedResetInput(ManagedVersionInput):
    restart: bool = False
    expected_profile: ManagedProfileIdentity | None = None
    resume_bridge_version: int | None = Field(default=None, ge=0)


class ManagedResetBridgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID | None = None
    checkpoint: str = Field(max_length=32)
    version: int | None = None
    reason: str | None = Field(default=None, max_length=64)
    stored_trust: str | None = Field(default=None, max_length=32)
    live_readiness: Literal["not_observed"] = "not_observed"


class ManagedResetResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    operation_id: UUID
    request_id: UUID
    restart: bool
    replayed: bool
    phase: str = Field(max_length=16)
    code: str = Field(max_length=64)
    stop_operation_id: UUID | None
    retirement_operation_id: UUID | None
    provision_operation_id: UUID | None
    start_operation_id: UUID | None
    retirement: Literal["not_required", "not_confirmed", "confirmed"]
    replacement: Literal["not_confirmed", "confirmed"]
    replacement_current: bool
    start: Literal["not_requested", "not_confirmed", "accepted"]
    profile_bound_state: Literal["not_confirmed", "invalidated"]
    recovery: str = Field(max_length=256)
    status: ManagedVersionStatus
    bridge_preparation: ManagedResetBridgeResult | None = None


class ManagedCleanResetService:
    def __init__(self, versions: ManagedVersionLifecycleService) -> None:
        self._v = versions
        self._sessions = versions._sessions

    def _get(self, identity: str) -> ManagedCleanResetRow:
        with self._sessions() as session:
            row = session.get(ManagedCleanResetRow, identity)
            assert row is not None
            session.expunge(row)
            return row

    def _cached(self, session: Session, row: ManagedCleanResetRow) -> None:
        cached = session.scalar(
            select(RuntimeRecipeInstallationRow)
            .where(
                RuntimeRecipeInstallationRow.runtime_revision_id
                == row.runtime_revision_id,
                RuntimeRecipeInstallationRow.image_id == row.image_id,
                RuntimeRecipeInstallationRow.generation == row.generation,
                RuntimeRecipeInstallationRow.state == "ready",
            )
            .order_by(RuntimeRecipeInstallationRow.updated_at.desc())
            .limit(1)
        )
        if cached is None:
            raise ManagedVersionError("approved_cached_runtime_required")
        observed = self._v._installer.observe_create(
            cast(Version, row.generation), UUID(cached.id)
        )
        if (
            observed.status != "ready"
            or observed.busy
            or str(observed.runtime_revision_id) != row.runtime_revision_id
            or observed.image_id != row.image_id
        ):
            raise ManagedVersionError("approved_cached_runtime_required")

    @staticmethod
    def _binding(row: ManagedCleanResetRow) -> ManagedVersionOperationRow:
        old = row.phase in {"accepted", "stopping", "stopped", "removing"} or (
            row.phase == "uncertain" and row.step in {"stop", "remove"}
        )
        return ManagedVersionOperationRow(
            target_id=row.target_id,
            generation=row.generation,
            action="start",
            runtime_revision_id=row.runtime_revision_id,
            image_id=row.image_id,
            container_id=row.old_container_id if old else row.new_container_id,
            expected_state_revision=row.expected_state_revision,
        )

    def _guard(
        self,
        session: Session,
        row: ManagedCleanResetRow,
        authorize: AuthorizeVersion,
        *,
        dispatch: bool = False,
    ) -> None:
        authorize(session, UUID(row.target_id))
        bound = self._binding(row)
        self._v._bound(session, bound, epoch=not dispatch)
        self._cached(session, row)
        self._v._protect_users(session, bound)
        instance = session.get(ManagedInstanceRow, row.target_id)
        if instance is None or instance.instance_type is not InstanceType.DISPOSABLE:
            raise ManagedVersionError("persistent_reset_unsupported")
        if dispatch:
            lease = session.get(ManagedInstanceMutationLeaseRow, row.target_id)
            assert row.step is not None
            child_id = getattr(row, f"{row.step}_id")
            if (
                lease is None
                or lease.operation_id != child_id
                or instance.runtime_state_revision + 1 != row.expected_state_revision
            ):
                raise ManagedVersionError("runtime_changed_since_request")
        else:
            self._v._idle(session, bound)
        # Also protect a stopped profile from another client's recent use/hold.
        self._v._resources._guard(
            session,
            UUID(row.target_id),
            reason="reset",
            actor=lifecycle_actor(row.client_id),
            own_operation_id=UUID(getattr(row, f"{row.step}_id")) if dispatch else None,
        )

    def _accept(
        self, client_id: UUID, operation: ManagedResetInput, authorize: AuthorizeVersion
    ) -> tuple[ManagedCleanResetRow, bool]:
        slot = self._v.slot(operation.version)
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            authorize(session, slot.canonical_target_id)
            row = session.scalar(
                select(ManagedCleanResetRow).where(
                    ManagedCleanResetRow.client_id == str(client_id),
                    ManagedCleanResetRow.request_id == str(operation.request_id),
                )
            )
            if row is not None:
                if (row.generation, row.restart, row.target_id) != (
                    operation.version,
                    operation.restart,
                    str(slot.canonical_target_id),
                ):
                    raise ManagedVersionError("request_conflict")
                if operation.expected_profile is not None and (
                    operation.expected_profile.container_id,
                    str(operation.expected_profile.runtime_revision_id),
                    operation.expected_profile.image_id,
                ) != (row.old_container_id, row.runtime_revision_id, row.image_id):
                    raise ManagedVersionError("request_conflict")
                session.expunge(row)
                return row, True
            if session.scalar(
                select(ManagedVersionOperationRow.id).where(
                    ManagedVersionOperationRow.client_id == str(client_id),
                    ManagedVersionOperationRow.request_id == str(operation.request_id),
                )
            ):
                raise ManagedVersionError("request_conflict")
            require_no_clean_reset(session, str(slot.canonical_target_id))
            if self._v._active_blocker(session, str(slot.canonical_target_id)):
                raise ManagedVersionError("managed_version_operation_pending")
            count = (
                session.scalar(select(func.count()).select_from(ManagedCleanResetRow))
                or 0
            )
            owned = (
                session.scalar(
                    select(func.count())
                    .select_from(ManagedCleanResetRow)
                    .where(ManagedCleanResetRow.client_id == str(client_id))
                )
                or 0
            )
            if count >= 32768 or owned >= 1024:
                raise ManagedVersionError("managed_version_receipt_quota")
            if not self._v._rate.allow(f"{client_id}:{slot.generation}:reset"):
                raise ManagedVersionError("rate_limited")
            inventory = self._v._records(session, slot)
            instance = self._v.register_cached(
                session, slot, create_target=client_id == WEB_ADMIN_OWNER
            )
            artifact = session.get(
                RuntimeContainerArtifactRow, instance.runtime_revision_id
            )
            if artifact is None:
                raise ManagedVersionError("approved_cached_runtime_required")
            selected = [
                r
                for r in inventory.containers
                if r.managed_instance_id == str(slot.canonical_target_id)
            ]
            if operation.expected_profile is not None and (
                len(selected) != 1
                or (
                    selected[0].container_id,
                    selected[0].runtime_revision_id,
                    selected[0].image_id,
                )
                != (
                    operation.expected_profile.container_id,
                    str(operation.expected_profile.runtime_revision_id),
                    operation.expected_profile.image_id,
                )
            ):
                raise ManagedVersionError("managed_reset_identity_changed")
            current = active_session(session)
            old = selected[0].container_id if selected else None
            if current is not None and current.target_id == str(
                slot.canonical_target_id
            ):
                if current.phase != "bound" or current.container_id != old:
                    raise ManagedVersionError("development_session_requires_review")
                generation = current.generation
            else:
                generation = None
            identity = uuid4()
            now = timestamp(self._v._clock())
            row = ManagedCleanResetRow(
                id=str(identity),
                client_id=str(client_id),
                request_id=str(operation.request_id),
                generation=operation.version,
                target_id=str(slot.canonical_target_id),
                restart=operation.restart,
                runtime_revision_id=instance.runtime_revision_id,
                image_id=artifact.image_id,
                old_container_id=old,
                selected_generation=generation,
                expected_state_revision=instance.runtime_state_revision,
                phase="accepted",
                code="accepted",
                created_at=now,
                updated_at=now,
                stop_id=str(uuid5(identity, "stop")),
                remove_id=str(uuid5(identity, "remove")),
                provision_id=str(uuid5(identity, "provision")),
                start_id=str(uuid5(identity, "start")),
            )
            # Publish the exact binding inside this accepting transaction before
            # reauthorization. Empty-slot registration has legitimately changed
            # the observed snapshot; every failed guard rolls all of this back.
            session.add(row)
            session.flush()
            # Ignore only this caller's own hold; actual release happens
            # after acceptance.
            use = session.get(
                ManagedInstanceUseRow, (row.target_id, lifecycle_actor(row.client_id))
            )
            original_hold = None if use is None else use.hold_until
            if use is not None:
                use.hold_until = None
                session.flush()
            self._guard(session, row, authorize)
            if use is not None:
                use.hold_until = original_hold
                session.flush()
            session.expunge(row)
            return row, False

    def invoke(
        self, client_id: UUID, operation: ManagedResetInput, authorize: AuthorizeVersion
    ) -> ManagedResetResult:
        slot = self._v.slot(operation.version)
        with self._sessions() as session:
            authorize(session, slot.canonical_target_id)
        with self._v._agent._lock(slot.canonical_target_id):
            try:
                row, replayed = self._accept(client_id, operation, authorize)
            except PermissionError:
                raise
            except Exception as error:
                raise ManagedVersionError(self._v._code(error)) from error
            try:
                self._refresh(row.id)
                for _ in range(5):
                    row = self._get(row.id)
                    phase = row.phase
                    if phase == "accepted":
                        self._stop(row, authorize)
                    elif phase == "stopped":
                        self._remove(row, authorize)
                    elif phase == "retired":
                        self._provision(row, authorize)
                    elif phase == "prepared":
                        self._start(row, authorize)
                    else:
                        break
                    self._refresh(row.id)
            except Exception as error:
                with self._sessions.begin() as session:
                    saved = session.get(ManagedCleanResetRow, row.id)
                    assert saved is not None
                    # A committed child claim is never retried after an exception.
                    if saved.phase in {
                        "stopping",
                        "removing",
                        "provisioning",
                        "starting",
                        "uncertain",
                    }:
                        saved.phase, saved.code = (
                            "uncertain",
                            "operation_requires_reconciliation",
                        )
                    else:
                        saved.phase, saved.code = "blocked", self._v._code(error)
                    saved.updated_at = timestamp(self._v._clock())
                if isinstance(error, PermissionError):
                    raise
                self._refresh(row.id)
            return self._result(row.id, authorize, replayed)

    def _release_own(self, session: Session, row: ManagedCleanResetRow) -> None:
        use = session.get(
            ManagedInstanceUseRow, (row.target_id, lifecycle_actor(row.client_id))
        )
        if use is not None:
            use.hold_until, use.released_at = None, timestamp(self._v._clock())
            session.flush()

    def _stop(self, row: ManagedCleanResetRow, authorize: AuthorizeVersion) -> None:
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            saved = session.get(ManagedCleanResetRow, row.id)
            assert saved is not None and saved.phase == "accepted"
            self._release_own(session, saved)
            self._guard(session, saved, authorize)
            selected = [
                r
                for r in self._v._inventory().containers
                if r.managed_instance_id == row.target_id
            ]
            if not selected or selected[0].runtime_state == "stopped":
                saved.phase, saved.code = "stopped", "stopped"
                return
        self._v._resources.stop_if_eligible(
            UUID(row.target_id),
            reason="released",
            actor=lifecycle_actor(row.client_id),
            operation_id=UUID(row.stop_id),
            authorize=lambda session: self._authority(session, row.id, authorize),
            on_claim=lambda session, handle: self._claim(
                session, row.id, "stop", "stopping", handle
            ),
        )

    def _authority(
        self, session: Session, identity: str, authorize: AuthorizeVersion
    ) -> None:
        row = session.get(ManagedCleanResetRow, identity)
        assert row is not None
        self._guard(
            session,
            row,
            authorize,
            dispatch=row.phase in {"stopping", "removing", "provisioning", "starting"},
        )

    def _claim(
        self,
        session: Session,
        identity: str,
        step: str,
        phase: str,
        handle: RunningLifecycleOperation,
    ) -> None:
        row = session.get(ManagedCleanResetRow, identity)
        assert row is not None and getattr(row, f"{step}_id") == str(
            handle.operation_id
        )
        row.phase, row.step = phase, step
        row.expected_state_revision = handle.runtime_state_revision + 1
        row.updated_at = timestamp(self._v._clock())

    def _before_dispatch(self, identity: str, authorize: AuthorizeVersion) -> None:
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            self._authority(session, identity, authorize)
            row = session.get(ManagedCleanResetRow, identity)
            assert row is not None
            if row.phase == "removing":
                row.remove_dispatched_at = timestamp(self._v._clock())

    @staticmethod
    def _scope(row: ManagedCleanResetRow) -> RuntimeRetirementScope:
        assert row.old_container_id is not None
        return RuntimeRetirementScope(
            managed_instance_id=UUID(row.target_id),
            runtime_revision_id=UUID(row.runtime_revision_id),
            container_id=row.old_container_id,
            image_id=row.image_id,
        )

    def _remove(self, row: ManagedCleanResetRow, authorize: AuthorizeVersion) -> None:
        if row.old_container_id is None:
            with self._sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                self._authority(session, row.id, authorize)
                saved = session.get(ManagedCleanResetRow, row.id)
                assert saved is not None
                saved.phase, saved.code = "retired", "empty_no_retirement"
                self._invalidate(session, saved)
            return

        def retired(session: Session, child: ManagedInstanceLifecycleOperation) -> None:
            if child.state.value == "succeeded":
                saved = session.get(ManagedCleanResetRow, row.id)
                assert saved is not None
                self._invalidate(session, saved)

        self._v._lifecycle.deprovision(
            UUID(row.target_id),
            expected_scope=self._scope(row),
            on_terminal=retired,
            operation_id=UUID(row.remove_id),
            precondition=lambda session: self._authority(session, row.id, authorize),
            before_dispatch=lambda: self._before_dispatch(row.id, authorize),
            on_claim=lambda session, handle: self._claim(
                session, row.id, "remove", "removing", handle
            ),
        )

    def _invalidate(self, session: Session, row: ManagedCleanResetRow) -> None:
        if row.invalidated_at is not None:
            return
        if row.old_container_id is not None:
            receipt = retirement_receipt_in_session(session, UUID(row.remove_id))
            if receipt.confirmation != "confirmed" or receipt.scope != self._scope(row):
                raise ManagedVersionError("retirement_unconfirmed")
        from kodi_lab.application.addon_zip_installations import (
            invalidate_addon_zip_installation_evidence,
        )
        from kodi_lab.application.development_source_deployments import (
            invalidate_development_source_deployments,
        )
        from kodi_lab.application.managed_bridge_trust import (
            invalidate_managed_bridge_trust,
        )

        if row.old_container_id is None:
            # Exact typed deprovision already invalidates these for an old profile.
            # Empty-slot preparation must also retire any stale stored evidence.
            invalidate_development_source_deployments(session, UUID(row.target_id))
            invalidate_addon_zip_installation_evidence(session, UUID(row.target_id))
            invalidate_managed_bridge_trust(
                session, UUID(row.target_id), defer_retirement=True
            )
        current = active_session(session)
        if row.selected_generation is not None:
            original = session.get(DevelopmentSessionRow, row.selected_generation)
            if original is None or (
                original.target_id,
                original.container_id,
                original.runtime_revision_id,
            ) != (row.target_id, row.old_container_id, row.runtime_revision_id):
                raise ManagedVersionError("development_session_identity_changed")
            if current is not None and current.generation == original.generation:
                policy = session.get(DevelopmentSessionPolicyRow, 1)
                assert policy is not None and original.phase == "bound"
                original.phase, original.updated_at = (
                    "closed",
                    timestamp(self._v._clock()),
                )
                policy.active_generation = None
            elif original.phase != "closed":
                raise ManagedVersionError("development_session_identity_changed")
        elif current is not None and current.target_id == row.target_id:
            raise ManagedVersionError("development_session_identity_changed")
        # Historical deployment/remote receipts retain their original identity.
        # Current unbound screenshot metadata must not label old pixels as new.
        screenshot = session.get(ManagedScreenshotRow, row.target_id)
        if screenshot is not None:
            for key in (
                "capture_id",
                "captured_at",
                "mime_type",
                "width",
                "height",
                "byte_size",
            ):
                setattr(screenshot, key, None)
            screenshot.last_error_code = "profile_replaced"
        row.invalidated_at = timestamp(self._v._clock())
        session.flush()

    def _provision(
        self, row: ManagedCleanResetRow, authorize: AuthorizeVersion
    ) -> None:
        def admission(session: Session) -> None:
            self._authority(session, row.id, authorize)
            saved = session.get(ManagedCleanResetRow, row.id)
            assert saved is not None and saved.phase == "retired"
            self._invalidate(session, saved)

        def result(
            session: Session,
            _handle: RunningLifecycleOperation,
            value: ProvisionManagedContainerResult,
        ) -> None:
            authorize(session, UUID(row.target_id))
            saved = session.get(ManagedCleanResetRow, row.id)
            assert saved is not None
            if (
                not value.created
                or value.container_id == row.old_container_id
                or (
                    value.managed_instance_id,
                    value.runtime_revision_id,
                    value.image_id,
                )
                != (row.target_id, row.runtime_revision_id, row.image_id)
            ):
                raise ManagedVersionError("replacement_identity_unconfirmed")
            saved.new_container_id = value.container_id

        self._v._lifecycle.provision(
            UUID(row.target_id),
            operation_id=UUID(row.provision_id),
            precondition=admission,
            before_dispatch=lambda: self._before_dispatch(row.id, authorize),
            on_claim=lambda session, handle: self._claim(
                session, row.id, "provision", "provisioning", handle
            ),
            on_result=result,
        )

    def _start(self, row: ManagedCleanResetRow, authorize: AuthorizeVersion) -> None:
        if not row.restart:
            with self._sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                self._authority(session, row.id, authorize)
                saved = session.get(ManagedCleanResetRow, row.id)
                assert saved is not None
                saved.phase, saved.code = "complete", "clean_stopped"
            return
        with self._sessions() as session:
            self._authority(session, row.id, authorize)
            if any(
                record.managed_instance_id != row.target_id
                and record.runtime_state != "stopped"
                for record in self._v._records(
                    session, self._v.slot(row.generation)
                ).containers
            ):
                raise ManagedVersionError("other_version_running")
        self._v._lifecycle.start_managed(
            UUID(row.target_id),
            operation_id=UUID(row.start_id),
            precondition=lambda session: self._authority(session, row.id, authorize),
            before_dispatch=lambda: self._before_dispatch(row.id, authorize),
            on_claim=lambda session, handle: self._claim(
                session, row.id, "start", "starting", handle
            ),
        )

    def _refresh(self, identity: str) -> None:
        with self._sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(ManagedCleanResetRow, identity)
            assert row is not None
            if row.phase not in {
                "stopping",
                "removing",
                "provisioning",
                "starting",
                "uncertain",
            }:
                return
            assert row.step is not None
            child_id = getattr(row, f"{row.step}_id")
            child = session.get(ManagedInstanceLifecycleOperationRow, child_id)
            kind = "deprovision" if row.step == "remove" else row.step
            if (
                child is None
                or child.original_managed_instance_id != row.target_id
                or child.kind.value != kind
            ):
                row.phase, row.code = "uncertain", "operation_requires_reconciliation"
                return
            if child.state.value == "succeeded" and not child.reconciliation_required:
                if row.step == "remove":
                    receipt = retirement_receipt_in_session(
                        session, UUID(row.remove_id)
                    )
                    if (
                        receipt.confirmation != "confirmed"
                        or receipt.scope != self._scope(row)
                    ):
                        row.phase, row.code = "uncertain", "retirement_unconfirmed"
                        return
                if row.step == "provision" and row.new_container_id is None:
                    row.phase, row.code = (
                        "uncertain",
                        "replacement_identity_unconfirmed",
                    )
                    return
                instance = session.get(ManagedInstanceRow, row.target_id)
                if (
                    instance is None
                    or instance.runtime_state_revision != row.expected_state_revision
                ):
                    row.phase, row.code = "uncertain", "runtime_changed_since_request"
                    return
                row.phase = {
                    "stop": "stopped",
                    "remove": "retired",
                    "provision": "prepared",
                    "start": "complete",
                }[row.step]
                row.code = "clean_start_accepted" if row.step == "start" else row.phase
            elif (
                child.state.value == "failed"
                and (row.step != "remove" or row.remove_dispatched_at is None)
                and not child.reconciliation_required
                and all(
                    session.get(model, row.target_id) is None
                    for model in (
                        ManagedInstanceMutationLeaseRow,
                        ManagedProvisionReservationRow,
                        ManagedStartReservationRow,
                    )
                )
            ):
                row.phase, row.code = (
                    "blocked",
                    "replacement_failed"
                    if row.step == "provision"
                    else "lifecycle_failed",
                )
            else:
                lease = session.get(ManagedInstanceMutationLeaseRow, row.target_id)
                live = (
                    lease is not None
                    and lease.operation_id == child_id
                    and lease.lease_expires_at is not None
                    and datetime.fromisoformat(
                        lease.lease_expires_at.replace("Z", "+00:00")
                    )
                    > self._v._clock()
                )
                if not live or child.reconciliation_required:
                    row.phase, row.code = (
                        "uncertain",
                        "operation_requires_reconciliation",
                    )
            row.updated_at = timestamp(self._v._clock())

    def _result(
        self, identity: str, authorize: AuthorizeVersion, replayed: bool
    ) -> ManagedResetResult:
        row = self._get(identity)
        status = self._v._observe(self._v.slot(row.generation), UUID(row.client_id))
        with self._sessions() as session:
            authorize(session, UUID(row.target_id))
            retired: Literal["not_required", "not_confirmed", "confirmed"] = (
                "not_required" if row.old_container_id is None else "not_confirmed"
            )
            if (
                row.old_container_id is not None
                and session.get(ManagedInstanceLifecycleOperationRow, row.remove_id)
                is not None
            ):
                try:
                    receipt = retirement_receipt_in_session(
                        session, UUID(row.remove_id)
                    )
                    if (
                        receipt.confirmation == "confirmed"
                        and receipt.scope == self._scope(row)
                    ):
                        retired = "confirmed"
                except Exception:
                    pass

            def child(identity: str) -> UUID | None:
                return (
                    UUID(identity)
                    if session.get(ManagedInstanceLifecycleOperationRow, identity)
                    is not None
                    else None
                )

            children = [
                child(value)
                for value in (
                    row.stop_id,
                    row.remove_id,
                    row.provision_id,
                    row.start_id,
                )
            ]
            prepared = False
            current_profile = False
            if row.new_container_id is not None:
                provision = session.get(
                    ManagedInstanceLifecycleOperationRow, row.provision_id
                )
                prepared = (
                    provision is not None and provision.state.value == "succeeded"
                )
            if prepared:
                try:
                    inventory = self._v._records(session, self._v.slot(row.generation))
                    records = [
                        r
                        for r in inventory.containers
                        if r.managed_instance_id == row.target_id
                    ]
                    instance = session.get(ManagedInstanceRow, row.target_id)
                    current_profile = (
                        len(records) == 1
                        and records[0].container_id == row.new_container_id
                        and records[0].runtime_revision_id == row.runtime_revision_id
                        and records[0].image_id == row.image_id
                        and instance is not None
                        and instance.runtime_revision_id == row.runtime_revision_id
                    )
                except Exception:
                    current_profile = False
        return ManagedResetResult(
            operation_id=UUID(row.id),
            request_id=UUID(row.request_id),
            restart=row.restart,
            replayed=replayed,
            phase=row.phase,
            code=row.code,
            stop_operation_id=children[0],
            retirement_operation_id=children[1],
            provision_operation_id=children[2],
            start_operation_id=children[3],
            retirement=retired,
            replacement="confirmed" if prepared else "not_confirmed",
            replacement_current=current_profile,
            start="not_requested"
            if not row.restart
            else "accepted"
            if row.phase == "complete"
            else "not_confirmed",
            profile_bound_state="invalidated"
            if row.invalidated_at
            else "not_confirmed",
            recovery=(
                "Review the original typed lifecycle/retirement history. Never "
                "retry removal or create over uncertainty."
                if row.phase == "uncertain"
                else (
                    "The original intent is terminal. Resolve its blocker; a new "
                    "authorized Start/Reset uses a new UUID and cannot repeat this "
                    "retirement."
                )
                if row.phase == "blocked"
                else (
                    "Replacement is unselected for development. Prepare Bridge and "
                    "explicitly select/bind it before deploying; inspect native "
                    "readiness separately."
                )
                if row.phase == "complete"
                else (
                    "Replay this exact Reset UUID to continue or observe its original "
                    "children."
                )
            ),
            status=status,
        )
