"""Enable a newly copied private Program/video project under its deployment lease."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.orm import Session

from kodi_lab.application.addon_dependencies import (
    declaration_mismatch,
    evaluate_dependencies,
    first_blocking_dependency,
)
from kodi_lab.development.launch import manifest_launch_entries, python_module_library
from kodi_lab.domain import (
    GlobalHeavyOperationKind,
    ManagedInstanceMutationKind,
    PythonBytecodePolicy,
)
from kodi_lab.storage.agent_project_context import owned_project_workspace
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import (
    ManagedInstanceRow,
    McpWorkspaceDeploymentOperationRow,
)
from kodi_lab.storage.repositories import (
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
)

if TYPE_CHECKING:
    from kodi_lab.application.mcp_workspace_development import (
        McpWorkspaceDevelopmentService,
    )


def enable_owned_project(
    service: McpWorkspaceDevelopmentService,
    operation: McpWorkspaceDeploymentOperationRow,
    digest: str,
    recheck_authority: Callable[[Session], object] | None,
) -> None:
    """Never retry enablement after an uncertain response or broaden external policy.

    The parent already records copied bytes and mutation_started. Every failure
    follows its existing stopped-tree recovery path; replay cannot dispatch again.
    """
    descriptor = service._workspace_descriptor(UUID(operation.workspace_id))
    entries = [entry for entry in manifest_launch_entries(descriptor) if entry.eligible]
    module = python_module_library(descriptor) is not None
    if (
        operation.target_class != "internal_lab"
        or recheck_authority is None
        or descriptor.has_auto_start_behavior
        or (not module and len(entries) != 1)
        or any(
            entry.point
            not in {
                "xbmc.python.script",
                "xbmc.python.pluginsource",
                "xbmc.python.module",
                "xbmc.addon.metadata",
            }
            for entry in descriptor.extensions
        )
    ):
        raise ValueError("Private passive project enablement unavailable")
    observation, profile, container = service._runtime_context(
        UUID(operation.target_id)
    )
    if (
        profile.python_bytecode_policy is not PythonBytecodePolicy.SUPPRESS
        or container != operation.original_container_id
        or str(profile.runtime_revision_id) != operation.runtime_revision_id
    ):
        raise ValueError("Project runtime changed")
    verified = service._broker.verify_mcp_workspace_destination(
        deployment_operation_id=operation.id,
        managed_instance_id=operation.target_id,
        expected_container_id=container,
    )
    if (
        not verified.matched
        or verified.managed_instance_id != operation.target_id
        or verified.development_source_id != operation.development_source_id
        or verified.runtime_revision_id != operation.runtime_revision_id
        or verified.container_id != container
        or verified.addon_id != operation.addon_id
        or verified.addon_version != operation.addon_version
    ):
        raise ValueError("Project copied bytes changed")
    inventory = service._inventory_factory(observation, profile).get_inventory()
    adapter = service._details_factory(observation, profile)
    details = adapter.get_addon_details(operation.addon_id)
    recognized = next(
        (item for item in inventory.items if item.addon_id == operation.addon_id), None
    )
    expected_type = (
        "xbmc.python.module"
        if module
        else "xbmc.python.script"
        if entries[0].kind == "script"
        else "xbmc.python.pluginsource"
    )
    if (
        inventory.identity.kodi_release != profile.kodi_release
        or recognized is None
        or recognized.version != operation.addon_version
        or recognized.kodi_type != expected_type
        or details.addon_id != operation.addon_id
        or details.version != operation.addon_version
        or details.kodi_type != expected_type
        or declaration_mismatch(descriptor.dependencies, details.dependencies)
        is not None
        or first_blocking_dependency(
            evaluate_dependencies(
                descriptor.dependencies,
                {item.addon_id: item for item in inventory.items},
                kodi_release=profile.kodi_release.canonical,
            )
        )
        is not None
    ):
        raise ValueError("Project identity or dependencies unavailable")
    if details.enabled:
        return
    with service._sessions.begin() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        row = session.get(McpWorkspaceDeploymentOperationRow, operation.id)
        instance = session.get(ManagedInstanceRow, operation.target_id)
        lease = ManagedInstanceMutationLeaseRepository(session).current(
            UUID(operation.target_id)
        )
        heavy = GlobalHeavyOperationLeaseRepository(session).current()
        now = timestamp(service._clock())
        if (
            row is None
            or row.state != "running"
            or row.phase != "inventory"
            or not row.mutation_started
            or not row.sync_completed
            or instance is None
            or instance.runtime_revision_id != operation.runtime_revision_id
            or lease is None
            or lease.operation_id != operation.id
            or lease.operation_kind
            is not ManagedInstanceMutationKind.MCP_WORKSPACE_DEPLOYMENT
            or lease.lease_token_digest != digest
            or lease.lease_expires_at is None
            or lease.lease_expires_at <= now
            or heavy is None
            or heavy.operation_id != operation.id
            or heavy.operation_kind
            is not GlobalHeavyOperationKind.MCP_WORKSPACE_DEPLOYMENT
            or heavy.lease_token_digest != digest
            or heavy.lease_expires_at is None
            or heavy.lease_expires_at <= now
            or not owned_project_workspace(
                session,
                workspace_id=operation.workspace_id,
                source_id=operation.development_source_id,
                client_id=operation.client_id,
            )
        ):
            raise ValueError("Project deployment authority changed")
        recheck_authority(session)
        service._session_admission(
            session,
            target_id=operation.target_id,
            revision_id=operation.runtime_revision_id,
            workspace_id=operation.workspace_id,
            source_id=operation.development_source_id,
            container_id=container,
        )
        # Commit the exact pre-dispatch state before the native call. Any lost
        # response leaves this original parent fenced; it cannot be replayed.
        row.addon_enabled = False
    adapter.enable(operation.addon_id)
    after = adapter.get_addon_details(operation.addon_id)
    if (
        after.addon_id != operation.addon_id
        or after.version != operation.addon_version
        or after.kodi_type != expected_type
        or not after.enabled
        or declaration_mismatch(descriptor.dependencies, after.dependencies) is not None
    ):
        raise ValueError("Project enablement postcondition unavailable")
