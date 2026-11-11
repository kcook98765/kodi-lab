"""Durable authorized Bridge maintenance over existing managed lifecycle authority."""

from collections.abc import Callable
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.lifecycle_operations import RunningLifecycleOperation
from kodi_lab.application.managed_resources import ManagedResourceService
from kodi_lab.application.managed_runtime_admission import require_managed_runtime
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.domain import ManagedInstanceLifecycleOperation, TargetType
from kodi_lab.domain import ManagedInstanceLifecycleOperationState as State
from kodi_lab.domain.create_kodi import CreateCheckpoint, CreateOperation
from kodi_lab.storage.create_kodi import _now
from kodi_lab.storage.create_kodi_start import require_start_receipt
from kodi_lab.storage.managed_start import ManagedStartBinding, require_managed_start
from kodi_lab.storage.models import (
    GlobalHeavyOperationLeaseRow,
    ManagedBridgeTrustRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    TargetRow,
)
from kodi_lab.storage.models import (
    ManagedBridgePreparationChildRow as Child,
)
from kodi_lab.storage.models import (
    ManagedBridgePreparationRow as Row,
)


class BridgePreparationError(RuntimeError):
    pass


class ManagedBridgePreparationService:
    """One child per explicit continuation; passive reads never dispatch work."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        lifecycle: ManagedRuntimeLifecycleService,
        resources: ManagedResourceService | None = None,
    ) -> None:
        self.sessions, self.lifecycle = sessions, lifecycle
        self.resources = resources or ManagedResourceService(
            sessions, lifecycle._broker, lifecycle
        )

    def _idle(self, session: Session, target_id: str) -> None:
        if (
            session.get(ManagedInstanceMutationLeaseRow, target_id) is not None
            or session.get(GlobalHeavyOperationLeaseRow, 1) is not None
            or ManagedInstanceReconciliationService(self.sessions)
            .status(UUID(target_id), read_session=session)
            .required
        ):
            raise BridgePreparationError(
                "managed_bridge_original_child_requires_review"
            )

    def get(self, identity: UUID) -> Row | None:
        with self.sessions() as session:
            row = session.get(Row, str(identity))
            if row:
                session.expunge(row)
            return row

    def for_create(self, identity: UUID) -> Row | None:
        with self.sessions() as session:
            row = session.scalar(
                select(Row).where(Row.create_operation_id == str(identity))
            )
            if row:
                session.expunge(row)
            return row

    def stored_trust(self, row: Row) -> str:
        """Stored lifecycle evidence only; never claim a stopped resident is live."""
        with self.sessions() as session:
            trust = session.get(ManagedBridgeTrustRow, row.target_id)
            instance = session.get(ManagedInstanceRow, row.target_id)
            if trust is None:
                return "uninstalled"
            if (
                instance is None
                or instance.runtime_revision_id != row.runtime_revision_id
                or (trust.container_id, trust.runtime_revision_id)
                != (row.container_id, row.runtime_revision_id)
            ):
                return "identity_changed"
            return trust.state

    def recovery_action(self, row: Row) -> str:
        """Read-only guidance; every eventual POST repeats current admission."""
        if row.checkpoint != "needs_attention":
            return "continue"
        try:
            with self.sessions() as session:
                self._idle(session, row.target_id)
                prior = session.get(
                    ManagedInstanceLifecycleOperationRow, str(self.child_id(row))
                )
                if prior and (
                    prior.state not in {State.SUCCEEDED, State.FAILED}
                    or prior.reconciliation_required
                ):
                    return "review"
                target = session.get(TargetRow, row.target_id)
                instance = session.get(ManagedInstanceRow, row.target_id)
                if (
                    target is None
                    or target.target_type is not TargetType.MANAGED
                    or instance is None
                ):
                    return "review"
                require_managed_runtime(
                    session,
                    UUID(row.target_id),
                    UUID(instance.runtime_revision_id),
                    data_root=self.lifecycle._data_root,
                )
                binding = require_managed_start(
                    session,
                    self.lifecycle._broker.managed_container_inventory().containers,
                    target_id=row.target_id,
                    revision_id=instance.runtime_revision_id,
                )
                if (binding.revision_id, binding.container_id, binding.image_id) != (
                    row.runtime_revision_id,
                    row.container_id,
                    row.image_id,
                ):
                    return "prepare_current"
                return "resume_original"
        except Exception:
            return "review"

    def _binding(
        self, session: Session, row: Row, *, epoch: bool = True
    ) -> ManagedStartBinding:
        instance = session.get(ManagedInstanceRow, row.target_id)
        target = session.get(TargetRow, row.target_id)
        if epoch:
            self._idle(session, row.target_id)
        records = self.lifecycle._broker.managed_container_inventory().containers
        own = [r for r in records if r.managed_instance_id == row.target_id]
        if (
            target is None
            or target.target_type is not TargetType.MANAGED
            or instance is None
            or instance.runtime_revision_id != row.runtime_revision_id
            or (epoch and instance.runtime_state_revision != row.runtime_state_revision)
            or len(own) != 1
            or (own[0].container_id, own[0].runtime_revision_id, own[0].image_id)
            != (row.container_id, row.runtime_revision_id, row.image_id)
            or own[0].runtime_state not in {"stopped", "running"}
        ):
            raise BridgePreparationError("managed_bridge_preparation_identity_changed")
        return ManagedStartBinding(
            row.target_id,
            row.runtime_revision_id,
            row.image_id,
            row.container_id,
            own[0].runtime_state.value,
        )

    def accept(
        self,
        *,
        request_id: UUID,
        target_id: UUID,
        keep_running: bool = False,
        create: CreateOperation | None = None,
        authorize: Callable[[Session, UUID], None] | None = None,
        expected: ManagedStartBinding | None = None,
    ) -> Row:
        with self.sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if authorize is not None:
                authorize(session, target_id)
            old = session.scalar(select(Row).where(Row.request_id == str(request_id)))
            if old:
                if (old.target_id, old.keep_running, old.create_operation_id) != (
                    str(target_id),
                    keep_running,
                    str(create.id) if create else None,
                ):
                    raise BridgePreparationError("managed_bridge_request_conflict")
                if expected is not None and (
                    old.target_id,
                    old.runtime_revision_id,
                    old.container_id,
                    old.image_id,
                ) != (
                    expected.target_id,
                    expected.revision_id,
                    expected.container_id,
                    expected.image_id,
                ):
                    raise BridgePreparationError(
                        "managed_bridge_preparation_identity_changed"
                    )
                session.expunge(old)
                return old
            if session.scalar(
                select(Row.id).where(
                    Row.target_id == str(target_id),
                    Row.checkpoint.not_in(("complete", "needs_attention")),
                )
            ):
                raise BridgePreparationError("managed_bridge_preparation_pending")
            if (
                session.scalar(select(func.count()).select_from(Row)) or 0
            ) >= 32768 or (
                session.scalar(
                    select(func.count())
                    .select_from(Row)
                    .where(Row.target_id == str(target_id))
                )
                or 0
            ) >= 1024:
                raise BridgePreparationError("managed_bridge_preparation_quota")
            instance = session.get(ManagedInstanceRow, str(target_id))
            target = session.get(TargetRow, str(target_id))
            if (
                instance is None
                or target is None
                or target.target_type is not TargetType.MANAGED
            ):
                raise BridgePreparationError("managed_bridge_target_unavailable")
            self._idle(session, str(target_id))
            if create is not None:
                if (
                    not create.bridge_required
                    or create.checkpoint is not CreateCheckpoint.COMPLETE
                    or create.target_id != target_id
                ):
                    raise BridgePreparationError(
                        "managed_bridge_native_receipt_required"
                    )
                require_start_receipt(session, create)
                if instance.runtime_state_revision != create.runtime_state_revision:
                    raise BridgePreparationError(
                        "managed_bridge_native_identity_changed"
                    )
            require_managed_runtime(
                session,
                target_id,
                UUID(instance.runtime_revision_id),
                data_root=self.lifecycle._data_root,
            )
            binding = require_managed_start(
                session,
                self.lifecycle._broker.managed_container_inventory().containers,
                target_id=str(target_id),
                revision_id=instance.runtime_revision_id,
            )
            if expected is not None and binding != expected:
                raise BridgePreparationError(
                    "managed_bridge_preparation_identity_changed"
                )
            if create is not None and (
                binding.revision_id,
                binding.container_id,
                binding.image_id,
                binding.state,
            ) != (
                str(create.runtime_revision_id),
                create.container_id,
                create.image_id,
                "running",
            ):
                raise BridgePreparationError("managed_bridge_native_identity_changed")
            identity = (
                uuid5(create.id, "managed-bridge-preparation:v1") if create else uuid4()
            )
            row = Row(
                id=str(identity),
                request_id=str(request_id),
                create_operation_id=str(create.id) if create else None,
                target_id=str(target_id),
                runtime_revision_id=binding.revision_id,
                container_id=binding.container_id,
                image_id=binding.image_id,
                runtime_state_revision=instance.runtime_state_revision,
                keep_running=keep_running,
                phase="start" if binding.state == "stopped" else "bridge",
                checkpoint="pending",
                version=0,
                attempt=0,
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(row)
            session.flush()
            session.expunge(row)
            return row

    @staticmethod
    def child_id(row: Row) -> UUID:
        return uuid5(UUID(row.id), f"{row.phase}:{row.attempt}")

    def continue_once(
        self,
        identity: UUID,
        *,
        version: int,
        authorize: Callable[[Session, UUID], None] | None = None,
        actor: str = "web",
    ) -> Row:
        op = self.get(identity)
        if op is None or op.version != version:
            raise BridgePreparationError("managed_bridge_stale_preparation")
        if authorize is not None:
            with self.sessions() as session:
                authorize(session, UUID(op.target_id))
        if op.checkpoint in {"complete", "needs_attention"}:
            return op
        if op.checkpoint == "dispatched":
            # Inspect original child evidence only. Never redispatch, including
            # after a process crash or a lost response to its terminal commit.
            with self.sessions() as session:
                child = session.get(
                    ManagedInstanceLifecycleOperationRow, str(self.child_id(op))
                )
                lease = session.get(ManagedInstanceMutationLeaseRow, op.target_id)
                live = (
                    child is not None
                    and child.state is State.RUNNING
                    and lease is not None
                    and lease.operation_id == child.id
                    and lease.lease_expires_at
                    and lease.lease_expires_at > _now()
                )
            if not live:
                self._attention(op, "managed_bridge_original_child_requires_review")
            return self.get(identity) or op
        child_id = self.child_id(op)

        def pending(session: Session) -> None:
            if authorize is not None:
                authorize(session, UUID(op.target_id))
            row = session.get(Row, op.id)
            if row is None or row.version != op.version or row.checkpoint != "pending":
                raise BridgePreparationError("managed_bridge_stale_preparation")
            self._binding(session, row)
            self.resources._guard(
                session, UUID(row.target_id), reason="released", actor=actor
            )

        def dispatch_guard() -> None:
            with self.sessions.begin() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                if authorize is not None:
                    authorize(session, UUID(op.target_id))
                row = session.get(Row, op.id)
                if (
                    row is None
                    or row.checkpoint != "dispatched"
                    or self.child_id(row) != child_id
                ):
                    raise BridgePreparationError("managed_bridge_claim_changed")
                self._binding(session, row, epoch=False)
                self.resources._guard(
                    session,
                    UUID(op.target_id),
                    reason="released",
                    actor=actor,
                    own_operation_id=child_id,
                )

        def before_stop() -> None:
            dispatch_guard()
            self.resources._playback_evidence(
                UUID(op.target_id), expected_container_id=op.container_id
            )
            dispatch_guard()

        def claim(session: Session, handle: RunningLifecycleOperation) -> None:
            row = session.get(Row, op.id)
            if (
                row is None
                or row.version != op.version
                or row.checkpoint != "pending"
                or handle.operation_id != child_id
                or handle.runtime_state_revision != row.runtime_state_revision + 1
            ):
                raise BridgePreparationError("managed_bridge_claim_changed")
            row.checkpoint, row.runtime_state_revision = (
                "dispatched",
                handle.runtime_state_revision,
            )
            row.version += 1
            row.updated_at = _now()
            session.add(
                Child(
                    operation_id=str(child_id),
                    preparation_id=row.id,
                    phase=row.phase,
                    attempt=row.attempt,
                )
            )

        def terminal(
            session: Session, child: ManagedInstanceLifecycleOperation
        ) -> None:
            row = session.get(Row, op.id)
            instance = session.get(ManagedInstanceRow, op.target_id)
            if row is None or row.checkpoint != "dispatched" or child.id != child_id:
                raise BridgePreparationError("managed_bridge_terminal_changed")
            if child.state is State.SUCCEEDED:
                binding = self._binding(session, row, epoch=False)
                if (
                    instance is None
                    or instance.runtime_state_revision != row.runtime_state_revision + 1
                    or binding.state != ("stopped" if op.phase == "stop" else "running")
                ):
                    raise BridgePreparationError(
                        "managed_bridge_terminal_identity_changed"
                    )
                if op.phase == "bridge":
                    trust = session.get(ManagedBridgeTrustRow, row.target_id)
                    if (
                        trust is None
                        or trust.state != "active"
                        or (trust.container_id, trust.runtime_revision_id)
                        != (row.container_id, row.runtime_revision_id)
                    ):
                        raise BridgePreparationError("managed_bridge_trust_unconfirmed")
                row.runtime_state_revision = instance.runtime_state_revision
                row.phase = (
                    "bridge"
                    if op.phase == "start"
                    else "stop"
                    if op.phase == "bridge" and not row.keep_running
                    else "complete"
                )
                row.checkpoint = "complete" if row.phase == "complete" else "pending"
            else:
                row.checkpoint, row.reason = (
                    "needs_attention",
                    "managed_bridge_child_requires_review",
                )
            row.version += 1
            row.updated_at = _now()

        try:
            expected = ManagedStartBinding(
                op.target_id,
                op.runtime_revision_id,
                op.image_id,
                op.container_id,
                "stopped" if op.phase == "start" else "running",
            )
            if op.phase == "start":
                self.lifecycle.start_managed(
                    UUID(op.target_id),
                    operation_id=child_id,
                    expected=expected,
                    precondition=pending,
                    on_claim=claim,
                    on_terminal=terminal,
                    before_dispatch=dispatch_guard,
                )
            elif op.phase == "bridge":
                self.lifecycle.prepare_managed_bridge(
                    UUID(op.target_id),
                    preparation_id=UUID(op.id),
                    operation_id=child_id,
                    expected=expected,
                    precondition=pending,
                    on_claim=claim,
                    on_terminal=terminal,
                    before_restart=before_stop,
                )
            else:
                self.lifecycle.stop(
                    UUID(op.target_id),
                    operation_id=child_id,
                    expected_container_id=op.container_id,
                    precondition=pending,
                    on_claim=claim,
                    on_terminal=terminal,
                    before_dispatch=before_stop,
                )
        except Exception:
            current = self.get(identity)
            # A losing concurrent caller must not disturb the winning claim.
            if (
                current
                and current.version == op.version
                and current.checkpoint == "pending"
            ):
                self._attention(op, "managed_bridge_preparation_requires_review")
        return self.get(identity) or op

    def _attention(self, op: Row, reason: str) -> None:
        with self.sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            current = session.get(Row, op.id)
            if current and current.version == op.version:
                current.checkpoint, current.reason = "needs_attention", reason
                current.version += 1
                current.updated_at = _now()

    def resume(
        self,
        identity: UUID,
        *,
        version: int,
        authorize: Callable[[Session, UUID], None] | None = None,
    ) -> Row:
        """Explicit recovery keeps the parent, all attempts and exact profile.

        A new child is permitted only after existing typed recovery has proven
        the previous attempt terminal. This is never called by replay or GET.
        """
        with self.sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            row = session.get(Row, str(identity))
            if (
                row is None
                or row.version != version
                or row.checkpoint != "needs_attention"
            ):
                raise BridgePreparationError("managed_bridge_stale_recovery")
            if authorize is not None:
                authorize(session, UUID(row.target_id))
            if row.attempt >= 32:
                raise BridgePreparationError("managed_bridge_preparation_quota")
            self._binding(session, row, epoch=False)
            self._idle(session, row.target_id)
            prior = session.get(
                ManagedInstanceLifecycleOperationRow, str(self.child_id(row))
            )
            if (
                prior
                and (
                    prior.state not in {State.SUCCEEDED, State.FAILED}
                    or prior.reconciliation_required
                )
            ) or session.get(ManagedInstanceMutationLeaseRow, row.target_id):
                raise BridgePreparationError(
                    "managed_bridge_original_child_requires_review"
                )
            binding = require_managed_start(
                session,
                self.lifecycle._broker.managed_container_inventory().containers,
                target_id=row.target_id,
                revision_id=row.runtime_revision_id,
            )
            instance = session.get(ManagedInstanceRow, row.target_id)
            assert instance is not None
            # A reviewed recovery can re-observe the same exact profile epoch;
            # it cannot substitute a new runtime, image or container.
            row.runtime_state_revision = instance.runtime_state_revision
            row.phase = (
                "stop"
                if row.phase == "stop" and binding.state == "running"
                else "start"
                if binding.state == "stopped"
                else "bridge"
            )
            row.attempt += 1
            row.checkpoint, row.reason = "pending", None
            row.version += 1
            row.updated_at = _now()
            session.flush()
            session.expunge(row)
            return row
