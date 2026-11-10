"""Meaningful managed-runtime use, separate from passive observations and retention."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from kodi_lab.domain.lifecycle_operations import ManagedInstanceLifecycleKind
from kodi_lab.domain.snapshots import ManagedInstanceMutationKind
from kodi_lab.storage.managed_lifecycle_owner import lifecycle_actor
from kodi_lab.storage.models import (
    ManagedAgentLifecycleRequestRow,
    ManagedCleanResetRow,
    ManagedIdleStateRow,
    ManagedInstanceLifecycleOperationRow,
    ManagedInstanceRow,
    ManagedInstanceUseRow,
    ManagedResourcePolicyRow,
    ManagedVersionOperationRow,
    McpAddonSettingsOperationRow,
    McpWorkspaceDeploymentOperationRow,
    McpWorkspaceExecutionOperationRow,
    McpWorkspaceLaunchRow,
)


def timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("managed activity timestamps must be timezone aware")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class ManagedResourcePolicyValue:
    max_running: int = 1
    idle_minutes: int | None = 60


def read_resource_policy(session: Session) -> ManagedResourcePolicyValue:
    """Read recorded policy or safe defaults without creating persistent state."""
    row = session.get(ManagedResourcePolicyRow, 1)
    return (
        ManagedResourcePolicyValue()
        if row is None
        # Issue #238 fixes the effective running limit at one. Preserve older
        # recorded values for audit; reading policy must not rewrite user state.
        else ManagedResourcePolicyValue(1, row.idle_minutes)
    )


def resource_policy(session: Session) -> ManagedResourcePolicyRow:
    defaults = ManagedResourcePolicyValue()
    session.execute(
        insert(ManagedResourcePolicyRow)
        .values(
            id=1,
            max_running=defaults.max_running,
            idle_minutes=defaults.idle_minutes,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    result = session.get(ManagedResourcePolicyRow, 1)
    assert result is not None
    return result


def enroll_new_instance(session: Session, instance_id: UUID, *, now: datetime) -> None:
    session.add(
        ManagedIdleStateRow(
            managed_instance_id=str(instance_id),
            enrolled=True,
            last_activity_at=timestamp(now),
        )
    )


def end_running_holds(session: Session, instance_id: UUID) -> None:
    """End holds for a proven stopped run; preserve activity and saved receipts.

    Call only under lifecycle authority after a confirmed Stop, or when claiming
    Start for an exact stopped profile. A pending/uncertain Stop is insufficient.
    A new explicitly requested hold is recorded after this transition.
    """
    for use in session.scalars(
        select(ManagedInstanceUseRow).where(
            ManagedInstanceUseRow.managed_instance_id == str(instance_id)
        )
    ):
        use.hold_until = None


def record_activity(
    session: Session,
    instance_id: UUID,
    *,
    actor: str,
    now: datetime,
) -> None:
    if actor != "web" and (
        not actor.startswith("mcp:") or str(UUID(actor[4:])) != actor[4:]
    ):
        raise ValueError("managed activity actor must be server derived")
    current = timestamp(now)
    state = session.get(ManagedIdleStateRow, str(instance_id))
    if state is None:
        state = ManagedIdleStateRow(
            managed_instance_id=str(instance_id),
            enrolled=False,
        )
        session.add(state)
    if state.last_activity_at is None or current >= state.last_activity_at:
        state.last_activity_at = current
        state.last_stop_attempt_at = None
    row = session.get(ManagedInstanceUseRow, (str(instance_id), actor))
    if row is None:
        row = ManagedInstanceUseRow(
            managed_instance_id=str(instance_id),
            actor_key=actor,
            last_activity_at=current,
        )
        session.add(row)
    elif current >= row.last_activity_at:
        row.last_activity_at = current
        row.released_at = None


def record_operation_activity(
    session: Session,
    instance_id: UUID,
    *,
    kind: ManagedInstanceMutationKind,
    operation_id: UUID,
    now: datetime,
) -> None:
    """Attribute completed typed MCP work to its owner; Web work stays distinct."""
    actor = "web"
    if (
        kind is ManagedInstanceMutationKind.INSTANCE_DELETE
        or session.get(ManagedInstanceRow, str(instance_id)) is None
    ):
        return
    if kind is ManagedInstanceMutationKind.LIFECYCLE:
        lifecycle = session.get(ManagedInstanceLifecycleOperationRow, str(operation_id))
        if (
            lifecycle is not None
            and lifecycle.kind is ManagedInstanceLifecycleKind.STOP
            and lifecycle.state.value == "succeeded"
            and lifecycle.resulting_state == "stopped"
        ):
            end_running_holds(session, instance_id)
            return
        idle = session.get(ManagedIdleStateRow, str(instance_id))
        if idle is not None and idle.last_stop_operation_id == str(operation_id):
            return
        request = session.scalar(
            select(ManagedAgentLifecycleRequestRow).where(
                ManagedAgentLifecycleRequestRow.lifecycle_operation_id
                == str(operation_id)
            )
        )
        if request is not None:
            if request.action == "release" or request.client_id is None:
                return
            actor = f"mcp:{request.client_id}"
        else:
            version = session.scalar(
                select(ManagedVersionOperationRow).where(
                    ManagedVersionOperationRow.lifecycle_operation_id
                    == str(operation_id)
                )
            )
            if version is not None:
                if version.action != "start":
                    return
                actor = lifecycle_actor(version.client_id)
            reset = session.scalar(
                select(ManagedCleanResetRow).where(
                    ManagedCleanResetRow.start_id == str(operation_id)
                )
            )
            if reset is not None:
                actor = lifecycle_actor(reset.client_id)
            lifecycle = session.get(
                ManagedInstanceLifecycleOperationRow, str(operation_id)
            )
            if (
                lifecycle is None
                or lifecycle.kind is not ManagedInstanceLifecycleKind.START
            ):
                return
        lifecycle = session.get(ManagedInstanceLifecycleOperationRow, str(operation_id))
        if lifecycle is not None and lifecycle.completed_at is not None:
            now = datetime.fromisoformat(lifecycle.completed_at.replace("Z", "+00:00"))
    if kind is ManagedInstanceMutationKind.MCP_WORKSPACE_DEPLOYMENT:
        deployment = session.get(McpWorkspaceDeploymentOperationRow, str(operation_id))
        if deployment is None or not deployment.mutation_started:
            return
        if deployment is not None and deployment.client_id is not None:
            actor = f"mcp:{deployment.client_id}"
        if deployment.completed_at is not None:
            now = datetime.fromisoformat(deployment.completed_at.replace("Z", "+00:00"))
    elif kind is ManagedInstanceMutationKind.MCP_ADDON_SETTINGS:
        settings = session.get(McpAddonSettingsOperationRow, str(operation_id))
        if settings is None or settings.state in {"pending", "failed"}:
            return
        actor = f"mcp:{settings.client_id}"
        if settings.completed_at is not None:
            now = datetime.fromisoformat(settings.completed_at.replace("Z", "+00:00"))
    elif kind is ManagedInstanceMutationKind.MCP_WORKSPACE_LAUNCH:
        launch = session.get(McpWorkspaceLaunchRow, str(operation_id))
        if launch is None or launch.started_at is None:
            return
        actor = f"mcp:{launch.client_id}"
        if launch.completed_at is not None:
            now = datetime.fromisoformat(launch.completed_at.replace("Z", "+00:00"))
    elif kind is ManagedInstanceMutationKind.MCP_WORKSPACE_EXECUTION:
        execution = session.get(McpWorkspaceExecutionOperationRow, str(operation_id))
        if execution is None or not execution.invocation_dispatched:
            return
        if execution is not None:
            actor = f"mcp:{execution.client_id}"
        if execution.completed_at is not None:
            now = datetime.fromisoformat(execution.completed_at.replace("Z", "+00:00"))
    record_activity(session, instance_id, actor=actor, now=now)
