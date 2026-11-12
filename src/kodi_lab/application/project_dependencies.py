"""Exact separately deployed owned Python modules; no recursive installer."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from kodi_lab.application.addon_dependencies import (
    declaration_mismatch,
    evaluate_dependencies,
    first_blocking_dependency,
)
from kodi_lab.development.launch import manifest_launch_entries, python_module_library
from kodi_lab.storage.agent_project_context import owned_project_workspace
from kodi_lab.storage.models import (
    AgentWorkspaceRow,
    McpWorkspaceDeploymentOperationRow,
)
from kodi_lab.storage.repositories import DevelopmentSourceRepository

if TYPE_CHECKING:
    from kodi_lab.application.mcp_workspace_development import (
        McpWorkspaceDevelopmentService,
    )


def verify_owned_dependencies(
    service: McpWorkspaceDevelopmentService,
    *,
    workspace_id: str,
    target_id: str,
    client_id: str | None,
    container_id: str | None = None,
) -> None:
    """Run under the caller's existing target mutation lease before native effects.

    Every module is a separate owned project with its own validated generation
    and verified deployment. A module cannot declare another module, so cycles
    and recursive ordering are refused rather than partially installed.
    """
    from kodi_lab.application.mcp_workspace_development import (
        McpWorkspaceDevelopmentError,
    )

    def refuse(code: str, dependency: str | None = None) -> None:
        raise McpWorkspaceDevelopmentError(code, reason=dependency)

    with service._sessions() as session:
        workspace = session.get(AgentWorkspaceRow, workspace_id)
        if workspace is None or not owned_project_workspace(
            session,
            workspace_id=workspace_id,
            source_id=workspace.development_source_id,
            client_id=client_id,
        ):
            return
        source = DevelopmentSourceRepository(session).get(
            UUID(workspace.development_source_id)
        )
    descriptor = service._workspace_descriptor(UUID(workspace_id))
    dependencies = [
        item for item in descriptor.dependencies if item.dependency_id != "xbmc.python"
    ]
    module = "xbmc.python.module" in descriptor.extension_types or (
        source is not None and "xbmc.python.module" in source.extension_types
    )
    if not module and not descriptor.dependencies:
        return
    if module and python_module_library(descriptor) is None:
        refuse("project_module_descriptor_unsupported")
    if not module and (
        descriptor.has_auto_start_behavior
        or len([item for item in manifest_launch_entries(descriptor) if item.eligible])
        != 1
    ):
        refuse("project_dependency_consumer_unsupported")
    if len(dependencies) > 3 or any(
        not item.dependency_id.startswith("script.module.")
        or item.dependency_id == descriptor.addon_id
        or item.optional
        or item.minversion is not None
        or item.version is None
        for item in dependencies
    ):
        refuse("project_dependency_unsupported")
    observed, profile, container = service._runtime_context(UUID(target_id))
    if container_id is not None and container != container_id:
        refuse("project_dependency_runtime_changed")
    inventory = service._inventory_factory(observed, profile).get_inventory()
    if inventory.identity.kodi_release != profile.kodi_release:
        refuse("project_dependency_runtime_changed")
    blocked = first_blocking_dependency(
        evaluate_dependencies(
            descriptor.dependencies,
            {item.addon_id: item for item in inventory.items},
            kodi_release=profile.kodi_release.canonical,
        )
    )
    if blocked is not None:
        refuse("project_dependency_" + blocked.status.value, blocked.dependency_id)
    for dependency in dependencies:
        with service._sessions() as session:
            # Any newer uncertain/failed receipt blocks reuse of an older success.
            deployment = session.scalar(
                select(McpWorkspaceDeploymentOperationRow)
                .where(
                    McpWorkspaceDeploymentOperationRow.target_id == target_id,
                    McpWorkspaceDeploymentOperationRow.addon_id
                    == dependency.dependency_id,
                )
                .order_by(
                    McpWorkspaceDeploymentOperationRow.requested_at.desc(),
                    McpWorkspaceDeploymentOperationRow.id.desc(),
                )
                .limit(1)
            )
            module_workspace = (
                None
                if deployment is None
                else session.get(AgentWorkspaceRow, deployment.workspace_id)
            )
            if (
                deployment is None
                or module_workspace is None
                or deployment.state != "succeeded"
                or deployment.client_id != client_id
                or deployment.deployment_mode != "direct"
                or deployment.runtime_revision_id != str(profile.runtime_revision_id)
                or deployment.original_container_id != container
                or deployment.addon_enabled is not True
                or deployment.source_manifest_digest is None
                or deployment.verified_destination_digest is None
                or deployment.generation != module_workspace.generation
                or deployment.source_manifest_digest
                != module_workspace.current_tree_digest
                or not owned_project_workspace(
                    session,
                    workspace_id=deployment.workspace_id,
                    source_id=deployment.development_source_id,
                    client_id=client_id,
                )
            ):
                refuse("project_dependency_not_deployed", dependency.dependency_id)
            assert deployment is not None
            session.expunge(deployment)
        module_descriptor = service._workspace_descriptor(UUID(deployment.workspace_id))
        if python_module_library(module_descriptor) is None:
            refuse("project_module_descriptor_unsupported", dependency.dependency_id)
        details = service._details_factory(observed, profile).get_addon_details(
            dependency.dependency_id
        )
        if (
            details.kodi_type != "xbmc.python.module"
            or not details.enabled
            or details.version != deployment.addon_version
            or declaration_mismatch(
                module_descriptor.dependencies, details.dependencies
            )
            is not None
        ):
            refuse("project_dependency_changed", dependency.dependency_id)
        verified = service._broker.verify_mcp_workspace_destination(
            deployment_operation_id=deployment.id,
            managed_instance_id=target_id,
            expected_container_id=container,
        )
        if (
            not verified.matched
            or verified.managed_instance_id != target_id
            or verified.container_id != container
            or verified.runtime_revision_id != str(profile.runtime_revision_id)
            or verified.addon_id != dependency.dependency_id
            or verified.addon_version != deployment.addon_version
            or verified.development_source_id != deployment.development_source_id
        ):
            refuse("project_dependency_changed", dependency.dependency_id)
