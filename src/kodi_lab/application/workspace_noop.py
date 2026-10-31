"""Verify an existing managed video-plugin deployment under normal admission."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from kodi_lab.application.workspace_deployment_selection import (
    latest_verified_workspace_deployment,
)
from kodi_lab.development.deployment import expected_destination_digest
from kodi_lab.development.measurements import DeploymentMeasurement
from kodi_lab.development.noop import ManagedDeploymentVerification
from kodi_lab.development.sources import copied_video_plugin_unsupported_reason
from kodi_lab.domain import (
    GlobalHeavyOperationKind,
    ManagedInstanceMutationKind,
    PythonBytecodePolicy,
)
from kodi_lab.storage.managed_activity import timestamp
from kodi_lab.storage.models import (
    ManagedBridgeTrustRow,
    ManagedInstanceRow,
    McpWorkspaceDeploymentOperationRow,
)
from kodi_lab.storage.repositories import (
    GlobalHeavyOperationLeaseRepository,
    ManagedInstanceMutationLeaseRepository,
)
from kodi_lab.workspace import capture_workspace_deployment_tree

if TYPE_CHECKING:
    from kodi_lab.application.mcp_workspace_development import (
        McpWorkspaceDevelopmentService,
        WorkspaceDeploymentResult,
    )


def _context(
    service: McpWorkspaceDevelopmentService,
    operation: McpWorkspaceDeploymentOperationRow,
    measurement: DeploymentMeasurement,
    *,
    destination_only_count: int | None = None,
) -> ManagedDeploymentVerification:
    verify_bridge = service._managed_noop_bridge
    if verify_bridge is None:
        raise ValueError("No authenticated managed Bridge verifier")
    observation, profile, container = service._runtime_context(
        UUID(operation.target_id)
    )
    if (
        profile.python_bytecode_policy is not PythonBytecodePolicy.SUPPRESS
        or str(profile.runtime_revision_id) != operation.runtime_revision_id
        or container != operation.original_container_id
    ):
        raise ValueError("Managed runtime changed")
    readiness = measurement.run(
        "noop_readiness",
        lambda: service._readiness_factory(service._broker, profile).wait_until_usable(
            operation.target_id
        ),
    )
    if getattr(readiness.usability_state, "value", "") != "usable":
        raise ValueError("Native readiness unavailable")
    binding = measurement.run(
        "noop_bridge_identity",
        lambda: verify_bridge(observation, profile.kodi_release.canonical),
    )
    inventory = measurement.run(
        "noop_inventory",
        lambda: service._inventory_factory(observation, profile).get_inventory(),
    )
    item = next(
        (item for item in inventory.items if item.addon_id == operation.addon_id), None
    )
    if (
        inventory.identity.kodi_release != profile.kodi_release
        or item is None
        or not item.enabled
        or item.version != operation.addon_version
        or item.kodi_type != "xbmc.python.pluginsource"
    ):
        raise ValueError("Native add-on identity or enabled state unavailable")
    with service._sessions() as session:
        instance = session.get(ManagedInstanceRow, operation.target_id)
        trust = session.get(ManagedBridgeTrustRow, operation.target_id)
        if (
            instance is None
            or trust is None
            or trust.state != "active"
            or instance.runtime_revision_id != operation.runtime_revision_id
            or trust.container_id != container
            or trust.runtime_revision_id != operation.runtime_revision_id
            or trust.generation != binding.generation
            or not trust.identity_json
        ):
            raise ValueError("Managed trust changed")
        service._session_admission(
            session,
            target_id=operation.target_id,
            revision_id=operation.runtime_revision_id,
            workspace_id=operation.workspace_id,
            source_id=operation.development_source_id,
            container_id=container,
        )
        return ManagedDeploymentVerification(
            container_id=container,
            runtime_revision_id=UUID(instance.runtime_revision_id),
            runtime_state_revision=instance.runtime_state_revision,
            observed_runtime_state_revision=instance.runtime_state_revision,
            bridge_generation=trust.generation,
            bridge_identity_digest="sha256:"
            + hashlib.sha256(trust.identity_json.encode()).hexdigest(),
            source_manifest_digest=operation.source_manifest_digest or "",
            destination_manifest_digest=operation.verified_destination_digest or "",
            destination_only_count=destination_only_count,
            native_restart_count=1,
        )


def record_managed_verification(
    service: McpWorkspaceDevelopmentService,
    operation: McpWorkspaceDeploymentOperationRow,
    measurement: DeploymentMeasurement,
    *,
    destination_only_count: int | None = None,
) -> None:
    """Optional future reuse proof; absence never changes ordinary deployment."""
    if service._managed_noop_bridge is None:
        return
    try:
        proof = _context(
            service,
            operation,
            measurement,
            destination_only_count=destination_only_count,
        )
        service._update_deployment(
            UUID(operation.id), managed_verification_json=proof.model_dump_json()
        )
    except Exception:
        # An ordinary deployment remains truthful; insufficient proof forces
        # the next new request through the existing full procedure.
        measurement.counts["noop_reuse_proof_unavailable"] = 1


def try_verified_noop(
    service: McpWorkspaceDevelopmentService,
    operation: McpWorkspaceDeploymentOperationRow,
    digest: str,
    measurement: DeploymentMeasurement,
    recheck_authority: Callable[[Session], object] | None,
) -> WorkspaceDeploymentResult | None:
    if service._managed_noop_bridge is None or recheck_authority is None:
        return None
    finalizing = False
    try:
        with service._sessions() as session:
            prior = latest_verified_workspace_deployment(
                session,
                workspace_id=operation.workspace_id,
                target_id=operation.target_id,
                source_id=operation.development_source_id,
                generation=operation.generation,
                exclude_operation_id=operation.id,
            )
            # Do not bypass newer history from another workspace for this add-on.
            latest = session.scalar(
                select(McpWorkspaceDeploymentOperationRow.id)
                .where(
                    McpWorkspaceDeploymentOperationRow.target_id == operation.target_id,
                    McpWorkspaceDeploymentOperationRow.addon_id == operation.addon_id,
                    McpWorkspaceDeploymentOperationRow.id != operation.id,
                )
                .order_by(McpWorkspaceDeploymentOperationRow.requested_at.desc())
                .limit(1)
            )
            if (
                prior is None
                or latest != prior.id
                or prior.managed_verification_json is None
            ):
                return None
            expected = ManagedDeploymentVerification.model_validate_json(
                prior.managed_verification_json
            )
            session.expunge(prior)
        descriptor = service._workspace_descriptor(UUID(operation.workspace_id))
        if copied_video_plugin_unsupported_reason(descriptor) is not None:
            return None
        observation, profile, container = service._runtime_context(
            UUID(operation.target_id)
        )
        del observation
        if (
            container != expected.container_id
            or prior.runtime_revision_id != operation.runtime_revision_id
        ):
            return None
        source = measurement.run(
            "noop_source_capture",
            lambda: capture_workspace_deployment_tree(
                service._data_root, UUID(operation.workspace_id)
            ),
        )
        destination = expected_destination_digest(
            source, runtime_uid=profile.runtime_uid, runtime_gid=profile.runtime_gid
        )
        with service._sessions() as session:
            workspace, _validation, _target = service._deployment_preflight_rows(
                session,
                UUID(operation.workspace_id),
                operation.generation,
                UUID(operation.target_id),
            )
        if (
            source.digest != prior.source_manifest_digest
            or source.digest.removeprefix("sha256:")
            != (workspace.current_tree_digest or "").removeprefix("sha256:")
            or destination != prior.verified_destination_digest
        ):
            return None
        current = _context(service, prior, measurement)
        identity_fields = (
            "container_id",
            "runtime_revision_id",
            "bridge_generation",
            "bridge_identity_digest",
            "source_manifest_digest",
            "destination_manifest_digest",
        )
        if any(
            getattr(current, field) != getattr(expected, field)
            for field in identity_fields
        ):
            return None
        # Normal lease reservation invalidates the observation epoch once.
        # Any additional lifecycle/runtime change makes this request ineligible.
        if current.runtime_state_revision != expected.runtime_state_revision + 1:
            return None
        verified = measurement.run(
            "noop_destination_readback",
            lambda: service._broker.verify_mcp_workspace_destination(
                deployment_operation_id=prior.id,
                managed_instance_id=operation.target_id,
                expected_container_id=container,
            ),
        )
        if (
            not verified.matched
            or not verified.video_plugin_descriptor_eligible
            or verified.container_id != container
            or verified.addon_id != operation.addon_id
            or verified.addon_version != operation.addon_version
            or verified.runtime_revision_id != operation.runtime_revision_id
            or verified.development_source_id != operation.development_source_id
        ):
            return None
        service._failure("noop_after_destination_readback")
        confirmation = measurement.run(
            "noop_source_confirmation",
            lambda: capture_workspace_deployment_tree(
                service._data_root, UUID(operation.workspace_id)
            ),
        )
        if confirmation.canonical_json != source.canonical_json:
            return None
        final = _context(service, prior, measurement)
        if final.runtime_state_revision != current.runtime_state_revision or any(
            getattr(final, field) != getattr(current, field)
            for field in identity_fields
        ):
            return None
        finalizing = True
        service._recheck_deployment(operation)
        if service._exact_running_container(UUID(operation.target_id)) != container:
            return None
        proof = final.model_copy(
            update={
                # Existing exact source/destination equality and fresh readback
                # prove zero independently of any historical advisory.
                "destination_only_count": 0,
                "reused_deployment_id": UUID(prior.id),
                "native_restart_count": 0,
                "bytes_staged": 0,
                "bytes_transferred": 0,
                "source_files_scanned": sum(
                    entry.entry_type == "file" for entry in source.entries
                )
                * 2,
                "source_bytes_hashed": source.included_bytes * 2,
                "docker_readback_bytes": verified.readback_bytes,
            }
        )
        with service._sessions.begin() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = session.get(McpWorkspaceDeploymentOperationRow, operation.id)
            instance = session.get(ManagedInstanceRow, operation.target_id)
            trust = session.get(ManagedBridgeTrustRow, operation.target_id)
            lease = ManagedInstanceMutationLeaseRepository(session).current(
                UUID(operation.target_id)
            )
            heavy = GlobalHeavyOperationLeaseRepository(session).current()
            now = timestamp(service._clock())
            if (
                row is None
                or row.state != "running"
                or row.mutation_started
                or instance is None
                or instance.runtime_revision_id != operation.runtime_revision_id
                or instance.runtime_state_revision != proof.runtime_state_revision
                or trust is None
                or trust.state != "active"
                or not trust.secret_reference
                or trust.generation != proof.bridge_generation
                or trust.runtime_revision_id != operation.runtime_revision_id
                or trust.container_id != container
                or not trust.identity_json
                or "sha256:" + hashlib.sha256(trust.identity_json.encode()).hexdigest()
                != proof.bridge_identity_digest
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
            ):
                from kodi_lab.application.mcp_workspace_development import (
                    McpWorkspaceDevelopmentError,
                )

                raise McpWorkspaceDevelopmentError("deployment_conflict")
            recheck_authority(session)
            service._session_admission(
                session,
                target_id=operation.target_id,
                revision_id=operation.runtime_revision_id,
                workspace_id=operation.workspace_id,
                source_id=operation.development_source_id,
                container_id=container,
            )
            row.original_container_id = container
            row.original_runtime_state_revision = proof.runtime_state_revision
            row.source_manifest_digest = source.digest
            row.verified_destination_digest = destination
            row.inventory_classification = "matched"
            row.addon_enabled = True
            row.result_classification = "verified_existing"
            row.managed_verification_json = proof.model_dump_json()
            service._finish_deployment(
                operation.id, "succeeded", None, digest=digest, session=session
            )
        measurement.counts.update(
            deployment_work_skipped=1,
            kodi_stops=0,
            kodi_starts=0,
            transferred_payload_bytes=0,
            staged_payload_bytes=0,
            source_files_scanned=sum(
                entry.entry_type == "file" for entry in source.entries
            )
            * 2,
            source_bytes_hashed=source.included_bytes * 2,
        )
        return service._deployment_result(
            service._deployment_required(UUID(operation.id))
        )
    except Exception:
        if finalizing:
            raise
        # No destination writes or native actions occurred. The ordinary path
        # retains its existing drift/reconciliation and failure semantics.
        measurement.counts["noop_ineligible"] = 1
        return None
