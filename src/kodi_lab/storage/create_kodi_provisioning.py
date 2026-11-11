"""Fixed transaction-local Create hooks inside existing lifecycle ownership."""

from uuid import UUID

from sqlalchemy.orm import Session

from kodi_lab.domain import (
    ManagedInstanceLifecycleKind,
    ManagedInstanceLifecycleOperationState,
)
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
)


def _parent(
    session: Session, operation_id: UUID, version: int
) -> CreateKodiOperationRow:
    row = session.get(CreateKodiOperationRow, str(operation_id))
    if row is None or row.version != version or row.stage != CreateStage.PROVISIONING:
        raise CreateTransitionError("stale_provision_context")
    return row


def planned_provision(
    session: Session, operation_id: UUID, version: int
) -> CreateOperation:
    row = _parent(session, operation_id, version)
    if row.checkpoint != CreateCheckpoint.PLANNED or row.provision_operation_id is None:
        raise CreateTransitionError("provision_not_planned")
    return _snapshot(row)


def _owned(
    session: Session,
    row: CreateKodiOperationRow,
    child_id: UUID,
    token_digest: str,
    owned_revision: int,
) -> None:
    child = session.get(ManagedInstanceLifecycleOperationRow, str(child_id))
    lease = session.get(ManagedInstanceMutationLeaseRow, row.target_id)
    instance = session.get(ManagedInstanceRow, row.target_id)
    if (
        row.provision_operation_id != str(child_id)
        or child is None
        or child.kind is not ManagedInstanceLifecycleKind.PROVISION
        or child.original_managed_instance_id != row.target_id
        or child.state is not ManagedInstanceLifecycleOperationState.RUNNING
        or lease is None
        or lease.operation_id != str(child_id)
        or lease.lease_token_digest != token_digest
        or lease.lease_expires_at is None
        or lease.lease_expires_at <= _now()
        or instance is None
        or instance.runtime_revision_id != row.runtime_revision_id
        or instance.runtime_state_revision != owned_revision
    ):
        raise CreateTransitionError("provision_ownership_changed")


def claim_provision(
    session: Session,
    operation_id: UUID,
    *,
    version: int,
    child_id: UUID,
    token_digest: str,
    owned_revision: int,
) -> CreateOperation:
    """Called by on_claim in the SAME child/lease writer transaction."""
    row = _parent(session, operation_id, version)
    if row.checkpoint != CreateCheckpoint.PLANNED or row.container_id is not None:
        raise CreateTransitionError("provision_already_dispatched")
    _owned(session, row, child_id, token_digest, owned_revision)
    row.checkpoint, row.dispatched_at = CreateCheckpoint.DISPATCHED, _now()
    row.version += 1
    row.updated_at = _now()
    session.flush()
    return _snapshot(row)


def capture_provision(
    session: Session,
    operation_id: UUID,
    *,
    version: int,
    child_id: UUID,
    token_digest: str,
    owned_revision: int,
    container_id: str,
) -> CreateOperation:
    """Persist verified identity while the exact child still owns mutation."""
    row = _parent(session, operation_id, version)
    if row.checkpoint != CreateCheckpoint.DISPATCHED or row.container_id is not None:
        raise CreateTransitionError("provision_capture_conflict")
    _owned(session, row, child_id, token_digest, owned_revision)
    row.container_id, row.runtime_state_revision = container_id, owned_revision
    row.version += 1
    row.updated_at = _now()
    session.flush()
    return _snapshot(row)


def finish_provision(
    session: Session,
    operation_id: UUID,
    *,
    version: int,
    child_id: UUID,
) -> CreateOperation:
    """Publish parent and authoritative child terminal result in one transaction."""
    row = _parent(session, operation_id, version)
    child = session.get(ManagedInstanceLifecycleOperationRow, str(child_id))
    instance = session.get(ManagedInstanceRow, row.target_id)
    if (
        row.checkpoint != CreateCheckpoint.DISPATCHED
        or row.provision_operation_id != str(child_id)
        or child is None
        or child.original_managed_instance_id != row.target_id
        or child.kind is not ManagedInstanceLifecycleKind.PROVISION
        or child.state is ManagedInstanceLifecycleOperationState.RUNNING
    ):
        raise CreateTransitionError("provision_terminal_not_verified")
    if child.state is ManagedInstanceLifecycleOperationState.SUCCEEDED:
        if (
            row.container_id is None
            or row.runtime_state_revision is None
            or instance is None
            or instance.runtime_revision_id != row.runtime_revision_id
            or instance.runtime_state_revision != row.runtime_state_revision + 1
            or child.resulting_state != "stopped"
        ):
            raise CreateTransitionError("provision_binding_not_retained")
        row.checkpoint, row.outcome = (
            CreateCheckpoint.CONFIRMED,
            CreateOutcome.SUCCEEDED,
        )
        if not row.start_after_setup and not row.bridge_required:
            row.checkpoint = CreateCheckpoint.COMPLETE
            row.completed_at = _now()
        # Existing lease release advances projection exactly once after owned
        # finalization. Preserve that exact post-release identity for later Start.
        row.runtime_state_revision = instance.runtime_state_revision
    else:
        row.checkpoint = CreateCheckpoint.NEEDS_ATTENTION
        row.outcome = (
            CreateOutcome.FAILED
            if child.state is ManagedInstanceLifecycleOperationState.FAILED
            else CreateOutcome.UNCERTAIN
        )
        row.reason, row.completed_at = "provision_" + row.outcome, _now()
    row.version += 1
    row.updated_at = _now()
    session.flush()
    return _snapshot(row)
