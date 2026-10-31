"""Catalog fence for explicitly admitted human Web viewer runtime identities.

The private broker resolves production pins; isolated qualification may inject
them. Browser requests cannot select a container, image or runtime incarnation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.domain import InstanceType, ManagedInstanceMutationKind
from kodi_lab.runtime.display_capabilities import (
    EXPERIMENTAL_OMEGA_DISPLAY,
    DisplayCapability,
)
from kodi_lab.storage.development_sessions import active_session
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    DevelopmentSessionRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    McpWorkspaceLaunchRow,
)
from kodi_lab.storage.repositories import (
    ManagedInstanceMutationLeaseRepository,
    ManagedRuntimeProjectionRepository,
    RuntimeContainerArtifactRepository,
    RuntimeExecutionProfileRepository,
)


class CatalogViewAuthority:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        pins: Mapping[UUID, ViewBinding],
        *,
        capability: DisplayCapability = EXPERIMENTAL_OMEGA_DISPLAY,
    ) -> None:
        self.sessions = sessions
        self.pins = dict(pins)
        self.capability = capability

    def resolve(
        self,
        instance_id: UUID,
        *,
        mode: str = "view-only",
    ) -> ViewBinding:
        if mode not in {"view-only", "interactive"}:
            raise ValueError("view mode unavailable")
        pin = self.pins[instance_id]
        with self.sessions() as db:
            instance = db.get(ManagedInstanceRow, str(instance_id))
            projection = ManagedRuntimeProjectionRepository(db)
            if (
                instance is None
                or instance.instance_type is not InstanceType.DISPOSABLE
                or projection.has_blocking_runtime_reconciliation(instance_id)
            ):
                raise ValueError("view runtime unavailable")
            require_resolved_view_work(db, instance_id, self.sessions)
            revision = UUID(instance.runtime_revision_id)
            artifact = RuntimeContainerArtifactRepository(db).get(revision)
            profile = RuntimeExecutionProfileRepository(db).get(revision)
            capability = self.capability
            if (
                artifact is None
                or artifact.image_id != pin.image
                or profile is None
                or profile.expected_application_name
                != capability.expected_application_name
                or profile.expected_kodi_version != capability.expected_kodi_version
                or profile.expected_version_tag != capability.expected_version_tag
                or profile.expected_prerelease_ordinal
                != capability.expected_prerelease_ordinal
                or profile.runtime_uid != capability.runtime_uid
                or profile.runtime_gid != capability.runtime_gid
            ):
                raise ValueError("view runtime not admitted")
            current = active_session(db)
            selected: tuple[object, ...] = (None,)
            if current is not None and current.target_id == str(instance_id):
                workspace = db.get(AgentWorkspaceRow, current.workspace_id)
                if (
                    current.phase != "bound"
                    or current.runtime_revision_id != str(revision)
                    or current.container_id != pin.container
                    or workspace is None
                    or workspace.development_source_id != current.source_id
                ):
                    raise ValueError("view session identity changed")
                selected = (
                    current.generation,
                    current.workspace_id,
                    current.source_id,
                    workspace.generation,
                )
            identity = (str(revision), instance.runtime_state_revision, *selected)
            lease = ManagedInstanceMutationLeaseRepository(db).current(instance_id)
            if lease is not None and (
                mode != "view-only"
                or current is None
                or current.target_id != str(instance_id)
                or not self._compatible_operation(db, lease, current, instance, pin)
            ):
                raise ValueError("view operation conflict")
            value = replace(pin, identity=identity)
            if pin.identity and pin.identity != identity:
                raise ValueError("view catalog identity changed")
            return value

    @staticmethod
    def _compatible_operation(
        db: Session,
        lease: ManagedInstanceMutationLeaseRow,
        current: DevelopmentSessionRow,
        instance: ManagedInstanceRow,
        pin: ViewBinding,
    ) -> bool:
        if not CatalogViewAuthority._operation_context(
            db, lease, current, instance, pin
        ):
            return False
        try:
            acquired = datetime.fromisoformat(lease.acquired_at or "")
            heartbeat = datetime.fromisoformat(lease.heartbeat_at or "")
            expires = datetime.fromisoformat(lease.lease_expires_at or "")
            return bool(
                lease.lease_token_digest is not None
                and acquired <= heartbeat <= datetime.now(UTC) < expires
            )
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _operation_context(
        db: Session,
        lease: ManagedInstanceMutationLeaseRow,
        current: DevelopmentSessionRow,
        instance: ManagedInstanceRow,
        pin: ViewBinding,
    ) -> bool:
        operation: McpWorkspaceLaunchRow | McpWorkspaceExecutionOperationRow | None
        if lease.operation_kind is ManagedInstanceMutationKind.MCP_WORKSPACE_LAUNCH:
            operation = db.get(McpWorkspaceLaunchRow, lease.operation_id)
            if operation is None or operation.state not in {"pending", "dispatching"}:
                return False
            if (
                operation.runtime_revision_id != instance.runtime_revision_id
                or operation.runtime_state_revision != instance.runtime_state_revision
                or operation.container_id != pin.container
            ):
                return False
        elif (
            lease.operation_kind is ManagedInstanceMutationKind.MCP_WORKSPACE_EXECUTION
        ):
            operation = db.get(McpWorkspaceExecutionOperationRow, lease.operation_id)
            if (
                operation is None
                or operation.state not in {"pending", "running"}
                or operation.execution_kind != "smoke"
                or (
                    operation.invocation_dispatched
                    and operation.original_container_id is None
                )
                or (
                    operation.original_container_id is not None
                    and (
                        operation.original_container_id != pin.container
                        or operation.original_runtime_state_revision
                        != instance.runtime_state_revision
                    )
                )
            ):
                return False
        else:
            return False
        now = datetime.now(UTC)
        try:
            requested = datetime.fromisoformat(operation.requested_at)
            if not requested <= now < requested + timedelta(minutes=15):
                return False
        except (TypeError, ValueError):
            return False
        workspace = db.get(AgentWorkspaceRow, current.workspace_id)
        deployment = db.get(McpWorkspaceDeploymentOperationRow, operation.deployment_id)
        return bool(
            workspace is not None
            and deployment is not None
            and operation.target_id == current.target_id == deployment.target_id
            and operation.workspace_id
            == current.workspace_id
            == deployment.workspace_id
            and operation.development_source_id
            == current.source_id
            == deployment.development_source_id
            and operation.generation == workspace.generation == deployment.generation
            and deployment.runtime_revision_id == instance.runtime_revision_id
            and deployment.original_container_id == pin.container
            and deployment.state == "succeeded"
        )


def require_resolved_view_work(
    db: Session, instance_id: UUID, sessions: sessionmaker[Session]
) -> None:
    from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService

    # Reuse existing recovery/containment semantics for historical uncertainty.
    if (
        ManagedInstanceReconciliationService(sessions)
        .status(instance_id, read_session=db)
        .required
    ):
        raise ValueError("view operation reconciliation required")
    lease = db.get(ManagedInstanceMutationLeaseRow, str(instance_id))
    for model, kind, states in (
        (
            McpWorkspaceLaunchRow,
            ManagedInstanceMutationKind.MCP_WORKSPACE_LAUNCH,
            ("pending", "dispatching"),
        ),
        (
            McpWorkspaceExecutionOperationRow,
            ManagedInstanceMutationKind.MCP_WORKSPACE_EXECUTION,
            ("pending", "running"),
        ),
    ):
        for operation_id in db.scalars(
            select(model.id).where(
                model.target_id == str(instance_id), model.state.in_(states)
            )
        ):
            if (
                lease is None
                or lease.operation_id != operation_id
                or lease.operation_kind is not kind
            ):
                raise ValueError("view operation ownership missing")
