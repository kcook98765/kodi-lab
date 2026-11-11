"""Positive, exact maintenance admission; never a development grant."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from kodi_lab.domain import ManagedInstanceLifecycleKind as Kind
from kodi_lab.domain import ManagedInstanceLifecycleOperationState as State
from kodi_lab.domain import ManagedInstanceMutationKind
from kodi_lab.storage.models import (
    ManagedBridgePreparationChildRow,
    ManagedBridgePreparationRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedStartAuthorizationRow,
)


def require_bridge_preparation(
    session: Session,
    preparation_id: UUID,
    *,
    target_id: str,
    revision_id: str,
    container_id: str,
    image_id: str,
) -> None:
    row = session.get(ManagedBridgePreparationRow, str(preparation_id))
    lease = session.get(ManagedInstanceMutationLeaseRow, target_id)
    instance = session.get(ManagedInstanceRow, target_id)
    link = (
        session.get(ManagedBridgePreparationChildRow, lease.operation_id)
        if lease
        else None
    )
    child = (
        session.get(ManagedInstanceLifecycleOperationRow, lease.operation_id)
        if lease
        else None
    )
    auth = (
        session.get(ManagedStartAuthorizationRow, lease.operation_id) if lease else None
    )
    if (
        row is None
        or row.phase != "bridge"
        or row.checkpoint != "dispatched"
        or (row.target_id, row.runtime_revision_id, row.container_id, row.image_id)
        != (target_id, revision_id, container_id, image_id)
        or lease is None
        or link is None
        or link.preparation_id != row.id
        or link.phase != "bridge"
        or link.attempt != row.attempt
        or lease.operation_kind is not ManagedInstanceMutationKind.LIFECYCLE
        or not lease.lease_expires_at
        or datetime.fromisoformat(lease.lease_expires_at) <= datetime.now(UTC)
        or child is None
        or child.kind is not Kind.START
        or child.state is not State.RUNNING
        or child.original_managed_instance_id != target_id
        or child.managed_instance_id != target_id
        or child.reconciliation_required
        or auth is None
        or instance is None
        or instance.runtime_revision_id != revision_id
        or (auth.target_id, auth.runtime_revision_id, auth.container_id, auth.image_id)
        != (target_id, revision_id, container_id, image_id)
        or auth.lease_digest != lease.lease_token_digest
        or auth.runtime_state_revision != instance.runtime_state_revision
        or row.runtime_state_revision != instance.runtime_state_revision
    ):
        raise PermissionError("managed_bridge_maintenance_authority_changed")
