"""Exact Create START checkpoints within the existing authority transactions."""

from uuid import UUID

from sqlalchemy.orm import Session

from kodi_lab.domain import ManagedInstanceLifecycleKind as Kind
from kodi_lab.domain import ManagedInstanceLifecycleOperationState as State
from kodi_lab.domain import ManagedInstanceMutationKind
from kodi_lab.domain.create_kodi import (
    CreateCheckpoint,
    CreateOperation,
    CreateOutcome,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.storage.create_kodi import _now, _snapshot
from kodi_lab.storage.models import (
    CreateKodiOperationRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedStartAuthorizationRow,
)


def require_provision_receipt(session: Session, op: CreateOperation) -> None:
    child = (
        session.get(
            ManagedInstanceLifecycleOperationRow, str(op.provision_operation_id)
        )
        if op.provision_operation_id
        else None
    )
    if (
        child is None
        or child.kind is not Kind.PROVISION
        or child.state is not State.SUCCEEDED
        or child.original_managed_instance_id != str(op.target_id)
        or child.managed_instance_id != str(op.target_id)
        or child.resulting_state != "stopped"
        or child.reconciliation_required
        or op.container_id is None
        or op.runtime_state_revision is None
    ):
        raise CreateTransitionError("confirmed_provision_receipt_required")


def require_start_receipt(session: Session, op: CreateOperation) -> None:
    """Retained success evidence, not permission to dispatch another Start."""
    require_provision_receipt(session, op)
    child = (
        session.get(ManagedInstanceLifecycleOperationRow, str(op.start_operation_id))
        if op.start_operation_id
        else None
    )
    auth = (
        session.get(ManagedStartAuthorizationRow, str(op.start_operation_id))
        if op.start_operation_id
        else None
    )
    if (
        child is None
        or child.kind is not Kind.START
        or child.state is not State.SUCCEEDED
        or child.original_managed_instance_id != str(op.target_id)
        or child.managed_instance_id != str(op.target_id)
        or child.resulting_state not in {"running_usable", "running_not_ready"}
        or child.reconciliation_required
        or child.completed_at is None
        or auth is None
        or auth.consumed_at is None
        or not (auth.created_at <= auth.consumed_at <= child.completed_at)
        or (auth.target_id, auth.runtime_revision_id, auth.image_id, auth.container_id)
        != (
            str(op.target_id),
            str(op.runtime_revision_id),
            op.image_id,
            op.container_id,
        )
        or op.runtime_state_revision != auth.runtime_state_revision + 1
    ):
        raise CreateTransitionError("confirmed_start_receipt_required")


def _parent(
    session: Session, operation_id: UUID, version: int
) -> CreateKodiOperationRow:
    row = session.get(CreateKodiOperationRow, str(operation_id))
    if row is None or row.version != version or row.stage != CreateStage.STARTUP:
        raise CreateTransitionError("stale_start_context")
    return row


def planned_start(
    session: Session, operation_id: UUID, version: int
) -> CreateOperation:
    row = _parent(session, operation_id, version)
    if row.checkpoint != CreateCheckpoint.PLANNED or row.start_operation_id is None:
        raise CreateTransitionError("start_not_planned")
    return _snapshot(row)


def _authority(
    session: Session,
    row: CreateKodiOperationRow,
    child_id: UUID,
) -> ManagedStartAuthorizationRow:
    child = session.get(ManagedInstanceLifecycleOperationRow, str(child_id))
    auth = session.get(ManagedStartAuthorizationRow, str(child_id))
    if (
        row.start_operation_id != str(child_id)
        or child is None
        or auth is None
        or child.kind is not Kind.START
        or child.original_managed_instance_id != row.target_id
        or child.managed_instance_id != row.target_id
        or (auth.target_id, auth.runtime_revision_id, auth.container_id, auth.image_id)
        != (row.target_id, row.runtime_revision_id, row.container_id, row.image_id)
    ):
        raise CreateTransitionError("start_authority_binding_changed")
    return auth


def claim_start(
    session: Session,
    operation_id: UUID,
    *,
    version: int,
    child_id: UUID,
    token_digest: str,
    owned_revision: int,
) -> CreateOperation:
    """After authorization issuance, before reserve, all in one writer claim."""
    row = _parent(session, operation_id, version)
    auth = _authority(session, row, child_id)
    instance = session.get(ManagedInstanceRow, row.target_id)
    child = session.get(ManagedInstanceLifecycleOperationRow, str(child_id))
    if (
        row.checkpoint != CreateCheckpoint.PLANNED
        or row.runtime_state_revision is None
        or instance is None
        or instance.runtime_revision_id != row.runtime_revision_id
        or instance.runtime_state_revision != row.runtime_state_revision
        or owned_revision != row.runtime_state_revision + 1
        or auth.runtime_state_revision != owned_revision
        or auth.lease_digest != token_digest
        or auth.consumed_at is not None
        or child is None
        or child.state is not State.RUNNING
    ):
        raise CreateTransitionError("start_claim_identity_changed")
    row.checkpoint, row.dispatched_at = CreateCheckpoint.DISPATCHED, _now()
    row.version += 1
    row.updated_at = _now()
    session.flush()
    return _snapshot(row)


def capture_start(
    session: Session,
    operation_id: UUID,
    *,
    version: int,
    child_id: UUID,
    token_digest: str,
    owned_revision: int,
) -> CreateOperation:
    row = _parent(session, operation_id, version)
    auth = _authority(session, row, child_id)
    lease = session.get(ManagedInstanceMutationLeaseRow, row.target_id)
    instance = session.get(ManagedInstanceRow, row.target_id)
    child = session.get(ManagedInstanceLifecycleOperationRow, str(child_id))
    if (
        row.checkpoint != CreateCheckpoint.DISPATCHED
        or row.runtime_state_revision is None
        or owned_revision != row.runtime_state_revision + 1
        or auth.runtime_state_revision != owned_revision
        or auth.consumed_at is None
        or auth.lease_digest != token_digest
        or lease is None
        or lease.operation_kind is not ManagedInstanceMutationKind.LIFECYCLE
        or lease.operation_id != str(child_id)
        or lease.lease_token_digest != token_digest
        or lease.lease_expires_at is None
        or lease.lease_expires_at <= _now()
        or instance is None
        or instance.runtime_revision_id != row.runtime_revision_id
        or instance.runtime_state_revision != owned_revision
        or child is None
        or child.state is not State.RUNNING
    ):
        raise CreateTransitionError("start_result_ownership_changed")
    # A captured reply advances prior -> owned; the immutable authorization
    # stores owned too. Consumption alone cannot produce this durable checkpoint.
    row.runtime_state_revision = owned_revision
    row.version += 1
    row.updated_at = _now()
    session.flush()
    return _snapshot(row)


def finish_start(
    session: Session,
    operation_id: UUID,
    *,
    version: int,
    child_id: UUID,
) -> CreateOperation:
    row = _parent(session, operation_id, version)
    auth = _authority(session, row, child_id)
    child = session.get(ManagedInstanceLifecycleOperationRow, str(child_id))
    instance = session.get(ManagedInstanceRow, row.target_id)
    if (
        row.checkpoint != CreateCheckpoint.DISPATCHED
        or child is None
        or child.state is State.RUNNING
    ):
        raise CreateTransitionError("start_terminal_not_verified")
    if child.state is State.SUCCEEDED:
        if (
            auth.consumed_at is None
            or instance is None
            or row.runtime_state_revision != auth.runtime_state_revision
            or instance.runtime_state_revision != auth.runtime_state_revision + 1
            or instance.runtime_revision_id != row.runtime_revision_id
            or child.resulting_state not in {"running_usable", "running_not_ready"}
        ):
            raise CreateTransitionError("start_reply_not_retained")
        row.checkpoint, row.outcome = (
            CreateCheckpoint.CONFIRMED,
            CreateOutcome.SUCCEEDED,
        )
        row.runtime_state_revision = instance.runtime_state_revision
    else:
        row.checkpoint = CreateCheckpoint.NEEDS_ATTENTION
        row.outcome = (
            CreateOutcome.FAILED
            if child.state is State.FAILED
            else CreateOutcome.UNCERTAIN
        )
        row.reason, row.completed_at = "start_" + row.outcome, _now()
    row.version += 1
    row.updated_at = _now()
    session.flush()
    return _snapshot(row)
